"""Release orchestration contracts; real image startup is covered by smoke-image."""
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


upgrade = module('upgrade')
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
    text = (ROOT / 'deploy/quadlet/openruyi-lookout.container.in').read_text()
    for key, value in (('IMAGE', OLD), ('CONFIG_DIR', str(config)), ('DATA_DIR', str(data))):
        text = text.replace('@' + key + '@', value)
    unit = tmp_path / 'fixture.container'
    unit.write_text(text)
    manifest = dict(version='0.1.0', revision='a' * 40, image=NEW, storage=2,
                    platform='linux/arm64')
    return NEW, unit, backups, manifest


def simulate(monkeypatch, manifest, *, fail=None, incompatible=False):
    calls, ready = [], []
    active = ['active']
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == 'busctl':
            if 'StartUnit' in argv: active[0] = 'active'
            if 'StopUnit' in argv: active[0] = 'inactive'
        if argv[:3] == ['systemctl', '--user', 'show']:
            return active[0]
        if argv[:2] == ['podman', 'info']:
            return 'true'
        if argv[:2] == ['podman', 'inspect']:
            return json.dumps([{'Image': OLD, 'State': {'Status': 'running'}}])
        if argv[:3] == ['podman', 'image', 'inspect']:
            return json.dumps([{'Id': argv[-1], 'Os': 'linux', 'Architecture': 'arm64', 'Config': {'User': '10001:10001',
                'Labels': {'org.opencontainers.image.revision': manifest['revision'],
                           'org.opencontainers.image.version': manifest['version']}}}])
        if argv[:2] == ['podman', 'run']:
            if 'storage.FORMAT' in argv[-1]:
                return json.dumps([manifest['version'], manifest['storage']])
            if fail and fail in argv:
                raise RuntimeError('fixture failure')
            if incompatible and OLD in argv and 'tracker.runtime_checks' in argv:
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
    assert result['status'] == 'plan' and result['reference'] == NEW
    assert unit.read_bytes() == before and not list(backups.iterdir())


def test_upgrade_keeps_data_and_orders_stop_backup_migrate_start(deployment, monkeypatch):
    bundle, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest)
    original = unit.read_text()
    result = upgrade.upgrade(bundle, unit, backups, apply=True)
    assert result['status'] == 'ready' and ready == [NEW]
    assert unit.read_text() == original.replace('Image=' + OLD, 'Image=' + NEW)
    assert (Path(result['data']) / 'state/tracker.sqlite3').read_bytes() == b'operator data'
    stop = next(i for i, call in enumerate(calls) if call[0] == 'busctl' and 'StopUnit' in call)
    backup = next(i for i, call in enumerate(calls) if '/app/deploy/backup-snapshot.py' in call)
    migrate = next(i for i, call in enumerate(calls) if '/app/deploy/migrate-state.py' in call)
    start = next(i for i, call in enumerate(calls) if call[0] == 'busctl' and 'StartUnit' in call)
    reload = next(i for i, call in enumerate(calls) if call[0] == 'busctl' and call[-1] == 'Reload')
    assert stop < backup < migrate < reload < start
    assert all(':Z' not in arg for call in calls[:stop] for arg in call)
    assert next(backups.glob('upgrade-*/previous.container')).read_text() == original


def test_reload_rpc_failure_is_not_reported_as_success(monkeypatch):
    def failed(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, '', 'manager unavailable')
    monkeypatch.setattr(operations.subprocess, 'run', failed)
    with pytest.raises(RuntimeError, match='busctl.*exit 1'):
        operations.manager('Reload')


@pytest.mark.parametrize('failure', ['/app/deploy/backup-snapshot.py', '/app/deploy/migrate-state.py', 'ready'])
def test_failed_upgrade_rolls_back_only_after_compatible_reader(deployment, monkeypatch, failure):
    bundle, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest, fail=failure)
    original = unit.read_text()
    with pytest.raises(RuntimeError):
        upgrade.upgrade(bundle, unit, backups, apply=True)
    assert unit.read_text() == original and ready[-1] == OLD
    old_check = next(i for i, call in enumerate(calls) if OLD in call and 'tracker.runtime_checks' in call)
    start = max(i for i, call in enumerate(calls) if call[0] == 'busctl' and 'StartUnit' in call)
    assert old_check < start


