"""Maintenance choices, row cues and error links share actual projected states."""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.helpers.documents import client_for
from tests.helpers.listing import document, facet_destination
from tracker import state
from tracker.monitors import model
from tracker.readmodel import monitors


@pytest.fixture
def client(snapshot, tmp_path, monkeypatch):
    monkeypatch.setattr(state, 'compare', lambda current, latest, *args:
                        'unknown' if not current or not latest else
                        'current' if current == latest else 'outdated')
    snapshot['monitor_catalog'] = {'fixture': {'title': 'Fixture provider'}}
    snapshot['monitors'] = {
        'binutils': {'fixture': {
            'subject': model.subject(snapshot, 'binutils'), 'status': 'error',
            'error': 'Fixture provider timeout', 'attempted_at': state.utcnow(), 'findings': [],
        }},
    }
    snapshot['tracks']['widget@3']['error'] = 'Fixture upstream failure'
    return client_for(snapshot, tmp_path)[0]


def values(row, column):
    return [value for line in row['cells'][column]['lines'] for value in line]


@pytest.mark.parametrize('query', [{}, {'view': 'updates'}, {'view': 'untracked'},
                                 {'maintenance': 'CheckFailed'}, {'view': 'updates', 'maintenance': 'CheckFailed'},
                                 {'q': 'foo', 'build': ['rva20:failed']}])
def test_maintenance_links_toggle_or_restart_without_hidden_conditions(client, query):
    page = document(client, query)
    nav = next(row for row in page['controls']['choice_rows'] if row['label'] == 'Alerts')
    assert nav['show_label'] is False
    assert {'Outdated', 'Untracked'} <= {c['label'] for c in nav['choices']}
    for choice in nav['choices']:
        result = facet_destination(client, page, choice)
        params = parse_qs(urlsplit(choice['href']).query)
        if 'Untracked' in params.get('maintenance', []):
            assert all(not any(v['text'] == 'Outdated' for v in values(row, 0))
                       for row in result['table']['rows'])


def test_labels_and_underlines_follow_the_same_version_selection(client):
    page = document(client, {})
    rows = {row['key']: row for row in page['table']['rows']}
    for view, label, column in [('updates', 'Outdated', 0), ('untracked', 'Untracked', 1)]:
        selected = {row['key'] for row in document(client, {'view': view})['table']['rows']}
        tagged = {name for name, row in rows.items() if any(v['text'] == label for v in values(row, column))}
        assert selected == tagged
    assert rows['untracked']['cells'][1]['lines'][0][0]['decoration'] == 'dashed'
    assert rows['unknown']['cells'][1]['lines'][0][0]['decoration'] is None  # no invented source version
    assert not any(v['text'] == 'Untracked' for v in values(rows['foo3'], 1))  # tracked but failed
    assert not any(v['text'] == 'Untracked' for v in values(rows['untracked'], 0))
    for row in rows.values():
        identity, = row['cells'][0]['lines']
        assert identity[0]['text'] == row['key']
        systems = [i for i, value in enumerate(identity) if value['variant'] == 'solid']
        assert not systems or systems == [1]


def test_errors_aggregate_packages_and_link_to_the_failed_check(client):
    page = document(client, {'maintenance': 'CheckFailed'})
    assert {row['key'] for row in page['table']['rows']} == {'binutils', 'foo3'}
    for row in page['table']['rows']:
        badge, = [v for v in values(row, 0) if v['text'] == 'CheckFailed']
        target = urlsplit(badge['href'])
        assert parse_qs(target.query)['maintenance'] == ['CheckFailed']
        detail = client.get('/api/ui/packages/' + row['key']).json()
        checks = next(section for section in detail['sections'] if section['id'] == 'checks')
        selected, = [item for item in checks['table']['rows']
                      if any(v['text'] == 'CheckFailed' for v in values(item, 1))]
        assert any(v['text'] == 'CheckFailed' for v in values(selected, 1))
        assert any('Fixture' in v['text'] for v in values(selected, 1))
    assert document(client, {'maintenance': 'CheckFailed', 'view': 'updates'})['total'] == 1


