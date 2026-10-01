"""Both search modes preserve their semantics through HTTP, edits and pagination."""
from itertools import product
from urllib.parse import parse_qsl, urlsplit

import pytest

from tracker.api import ListingQuery, PackageQuery
from tracker.presentation.navigation import Links
from tracker.readmodel.query import MAX_QUERY_NODES, Condition, Evaluation, FilterQuery, encode_parameters

INDEX = {
    'build:rva23': {'failed': {0}, 'succeeded': {1, 2}},
    'build:rva20': {'failed': {1, 3}, 'succeeded': {0, 2}},
    'buildsystem': {'cmake': {0, 1}, 'pyproject': {2}},
    'maintenance': {'Yanked': {0, 2}, 'Outdated': {0, 1}, 'Advisory': {1, 2}},
}


def query(raw):
    return ListingQuery.from_parameters(parse_qsl(raw, keep_blank_values=True)).filters


@pytest.mark.parametrize('operators', tuple(product(('AND', 'OR', 'NOT'), repeat=3)))
def test_advanced_ordered_roundtrip(operators):
    pairs = [(value, logic) for logic, value in zip(operators, 'ABC')]
    for markers in ([], [(1, 'NOT')], [(0, 'OR'), (1, 'AND'), (2, 'NOT')]):
        marked = list(pairs)
        for position, logic in reversed(markers):
            marked.insert(position + 1, ('Group', logic))
        parsed, remaining = FilterQuery.extract(marked)
        assert remaining == [] and parsed.parameters() == marked
        assert query(urlsplit(Links({'filters': parsed.model_dump()}).to()).query) == parsed


@pytest.mark.parametrize('raw,expected', [
    ('', {0, 1, 2, 3}),
    ('rva23_failed+ rva23_succeeded&Yanked', None),
    ('rva23_failed+rva23_succeed&Yanked', {0, 2}),
    ('rva23_failed%2Brva23_succeeded&Yanked', {0, 2}),
    ('rva23_failed&rva23_succeeded&Yanked', {0, 2}),
    ('buildsystem_cmake+buildsystem_pyproject&Yanked', {0, 2}),
    ('rva23_failed+rva23_succeeded&rva20_failed&Yanked', set()),
    ('Outdated&Advisory', {1}),
    ('Outdated=AND&Advisory=OR', {0, 1, 2}),
    ('Outdated=AND&Yanked=AND&Advisory=NOT', {0}),
    ('rva23_failed=AND&rva23_succeeded=OR&Group=AND&Yanked=AND', {0, 2}),
    ('Outdated=OR', {0, 1, 2, 3}),
    ('Outdated=NOT', {2, 3}),
])
def test_modes_use_the_requested_set_algebra(raw, expected):
    if expected is None:
        with pytest.raises(ValueError):
            query(raw)
        return
    parsed = query(raw)
    assert Evaluation(INDEX, range(4), parsed).matches == expected
    assert query(encode_parameters(parsed.parameters())) == parsed


def test_candidate_numbers_are_hypothetical_result_totals():
    selected = query('rva23_failed&Yanked')
    basic = Evaluation(INDEX, range(4), selected)
    assert basic.count(Condition(dimension='build:rva23', value='succeeded')) == 1
    assert basic.count(Condition(dimension='maintenance', value='Advisory')) == 0
    for operator, expected in [('and', 1), ('or', 1), ('not', 1)]:
        advanced = Evaluation(INDEX, range(4), query('Outdated=AND'), operator)
        assert advanced.matches == {0, 1}
        assert advanced.count(Condition(dimension='maintenance', value='Advisory')) == expected


def test_modes_are_exclusive_and_clear_on_leaving(scoped_client):
    def follow(href):
        response = scoped_client.get('/api/ui/packages?' + urlsplit(href).query)
        assert response.status_code == 200, response.text
        return response.json()
    ordinary = follow('/?rva20_failed+rva20_excluded')
    editor = ordinary['controls']['editor']
    assert not editor['advanced']['selected'] and not editor['operators'] and editor['group'] is None
    advanced = follow(editor['advanced']['href'])
    assert advanced['total'] == ordinary['total']
    assert advanced['controls']['editor']['advanced']['selected']
    plain = follow(advanced['controls']['editor']['advanced']['href'])
    assert plain['controls']['editor']['query'] == {'mode': 'basic', 'groups': [], 'tail': []}
    empty_advanced = follow(plain['controls']['editor']['advanced']['href'])
    assert empty_advanced['controls']['editor']['advanced']['selected']
    assert len(empty_advanced['controls']['editor']['operators']) == 3


