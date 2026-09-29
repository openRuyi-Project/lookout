"""Release orchestration contracts; real image startup is covered by smoke-image."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[3]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / ('deploy/' + name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


upgrade, release = module('upgrade'), module('release')
import deployment as operations
NEW, OLD = 'sha256:' + '2' * 64, 'sha256:' + '1' * 64


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setattr(operations.os, 'geteuid', lambda: 10001)
    config, data, backups, bundle = [tmp_path / name for name in ('config', 'data', 'backups', 'release')]
    for path in (config, data, backups, bundle):
        path.mkdir()
    (data / 'state').mkdir()
    (data / 'state/tracker.sqlite3').write_bytes(b'operator data')
    text = (ROOT / 'deploy/quadlet/openruyi-monitor.container.in').read_text()
    for key, value in (('IMAGE', OLD), ('CONFIG_DIR', str(config)), ('DATA_DIR', str(data))):
        text = text.replace('@' + key + '@', value)
    unit = tmp_path / 'fixture.container'
    unit.write_text(text)
    manifest = dict(version='0.1.0', revision='a' * 40, image=NEW, storage=2,
                    platform='linux/arm64', sha256=hashlib.sha256(b'archive').hexdigest())
    (bundle / 'image.tar').write_bytes(b'archive')
    (bundle / 'release.json').write_text(json.dumps(manifest))
    return bundle, unit, backups, manifest


def simulate(monkeypatch, manifest, *, fail=None, incompatible=False):
    calls, ready = [], []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ['podman', 'info']:
            return 'true'
        if argv[:3] == ['podman', 'image', 'inspect']:
            return json.dumps([{'Id': argv[-1], 'Os': 'linux', 'Architecture': 'arm64', 'Config': {'User': '10001:10001',
                'Labels': {'org.opencontainers.image.revision': manifest['revision'],
                           'org.opencontainers.image.version': manifest['version']}}}])
        if argv[:2] == ['podman', 'run']:
            if 'storage.FORMAT' in argv[-1]:
                return json.dumps([manifest['version'], manifest['storage']])
            if fail and fail in argv:
                raise RuntimeError('fixture failure')
            if incompatible and OLD in argv:
                raise RuntimeError('old reader rejects migrated database')
            if '/app/deploy/backup-snapshot.py' in argv:
                mount = next(value for value in argv if value.endswith(':/backup:Z'))
                (Path(mount.removesuffix(':/backup:Z')) / 'before.sqlite3').write_bytes(b'fixture backup')
        return ''
    def healthy(engine, name, image):
        ready.append(image)
        if fail == 'ready' and image == NEW:
            raise RuntimeError('new service unavailable')
    monkeypatch.setattr(upgrade, 'run', run)
    monkeypatch.setattr(operations, 'run', run)
    monkeypatch.setattr(upgrade, 'healthy', healthy)
    return calls, ready


def test_plan_performs_no_engine_or_service_actions(deployment, monkeypatch):
    bundle, unit, backups, _ = deployment
    monkeypatch.setattr(upgrade, 'run', lambda *args, **kwargs: pytest.fail('plan must not execute'))
    before = unit.read_bytes()
    result = upgrade.upgrade(bundle, unit, backups)
    assert result['status'] == 'plan' and result['image'] == NEW
    assert unit.read_bytes() == before and not list(backups.iterdir())


def test_upgrade_keeps_data_and_orders_stop_backup_migrate_start(deployment, monkeypatch):
    bundle, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest)
    original = unit.read_text()
    result = upgrade.upgrade(bundle, unit, backups, apply=True)
    assert result['status'] == 'ready' and ready == [NEW]
    assert unit.read_text() == original.replace('Image=' + OLD, 'Image=' + NEW)
    assert (Path(result['data']) / 'state/tracker.sqlite3').read_bytes() == b'operator data'
    stop = next(i for i, call in enumerate(calls) if call[:3] == ['systemctl', '--user', 'stop'])
    backup = next(i for i, call in enumerate(calls) if '/app/deploy/backup-snapshot.py' in call)
    migrate = next(i for i, call in enumerate(calls) if '/app/deploy/migrate-state.py' in call)
    start = next(i for i, call in enumerate(calls) if call[:3] == ['systemctl', '--user', 'start'])
    assert stop < backup < migrate < start
    assert all(':Z' not in arg for call in calls[:stop] for arg in call)
    assert next(backups.glob('upgrade-*/previous.container')).read_text() == original


@pytest.mark.parametrize('failure', ['/app/deploy/backup-snapshot.py', '/app/deploy/migrate-state.py', 'ready'])
def test_failed_upgrade_rolls_back_only_after_compatible_reader(deployment, monkeypatch, failure):
    bundle, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest, fail=failure)
    original = unit.read_text()
    with pytest.raises(RuntimeError):
        upgrade.upgrade(bundle, unit, backups, apply=True)
    assert unit.read_text() == original and ready[-1] == OLD
    old_check = next(i for i, call in enumerate(calls) if OLD in call and 'tracker.runtime_checks' in call)
    start = max(i for i, call in enumerate(calls) if call[:3] == ['systemctl', '--user', 'start'])
    assert old_check < start


def test_incompatible_rollback_leaves_data_intact_and_service_stopped(deployment, monkeypatch):
    bundle, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest, fail='ready', incompatible=True)
    with pytest.raises(RuntimeError, match='old image preflight failed'):
        upgrade.upgrade(bundle, unit, backups, apply=True)
    stops = [i for i, call in enumerate(calls) if call[:3] == ['systemctl', '--user', 'stop']]
    assert not any(call[:3] == ['systemctl', '--user', 'start'] for call in calls[stops[-1]:])
    assert ready == [NEW]
    assert (operations.quadlet(unit.read_text())['data'] / 'state/tracker.sqlite3').read_bytes() == b'operator data'


def test_release_tampering_is_rejected_before_any_service_operation(deployment, monkeypatch):
    bundle, unit, backups, _ = deployment
    (bundle / 'image.tar').write_bytes(b'changed')
    monkeypatch.setattr(upgrade, 'run', lambda *args, **kwargs: pytest.fail('must reject before execution'))
    with pytest.raises(ValueError, match='checksum'):
        upgrade.upgrade(bundle, unit, backups, apply=True)


@pytest.mark.parametrize('change', ['Environment=TRACKER_DB=/other/db', 'PodmanArgs=--privileged',
                                    'Volume=/tmp:/config:ro,Z', 'EnvironmentFile=/secrets'])
def test_custom_runtime_boundaries_fail_closed(deployment, change):
    _, unit, _, _ = deployment
    text = unit.read_text().replace('[Container]', '[Container]\n' + change)
    with pytest.raises(ValueError):
        operations.quadlet(text)


def test_unit_compare_and_swap_preserves_concurrent_operator_edit(tmp_path):
    unit = tmp_path / 'unit'
    unit.write_text('operator update')
    with pytest.raises(ValueError, match='changed since review'):
        operations.write_unit(unit, 'old unit', 'new unit')
    assert unit.read_text() == 'operator update'


def test_packager_refuses_uncommitted_source(tmp_path, monkeypatch):
    monkeypatch.setattr(release, 'run', lambda args: 'a' * 40 if args[-1] == 'HEAD' else ' M source.py')
    with pytest.raises(ValueError, match='clean checkout'):
        release.package('image', tmp_path / 'release', 'docker')
    assert not (tmp_path / 'release').exists()


def test_packager_checks_revision_and_image_version(tmp_path, monkeypatch):
    import tomllib
    version = tomllib.loads((ROOT / 'backend/pyproject.toml').read_text())['project']['version']
    def run(argv):
        if argv[0] == 'git':
            return 'a' * 40 if argv[-1] == 'HEAD' else ''
        if argv[1:3] == ['image', 'inspect']:
            return json.dumps([{'Id': NEW, 'Os': 'linux', 'Architecture': 'arm64',
                'Config': {'Labels': {'org.opencontainers.image.revision': 'a' * 40,
                                       'org.opencontainers.image.version': version}}}])
        if argv[1] == 'run':
            return json.dumps([version, 2])
        if argv[1] == 'save':
            Path(argv[3]).write_bytes(b'archive')
        return ''
    monkeypatch.setattr(release, 'run', run)
    output = release.package('image', tmp_path / 'release', 'docker')
    manifest = upgrade.read_release(output)
    assert manifest['version'] == version and manifest['image'] == NEW
    assert (output / 'upgrade.py').read_bytes() == (ROOT / 'deploy/upgrade.py').read_bytes()
    with pytest.raises(FileExistsError):
        release.package('image', output, 'docker')


def test_health_poll_has_bounded_subprocess_timeouts(monkeypatch):
    times = iter([0, 0, 100])
    monkeypatch.setattr(operations.time, 'monotonic', lambda: next(times))
    monkeypatch.setattr(operations.time, 'sleep', lambda *_: None)
    def run(argv, timeout):
        assert timeout <= 10
        raise subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(upgrade, 'run', run)
    monkeypatch.setattr(operations, 'run', run)
    with pytest.raises(RuntimeError, match='startup budget'):
        operations.healthy('podman', 'fixture', NEW)


@pytest.mark.parametrize('port', [1, 18730, 28730, 65535])
def test_quadlet_accepts_operator_loopback_port_and_keeps_it_on_upgrade(deployment, port):
    _, unit, _, _ = deployment
    text = unit.read_text().replace('127.0.0.1:18730', f'127.0.0.1:{port}')
    assert operations.quadlet(text)['image'] == OLD
    assert operations.replace_image(text, NEW) == text.replace(OLD, NEW)


@pytest.mark.parametrize('port', ['0', '65536', 'foo', '-1'])
def test_quadlet_rejects_invalid_ports(deployment, port):
    _, unit, _, _ = deployment
    with pytest.raises(ValueError, match='loopback'):
        operations.quadlet(unit.read_text().replace(':18730:', ':' + port + ':'))


def test_quadlet_rejects_public_listener_and_host_network(deployment):
    _, unit, _, _ = deployment
    for text in (unit.read_text().replace('127.0.0.1', '0.0.0.0'),
                 unit.read_text().replace('[Container]', '[Container]\nNetwork=host')):
        with pytest.raises(ValueError):
            operations.quadlet(text)


def test_installer_rejects_conflicting_environment_before_engine_call(tmp_path, monkeypatch):
    install = module('install')
    monkeypatch.setattr(install, 'run', lambda *a, **kw: pytest.fail('no engine writes'))
    for value in ['PORT=9000', 'TRACKER_DB=/tmp/db', 'HOST=0.0.0.0', 'missing-separator']:
        with pytest.raises(ValueError, match='environment'):
            install.install(tmp_path, 'fixture', environment=[value])


def test_config_archive_preserves_bytes_and_rejects_symlinks(tmp_path):
    import io
    import tarfile
    install = module('install')
    (tmp_path / 'tracker.toml').write_bytes(b'private configuration')
    (tmp_path / 'tracker.toml').chmod(0o600)
    with tarfile.open(fileobj=io.BytesIO(install.config_archive(tmp_path))) as archive:
        assert archive.extractfile('tracker.toml').read() == b'private configuration'
    (tmp_path / 'link').symlink_to('tracker.toml')
    with pytest.raises(ValueError, match='regular'):
        install.config_archive(tmp_path)


def test_atomic_metadata_never_overwrites_existing_file(tmp_path):
    path = tmp_path / 'release.json'
    operations.write_exclusive(path, b'original')
    with pytest.raises(FileExistsError):
        operations.write_exclusive(path, b'new')
    assert path.read_bytes() == b'original'
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_config_read_reports_permissions_instead_of_claiming_file_type(tmp_path):
    from tracker.config import read_input
    directory = tmp_path / 'private'
    directory.mkdir()
    path = directory / 'tracker.toml'
    path.write_text('x=1')
    directory.chmod(0)
    try:
        with pytest.raises(PermissionError):
            read_input(path)
    finally:
        directory.chmod(0o700)
