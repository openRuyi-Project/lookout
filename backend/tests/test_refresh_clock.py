"""Observation clocks do not rewrite content or hide failed and missing evidence."""
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


def test_identical_build_poll_keeps_payload_revision_and_projection(config, snapshot, tmp_path, monkeypatch):
    at = datetime.now(timezone.utc).replace(microsecond=0)
    clock = [at]
    monkeypatch.setattr(state, 'utcnow', lambda: clock[0].isoformat())
    monkeypatch.setattr(api, 'time', SimpleNamespace(time=lambda: clock[0].timestamp()))
    requests = []
    class Client(FakeOBS):
        def __init__(self, c, **kwargs):
            super().__init__(c, names=tuple(snapshot['inventory']))
        def get(self, path):
            requests.append(path)
            return super().get(path)
        def close(self):
            pass
    monkeypatch.setattr(collector.obs, 'Client', Client)
    db = tmp_path / 'snapshot.db'
    state.commit(db, snapshot)
    first = collector.collect_builds(config, db)
    def stored():
        with sqlite3.connect(db) as conn:
            return conn.execute('SELECT payload FROM snapshot').fetchone(), conn.execute('SELECT revision FROM snapshot_clock').fetchone()
    original = stored()
    calls = []
    project = view.project_monitors
    monkeypatch.setattr(view, 'project_monitors', lambda *a, **kw: calls.append(1) or project(*a, **kw))
    client = TestClient(api.create_app(db))
    before = client.get('/api/v2/packages/binutils').json()
    clock[0] += timedelta(seconds=15)
    second = collector.collect_builds(config, db)
    after = client.get('/api/v2/packages/binutils').json()
    assert stored() == original and second['generation'] == first['generation']
    assert len(requests) == 2 and len(calls) == 1
    assert after['monitors']['source'] == before['monitors']['source']
    assert after['monitors']['version'] == before['monitors']['version']
    assert after['monitors']['build']['check']['checked_at'] == clock[0].isoformat()
    assert state.read(db) == second
    assert second['builds']['binutils']['rva23']['fetched_at'] != first['builds']['binutils']['rva23']['fetched_at']
    # The lightweight path must never bypass the actual stale-state projection.
    clock[0] += timedelta(days=2)
    assert client.get('/api/v2/packages/binutils').json()['monitors']['build']['check']['stale']
    assert len(calls) == 2
    # Ordinary commits consume the clock atomically and invalidate the content cache.
    second['builds']['binutils']['rva23']['raw_status'] = 'building'
    state.commit(db, second)
    assert stored()[1] != original[1]
    assert client.get('/api/v2/packages/binutils').json()['monitors']['build']['data']['targets'][0]['raw_status'] == 'building'


@pytest.mark.parametrize('old_age,new_offset,before_status,after_status', [
    (20, 0, 'expired', 'ok'),
    (0, 301, 'ok', 'expired'),
])
def test_clock_only_freshness_change_reprojects_rows_and_facets(
        snapshot, tmp_path, monkeypatch, old_age, new_offset, before_status, after_status):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    snapshot['obs_stale_after_seconds'] = 10
    old_stamp = (now - timedelta(seconds=old_age)).isoformat()
    for targets in snapshot['builds'].values():
        for fact in targets.values():
            fact.update(fetched_at=old_stamp, attempted_at=old_stamp)
    snapshot['components']['builds'] = state.success({}, {}, old_stamp)
    db = tmp_path / 'snapshot.db'
    state.commit(db, snapshot)
    revision = state.read_cached(db)[1]
    monkeypatch.setattr(api, 'time', SimpleNamespace(time=lambda: now.timestamp()))
    calls = []
    project = view.project_monitors
    monkeypatch.setattr(view, 'project_monitors', lambda *a, **kw: calls.append(1) or project(*a, **kw))
    client = TestClient(api.create_app(db))
    before = client.get('/api/v2/packages/binutils').json()['monitors']['build']
    assert before['check']['status'] == before_status and len(calls) == 1

    stamp = (now + timedelta(seconds=new_offset)).isoformat()
    patches = collector.build_patch(snapshot, 'builds')
    for targets in patches.values():
        for fact in targets.values():
            fact.update(fetched_at=stamp, attempted_at=stamp)
    assert state.commit_build_heartbeat(db, snapshot, patches, state.success(snapshot['components']['builds'], {}, stamp))
    assert state.read_cached(db)[1] == revision

    after = client.get('/api/v2/packages/binutils').json()['monitors']['build']
    assert after['check']['status'] == after_status and len(calls) == 2
    expected_rows, expected_collection = project(state.read(db), now)
    expected = next(row for row in expected_rows if row['name'] == 'binutils')['monitors']['build']
    assert after['data'] == expected['data']
    listing = client.get('/api/v2/packages', params={'monitor': 'build', 'check': after_status}).json()
    assert listing['total'] == len(snapshot['sources'])
    assert listing['check_statuses'] == {after_status: len(snapshot['sources'])}
    assert listing['collection'] == expected_collection
    assert listing['counts']['attention'] == sum(
        any('attention' in module['dimensions'].get('view', []) for module in row['monitors'].values())
        for row in expected_rows
    )
    assert client.get('/api/v2/packages', params={'monitor': 'build', 'check': before_status}).json()['total'] == 0
    assert len(calls) == 2


