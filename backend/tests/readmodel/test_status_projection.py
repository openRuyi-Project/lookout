"""The status endpoint consumes prepared monitor facts, without per-request projection."""
from collections import Counter
from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from tracker import api, state
from tracker.monitors import model as monitor_model
from tracker.readmodel import monitors as monitor_views, snapshot as view
from tracker.readmodel.packages import PackageList


def test_new_monitors_share_coverage_counts_with_their_filters():
    statuses = ['ok', 'unsupported', 'error', 'unsupported', 'provider-specific-state']
    rows = []
    for number, status in enumerate(statuses):
        module = monitor_views.Monitor('external-fixture', 'Fixture', 'evidence',
            lambda context, status=status: {'check': {'status': status}, 'dimensions': {}, 'data': {}})
        rows.append({'name': f'fixture-{number}', 'monitors': {module.id: module.read(None)}})
    index = PackageList(rows, [])
    counts = index.monitor_coverage()['external-fixture']
    assert counts == Counter(statuses)
    assert not set(counts).intersection(monitor_model.CHECK_GROUPS)
    for status, count in counts.items():
        selection = index.select(view='all', buildsystem='', maintenance='', builds={},
                                 monitor='external-fixture', check=status, page=1, per_page=10)
        assert selection['total'] == count
    counts['ok'] = 99
    assert index.monitor_coverage()['external-fixture']['ok'] == 1


def test_indexed_coverage_and_status_preserve_the_published_response(snapshot, tmp_path, monkeypatch):
    db = tmp_path / 'status.db'
    snapshot['tracks']['binutils'].update(error='fixture timeout', source={'source': 'git', 'git': 'https://forge.example/repo'})
    state.commit(db, snapshot)
    app = api.create_app(db)
    cache = app.state.projection
    cache.refresh()
    snap, index, collection = cache.read()
    expected_coverage = {}
    for row in index.rows:
        for mid, module in row['monitors'].items():
            expected_coverage.setdefault(mid, Counter()).update([module['check']['status']])
    expected = {
        **collection, 'packages': len(index.rows),
        'source_versions': sum(bool(row['monitors']['source']['data']['version']) for row in index.rows),
        'tracked_packages': sum(bool(row['monitors']['version']['data']['track']) for row in index.rows),
        'components': snap['components'], 'monitor_coverage': expected_coverage,
        'upstream_failures': [{'provider': 'forge.example', 'error': 'fixture timeout', 'count': 1, 'packages': ['binutils']}],
    }
    original = deepcopy(index.rows)
    # Existing, separately checked transforms are forbidden on the request path.
    monkeypatch.setattr(view, 'project_monitors', lambda *args: pytest.fail('request must not reproject'))
    monkeypatch.setattr(state, 'read', lambda *args: pytest.fail('request must not read SQLite'))
    client = TestClient(app)
    for _ in range(3):
        response = client.get('/api/v2/status')
        assert response.status_code == 200
        assert response.json() == expected
    assert index.rows == original
    assert index.monitor_coverage() == expected_coverage


def test_status_and_facets_switch_to_the_same_new_generation(snapshot, tmp_path):
    db = tmp_path / 'status.db'
    state.commit(db, snapshot)
    app = api.create_app(db)
    cache = app.state.projection
    cache.refresh()
    client = TestClient(app)
    old = client.get('/api/v2/status').json()
    old_index = cache.read()[1]
    changed = deepcopy(snapshot)
    changed['tracks']['binutils']['error'] = 'fixture timeout'
    changed['generation'] += 1
    state.commit(db, changed)
    # Committing storage alone cannot publish a half-updated read model.
    assert client.get('/api/v2/status').json() == old
    cache.refresh()
    updated = client.get('/api/v2/status').json()
    assert updated['generation'] == old['generation'] + 1
    assert updated['monitor_coverage']['version']['error'] == 1
    listing = client.get('/api/v2/packages?monitor=version&check=error').json()
    assert listing['total'] == updated['monitor_coverage']['version']['error']
    assert [row['name'] for row in listing['items']] == ['binutils']
    assert old_index.monitor_coverage() == old['monitor_coverage']
