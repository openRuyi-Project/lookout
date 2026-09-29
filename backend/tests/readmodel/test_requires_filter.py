"""Filter plumbing consumes projected dimensions, not requirement/provider facts."""
from itertools import product
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

from tracker.api import create_app
from tracker.presentation import navigation as presentation_navigation
from tracker.readmodel import snapshot as view
from tracker.readmodel.packages import PackageList


TARGETS = [dict(id=name, label=name) for name in ('first', 'second')]


def dimension_rows():
    # Duplicate unmet values model several failed requirements in one package.
    examples = [
        ('pkg-a', ['unmet', 'changes', 'unmet'], 'cmake', 'Advisory', 'blocked', 'failed', 'ok'),
        ('pkg-b', ['changes'], 'cmake', 'Advisory', 'blocked', 'succeeded', 'ok'),
        ('pkg-c', ['unknown'], 'cmake', 'Advisory', 'failed', 'succeeded', 'partial'),
        ('pkg-d', ['unmet'], 'meson', 'EOL', 'succeeded', 'failed', 'ok'),
        ('pkg-e', ['unmet'], 'cmake', 'EOL', 'blocked', 'failed', 'error'),
        ('pkg-f', [], 'cmake', 'Advisory', 'succeeded', 'succeeded', 'ok'),
        ('other', [], 'cmake', 'Advisory', 'succeeded', 'succeeded', 'pending'),
    ]
    return [dict(name=name, monitors={'requires': dict(dimensions={
        'requires': values, 'buildsystem': [system], 'maintenance': [label],
        'view': ['updates'] if 'changes' in values else [],
        'build:first': [first], 'build:second': [second],
        'check:requires': [check], 'findings:requires': ['yes'] if name != 'other' else [],
    })}) for name, values, system, label, first, second, check in examples]


def select(index, **changes):
    return index.select(**dict(view='all', buildsystem='', maintenance='', builds={}, page=1,
                               per_page=100, monitor='requires', **changes))


def test_requires_counts_and_every_linked_facet_match_independent_scan():
    rows = dimension_rows()
    index = PackageList(rows, TARGETS)
    for query, requirement, system, label, first, second in product(
        ('', 'PKG'), ('unmet', 'changes'), ('', 'cmake', 'meson'),
        ('', 'Advisory', 'EOL'), ('', 'blocked'), ('', 'failed'),
    ):
        selections = {'requires': requirement, 'buildsystem': system, 'maintenance': label,
                      'build:first': first, 'build:second': second}

        def matches(row, without=None, value=None):
            dimensions = row['monitors']['requires']['dimensions']
            return (query.casefold() in row['name'].casefold()
                    and all(not choice or choice in dimensions[dimension]
                            for dimension, choice in selections.items() if dimension != without)
                    and (not value or value in dimensions[without]))

        result = index.select(view='all', query=query, requires=requirement, buildsystem=system,
                              maintenance=label, builds={'first': first, 'second': second}, page=1, per_page=2)
        expected = [row for row in rows if matches(row)]
        assert result['total'] == len(expected)
        assert result['items'] == expected[:2]
        assert result['requires_counts']['all'] == sum(matches(row, 'requires') for row in rows)
        for option in ('unmet', 'changes'):
            assert result['requires_counts'][option] == sum(matches(row, 'requires', option) for row in rows)
        for dimension, options in [('buildsystem', result['buildsystems']),
                                   ('maintenance', result['maintenance_labels'])]:
            for value, count in options.items():
                assert count == sum(matches(row, dimension, value)
                                    and (dimension != 'maintenance' or matches(row)) for row in rows)
        for target, options in result['build_statuses'].items():
            for option in options:
                assert option['count'] == sum(matches(row, 'build:' + target, option['value']) for row in rows)


