"""Ordered URL parameters are a transport for the existing two-level query."""
from itertools import product
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest
from pydantic import ValidationError

from tracker.api import ListingQuery, PackageQuery
from tracker.presentation.navigation import Links
from tracker.readmodel.query import Evaluation, FilterQuery, Group, MAX_QUERY_NODES


def read_inline(parameters):
    query, remaining = FilterQuery.extract(parameters)
    assert remaining == []
    return query


@pytest.mark.parametrize('operators', tuple(product(('AND', 'OR'), repeat=3)))
def test_single_row_roundtrip_preserves_each_link_and_first_connector(operators):
    parameters = [(logic + '-maintenance', value) for logic, value in zip(operators, 'ABC')]
    query = read_inline(parameters)
    assert query.groups[0].logic == operators[0].lower()
    assert [(term.logic, term.value) for term in query.groups[0].conditions] == [
        (logic.lower(), value) for logic, value in zip(operators, 'ABC')]
    assert query.parameters() == parameters
    href = Links({'filters': query.model_dump()}).to()
    assert read_inline(parse_qsl(urlsplit(href).query)) == query
    assert 'filters=' not in href


def test_repeated_interleaved_keys_are_not_a_dictionary_or_an_implicit_all():
    parameters = [('AND-maintenance', 'A'), ('OR-maintenance', 'B'), ('AND-maintenance', 'C')]
    query = read_inline(parameters)
    index = {'maintenance': {'A': {0, 1, 2}, 'B': {2, 3}, 'C': {3, 4}}}
    assert Evaluation(index, range(6), query).matches == {0, 1, 2, 3}
    assert Evaluation(index, range(6), read_inline([('OR-maintenance', 'A')])).matches == {0, 1, 2}
    assert read_inline(parameters + [('OR-maintenance', 'A')]) == query


@pytest.mark.parametrize('dimension,value', [
    ('a=b', 'c=d'), ('build:rva23', 'failed'), ('checks-with-hyphens', 'A'),
    (' 值 &=#? + <script>', ' 值 &=#? + <svg onload=alert(1)>'),
])
def test_inline_keys_and_values_are_separately_url_escaped(dimension, value):
    query = read_inline([('OR-' + dimension, value)])
    href = Links({'filters': query.model_dump()}).to()
    assert urlsplit(href).fragment == ''
    assert read_inline(parse_qsl(urlsplit(href).query)) == query


def test_multirow_and_empty_editor_rows_use_json_without_losing_positions():
    one = read_inline([('AND-maintenance', 'A')]).groups[0]
    for groups in ((one, one.model_copy(update={'logic': 'or'})), (one, Group()), (Group(),)):
        query = FilterQuery(groups=groups)
        parameters = query.parameters()
        assert [key for key, value in parameters] == ['filters']
        assert read_inline(parameters) == query
    assert FilterQuery().parameters() == []


def test_request_adapter_preserves_include_lists_and_rejects_unknown_fields():
    parameters = [('include', 'version'), ('AND-maintenance', 'A'), ('include', 'source'),
                  ('OR-maintenance', 'B'), ('AND-maintenance', 'C'), ('per_page', '2')]
    parsed = PackageQuery.from_parameters(parameters)
    assert parsed.include == ['version', 'source'] and parsed.per_page == 2
    assert parsed.filters == read_inline([item for item in parameters if item[0].endswith('-maintenance')])
    with pytest.raises(ValidationError, match='Extra inputs'):
        PackageQuery.from_parameters(parameters + [('per_pgae', '5')])


@pytest.mark.parametrize('route', ['/api/v2/packages', '/api/ui/packages'])
def test_inline_json_results_counts_and_normalized_response_are_identical(scoped_client, route):
    pairs = [('OR-build:rva20', 'failed'), ('OR-build:rva20', 'excluded'), ('AND-buildsystem', 'meson')]
    query = read_inline(pairs)
    for mode in ('and', 'or'):
        scope = [('next_logic', mode), ('per_page', '1')]
        inline = scoped_client.get(route + '?' + urlencode(scope + pairs))
        serialized = scoped_client.get(route, params={'next_logic': mode, 'per_page': '1', 'filters': query.encode()})
        assert inline.status_code == serialized.status_code == 200
        assert inline.json() == serialized.json()
        body = inline.json()
        assert body['total'] == 2
        if route == '/api/v2/packages':
            assert body['filters'] == query.model_dump(mode='json')
        else:
            assert body['controls']['editor']['query'] == query.model_dump(mode='json')
            hidden = [(item['name'], item['value']) for item in body['controls']['hidden']]
            assert ListingQuery.from_parameters(hidden).filters == query
            for choice in body['pagination']:
                destination = scoped_client.get('/api/ui/packages?' + urlsplit(choice['href']).query)
                assert destination.status_code == 200, destination.text
                assert destination.json()['controls']['editor']['query'] == query.model_dump(mode='json')


@pytest.mark.parametrize('parameters', [
    [('AND-', 'A')], [('OR-maintenance', '')], [('and-maintenance', 'A')],
    [('XOR-maintenance', 'A')], [('AND-absent', 'A')],
    [('AND-maintenance', 'A' * 101)], [('AND-' + 'a' * 101, 'A')],
    [('AND-maintenance', 'A'), ('filters', '{}')],
    [('filters', '{}'), ('filters', '{}')], [('page', '0')],
    [('per_page', '201')], [('next_logic', 'xor')], [('active_group', '1')],
])
def test_invalid_parameters_are_explicit_422_without_truncation(scoped_client, parameters):
    for route in ('/api/v2/packages', '/api/ui/packages'):
        response = scoped_client.get(route + '?' + urlencode(parameters))
        assert response.status_code == 422, response.text
        assert response.headers['cache-control'] == 'no-store'


def test_inline_budget_counts_raw_repetitions_before_deduplication(scoped_client):
    at_limit = [('AND-maintenance', 'DepMismatch')] * (MAX_QUERY_NODES - 1)
    assert read_inline(at_limit).nodes == 2
    for route in ('/api/v2/packages', '/api/ui/packages'):
        assert scoped_client.get(route + '?' + urlencode(at_limit)).status_code == 200
        rejected = scoped_client.get(route + '?' + urlencode(at_limit + at_limit[:1]))
        assert rejected.status_code == 422 and str(MAX_QUERY_NODES) in rejected.text


def test_openapi_query_fields_are_the_validated_models_not_a_second_manual_list(scoped_client):
    spec = scoped_client.get('/openapi.json').json()
    for route, model in (('/api/v2/packages', PackageQuery), ('/api/ui/packages', ListingQuery)):
        operation = spec['paths'][route]['get']
        fields = model.model_json_schema()['properties']
        assert {p['name']: p['schema'] for p in operation['parameters']} == fields
        assert '422' in operation['responses']
        assert 'AND-' in fields['filters']['description'] and 'OR-' in fields['filters']['description']
