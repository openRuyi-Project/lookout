from copy import deepcopy
from datetime import UTC, datetime, timedelta

from tests.helpers.documents import client_for
from tracker import collector, state
from tracker.monitors import runner
from tracker.readmodel import snapshot as view


def test_refresh_failure_retains_comparison_and_exact_monitor_inputs(snapshot, config):
    now = datetime.now(UTC)
    stamp = (now - timedelta(days=30)).isoformat()
    config.setdefault('spec', {})['interval_seconds'] = 60
    config['packages']['binutils'] = {'monitors': {'eol': {'product': 'fixture', 'cycle_parts': 2}}}
    fact = state.success({}, {'head': 'fixture-head', 'metadata': {'version': '3.9.0'}}, stamp)
    snapshot = collector.merge_specs(config, snapshot, {'binutils': fact}, 'git fetch timeout', now.isoformat())
    original = deepcopy(snapshot)
    rows, collection = view.project_monitors(snapshot, now)
    version = next(r for r in rows if r['name'] == 'binutils')['monitors']['version']
    assert version['data']['relation'] == 'outdated'
    assert version['check']['status'] == 'expired'
    assert stamp in version['check']['note']
    assert 'Outdated' in version['dimensions']['maintenance']
    assert 'git fetch timeout' in collection['errors']
    assert runner.plan(config, snapshot, 'binutils', 'eol')['status'] == 'pending'
    assert snapshot == original
    recovered = collector.merge_specs(config, snapshot,
        {'binutils': state.success(fact, {}, now.isoformat())}, None, now.isoformat())
    rows, _ = view.project_monitors(recovered, now)
    version = next(r for r in rows if r['name'] == 'binutils')['monitors']['version']
    assert version['data']['relation'] == 'outdated'
    assert version['check']['status'] == 'ok'
    recovered['specs']['binutils']['error'] = 'native SPEC parse failed'
    rows, _ = view.project_monitors(recovered, now)
    version = next(r for r in rows if r['name'] == 'binutils')['monitors']['version']
    assert version['data']['relation'] == 'unknown'
    assert version['check']['status'] == 'input_unavailable'
    assert runner.plan(config, recovered, 'binutils', 'eol')['status'] == 'unsupported'


def test_home_reports_failure_and_keeps_retained_version(snapshot, tmp_path):
    stamp = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    snapshot['specs']['binutils'] = state.success({}, {'head': 'fixture', 'metadata': {'version': '3.9.0'}}, stamp)
    snapshot['components']['spec_git'] = state.failure({}, 'git fetch timeout', state.utcnow())
    client, _ = client_for(snapshot, tmp_path)
    document = client.get('/api/ui/packages').json()
    assert any('git fetch timeout' in n for n in document['data_status']['notes'])
    row = next(r for r in document['table']['rows'] if r['key'] == 'binutils')
    texts = [v['text'] for c in row['cells'] for line in c['lines'] for v in line]
    assert '3.9.0' in texts and '3.10.0' in texts
