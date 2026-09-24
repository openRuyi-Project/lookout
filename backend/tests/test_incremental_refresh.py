"""The scheduled path selects work; cached evidence retains its real age and scope."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from tracker import api, collector, config as cfg, monitor, nv, spec_git, native_spec, state, view
from tracker.monitor_model import version_query
from test_core import FakeOBS


def test_automatic_upstream_subset_retries_and_noop(config, snapshot, tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    clock = [now]
    monkeypatch.setattr(state, 'utcnow', lambda: clock[0].isoformat())
    config['native'].pop('not-in-obs')
    config.update(nv_digest='rules', config_digest='cfg')
    snapshot['native_ids'] = list(config['native'])
    snapshot['nv_digest'] = 'rules'
    snapshot['components']['nvchecker']['options_fingerprint'] = cfg.track_fingerprint({})
    for fact in snapshot['tracks'].values():
        fact.update(attempted_at=now.isoformat(), fetched_at=now.isoformat())
    config['native']['binutils']['prefix'] = 'v'
    db = tmp_path / 'snapshot.db'
    state.commit(db, snapshot)
    monkeypatch.setattr(cfg, 'load', lambda _: config)
    selected = []
    failure = [True]
    def run(c, old, at, tracks, on_results):
        selected.append(tracks)
        events = '' if failure[0] else '\n'.join(json.dumps(dict(name=n, event='updated', version='4.2')) for n in tracks)
        return nv.import_events(events, {n:c['native'][n] for n in tracks}, old, at)
    attempt = {}
    first = collector.check_upstreams(config, 'unused', db, run_nv=run, due=True, attempt=attempt)
    assert selected == [['binutils']] and attempt['selected_track_count'] == 1
    assert first['tracks']['binutils']['failures'] == 1
    assert first['tracks']['widget@3'] == snapshot['tracks']['widget@3']
    clock[0] += timedelta(seconds=60)
    idle = collector.check_upstreams(config, 'unused', db, run_nv=run, due=True, attempt=attempt)
    assert attempt['selected_track_count'] == 0 and idle['generation'] == first['generation']
    clock[0] = now + timedelta(seconds=300)
    failure[0] = False
    recovered = collector.check_upstreams(config, 'unused', db, run_nv=run, due=True)
    assert selected == [['binutils'], ['binutils']]
    assert recovered['tracks']['binutils']['failures'] == 0
    assert recovered['components']['nvchecker']['fetched_at'] == now.isoformat()
    # Current RPM source changes recompute comparison, not upstream identity queries.
    recovered['sources']['binutils']['version'] = '3.9.1'
    state.commit(db, recovered)
    collector.check_upstreams(config, 'unused', db, run_nv=run, due=True)
    assert len(selected) == 2
    clock[0] = now + timedelta(seconds=21600)
    collector.check_upstreams(config, 'unused', db, run_nv=run, due=True)
    assert selected[-1] == ['widget@3', 'widget@4']
    # Removed rules are pruned without any provider request.
    config['native'].pop('widget@4'); config['nv_digest'] = 'removed'
    latest = collector.check_upstreams(config, 'unused', db, run_nv=run, due=True)
    assert 'widget@4' not in latest['tracks'] and len(selected) == 3


def test_query_dependencies_rebind_source_without_requery(config, snapshot, tmp_path, monkeypatch):
    snapshot['sources'] = {'binutils': snapshot['sources']['binutils']}
    config.update(monitors={'enabled':['fixture']}, nv_digest='rules', config_digest='cfg')
    monkeypatch.setattr(cfg, 'load', lambda _: config)
    calls = []
    adapter = SimpleNamespace(VERSION=1, HOSTS=set(), inputs=lambda *args: {'identity':'fixture'},
                              query_subject=version_query,
                              check=lambda subject, *args: calls.append(subject.copy()) or
                              {'status':'ok', 'findings':[], 'note':None})
    monkeypatch.setitem(monitor.REGISTRY, 'fixture', adapter)
    io = SimpleNamespace(for_hosts=lambda *args, **kwargs: None)
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    first = monitor.collect(config, 'unused', db, io=io)
    old = first['monitors']['binutils']['fixture']
    first['sources']['binutils']['srcmd5'] = 'comment-only-change'
    state.commit(db, first)
    rebound = monitor.collect(config, 'unused', db, io=io)['monitors']['binutils']['fixture']
    assert len(calls) == 1 and rebound['subject']['revision'] == 'comment-only-change'
    for key in ('fingerprint', 'checked_at', 'evidence_revision', 'changed_at'):
        assert rebound[key] == old[key]
    updated = state.read(db)
    updated['sources']['binutils']['version'] = '4.0'
    state.commit(db, updated)
    monitor.collect(config, 'unused', db, io=io)
    assert len(calls) == 2
    # Ports without an explicit dependency hook keep safe revision invalidation.
    del adapter.query_subject
    current = state.read(db)
    before = monitor.plan(config, current, 'binutils', 'fixture')['fingerprint']
    current['sources']['binutils']['srcmd5'] = 'next-revision'
    assert monitor.plan(config, current, 'binutils', 'fixture')['fingerprint'] != before


def test_git_delta_rename_retry_macros_and_rewritten_history(config, snapshot, tmp_path, monkeypatch):
    repo = tmp_path / 'repo'; repo.mkdir()
    def git(*args):
        return subprocess.run(['git','-C',str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()
    git('init','-qb','main')
    def write(name, text):
        path=repo/'SPECS'/name/(name+'.spec'); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
    def commit():
        git('add','.');git('-c','user.name=Fixture','-c','user.email=test@example.invalid','commit','-qm','fixture')
    write('binutils','1.0');write('foo3','1.0');commit()
    config['spec'] = dict(repo=str(repo), macro_package='macros', changelog_limit=20, fetch_timeout_seconds=30, interval_seconds=60)
    snapshot['sources'] = {n:snapshot['sources'][n] for n in ('binutils','foo3')}
    db=tmp_path/'snapshot.db';state.commit(db,snapshot)
    monkeypatch.setattr(spec_git,'fetch',lambda *a,**k:(True,None))
    parsed=Counter(); broken=[False]
    def describe(data, macros=()):
        parsed[data.decode()] += 1
        return {'metadata':None if broken[0] else {'version':data.decode()},
                'metadata_error':'parse failed' if broken[0] else None,
                'native_query':{'context':{'resolver':native_spec.RESOLVER,'additional_macros':[p for p,_ in macros]}}}
    first=collector.check_specs(config,db,describe)
    assert first['components']['spec_git']['mode']=='full' and parsed=={'1.0':2}
    write('binutils','2.0');commit();broken[0]=True
    second=collector.check_specs(config,db,describe)
    assert second['components']['spec_git']['selected_packages']==1 and parsed=={'1.0':2,'2.0':1}
    broken[0]=False
    retried=collector.check_specs(config,db,describe)
    assert not retried['specs']['binutils']['error'] and parsed['2.0']==2
    # Rename leaves the old OBS inventory member explicit/missing, not silently removed.
    git('mv','SPECS/foo3','SPECS/foo4');commit()
    renamed=collector.check_specs(config,db,describe)
    assert renamed['specs']['foo3']['error']=='SPEC not found in clone'
    assert 'foo3' in renamed['sources']
    renamed['sources']['foo4']=deepcopy(renamed['sources']['foo3']);state.commit(db,renamed)
    added=collector.check_specs(config,db,describe)
    assert added['specs']['foo4']['metadata']['version']=='1.0'
    macro=repo/'SPECS/macros/macros.test';macro.parent.mkdir();macro.write_text('%fixture 1\n');commit()
    changed=collector.check_specs(config,db,describe)
    assert changed['components']['spec_git']['mode']=='full' and parsed['2.0']==3
    # Create a root commit of the same tree: no ancestor checkpoint is trusted.
    tree=git('rev-parse','HEAD^{tree}')
    root=git('-c','user.name=Fixture','-c','user.email=test@example.invalid','commit-tree',tree,'-m','rewritten')
    git('update-ref','HEAD',root)
    assert collector.check_specs(config,db,describe)['components']['spec_git']['mode']=='full'


@pytest.mark.parametrize('provider_status', ['ok', 'unsupported', 'partial', 'error'])
def test_source_recovery_reuses_the_same_fresh_provider_result(config, snapshot, tmp_path, monkeypatch, provider_status):
    snapshot['sources'] = {'binutils': snapshot['sources']['binutils']}
    config.update(monitors={'enabled': ['fixture']}, config_digest='cfg', nv_digest='rules')
    monkeypatch.setattr(cfg, 'load', lambda _: config)
    calls = []
    def check(*args):
        calls.append(1)
        if provider_status == 'error':
            raise TimeoutError('provider unavailable')
        return {'status': provider_status, 'findings': [], 'note': None}
    monkeypatch.setitem(monitor.REGISTRY, 'fixture', SimpleNamespace(
        VERSION=1, HOSTS=set(), query_subject=version_query, inputs=lambda *a: {},
        check=check))
    db = tmp_path / 'snapshot.db'
    io = SimpleNamespace(for_hosts=lambda *a, **kw: None)
    state.commit(db, snapshot)
    first = monitor.collect(config, 'unused', db, io=io)
    first['sources']['binutils']['error'] = 'source unavailable'
    state.commit(db, first)
    blocked = monitor.collect(config, 'unused', db, io=io)
    assert len(calls) == 1
    old = first['monitors']['binutils']['fixture']
    fact = blocked['monitors']['binutils']['fixture']
    assert fact['status'] == provider_status and fact['input_status'] == 'unsupported'
    for key in ('checked_at', 'attempted_at', 'changed_at', 'evidence_revision', 'failures', 'error'):
        assert fact.get(key) == old.get(key)
    from tracker import monitor_model
    assert monitor_model.project(blocked, 'binutils', datetime.now(timezone.utc))['checks'][0]['status'] == 'input_unavailable'
    blocked['sources']['binutils']['error'] = None
    state.commit(db, blocked)
    recovered = monitor.collect(config, 'unused', db, io=io)
    assert len(calls) == 1 and recovered['monitors']['binutils']['fixture']['status'] == provider_status
    assert recovered['monitors']['binutils']['fixture']['checked_at'] == old['checked_at']
    # A real query change still invalidates the result.
    recovered['sources']['binutils']['version'] = '3.9.1'
    state.commit(db, recovered)
    monitor.collect(config, 'unused', db, io=io)
    assert len(calls) == 2


def test_legacy_source_gate_is_rechecked_after_recovery(config, snapshot, tmp_path, monkeypatch):
    snapshot['sources'] = {'binutils': snapshot['sources']['binutils']}
    config.update(monitors={'enabled': ['fixture']}, config_digest='cfg', nv_digest='rules')
    monkeypatch.setattr(cfg, 'load', lambda _: config)
    calls = []
    monkeypatch.setitem(monitor.REGISTRY, 'fixture', SimpleNamespace(
        VERSION=1, HOSTS=set(), inputs=lambda *a: {}, query_subject=version_query,
        check=lambda *a: calls.append(1) or {'status': 'ok', 'findings': [], 'note': None}))
    snapshot['sources']['binutils']['error'] = 'source unavailable'
    legacy = monitor.plan(config, snapshot, 'binutils', 'fixture')
    legacy.update(checked_at=state.utcnow(), attempted_at=state.utcnow())
    snapshot['monitors'] = {'binutils': {'fixture': legacy}}
    db = tmp_path / 'state.db'
    io = SimpleNamespace(for_hosts=lambda *a, **kw: None)
    state.commit(db, snapshot)
    blocked = monitor.collect(config, 'unused', db, io=io)
    assert not calls and blocked['monitors']['binutils']['fixture']['status'] == 'pending'
    blocked['sources']['binutils']['error'] = None
    state.commit(db, blocked)
    recovered = monitor.collect(config, 'unused', db, io=io)
    assert calls == [1] and recovered['monitors']['binutils']['fixture']['status'] == 'ok'
    monitor.collect(config, 'unused', db, io=io)
    assert calls == [1]


def test_source_gate_does_not_postpone_expired_checks(config, snapshot, tmp_path, monkeypatch):
    snapshot['sources'] = {'binutils': snapshot['sources']['binutils']}
    config.update(monitors={'enabled': ['fixture']}, config_digest='cfg', nv_digest='rules')
    monkeypatch.setattr(cfg, 'load', lambda _: config)
    calls = []
    monkeypatch.setitem(monitor.REGISTRY, 'fixture', SimpleNamespace(
        VERSION=1, HOSTS=set(), inputs=lambda *a: {}, query_subject=version_query,
        check=lambda *a: calls.append(1) or {'status': 'ok', 'findings': [], 'note': None}))
    db = tmp_path / 'state.db'
    io = SimpleNamespace(for_hosts=lambda *a, **kw: None)
    state.commit(db, snapshot)
    checked = monitor.collect(config, 'unused', db, io=io)
    fact = checked['monitors']['binutils']['fixture']
    fact.update(checked_at='2000-01-01T00:00:00+00:00', attempted_at='2000-01-01T00:00:00+00:00',
                input_status='unsupported')
    state.commit(db, checked)
    monitor.collect(config, 'unused', db, io=io)
    assert len(calls) == 2
