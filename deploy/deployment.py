"""Container operations shared by installation and release upgrades.

Docker's container and Quadlet's unit remain the runtime configuration owners.
This module validates those inputs; it does not invent another service format.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

PYTHON = '/opt/venv/bin/python'
LABEL = 'org.openruyi.instance'
LIMITS = ['--memory', '8g', '--cpus', '4', '--pids-limit', '512']
PROTECTION = ['--read-only', '--cap-drop=all', '--security-opt=no-new-privileges',
              '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m,mode=1777']


def run(argv, *, timeout=120, input=None):
    result = subprocess.run(argv, input=input, text=not isinstance(input, bytes),
                            capture_output=True, timeout=timeout)
    if result.returncode:
        # A provider proxy or a configuration error may contain credentials.
        raise RuntimeError(f'{argv[0]} {argv[1]} failed (exit {result.returncode})')
    output = result.stdout
    return (output.decode() if isinstance(output, bytes) else output).strip()


def read_release(directory):
    manifest = json.loads((directory / 'release.json').read_text())
    if (set(manifest) != {'version', 'revision', 'image', 'storage', 'platform', 'sha256'}
            or not re.fullmatch(r'\d+\.\d+\.\d+', manifest['version'])
            or not re.fullmatch(r'[0-9a-f]{40}', manifest['revision'])
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', manifest['image'])
            or type(manifest['storage']) is not int or manifest['storage'] < 1):
        raise ValueError('invalid release manifest')
    with (directory / 'image.tar').open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != manifest['sha256']:
        raise ValueError('release image checksum mismatch')
    return manifest


def load_image(engine, directory, manifest):
    run([engine, 'load', '--input', str(directory / 'image.tar')], timeout=600)
    info = json.loads(run([engine, 'image', 'inspect', manifest['image']]))[0]
    labels = info['Config'].get('Labels') or {}
    if (info['Os'] + '/' + info['Architecture'] != manifest['platform']
            or info['Config'].get('User') != '10001:10001'
            or labels.get('org.opencontainers.image.revision') != manifest['revision']
            or labels.get('org.opencontainers.image.version') != manifest['version']):
        raise ValueError('loaded image does not match release metadata')
    code = ('import json,tomllib; from tracker import storage; '
            'p=tomllib.load(open("/app/backend/pyproject.toml","rb")); '
            'print(json.dumps([p["project"]["version"], storage.FORMAT]))')
    actual = json.loads(run([engine, 'run', '--rm', '--network', 'none', *PROTECTION,
                            '--entrypoint', PYTHON, manifest['image'], '-c', code]))
    if actual != [manifest['version'], manifest['storage']]:
        raise ValueError('image application/storage version does not match release metadata')


@contextmanager
def upgrade_lock(path):
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def write_exclusive(path, data):
    """Publish a fully synced file without overwriting a concurrently created one."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def healthy(engine, name, image, *, timeout=90):
    deadline = time.monotonic() + timeout
    probe = ('import urllib.request; '
             '[urllib.request.urlopen("http://127.0.0.1:8080/"+p,timeout=3).read() '
             'for p in ("livez","readyz")]')
    while time.monotonic() < deadline:
        try:
            info = json.loads(run([engine, 'inspect', name], timeout=5))[0]
            if info['Image'].removeprefix('sha256:') != image.removeprefix('sha256:'):
                raise ValueError('running image does not match the selected release')
            if info.get('State', {}).get('Status') in ('exited', 'dead', 'restarting'):
                raise ValueError('application exited before becoming ready')
            run([engine, 'exec', name, PYTHON, '-c', probe], timeout=10)
            return
        except (RuntimeError, subprocess.TimeoutExpired):
            time.sleep(1)
    raise RuntimeError('release did not become live and ready within the startup budget')


def no_data_users(service):
    for ident in run([service.engine, 'ps', '-q']).split():
        info = json.loads(run([service.engine, 'inspect', ident]))[0]
        for mount in info.get('Mounts', []):
            if service.engine == 'docker':
                overlap = mount.get('Name') == service.settings['data']
            else:
                source = Path(mount.get('Source', '/nonexistent')).resolve()
                data = service.settings['data']
                overlap = source == data or source in data.parents or data in source.parents
            if overlap:
                raise RuntimeError('another running container mounts this data directory')


