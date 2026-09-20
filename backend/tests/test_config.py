from pathlib import Path
import tomllib
import pytest
from tracker import config as cfg, nv, state

ROOT=Path(__file__).resolve().parents[2]

def test_actual_native_config():
    snapshot = {
        'sources': {'python-requests': {'version': '2.32.5'}},
        'specs': {'python-requests': {
            'metadata': {'name': 'python-requests', 'version': '2.32.5',
                         'sources': [{'number': 0, 'url': 'https://files.pythonhosted.org/packages/source/r/requests/requests-2.32.5.tar.gz'}]},
            'native_query': {'spec_sha256': 'a' * 64, 'context': {'resolver': 6}},
        }},
    }
    c=cfg.load(ROOT/'config/tracker.toml', snapshot=snapshot)
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

@pytest.mark.parametrize('key,value', [('obs_interval_seconds','0'),
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
