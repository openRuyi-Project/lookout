from tests.helpers.listing import document, follow, modes, parsed
"""Reader filters follow visible controls; raw facts remain freely composable."""
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

from tracker import state
from tracker.api import create_app
from tracker.readmodel import snapshot as view
from tracker.readmodel.packages import PackageList












@pytest.mark.parametrize('monitor,section,retained', [
    ('', 'results', {'view', 'build', 'maintenance'}),
    ('version', 'results', {'view'}),
    ('build', 'results', {'build'}),
    ('requires', 'results', {'requires'}),
    ('fixture_signature', 'results', set()),
    ('version', 'coverage', set()),
    ('build', 'coverage', set()),
    ('requires', 'coverage', set()),
    ('fixture_signature', 'coverage', set()),
    ('source', 'coverage', set()),
])
def test_direct_ui_urls_ignore_filters_without_target_controls(scoped_client, monitor, section, retained):
    common = {'monitor': monitor, 'section': section, 'buildsystem': 'cmake', 'q': 'u',
              'per_page': 1, 'page': 1}
    if section == 'coverage' and monitor != 'source':
        common['check'] = 'uncovered'
    incoming = {'view': 'updates', 'requires': 'unmet', 'build': 'rva20:failed',
                'maintenance': 'Signature'}
    noisy = document(scoped_client, common | incoming)
    expected = document(scoped_client, common | {key: value for key, value in incoming.items() if key in retained})
    assert noisy == expected
    hidden = {item['name'] for item in noisy['controls']['hidden']}
    forbidden = incoming.keys() - retained
    assert not hidden & forbidden
    for choice in noisy['navigation']['choices'] + modes(noisy) + noisy['pagination']:
        destination = parsed(choice['href'])
        target_section = 'coverage' if destination.get('check') else destination.get('section', ['results'])[0]
        if destination.get('monitor', [''])[0] == monitor and target_section == section:
            # Staying in the same mode must not restore a discarded filter.
            assert not destination.keys() & forbidden


@pytest.mark.parametrize('monitor,section,has_build', [
    ('', 'results', True), ('build', 'results', True),
    ('version', 'results', False), ('requires', 'results', False),
    ('fixture_signature', 'results', False), ('build', 'coverage', False),
    ('version', 'coverage', False), ('requires', 'coverage', False),
])
def test_architecture_controls_only_accompany_build_columns(scoped_client, monitor, section, has_build):
    query = {'monitor': monitor, 'section': section}
    if section == 'coverage':
        query['check'] = 'uncovered'
    page = document(scoped_client, query)
    controls = [facet for facet in page['controls']['facets'] if facet['name'] == 'build']
    columns = [column['title'] for column in page['table']['columns']]
    targets = {'rva23', 'rva20', 'x86_64'}
    controls += [row for row in page['controls']['choice_rows'] if row['label'] in targets]
    assert bool(controls) is has_build
    assert bool(targets & set(columns)) is has_build
    assert ({control['label'] for control in controls} == targets) is has_build
    if monitor:
        assert not any(facet['name'] == 'maintenance' for facet in page['controls']['facets'])


def test_monitor_navigation_retains_only_destination_controls(scoped_client):
    page = document(scoped_client, {'monitor': 'build', 'build': ['rva23:succeeded', 'rva20:failed'],
                                   'q': 'foo', 'buildsystem': 'cmake', 'per_page': 1})
    for choice in page['navigation']['choices']:
        query = parsed(choice['href'])
        assert query['q'] == ['foo'] and query['buildsystem'] == ['cmake']
        assert query['per_page'] == ['1'] and query['page'] == ['1']
        has_build = query.get('monitor', [''])[0] in ('', 'build')
        assert ('build' in query) is has_build
        assert not {'requires', 'maintenance'} & query.keys()
        destination = follow(scoped_client, choice['href'])
        clean = {key: value for key, value in query.items() if key != 'build'}
        if not has_build:
            assert destination == document(scoped_client, clean)