def image_command(service, image, script, *args, backup_dir=None):
    command = [service.engine, 'run', '--rm', '--network', 'none', *PROTECTION]
    if service.engine == 'podman':
        command += ['--userns=keep-id:uid=10001,gid=10001']
    for role in ('config', 'data'):
        source = service.settings[role]
        if service.engine == 'docker':
            value = f'type=volume,src={source},dst=/{role},volume-nocopy'
            command += ['--mount', value + (',readonly' if role == 'config' else '')]
        else:
            command += ['-v', f'{source}:/{role}:' + ('ro,Z' if role == 'config' else 'Z')]
    if backup_dir:
        if service.engine == 'docker':
            command += ['--mount', f'type=volume,src={service.backup_volume},dst=/backup,volume-nocopy']
        else:
            command += ['-v', f'{backup_dir}:/backup:Z']
    for value in service.settings.get('environment', []):
        command += ['-e', value]
    return command + ['--entrypoint', PYTHON, image, script, *args]


class Docker:
    engine = 'docker'

    def __init__(self, name):
        self.info = json.loads(run(['docker', 'inspect', name]))[0]
        self.name = self.info['Name'].lstrip('/')
        config, host = self.info['Config'], self.info['HostConfig']
        self.instance = (config.get('Labels') or {}).get(LABEL)
        if (not self.instance or config['User'] != '10001:10001'
                or not host['ReadonlyRootfs'] or host.get('Privileged')
                or set(host.get('CapDrop') or []) != {'ALL'}
                or not any(v.startswith('no-new-privileges') for v in host.get('SecurityOpt') or [])
                or host.get('NetworkMode') not in ('default', 'bridge', 'none')
                or host.get('CapAdd') or host.get('Devices') or host.get('Binds')):
            raise ValueError('container does not match the installed Docker runtime contract')
        mounts = {m['Destination']: m for m in self.info['Mounts'] if m['Type'] != 'tmpfs'}
        if set(mounts) != {'/config', '/data'}:
            raise ValueError('Docker deployment needs exactly its config and data volumes')
        for path, mount in mounts.items():
            if mount['Type'] != 'volume' or mount['RW'] != (path == '/data'):
                raise ValueError('invalid Docker persistent mount')
            labels = json.loads(run(['docker', 'volume', 'inspect', mount['Name']]))[0].get('Labels') or {}
            if labels.get(LABEL) != self.instance:
                raise ValueError('volume identity does not match the installed instance')
        self.backup_volume = config['Labels'].get('org.openruyi.backups')
        if not self.backup_volume:
            raise ValueError('installed backup volume is missing')
        labels = json.loads(run(['docker', 'volume', 'inspect', self.backup_volume]))[0].get('Labels') or {}
        if labels.get(LABEL) != self.instance:
            raise ValueError('backup volume identity does not match the installed instance')
        image_config = json.loads(run(['docker', 'image', 'inspect', self.info['Image']]))[0]['Config']
        if any(config.get(key) != image_config.get(key) for key in ('Entrypoint', 'Cmd')):
            raise ValueError('custom application entrypoint requires operator review')
        image_env = image_config.get('Env') or []
        explicit_env = json.loads(config['Labels'].get('org.openruyi.environment', '[]'))
        self.settings = dict(image=self.info['Image'], name=self.name,
            config=mounts['/config']['Name'], data=mounts['/data']['Name'],
            environment=[e for e in config.get('Env') or []
                         if e not in image_env or e.split('=', 1)[0] in explicit_env])
        if any(e.split('=', 1)[0] in {'TRACKER_DB', 'TRACKER_CONFIG', 'PORT', 'API_PORT', 'HOST'}
               and e not in {'HOST=0.0.0.0', 'PORT=8080', 'API_PORT=18731'} for e in self.settings['environment']):
            raise ValueError('custom database or listener requires operator review')
        self.previous = None
        if not host.get('Memory') or not host.get('NanoCpus') or not host.get('PidsLimit'):
            raise ValueError('installed container is missing resource limits')
        for port, bindings in (host.get('PortBindings') or {}).items():
            if port != '8080/tcp' or any(b['HostIp'] != '127.0.0.1' for b in bindings):
                raise ValueError('application ports must stay on host loopback')

    def stop(self):
        if run(['docker', 'ps', '-aq', '--filter', 'name=^/' + self.name + '$']):
            run(['docker', 'stop', '--time', '20', self.name], timeout=40)

    def stage(self, image, *, port=None):
        self.previous = self.name + '-previous-' + uuid.uuid4().hex[:10]
        run(['docker', 'rename', self.name, self.previous])
        h = self.info['HostConfig']
        command = ['docker', 'create', '--name', self.name, *PROTECTION,
                   '--memory', str(h['Memory']), '--cpus', str(h['NanoCpus'] / 1e9),
                   '--pids-limit', str(h['PidsLimit']), '--network', h['NetworkMode'],
                   '--restart', h['RestartPolicy']['Name'], '--stop-timeout', '20',
                   '--log-driver', h['LogConfig']['Type']]
        for key, value in h['LogConfig'].get('Config', {}).items():
            command += ['--log-opt', f'{key}={value}']
        for container_port, bindings in (h.get('PortBindings') or {}).items():
            for binding in bindings:
                if binding['HostIp'] != '127.0.0.1' or container_port != '8080/tcp':
                    raise ValueError('application ports must stay on host loopback')
                command += ['-p', f'127.0.0.1:{port or binding["HostPort"]}:8080']
        for key, value in self.info['Config']['Labels'].items():
            if key.startswith('org.openruyi.'):
                command += ['--label', f'{key}={value}']
        for role in ('config', 'data'):
            command += ['--mount', f'type=volume,src={self.settings[role]},dst=/{role},volume-nocopy'
                        + (',readonly' if role == 'config' else '')]
        for value in self.settings['environment']:
            command += ['-e', value]
        run(command + [image])

    def start(self):
        run(['docker', 'start', self.name])

    def rollback(self):
        if self.previous:
            existing = run(['docker', 'ps', '-aq', '--filter', 'name=^/' + self.name + '$'])
            if existing:
                run(['docker', 'rm', self.name])
            run(['docker', 'rename', self.previous, self.name])
        self.start()

    def save(self, directory):
        write_exclusive(directory / 'previous.json', json.dumps(self.info, indent=2).encode())

    def export_backup(self, image, filename, destination):
        helper = 'openruyi-backup-' + uuid.uuid4().hex[:12]
        run(['docker', 'create', '--name', helper, '--network', 'none', *PROTECTION,
             '--mount', f'type=volume,src={self.backup_volume},dst=/backup,readonly,volume-nocopy',
             '--entrypoint', PYTHON, image, '-c', 'pass'])
        try:
            with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
                staged = Path(temporary) / 'backup.sqlite3'
                run(['docker', 'cp', f'{helper}:/backup/{filename}', str(staged)])
                with staged.open('rb') as stream:
                    os.fsync(stream.fileno())
                staged.chmod(0o600)
                os.link(staged, destination)
                fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            run(['docker', 'rm', helper])

    def finish(self):
        if self.previous:
            run(['docker', 'rm', self.previous])


