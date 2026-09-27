from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from tests.helpers.obs import FakeOBS
from tracker import collector, config as cfg, state
from tracker.monitors.build import obs
from tracker.monitors.version import nvchecker as nv
from tracker.readmodel import snapshot as view

@pytest.mark.parametrize('a,b,result', [('3.9','3.10','outdated'),('3.10','3.9','ahead'),('3.10','3.10','current'),('1.0~rc1','1.0','outdated'),('1.0^git1','1.0','ahead'),('1.0^git1','1.0.1','outdated')])
def test_native_rpm(a,b,result):
    import rpm
    assert state.compare(a,b) == result

@pytest.mark.parametrize('a,b', [(None,'1'),('1:3.0-2','3.1'),('%{version}','2'),('unknown version','1'),('1.0-4','2.0'),('','1')])
def test_not_full_evr_or_macro(a,b):
    assert state.compare(a,b) == 'unknown'

def test_explicit_incomparable():
    assert state.compare('20250912','1.2',False) == 'unknown'

@pytest.mark.parametrize('value',['0.7+git20231216.MACRO','MACRO','%{__default_python3_version}'])
def test_obs_macro_is_not_a_reliable_version(value,snapshot):
    assert state.compare(value,'999')=='unknown'
    snapshot['sources']['binutils']['version']=value
    rows,_=view.project_monitors(snapshot)
    row=next(r for r in rows if r['name']=='binutils')
    assert row['monitors']['source']['data']['version'] is None and row['monitors']['version']['data']['relation']=='unknown'
    assert row['monitors']['source']['data']['obs']['raw_version']==value

def test_obs_scope_cannot_mix(config,snapshot):
    config['obs']={**config['obs'],'project':'different-project'}
    with pytest.raises(ValueError,match='scope'):
        collector.collect(config,snapshot,FakeOBS(config),state.utcnow())

def test_sources_not_tracks_create_rows(snapshot):
    rows,_=view.project_monitors(snapshot)
    assert len(rows)==5
    assert not any(r['name'] in ('not-in-obs','widget@4','foo3:tools') for r in rows)

def test_tracks_never_mix(snapshot):
    rows,_=view.project_monitors(snapshot)
    foo3=next(r for r in rows if r['name']=='foo3')
    assert (foo3['monitors']['version']['data']['latest'],foo3['monitors']['version']['data']['relation'])==('3.10.0','current')
    assert foo3['monitors']['version']['data']['watch'][0]['version']=='4.2.0'

def test_rva23_and_rva20_preserve_flavor_and_log(snapshot):
    rows,_=view.project_monitors(snapshot)
    foo3=next(r for r in rows if r['name']=='foo3')
    a,b,c=foo3['monitors']['build']['data']['targets']
    assert a['target']=='rva23' and a['repository']=='riscv64' and a['raw_status']=='succeeded'
    assert b['target']=='rva20' and b['architecture']=='riscv64' and b['raw_status']=='failed'
    assert 'foo3%3Atools/rva20/riscv64' in b['log_url']
    assert len(b['flavors'])==2
    assert a['matches_source'] is b['matches_source'] is None

def test_disabled_excluded_not_failures(snapshot):
    rows,_=view.project_monitors(snapshot)
    row=next(r for r in rows if r['name']=='foo4')
    assert all(b['kind']!='error' for b in row['monitors']['build']['data']['targets'])

def test_failure_retains_value_but_not_current(snapshot):
    before=deepcopy(snapshot['tracks']['widget@3'])
    snapshot['tracks']['widget@3']=state.failure(before,'timeout',state.utcnow())
    rows,_=view.project_monitors(snapshot)
    row=next(r for r in rows if r['name']=='foo3')
    assert row['monitors']['version']['data']['latest']=='3.10.0' and row['monitors']['version']['data']['relation']=='unknown'
    assert row['monitors']['version']['data']['upstream']['fetched_at']==before['fetched_at']

def test_stale_changes_without_collection(snapshot):
    rows,_=view.project_monitors(snapshot,datetime.now(timezone.utc)+timedelta(days=2))
    assert all(r['monitors']['version']['data']['relation']=='unknown' and any('attention' in m['dimensions'].get('view', []) for m in r['monitors'].values()) for r in rows)

