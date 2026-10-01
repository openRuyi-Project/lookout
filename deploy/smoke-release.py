#!/usr/bin/env python3
"""Exercise Docker installation, reconfiguration, upgrade and recovery offline."""
import argparse
import importlib.util
import json
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid

from deployment import Docker, PROTECTION, PYTHON, healthy, image_command, resolve_image, run
from install import install
from maintain import backup, configure, status
from upgrade import upgrade


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def catalog_upgrade(smoke, base, root, extra_images, volumes, *, copied=False):
    images = []
    for phase in ('before', 'next'):
        tag = smoke.prefix + ':catalog-' + ('copied-' if copied else '') + phase
        extra_images.append(tag)
        definition = (f'FROM {base}\nUSER 0\nRUN TRACKER_SPEC_REPO= /opt/venv/bin/python -c '
                      f'"from tests.container_smoke_fixture import release_catalog; release_catalog(\'{phase}\')"\n'
                      'USER 10001:10001\n')
        if copied and phase == 'before':
            legacy = ('import shutil\ndef initialize(source,destination):\n'
                      '    return shutil.copytree(source,destination)\n')
            code = ('from pathlib import Path; '
                    f'Path("/app/deploy/init-config.py").write_text({legacy!r}); '
                    'Path("/app/deploy/release-upgrade.py").write_text("raise SystemExit(41)\\n")')
            definition = definition.replace('USER 10001:10001\n', 'RUN ' + PYTHON + ' -c ' + shlex.quote(code) + '\nUSER 10001:10001\n')
        subprocess.run(['docker', 'build', '--network', 'none', '--pull=false', '-t', tag, '-'],
                       input=definition, text=True, check=True)
        images.append(resolve_image('docker', json.loads(run(['docker', 'image', 'inspect', tag]))[0]['Id'])['image'])
    name = smoke.prefix + ('-copied-catalog' if copied else '-catalog')
    smoke.containers.append(name)
    volumes.update(name + '-' + role for role in ('config', 'data', 'backups'))
    install(images[0], name, network='none', memory='4g', cpus=2, environment=['TRACKER_SPEC_REPO='])
    before = Docker(name)
    before.stop()
    if copied:
        edited = '''from pathlib import Path
import tomlkit
p=Path('/config'); n=p/'nvchecker.toml'; v=tomlkit.parse(n.read_text())
v['catalog-local']={'source':'cmd','cmd':"printf '1.2.3\\n'"}
v['__config__']={'keyfile':'keys.toml'}; n.write_text(tomlkit.dumps(v))
k=p/'keys.toml'; k.write_text('# private fixture keyfile\\n'); k.chmod(0o600)
f=p/'packages.toml'; v=tomlkit.parse(f.read_text())
v['catalog-stable']['monitors']['eol']={'product':'operator-cycle','cycle_parts':1}
f.write_text(tomlkit.dumps(v))
f=p/'tracker.toml'; v=tomlkit.parse(f.read_text()); v['obs']['project']='operator-project'
f.write_text(tomlkit.dumps(v))
'''
        command = image_command(before, images[0], '-c', edited)
        # Only this test's new config volume is writable to the fixture editor.
        command = [value.removesuffix(',readonly') if 'dst=/config,' in value else value
                   for value in command]
        run(command)
    # Seed dated, synthetic observations through the real writer. Subsequent
    # entrypoint heartbeats must reuse the unchanged query and refresh the edit.
    seed = '''from pathlib import Path
import json
from tracker import config, state
from tracker.monitors import runner
c=config.load('/config/tracker.toml')
p=Path('/data/state/tracker.sqlite3'); s=state.read(p); now=state.utcnow()
s.update(bindings=c['packages'], native_ids=list(c['native']), stale_after_seconds=7200)
for name in c['native']:
    s['inventory'][name]=name
    s['sources'][name]=state.success({}, {'version':'1.2.3', 'srcmd5':'fixture'}, now)
    s['specs'][name]=state.success({}, {'metadata':{'version':'1.2.3'},
        'native_query':{'spec_sha256':'a'*64}, 'head':'b'*40}, now)
    observation=runner.plan(c,s,name,'security')
    observation.update(status='ok', checked_at=now, observed_at=now, fetched_at=now, error=None, input_note=None)
    s['monitors'].setdefault(name,{})['security']=observation
state.commit(p,s)
print(json.dumps({'stable':s['monitors']['catalog-stable']['security'],
 'files':{str(f):f.read_text() for f in Path('/config').rglob('*') if f.is_file()}}))
'''
    saved = json.loads(run(image_command(before, images[0], '-c', seed)))
    before.start()
    healthy('docker', name, images[0])
    if copied:
        failed = smoke.prefix + ':catalog-failed'
        extra_images.append(failed)
        run(['docker', 'tag', images[1], failed])
        subprocess.run(['docker','build','--network','none','--pull=false','-t',failed,'-'],
                       input=f'FROM {failed}\nENTRYPOINT ["/bin/sh","-c","exit 2"]\n',text=True,check=True)
        try:
            upgrade(json.loads(run(['docker','image','inspect',failed]))[0]['Id'], None, root, container=name, apply=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError('failed catalog/image pair was accepted')
        restored = Docker(name)
        assert restored.settings['config'] == before.settings['config']
        assert restored.settings['data'] == before.settings['data']
        healthy('docker', name, images[0])
        print('PASS copied catalog rollback: exact old image/config pair, same database',flush=True)
    result = upgrade(images[1], None, root, container=name, apply=True)
    assert result['status'] == 'ready'
    after = Docker(name)
    assert after.settings['data'] == before.settings['data']
    assert (after.settings['config'] != before.settings['config']) == copied
    check = '''import json
from pathlib import Path
from tracker import config,state
c=config.load('/config/tracker.toml'); s=state.read('/data/state/tracker.sqlite3')
assert 'catalog-added' in c['native'] and 'catalog-added' in c['packages']
assert c['packages']['catalog-changed']['monitors']['security']['product']=='corrected-fixture'
assert not Path('/config/packages.toml').exists()
assert not Path('/config/nvchecker.toml').exists()
print(json.dumps({'stable':s['monitors']['catalog-stable']['security'],
 'files':{str(f):f.read_text() for f in Path('/config').rglob('*') if f.is_file()}}))
'''
    observed = json.loads(run(['docker', 'exec', name, PYTHON, '-c', check]))
    if copied:
        verify = '''from pathlib import Path
from tracker import config
c=config.load('/config/tracker.toml')
assert c['obs']['project']=='operator-project'
assert c['native']['catalog-local']['source']=='cmd'
assert c['native_options']['keyfile']=='keys.toml'
assert c['packages']['catalog-stable']['monitors']['eol']['product']=='operator-cycle'
assert Path('/config/keys.toml').read_text()=='# private fixture keyfile\\n'
assert Path('/config/keys.toml').stat().st_mode & 0o777 == 0o600
'''
        run(['docker','exec',name,PYTHON,'-c',verify])
        assert result['catalogs']['baseline_image']==images[0]
        original = json.loads(run(image_command(before,images[0],'-c',
            "import json; from pathlib import Path; print(json.dumps({str(p):p.read_text() for p in Path('/config').rglob('*') if p.is_file()}))")))
        assert original == saved['files']
        assert upgrade(images[1],None,root,container=name,apply=True)['status']=='unchanged'
    else:
        assert observed['files'] == saved['files']
    assert observed['stable'] == saved['stable']
    print('PASS '+('copied catalog migration: original image provenance; preserved overrides/credentials, same database and stable observation' if copied
                  else 'catalog upgrade: added/corrected identities and rules; unchanged config/data volumes and stable observation'), flush=True)


def exercise(image, root):
    image = json.loads(run(['docker', 'image', 'inspect', image]))[0]['Id']
    smoke = module('smoke-image').Smoke('docker', image)
    name = smoke.prefix + '-installed'
    base = smoke.prefix + ':base'
    extra_images, volumes = [base], set()
    try:
        # Dockerfile FROM resolves image references, not the engine's image ID.
        run(['docker', 'tag', image, base])
        fixture = smoke.fixture('seeded')
        helper = smoke.start('source', fixture)
        smoke.wait_live(helper)
        config = root / 'config'
        config.mkdir(mode=0o700)
        run(['docker', 'cp', helper + ':/config/.', str(config)])
        for path in config.iterdir():
            path.chmod(0o600)
        initial = resolve_image('docker', image)
        port = free_port()
        smoke.containers.append(name)
        volumes.update(name + '-' + role for role in ('config', 'data', 'backups'))
        result = install(initial['image'], name, config=config, port=port, memory='4g', cpus=2,
                         environment=['TRACKER_SPEC_REPO='])
        assert result['status'] == 'ready'
        service = Docker(name)
        original_data = service.settings['data']
        assert service.info['HostConfig']['PortBindings']['8080/tcp'][0] == {
            'HostIp': '127.0.0.1', 'HostPort': str(port)}
        assert service.info['HostConfig']['Memory'] == 4 * 1024 ** 3
        assert service.info['Config']['User'] == '10001:10001'
        # Seed through the actual SQLite writer in the stopped test instance.
        service.stop()
        seed = '''from pathlib import Path
from tracker import state
p=Path('/data/state/tracker.sqlite3')
s=state.read(p)
s['inventory']['release-fixture']='release-fixture'
s['sources']['release-fixture']=state.success({}, {'version':'1.2.3'}, state.utcnow())
state.commit(p,s)
'''
        run(['docker', 'run', '--rm', '--network', 'none', *PROTECTION,
             '--mount', f'type=volume,src={original_data},dst=/data,volume-nocopy',
             '--entrypoint', PYTHON, image, '-c', seed])
        service.start()
        healthy('docker', name, initial['image'])
        print('PASS install: default UID, private host config, custom loopback port, explicit persistent volume', flush=True)

        config_file = config / 'tracker.toml'
        config_file.write_text(config_file.read_text().replace('timeout_seconds = 1', 'timeout_seconds = 2'))
        next_port = free_port()
        configure(service, config, next_port)
        service = Docker(name)
        volumes.add(service.settings['config'])
        assert service.settings['data'] == original_data
        assert service.info['HostConfig']['PortBindings']['8080/tcp'][0]['HostPort'] == str(next_port)
        observed_config = run(['docker', 'exec', name, PYTHON, '-c',
            'from pathlib import Path; print(Path("/config/tracker.toml").read_text())'])
        assert 'timeout_seconds = 2' in observed_config
        saved = root / 'saved.sqlite3'
        backup(service, saved)
        assert saved.stat().st_mode & 0o777 == 0o600
        assert status(service, root, 26)['ok']
        print('PASS configure: port and config changed, data identity unchanged; online backup verified', flush=True)

        # Different image identities exercise replacement, not just a container restart.
        for label, instruction in [('next', 'LABEL org.openruyi.test=next'),
                                    ('failed', 'ENTRYPOINT ["/bin/sh", "-c", "exit 2"]')]:
            tag = smoke.prefix + ':' + label
            extra_images.append(tag)
            subprocess.run(['docker', 'build', '--network', 'none', '--pull=false', '-t', tag, '-'],
                           input=f'FROM {base}\n{instruction}\n', text=True, check=True)
            ident = json.loads(run(['docker', 'image', 'inspect', tag]))[0]['Id']
            manifest = resolve_image('docker', ident)
            if label == 'next':
                outcome = upgrade(manifest['image'], None, root, container=name, apply=True)
                assert outcome['status'] == 'ready'
                upgraded = Docker(name)
                assert upgraded.settings['data'] == original_data
                assert upgraded.settings['config'] == service.settings['config']
                assert upgraded.info['HostConfig']['PortBindings'] == service.info['HostConfig']['PortBindings']
                assert upgraded.info['HostConfig']['Memory'] == service.info['HostConfig']['Memory']
                expected = manifest['image']
                print('PASS upgrade: changed image; same port, limits, config, data and observed version', flush=True)
            else:
                try:
                    upgrade(manifest['image'], None, root, container=name, apply=True)
                except (RuntimeError, ValueError):
                    pass
                else:
                    raise AssertionError('failing image was accepted')
                healthy('docker', name, expected)
                print('PASS failed upgrade: compatible previous image resumed without restoring old data', flush=True)
        code = ('from tracker import state; '
                'assert state.read("/data/state/tracker.sqlite3")["sources"]["release-fixture"]["version"]=="1.2.3"')
        run(['docker', 'exec', name, PYTHON, '-c', code])
        # Restore into a new test volume; never overwrite the original instance.
        restore = smoke.prefix + '-restored'
        volumes.add(restore)
        run(['docker', 'volume', 'create', restore])
        loader = smoke.prefix + '-restore-loader'
        smoke.containers.append(loader)
        run(['docker', 'create', '--name', loader, '--network', 'none', '--user', '0',
             '--mount', f'type=volume,src={restore},dst=/data,volume-nocopy',
             '--entrypoint', PYTHON, image, '-c',
             'import os; os.chown("/data/restored.sqlite3",10001,10001)'])
        run(['docker', 'cp', str(saved), loader + ':/data/restored.sqlite3'])
        run(['docker', 'start', '--attach', loader])
        restore_code = ('from tracker import state; import sqlite3; '
                        'p="/data/restored.sqlite3"; '
                        'assert sqlite3.connect("file:"+p+"?mode=ro",uri=True).execute("pragma integrity_check").fetchone()[0]=="ok"; '
                        'assert state.read(p)["sources"]["release-fixture"]["version"]=="1.2.3"')
        run(['docker', 'run', '--rm', '--network', 'none', *PROTECTION,
             '--mount', f'type=volume,src={restore},dst=/data,readonly,volume-nocopy',
             '--entrypoint', PYTHON, image, '-c', restore_code])
        print('PASS restore: independent volume, SQLite integrity and source observation retained', flush=True)
        catalog_upgrade(smoke, base, root, extra_images, volumes)
        catalog_upgrade(smoke, base, root, extra_images, volumes, copied=True)
    finally:
        for container in smoke.containers:
            log = subprocess.run(['docker', 'logs', container], capture_output=True, text=True)
            (root / (container + '.log')).write_text(log.stdout + log.stderr)
            subprocess.run(['docker', 'rm', '-f', container], capture_output=True)
        # Include retained config and stopped transaction containers owned by this test only.
        for container in run(['docker', 'ps', '-aq', '--filter', 'name=' + smoke.prefix]).split():
            run(['docker', 'rm', '-f', container])
        for volume in run(['docker', 'volume', 'ls', '--format', '{{.Name}}']).splitlines():
            if volume.startswith(smoke.prefix):
                volumes.add(volume)
        for volume in volumes | set(smoke.volumes):
            subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True)
        for tag in extra_images:
            subprocess.run(['docker', 'image', 'rm', tag], capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    parser.add_argument('--output', type=Path, help='new directory retaining logs and test backups')
    args = parser.parse_args()
    try:
        if args.output:
            args.output.mkdir(mode=0o700)
            exercise(args.image, args.output)
        else:
            with tempfile.TemporaryDirectory(prefix='openruyi-release-smoke-') as directory:
                exercise(args.image, Path(directory))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, AssertionError) as error:
        print(f'release smoke failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
