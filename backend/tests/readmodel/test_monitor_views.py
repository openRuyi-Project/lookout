from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import ProjectedClient
from tracker import state
from tracker.api import create_app
from tracker.monitors import model as monitor_model, registry as monitor_registry, runner as monitor
from tracker.readmodel import monitors as monitor_views, snapshot as view


def test_uniform_results_preserve_source_facts_and_raw_evidence(snapshot, tmp_path):
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    client = ProjectedClient(create_app(db))
    new = client.get('/api/v2/packages/foo3').json()
    modules = new['monitors']
    assert {m['data']['kind'] for m in modules.values()} == {'source', 'version', 'build'}
    for key, result in modules.items():
        assert set(result) == {'id', 'title', 'check', 'data'}
        assert result['id'] == key
    source, version, build = (modules[k]['data'] for k in ('source', 'version', 'build'))
    assert source['version'] == version['current'] == snapshot['sources']['foo3']['version']
    assert version['relation'] == 'current'
    assert version['watch'][0]['version'] == snapshot['tracks']['widget@4']['version']
    assert build['targets'][0]['raw_status'] == snapshot['builds']['foo3']['rva23']['raw_status']
    assert source['obs']['srcmd5'] == snapshot['sources']['foo3']['srcmd5']
    assert source['source_url'] is None
    # A failed build is an observed result, not a failed collection.
    assert modules['build']['check']['status'] == 'ok'
    assert any(b['raw_status'] == 'failed' for b in build['targets'])
    listing = client.get('/api/v2/packages?q=foo3').json()
    summary = listing['items'][0]['monitors']
    assert 'metadata' not in summary['source']['data']
    assert 'watch' not in summary['version']['data']
    assert all('flavors' not in b for b in summary['build']['data']['targets'])
    assert listing['build_statuses']['rva20'][0]['count'] >= 0
    assert [row['name'] for row in client.get('/api/v2/packages?build=rva20:failed').json()['items']] == ['foo3']
    assert [row['name'] for row in client.get('/api/v2/packages?monitor=build&build=rva20:failed').json()['items']] == ['foo3']


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
    assert result['dimensions']['build:rva23'] == ['failed']
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
    result = ProjectedClient(create_app(db)).get('/api/v2/packages?monitor=future&section=coverage').json()
    assert result['check_statuses'] == {'pending': 5}
    assert result['total'] == 5
    assert all(row['monitors']['future']['check']['status'] == 'pending' for row in result['items'])
    assert result['maintenance_labels'] == {'Outdated': 2, 'Untracked': 2}
    assert all(not row['monitors']['future']['data']['labels'] for row in result['items'])


def test_only_declared_adapters_are_published_without_importing_providers(snapshot):
    # Saved observations cannot activate a monitor; the catalog owns that decision.
    subject = monitor_model.subject(snapshot, 'binutils')
    snapshot['monitors'] = {'binutils': {'old_plugin': {
        'status': 'ok', 'subject': subject, 'findings': [], 'checked_at': state.utcnow(),
    }}}
    assert 'old_plugin' not in {module.id for module in monitor_views.registry(snapshot)}
    snapshot['monitor_catalog'] = {'old_plugin': {'title': 'External'}}
    modules = monitor_views.registry(snapshot)
    assert modules[-1].describe() == {'id': 'old_plugin', 'title': 'External', 'kind': 'evidence'}
    rows, _ = view.project_monitors(snapshot)
    assert rows[0]['monitors']['old_plugin']['data']['findings'] == []


def test_enabled_adapter_cannot_shadow_a_core_monitor(config, monkeypatch):
    monkeypatch.setitem(monitor_registry.REGISTRY, 'source', monitor_registry.REGISTRY['license'])
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
    api = ProjectedClient(create_app(db))
    original = api.get('/api/v2/packages?maintenance=Signal').json()
    for mid in ('source', 'version', 'build', 'external'):
        focused = api.get('/api/v2/packages?maintenance=Signal&monitor=' + mid).json()
        assert [row['name'] for row in focused['items']] == ['binutils']
        for key in ('total', 'counts', 'buildsystems', 'build_statuses', 'maintenance_labels'):
            assert focused[key] == original[key]
