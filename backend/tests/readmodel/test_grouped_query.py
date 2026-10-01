"""Sealed groups, pending edits and server-generated navigation share one query."""
from urllib.parse import urlencode, urlsplit

import pytest
from pydantic import ValidationError

from tracker.readmodel.packages import PackageList
from tracker.readmodel.query import Condition, Evaluation, FilterQuery, Group, MAX_QUERY_NODES
from tracker.presentation.query_editor import QueryEditor


def condition(dimension, value):
    return Condition(dimension=dimension, value=value)


A, B, C, D = [condition('maintenance', value) for value in 'ABCD']


def expression(root, *groups):
    return FilterQuery(groups=tuple(Group(logic=root,
        conditions=tuple(c.model_copy(update={'logic': logic}) for c in terms)) for logic, terms in groups))


def test_or_counts_use_search_and_evidence_scope_without_changing_totals():
    rows = [{'name': name, 'monitors': {'m': {'dimensions': {
        'maintenance': labels, 'findings:m': ['yes'] if finding else [],
        'check:m': ['ok' if finding else 'unsupported'],
    }}}} for name, labels, finding in (
        ('pkg-a', ['A'], True), ('pkg-b', ['B'], True), ('pkg-c', ['B'], False),
        ('elsewhere', ['B'], True),
    )]
    packages = PackageList(rows, [])
    query = expression('and', ('and', (A,)))
    for findings_only in (False, True):
        params = dict(query='pkg-', monitor='m', filters=query, findings_only=findings_only)
        intersection = packages.select(**params, next_logic='and')
        union = packages.select(**params, next_logic='or')
        assert union['maintenance_labels']['B'] == (1 if findings_only else 2)
        assert intersection['maintenance_labels']['B'] == 0
        assert union['check_statuses']['unsupported'] == 1  # Checks always target coverage.
        for key in ('items', 'total', 'pages', 'result_count', 'coverage_count'):
            assert union[key] == intersection[key]
        assert union['total'] == union['counts']['all'] == union['requires_counts']['all'] == 1
    empty = packages.select(filters=FilterQuery(), next_logic='or', query='pkg-')
    assert empty['total'] == 3 and empty['maintenance_labels']['B'] == 2



def test_group_seals_only_pending_terms_and_keeps_operators_independent():
    editor = QueryEditor(FilterQuery()).mode('not').toggle(A).mode('or').toggle(B)
    before = editor.query
    sealed = editor.mode('and').group()
    assert sealed.query.groups == (Group(logic='and', conditions=before.tail),)
    assert sealed.query.tail == () and editor.query == before
    after = sealed.mode('not').toggle(C).toggle(A)
    assert after.query.groups == sealed.query.groups
    assert after.query.tail == (C.model_copy(update={'logic': 'not'}), A.model_copy(update={'logic': 'not'}))
    assert after.toggle(A).query.tail == (after.query.tail[0],)
    assert after.remove(0).query.tail == after.query.tail
    assert after.remove(0, A).query.groups[0].logic == 'and'
    assert after.remove(0, A).query.groups[0].conditions == (before.tail[1],)
    with pytest.raises(ValueError):
        sealed.group()


def test_same_architecture_has_no_implicit_exclusivity():
    pairs = [('AND-build:cpu', 'failed'), ('OR-build:cpu', 'unresolvable')]
    index = {'build:cpu': {'failed': {0}, 'unresolvable': {1}}}
    union, _ = FilterQuery.extract(pairs)
    intersection, _ = FilterQuery.extract([pairs[0], ('AND-build:cpu', 'unresolvable')])
    assert Evaluation(index, range(3), union).matches == {0, 1}
    assert Evaluation(index, range(3), intersection).matches == set()


def test_dedup_is_local_and_empty_groups_do_not_filter():
    query = FilterQuery(groups=(Group(conditions=(A, A)), Group()), tail=(A, B, A))
    assert query.groups == (Group(conditions=(A,)),) and query.tail == (A, B)
    assert Evaluation({'maintenance': {}}, range(3), FilterQuery(groups=(Group(),))).matches == {0, 1, 2}
    with pytest.raises(ValidationError):
        FilterQuery(groups=({'conditions': [A.model_dump()] * MAX_QUERY_NODES},))
    with pytest.raises(ValidationError):
        Group(groups=[])


def test_group_navigation_mode_pagination_and_clear(scoped_client):
    def follow(href):
        response = scoped_client.get('/api/ui/packages?' + urlsplit(href).query)
        assert response.status_code == 200, response.text
        return response.json()
    page = follow('/?AND-build:rva20=failed&OR-build:rva20=excluded&per_page=1&next_logic=or')
    editor = page['controls']['editor']
    switched = follow(editor['operators'][0]['href'])
    assert switched['total'] == page['total'] == 2
    assert switched['controls']['editor']['query'] == editor['query']
    for before, after in zip(page['controls']['choice_rows'], switched['controls']['choice_rows']):
        assert all(b['count'] <= a['count'] for a, b in zip(before['choices'], after['choices']))
    sealed = follow(switched['controls']['editor']['group'])
    group = sealed['controls']['editor']['groups'][0]
    assert group['closed'] and group['palette'] == 0 and group['logic'] == 'and'
    assert sealed['total'] == 2
    assert sealed['controls']['editor']['group'] is None
    assert follow(group['clear'])['controls']['editor']['query'] == {'groups': [], 'tail': []}
    assert follow(sealed['controls']['editor']['clear'])['controls']['editor']['groups'] == []


def test_palette_cycles_without_limiting_group_count(scoped_client):
    pairs = [('AND-maintenance', 'Advisory'), ('group', 'OR')] * 6
    response = scoped_client.get('/api/ui/packages?' + urlencode(pairs))
    assert response.status_code == 200
    groups = response.json()['controls']['editor']['groups']
    assert [g['palette'] for g in groups] == [0, 1, 2, 3, 0, 1]
    assert all(g['logic'] == 'or' and g['conditions'][0]['logic'] == 'and' for g in groups)


def test_budget_disables_addition_not_removal(scoped_client):
    pairs = [('AND-maintenance', str(i)) for i in range(MAX_QUERY_NODES)]
    response = scoped_client.get('/api/ui/packages?' + urlencode(pairs))
    assert response.status_code == 200
    page = response.json()
    assert page['controls']['editor']['group'] is None
    assert all(c['href'] is None for row in page['controls']['choice_rows'] for c in row['choices'] if not c['selected'])
    href = page['controls']['editor']['groups'][0]['conditions'][0]['href']
    restored = scoped_client.get('/api/ui/packages?' + urlsplit(href).query)
    assert restored.status_code == 200 and restored.json()['controls']['editor']['group']