def quadlet(text):
    """Read only the installed template's deployment boundary, not a second INI DSL."""
    section, fields = None, {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('['):
            section = line
        elif section == '[Container]' and '=' in line and not line.startswith(('#', ';')):
            key, value = line.split('=', 1)
            fields.setdefault(key, []).append(value)
    def one(key):
        values = fields.get(key, [])
        if len(values) != 1:
            raise ValueError('Quadlet requires one ' + key)
        return values[0]
    fixed = {'UserNS': 'keep-id:uid=10001,gid=10001', 'User': '10001', 'Group': '10001',
             'ReadOnly': 'true', 'NoNewPrivileges': 'true', 'DropCapability': 'all'}
    if any(one(key) != value for key, value in fixed.items()):
        raise ValueError('Quadlet does not match the rootless runtime contract')
    volumes = {}
    for value in fields.get('Volume', []):
        parts = value.split(':')
        if len(parts) != 3 or parts[1] not in ('/config', '/data') or parts[1] in volumes:
            raise ValueError('upgrade supports one config mount and one data mount')
        path = Path(parts[0])
        options = set(parts[2].split(','))
        expected = {'ro', 'Z'} if parts[1] == '/config' else {'Z'}
        if not path.is_absolute() or not path.is_dir() or options != expected:
            raise ValueError('invalid persistent mount')
        volumes[parts[1]] = path.resolve()
    if set(volumes) != {'/config', '/data'}:
        raise ValueError('config/data mount is missing')
    name = one('ContainerName')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
        raise ValueError('invalid container name')
    # Preflight must use the same default database and ports as the service.
    environment = fields.get('Environment', [])
    for value in environment:
        key, sep, _ = value.partition('=')
        if not sep or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', key):
            raise ValueError('use one unquoted KEY=value per Environment line')
        if key in {'TRACKER_DB', 'TRACKER_CONFIG', 'API_PORT'}:
            raise ValueError('custom data/config/internal API requires operator review')
        if key in {'HOST', 'PORT'} and value not in {'HOST=0.0.0.0', 'PORT=8080'}:
            raise ValueError('use the default internal listener; configure PublishPort instead')
    if any(fields.get(key) for key in ('EnvironmentFile', 'PodmanArgs', 'Exec', 'Entrypoint',
                                      'AddCapability', 'Device', 'SecurityLabelDisable')):
        raise ValueError('custom runtime overrides require an operator-reviewed upgrade')
    if any(v == 'host' for v in fields.get('Network', [])):
        raise ValueError('use a private container network')
    for value in fields.get('PublishPort', []):
        match = re.fullmatch(r'127\.0\.0\.1:(\d+):8080', value)
        if not match or not 1 <= int(match[1]) <= 65535:
            raise ValueError('publish a valid port on host loopback only')
    return dict(image=one('Image'), name=name, config=volumes['/config'], data=volumes['/data'],
                environment=fields.get('Environment', []))


def replace_image(text, image):
    section, lines, changed = None, [], 0
    for line in text.splitlines(keepends=True):
        if line.strip().startswith('['):
            section = line.strip()
        if section == '[Container]' and line.startswith('Image='):
            line, changed = f'Image={image}\n', changed + 1
        lines.append(line)
    if changed != 1:
        raise ValueError('expected one Image= in Container section')
    return ''.join(lines)


def write_unit(path, expected, replacement):
    if path.is_symlink() or path.read_text() != expected:
        raise ValueError('unit changed since review; refusing overwrite')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)