@pytest.mark.parametrize('monitor', ['version', 'requires', 'build', 'fixture_signature'])
@pytest.mark.parametrize('mode', ['results', 'uncovered', 'failed'])
def test_mutually_exclusive_modes_keep_stable_navigation_and_correct_counts(scoped_client, monitor, mode):
    query = {'monitor': monitor, 'buildsystem': 'cmake', 'per_page': 1,
             'requires': 'unmet', 'view': 'updates', 'build': 'rva20:failed'}
    if mode != 'results':
        query['check'] = mode
    page = document(scoped_client, query)
    choices = modes(page)
    labels = [choice['label'] for choice in choices]
    assert 'Checks' not in labels
    assert {'Uncovered', 'Failed'} <= set(labels)
    if monitor == 'requires':
        assert labels == ['All', 'Unmet', 'Changes', 'Uncovered', 'Failed']
    elif monitor == 'version':
        assert labels == ['All', 'Updates', 'Uncovered', 'Failed']
    for choice in choices:
        if choice['count'] is not None:
            destination = follow(scoped_client, choice['href'])
            assert destination['total'] == choice['count'], (monitor, mode, choice)
        target = parsed(choice['href'])
        if choice['label'] in ('Uncovered', 'Failed'):
            assert not {'requires', 'view', 'build', 'maintenance'} & target.keys()
        elif choice['label'] in ('All', 'Unmet', 'Changes', 'Updates', 'Results'):
            assert 'check' not in target
            assert target.get('section', ['results']) == ['results']


def test_uncovered_requires_does_not_intersect_unmet_results(scoped_client):
    selected = document(scoped_client, {'monitor': 'requires', 'requires': 'unmet'})
    gaps = next(choice for choice in modes(selected) if choice['label'] == 'Uncovered')
    assert gaps['count'] == 2
    page = follow(scoped_client, gaps['href'])
    assert {row['key'] for row in page['table']['rows']} == {'foo3', 'unknown'}
    return_to_all = next(choice for choice in modes(page) if choice['label'] == 'All')
    assert return_to_all['count'] == 2
    assert {row['key'] for row in follow(scoped_client, return_to_all['href'])['table']['rows']} == {
        'binutils', 'untracked'}


def test_search_pagination_and_package_tags_cannot_restore_discarded_filters(scoped_client):
    page = document(scoped_client, {'monitor': 'requires', 'build': 'rva20:failed',
                                   'view': 'updates', 'maintenance': 'Signature', 'per_page': 1})
    assert page['total'] == 2
    forbidden = {'build', 'view', 'maintenance'}
    hidden = {item['name']: item['value'] for item in page['controls']['hidden']}
    assert not forbidden & hidden.keys()
    searched = document(scoped_client, hidden | {'q': 'untracked'})
    assert [row['key'] for row in searched['table']['rows']] == ['untracked']
    assert page['pagination']
    for link in page['pagination']:
        assert not forbidden & parsed(link['href']).keys()
        assert follow(scoped_client, link['href'])['total'] == 2
    for row in page['table']['rows']:
        tag = next(value for line in row['cells'][0]['lines'] for value in line
                   if value['text'] == 'cmake')
        assert not forbidden & parsed(tag['href']).keys()
        assert follow(scoped_client, tag['href'])['total'] == 2


def test_raw_v2_filters_remain_composable_without_ui_controls(scoped_client):
    query = {'monitor': 'fixture_signature', 'section': 'results', 'build': 'rva20:failed'}
    raw = scoped_client.get('/api/v2/packages', params=query)
    assert raw.status_code == 200
    assert [item['name'] for item in raw.json()['items']] == ['foo3']
    page = document(scoped_client, query)
    assert {row['key'] for row in page['table']['rows']} == {'foo3', 'untracked'}
    assert not any(facet['name'] == 'build' for facet in page['controls']['facets'])


@pytest.mark.parametrize('monitor', ['version', 'build', 'requires', 'fixture_signature'])
def test_old_all_checks_link_returns_to_results_without_redundant_ui(scoped_client, monitor):
    query = {'monitor': monitor, 'q': 'u', 'buildsystem': 'cmake', 'per_page': 1}
    legacy = document(scoped_client, query | {'section': 'coverage'})
    results = document(scoped_client, query | {'section': 'results'})
    assert legacy == results
    assert 'Check' not in [column['title'] for column in legacy['table']['columns']]
    assert 'Checks' not in [choice['label'] for choice in modes(legacy)]


def test_context_only_monitor_retains_its_collection_deep_link(scoped_client):
    page = document(scoped_client, {'monitor': 'source', 'section': 'results'})
    assert [column['title'] for column in page['table']['columns']] == ['Package', 'Check', 'Last checked']
    assert {parameter['name']: parameter['value'] for parameter in page['controls']['hidden']}['section'] == 'coverage'
    assert not any(facet['name'] in ('build', 'maintenance') for facet in page['controls']['facets'])
