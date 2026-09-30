#!/usr/bin/env python3
"""Install a registry image with private, explicitly named persistent volumes."""
import argparse
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deployment import (LABEL, PROTECTION, PYTHON, Docker, Quadlet, healthy, image_command,
                        image_reference, manager, no_data_users, resolve_image, run, service_action, write_exclusive)

PREPARE = '''import os, runpy, shutil, sys, tarfile, tempfile
from pathlib import Path
roots = [Path(p) for p in sys.argv[2:]]
if any(list(p.iterdir()) for p in roots):
    raise SystemExit('refusing nonempty installation volumes')
if sys.argv[1] == 'image':
    with tempfile.TemporaryDirectory() as temporary:
        prepared = Path(temporary) / 'config'
        runpy.run_path('/app/deploy/init-config.py')['initialize']('/app/config', prepared)
        shutil.copytree(prepared, '/config', dirs_exist_ok=True)
else:
    with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
        archive.extractall('/config', filter='data')
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


def install(reference, name, *, config=None, port=18730, network='bridge', environment=(), memory='8g', cpus=4.0, pids=512,
            engine='docker', directory=None, data=None, auto_update=None):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
        raise ValueError('invalid container name')
    for value in environment:
        key, sep, _ = value.partition('=')
        if (not sep or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', key)
                or key in {'HOST', 'PORT', 'API_PORT', 'TRACKER_DB', 'TRACKER_CONFIG'}):
            raise ValueError('environment cannot replace data/config/listener identities')
    if (not re.fullmatch(r'[1-9][0-9]*[bkmg]?', memory.lower())
            or not 0 < cpus <= 1024 or not 64 <= pids <= 65536
            or not 1 <= port <= 65535):
        raise ValueError('invalid port or runtime resource budget')
    if engine == 'podman':
        return install_podman(reference, name, directory, config=config, data=data, port=port,
                              network=network, environment=environment, memory=memory, cpus=cpus,
                              pids=pids, auto_update=auto_update)
    if engine != 'docker' or network not in ('bridge', 'none') or data or directory or auto_update:
        raise ValueError('Docker uses named volumes; automation/directory options are for rootless Podman')
    manifest = resolve_image('docker', reference)
    volumes = {role: name + '-' + role for role in ('config', 'data', 'backups')}
    if run(['docker', 'ps', '-aq', '--filter', 'name=^/' + re.escape(name) + '$']):
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
         '--cap-add=CHOWN', *mounts, *[arg for value in environment for arg in ('-e', value)],
         '--entrypoint', PYTHON, manifest['image'],
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


def install_podman(reference, name, directory, *, config, data, port, network, environment, memory, cpus, pids, auto_update):
    if os.geteuid() == 0 or run(['podman', 'info', '--format', '{{.Host.Security.Rootless}}']) != 'true':
        raise ValueError('use the non-root Rootless Podman service owner')
    if auto_update:
        if not image_reference(auto_update):
            raise ValueError('automatic updates require a registry channel, not a local image ID')
        if run(['loginctl', 'show-user', str(os.geteuid()), '--property=Linger', '--value']) != 'yes':
            raise ValueError('enable user linger before unattended installation: loginctl enable-linger USER')
    root = directory.resolve() if directory else Path.home() / '.local/share/lookout'
    unit_root = Path.home() / '.config/containers/systemd'
    unit = unit_root / (name + '.container')
    generator = next((p for p in (Path('/usr/lib/systemd/system-generators/podman-system-generator'),
                                 Path('/usr/libexec/podman/quadlet')) if p.is_file()), None)
    if generator is None:
        raise ValueError('install Podman with its Quadlet systemd generator')
    values = [str(root), str(data or ''), network, *environment]
    if network == 'host' or any(any(c.isspace() or c in "'\"%" for c in value) for value in values):
        raise ValueError('use a private network and unit values without whitespace, quotes or % specifiers')
    if ':' in str(root) or data and (':' in str(data) or not data.is_dir()):
        raise ValueError('use existing local data and paths without colons')
    for path in (unit, root / 'config', root / 'backups', root / 'units', *([] if data else [root / 'data'])):
        if os.path.lexists(path):
            raise ValueError('installation destination exists; do not initialize over an existing instance: ' + str(path))
    if run(['podman', 'ps', '-aq', '--filter', 'name=^' + re.escape(name) + '$']):
        raise ValueError('container name exists; use upgrade.py')
    manifest = resolve_image('podman', reference)
    if config:
        config_archive(config)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for role in ('config', 'backups', 'units'):
        (root / role).mkdir(mode=0o700)
    data = data.resolve() if data else root / 'data'
    data.mkdir(mode=0o700, exist_ok=True)
    if config:
        shutil.copytree(config, root / 'config', dirs_exist_ok=True)
    else:
        run(['podman', 'run', '--rm', '--network', 'none', *PROTECTION,
             '--userns=keep-id:uid=10001,gid=10001', '-v', f'{root / "config"}:/bootstrap:Z',
             *[arg for value in environment for arg in ('-e', value)],
             '--entrypoint', PYTHON, manifest['image'], '-c',
             'import runpy,shutil,tempfile; from pathlib import Path; '
             'd=tempfile.TemporaryDirectory(); p=Path(d.name)/"config"; '
             'runpy.run_path("/app/deploy/init-config.py")["initialize"]("/app/config",p); '
             'shutil.copytree(p,"/bootstrap",dirs_exist_ok=True)'])
    for path in (root / 'config').rglob('*'):
        path.chmod(0o700 if path.is_dir() else 0o600)
    template = Path(__file__).parent / 'quadlet/openruyi-lookout.container.in'
    text = template.read_text()
    for key, value in {'IMAGE': manifest['image'], 'CONFIG_DIR': str(root / 'config'), 'DATA_DIR': str(data)}.items():
        text = text.replace('@' + key + '@', value)
    text = (text.replace('ContainerName=openruyi-lookout', 'ContainerName=' + name)
            .replace('127.0.0.1:18730:8080', f'127.0.0.1:{port}:8080')
            .replace('Memory=8g', 'Memory=' + memory).replace('PidsLimit=512', 'PidsLimit=' + str(pids))
            .replace('CPUQuota=400%', 'CPUQuota=' + str(cpus * 100) + '%'))
    text = text.replace('StopTimeout=20', '\n'.join(['Network=' + network, *['Environment=' + v for v in environment], 'StopTimeout=20']))
    temporary_unit = root / 'units' / unit.name
    write_exclusive(temporary_unit, text.encode())
    service = Quadlet(temporary_unit)
    no_data_users(service)
    run(image_command(service, manifest['image'], '-m', 'tracker.runtime_checks',
                      '--config', '/config/tracker.toml', '--db', '/data/state/tracker.sqlite3'))
    run([str(generator), '--user', '--dryrun'], env={**os.environ, 'QUADLET_UNIT_DIRS': str(root / 'units')})
    unit_root.mkdir(parents=True, exist_ok=True)
    write_exclusive(unit, text.encode())
    service = Quadlet(unit)
    manager('Reload')
    try:
        service.start()
        healthy('podman', name, manifest['image'])
    except BaseException:
        service.stop()
        raise
    if auto_update:
        install_automation(name, root, unit, auto_update)
    return dict(container=name, image=manifest['image'], unit=str(unit),
                config=str(root / 'config'), data=str(data), backups=str(root / 'backups'), status='ready')


def install_automation(name, root, unit, channel):
    tools = Path(__file__).resolve().parent
    user_units = Path.home() / '.config/systemd/user'
    user_units.mkdir(parents=True, exist_ok=True)
    values = {'TOOLS': str(tools), 'UNIT': str(unit), 'BACKUPS': str(root / 'backups'), 'IMAGE': channel}
    paths = [user_units / (name + '-' + suffix) for suffix in ('update.service', 'update.timer', 'backup.service', 'backup.timer')]
    if any(os.path.lexists(path) for path in paths):
        raise ValueError('automation units already exist; review them instead of overwriting')
    for path in paths:
        text = (tools / 'systemd' / path.name.removeprefix(name + '-')).read_text()
        for key, value in values.items():
            text = text.replace('@' + key + '@', value)
        write_exclusive(path, text.encode())
    manager('Reload')
    timers = [name + '-' + role + '.timer' for role in ('update', 'backup')]
    manager('EnableUnitFiles', 'asbb', str(len(timers)), *timers, 'false', 'false')
    for timer in timers:
        service_action(timer, 'StartUnit')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--name', default='lookout')
    parser.add_argument('--config', type=Path, help='copy a reviewed host config; omit to initialize image defaults')
    parser.add_argument('--port', type=int, default=18730)
    parser.add_argument('--engine', choices=['docker', 'podman'], default='docker')
    parser.add_argument('--directory', type=Path, help='persistent instance directory (Podman)')
    parser.add_argument('--data', type=Path, help='adopt an existing, stopped data directory (Podman)')
    parser.add_argument('--auto-update', help='opt in to this registry channel and daily backups (Podman)')
    parser.add_argument('--network', help='Docker: bridge/none; Podman: private network, default pasta')
    parser.add_argument('--env', action='append', default=[])
    parser.add_argument('--memory', default='8g')
    parser.add_argument('--cpus', type=float, default=4)
    parser.add_argument('--pids-limit', type=int, default=512)
    args = parser.parse_args(argv)
    try:
        if not 0 <= args.port <= 65535:
            raise ValueError('invalid loopback port')
        result = install(args.image, args.name, config=args.config,
                         port=args.port, network=args.network or ('pasta' if args.engine == 'podman' else 'bridge'), environment=args.env,
                         memory=args.memory, cpus=args.cpus, pids=args.pids_limit, engine=args.engine,
                         directory=args.directory, data=args.data, auto_update=args.auto_update)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'installation failed: {error}; existing volumes were not deleted', file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