def test_unknown_and_changes_do_not_imply_unmet():
    index = PackageList(dimension_rows(), TARGETS)
    unmet = select(index, requires='unmet')
    assert [row['name'] for row in unmet['items']] == ['pkg-a', 'pkg-d', 'pkg-e']
    assert unmet['requires_counts'] == {'all': 7, 'changes': 2, 'unknown': 1, 'unmet': 3}
    assert [row['name'] for row in select(index, requires='changes')['items']] == ['pkg-a', 'pkg-b']
    # Own-facet counts offer both choices even after selecting one of them.
    assert select(index, requires='changes')['requires_counts'] == unmet['requires_counts']


def test_requires_results_and_coverage_preserve_their_other_selections():
    index = PackageList(dimension_rows(), TARGETS)
    results = select(index, requires='unmet', findings_only=True)
    assert (results['total'], results['result_count'], results['coverage_count']) == (3, 3, 3)
    assert results['requires_counts']['all'] == 6
    coverage = select(index, requires='unmet', check='ok')
    assert (coverage['total'], coverage['result_count'], coverage['coverage_count']) == (2, 3, 3)
    assert coverage['check_statuses'] == {'error': 1, 'ok': 2}
    assert coverage['requires_counts'] == {'all': 4, 'changes': 2, 'unmet': 2}
    empty = select(index, requires='unmet', query='pkg-b')
    assert empty['total'] == 0
    assert empty['requires_counts'] == {'all': 1, 'changes': 1, 'unmet': 0}


@pytest.fixture
def linked_client(snapshot, tmp_path, monkeypatch):
    snapshot['monitor_catalog'] = {'requires': {'title': 'RuntimeDeps'}}
    rows, collection = view.project_monitors(snapshot)
    projected = {'binutils': ['unmet', 'changes', 'unmet'], 'foo3': ['changes'],
                 'foo4': ['unknown'], 'unknown': [], 'untracked': ['unmet']}
    for row in rows:
        result = row['monitors']['requires']
        check = {'unknown': 'not_configured', 'untracked': 'error'}.get(row['name'], 'ok')
        result['check'].update(status=check)
        result['dimensions'].update({
            'requires': projected[row['name']],
            'check:requires': [check],
            'findings:requires': ['yes'] if projected[row['name']] else [],
            'maintenance': ['Advisory'],
        })
        row['monitors']['source']['dimensions']['buildsystem'] = ['cmake']
    index = PackageList(rows, snapshot['targets'])
    app = create_app(tmp_path / 'unused.db')
    # Tests this boundary against a prepared projection; no provider/model rewrite.
    monkeypatch.setattr(app.state.projection, 'read', lambda: (snapshot, index, collection))
    return TestClient(app)


def parsed(link):
    return parse_qs(urlsplit(link).query)


def test_requires_filter_is_validated_only_on_monitored_api(linked_client):
    response = linked_client.get('/api/v2/packages?monitor=requires&requires=unmet&section=results')
    assert response.status_code == 200
    result = response.json()
    assert [item['name'] for item in result['items']] == ['binutils', 'untracked']
    assert result['requires_counts'] == {'all': 4, 'changes': 2, 'unknown': 1, 'unmet': 2}
    for path in ('/api/v2/packages', '/api/ui/packages'):
        assert linked_client.get(path + '?requires=unknown').status_code == 422
        assert linked_client.get(path + '?requires=invalid').status_code == 422