class Quadlet:
    engine = 'podman'

    def __init__(self, unit):
        if os.geteuid() == 0:
            raise ValueError('run as the rootless service owner, not root')
        if unit.suffix != '.container' or unit.is_symlink():
            raise ValueError('use the installed Quadlet .container file')
        self.unit, self.original = unit, unit.read_text()
        self.settings = quadlet(self.original)
        self.name = self.settings['name']
        self.service = unit.stem + '.service'
        self.replacement = None

    def check(self):
        if run(['podman', 'info', '--format', '{{.Host.Security.Rootless}}']) != 'true':
            raise ValueError('rootless Podman is required')
        run(['systemctl', '--user', 'is-active', '--quiet', self.service])
        if self.unit.read_text() != self.original:
            raise ValueError('unit changed since review; refusing upgrade')

    def stop(self):
        run(['systemctl', '--user', 'stop', self.service], timeout=60)

    def stage(self, image):
        replacement = replace_image(self.original, image)
        write_unit(self.unit, self.original, replacement)
        self.replacement = replacement
        run(['systemctl', '--user', 'daemon-reload'])

    def start(self):
        run(['systemctl', '--user', 'start', self.service])

    def rollback(self):
        expected = self.replacement or self.original
        write_unit(self.unit, expected, replace_image(self.original, self.settings['image']))
        run(['systemctl', '--user', 'daemon-reload'])
        self.start()

    def save(self, directory):
        write_exclusive(directory / 'previous.container', self.original.encode())

    def export_backup(self, image, filename, destination):
        if not destination.is_file():
            raise RuntimeError('backup was not published')

    def finish(self):
        pass
