#!/usr/bin/env python3
"""Package an already-tested local image as a versioned, offline release."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def run(argv):
    return subprocess.run(argv, check=True, text=True, capture_output=True, timeout=600).stdout.strip()


def package(image, output, engine):
    if engine not in ('docker', 'podman'):
        raise ValueError('CONTAINER_ENGINE must be docker or podman')
    revision = run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'])
    if run(['git', '-C', str(ROOT), 'status', '--porcelain']):
        raise ValueError('release requires a clean checkout')
    version = tomllib.loads((ROOT / 'backend/pyproject.toml').read_text())['project']['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('release requires an explicit three-part version')
    info = json.loads(run([engine, 'image', 'inspect', image]))[0]
    image_id = 'sha256:' + info['Id'].removeprefix('sha256:')
    labels = info['Config'].get('Labels') or {}
    if (labels.get('org.opencontainers.image.revision') != revision
            or labels.get('org.opencontainers.image.version') != version):
        raise ValueError('image labels do not match this checkout; build with SOURCE_REVISION and RELEASE_VERSION')
    # Read version from the image too; a tag or a build label alone is not evidence.
    code = ('import json,tomllib; from tracker import storage; '
            'p=tomllib.load(open("/app/backend/pyproject.toml","rb")); '
            'print(json.dumps([p["project"]["version"], storage.FORMAT]))')
    image_version, storage = json.loads(run([engine, 'run', '--rm', '--network', 'none', '--read-only',
        '--cap-drop=all', '--security-opt=no-new-privileges', '--entrypoint', '/opt/venv/bin/python',
        image_id, '-c', code]))
    if image_version != version:
        raise ValueError('image version does not match this checkout')
    output = Path(output)
    output.mkdir(mode=0o700)  # Never replace an existing release.
    archive = output / 'image.tar'
    run([engine, 'save', '--output', str(archive), image_id])
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    manifest = dict(version=version, revision=revision, image=image_id, storage=storage,
                    platform=info['Os'] + '/' + info['Architecture'], sha256=digest)
    for name in ('upgrade.py', 'deployment.py', 'install.py', 'maintain.py'):
        shutil.copyfile(ROOT / 'deploy' / name, output / name)
    manual = (ROOT / 'docs/deployment.md').read_text()
    # The offline bundle has no parent checkout. Keep repository references
    # identifiable without publishing broken links outside the bundle.
    manual = re.sub(r'\[([^\]]+)\]\(\.\./([^)]+)\)',
                    r'\1 (`\2` in the source checkout)', manual)
    (output / 'README.md').write_text(manual)
    shutil.copyfile(ROOT / 'deploy/quadlet/openruyi-monitor.container.in', output / 'openruyi-monitor.container.in')
    (output / 'release.json').write_text(json.dumps(manifest, indent=2) + '\n')
    sums = []
    for path in sorted(output.iterdir()):
        with path.open('rb') as stream:
            sums.append(hashlib.file_digest(stream, 'sha256').hexdigest() + '  ' + path.name)
    (output / 'SHA256SUMS').write_text('\n'.join(sums) + '\n')
    return output.resolve()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(package(args.image, args.output, os.environ.get('CONTAINER_ENGINE', 'podman')))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f'release failed: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
