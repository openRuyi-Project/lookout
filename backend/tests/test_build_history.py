"""Successful versions are OBS evidence, never inferred from green status alone."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import pytest
from tracker import collector, obs, state, view
from tracker.api import create_app
from fastapi.testclient import TestClient
from test_core import FakeOBS


def history(version='3.11.0-1', digest='new-binutils', code='succeeded', end='1788572131'):
    return (f'<jobhistlist><jobhist package="binutils" versrel="{version}" srcmd5="{digest}" '
            f'code="{code}" endtime="{end}" uri="http://private-worker:123"/></jobhistlist>').encode()


def test_history_success_ignores_reuse_failure_and_private_fields():
    expected = {'binutils': {'version': '3.11.0', 'time': '2026-09-05T01:35:31+00:00', 'srcmd5': 'new-binutils'}}
    assert obs.last_successes(history()) == expected
    assert obs.last_successes(history(code='unchanged')) == {'binutils': None}
    assert obs.last_successes(history(code='failed')) == {}
    assert obs.last_successes(b'<jobhistlist/>') == {}


@pytest.mark.parametrize('data', [b'<buildhistory/>', history(end='0'), history(end='nan')])
def test_history_rejects_nonfacts(data):
    with pytest.raises(ValueError):
        obs.last_successes(data)


def test_history_uses_latest_completed_success_not_input_order():
    a = history(end='1788572131').decode().removeprefix('<jobhistlist>').removesuffix('</jobhistlist>')
    b = history(version='3.10.0-1', end='1788572100').decode().removeprefix('<jobhistlist>').removesuffix('</jobhistlist>')
    assert obs.last_successes(f'<jobhistlist>{a}{b}</jobhistlist>'.encode())['binutils']['version'] == '3.11.0'


class HistoryOBS(FakeOBS):
    def __init__(self, config, fail_history=False):
        super().__init__(config, new_hash='new-binutils')
        self.requests = []
        self.fail_history = fail_history

    def get(self, path):
        self.requests.append(path)
        if '_jobhistory' in path:
            if self.fail_history:
                raise TimeoutError()
            return history()
        return super().get(path)


def test_bulk_history_bounded_cached_and_retained_on_error(config, snapshot):
    now = datetime.now(timezone.utc)
    client = HistoryOBS(config)
    first = collector.collect(config, snapshot, client, now.isoformat())
    assert sum('_jobhistory' in r for r in client.requests) == len(config['targets'])
    assert all('code=lastfailures' in r for r in client.requests if '_jobhistory' in r)
    fact = first['builds']['binutils']['rva23']
    assert fact['last_success']['version'] == '3.11.0'
    client.requests.clear()
    second = collector.collect(config, first, client, (now + timedelta(seconds=60)).isoformat())
    assert not any('_jobhistory' in r for r in client.requests)
    client.fail_history = True
    third = collector.collect(config, second, client, (now + timedelta(seconds=301)).isoformat())
    retained = third['builds']['binutils']['rva23']
    assert retained['last_success'] == fact['last_success']
    assert retained['history_checked_at'] == fact['history_checked_at']
    assert retained['history_error']


def set_success(snapshot, name, version, digest):
    for fact in snapshot['builds'][name].values():
        fact['last_success'] = {'version': version, 'time': state.utcnow(), 'srcmd5': digest}
        fact['history_checked_at'] = state.utcnow()


def row(snapshot, name='binutils'):
    return next(r for r in view.project(snapshot)[0] if r['name'] == name)


def test_status_alone_never_claims_current_source_success(snapshot):
    assert row(snapshot)['current_build_success'] is None
    set_success(snapshot, 'binutils', '3.9.0', 'h-binutils')
    assert row(snapshot)['current_build_success'] is True
    assert row(snapshot)['last_successful_version'] == '3.9.0'


def test_failed_current_source_can_retain_prior_success(snapshot):
    set_success(snapshot, 'binutils', '3.8.0', 'older')
    for fact in snapshot['builds']['binutils'].values():
        fact['raw_status'] = 'failed'
    result = row(snapshot)
    assert result['current_build_success'] is False
    assert result['last_successful_version'] == '3.8.0'
    assert all(b['last_success']['version'] == '3.8.0' for b in result['builds'])
    # A rebuild failure does not erase an earlier success of this exact source.
    set_success(snapshot, 'binutils', '3.9.0', 'h-binutils')
    assert row(snapshot)['current_build_success'] is True


def test_missing_or_stale_history_and_source_do_not_claim_failure(snapshot):
    set_success(snapshot, 'binutils', '3.8.0', 'older')
    for fact in snapshot['builds']['binutils'].values():
        fact['raw_status'] = 'failed'
        fact['history_checked_at'] = '2000-01-01T00:00:00+00:00'
    assert row(snapshot)['current_build_success'] is None
    for fact in snapshot['builds']['binutils'].values():
        fact['history_checked_at'] = state.utcnow()
        fact['history_error'] = 'TimeoutError'
    assert row(snapshot)['current_build_success'] is None


def test_flavors_and_targets_with_different_successes_are_not_flattened(snapshot):
    set_success(snapshot, 'foo3', '3.10.0', 'h-foo3')
    set_success(snapshot, 'foo3:tools', '3.9.0', 'old-tools')
    result = row(snapshot, 'foo3')
    assert result['last_successful_version'] is None
    assert all(b['last_success'] is None for b in result['builds'])
    assert result['builds'][0]['flavors'][1]['last_success']['version'] == '3.9.0'
    # No flavor index identity: never borrow its owner's hash for a success claim.
    assert result['builds'][0]['flavors'][1]['matches_source'] is None


def test_untracked_filter_is_not_attention(tmp_path, snapshot):
    # A tracked package with stale upstream is attention, not untracked.
    snapshot['tracks']['binutils']['error'] = 'timeout'
    set_success(snapshot, 'binutils', '3.9.0', 'h-binutils')
    db = tmp_path / 'snapshot.db'
    state.commit(db, snapshot)
    client = TestClient(create_app(db))
    payload = client.get('/api/v1/packages?view=untracked').json()
    assert payload['counts']['attention'] == 3
    assert payload['counts']['untracked'] == 2
    assert [r['name'] for r in payload['items']] == ['unknown', 'untracked']
    detail = client.get('/api/v1/packages/binutils').json()
    assert detail['current_build_success'] is True
    assert detail['builds'][0]['last_success']['version'] == '3.9.0'


def test_obs_network_does_not_hold_snapshot_lock_or_rewind_other_sources(config, snapshot, tmp_path, monkeypatch):
    db = tmp_path / 'snapshot.db'
    state.commit(db, snapshot)
    class ConcurrentOBS(HistoryOBS):
        changed = False
        def get(self, path):
            if not self.changed:
                self.changed = True
                with state.writer_lock(db):
                    latest = state.read(db)
                    latest['tracks']['binutils']['version'] = 'concurrent-upstream'
                    latest['specs']['binutils'] = {'head': 'concurrent-spec'}
                    latest['components']['spec_git'] = {'fetched_at': 'concurrent'}
                    latest['generation'] += 1
                    state.commit(db, latest)
            return super().get(path)
        def close(self):
            pass
    monkeypatch.setattr(obs, 'Client', ConcurrentOBS)
    result = collector.collect_obs(config, db)
    assert result['tracks']['binutils']['version'] == 'concurrent-upstream'
    assert result['specs']['binutils']['head'] == 'concurrent-spec'
    assert result['components']['spec_git']['fetched_at'] == 'concurrent'
    assert result['generation'] == snapshot['generation'] + 2


def test_success_with_unresolved_version_keeps_real_time_and_hash():
    fact = obs.last_successes(history(version='MACRO-1'))['binutils']
    assert fact['version'] is None
    assert fact['time'] == '2026-09-05T01:35:31+00:00'
    assert fact['srcmd5'] == 'new-binutils'


def test_history_ttl_honors_operator_interval_without_false_stale(snapshot):
    snapshot['build_history_interval_seconds'] = 3600
    snapshot['obs_stale_after_seconds'] = 300
    now = datetime.now(timezone.utc)
    old_history = (now - timedelta(seconds=700)).isoformat()
    set_success(snapshot, 'binutils', '3.8.0', 'older')
    for fact in snapshot['builds']['binutils'].values():
        fact['raw_status'] = 'failed'
        fact['history_checked_at'] = old_history
    snapshot['components']['build_history:rva23'] = {'fetched_at': old_history}
    rows, collection = view.project(snapshot, now)
    assert next(r for r in rows if r['name'] == 'binutils')['current_build_success'] is False
    assert 'collection observations are stale' not in collection['errors']


def test_unchanged_cannot_replace_actual_success_timestamp(config, snapshot):
    now = datetime.now(timezone.utc)
    first = collector.collect(config, snapshot, HistoryOBS(config), now.isoformat())
    prior = deepcopy(first['builds']['binutils']['rva23']['last_success'])
    class ReuseOBS(HistoryOBS):
        def get(self, path):
            if '_jobhistory' in path:
                return history(code='unchanged', end='1788579999')
            return super().get(path)
    result = collector.collect(config, first, ReuseOBS(config), (now + timedelta(seconds=301)).isoformat())
    assert result['builds']['binutils']['rva23']['last_success'] == prior


def test_initial_reuse_followed_by_failure_is_unknown_not_never_succeeded(config, snapshot):
    class ReuseThenFailOBS(HistoryOBS):
        def get(self, path):
            if '_jobhistory' in path:
                return history(code='unchanged')
            data = super().get(path)
            return data.replace(b'code="succeeded"', b'code="failed"') if path.endswith('/_result') else data
    result = collector.collect(config, snapshot, ReuseThenFailOBS(config), state.utcnow())
    result = collector.refresh_builds(config, result, ReuseThenFailOBS(config))
    assert result['builds']['binutils']['rva23']['history_unresolved'] is True
    assert result['builds']['binutils']['rva23']['last_success'] is None
    assert row(result)['current_build_success'] is None


def test_repointed_target_invalidates_success_provenance_and_history_cache(config, snapshot):
    now = datetime.now(timezone.utc)
    first = collector.collect(config, snapshot, HistoryOBS(config), now.isoformat())
    assert row(first)['current_build_success'] is True
    changed = deepcopy(config)
    changed['targets'][0]['repository'] = 'different-repository'
    class OtherRepositoryOBS(HistoryOBS):
        def get(self, path):
            if '/different-repository/' in path and '_jobhistory' in path:
                self.requests.append(path)
                return b'<jobhistlist/>'
            return super().get(path)
    client = OtherRepositoryOBS(changed)
    second = collector.collect(changed, first, client, (now + timedelta(seconds=60)).isoformat())
    queried = [p for p in client.requests if '_jobhistory' in p]
    assert len(queried) == 1 and '/different-repository/' in queried[0]
    assert second['builds']['binutils']['rva23']['last_success'] is None
    assert second['builds']['binutils']['rva20']['last_success'] == first['builds']['binutils']['rva20']['last_success']
    assert row(second)['current_build_success'] is None
