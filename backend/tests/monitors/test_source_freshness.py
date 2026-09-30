"""Repository refresh failures do not invalidate fresh, pinned package observations."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from tracker import state
from tracker.monitors import runner
from tracker.readmodel import snapshot as view


def prepare(snapshot, now):
    snapshot['specs']['binutils'] = state.success({}, dict(
        head='a' * 40, metadata={'version': '1.0'}, native_query={}), now.isoformat())
    snapshot['components']['spec_git'] = state.success({}, {'head': 'a' * 40}, now.isoformat())
    return snapshot['specs']['binutils']


def test_repository_failure_preserves_fresh_facts_and_query_fingerprints(config, snapshot):
    now = datetime.now(timezone.utc)
    prepare(snapshot, now)
    config['native']['binutils'] = {'source': 'pypi', 'pypi': 'fixture'}
    before = deepcopy(snapshot)
    proposed = runner.plan(config, snapshot, 'binutils', 'security')
    snapshot['components']['spec_git']['error'] = 'git fetch exited 128 (tls)'
    after = runner.plan(config, snapshot, 'binutils', 'security')
    assert after['status'] == proposed['status'] == 'pending'
    assert after['fingerprint'] == proposed['fingerprint']
    source = state.current_source(snapshot, 'binutils')
    assert source['version'] == '1.0' and source['error'] is None
    assert source['fetched_at'] == before['specs']['binutils']['fetched_at']
    rows, collection = view.project_monitors(snapshot, now)
    source_view = next(row for row in rows if row['name'] == 'binutils')['monitors']['source']
    check = source_view['check']
    assert check['status'] == 'ok'
    assert 'CheckFailed' not in source_view['dimensions'].get('maintenance', [])
    assert check['note'] == 'Repository refresh: git fetch exited 128 (tls)'
    assert 'git fetch exited 128 (tls)' in collection['errors']


def test_repository_retry_does_not_make_expired_facts_fresh(config, snapshot):
    now = datetime.now(timezone.utc)
    fact = prepare(snapshot, now - timedelta(days=2))
    config['native']['binutils'] = {'source': 'pypi', 'pypi': 'fixture'}
    snapshot['components']['spec_git']['error'] = 'git fetch timeout'
    snapshot['components']['spec_git']['attempted_at'] = now.isoformat()
    rows, _ = view.project_monitors(snapshot, now)
    check = next(row for row in rows if row['name'] == 'binutils')['monitors']['source']['check']
    assert check['status'] == 'expired'
    assert state.stale(state.current_source(snapshot, 'binutils'), now, 86400)
    assert runner.plan(config, snapshot, 'binutils', 'security')['status'] == 'unsupported'
    assert fact['fetched_at'] != now.isoformat()


def test_package_parse_failure_remains_a_package_error(snapshot):
    now = datetime.now(timezone.utc)
    fact = prepare(snapshot, now)
    fact['error'] = 'fixture SPEC parse failed'
    snapshot['components']['spec_git']['error'] = 'git fetch timeout'
    assert state.current_source(snapshot, 'binutils')['error'] == fact['error']
    rows, _ = view.project_monitors(snapshot, now)
    check = next(row for row in rows if row['name'] == 'binutils')['monitors']['source']['check']
    assert check['status'] == 'error'
