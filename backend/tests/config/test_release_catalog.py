"""Release catalogs evolve; operator overrides and unrelated evidence do not."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import shutil
import tomllib

import pytest
import tomlkit

from tests.helpers.config import setup_config
from tracker import config, config_change, state
from tracker.monitors import runner
from tracker.monitors.version import nvchecker
from tracker.package import package_location, rule_location

ROOT = Path(__file__).resolve().parents[3]


def deploy_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, values):
    path.write_text(tomlkit.dumps(values))


def release(tmp_path):
    path = setup_config(tmp_path / 'release')
    write(path.parent / 'packages.toml', {'widget': {'monitors': {
        'security': {'vendor': 'fixture', 'product': 'old', 'part': 'o'},
        'eol': {'product': 'fixture', 'cycle_parts': 1}}}})
    text = path.read_text()
    path.write_text('distribution_config="distribution.toml"\n' + text)
    write(path.parent / 'distribution.toml', {'dependencies': {'python': 'python'},
        'buildsystems': {'fixture': {'background': '#112233', 'foreground': '#ffffff'}}})
    return path


def initialized(tmp_path):
    source = release(tmp_path)
    target = tmp_path / 'operator'
    deploy_module('init-config').initialize(source.parent, target)
    return source, target / 'tracker.toml'


def version_subject(loaded):
    now = state.utcnow()
    snapshot = state.empty()
    snapshot.update(bindings=loaded['packages'], native_ids=list(loaded['native']), stale_after_seconds=86400)
    snapshot['inventory'] = {'widget': 'widget'}
    snapshot['sources']['widget'] = state.success({}, {'version': '1.2.3', 'srcmd5': 'fixture-revision'}, now)
    snapshot['components']['nvchecker'] = {'options_fingerprint': config.track_fingerprint(loaded['native_options'])}
    snapshot['tracks'] = {name: state.success({}, {'version': '1.2.3',
        'configuration_fingerprint': config.track_fingerprint(entry)}, now) for name, entry in loaded['native'].items()}
    return snapshot


def test_same_operator_config_follows_catalog_changes_without_writes(tmp_path):
    source, target = initialized(tmp_path)
    before = {p: p.read_bytes() for p in target.parent.rglob('*') if p.is_file()}
    old = config.load(target)
    snapshot = version_subject(old)
    old_query = runner.plan(old, snapshot, 'widget', 'security')
    write(source.parent / 'packages.toml', {'widget': {'monitors': {
        'security': {'vendor': 'fixture', 'product': 'new'},
        'eol': {'product': 'fixture', 'cycle_parts': 1}}},
        'added': {'monitors': {'security': {'vendor': 'fixture', 'product': 'added'}}}})
    document = tomllib.loads((source.parent / 'native.toml').read_text())
    document['added'] = {'source': 'pypi', 'pypi': 'added'}
    write(source.parent / 'native.toml', document)
    loaded = config.load(target)
    assert loaded['packages']['added']['monitors']['security']['product'] == 'added'
    assert 'added' in loaded['native']
    assert old_query['fingerprint'] != runner.plan(loaded, snapshot, 'widget', 'security')['fingerprint']
    assert nvchecker.due_names(loaded, snapshot, datetime.now(timezone.utc)) == ['added']
    assert before == {p: p.read_bytes() for p in target.parent.rglob('*') if p.is_file()}


def overrides(target):
    document = tomllib.loads(target.read_text())
    document['package_overrides'] = 'identities.toml'
    document['collector']['version_overrides'] = 'versions.toml'
    write(target, document)
    write(target.parent / 'identities.toml', {'widget': {'monitors': {
        'security': {'vendor': 'operator', 'product': 'explicit'}}}})
    write(target.parent / 'versions.toml', {'__config__': {'keyfile': 'keys.toml'},
        'widget': {'source': 'pypi', 'pypi': 'operator-widget'}})
    (target.parent / 'keys.toml').write_text('# private operator keyfile\n')


def test_adapter_override_replaces_one_identity_not_all_monitors(tmp_path):
    source, target = initialized(tmp_path)
    overrides(target)
    loaded = config.load(target)
    assert loaded['packages']['widget']['monitors'] == {
        'security': {'vendor': 'operator', 'product': 'explicit'},
        'eol': {'product': 'fixture', 'cycle_parts': 1}}
    assert loaded['native']['widget'] == {'source': 'pypi', 'pypi': 'operator-widget'}
    assert package_location(loaded, 'widget', 'monitors', 'security')['file'] == str(target.parent / 'identities.toml')
    assert package_location(loaded, 'widget', 'monitors', 'eol')['file'] == str(source.parent / 'packages.toml')
    assert rule_location(loaded, 'widget')['file'] == str(target.parent / 'versions.toml')
    with nvchecker.command_config(loaded, None) as path:
        native = tomllib.loads(Path(path).read_text())
        assert native['widget'] == loaded['native']['widget']
        assert native['__config__']['keyfile'] == str(target.parent / 'keys.toml')
        assert native['__config__']['http_timeout'] == 20


def test_explicit_disable_does_not_fall_back_to_automatic_identity(tmp_path):
    _, target = initialized(tmp_path)
    overrides(target)
    write(target.parent / 'identities.toml', {'widget': {'monitors': {'security': False}}})
    loaded = config.load(target)
    proposed = runner.plan(loaded, version_subject(loaded), 'widget', 'security')
    assert proposed['inputs'] is None and proposed['status'] == 'not_applicable'
    document = tomllib.loads(target.read_text())
    document['collector']['exclude_tracks'] = ['widget']
    write(target, document)
    loaded = config.load(target)
    assert 'widget' not in loaded['native']


@pytest.mark.parametrize('input_name', ['packages.toml', 'native.toml', 'distribution.toml'])
def test_every_release_input_is_guarded_even_when_shared(tmp_path, input_name):
    source, target = initialized(tmp_path)
    loaded = config.load(target)
    file = source.parent / input_name
    file.write_text(file.read_text() + '# changed after collection began\n')
    with pytest.raises(ValueError, match='configuration changed'):
        config.require_unchanged(loaded)


def test_unrelated_identity_and_style_changes_keep_query_fingerprints(tmp_path):
    source, target = initialized(tmp_path)
    loaded = config.load(target)
    snapshot = version_subject(loaded)
    old = runner.plan(loaded, snapshot, 'widget', 'security')
    write(source.parent / 'distribution.toml', {'dependencies': {'python': 'operator-python'},
        'buildsystems': {'fixture': {'background': '#223344', 'foreground': '#ffffff'}}})
    document = tomllib.loads((source.parent / 'packages.toml').read_text())
    document['unrelated'] = {'monitors': {'security': {'vendor': 'fixture', 'product': 'other'}}}
    write(source.parent / 'packages.toml', document)
    updated = config.load(target)
    assert runner.plan(updated, snapshot, 'widget', 'security')['fingerprint'] == old['fingerprint']
    assert nvchecker.due_names(updated, snapshot, datetime.now(timezone.utc)) == []
    assert updated['openruyi']['dependencies']['python'] == 'operator-python'


def test_promote_first_overrides_does_not_copy_or_modify_release_catalog(tmp_path):
    source, target = initialized(tmp_path)
    paths = []
    for name in ('base', 'candidate', 'runtime'):
        shutil.copytree(target.parent, tmp_path / name)
        paths.append(tmp_path / name / 'tracker.toml')
    overrides(paths[1])
    write(paths[1].parent / 'versions.toml', {'widget': {'source': 'pypi', 'pypi': 'operator-widget'}})
    (paths[2].parent / 'private.key').write_text('private fixture bytes')
    before = {p: p.read_bytes() for p in source.parent.rglob('*') if p.is_file()}
    review, prepared = tmp_path / 'review', tmp_path / 'prepared'
    record = config_change.plan(*paths, review)
    assert set(record['proposed_hashes']) == {'tracker.toml', 'versions.toml', 'identities.toml'}
    assert record['changed_tracks'] == ['widget'] and record['changed_bindings'] == ['widget']
    config_change.apply(review, paths[2], prepared)
    loaded = config.load(prepared / 'tracker.toml')
    assert loaded['packages']['widget']['monitors']['security']['vendor'] == 'operator'
    assert loaded['packages']['widget']['monitors']['eol']['product'] == 'fixture'
    assert loaded['native']['widget']['pypi'] == 'operator-widget'
    assert (prepared / 'private.key').read_text() == 'private fixture bytes'
    assert before == {p: p.read_bytes() for p in source.parent.rglob('*') if p.is_file()}


def test_catalog_drift_rejects_reviewed_override_promotion(tmp_path):
    source, target = initialized(tmp_path)
    review = tmp_path / 'review'
    config_change.plan(target, target, target, review)
    file = source.parent / 'packages.toml'
    file.write_text(file.read_text() + '# new image catalog\n')
    with pytest.raises(ValueError, match='catalog drift'):
        config_change.apply(review, target, tmp_path / 'prepared')
    assert not (tmp_path / 'prepared').exists()


def test_migration_preserves_operator_edits_and_adopts_new_defaults(tmp_path):
    source = release(tmp_path)
    baseline, operator, supplied = [tmp_path / name for name in ('baseline', 'operator', 'supplied')]
    for directory in (baseline, operator, supplied):
        shutil.copytree(source.parent, directory)
    policies = tomllib.loads((operator / 'packages.toml').read_text())
    policies['widget']['monitors']['eol'] = {'product': 'operator-cycle', 'cycle_parts': 2}
    write(operator / 'packages.toml', policies)
    document = tomllib.loads((operator / 'tracker.toml').read_text())
    document['obs']['project'] = 'operator-project'
    document['collector']['obs_interval_seconds'] = 90
    document['openruyi'] = {'dependencies': {'python': 'operator-python'}}
    write(operator / 'tracker.toml', document)
    (operator / 'private.key').write_text('fixture secret retained privately')
    write(supplied / 'packages.toml', {'widget': {'monitors': {
        'security': {'vendor': 'fixture', 'product': 'new'}}},
        'added': {'monitors': {'security': {'vendor': 'fixture', 'product': 'added'}}}})
    before = {p: p.read_bytes() for p in operator.rglob('*') if p.is_file()}
    destination = tmp_path / 'prepared'
    result = deploy_module('migrate-config').migrate(baseline, operator, destination, release=supplied)
    loaded = config.load(destination / 'tracker.toml')
    assert result['version_overrides'] == [] and result['package_overrides'] == ['widget']
    assert loaded['packages']['widget']['monitors']['security']['product'] == 'new'
    assert loaded['packages']['widget']['monitors']['eol'] == {'product': 'operator-cycle', 'cycle_parts': 2}
    assert loaded['packages']['added']['monitors']['security']['product'] == 'added'
    assert loaded['obs']['project'] == 'operator-project'
    assert loaded['collector']['obs_interval_seconds'] == 90
    assert loaded['openruyi']['dependencies']['python'] == 'operator-python'
    assert (destination / 'private.key').read_text() == 'fixture secret retained privately'
    assert before == {p: p.read_bytes() for p in operator.rglob('*') if p.is_file()}
    assert not (destination / 'packages.toml').exists()


def test_migration_preserves_removed_monitors_tracks_and_relative_credentials(tmp_path):
    source = release(tmp_path)
    baseline, operator = tmp_path / 'baseline', tmp_path / 'operator'
    for directory in (baseline, operator):
        shutil.copytree(source.parent, directory)
    write(operator / 'packages.toml', {})
    write(operator / 'native.toml', {'__config__': {'http_timeout': 20, 'keyfile': 'private.key'}})
    (operator / 'private.key').write_text('fixture private file')
    destination = tmp_path / 'prepared'
    deploy_module('migrate-config').migrate(baseline, operator, destination, release=source.parent)
    loaded = config.load(destination / 'tracker.toml')
    assert loaded['native'] == {}
    assert loaded['packages']['widget']['monitors'] == {'eol': False, 'security': False}
    assert loaded['native_options']['keyfile'] == 'private.key'
    assert (destination / 'private.key').read_text() == 'fixture private file'


def test_missing_migration_baseline_never_creates_a_destination(tmp_path):
    source = release(tmp_path)
    with pytest.raises(ValueError, match='does not exist'):
        deploy_module('migrate-config').migrate(tmp_path / 'unknown', source.parent, tmp_path / 'prepared')
    assert not (tmp_path / 'prepared').exists()


def test_implicit_native_catalog_is_not_frozen_by_initialization(tmp_path):
    source = release(tmp_path)
    native = source.parent / 'native.toml'
    native.rename(source.parent / 'nvchecker.toml')
    document = tomllib.loads(source.read_text())
    document['collector'].pop('nvchecker_config')
    write(source, document)
    target = tmp_path / 'operator'
    deploy_module('init-config').initialize(source.parent, target)
    loaded = config.load(target / 'tracker.toml')
    assert loaded['nvpath'] == str(source.parent / 'nvchecker.toml')
    assert not (target / 'nvchecker.toml').exists()


def test_initializer_keeps_relative_external_catalog_intact(tmp_path):
    source = release(tmp_path)
    external = tmp_path / 'native.toml'
    (source.parent / 'native.toml').rename(external)
    document = tomllib.loads(source.read_text())
    document['collector']['nvchecker_config'] = '../native.toml'
    write(source, document)
    original = external.read_bytes()
    target = tmp_path / 'operator'
    deploy_module('init-config').initialize(source.parent, target)
    assert external.read_bytes() == original
    assert config.load(target / 'tracker.toml')['nvpath'] == str(external)


@pytest.mark.parametrize('operation', ['init-config', 'migrate-config'])
def test_catalog_destination_cannot_reenter_source_through_symlink(tmp_path, operation):
    source = release(tmp_path)
    alias = tmp_path / 'source-alias'
    alias.symlink_to(source.parent, target_is_directory=True)
    destination = alias / 'prepared'
    module = deploy_module(operation)
    with pytest.raises(ValueError, match='outside'):
        if operation == 'init-config':
            module.initialize(source.parent, destination)
        else:
            module.migrate(source.parent, source.parent, destination, release=source.parent)
    assert not (source.parent / 'prepared').exists()
