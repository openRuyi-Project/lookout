from datetime import datetime, timezone

from tests.conftest import ProjectedClient
from tracker import state
from tracker.api import create_app
from tracker.monitors import model as monitor_model, runner as monitor


def observed(snapshot, name, findings=(), status='ok'):
    return {'status': status, 'subject': monitor_model.subject(snapshot, name),
            'findings': list(findings), 'checked_at': state.utcnow()}


def test_results_and_coverage_share_linked_filter_context(snapshot, tmp_path):
    snapshot['monitor_catalog'] = {'license': {'title': 'License'}}
    finding = monitor_model.finding('changed', 'License', 'MIT → BSD-2-Clause', [], 'https://example.org/')
    snapshot['monitors'] = {
        'binutils': {'license': observed(snapshot, 'binutils', [finding])},
        'foo4': {'license': observed(snapshot, 'foo4')},
        'untracked': {'license': observed(snapshot, 'untracked', status='unsupported')},
    }
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    client = ProjectedClient(create_app(db))
    # Coverage is explicit; both HTTP representations default to results.
    assert client.get('/api/v2/packages?monitor=license&section=coverage').json()['total'] == 5
    results = client.get('/api/v2/packages?monitor=license&section=results').json()
    assert [item['name'] for item in results['items']] == ['binutils']
    assert (results['result_count'], results['coverage_count']) == (1, 5)
    assert results['check_statuses'] == {'ok': 2, 'pending': 2, 'unsupported': 1}
    assert results['items'][0]['monitors']['license']['data']['entries'][0]['title'] == 'MIT → BSD-2-Clause'
    assert results['build_statuses']['rva23'] == [
        {'value': 'succeeded', 'label': 'Succeeded', 'count': 1},
    ]
    empty = client.get('/api/v2/packages?monitor=license&section=results&q=foo').json()
    assert (empty['total'], empty['result_count'], empty['coverage_count']) == (0, 0, 2)
    coverage = client.get('/api/v2/packages?monitor=license&section=coverage&build=rva20:failed').json()
    assert [item['name'] for item in coverage['items']] == ['foo3']
    assert (coverage['result_count'], coverage['coverage_count']) == (0, 1)
    assert coverage['check_statuses'] == {'pending': 1}
    check_link = client.get('/api/v2/packages?monitor=license&section=results&check=pending').json()
    assert check_link['section'] == 'coverage' and check_link['total'] == 2
    core = client.get('/api/v2/packages?monitor=build&section=results').json()
    assert core['total'] == 5


def test_focused_identifiers_are_complete_without_copying_provider_facts(snapshot, tmp_path):
    findings = [monitor_model.finding(str(i), 'Signal', f'Fact {i}', [], 'https://example.org/') for i in range(5)]
    snapshot['monitors'] = {'binutils': {'custom': observed(snapshot, 'binutils', findings)}}
    snapshot['monitor_catalog'] = {'custom': {'title': 'Custom'}}
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    client = ProjectedClient(create_app(db))
    overview = client.get('/api/v2/packages').json()['items'][0]['monitors']['custom']['data']
    assert overview['entries'] == [] and overview['labels'][0]['count'] == 5
    focused = client.get('/api/v2/packages?monitor=custom&section=results').json()
    data = focused['items'][0]['monitors']['custom']['data']
    assert len(data['entries']) == 5 and data['finding_count'] == 5 and 'findings' not in data
    assert all('facts' not in entry for entry in data['entries'])
    detail = client.get('/api/v2/packages/binutils').json()
    assert len(detail['monitors']['custom']['data']['findings']) == 5


def test_transient_source_failure_keeps_upgrade_evidence_explicitly_old(config, snapshot):
    config['packages']['binutils'] = {'monitors': {'license': {'pypi': 'fixture'}}}
    proposed = monitor.plan(config, snapshot, 'binutils', 'license')
    target = proposed['subject']['target_version']
    finding = monitor_model.finding('license', 'License', 'MIT → ISC', [], 'https://example.org/',
                                    scope='upgrade', target_version=target)
    previous = {**proposed, 'status': 'ok', 'checked_at': state.utcnow(),
                'attempted_at': state.utcnow(), 'findings': [finding]}
    snapshot['sources']['binutils']['error'] = 'temporary source failure'
    blocked = monitor.plan(config, snapshot, 'binutils', 'license')
    snapshot['monitors'] = {'binutils': {'license': monitor.execute('license', blocked, None, previous)}}
    result = monitor_model.project(snapshot, 'binutils', datetime.now(timezone.utc))
    assert len(result['findings']) == 1 and result['findings'][0]['stale']
    assert result['checks'][0]['status'] == 'input_unavailable'
    # A changed comparison input must not retain a mismatched assertion.
    snapshot['tracks']['binutils']['version'] = '4.0.0'
    assert monitor_model.project(snapshot, 'binutils', datetime.now(timezone.utc))['findings'] == []