def test_source_revision_mismatch():
    data=b'<sourceinfo package="foo" srcmd5="new"><version>2</version></sourceinfo>'
    with pytest.raises(ValueError,match='revision'):
        obs.source_info(data,'foo','old')

@pytest.mark.parametrize('data', [b'<directory/>',b'<sourceinfo package="foo"><version>unknown</version></sourceinfo>',b'<sourceinfo package="bar"><version>1</version></sourceinfo>'])
def test_source_version_not_guessed(data):
    with pytest.raises(ValueError):obs.source_info(data,'foo')

def test_safe_xml():
    with pytest.raises(Exception):obs.inventory(b'<!DOCTYPE directory [<!ENTITY x SYSTEM "file:///etc/passwd">]><directory>&x;</directory>')

@pytest.mark.parametrize('status,expected_requests',[(404,1),(403,1),(503,3)])
def test_obs_does_not_burst_retry_permanent_errors(config,monkeypatch,status,expected_requests):
    import httpx
    calls=[]
    def response(request):
        calls.append(request.url)
        return httpx.Response(status,request=request)
    client=obs.Client(config);client.client.close()
    client.client=httpx.Client(transport=httpx.MockTransport(response))
    monkeypatch.setattr(obs.time,'sleep',lambda _:None)
    try:
        with pytest.raises(httpx.HTTPStatusError):client.get('/source/openruyi/missing')
    finally:client.close()
    assert len(calls)==expected_requests

@pytest.mark.parametrize('data', [b'<directory><entry name="x"/><entry name="x"/></directory>',b'<directory><entry name="x:y" originpackage="x"/></directory>',b'<directory><entry name="../x"/></directory>',b'<status/>'])
def test_bad_inventory_not_authoritative(data):
    with pytest.raises(ValueError):obs.inventory(data)

def test_partial_nv_log(snapshot,config):
    now=state.utcnow(); old=snapshot['tracks']
    raw='\n'.join(json.dumps(e) for e in [{'name':'binutils','event':'up-to-date','version':'3.10.0'}, {'name':'widget@3','event':'no-result'}, {'name':'unconfigured','event':'updated','version':'999'}])
    result,error=nv.import_events(raw,config['native'],old,now,'nvchecker exited 1')
    assert result['binutils']['version']=='3.10.0' and result['binutils']['error'] is None
    assert result['widget@3']['version']=='3.10.0' and result['widget@3']['fetched_at']==old['widget@3']['fetched_at']
    assert result['widget@4']['error'] and 'unconfigured' not in result
    assert error=='nvchecker exited 1'

def test_error_dominates_same_track(config):
    raw='{"name":"binutils","event":"updated","version":"999"}\n{"name":"binutils","level":"error"}'
    r,_=nv.import_events(raw,config['native'],{},state.utcnow())
    assert 'version' not in r['binutils'] and r['binutils']['error']

def test_source_secrets_omitted():
    p=cfg.public_source({'source':'jq','url':'https://user:password@example.org/data?token=secret','cmd':'echo secret','httptoken':'secret'})
    assert p=={'source':'jq','url':'https://example.org/data'}

def test_atomic_bad_snapshot(tmp_path,snapshot):
    db=tmp_path/'db';state.commit(db,snapshot)
    bad=deepcopy(snapshot);bad['bad']=object()
    with pytest.raises(TypeError):state.commit(db,bad)
    assert state.read(db)==snapshot
    with sqlite3.connect(db) as conn:assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'

def test_flock_excludes_overlap(tmp_path):
    with state.writer_lock(tmp_path/'db'):
        with pytest.raises(BlockingIOError):
            with state.writer_lock(tmp_path/'db'):pass


def keep_nv(config,previous,now,on_results=None):return previous,None

def test_failed_inventory_never_deletes(config,snapshot):
    new=collector.collect(config,snapshot,FakeOBS(config,failed=('/source/openruyi',)),state.utcnow())
    assert set(new['sources'])==set(snapshot['sources'])
    assert new['sources']['binutils']['version']=='3.9.0'
    assert new['sources']['binutils']['error']

def test_complete_inventory_is_only_delete_authority(config,snapshot):
    new=collector.collect(config,snapshot,FakeOBS(config),state.utcnow())
    assert list(new['sources'])==['binutils']
    assert new['sources']['binutils']['version']=='3.9.0' # mismatched source hash kept old but flagged
    assert new['sources']['binutils']['error']

