"""Fast OBS polling has a fixed request budget and disjoint publication rights."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import threading

import httpx
import pytest
from tracker import collector, obs, state
from test_core import FakeOBS


def test_fast_poll_is_visible_while_metadata_is_blocked(config, snapshot, tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    waiting, release = threading.Event(), threading.Event()
    requests = []

    class Client(FakeOBS):
        def __init__(self, config, *, attempts=3):
            super().__init__(config, names=tuple(snapshot['inventory']), new_hash='new-binutils')
            self.fast = attempts == 1
        def get(self, path):
            requests.append((self.fast, path))
            if not self.fast and path.endswith('/_meta'):
                waiting.set()
                assert release.wait(10)
            data = super().get(path)
            return data.replace(b'code="succeeded"', b'code="building"') if self.fast else data
        def close(self):
            pass

    monkeypatch.setattr(obs, 'Client', Client)
    with ThreadPoolExecutor(max_workers=1) as pool:
        slow = pool.submit(collector.collect_obs, config, db)
        try:
            assert waiting.wait(5)
            fast = collector.collect_builds(config, db)
            assert fast['builds']['binutils']['rva23']['raw_status'] == 'building'
            assert not slow.done()
            # A second phase can commit without waiting for OBS's metadata lock.
            assert state.read(db)['components']['builds']['error'] is None
        finally:
            release.set()
        complete = slow.result(timeout=10)
    assert complete['builds']['binutils']['rva23']['raw_status'] == 'building'
    assert complete['components']['builds'] == fast['components']['builds']
    assert [path for fast, path in requests if fast] == ['/build/openruyi/_result']
    assert not any(path.endswith('/_result') for fast, path in requests if not fast)


def test_fast_poll_preserves_concurrent_success_history(config, snapshot, tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    success = {'version': '3.9.0', 'time': state.utcnow(), 'srcmd5': 'h-binutils'}
    class Client(FakeOBS):
        def __init__(self, config, **kwargs):
            super().__init__(config, names=tuple(snapshot['inventory']))
        def get(self, path):
            with state.writer_lock(db):
                latest = state.read(db)
                latest['builds']['binutils']['rva23']['last_success'] = success
                state.commit(db, latest)
            return super().get(path)
        def close(self):
            pass
    monkeypatch.setattr(obs, 'Client', Client)
    result = collector.collect_builds(config, db)
    assert result['builds']['binutils']['rva23']['last_success'] == success


def test_fast_poll_only_copies_owned_status_fields(config, snapshot):
    class Evidence(dict):
        def __deepcopy__(self, memo):
            pytest.fail('the status lane must not copy unrelated evidence')
    snapshot['specs'] = Evidence({'binutils': {'metadata': {'version': '3.9.0'}}})
    snapshot['monitors'] = Evidence({'binutils': {'security': {'findings': ['unchanged']}}})
    prior_builds = deepcopy(snapshot['builds'])
    snapshot['builds']['binutils']['rva23']['last_success'] = Evidence({'version': '3.8'})
    observed = collector.refresh_builds(config, snapshot, FakeOBS(config), 'new-time')
    assert set(observed) == {'builds', 'components'}
    assert set(observed['components']) == {'builds'}
    for name, targets in observed['builds'].items():
        for tid, fact in targets.items():
            assert set(fact) <= state.BUILD_FIELDS['builds']
            old = snapshot['builds'][name][tid]
            assert {key: old[key] for key in prior_builds[name][tid]} == prior_builds[name][tid]
    assert observed['builds']['binutils']['rva23']['raw_status'] == 'succeeded'
    assert observed['builds']['binutils']['rva23']['fetched_at'] == 'new-time'


def test_retry_is_owned_by_heartbeat_not_http_loop(config, snapshot, monkeypatch):
    client = obs.Client(config, attempts=1)
    client.client.close()
    requests = []
    def unavailable(request):
        requests.append(request.url.path)
        return httpx.Response(503)
    client.client = httpx.Client(transport=httpx.MockTransport(unavailable))
    try:
        result = collector.refresh_builds(config, snapshot, client)
    finally:
        client.close()
    assert requests == ['/build/openruyi/_result']
    actual = result['builds']['binutils']['rva23']
    assert actual['error']
    assert actual['fetched_at'] == snapshot['builds']['binutils']['rva23']['fetched_at']
    assert actual['raw_status'] == snapshot['builds']['binutils']['rva23']['raw_status']


@pytest.mark.parametrize('phase,illegal', [('obs', {'raw_status': 'failed'}),
                                         ('builds', {'last_success': {'version': 'invented'}})])
def test_build_record_field_ownership(snapshot, phase, illegal):
    with pytest.raises(ValueError, match='ownership'):
        state.merge(snapshot, phase, {'builds': {'binutils': {'rva23': illegal}}})


def test_repointed_target_cannot_retain_old_build_status(snapshot):
    targets = deepcopy(snapshot['targets'])
    targets[0]['repository'] = 'another-repository'
    result = state.merge(snapshot, 'obs', {'targets': targets, 'builds': {}})
    assert 'rva23' not in result['builds']['binutils']
    assert result['builds']['binutils']['rva20'] == snapshot['builds']['binutils']['rva20']


def test_removed_inventory_is_not_reintroduced_by_fast_poll(config, snapshot, tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    class Client(FakeOBS):
        def __init__(self, config, **kwargs):
            super().__init__(config)
        def get(self, path):
            latest = state.read(db)
            latest['inventory'].pop('binutils')
            latest['builds'].pop('binutils')
            state.commit(db, latest)
            return super().get(path)
        def close(self):
            pass
    monkeypatch.setattr(obs, 'Client', Client)
    result = collector.collect_builds(config, db)
    assert 'binutils' not in result['builds']


def test_inflight_status_for_repointed_target_is_discarded(config, snapshot, tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    class Client(FakeOBS):
        def __init__(self, config, **kwargs):
            super().__init__(config)
        def get(self, path):
            latest = state.read(db)
            latest['targets'][0]['repository'] = 'another-repository'
            state.commit(db, latest)
            return super().get(path)
        def close(self):
            pass
    monkeypatch.setattr(obs, 'Client', Client)
    with pytest.raises(ValueError, match='scope changed'):
        collector.collect_builds(config, db)
    assert state.read(db)['builds'] == snapshot['builds']


def test_fast_lane_waits_for_first_inventory_without_network(config, tmp_path, monkeypatch):
    monkeypatch.setattr(obs, 'Client', lambda *a, **k: pytest.fail('no inventory'))
    db = tmp_path / 'missing.db'
    assert collector.collect_builds(config, db) == state.empty()
    assert not db.exists()


def test_unrelated_provider_failure_does_not_back_off_obs(config, snapshot, tmp_path, monkeypatch, capsys):
    import json
    import sys
    snapshot['components']['nvchecker']['error'] = 'unrelated provider failure'
    monkeypatch.setattr(collector.cfg, 'load', lambda _: config)
    monkeypatch.setattr(collector, 'collect_builds', lambda *_: snapshot)
    monkeypatch.setattr(sys, 'argv', ['collector', '--config', 'unused', '--db', str(tmp_path/'db'), '--only', 'builds'])
    assert collector.main() == 0
    assert json.loads(capsys.readouterr().out)['errors'] == []