def test_incompatible_rollback_leaves_data_intact_and_service_stopped(deployment, monkeypatch):
    bundle, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest, fail='ready', incompatible=True)
    with pytest.raises(RuntimeError, match='rollback/preflight failed'):
        upgrade.upgrade(bundle, unit, backups, apply=True)
    stops = [i for i, call in enumerate(calls) if call[0] == 'busctl' and 'StopUnit' in call]
    assert not any(call[0] == 'busctl' and 'StartUnit' in call for call in calls[stops[-1]:])
    assert ready == [NEW]
    assert (operations.quadlet(unit.read_text())['data'] / 'state/tracker.sqlite3').read_bytes() == b'operator data'


def test_invalid_image_reference_is_rejected_before_service_stop(deployment, monkeypatch):
    _, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest)
    with pytest.raises(ValueError, match='fully qualified'):
        upgrade.upgrade('--injected', unit, backups, apply=True)
    assert not any('StopUnit' in call for call in calls)
    assert not list(backups.iterdir())


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
            install.install(NEW, 'fixture', environment=[value])


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


@pytest.mark.parametrize('method,states', [
    ('StartUnit', ['inactive', 'activating', 'active']),
    ('StopUnit', ['active', 'deactivating', 'inactive']),
])
def test_acknowledged_job_must_reach_requested_state(monkeypatch, method, states):
    observed = iter(states)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return next(observed) if argv[0] == 'systemctl' else ''
    monkeypatch.setattr(operations, 'run', run)
    monkeypatch.setattr(operations.time, 'sleep', lambda *_: None)
    operations.service_action('fixture.service', method)
    assert len(calls) == 4 and method in calls[0]


def test_service_transition_is_bounded_and_failed_start_rejected(monkeypatch):
    monkeypatch.setattr(operations, 'run', lambda *a, **k: 'failed')
    with pytest.raises(RuntimeError, match='failed to start'):
        operations.service_action('fixture.service', 'StartUnit')
    clock = iter([0, 61])
    monkeypatch.setattr(operations.time, 'monotonic', lambda: next(clock))
    with pytest.raises(RuntimeError, match='timed out'):
        operations.service_action('fixture.service', 'StartUnit')


def test_same_image_is_verified_without_stop_or_backup(deployment, monkeypatch):
    _, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest)
    result = upgrade.upgrade(OLD, unit, backups, apply=True)
    assert result['status'] == 'unchanged' and ready == [OLD]
    assert not any('StopUnit' in call or 'StartUnit' in call for call in calls)
    assert not list(backups.iterdir())


def test_lost_stop_acknowledgement_resumes_old_instance(deployment, monkeypatch):
    reference, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest)
    run = operations.run
    failed = False
    def lost(argv, **kwargs):
        nonlocal failed
        result = run(argv, **kwargs)
        if 'StopUnit' in argv and not failed:
            failed = True
            raise RuntimeError('acknowledgement lost')
        return result
    monkeypatch.setattr(operations, 'run', lost)
    with pytest.raises(RuntimeError, match='acknowledgement lost'):
        upgrade.upgrade(reference, unit, backups, apply=True)
    assert ready == [OLD]
    assert any('StartUnit' in call for call in calls)
    assert json.loads(next(backups.glob('upgrade-*/result.json')).read_text()) == {'status': 'failed', 'rollback': 'ready'}


def test_old_running_image_is_not_resolved_through_pulled_channel(deployment, monkeypatch):
    _, unit, backups, manifest = deployment
    unit.write_text(unit.read_text().replace(OLD, 'ghcr.io/example/lookout:main'))
    calls, ready = simulate(monkeypatch, manifest, fail='ready')
    real = operations.run
    def channel(argv, **kwargs):
        if argv[:3] == ['podman', 'image', 'inspect'] and argv[-1] == 'ghcr.io/example/lookout:main':
            argv = [*argv[:-1], NEW]
        return real(argv, **kwargs)
    monkeypatch.setattr(operations, 'run', channel)
    with pytest.raises(RuntimeError):
        upgrade.upgrade('ghcr.io/example/lookout:main', unit, backups, apply=True)
    assert ready == [NEW, OLD]
    assert 'Image=' + OLD in unit.read_text()


def test_upgrade_lock_is_owned_by_instance_not_backup_directory(deployment):
    _, unit, _, _ = deployment
    first = operations.Quadlet(unit)
    second = operations.Quadlet(unit)
    assert operations.instance_lock(first) == operations.instance_lock(second)
    with operations.upgrade_lock(operations.instance_lock(first)):
        with pytest.raises(BlockingIOError):
            with operations.upgrade_lock(operations.instance_lock(second)):
                pytest.fail('concurrent upgrade admitted')


