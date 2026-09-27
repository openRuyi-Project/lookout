"""Real readers must never wait for a writer's whole-snapshot projection."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import threading

from fastapi.testclient import TestClient

from tracker import api, state
from tracker.readmodel import snapshot as view


def test_requests_read_complete_previous_model_while_replacement_is_computing(snapshot, tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    app = api.create_app(db)
    app.state.projection.refresh()
    client = TestClient(app)
    original = client.get('/api/v2/packages').json()
    started, release = threading.Event(), threading.Event()
    project = view.project_monitors

    def blocked(snap, now):
        started.set()
        assert release.wait(5)
        return project(snap, now)

    monkeypatch.setattr(view, 'project_monitors', blocked)
    updated = deepcopy(snapshot)
    updated['sources']['binutils']['version'] = 'replacement'
    updated['targets'][0]['label'] = 'replacement target'
    state.commit(db, updated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        work = pool.submit(app.state.projection.refresh)
        try:
            assert started.wait(5)
            for path in ('/api/v2/packages', '/api/ui/packages', '/api/ui/packages/binutils', '/readyz'):
                assert pool.submit(client.get, path).result(timeout=2).status_code == 200
            assert client.get('/api/v2/packages').json() == original
            assert not work.done()
        finally:
            release.set()
        work.result(timeout=5)
    actual = client.get('/api/v2/packages').json()
    assert actual['items'][0]['monitors']['source']['data']['version'] == 'replacement'
    assert actual['targets'][0]['label'] == actual['items'][0]['monitors']['build']['data']['targets'][0]['label'] == 'replacement target'


def test_refresh_failure_retains_data_but_never_reports_fresh_success(snapshot, tmp_path):
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    app = api.create_app(db)
    app.state.projection.refresh()
    client = TestClient(app)
    db.write_bytes(b'corrupt fixture')
    app.state.projection.refresh()
    ready = client.get('/readyz').json()
    assert ready['status'] == 'degraded'
    assert 'refresh failed' in ready['projection_notice']
    assert client.get('/api/ui/packages').json()['notices'][-1] == ready['projection_notice']
    assert client.get('/api/ui/packages/binutils').json()['notices'] == [ready['projection_notice']]
    replacement = tmp_path / 'replacement.db'
    state.commit(replacement, snapshot)
    replacement.replace(db)
    app.state.projection.refresh()
    assert client.get('/readyz').json()['projection_notice'] is None


def test_expired_prepared_evidence_is_marked_until_refresh(snapshot, tmp_path, monkeypatch):
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    app = api.create_app(db)
    cache = app.state.projection
    cache.refresh()
    monkeypatch.setattr(cache, 'now', lambda: cache._prepared.deadline + timedelta(seconds=1))
    ready = TestClient(app).get('/readyz').json()
    assert ready['status'] == 'degraded'
    assert 'freshness is being recalculated' in ready['projection_notice']


def test_lifespan_starts_and_stops_producer_without_blocking_cold_liveness(tmp_path, monkeypatch):
    app = api.create_app(tmp_path / 'missing.db')
    entered, stopped = threading.Event(), threading.Event()

    def run():
        entered.set()
        app.state.projection._stop.wait(5)
        stopped.set()

    monkeypatch.setattr(app.state.projection, 'run', run)
    with TestClient(app) as client:
        assert entered.wait(2)
        assert client.get('/healthz').status_code == 200
        assert client.get('/readyz').status_code == 503
        assert client.get('/api/v2/packages').status_code == 503
    assert stopped.is_set()
