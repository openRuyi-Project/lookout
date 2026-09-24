from pathlib import Path
import tomllib
import tomlkit
import pytest
from tracker import config as cfg, nv, state

ROOT=Path(__file__).resolve().parents[2]

def test_actual_native_config():
    c=cfg.load(ROOT/'config/tracker.toml')
    assert c['native'] and all(e.get('source') != 'manual' for e in c['native'].values())
    assert Path(c['nvpath']).name=='nvchecker.toml'
    assert c['native']['python-requests']['source'] == 'pypi'  # promoted discovery candidate is explicit
    assert cfg.binding(c,'openssl')['track_label']=='3.x'
    assert not (ROOT/'config/nvchecker.d').exists()
    assert 'nvtext' not in c

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
def test_invalid_timer_policy_rejected(tmp_path,key,value):
    text=(ROOT/'config/tracker.toml').read_text()
    import re
    text=re.sub(r'^'+key+r' = .*$',key+' = '+value,text,flags=re.M)
    p=tmp_path/'tracker.toml';p.write_text(text)
    with pytest.raises(ValueError):cfg.load(p)

def test_spec_repo_env_override_enables_source(monkeypatch):
    # No [spec] table in the real config: the source is off unless env enables it.
    monkeypatch.delenv('TRACKER_SPEC_REPO', raising=False)
    assert cfg.load(ROOT/'config/tracker.toml')['spec']['repo'] is None
    # The container image sets TRACKER_SPEC_REPO to the clone path on its data volume.
    monkeypatch.setenv('TRACKER_SPEC_REPO', '/data/spec-full.git')
    assert cfg.load(ROOT/'config/tracker.toml')['spec']['repo'] == '/data/spec-full.git'


def test_unchanged_configuration_is_checked_without_parsing(config, configured_path, monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail('a loaded configuration must not be parsed again for publication')
    monkeypatch.setattr(cfg, 'load', unexpected)
    monkeypatch.setattr(cfg.tomllib, 'loads', unexpected)
    before = {path: path.read_bytes() for path in (configured_path, Path(config['nvpath']))}
    cfg.require_unchanged(config, configured_path)
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize('change', ['rule', 'binding', 'operator_option', 'comment'])
def test_configuration_guard_covers_every_loaded_input(config, configured_path, change):
    if change == 'rule':
        path = Path(config['nvpath'])
        text = path.read_text().replace('source = "manual"', 'source = "pypi"', 1)
    else:
        path = configured_path
        text = path.read_text()
        if change == 'binding':
            text = text.replace('3.x', 'new-line', 1)
        elif change == 'operator_option':
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