def test_matching_revision_refreshes_source(config,snapshot):
    new=collector.collect(config,snapshot,FakeOBS(config,new_hash='new-binutils'),state.utcnow())
    assert new['sources']['binutils']['version']=='3.11.0'
    assert new['sources']['binutils']['error'] is None

def test_build_failure_retains_success_time(config,snapshot):
    previous=snapshot['builds']['binutils']['rva23']
    new=collector.refresh_builds(config,snapshot,FakeOBS(config,failed=('/_result',)),state.utcnow())
    actual=new['builds']['binutils']['rva23']
    assert actual['raw_status']==previous['raw_status'] and actual['fetched_at']==previous['fetched_at']
    assert actual['error']

def test_obs_collection_refreshes_source_without_upstreams(config,snapshot):
    # OBS collection refreshes source facts and never touches upstream tracks.
    new=collector.collect(config,snapshot,FakeOBS(config,new_hash='new-binutils'),state.utcnow())
    assert new['sources']['binutils']['version']=='3.11.0'
    assert new['tracks']==snapshot['tracks']

def test_collect_has_no_upstream_entry_point(config,snapshot):
    # collect() cannot launch nvchecker: the OBS phase holds the writer lock, upstreams do not.
    import inspect
    params=inspect.signature(collector.collect).parameters
    assert 'run_nv' not in params and 'include_upstreams' not in params
    new=collector.collect(config,snapshot,FakeOBS(config,new_hash='new-binutils'),state.utcnow())
    assert new['tracks']==snapshot['tracks']

def test_slow_upstream_run_preserves_newer_obs(config,snapshot,tmp_path,monkeypatch):
    db=tmp_path/'snapshot.db';state.commit(db,snapshot)
    config['nv_digest']='fixed'
    monkeypatch.setattr(cfg,'require_unchanged',lambda *_args:None)
    def concurrent_obs(config,prior,now,on_results=None):
        # This write occurs while the external checker is running: no writer lock held.
        with state.writer_lock(db):
            newer=state.read(db);newer['sources']['binutils']['version']='4.0.0'
            newer['generation']+=1;state.commit(db,newer)
        return prior,None
    new=collector.check_upstreams(config,'unused',db,run_nv=concurrent_obs)
    assert new['sources']['binutils']['version']=='4.0.0'
    assert new['generation']==snapshot['generation']+2

def test_config_change_during_upstream_check_is_not_published(config,snapshot,tmp_path,configured_path):
    db=tmp_path/'snapshot.db';state.commit(db,snapshot)
    configured_path.write_text(configured_path.read_text() + '\n# operator changed configuration\n')
    with pytest.raises(ValueError,match='configuration changed'):
        collector.check_upstreams(config,configured_path,db,run_nv=keep_nv)
    assert state.read(db)==snapshot


def test_watch_is_observation_only_and_has_freshness(snapshot):
    from copy import deepcopy
    from datetime import datetime
    from datetime import timedelta
    before = next(r for r in view.project_monitors(snapshot)[0] if r['name'] == 'foo3')
    changed = deepcopy(snapshot)
    changed['tracks']['widget@4'].update(version='999rc1', error='network error')
    row = next(r for r in view.project_monitors(changed)[0] if r['name'] == 'foo3')
    assert (row['monitors']['version']['data']['latest'], row['monitors']['version']['data']['relation'], row['monitors']['version']['dimensions']) == (before['monitors']['version']['data']['latest'], before['monitors']['version']['data']['relation'], before['monitors']['version']['dimensions'])
    assert row['monitors']['version']['data']['watch'][0]['version'] == '999rc1' and row['monitors']['version']['data']['watch'][0]['error'] == 'network error'
    observed = datetime.fromisoformat(changed['tracks']['widget@4']['fetched_at'])
    assert not next(r for r in view.project_monitors(changed, observed)[0] if r['name'] == 'foo3')['monitors']['version']['data']['watch'][0]['stale']
    assert next(r for r in view.project_monitors(changed, observed+timedelta(days=2))[0] if r['name'] == 'foo3')['monitors']['version']['data']['watch'][0]['stale']
