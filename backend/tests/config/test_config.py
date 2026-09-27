from pathlib import Path
import tomllib

import pytest
import tomlkit

from tracker import config as cfg, state
from tracker.monitors.version import nvchecker as nv

ROOT=Path(__file__).resolve().parents[3]

def test_actual_native_config():
    c=cfg.load(ROOT/'config/tracker.toml')
    assert c['native'] and all(e.get('source') != 'manual' for e in c['native'].values())
    assert Path(c['nvpath']).name=='nvchecker.toml'
    assert not (ROOT/'config/nvchecker.d').exists()
    assert 'nvtext' not in c


def test_explicit_provider_identity_and_track_label_are_preserved(configured_path):
    document = tomlkit.parse(configured_path.read_text())
    native_path = configured_path.parent / document['collector']['nvchecker_config']
    native = tomlkit.parse(native_path.read_text())
    native['widget'] = {'source': 'pypi', 'pypi': 'explicit-upstream-identity'}
    native_path.write_text(tomlkit.dumps(native))
    packages_path = configured_path.parent / document['packages_config']
    packages = tomlkit.parse(packages_path.read_text())
    packages['widget'] = {'track_label': 'reviewed-maintenance-line'}
    packages_path.write_text(tomlkit.dumps(packages))
    loaded = cfg.load(configured_path)
    assert loaded['native']['widget'] == {'source': 'pypi', 'pypi': 'explicit-upstream-identity'}
    assert cfg.binding(loaded, 'widget')['track_label'] == 'reviewed-maintenance-line'

def test_two_tracks_same_toml():
    native=tomllib.loads('["foo@3"]\nsource="manual"\nmanual="3.10"\n["foo@4"]\nsource="manual"\nmanual="4.2"')
    assert set(native)=={'foo@3','foo@4'}

def test_duplicate_track_is_rejected():
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads('[foo]\nsource="manual"\n[foo]\nsource="pypi"')

def test_track_change_does_not_relabel_prior_version():
    old={'foo':{'version':'3.10','source':{'source':'pypi'},'fetched_at':'2026-01-01T00:00:00Z','configuration_fingerprint':cfg.track_fingerprint({'source':'pypi','pypi':'foo'})}}
    result,_=nv.import_events('',{'foo':{'source':'jq','url':'https://example.org/other'}},old,state.utcnow(),'timeout')
    assert result['foo'].get('version') is None
    assert result['foo']['previous_configuration']['version']=='3.10'
    assert result['foo']['error']=='timeout'

@pytest.mark.parametrize('key,value', [('obs_interval_seconds','0'), ('build_interval_seconds', '0'),
    ('build_interval_seconds', '9'), ('build_interval_seconds', '300'),
    ('nvchecker_interval_seconds','-1'), ('nvchecker_interval_seconds','true'),
    ('obs_interval_seconds','"60"'), ('nvchecker_interval_seconds','86400')])
def test_invalid_timer_policy_rejected(configured_path,key,value):
    document = tomlkit.parse(configured_path.read_text())
    document['collector'].update(obs_stale_after_seconds=300, stale_after_seconds=86400)
    document['collector'][key] = tomllib.loads('value=' + value)['value']
    configured_path.write_text(tomlkit.dumps(document))
    with pytest.raises(ValueError, match=key):
        cfg.load(configured_path)

def test_spec_repo_env_override_enables_source(configured_path, monkeypatch):
    # The fixture deliberately omits SPEC configuration, regardless of shipped defaults.
    monkeypatch.delenv('TRACKER_SPEC_REPO', raising=False)
    assert cfg.load(configured_path)['spec']['repo'] is None
    # The container image sets TRACKER_SPEC_REPO to the clone path on its data volume.
    monkeypatch.setenv('TRACKER_SPEC_REPO', '/data/spec-full.git')
    assert cfg.load(configured_path)['spec']['repo'] == '/data/spec-full.git'


def test_unchanged_configuration_is_checked_without_parsing(config, configured_path, monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail('a loaded configuration must not be parsed again for publication')
    monkeypatch.setattr(cfg, 'load', unexpected)
    monkeypatch.setattr(cfg.tomllib, 'loads', unexpected)
    before = {Path(name): Path(name).read_bytes() for name in config['input_hashes']}
    cfg.require_unchanged(config, configured_path)
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize('change', ['rule', 'binding', 'operator_option', 'comment'])
def test_configuration_guard_covers_every_loaded_input(config, configured_path, change):
    if change == 'rule':
        path = Path(config['nvpath'])
        text = path.read_text().replace('source = "manual"', 'source = "pypi"', 1)
    elif change == 'binding':
        path = Path(config['packages_path'])
        text = path.read_text().replace('3.x', 'new-line', 1)
    else:
        path = configured_path
        text = path.read_text()
        if change == 'operator_option':
            document = tomlkit.parse(text)
            document['collector']['source_workers'] += 1
            text = tomlkit.dumps(document)
        else:
            text += '\n# reviewer-visible change\n'
    assert text != path.read_text()
    path.write_text(text)
    with pytest.raises(ValueError, match='configuration changed'):
        cfg.require_unchanged(config, configured_path)


@pytest.mark.parametrize('replacement', ['symlink', 'directory', 'missing'])
def test_configuration_guard_rejects_nonregular_native_input(config, configured_path, replacement):
    path = Path(config['nvpath'])
    saved = path.with_suffix('.saved')
    path.rename(saved)
    if replacement == 'symlink':
        path.symlink_to(saved)
    elif replacement == 'directory':
        path.mkdir()
    with pytest.raises(ValueError, match='configuration changed'):
        cfg.require_unchanged(config, configured_path)


def test_configuration_guard_rejects_missing_tracker(config, configured_path):
    configured_path.unlink()
    with pytest.raises(ValueError, match='configuration changed'):
        cfg.require_unchanged(config, configured_path)


@pytest.mark.parametrize('settings', [
    {'extra_macro_packages': ['../escape']},
    {'extra_macro_packages': ['fixture', 'fixture']},
    {'local_sources': {'fixture': ['../escape']}},
    {'local_sources': {'fixture': ['series', 'series']}},
])
def test_spec_auxiliary_input_paths_are_bounded(configured_path, settings):
    document = tomlkit.parse(configured_path.read_text())
    document['spec'] = settings
    configured_path.write_text(tomlkit.dumps(document))
    with pytest.raises(ValueError, match='spec.'):
        cfg.load(configured_path)