@pytest.mark.parametrize('change', ['missing', 'failed', 'changed', 'scope', 'unknown_field'])
def test_partial_or_changed_status_cannot_refresh_a_whole_vector(snapshot, tmp_path, change):
    db = tmp_path / 'snapshot.db'
    state.commit(db, snapshot)
    patches = collector.build_patch(snapshot, 'builds')
    stamp = state.utcnow()
    for targets in patches.values():
        for fact in targets.values():
            fact.update(fetched_at=stamp, attempted_at=stamp)
    component = state.success(snapshot['components']['builds'], {}, stamp)
    if change == 'missing':
        patches['binutils'].pop('rva23')
    elif change == 'failed':
        patches['binutils']['rva23']['error'] = 'timeout'
    elif change == 'changed':
        patches['binutils']['rva23']['raw_status'] = 'building'
    elif change == 'scope':
        patches.pop('binutils')
    else:
        patches['binutils']['rva23']['extra'] = True
    assert not state.commit_build_heartbeat(db, snapshot, patches, component)
    assert state.read(db) == snapshot


def test_cached_storage_reads_old_database_and_backup_clock(tmp_path, snapshot):
    db = tmp_path / 'old.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE snapshot(id INTEGER PRIMARY KEY, payload TEXT)')
        conn.execute('INSERT INTO snapshot VALUES (1,?)', (json.dumps(snapshot),))
    assert state.read_cached(db) == (snapshot, None)
    state.commit(db, snapshot)
    before, revision = state.read_cached(db)
    patches = collector.build_patch(snapshot, 'builds')
    stamp = (datetime.now(timezone.utc) + timedelta(seconds=15)).isoformat()
    for targets in patches.values():
        for fact in targets.values():
            fact.update(fetched_at=stamp, attempted_at=stamp)
    assert state.commit_build_heartbeat(db, snapshot, patches, state.success(snapshot['components']['builds'], {}, stamp))
    after, same = state.read_cached(db, (before, revision))
    assert same == revision and before['builds'] != after['builds']
    assert before == snapshot  # Readers never mutate another request's snapshot.
    backup = tmp_path / 'backup.db'
    with sqlite3.connect(db) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    assert state.read(backup) == after




def test_restore_with_same_revision_does_not_reuse_a_newer_clock(snapshot, tmp_path, monkeypatch):
    at = datetime.now(timezone.utc).replace(microsecond=0)
    snapshot['obs_stale_after_seconds'] = 10
    for targets in snapshot['builds'].values():
        for fact in targets.values():
            fact.update(fetched_at=at.isoformat(), attempted_at=at.isoformat())
    snapshot['components']['builds'] = state.success({}, {}, at.isoformat())
    db = tmp_path / 'state.db'; backup = tmp_path / 'backup.db'
    state.commit(db, snapshot)
    with sqlite3.connect(db) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    now = at + timedelta(seconds=20)
    monkeypatch.setattr(api, 'time', SimpleNamespace(time=lambda: now.timestamp()))
    patches = collector.build_patch(snapshot, 'builds')
    for targets in patches.values():
        for fact in targets.values():
            fact.update(fetched_at=now.isoformat(), attempted_at=now.isoformat())
    assert state.commit_build_heartbeat(db, snapshot, patches, state.success(snapshot['components']['builds'], {}, now.isoformat()))
    client = TestClient(api.create_app(db))
    url = '/api/v2/packages/binutils'
    assert client.get(url).json()['monitors']['build']['check']['status'] == 'ok'
    # An in-place SQLite restore keeps both the inode and original content revision.
    with sqlite3.connect(backup) as src, sqlite3.connect(db) as dst:
        src.backup(dst)
    assert client.get(url).json()['monitors']['build']['check']['status'] == 'expired'
    assert state.read(db) == snapshot
