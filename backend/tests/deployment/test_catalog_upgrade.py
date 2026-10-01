"""Release-owned catalog preparation and image/config rollback share one boundary."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.deployment.test_upgrade import deployment, simulate, module, NEW, OLD, operations

worker = module('release-upgrade')
launcher = module('upgrade')
import catalog_upgrade as catalogs


def proposed_config(monkeypatch, deployment, *, failure=None):
    _, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest, fail=failure)
    # simulate configures the worker loaded in the neighboring test module.
    monkeypatch.setattr(worker, 'run', operations.run)
    def healthy(engine, name, image):
        ready.append(image)
        selected = operations.quadlet(unit.read_text())
        if image == NEW:
            assert selected['config'].name == 'prepared'
            if failure == 'ready':
                raise RuntimeError('new service unavailable')
        else:
            assert selected['config'].name == 'config'
    monkeypatch.setattr(worker, 'healthy', healthy)
    prepared = unit.parent / 'prepared'
    prepared.mkdir()
    def prepare(service, image, transaction, baseline):
        assert baseline == OLD
        assert image == NEW
        assert any('/app/deploy/migrate-state.py' in call for call in calls)
        return prepared, {'previous': str(service.settings['config']), 'prepared': str(prepared),
                          'ownership': 'release', 'baseline_image': baseline}
    monkeypatch.setattr(worker, 'prepare', prepare)
    return calls, ready, prepared


def test_catalog_and_image_are_selected_together_without_replacing_data(deployment, monkeypatch):
    reference, unit, backups, _ = deployment
    calls, _, prepared = proposed_config(monkeypatch, deployment)
    original = unit.read_text()
    result = worker.upgrade(reference, unit, backups, apply=True, catalog_baseline=OLD)
    assert result['catalogs']['baseline_image'] == OLD
    assert operations.quadlet(unit.read_text())['config'] == prepared
    assert operations.quadlet(unit.read_text())['image'] == NEW
    assert next(backups.glob('upgrade-*/previous.container')).read_text() == original
    assert (Path(result['data']) / 'state/tracker.sqlite3').read_bytes() == b'operator data'
    preflight = next(call for call in calls if 'tracker.runtime_checks' in call)
    assert f'{prepared}:/config:ro,Z' in preflight


def test_failed_new_catalog_restores_exact_original_config_image_pair(deployment, monkeypatch):
    reference, unit, backups, _ = deployment
    calls, ready, prepared = proposed_config(monkeypatch, deployment, failure='ready')
    original = unit.read_bytes()
    with pytest.raises(RuntimeError, match='unavailable'):
        worker.upgrade(reference, unit, backups, apply=True, catalog_baseline=OLD)
    assert unit.read_bytes() == original and ready == [NEW, OLD]
    rollback = next(call for call in calls if OLD in call and 'tracker.runtime_checks' in call)
    assert f'{unit.parent / "config"}:/config:ro,Z' in rollback
    assert f'{prepared}:/config:ro,Z' not in rollback


def test_installer_provenance_supplies_baseline_without_a_per_update_flag(deployment, monkeypatch):
    reference, unit, backups, _ = deployment
    unit.write_text(unit.read_text().replace('[Container]', '[Container]\nLabel=org.openruyi.catalog-image=' + OLD))
    proposed_config(monkeypatch, deployment)
    worker.upgrade(reference, unit, backups, apply=True)
    assert json.loads(next(backups.glob('upgrade-*/catalogs.json')).read_text())['baseline_image'] == OLD


@pytest.mark.parametrize('failure', ['missing original image', 'removed setting needs an explicit override'])
def test_preparation_failure_does_not_overwrite_source_or_restart_new_image(deployment, monkeypatch, failure):
    reference, unit, backups, manifest = deployment
    calls, ready = simulate(monkeypatch, manifest)
    monkeypatch.setattr(worker, 'run', operations.run)
    monkeypatch.setattr(worker, 'healthy', lambda engine, name, image: ready.append(image))
    def fail(*args):
        raise ValueError(failure)
    monkeypatch.setattr(worker, 'prepare', fail)
    original = unit.read_bytes()
    with pytest.raises(ValueError, match=failure):
        worker.upgrade(reference, unit, backups, apply=True, catalog_baseline=OLD)
    assert unit.read_bytes() == original and ready == [OLD]


@pytest.mark.parametrize('local,baseline,ownership', [(True, None, 'operator'), (False, OLD, 'release')])
def test_local_catalog_without_provenance_and_shared_catalog_are_not_rewritten(tmp_path, monkeypatch, local, baseline, ownership):
    service = SimpleNamespace(engine='podman', settings={'config': tmp_path, 'data': tmp_path})
    monkeypatch.setattr(catalogs, 'run', lambda argv: json.dumps({'local': local, 'inputs': {}}))
    monkeypatch.setattr(catalogs, 'resolve_image', lambda *args: pytest.fail('no baseline needed'))
    prepared, result = catalogs.prepare(service, NEW, tmp_path, baseline)
    assert prepared == tmp_path and result['ownership'] == ownership
    assert list(tmp_path.iterdir()) == []


def test_moving_baseline_tag_is_refused_before_extraction(tmp_path, monkeypatch):
    service = SimpleNamespace(engine='podman', settings={'config': tmp_path, 'data': tmp_path})
    monkeypatch.setattr(catalogs, 'run', lambda argv: json.dumps({'local': True, 'inputs': {}}))
    with pytest.raises(ValueError, match='immutable'):
        catalogs.prepare(service, NEW, tmp_path, 'ghcr.io/example/lookout:main')


def test_host_launcher_executes_selected_image_tools_not_installed_worker(deployment, monkeypatch):
    reference, unit, backups, manifest = deployment
    calls, _ = simulate(monkeypatch, manifest)
    monkeypatch.setattr(launcher, 'run', operations.run)
    monkeypatch.setattr(launcher, 'resolve_image', lambda *_: manifest)
    def export(engine, image, source, destination):
        assert image == NEW and source == '/app/deploy'
        destination.mkdir()
        (destination / 'release-upgrade.py').write_text('selected release')
    monkeypatch.setattr(launcher, 'export_image_tree', export)
    def execute(argv, **kwargs):
        assert Path(argv[1]).read_text() == 'selected release'
        assert argv[argv.index('--image') + 1] == NEW
        assert argv[argv.index('--catalog-baseline') + 1] == OLD
        assert not any('socket' in arg for call in calls for arg in call)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'image': NEW, 'status': 'ready'}))
    monkeypatch.setattr(launcher.subprocess, 'run', execute)
    result = launcher.upgrade(reference, unit, backups, apply=True, catalog_baseline=OLD)
    assert result['status'] == 'ready' and not list(backups.iterdir())


def test_missing_release_upgrade_protocol_never_stops_service(deployment, monkeypatch):
    reference, unit, backups, manifest = deployment
    calls, _ = simulate(monkeypatch, manifest)
    monkeypatch.setattr(launcher, 'run', operations.run)
    monkeypatch.setattr(launcher, 'resolve_image', lambda *_: manifest)
    monkeypatch.setattr(launcher, 'export_image_tree', lambda engine, image, source, destination: destination.mkdir())
    with pytest.raises(ValueError, match='protocol'):
        launcher.upgrade(reference, unit, backups, apply=True)
    assert not any('StopUnit' in call for call in calls)


def test_image_export_rejects_symlinks_and_removes_only_its_helper(tmp_path, monkeypatch):
    calls = []
    destination = tmp_path / 'tools'
    def run(argv):
        calls.append(argv)
        if argv[1] == 'cp':
            destination.mkdir()
            (destination / 'bad.py').symlink_to('/etc/passwd')
        return ''
    monkeypatch.setattr(operations, 'run', run)
    with pytest.raises(ValueError, match='regular'):
        operations.export_image_tree('docker', NEW, '/app/deploy', destination)
    assert calls[-1][:2] == ['docker', 'rm']
    assert calls[-1][-1] == calls[0][calls[0].index('--name') + 1]
    assert not any('--mount' in call or '-v' in call for call in calls)


def test_preparer_runs_real_catalog_migration_and_detects_input_drift(tmp_path, monkeypatch):
    import shutil
    import tomllib
    import tomlkit
    from tests.config.test_release_catalog import release, deploy_module
    from tracker import config

    original = release(tmp_path).parent
    source, supplied = tmp_path / 'operator', tmp_path / 'supplied'
    for directory in (source, supplied):
        shutil.copytree(original, directory)
    (supplied / 'packages.toml').write_text(tomlkit.dumps({
        'widget': {'monitors': {'security': {'vendor': 'fixture', 'product': 'new'}}},
        'added': {'monitors': {'security': {'vendor': 'fixture', 'product': 'added'}}}}))
    settings = tomllib.loads((source / 'tracker.toml').read_text())
    settings['obs']['project'] = 'operator-project'
    (source / 'tracker.toml').write_text(tomlkit.dumps(settings))
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    service = SimpleNamespace(engine='podman', settings={'config': source, 'data': tmp_path / 'data'})
    transaction = tmp_path / 'transaction'
    transaction.mkdir()
    changed = False

    def run(argv):
        if catalogs.CATALOG_STATUS in argv:
            loaded = config.load(source / 'tracker.toml')
            return json.dumps({'local': True, 'inputs': loaded['input_hashes']})
        mounts = {arg.split(':')[1]: Path(arg.split(':')[0]) for arg in argv if ':/' in arg and not arg.startswith('sha256:')}
        result = deploy_module('migrate-config').migrate(mounts['/baseline'], source,
                    mounts['/prepared'] / 'config', release=supplied)
        if changed:
            path = source / 'packages.toml'
            path.write_text(path.read_text() + '# concurrent administrator edit\n')
        return json.dumps(result)

    monkeypatch.setattr(catalogs, 'run', run)
    monkeypatch.setattr(catalogs, 'resolve_image', lambda *_: {'image': OLD})
    monkeypatch.setattr(catalogs, 'export_image_tree', lambda engine, image, path, destination: shutil.copytree(original, destination))
    prepared, record = catalogs.prepare(service, NEW, transaction, OLD)
    loaded = config.load(prepared / 'tracker.toml')
    assert loaded['packages']['widget']['monitors']['security']['product'] == 'new'
    assert 'added' in loaded['packages'] and loaded['obs']['project'] == 'operator-project'
    assert record['ownership'] == 'release' and record['baseline_image'] == OLD
    assert before == {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    changed = True
    with pytest.raises(ValueError, match='changed during'):
        catalogs.prepare(service, NEW, transaction, OLD)
    assert service.settings['config'] == source
    assert (source / 'packages.toml').read_text().endswith('# concurrent administrator edit\n')


def test_config_selection_accepts_existing_noncanonical_mount_spelling(deployment, monkeypatch):
    reference, unit, backups, _ = deployment
    config = unit.parent / 'config'
    alias = config / '..' / 'config'
    unit.write_text(unit.read_text().replace(f'Volume={config}:', f'Volume={alias}:'))
    proposed_config(monkeypatch, deployment)
    result = worker.upgrade(reference, unit, backups, apply=True, catalog_baseline=OLD)
    assert result['status'] == 'ready'
    assert operations.quadlet(unit.read_text())['config'].name == 'prepared'


@pytest.mark.parametrize('entry', [launcher, worker])
@pytest.mark.parametrize('baseline', ['ghcr.io/example/lookout:main', '--invalid', ''])
def test_invalid_original_image_is_rejected_before_engine_or_service_actions(deployment, monkeypatch, entry, baseline):
    reference, unit, backups, _ = deployment
    monkeypatch.setattr(entry, 'Quadlet', lambda *_: pytest.fail('validate baseline before reading live deployment'))
    with pytest.raises(ValueError):
        entry.upgrade(reference, unit, backups, apply=True, catalog_baseline=baseline)
    assert not list(backups.iterdir())
