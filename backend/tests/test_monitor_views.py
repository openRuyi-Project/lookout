from copy import deepcopy
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from tracker import monitor, monitor_model, monitor_views, state, view
from tracker.api import create_app


def test_uniform_results_preserve_v1_values_and_raw_evidence(snapshot, tmp_path):
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    client = TestClient(create_app(db))
    old = client.get('/api/v1/packages/foo3').json()
    new = client.get('/api/v2/packages/foo3').json()
    modules = new['monitors']
    assert {m['data']['kind'] for m in modules.values()} == {'source', 'version', 'build'}
    for key, result in modules.items():
        assert set(result) == {'id', 'title', 'check', 'data'}
        assert result['id'] == key
    source, version, build = (modules[k]['data'] for k in ('source', 'version', 'build'))
    assert source['version'] == version['current'] == old['current']
    assert version['relation'] == old['relation']
    assert version['watch'] == old['watch']
    assert build['targets'] == old['builds']
    assert source['obs'] == old['source']
    assert source['source_url'] == old['spec']['source_url']
    # A failed build is an observed result, not a failed collection.
    assert modules['build']['check']['status'] == 'ok'
    assert any(b['raw_status'] == 'failed' for b in build['targets'])
    listing = client.get('/api/v2/packages?q=foo3').json()
    summary = listing['items'][0]['monitors']
    assert 'metadata' not in summary['source']['data']
    assert 'watch' not in summary['version']['data']
    assert all('flavors' not in b for b in summary['build']['data']['targets'])
    assert listing['build_statuses']['rva20'][0]['count'] >= 0
    assert [row['name'] for row in client.get('/api/v1/packages?build=rva20:issues').json()['items']] == ['foo3']
    assert [row['name'] for row in client.get('/api/v2/packages?monitor=build&build=rva20:issues').json()['items']] == ['foo3']


def test_error_and_expiry_do_not_erase_build_result(snapshot):
    now = datetime.now(timezone.utc)
    fact = snapshot['builds']['binutils']['rva23']
    fact['raw_status'] = 'failed'
    fact['error'] = 'request timed out'
    fact['attempted_at'] = now.isoformat()
    original = deepcopy(snapshot)
    rows, _ = view.project_monitors(snapshot, now)
    result = next(row for row in rows if row['name'] == 'binutils')['monitors']['build']
    assert result['check']['status'] == 'error'
    assert result['check']['checked_at'] == fact['fetched_at']
    assert result['data']['targets'][0]['raw_status'] == 'failed'
    assert result['dimensions']['build:rva23'] == ['failed', 'issues']
    assert snapshot == original
    del fact['error']
    rows, _ = view.project_monitors(snapshot, now + timedelta(days=2))
    result = rows[0]['monitors']['build']
    assert result['check']['status'] == 'expired'
    assert result['data']['targets'][0]['raw_status'] == 'failed'


def test_pending_monitor_is_not_a_negative_finding(snapshot, tmp_path):
    snapshot['monitor_catalog'] = {'future': {'title': 'New observation'}}
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    result = TestClient(create_app(db)).get('/api/v2/packages?monitor=future').json()
    assert result['check_statuses'] == {'pending': 5}
    assert result['total'] == 5
    assert all(row['monitors']['future']['check']['status'] == 'pending' for row in result['items'])
    assert result['maintenance_labels'] == {}


def test_unknown_old_adapter_uses_the_generic_envelope(snapshot):
    # No runtime provider import or configured registry entry is needed to read facts.
    subject = monitor_model.subject(snapshot, 'binutils')
    snapshot['monitors'] = {'binutils': {'old_plugin': {
        'status': 'ok', 'subject': subject, 'findings': [], 'checked_at': state.utcnow(),
    }}}
    modules = monitor_views.registry(snapshot)
    assert modules[-1].describe() == {'id': 'old_plugin', 'title': 'old_plugin', 'kind': 'evidence'}
    rows, _ = view.project_monitors(snapshot)
    assert rows[0]['monitors']['old_plugin']['data']['findings'] == []


def test_enabled_adapter_cannot_shadow_a_core_monitor(config, monkeypatch):
    monkeypatch.setitem(monitor.REGISTRY, 'source', monitor.REGISTRY['license'])
    config['monitors'] = {'enabled': ['source']}
    with pytest.raises(ValueError, match='reserved'):
        monitor.settings(config)


def test_monitor_focus_does_not_change_other_filter_dimensions(snapshot, tmp_path):
    snapshot['monitor_catalog'] = {'external': {'title': 'External facts'}}
    snapshot['monitors'] = {'binutils': {'external': {
        'status': 'ok', 'checked_at': state.utcnow(),
        'subject': monitor_model.subject(snapshot, 'binutils'),
        'findings': [monitor_model.finding('example', 'Signal', 'Signal', [], 'https://example.org/')],
    }}}
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    api = TestClient(create_app(db))
    original = api.get('/api/v2/packages?maintenance=Signal').json()
    for mid in ('source', 'version', 'build', 'external'):
        focused = api.get('/api/v2/packages?maintenance=Signal&monitor=' + mid).json()
        assert [row['name'] for row in focused['items']] == ['binutils']
        for key in ('total', 'counts', 'buildsystems', 'build_statuses', 'maintenance_labels'):
            assert focused[key] == original[key]