def test_docker_lock_is_scoped_by_daemon_and_instance(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    monkeypatch.setattr(operations, 'run', lambda *_: '"daemon-id"')
    service = SimpleNamespace(engine='docker', instance='instance-a')
    first = operations.instance_lock(service)
    assert first == operations.instance_lock(service)
    service.instance = 'instance-b'
    assert first != operations.instance_lock(service)


@pytest.mark.parametrize('reference', ['image', '--flag', 'ghcr.io/example/lookout', 'ghcr.io/example/lookout:main\n'])
def test_image_reference_requires_explicit_registry_identity(reference):
    with pytest.raises(ValueError):
        operations.resolve_image('docker', reference)


def test_registry_pull_is_pinned_before_metadata_probe(monkeypatch):
    calls = []
    reference = 'ghcr.io/example/lookout:main'
    def run(argv, **kwargs):
        calls.append(argv)
        if 'inspect' in argv:
            return json.dumps([{'Id': NEW, 'Os': 'linux', 'Architecture': 'amd64',
                'RepoDigests': ['ghcr.io/example/lookout@sha256:' + '3' * 64],
                'Config': {'User': '10001:10001', 'Labels': {
                    'org.opencontainers.image.version': '0.1.0',
                    'org.opencontainers.image.revision': 'a' * 40}}}])
        if argv[1] == 'run':
            assert NEW in argv and reference not in argv
            return '["0.1.0", 2]'
        return ''
    monkeypatch.setattr(operations, 'run', run)
    result = operations.resolve_image('podman', reference)
    assert calls[0] == ['podman', 'pull', reference]
    assert result['image'] == NEW and result['reference'] == reference
    assert result['storage'] == 2 and result['platform'] == 'linux/amd64'


def test_automation_keeps_host_update_and_backup_separate(tmp_path, monkeypatch):
    install = module('install')
    monkeypatch.setattr(install.Path, 'home', lambda: tmp_path)
    calls = []
    monkeypatch.setattr(install, 'run', lambda argv, **kwargs: calls.append(argv))
    monkeypatch.setattr(install, 'manager', lambda *args: calls.append(args))
    unit = tmp_path / 'lookout.container'
    install.install_automation('lookout', tmp_path, unit, 'ghcr.io/example/lookout:main')
    user_units = tmp_path / '.config/systemd/user'
    update = (user_units / 'lookout-update.service').read_text()
    backup = (user_units / 'lookout-backup.service').read_text()
    assert '--image ghcr.io/example/lookout:main' in update
    assert '--unit ' + str(unit) in update and '--quiet' in update
    assert '--backup-dir ' + str(tmp_path / 'backups') in backup
    assert '@' not in update and '@' not in backup
    assert ['systemctl', '--user', 'enable', '--now', 'lookout-update.timer'] in calls
    with pytest.raises(ValueError, match='already exist'):
        install.install_automation('lookout', tmp_path, unit, 'ghcr.io/example/lookout:main')


def test_rootless_installer_requires_linger_before_creating_directories(tmp_path, monkeypatch):
    install = module('install')
    monkeypatch.setattr(install.os, 'geteuid', lambda: 1001)
    monkeypatch.setattr(install, 'run', lambda argv, **kwargs: 'true' if argv[0] == 'podman' else 'no')
    destination = tmp_path / 'instance'
    with pytest.raises(ValueError, match='linger'):
        install.install(NEW, 'lookout', engine='podman', directory=destination,
                        network='pasta', auto_update='ghcr.io/example/lookout:main')
    assert not destination.exists()


@pytest.mark.parametrize('reference', [NEW, NEW.removeprefix('sha256:')])
def test_podman_bare_image_ids_are_normalized(monkeypatch, reference):
    def run(argv, **kwargs):
        assert argv[1] != 'pull'
        if 'inspect' in argv:
            return json.dumps([{'Id': NEW.removeprefix('sha256:'), 'Os': 'linux', 'Architecture': 'amd64',
                'Config': {'User': '10001:10001', 'Labels': {
                    'org.opencontainers.image.version': '0.1.0',
                    'org.opencontainers.image.revision': 'a' * 40}}}])
        return '["0.1.0", 2]'
    monkeypatch.setattr(operations, 'run', run)
    assert operations.resolve_image('podman', reference)['image'] == NEW