def test_requires_navigation_and_forms_keep_search_menus_and_page_size(linked_client):
    query = ('monitor=requires&requires=unmet&section=results&buildsystem=cmake'
             '&maintenance=Advisory&build=rva23:succeeded&build=rva20:succeeded&per_page=1&page=2')
    document = linked_client.get('/api/ui/packages?' + query).json()
    controls = document['controls']
    navigation = next(nav for nav in controls['navigation'] if nav['label'] == 'Dependencies')
    assert [(choice['label'], choice['count'], choice['selected']) for choice in navigation['choices']] == [
        ('DepMismatch', 2, True), ('DepChanges', 2, False),
        ('Uncovered', 1, False), ('CheckFailed', 1, False)]
    assert not any(nav['label'] == 'Results and coverage' for nav in controls['navigation'])
    for choice, requirement in zip(navigation['choices'], ('unmet', 'changes')):
        query = parsed(choice['href'])
        assert query.get('requires', ['']) == [requirement]
        assert query['page'] == ['1']
        assert 'build' not in query and 'maintenance' not in query
        assert query['buildsystem'] == ['cmake']
        assert query['per_page'] == ['1']
    assert {'name': 'requires', 'value': 'unmet'} in controls['hidden']
    assert {'name': 'buildsystem', 'value': 'cmake'} in controls['hidden']
    assert document['global_navigation'][0]['label'] == 'BuildSystem'
    unmet_chip = next(choice for choice in controls['active'] if choice['label'] == 'DepMismatch')
    assert 'requires' not in parsed(unmet_chip['href'])
    assert parsed(unmet_chip['href'])['buildsystem'] == ['cmake']
    assert parsed(document['pagination'][0]['href'])['requires'] == ['unmet']
    assert parsed(document['pagination'][0]['href'])['page'] == ['1']
    links = presentation_navigation.Links({'q': 'a&b', 'requires': 'unmet', 'page': 9})
    assert parsed(links.to(buildsystem='cmake')) == {
        'q': ['a&b'], 'requires': ['unmet'], 'page': ['1'], 'buildsystem': ['cmake']}


def test_uncovered_link_resets_requirement_filter_and_keeps_navigation_stable(linked_client):
    document = linked_client.get('/api/ui/packages?monitor=requires&requires=unmet').json()
    navigation = next(nav for nav in document['controls']['navigation'] if nav['label'] == 'Dependencies')
    coverage_link = next(choice['href'] for choice in navigation['choices'] if choice['label'] == 'Uncovered')
    assert 'requires' not in parsed(coverage_link)
    coverage = linked_client.get(coverage_link.replace('/?', '/api/ui/packages?')).json()
    assert coverage['total'] == 1
    assert [row['key'] for row in coverage['table']['rows']] == ['unknown']
    checks = next(nav for nav in coverage['controls']['navigation'] if nav['label'] == 'Dependencies')
    assert [(choice['label'], choice['count']) for choice in checks['choices']] == [
        ('DepMismatch', 2), ('DepChanges', 2), ('Uncovered', 1), ('CheckFailed', 1)]
    assert [chip['label'] for chip in coverage['controls']['active']] == ['Check: Uncovered']
    unmet_link = next(choice['href'] for choice in checks['choices'] if choice['label'] == 'DepMismatch')
    assert parsed(unmet_link)['requires'] == ['unmet'] and 'check' not in parsed(unmet_link)
    unmet = linked_client.get(unmet_link.replace('/?', '/api/ui/packages?')).json()
    assert [row['key'] for row in unmet['table']['rows']] == ['binutils', 'untracked']


@pytest.mark.parametrize('section', ['results', 'coverage'])
@pytest.mark.parametrize('requirement', ['', 'unmet', 'changes'])
def test_navigation_counts_describe_the_link_destination(linked_client, section, requirement):
    document = linked_client.get('/api/ui/packages', params={
        'monitor': 'requires', 'section': section, 'requires': requirement}).json()
    for navigation in document['controls']['navigation']:
        if navigation['label'] != 'Dependencies':
            continue
        for choice in navigation['choices']:
            if choice['count'] is None:
                continue
            destination = linked_client.get(choice['href'].replace('/?', '/api/ui/packages?')).json()
            assert destination['total'] == choice['count']


def test_requires_navigation_is_not_added_to_other_monitors(linked_client):
    for query in ('', 'monitor=build', 'monitor=version'):
        document = linked_client.get('/api/ui/packages?' + query).json()
        assert not any(nav['label'] == 'Dependencies' for nav in document['controls']['navigation'])
