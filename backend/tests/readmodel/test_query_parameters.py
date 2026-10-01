"""Ordered parameters preserve operators, group boundaries and repeated keys."""
from itertools import product
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from tracker.api import ListingQuery, PackageQuery
from tracker.presentation.navigation import Links
from tracker.readmodel.query import FilterQuery, MAX_QUERY_NODES


@pytest.mark.parametrize('operators', tuple(product(('AND', 'OR', 'NOT'), repeat=3)))
def test_ordered_roundtrip(operators):
    pairs = [(logic + '-maintenance', value) for logic, value in zip(operators, 'ABC')]
    for markers in ([], [(1, 'NOT')], [(0, 'OR'), (1, 'AND'), (2, 'NOT')]):
        marked = list(pairs)
        for position, logic in reversed(markers):
            marked.insert(position + 1, ('group', logic))
        query, remaining = FilterQuery.extract(marked)
        assert remaining == [] and query.parameters() == marked
        href = Links({'filters': query.model_dump()}).to()
        assert FilterQuery.extract(parse_qsl(urlsplit(href).query))[0] == query


@pytest.mark.parametrize('dimension,value', [('a=b', 'c=d'), ('build:rva23', 'failed'),
    (' 值 &=#? + <script>', ' 值 &=#? + <svg onload=alert(1)>')])
def test_url_escaping(dimension, value):
    query, _ = FilterQuery.extract([('NOT-' + dimension, value), ('group', 'AND')])
    href = Links({'filters': query.model_dump()}).to()
    assert not urlsplit(href).fragment
    assert FilterQuery.extract(parse_qsl(urlsplit(href).query))[0] == query


def test_includes_and_group_markers_preserve_order(scoped_client):
    pairs = [('include', 'version'), ('AND-build:rva20', 'failed'), ('include', 'source'),
             ('OR-build:rva20', 'excluded'), ('group', 'AND'), ('per_page', '1')]
    parsed = PackageQuery.from_parameters(pairs)
    assert parsed.include == ['version', 'source'] and parsed.per_page == 1
    raw = scoped_client.get('/api/v2/packages?' + urlencode(pairs))
    page = scoped_client.get('/api/ui/packages?' + urlencode([p for p in pairs if p[0] != 'include']))
    assert raw.status_code == page.status_code == 200
    assert raw.json()['total'] == page.json()['total'] == 2
    assert raw.json()['filters'] == page.json()['controls']['editor']['query']
    hidden = [(p['name'], p['value']) for p in page.json()['controls']['hidden']]
    assert ListingQuery.from_parameters(hidden).filters == parsed.filters
    for link in page.json()['pagination']:
        destination = scoped_client.get('/api/ui/packages?' + urlsplit(link['href']).query)
        assert destination.status_code == 200
        assert destination.json()['controls']['editor']['query'] == raw.json()['filters']


@pytest.mark.parametrize('pairs', [
    [('AND-', 'A')], [('OR-maintenance', '')], [('and-maintenance', 'A')],
    [('XOR-maintenance', 'A')], [('AND-absent', 'A')], [('group', 'AND')],
    [('AND-maintenance', 'A'), ('group', 'XOR')],
    [('AND-maintenance', 'A'), ('group', 'AND'), ('group', 'OR')],
    [('AND-maintenance', 'A' * 101)], [('AND-' + 'a' * 101, 'A')],
    [('filters', '{}')], [('page', '0')], [('per_page', '201')], [('next_logic', 'xor')],
    [('unknown_option', '1')],
])
def test_invalid_http_parameters_are_rejected(scoped_client, pairs):
    for route in ('/api/v2/packages', '/api/ui/packages'):
        response = scoped_client.get(route + '?' + urlencode(pairs))
        assert response.status_code == 422, response.text
        assert response.headers['cache-control'] == 'no-store'


def test_budget_counts_raw_repeated_conditions_and_group_markers(scoped_client):
    pairs = [('AND-maintenance', 'Advisory')] * (MAX_QUERY_NODES - 1) + [('group', 'AND')]
    assert FilterQuery.extract(pairs)[0].nodes == 2
    for route in ('/api/v2/packages', '/api/ui/packages'):
        assert scoped_client.get(route + '?' + urlencode(pairs)).status_code == 200
        response = scoped_client.get(route + '?' + urlencode(pairs + pairs[:1]))
        assert response.status_code == 422 and str(MAX_QUERY_NODES) in response.text


def test_openapi_describes_group_stream_and_scalar_options(scoped_client):
    spec = scoped_client.get('/openapi.json').json()
    for route, model in (('/api/v2/packages', PackageQuery), ('/api/ui/packages', ListingQuery)):
        operation = spec['paths'][route]['get']
        fields = {name: field for name, field in model.model_json_schema()['properties'].items() if name != 'filters'}
        parameters = {p['name']: p for p in operation['parameters']}
        assert {name: parameters[name]['schema'] for name in fields} == fields
        assert parameters['group']['schema']['enum'] == ['AND', 'OR', 'NOT']
        assert str(MAX_QUERY_NODES) in parameters['group']['description']
        assert '422' in operation['responses']
