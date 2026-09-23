from datetime import datetime, timedelta, timezone
from tracker import monitor, monitor_model, state, view


def spec(snapshot, name='binutils', version='3.10.0'):
    snapshot['spec_interval_seconds'] = 60
    snapshot['specs'][name] = state.success({}, {'head': 'spec-revision',
        'native_query': {'spec_sha256': 'spec-bytes', 'context': {'additional_macros': []}},
        'metadata': {'version': version}}, state.utcnow())


def test_spec_version_drives_display_comparison_and_monitor(config, snapshot):
    spec(snapshot)
    config['packages']['binutils'] = {'monitors': {'eol': {'product': 'fixture'}}}
    rows, _ = view.project(snapshot)
    row = next(r for r in rows if r['name'] == 'binutils')
    assert row['current'] == row['latest'] == '3.10.0'
    assert row['relation'] == 'current'
    assert row['obs_version'] == row['source']['version'] == '3.9.0'
    planned = monitor.plan(config, snapshot, 'binutils', 'eol')
    assert planned['subject']['version'] == row['current']
    assert planned['subject']['revision'].startswith('spec:')
    assert planned['status'] == 'pending'
    assert row['builds'][0]['raw_status'] == 'succeeded'


def test_old_source_monitor_evidence_invalidates_on_spec_arrival(config, snapshot):
    old_subject = monitor_model.subject(snapshot, 'binutils')
    snapshot['monitors'] = {'binutils': {'eol': {'subject': old_subject, 'scope': 'current',
        'status': 'ok', 'checked_at': state.utcnow(), 'findings': []}}}
    spec(snapshot)
    result = monitor_model.project(snapshot, 'binutils', datetime.now(timezone.utc))
    assert result['checks'][0]['status'] == 'input_changed'
    first = monitor_model.subject(snapshot, 'binutils')
    snapshot['specs']['binutils']['fetched_at'] = state.utcnow()
    assert monitor_model.subject(snapshot, 'binutils') == first
    snapshot['specs']['binutils']['head'] = 'patch-change'
    assert monitor_model.subject(snapshot, 'binutils') != first


def test_failed_spec_never_falls_back_to_obs(config, snapshot):
    spec(snapshot)
    snapshot['specs']['binutils']['error'] = 'parse failure'
    rows, _ = view.project(snapshot)
    row = next(r for r in rows if r['name'] == 'binutils')
    assert row['current'] == '3.10.0' and row['relation'] == 'unknown'
    config['packages']['binutils'] = {'monitors': {'eol': {'product': 'fixture'}}}
    assert monitor.plan(config, snapshot, 'binutils', 'eol')['status'] == 'unsupported'
    snapshot['specs']['binutils']['metadata'] = None
    assert state.current_source(snapshot, 'binutils')['version'] is None


def test_spec_expiry_has_cache_deadline_and_fetch_failure_stays_visible(config, snapshot):
    spec(snapshot)
    now = datetime.now(timezone.utc)
    snapshot['obs_stale_after_seconds'] = 100
    snapshot['specs']['binutils']['fetched_at'] = (now - timedelta(seconds=119)).isoformat()
    assert view.next_transition(snapshot, now) <= now + timedelta(seconds=2)
    rows, _ = view.project(snapshot, now + timedelta(seconds=2))
    assert next(r for r in rows if r['name'] == 'binutils')['relation'] == 'unknown'
    snapshot['specs']['binutils']['fetched_at'] = now.isoformat()
    snapshot['components']['spec_git'] = {'error': 'fetch failed'}
    assert state.current_source(snapshot, 'binutils')['error'] == 'fetch failed'


def test_no_spec_preserves_obs_fallback(snapshot):
    subject = monitor_model.subject(snapshot, 'binutils')
    assert subject['version'] == '3.9.0' and subject['revision'] == 'h-binutils'


def test_failed_refresh_retains_matching_evidence_without_rechecking(config, snapshot):
    spec(snapshot)
    config['packages']['binutils'] = {'monitors': {'eol': {'product': 'fixture'}}}
    good = monitor.plan(config, snapshot, 'binutils', 'eol')
    previous = {**good, 'status': 'ok', 'checked_at': state.utcnow(),
                'findings': [monitor_model.finding('old', 'EOL', 'Old fact', [], 'https://example.org/')],
                'changed_at': 'original-change', 'evidence_revision': 'original-evidence'}
    snapshot['components']['spec_git'] = {'error': 'offline'}
    unavailable = monitor.plan(config, snapshot, 'binutils', 'eol')
    assert unavailable['status'] == 'unsupported'
    result = monitor.execute('eol', unavailable, None, previous)
    assert result['findings'] == previous['findings']
    assert result['changed_at'] == previous['changed_at']
    assert result['checked_at'] == previous['checked_at']
    snapshot['monitors'] = {'binutils': {'eol': result}}
    projected = monitor_model.project(snapshot, 'binutils', datetime.now(timezone.utc))
    assert projected['findings'][0]['stale'] is True
    snapshot['specs']['binutils']['head'] = 'different-revision'
    changed = monitor.plan(config, snapshot, 'binutils', 'eol')
    assert monitor.execute('eol', changed, None, previous)['findings'] == []