@pytest.mark.parametrize('label,other', [('Outdated', 'Untracked'), ('Untracked', 'Outdated')])
def test_version_issues_use_the_same_intersection_and_zero_restart(client, label, other):
    page = document(client, {'maintenance': [label, 'CheckFailed']})
    choices = {c['label']: c for c in page['controls']['choice_rows'][0]['choices']}
    assert choices[other]['count'] == 0
    assert {c['label'] for c in choices.values() if c['selected']} == {label, 'CheckFailed'}
    assert parse_qs(urlsplit(choices[other]['href']).query)['maintenance'] == [other]
    restarted = facet_destination(client, page, choices[other])
    assert restarted['total'] == document(client, {'maintenance': other})['total']
    impossible = document(client, {'maintenance': [label, other]})
    assert impossible['total'] == 0
    assert all(c['count'] == 0 for c in impossible['controls']['choice_rows'][0]['choices'])


def test_version_issue_counts_and_row_links_have_one_authority(client):
    for view, label, column in [('updates', 'Outdated', 0), ('untracked', 'Untracked', 1)]:
        version_view = client.get('/api/v2/packages', params={'view': view}).json()
        filtered = client.get('/api/v2/packages', params={'maintenance': label}).json()
        assert version_view['items'] == filtered['items']
        page = document(client, {'maintenance': label})
        choices = page['controls']['choice_rows'][0]['choices']
        assert len(choices) == len({c['label'] for c in choices})
        assert next(c for c in choices if c['label'] == label)['count'] == page['total']
        for row in page['table']['rows']:
            tag = next(v for v in values(row, column) if v['text'] == label)
            query = parse_qs(urlsplit(tag['href']).query)
            assert query['maintenance'] == [label] and 'view' not in query


@pytest.mark.parametrize('status', ['ok', 'pending', 'unsupported', 'not_configured',
                                  'not_applicable', 'input_unavailable', 'expired', 'partial', 'error'])
def test_error_means_monitor_error_not_unknown_or_coverage(status):
    result = {'check': {'status': status}, 'dimensions': {'maintenance': ['Advisory']}}
    module = monitors.Monitor('fixture', 'Fixture', 'evidence', lambda _: deepcopy(result))
    projected = module.read(None)
    assert ('CheckFailed' in projected['dimensions']['maintenance']) == (status == 'error')
    assert result['dimensions']['maintenance'] == ['Advisory']


def test_failed_obs_build_is_not_a_monitor_error(snapshot, tmp_path):
    assert snapshot['builds']['foo3:tools']['rva20']['raw_status'] == 'failed'
    client, _ = client_for(snapshot, tmp_path)
    assert document(client, {'maintenance': 'CheckFailed'})['total'] == 0


def test_not_applicable_is_not_untracked_even_when_source_is_missing(snapshot, tmp_path):
    snapshot['bindings']['unknown'] = {'not_applicable': True}
    client, _ = client_for(snapshot, tmp_path)
    page = document(client, {'view': 'untracked'})
    assert 'unknown' not in {row['key'] for row in page['table']['rows']}


def test_multiple_failed_monitors_count_once_and_checks_keep_each_error(snapshot, tmp_path):
    snapshot['tracks']['widget@3']['error'] = 'Fixture version error'
    snapshot['builds']['foo3']['rva23']['error'] = 'Fixture build API error'
    client, _ = client_for(snapshot, tmp_path)
    page = document(client, {'maintenance': 'CheckFailed'})
    assert page['total'] == 1
    badge, = [v for v in values(page['table']['rows'][0], 0) if v['text'] == 'CheckFailed']
    assert parse_qs(urlsplit(badge['href']).query)['maintenance'] == ['CheckFailed']
    detail = client.get('/api/ui/packages/foo3').json()
    checks = next(s for s in detail['sections'] if s['id'] == 'checks')['table']['rows']
    errors = [row for row in checks if any(v['text'] == 'CheckFailed' for v in values(row, 1))]
    assert {row['id'] for row in errors} == {'check-version', 'check-build'}
