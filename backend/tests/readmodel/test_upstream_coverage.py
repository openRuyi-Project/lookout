"""Package coverage and failed subchecks share the same API and UI selection."""
import pytest

from tracker import state
from tracker.monitors import model
from tests.helpers.documents import client_for


@pytest.mark.parametrize('status', ['ok', 'partial', 'error', 'unsupported', 'pending'])
def test_untracked_is_version_coverage_independent_of_other_monitors(snapshot, tmp_path, status):
    snapshot['monitor_catalog'] = {'fixture': {'title': 'Fixture'}}
    snapshot['monitors'] = {'untracked': {'fixture': dict(
        subject=model.subject(snapshot, 'untracked'), status=status,
        checked_at=state.utcnow(), findings=[])}}
    client, _ = client_for(snapshot, tmp_path)
    selection = client.get('/api/v2/packages', params={'maintenance': 'Untracked'}).json()
    assert {row['name'] for row in selection['items']} == {'untracked', 'unknown'}


@pytest.mark.parametrize('scope_status,failed', [('error', True), ('unsupported', False)])
def test_partial_scope_keeps_facts_and_counts_only_actual_failures(snapshot, tmp_path, scope_status, failed):
    snapshot['monitor_catalog'] = {'fixture': {'title': 'Fixture'}}
    snapshot['monitors'] = {'binutils': {'fixture': dict(
        subject=model.subject(snapshot, 'binutils'), scope='current_and_upgrade',
        status='partial', checked_at=state.utcnow(), findings=[], scope_checks={
            'current': {'status': 'ok', 'checked_at': state.utcnow(), 'note': None},
            'upgrade': {'status': scope_status, 'note': 'Fixture upgrade metadata'}},
    )}}
    client, _ = client_for(snapshot, tmp_path)
    for parameters in ({'maintenance': 'CheckFailed'}, {'monitor': 'fixture', 'check': 'failed'}):
        response = client.get('/api/v2/packages', params=parameters).json()
        assert response['total'] == int(failed)
        if failed:
            assert response['items'][0]['monitors']['fixture']['check']['status'] == 'partial'
    result = client.get('/api/ui/packages', params={'monitor': 'fixture', 'check': 'failed'}).json()
    choices = result['controls']['navigation'][0]['choices']
    assert next(c['count'] for c in choices if c['label'] == 'CheckFailed') == int(failed)
    checks = next(s for s in client.get('/api/ui/packages/binutils').json()['sections'] if s['id'] == 'checks')
    row = next(row for row in checks['table']['rows'] if row['id'] == 'check-fixture')
    if failed:
        assert 'upgrade: Fixture upgrade metadata' in [v['text'] for cell in row['cells'] for line in cell['lines'] for v in line]


def test_failed_watch_history_and_source_parse_reach_checks(snapshot, tmp_path):
    snapshot['tracks']['widget@4']['error'] = 'Watch request failed'
    snapshot['builds']['foo3']['rva20']['history_error'] = 'History request failed'
    snapshot['sources']['foo3']['version_error'] = 'RPM parse failed'
    client, _ = client_for(snapshot, tmp_path)
    response = client.get('/api/v2/packages/foo3').json()
    failed = {mid: m['check']['failures'] for mid, m in response['monitors'].items() if model.check_failed(m['check'])}
    assert set(failed) == {'source', 'version', 'build'}
    assert 'widget@4: Watch request failed' in failed['version']
    assert any('rva20 / foo3 / history: History request failed' == reason for reason in failed['build'])
    document = client.get('/api/ui/packages/foo3').json()
    checks = next(s for s in document['sections'] if s['id'] == 'checks')
    for mid, reasons in failed.items():
        row = next(row for row in checks['table']['rows'] if row['id'] == 'check-' + mid)
        assert set(reasons) <= {v['text'] for cell in row['cells'] for line in cell['lines'] for v in line}
    listing = client.get('/api/v2/packages', params={'q': 'foo3', 'maintenance': 'CheckFailed'}).json()
    assert listing['total'] == 1


def test_partial_enrichment_is_failed_not_an_empty_success(snapshot, tmp_path):
    snapshot['monitor_catalog'] = {'fixture': {'title': 'Fixture'}}
    snapshot['monitors'] = {'binutils': {'fixture': dict(
        subject=model.subject(snapshot, 'binutils'), status='partial',
        note='Auxiliary provider timed out', checked_at=state.utcnow(), findings=[])}}
    client, _ = client_for(snapshot, tmp_path)
    result = client.get('/api/v2/packages', params={'monitor': 'fixture', 'check': 'failed'}).json()
    assert result['total'] == result['check_groups']['failed'] == 1
    assert result['check_statuses'] == {'partial': 1, 'pending': 4}
    check = result['items'][0]['monitors']['fixture']['check']
    assert check['failures'] == ['Auxiliary provider timed out']
