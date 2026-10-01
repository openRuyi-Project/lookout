"""Prepare release-owned catalog migration; never modify the mounted old config."""
import json
from pathlib import Path
import tempfile
import uuid

from deployment import (LABEL, PROTECTION, PYTHON, export_image_tree, image_command,
                        resolve_image, run, validate_catalog_baseline)
from install import config_archive

CATALOG_STATUS = '''import hashlib,json
from pathlib import Path
from tracker import config
c=config.load('/config/tracker.toml')
paths=[c[k] for k in ('nvpath','packages_path','distribution_path') if c.get(k)]
print(json.dumps({'local':any(Path(p).is_relative_to('/config') for p in paths),
                  'inputs':c['input_hashes'],
                  'private_files':{str(p.relative_to('/config')):hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in Path('/config').rglob('*') if p.is_file()}}))
'''

# This helper owns only a newly created Docker volume, never the old config/data.
VOLUME_BASELINE = '''import os, sys, tarfile
from pathlib import Path
root=Path('/prepared')
if list(root.iterdir()):
    raise SystemExit('refusing nonempty preparation volume')
baseline=root/'baseline'; baseline.mkdir()
with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as archive:
    archive.extractall(baseline,filter='data')
# Relinquish the private parent last: this helper has CHOWN, not DAC_OVERRIDE.
for path in reversed([root,*root.rglob('*')]):
    path.chmod(0o700 if path.is_dir() else 0o600)
    os.chown(path,10001,10001)
'''
VOLUME_PUBLISH = '''import os,shutil
from pathlib import Path
root=Path('/prepared'); config=root/'config'
shutil.rmtree(root/'baseline')
for path in config.iterdir():
    path.rename(root/path.name)
config.rmdir()
fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
try: os.fsync(fd)
finally: os.close(fd)
'''


def mount_preparation(command, arguments):
    index = command.index('--entrypoint')
    return command[:index] + arguments + command[index:]


def prepare(service, image, transaction, baseline_image):
    status = json.loads(run(image_command(service, image, '-c', CATALOG_STATUS)))
    previous = service.settings['config']
    result = {'ownership': 'operator' if status['local'] else 'release',
              'previous': str(previous), 'prepared': str(previous)}
    if not status['local'] or not baseline_image:
        return previous, result
    validate_catalog_baseline(baseline_image)
    baseline_manifest = resolve_image(service.engine, baseline_image)
    with tempfile.TemporaryDirectory(prefix='.catalog-baseline-', dir=transaction) as temporary:
        baseline = Path(temporary) / 'config'
        export_image_tree(service.engine, baseline_manifest['image'], '/app/config', baseline)
        if service.engine == 'docker':
            prepared = service.name + '-config-' + uuid.uuid4().hex[:12]
            run(['docker', 'volume', 'create', '--label', f'{LABEL}={service.instance}', prepared])
            mount = ['--mount', f'type=volume,src={prepared},dst=/prepared,volume-nocopy']
            run(['docker', 'run', '--rm', '-i', '--network', 'none', *PROTECTION,
                 '--user', '0', '--cap-add=CHOWN', *mount, '--entrypoint', PYTHON,
                 image, '-c', VOLUME_BASELINE], input=config_archive(baseline))
            baseline_mount = []
            baseline_path = '/prepared/baseline'
        else:
            parent = previous.parent / ('config-' + uuid.uuid4().hex[:12])
            parent.mkdir(mode=0o700)
            prepared = parent / 'config'
            mount = ['-v', f'{parent}:/prepared:Z']
            baseline_mount = ['-v', f'{baseline}:/baseline:ro,Z']
            baseline_path = '/baseline'
        command = image_command(service, image, '/app/deploy/migrate-config.py',
                                '--baseline', baseline_path, '--source', '/config',
                                '--destination', '/prepared/config')
        report = json.loads(run(mount_preparation(command, mount + baseline_mount)))
        if service.engine == 'docker':
            command = image_command(service, image, '-c', VOLUME_PUBLISH)
            run(mount_preparation(command, mount))
        current = json.loads(run(image_command(service, image, '-c', CATALOG_STATUS)))
        if current != status:
            raise ValueError('operator configuration changed during catalog preparation')
    return prepared, {**result, 'ownership': 'release', 'prepared': str(prepared),
                      'baseline_image': baseline_manifest['image'], 'migration': report}