def test_includes_and_pagination_preserve_modes(scoped_client):
    for raw in ('rva20_failed+rva20_excluded', 'rva20_failed=AND&rva20_excluded=OR&Group=AND'):
        parsed = PackageQuery.from_parameters(parse_qsl(raw + '&include=version&include=source&per_page=1', keep_blank_values=True))
        assert parsed.include == ['version', 'source']
        response = scoped_client.get('/api/ui/packages?' + raw + '&per_page=1').json()
        assert response['total'] == 2
        hidden = [(p['name'], p['value']) for p in response['controls']['hidden']]
        assert ListingQuery.from_parameters(hidden).filters == parsed.filters
        for link in response['pagination']:
            assert query(urlsplit(link['href']).query) == parsed.filters


@pytest.mark.parametrize('raw', [
    'Outdated&Yanked=OR', 'advanced=1&Outdated', 'Group=AND', 'Outdated=AND&Group=AND&Group=OR',
    'Outdated=XOR', 'Outdated=AND&Group=XOR', 'Outdated+Yanked',
    'rva23_failed+rva20_failed', 'filters={}', 'page=0', 'per_page=201',
    'next_logic=not', 'advanced=1&next_logic=xor', 'unknown_option=1',
    'absent/value=AND',
])
def test_invalid_http_parameters_are_rejected(scoped_client, raw):
    for route in ('/api/v2/packages', '/api/ui/packages'):
        response = scoped_client.get(route + '?' + raw)
        assert response.status_code == 422, response.text
        assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('condition', ['Advisory', 'Advisory=AND'])
def test_budget_counts_raw_repeats(scoped_client, condition):
    valid = '&'.join([condition] * MAX_QUERY_NODES)
    for route in ('/api/v2/packages', '/api/ui/packages'):
        assert scoped_client.get(route + '?' + valid).status_code == 200
        response = scoped_client.get(route + '?' + valid + '&' + condition)
        assert response.status_code == 422 and str(MAX_QUERY_NODES) in response.text


def test_openapi_documents_search_modes(scoped_client):
    spec = scoped_client.get('/openapi.json').json()
    for route in ('/api/v2/packages', '/api/ui/packages'):
        parameters = {p['name']: p for p in spec['paths'][route]['get']['parameters']}
        assert parameters['Group']['schema']['enum'] == ['AND', 'OR', 'NOT']
        assert str(MAX_QUERY_NODES) in parameters['Group']['description']
        assert 'ordinary' in parameters['advanced']['description']


def test_ordinary_sets_and_counts_match_boolean_membership():
    from tracker.presentation.query_editor import QueryEditor
    terms = tuple(Condition(dimension=dimension, value=value)
                  for dimension, values in INDEX.items() for value in values)
    for mask in product((False, True), repeat=len(terms)):
        selected = tuple(term for term, keep in zip(terms, mask) if keep)
        ordinary = FilterQuery(mode='basic', tail=selected)

        def expected(conditions, scope):
            maintenance = [term for term in conditions if term.dimension == 'maintenance']
            categories = {term.dimension for term in conditions if term.dimension != 'maintenance'}
            return {n for n in scope
                    if all(n in INDEX[term.dimension][term.value] for term in maintenance)
                    and all(any(n in INDEX[term.dimension][term.value] for term in conditions
                                if term.dimension == dimension) for dimension in categories)}

        for scope in (range(4), (0, 2), ()):
            result = Evaluation(INDEX, scope, ordinary)
            assert result.matches == expected(selected, scope)
            advanced = QueryEditor(ordinary).switch_mode().query
            assert Evaluation(INDEX, scope, advanced).matches == result.matches
            for term in terms:
                candidate = expected((*selected, term), scope)
                is_alternative = (term not in selected and term.dimension != 'maintenance'
                                  and any(c.dimension == term.dimension for c in selected))
                count = (len(candidate - result.matches) if is_alternative else
                         len(result.matches & INDEX[term.dimension][term.value]))
                assert result.count(term) == count


def test_ordinary_budget_includes_implicit_group_and_raw_repeats(scoped_client):
    flags = ['rva23_failed'] * (MAX_QUERY_NODES - 2) + ['rva23_succeeded']
    valid = '&'.join(flags)
    assert scoped_client.get('/api/ui/packages?' + valid).status_code == 200
    assert scoped_client.get('/api/ui/packages?' + valid + '&rva23_failed').status_code == 422
