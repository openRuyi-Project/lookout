#!/usr/bin/env python3
"""Install a release on Docker with private, explicitly named persistent volumes."""
import argparse
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deployment import LABEL, PROTECTION, PYTHON, Docker, healthy, image_command, load_image, read_release, run

PREPARE = '''import os, shutil, sys, tarfile
from pathlib import Path
from tracker.config import load
roots = [Path(p) for p in sys.argv[2:]]
if any(list(p.iterdir()) for p in roots):
    raise SystemExit('refusing nonempty installation volumes')
if sys.argv[1] == 'image':
    shutil.copytree('/app/config', '/config', dirs_exist_ok=True)
else:
    with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
        archive.extractall('/config', filter='data')
load(Path('/config/tracker.toml'))
for root in roots:
    for path in reversed([root, *root.rglob('*')]):
        path.chmod(0o700 if path.is_dir() else 0o600)
        os.chown(path, 10001, 10001)
'''


def config_archive(directory):
    if not directory.is_dir() or not (directory / 'tracker.toml').is_file():
        raise ValueError('configuration directory must contain tracker.toml')
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for path in sorted(directory.rglob('*')):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError('configuration must contain only regular files and directories')
            if path.is_file():
                archive.add(path, arcname=path.relative_to(directory), recursive=False)
    return stream.getvalue()


def install(directory, name, *, config=None, port=18730, network='bridge', environment=(), memory='8g', cpus=4.0, pids=512):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
        raise ValueError('invalid container name')
    for value in environment:
        key, sep, _ = value.partition('=')
        if (not sep or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', key)
                or key in {'HOST', 'PORT', 'API_PORT', 'TRACKER_DB', 'TRACKER_CONFIG'}):
            raise ValueError('environment cannot replace data/config/listener identities')
    if (not re.fullmatch(r'[1-9][0-9]*[bkmg]?', memory.lower())
            or not 0 < cpus <= 1024 or not 64 <= pids <= 65536
            or not 1 <= port <= 65535 or network not in ('bridge', 'none')):
        raise ValueError('invalid port or runtime resource budget')
    manifest = read_release(directory)
    load_image('docker', directory, manifest)
    volumes = {role: name + '-' + role for role in ('config', 'data', 'backups')}
    if run(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$']):
        raise ValueError('container already exists; use upgrade.py')
    existing = set(run(['docker', 'volume', 'ls', '--format', '{{.Name}}']).splitlines())
    if existing.intersection(volumes.values()):
        raise ValueError('persistent volume already exists; refusing to initialize over it')
    data = config_archive(config) if config else None
    instance = uuid.uuid4().hex
    for volume in volumes.values():
        run(['docker', 'volume', 'create', '--label', f'{LABEL}={instance}', volume])
    mounts = []
    for role, volume in volumes.items():
        target = '/backup' if role == 'backups' else '/' + role
        mounts += ['--mount', f'type=volume,src={volume},dst={target},volume-nocopy']
    # Only the three newly created volumes are visible to this one-time root helper.
    run(['docker', 'run', '--rm', '-i', '--network', 'none', *PROTECTION, '--user', '0',
         '--cap-add=CHOWN', *mounts, '--entrypoint', PYTHON, manifest['image'],
         '-c', PREPARE, 'archive' if config else 'image', '/config', '/data', '/backup'], input=data)
    command = ['docker', 'create', '--name', name, *PROTECTION,
               '--memory', memory, '--cpus', str(cpus), '--pids-limit', str(pids),
               '--restart', 'unless-stopped', '--stop-timeout', '20', '--network', network,
               '--log-driver', 'local', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
               '--label', f'{LABEL}={instance}',
               '--label', 'org.openruyi.environment=' + json.dumps([v.split('=', 1)[0] for v in environment]),
               '--label', 'org.openruyi.backups=' + volumes['backups'],
               '--mount', f'type=volume,src={volumes["config"]},dst=/config,readonly,volume-nocopy',
               '--mount', f'type=volume,src={volumes["data"]},dst=/data,volume-nocopy']
    if network != 'none':
        command += ['-p', f'127.0.0.1:{port}:8080']
    for value in environment:
        command += ['-e', value]
    run(command + [manifest['image']])
    service = Docker(name)
    run(image_command(service, manifest['image'], '-m', 'tracker.runtime_checks',
                      '--config', '/config/tracker.toml', '--db', '/data/state/tracker.sqlite3'))
    service.start()
    try:
        healthy('docker', name, manifest['image'])
    except BaseException:
        service.stop()
        raise
    return dict(container=name, image=manifest['image'], volumes=volumes, status='ready')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--name', default='openruyi-monitor')
    parser.add_argument('--config', type=Path, help='copy a reviewed host config; omit to initialize image defaults')
    parser.add_argument('--port', type=int, default=18730)
    parser.add_argument('--network', choices=['bridge', 'none'], default='bridge')
    parser.add_argument('--env', action='append', default=[])
    parser.add_argument('--memory', default='8g')
    parser.add_argument('--cpus', type=float, default=4)
    parser.add_argument('--pids-limit', type=int, default=512)
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.port <= 65535:
            raise ValueError('invalid loopback port')
        result = install(args.release.resolve(), args.name, config=args.config,
                         port=args.port, network=args.network, environment=args.env,
                         memory=args.memory, cpus=args.cpus, pids=args.pids_limit)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'installation failed: {error}; existing volumes were not deleted', file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
