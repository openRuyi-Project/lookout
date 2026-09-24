"""Projection caching preserves the exact freshness transitions of saved facts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
import threading

from fastapi.testclient import TestClient
import pytest
from tracker import api, state, view
from conftest import make_snapshot

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def setup_cache(tmp_path, config, monkeypatch, change=None):
    snapshot = make_snapshot(config, NOW.isoformat())
    snapshot.update(stale_after_seconds=100, obs_stale_after_seconds=100,
                    build_history_interval_seconds=50)
    if change:
        change(snapshot)
    db = tmp_path / 'cache.sqlite3'
    state.commit(db, snapshot)
    clock, calls = [NOW], []
    monkeypatch.setattr(api.time, 'time', lambda: clock[0].timestamp())
    project = view.project_monitors
    def counted(snap, now=None):
        calls.append((snap['generation'], now))
        return project(snap, now)
    monkeypatch.setattr(view, 'project_monitors', counted)
    return TestClient(api.create_app(db)), db, snapshot, clock, calls


def get(client, path='/api/v1/packages/binutils'):
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


def test_unchanged_snapshot_is_not_reprojected_every_five_seconds(tmp_path, config, monkeypatch):
    client, _, _, clock, calls = setup_cache(tmp_path, config, monkeypatch)
    first = get(client)
    for second in (6, 20, 99, 100):
        clock[0] = NOW + timedelta(seconds=second)
        assert get(client) == first
    assert len(calls) == 1
    clock[0] += timedelta(microseconds=1)
    assert get(client)['stale'] is True
    assert len(calls) == 2
    clock[0] += timedelta(days=1)
    assert get(client)['stale'] is True
    assert len(calls) == 2  # All observations expired; no forward transition left.


@pytest.mark.parametrize('observation', ['source', 'upstream', 'build', 'history', 'obs_component', 'upstream_component', 'history_component'])
def test_each_freshness_boundary_invalidates_without_a_write(tmp_path, config, monkeypatch, observation):
    def change(snap):
        old = (NOW - timedelta(seconds=90)).isoformat()
        if observation == 'source':
            snap['sources']['binutils']['checked_at'] = old
        elif observation == 'upstream':
            snap['tracks']['binutils']['fetched_at'] = old
        elif observation == 'build':
            snap['builds']['binutils']['rva23']['fetched_at'] = old
        elif observation == 'history':
            snap['builds']['binutils']['rva23'].update(raw_status='failed', history_checked_at=old,
                last_success={'version': '3.8.0', 'srcmd5': 'old', 'time': old})
        else:
            key = {'obs_component': 'builds', 'upstream_component': 'nvchecker',
                   'history_component': 'build_history:rva23'}[observation]
            snap['components'][key] = {'fetched_at': old}
    client, _, _, clock, calls = setup_cache(tmp_path, config, monkeypatch, change)
    path = '/api/v1/packages' if observation.endswith('component') else '/api/v1/packages/binutils'
    before = get(client, path)
    clock[0] += timedelta(seconds=10)
    assert get(client, path) == before  # Existing stale() treats exact TTL as fresh.
    assert len(calls) == 1
    clock[0] += timedelta(microseconds=1)
    after = get(client, path)
    assert len(calls) == 2
    if observation in ('source', 'upstream'):
        assert not before['stale'] and after['stale']
        assert after['relation'] == 'unknown'
    elif observation == 'build':
        assert not before['builds'][0]['stale'] and after['builds'][0]['stale']
    elif observation == 'history':
        assert before['builds'][0]['matches_source'] is False
        assert after['builds'][0]['matches_source'] is None
    else:
        assert before['collection']['errors'] == []
        assert after['collection']['errors'] == ['collection observations are stale']


def test_source_checked_time_wins_and_history_uses_configured_lifetime(config):
    snap = make_snapshot(config, NOW.isoformat())
    snap.update(stale_after_seconds=10000, obs_stale_after_seconds=10000,
                build_history_interval_seconds=6000)
    snap['sources']['binutils'].update(fetched_at=(NOW-timedelta(days=1)).isoformat(),
                                      checked_at=(NOW-timedelta(seconds=5)).isoformat())
    assert view.next_transition(snap, NOW) == NOW + timedelta(seconds=9995, microseconds=1)
    snap['builds']['binutils']['rva23']['history_checked_at'] = (NOW-timedelta(seconds=11990)).isoformat()
    assert view.next_transition(snap, NOW) == NOW + timedelta(seconds=10, microseconds=1)


def test_future_time_recovers_then_expires_at_real_boundaries(tmp_path, config, monkeypatch):
    def change(snap):
        snap['sources']['binutils']['checked_at'] = (NOW+timedelta(seconds=310)).isoformat()
    client, _, _, clock, calls = setup_cache(tmp_path, config, monkeypatch, change)
    assert get(client)['stale'] is True
    clock[0] += timedelta(seconds=9)
    assert get(client)['stale'] is True
    assert len(calls) == 1
    clock[0] += timedelta(seconds=1)
    assert get(client)['stale'] is False
    assert len(calls) == 2


@pytest.mark.parametrize('fact', [{}, {'fetched_at': 'bad'}, {'fetched_at': 7},
                                  {'fetched_at': '2026-09-19T12:00:00'}])
def test_invalid_time_has_no_forward_transition(fact):
    assert state.stale(fact, NOW, 100)
    assert state.next_stale_change(fact, NOW, 100) is None


def test_next_stale_change_timezone_and_boundaries():
    fact = {'fetched_at': '2026-09-19T20:00:00+08:00'}
    expiry = NOW + timedelta(seconds=100, microseconds=1)
    assert state.next_stale_change(fact, NOW, 100) == expiry
    assert state.next_stale_change(fact, expiry, 100) is None
    future = {'fetched_at': (NOW+timedelta(seconds=301)).isoformat()}
    assert state.next_stale_change(future, NOW, 100) == NOW+timedelta(seconds=1)
    assert state.next_stale_change(future, NOW+timedelta(seconds=1), 100) == NOW+timedelta(seconds=401, microseconds=1)


def test_backward_clock_invalidates_against_last_request(tmp_path, config, monkeypatch):
    client, _, _, clock, calls = setup_cache(tmp_path, config, monkeypatch)
    get(client)
    clock[0] = NOW + timedelta(seconds=80)
    get(client)
    assert len(calls) == 1
    clock[0] = NOW + timedelta(seconds=70)
    get(client)
    assert len(calls) == 2  # Still after projection creation, but clock went backwards.
    clock[0] = NOW + timedelta(seconds=101)
    assert get(client)['stale']
    clock[0] = NOW + timedelta(seconds=90)
    assert not get(client)['stale']
    assert len(calls) == 4


@pytest.mark.parametrize('generation', [1, 2])
def test_snapshot_changes_invalidate_even_with_same_generation(tmp_path, config, monkeypatch, generation):
    client, db, snap, _, calls = setup_cache(tmp_path, config, monkeypatch)
    assert get(client)['current'] == '3.9.0'
    snap['sources']['binutils']['version'] = '3.9.1'
    snap['generation'] = generation
    state.commit(db, snap)
    assert get(client)['current'] == '3.9.1'
    assert len(calls) == 2


def test_atomic_replacement_with_same_mtime_and_size_invalidates(tmp_path, config, monkeypatch):
    client, db, snap, _, calls = setup_cache(tmp_path, config, monkeypatch)
    assert get(client)['current'] == '3.9.0'
    previous = db.stat()
    snap['sources']['binutils']['version'] = '3.9.1'
    replacement = tmp_path/'replacement.sqlite3'
    state.commit(replacement, snap)
    assert replacement.stat().st_size == previous.st_size
    os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    os.replace(replacement, db)
    assert get(client)['current'] == '3.9.1'
    assert len(calls) == 2


def test_concurrent_reads_only_project_once(tmp_path, config, monkeypatch):
    client, _, _, _, calls = setup_cache(tmp_path, config, monkeypatch)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: get(client), range(16)))
    assert all(row == results[0] for row in results)
    assert len(calls) == 1


def test_concurrent_replacement_never_mixes_snapshot_and_projection(tmp_path, config, monkeypatch):
    client, db, snap, _, _ = setup_cache(tmp_path, config, monkeypatch)
    started, proceed = threading.Event(), threading.Event()
    project = view.project_monitors
    def blocked(snapshot, now=None):
        if snapshot['targets'][0]['label'] == 'rva23':
            started.set()
            assert proceed.wait(5), 'projection was not released'
        return project(snapshot, now)
    monkeypatch.setattr(view, 'project_monitors', blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(get, client, '/api/v1/packages')
        assert started.wait(5), 'projection did not begin'
        replacement = deepcopy(snap)
        replacement['targets'][0]['label'] = 'new target'
        replacement['sources']['binutils']['version'] = '3.9.1'
        state.commit(db, replacement)  # Same generation; change while first projection is running.
        second = pool.submit(get, client, '/api/v1/packages')
        proceed.set()
        old, new = first.result(timeout=5), second.result(timeout=5)
    assert old['items'][0]['current'] == '3.9.0'
    assert old['targets'][0]['label'] == old['items'][0]['builds'][0]['label'] == 'rva23'
    assert new['items'][0]['current'] == '3.9.1'
    assert new['targets'][0]['label'] == new['items'][0]['builds'][0]['label'] == 'new target'


@pytest.mark.parametrize('interval', [600, 21600])
def test_spec_component_uses_own_interval_and_cache_expiry(tmp_path, config, monkeypatch, interval):
    def change(snap):
        snap['spec_interval_seconds'] = interval
        snap['components']['spec_git'] = state.success({}, {}, NOW.isoformat())
        # No unrelated expiring observations in this focused fixture.
        snap['sources'].clear(); snap['tracks'].clear(); snap['builds'].clear()
        snap['components'] = {'spec_git': snap['components']['spec_git']}
    client, _, _, clock, calls = setup_cache(tmp_path, config, monkeypatch, change)
    before = get(client, '/api/v1/packages')
    assert not before['collection']['errors']
    for second in (301, interval, interval * 2):
        clock[0] = NOW + timedelta(seconds=second)
        assert get(client, '/api/v1/packages') == before
    assert len(calls) == 1
    clock[0] += timedelta(microseconds=1)
    assert get(client, '/api/v1/packages')['collection']['errors'] == ['collection observations are stale']
    assert len(calls) == 2


def test_spec_age_allowance_does_not_hide_failures_or_extend_obs(config):
    snap = make_snapshot(config, NOW.isoformat())
    snap.update(obs_stale_after_seconds=300, spec_interval_seconds=21600)
    snap['components']['spec_git'] = state.failure({}, 'git fetch timeout', NOW.isoformat())
    assert 'git fetch timeout' in view.project(snap, NOW)[1]['errors']
    assert view.component_ttl(snap, 'spec_git') == 43200
    assert view.component_ttl(snap, 'builds') == 300
    assert view.component_ttl(snap, 'nvchecker') == 86400
    snap.pop('spec_interval_seconds')
    assert view.component_ttl(snap, 'spec_git') == 300


def test_spec_component_failure_does_not_move_success_deadline(config):
    snap = make_snapshot(config, NOW.isoformat())
    snap.update(obs_stale_after_seconds=300, spec_interval_seconds=21600)
    snap['sources'].clear(); snap['tracks'].clear(); snap['builds'].clear()
    snap['components'] = {'spec_git': state.failure(state.success({}, {}, NOW.isoformat()),
        'git fetch timeout', (NOW+timedelta(hours=5)).isoformat())}
    assert view.next_transition(snap, NOW+timedelta(hours=6)) == NOW+timedelta(hours=12, microseconds=1)


def test_query_index_is_shared_without_sharing_selections(tmp_path, config, monkeypatch):
    client, db, snap, clock, _ = setup_cache(tmp_path, config, monkeypatch)
    builds = []
    initialize = api.package_list.PackageList.__init__
    def counted(self, *args, **kwargs):
        builds.append(1)
        initialize(self, *args, **kwargs)
    monkeypatch.setattr(api.package_list.PackageList, '__init__', counted)
    paths = [
        '/api/v2/packages?q=BIN&monitor=version',
        '/api/v2/packages?q=foo&monitor=build&build=rva23:issues',
        '/api/v1/packages?q=bin',
        '/api/ui/packages?monitor=source&per_page=2',
        '/api/v2/packages/binutils',
        '/api/ui/packages/binutils',
    ]
    expected = [get(client, path) for path in paths]
    assert [p['name'] for p in expected[0]['items']] == ['binutils']
    assert all('foo' in p['name'] for p in expected[1]['items'])
    with ThreadPoolExecutor(max_workers=6) as pool:
        actual = list(pool.map(lambda path: get(client, path), paths * 3))
    assert actual == expected * 3
    assert len(builds) == 1
    clock[0] += timedelta(seconds=101)
    get(client)
    assert len(builds) == 2
    snap['sources']['binutils']['version'] = 'replacement'
    state.commit(db, snap)
    assert get(client)['current'] == 'replacement'
    assert len(builds) == 3
