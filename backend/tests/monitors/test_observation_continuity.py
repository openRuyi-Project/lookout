"""A deployment may schedule work; it must not erase the last provider result."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tracker import state
from tracker.monitors import model, runner
from tracker.monitors.observations import visible
from tracker.monitors.requires import monitor as requires


@pytest.fixture
def observed(config, snapshot, monkeypatch):
    adapter = SimpleNamespace(VERSION=1, HOSTS=set(), query_subject=model.version_query,
        inputs=lambda package, configured: {'name': package['name']},
        check=lambda *args: {'status': 'ok', 'findings': [], 'note': None})
    monkeypatch.setitem(runner.REGISTRY, 'fixture', adapter)
    proposed = runner.plan(config, snapshot, 'binutils', 'fixture')
    result = {**proposed, 'status': 'ok', 'checked_at': state.utcnow(), 'attempted_at': state.utcnow(),
        'findings': [model.finding('fixture', 'Advisory', 'Fixture', [], 'https://example.org/advisory')]}
    return adapter, result


def test_interpretation_change_retains_original_evidence_and_time(config, snapshot, observed):
    adapter, result = observed
    original = deepcopy(result)
    adapter.VERSION += 1
    proposed = runner.plan(config, snapshot, 'binutils', 'fixture')
    pending = runner.retain_current(proposed, result)
    assert pending['fingerprint'] != result['fingerprint']
    assert pending['query_fingerprint'] == result['query_fingerprint']
    assert pending['last_result'] == original and result == original
    snapshot['monitors'] = {'binutils': {'fixture': pending}}
    projection = model.project(snapshot, 'binutils', datetime.now(timezone.utc))
    assert projection['checks'][0]['status'] == 'pending'
    assert projection['checks'][0]['checked_at'] == original['checked_at']
    assert len(projection['findings']) == 1 and projection['findings'][0]['stale']


def test_new_query_cannot_display_old_findings(config, snapshot, observed):
    _, result = observed
    snapshot['sources']['binutils']['version'] = '5.0'
    proposed = runner.plan(config, snapshot, 'binutils', 'fixture')
    pending = runner.retain_current(proposed, result)
    assert pending['last_result'] == result
    assert visible(pending)['findings'] == []


def test_retries_do_not_nest_or_destroy_history(config, snapshot, observed):
    adapter, result = observed
    adapter.VERSION += 1
    proposed = runner.plan(config, snapshot, 'binutils', 'fixture')
    pending = runner.retain_current(proposed, result)
    for _ in range(5):
        pending = runner.retain_current(proposed, pending)
        pending = runner.failed(proposed, pending, OSError('offline'), state.utcnow())
    assert pending['last_result'] == result
    assert 'last_result' not in pending['last_result']
    assert visible(pending)['checked_at'] == result['checked_at']


def test_completed_empty_check_replaces_old_findings(config, snapshot, observed):
    adapter, result = observed
    adapter.VERSION += 1
    proposed = runner.plan(config, snapshot, 'binutils', 'fixture')
    pending = runner.retain_current(proposed, result)
    io = SimpleNamespace(for_hosts=lambda *args, **kwargs: None)
    completed = runner.execute('fixture', proposed, io, pending)
    assert completed['status'] == 'ok' and completed['findings'] == []
    assert 'last_result' not in completed


def test_reinterpretation_of_cache_does_not_refresh_evidence_age(config, snapshot, observed):
    adapter, result = observed
    proposed = runner.plan(config, snapshot, 'binutils', 'fixture')
    stamp = '2026-01-01T00:00:00+00:00'
    io = SimpleNamespace(for_hosts=lambda *args, **kwargs: SimpleNamespace(observed_at=stamp))
    completed = runner.execute('fixture', proposed, io, result)
    assert completed['checked_at'] == stamp
    assert completed['attempted_at'] != stamp


def test_old_query_key_can_be_migrated_without_refetch(config, snapshot, observed):
    _, result = observed
    result.pop('query_fingerprint')
    result['subject']['revision'] = 'older-packaging-revision'
    result['fingerprint'] = 'old-revision-dependent-fingerprint'
    proposed = runner.plan(config, snapshot, 'binutils', 'fixture')
    migrated = runner.previous_query('fixture', proposed, result)
    assert migrated['fingerprint'] == proposed['fingerprint']
    assert migrated['findings'] == result['findings']
    assert migrated['checked_at'] == result['checked_at']


def test_new_backend_and_packaging_changes_do_not_invalidate_requires(config, snapshot, monkeypatch):
    config['packages']['binutils'] = {'monitors': {'requires': {'pypi': 'widget'}}}
    before = runner.plan(config, snapshot, 'binutils', 'requires')
    snapshot['sources']['binutils']['srcmd5'] = 'description-only-revision'
    monkeypatch.setitem(requires.BACKENDS, 'new_registry', SimpleNamespace(inputs=lambda *args: None))
    after = runner.plan(config, snapshot, 'binutils', 'requires')
    assert before['fingerprint'] == after['fingerprint']


def test_batch_publication_retains_jobs_not_selected(config, snapshot, observed, configured_path, tmp_path):
    adapter, result = observed
    from tests.monitors.test_monitors import FixtureIO
    config['monitors'] = {'enabled': ['fixture'], 'batch_size': 1}
    snapshot['monitors'] = {'binutils': {'fixture': result}}
    # Sorting selects an earlier package, leaving binutils pending this batch.
    snapshot['sources']['aaa'] = deepcopy(snapshot['sources']['binutils'])
    snapshot['inventory']['aaa'] = 'aaa'
    adapter.VERSION += 1
    db = tmp_path / 'db'
    state.commit(db, snapshot)
    runner.collect(config, configured_path, db, io=FixtureIO({}))
    saved = state.read(db)['monitors']['binutils']['fixture']
    assert saved['status'] == 'pending' and saved['last_result'] == result
