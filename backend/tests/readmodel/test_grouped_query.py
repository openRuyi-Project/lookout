"""The independent oracle scans rows; production combines indexed package sets."""
import json
from itertools import product
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from tracker.readmodel.packages import PackageList
from tracker.readmodel.query import Condition, Evaluation, FilterQuery, Group, MAX_QUERY_NODES
from tracker.presentation.query_editor import QueryEditor
from tracker.presentation.navigation import Links
from tests.helpers.query import filter_in


def condition(dimension, value):
    return Condition(dimension=dimension, value=value)


A, B, C, D = [condition('maintenance', value) for value in ('A', 'B', 'C', 'D')]


def expression(root, *groups):
    return FilterQuery(groups=tuple(Group(logic=root,
        conditions=tuple(c.model_copy(update={'logic': logic}) for c in terms)) for logic, terms in groups))


def boolean_chain(operands):
    blocks = [[]]
    for logic, value in operands:
        if logic == 'or' and blocks[-1]:
            blocks.append([])
        blocks[-1].append(value)
    return any(all(block) for block in blocks)


def oracle(query, facts):
    return boolean_chain((g.logic, boolean_chain(
        (c.logic, c.value in facts.get(c.dimension, ())) for c in g.conditions))
        for g in query.groups if g.conditions)


def test_both_mixed_examples_and_same_architecture_zero():
    failed = condition('build:cpu', 'failed')
    unresolved = condition('build:cpu', 'unresolvable')
    rows = [{'name': name, 'monitors': {'fixture': {'dimensions': {
        'build:cpu': [status], 'maintenance': labels}}}} for name, status, labels in [
            ('a', 'failed', ['A', 'B']), ('b', 'unresolvable', ['C', 'D']),
            ('c', 'unresolvable', ['A', 'B']), ('d', 'succeeded', ['A', 'B'])]]
    index = PackageList(rows, [{'id': 'cpu'}])
    first = expression('and', ('or', (failed, unresolved)), ('and', (A, B)))
    second = expression('or', ('and', (failed, A)), ('and', (unresolved, D)))
    select = lambda q: [r['name'] for r in index.select(filters=q)['items']]
    assert select(first) == ['a', 'c']
    assert select(second) == ['a', 'b']
    assert select(expression('and', ('and', (failed, unresolved)))) == []
    assert select(expression('or', ('or', (failed, unresolved)))) == ['a', 'b', 'c']


def test_result_totals_and_candidate_counts_against_row_oracle():
    rows = [{'name': str(n), 'monitors': {'m': {'dimensions': {'maintenance': [
        c.value for c, present in zip((A, B, C, D), bits) if present]}}}}
        for n, bits in enumerate(product((False, True), repeat=4))]
    index = PackageList(rows, [])
    for root, left, right, active, mode in product(('and', 'or'), ('and', 'or'), ('and', 'or'), (0, 1), ('and', 'or')):
        query = expression(root, (left, (A, B)), (right, (B, C)))
        evaluation = Evaluation(index.index, index.all, query, active, mode)
        expected = {i for i, row in enumerate(rows) if oracle(query, row['monitors']['m']['dimensions'])}
        assert evaluation.matches == expected
        result = index.select(filters=query, active_group=active, next_logic=mode, per_page=3)
        assert result['total'] == len(expected)
        assert [r['name'] for r in result['items']] == [rows[i]['name'] for i in sorted(expected)[:3]]
        for term in (A, B, C, D):
            groups = list(query.groups)
            groups[active] = groups[active].append(term, mode)
            candidate = FilterQuery(groups=tuple(groups))
            count = sum(oracle(candidate, row['monitors']['m']['dimensions']) for row in rows)
            count = min(count, sum(term.value in row['monitors']['m']['dimensions']['maintenance'] for row in rows))
            assert evaluation.count(term) == count == result['maintenance_labels'][term.value]


def test_empty_groups_dedup_and_or_package_uniqueness():
    index = {'maintenance': {'A': {0, 1}, 'B': {1, 2}}}
    for root in ('and', 'or'):
        assert Evaluation(index, range(4), expression(root, ('or', ()), ('and', ()))).matches == set(range(4))
        query = expression(root, ('or', (A, A, B)), ('or', ()))
        assert [c.identity for c in query.groups[0].conditions] == [A.identity, B.identity]
        assert Evaluation(index, range(4), query).matches == {0, 1, 2}
    query = expression('or', ('and', (A,)), ('and', (A,)))
    assert query.groups[0] == query.groups[1]
    assert query.groups[0].conditions[0].identity == A.identity
    assert Evaluation(index, range(4), query).matches == {0, 1}
    assert Evaluation(index, range(4), query, 1).count(B) == 2


def test_editor_isolation_mode_add_toggle_clear():
    editor = QueryEditor(expression('and', ('or', (A, B)), ('and', (B, C))), 1)
    removed = editor.toggle(B)
    assert removed.query.groups[0] == editor.query.groups[0]
    assert removed.current.conditions == (C,)
    assert removed.toggle(B).current.conditions == (C, B)
    changed = editor.mode('or')
    assert changed.query == editor.query
    assert changed.next_logic == 'or'
    added = changed.add()
    assert added.active == 2 and added.current.logic == 'or' and added.current.conditions == ()
    assert added.toggle(A).query.groups[0] == editor.query.groups[0]
    assert added.mode('and').toggle(A).current.logic == 'and'
    assert added.select(0).next_logic == 'or'
    inserted = changed.select(0).add()
    assert inserted.active == 1 and inserted.current.logic == 'or'
    assert inserted.query.groups[0] == editor.query.groups[0]
    assert inserted.query.groups[2] == editor.query.groups[1]
    cleared = inserted.clear(0)
    assert cleared.active == 0 and cleared.current.conditions == ()
    assert cleared.query.groups[1:] == inserted.query.groups[1:]
    sole = QueryEditor(expression('or', ('or', (A,))))
    assert sole.toggle(A).query.groups == ()
    assert sole.clear(0).query.groups == ()
    assert sole.clear(0).active == 0
    emptied = editor.clear(0).clear(1)
    assert emptied.query.groups == () and emptied.active == 0
    last = editor.clear(0).select(1).toggle(B).toggle(C)
    assert last.query.groups == () and last.active == 0
    for invalid in (-1, 2):
        with pytest.raises(ValueError, match='Group does not exist'):
            editor.clear(invalid)
    with pytest.raises(ValueError, match='Active group'):
        editor.select(2)


def test_url_roundtrip_preserves_query_not_editor_identity():
    query = expression('or', ('and', (A,)), ('or', (B, C)))
    links = Links({'filters': query.model_dump(), 'active_group': 1, 'next_logic': 'or', 'q': 'with spaces & symbols', 'per_page': 3})
    params = parse_qs(urlsplit(links.to(page=2)).query)
    assert FilterQuery.decode(params['filters'][0]) == query
    assert set(json.loads(params['filters'][0])) == {'groups'}
    assert params['next_logic'] == ['or']
    assert params['active_group'] == ['1']
    assert params['q'] == ['with spaces & symbols']
    assert params['per_page'] == ['3'] and params['page'] == ['2']
    changed = parse_qs(urlsplit(links.condition('maintenance', 'B')).query)
    result = FilterQuery.decode(changed['filters'][0])
    assert result.groups[0] == query.groups[0] and tuple(c.identity for c in result.groups[1].conditions) == (C.identity,)
    assert links.to(monitor='build').find('filters=') > 0


def test_one_node_limit_counts_groups_and_raw_duplicates():
    # More than six groups and eight conditions is legal; only the total matters.
    query = expression('and', *(('or', tuple(condition('maintenance', str(i)) for i in range(9))) for _ in range(7)))
    assert query.nodes == 70
    assert len(FilterQuery(groups=(Group(),) * MAX_QUERY_NODES).groups) == MAX_QUERY_NODES
    with pytest.raises(ValidationError, match='nodes'):
        FilterQuery(groups=(Group(),) * (MAX_QUERY_NODES + 1))
    data = {'groups': [{'conditions': [A.model_dump()] * (MAX_QUERY_NODES - 1)}]}
    assert FilterQuery.model_validate(data).nodes == 2
    data['groups'][0]['conditions'].append(A.model_dump())
    with pytest.raises(ValidationError, match='nodes'):
        FilterQuery.model_validate(data)
    at_limit = QueryEditor(expression('and', ('and', tuple(condition('maintenance', str(i)) for i in range(MAX_QUERY_NODES - 1)))))
    assert at_limit.query.nodes == MAX_QUERY_NODES
    with pytest.raises(ValidationError, match='nodes'):
        at_limit.toggle(A)


@pytest.mark.parametrize('raw', [
    {'logic': 'or'}, {'groups': [{'logic': 'xor'}]}, {'groups': [{'groups': []}]},
    {'groups': [{'conditions': [{'logic': 'or', 'conditions': []}]}]},
    {'groups': [{'conditions': [{'dimension': 'maintenance', 'value': 'A', 'not': True}]}]},
    {'groups': [{'conditions': [{'dimension': 'maintenance', 'value': ''}]}]},
    {'active_group': 1},
])
def test_no_recursive_language_or_extra_semantics(raw):
    with pytest.raises(ValidationError):
        FilterQuery.model_validate(raw)


def test_api_and_document_share_query_limit_results_and_candidate_links(scoped_client):
    failed, excluded = [condition('build:rva20', v) for v in ('failed', 'excluded')]
    query = expression('and', ('or', (failed, excluded)))
    params = {'filters': query.encode()}
    raw = scoped_client.get('/api/v2/packages', params=params)
    assert raw.status_code == 200, raw.text
    doc = scoped_client.get('/api/ui/packages', params=params)
    assert doc.status_code == 200, doc.text
    body, page = raw.json(), doc.json()
    assert body['total'] == page['total'] == 2
    assert [r['name'] for r in body['items']] == [r['key'] for r in page['table']['rows']]
    assert body['filters'] == page['controls']['editor']['query'] == query.model_dump(mode='json')
    assert body['query_node_limit'] == page['controls']['editor']['node_limit'] == MAX_QUERY_NODES
    choices = [choice for nav in page['controls']['choice_rows'] for choice in nav['choices']]
    for choice in choices:
        dest = scoped_client.get('/api/ui/packages?' + urlsplit(choice['href']).query)
        assert dest.status_code == 200, dest.text
        edited = filter_in(choice['href'])
        changed = set(c.identity for c in query.groups[0].conditions) ^ set(
            c.identity for g in edited.groups for c in g.conditions)
        dimension, value = changed.pop()
        alone = scoped_client.get('/api/ui/packages', params={'filters': f'{dimension}={value}'})
        assert alone.status_code == 200, alone.text
        assert choice['count'] == min(page['total'] if choice['selected'] else dest.json()['total'], alone.json()['total'])
    for route in ('/api/v2/packages', '/api/ui/packages'):
        too_big = json.dumps({'groups': [{}] * (MAX_QUERY_NODES + 1)})
        rejected = scoped_client.get(route, params={'filters': too_big})
        assert rejected.status_code == 422 and str(MAX_QUERY_NODES) in rejected.text
        assert scoped_client.get(route, params={'filters': json.dumps({'groups': [{'conditions': [condition('absent', 'A').model_dump()]}]})}).status_code == 422
        assert scoped_client.get(route, params={'active_group': 1}).status_code == 422
        assert scoped_client.get(route, params={'next_logic': 'xor'}).status_code == 422
        assert scoped_client.get(route, params={'filters': '{broken'}).status_code == 422
        assert scoped_client.get(route, params={'build': 'rva20:failed'}).status_code == 422


def test_search_pagination_and_concurrent_edits_do_not_mutate_index():
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy
    rows = [{'name': f'pkg-{i}', 'monitors': {'m': {'dimensions': {
        'maintenance': ['A'] if i % 2 else ['B'],
        'buildsystem': ['one' if i % 3 else 'two']}}}} for i in range(20)]
    before = deepcopy(rows)
    index = PackageList(rows, [])
    queries = [expression(root, ('or', (A, B)), ('and', (A,))) for root in ('and', 'or')]
    def select(query):
        return index.select(filters=query, query='pkg-1', per_page=2, page=2)
    expected = [select(query) for query in queries]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(select, queries * 5)) == expected * 5
    assert rows == before
    assert all(r['name'].startswith('pkg-1') for result in expected for r in result['items'])
    assert index.select(filters=expression('and', ('and', (A, B))), page=999)['page'] == 1
    empty = PackageList([], []).select(filters=expression('or', ('and', (A,))))
    assert empty['items'] == [] and empty['total'] == 0
    assert empty['maintenance_labels']['A'] == 0


def test_group_switches_do_not_change_results_and_links_preserve_all_groups(scoped_client):
    query = expression('or', ('and', (condition('maintenance', 'Signature'),)),
                       ('or', (condition('build:rva20', 'failed'), condition('buildsystem', 'cmake'))))
    pages = [scoped_client.get('/api/ui/packages', params={'filters': query.encode(), 'active_group': i,
        'per_page': 1}).json() for i in range(2)]
    assert [r['key'] for r in pages[0]['table']['rows']] == [r['key'] for r in pages[1]['table']['rows']]
    assert pages[0]['total'] == pages[1]['total']
    raw = [scoped_client.get('/api/v2/packages', params={'filters': query.encode(), 'active_group': i}).json() for i in range(2)]
    assert raw[0]['items'] == raw[1]['items']
    for page in pages:
        assert sum(g['active'] for g in page['controls']['editor']['groups']) == 1
        for choice in page['pagination'] + page['navigation']['choices']:
            params = parse_qs(urlsplit(choice['href']).query)
            assert FilterQuery.decode(params['filters'][0]) == query
        hidden = {p['name']: p['value'] for p in page['controls']['hidden']}
        assert FilterQuery.decode(hidden['filters']) == query
        active = page['controls']['editor']['active_group']
        other = 1 - active
        groups = page['controls']['editor']['groups']
        selected = scoped_client.get('/api/ui/packages?' + urlsplit(groups[other]['conditions'][0]['href']).query).json()
        assert selected['controls']['editor']['query'] == page['controls']['editor']['query']
        assert selected['controls']['editor']['active_group'] == other


def test_budget_disables_only_additions_and_still_allows_clearing(scoped_client):
    query = FilterQuery(groups=(Group(conditions=(condition('build:rva20', 'failed'),)),
                              *(Group() for _ in range(MAX_QUERY_NODES - 2))))
    response = scoped_client.get('/api/ui/packages', params={'filters': query.encode()})
    assert response.status_code == 200, response.text
    page = response.json()
    editor = page['controls']['editor']
    assert all(g['add'] is None for g in editor['groups'])
    assert editor['node_limit'] == MAX_QUERY_NODES
    assert all(c['href'] is None for nav in page['controls']['choice_rows'] for c in nav['choices'] if not c['selected'])
    cleared = scoped_client.get('/api/ui/packages?' + urlsplit(editor['groups'][0]['clear']).query)
    assert cleared.status_code == 200
    cleared_editor = cleared.json()['controls']['editor']
    assert cleared_editor['groups'] == [] and cleared_editor['active_group'] == 0
    assert all(c['href'] for nav in cleared.json()['controls']['choice_rows'] for c in nav['choices'])
    reset = scoped_client.get('/api/ui/packages?' + urlsplit(editor['clear']).query).json()
    assert reset['controls']['editor']['groups'] == []


def test_first_selection_creates_row_and_last_removal_hides_it(scoped_client):
    def follow(href):
        response = scoped_client.get('/api/ui/packages?' + urlsplit(href).query)
        assert response.status_code == 200, response.text
        return response.json()

    initial = scoped_client.get('/api/ui/packages').json()
    assert initial['controls']['editor']['groups'] == []
    choice = next(c for nav in initial['controls']['choice_rows'] for c in nav['choices'] if c['count'])
    selected = follow(choice['href'])
    group, = selected['controls']['editor']['groups']
    assert len(group['conditions']) == 1 and group['active']
    for href in (group['clear'], group['conditions'][0]['href']):
        restored = follow(href)
        assert restored['controls']['editor']['groups'] == []
        assert restored['total'] == initial['total']

    two = follow(group['add'])['controls']['editor']
    assert two['active_group'] == 1
    assert len(two['groups']) == 2 and two['groups'][1]['conditions'] == []
    inserted = follow(two['groups'][0]['add'])['controls']['editor']
    assert inserted['active_group'] == 1 and len(inserted['groups']) == 3
    assert inserted['query']['groups'][2] == two['query']['groups'][1]
    cleared = follow(inserted['groups'][0]['clear'])['controls']['editor']
    assert cleared['active_group'] == 0
    assert cleared['query']['groups'] == []


def test_next_operator_preserves_history_and_uses_boolean_precedence():
    editor = QueryEditor(FilterQuery()).toggle(A)
    unchanged = editor.mode('or')
    assert unchanged.query == editor.query
    editor = unchanged.toggle(B).mode('and').toggle(C)
    index = {'maintenance': {'A': {0}, 'B': {1, 2}, 'C': {2}}}
    assert Evaluation(index, range(4), editor.query).matches == {0, 2}
    # Switching to OR, including at the empty start, never introduces ALL OR x.
    assert Evaluation(index, range(4), QueryEditor(FilterQuery()).mode('or').toggle(A).query).matches == {0}
    removed = editor.mode('or').toggle(A)
    assert not removed.current.contains(A)
    assert removed.current.logic == editor.current.logic
    assert editor.query.groups[0].conditions[0] == A


def test_mixed_connectors_at_both_levels_and_count_all_append_contexts():
    rows = [{'name': str(n), 'monitors': {'m': {'dimensions': {'maintenance': [
        c.value for c, present in zip((A, B, C, D), bits) if present]}}}}
        for n, bits in enumerate(product((False, True), repeat=4))]
    index = PackageList(rows, [])
    for ab, bc, outer_b, outer_c, active, mode in product(*[('and', 'or')] * 4, range(4), ('and', 'or')):
        query = FilterQuery(groups=(
            Group(conditions=(A, B.model_copy(update={'logic': ab}), C.model_copy(update={'logic': bc}))),
            Group(logic=outer_b, conditions=(B,)), Group(logic=outer_c, conditions=(D,)), Group()))
        evaluation = Evaluation(index.index, index.all, query, active, mode)
        assert evaluation.matches == {i for i, row in enumerate(rows) if oracle(query, row['monitors']['m']['dimensions'])}
        for term in (A, B, C, D):
            groups = list(query.groups)
            groups[active] = groups[active].append(term, mode)
            candidate = FilterQuery(groups=tuple(groups))
            count = sum(oracle(candidate, row['monitors']['m']['dimensions']) for row in rows)
            count = min(count, sum(term.value in row['monitors']['m']['dimensions']['maintenance'] for row in rows))
            assert evaluation.count(term) == count


def test_counts_cap_candidates_not_results_or_boolean_membership():
    index = {'maintenance': {'A': {0, 1, 2}, 'B': {2, 3}, 'C': {0, 2, 4}, 'D': {4, 5}}}
    query = expression('and', ('and', (A,)), ('and', (C,)))
    selected = Evaluation(index, range(6), query, next_logic='or')
    assert selected.matches == {0, 2}
    assert selected.count(D) == 2  # min(3 combined, 2 own), not their intersection of 1.
    assert selected.count(A) == 2  # min(2 combined, 3 own).
    assert selected.count(condition('maintenance', 'absent')) == 0
    assert Evaluation(index, range(6), query, next_logic='and').count(B) == 1
    assert Evaluation(index, range(6), query, active=1, next_logic='or').count(D) == 2
    union = expression('and', ('or', (A, B)))
    for mode in ('and', 'or'):
        evaluated = Evaluation(index, range(6), union, next_logic=mode)
        assert evaluated.matches == {0, 1, 2, 3}
        assert evaluated.count(B) == 2
        assert evaluated.count(condition('maintenance', 'absent')) == 0


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


def test_mode_links_only_change_edit_state_and_counts(scoped_client):
    query = expression('and', ('and', (condition('build:rva20', 'failed'),)))
    page = scoped_client.get('/api/ui/packages', params={'filters': query.encode()}).json()
    editor = page['controls']['editor']
    assert [option['label'] for option in editor['operators']] == ['AND', 'OR', 'NOT']
    switched = scoped_client.get('/api/ui/packages?' + urlsplit(editor['operators'][1]['href']).query).json()
    assert switched['controls']['editor']['query'] == editor['query']
    assert switched['total'] == page['total']
    assert switched['controls']['editor']['operators'][1]['selected']
    for nav in switched['controls']['choice_rows']:
        for choice in nav['choices']:
            if not choice['selected']:
                destination = scoped_client.get('/api/ui/packages?' + urlsplit(choice['href']).query)
                assert destination.status_code == 200, destination.text
                params = parse_qs(urlsplit(choice['href']).query)
                candidate = filter_in(choice['href']).groups[0].conditions[-1]
                params = {key: value for key, value in params.items() if not key.startswith(('AND-', 'OR-')) and key != 'filters'}
                params['filters'] = expression('and', ('and', (candidate,))).encode()
                standalone = scoped_client.get('/api/ui/packages', params=params)
                assert standalone.status_code == 200, standalone.text
                assert choice['count'] == min(destination.json()['total'], standalone.json()['total'])


@pytest.mark.parametrize('scope', [range(16), (0, 1, 4, 11), ()])
def test_and_counts_never_exceed_or_or_standalone_counts(scope):
    index = {'maintenance': {term.value: {i for i, bits in enumerate(product((False, True), repeat=4))
        if bits[n]} for n, term in enumerate((A, B, C, D))}}
    for root, left, right, active in product(('and', 'or'), ('and', 'or'), ('and', 'or'), range(3)):
        query = expression(root, (left, (A, B)), (right, (B, C)), ('and', ()))
        intersection = Evaluation(index, scope, query, active, 'and')
        union = Evaluation(index, scope, query, active, 'or')
        assert intersection.matches == union.matches
        for term in (A, B, C, D, condition('maintenance', 'absent')):
            own = len(set(scope) & index['maintenance'].get(term.value, set()))
            assert 0 <= intersection.count(term) <= union.count(term) <= own


def test_switch_from_two_or_choices_to_and_preserves_results_and_bounded_counts(scoped_client):
    query = expression('and', ('or', (condition('build:rva20', 'failed'), condition('build:rva20', 'excluded'))))
    union = scoped_client.get('/api/ui/packages', params={'filters': query.encode(), 'next_logic': 'or'}).json()
    switched = scoped_client.get('/api/ui/packages?' + urlsplit(union['controls']['editor']['operators'][0]['href']).query).json()
    assert switched['total'] == union['total'] == 2
    assert [r['key'] for r in switched['table']['rows']] == [r['key'] for r in union['table']['rows']]
    assert switched['controls']['editor']['query'] == union['controls']['editor']['query']
    assert switched['controls']['editor']['operators'][0]['selected']
    for before, after in zip(union['controls']['choice_rows'], switched['controls']['choice_rows']):
        for left, right in zip(before['choices'], after['choices']):
            assert right['label'] == left['label']
            assert right['count'] <= left['count']
    build = next(n for n in switched['controls']['choice_rows'] if n['label'] == 'rva20')
    assert {c['label']: c['count'] for c in build['choices'] if c['selected']} == {'Failed': 1, 'Excluded': 1}


@pytest.mark.parametrize('dimension,value', [
    ('maintenance', 'DepMismatch'), ('build:rva23', 'failed'),
    ('check:security', 'failed'), ('maintenance', '值 &=#? + <script>'),
    ('a=b', 'c=d'), (' {dimension', 'value'),
])
def test_single_condition_codec_and_url_roundtrip(dimension, value):
    query = expression('and', ('and', (condition(dimension, value),)))
    encoded = query.encode()
    assert FilterQuery.decode(encoded) == query
    href = Links({'filters': query.model_dump()}).to()
    params = parse_qs(urlsplit(href).query)
    assert set(params) == {'AND-' + dimension}
    assert filter_in(href) == query
    assert not urlsplit(href).fragment


def test_simple_query_links_omit_defaults_and_preserve_nondefault_scope(scoped_client):
    page = scoped_client.get('/api/ui/packages', params={
        'page': 1, 'per_page': 100, 'next_logic': 'and', 'active_group': 0, 'section': 'results',
    }).json()
    mismatch = next(c for n in page['controls']['choice_rows'] for c in n['choices'] if c['label'] == 'DepMismatch')
    assert mismatch['href'] == '/?AND-maintenance=DepMismatch'
    scoped = scoped_client.get('/api/ui/packages', params={'monitor': 'build', 'section': 'coverage', 'per_page': 2, 'q': 'foo'}).json()
    choice = next(c for n in scoped['controls']['navigation'] for c in n['choices'] if c['label'] == 'CheckFailed')
    params = parse_qs(urlsplit(choice['href']).query)
    assert {k: params[k] for k in ('monitor', 'section', 'per_page', 'q')} == {
        'monitor': ['build'], 'section': ['coverage'], 'per_page': ['2'], 'q': ['foo']}
    assert 'page' not in params and 'active_group' not in params and 'next_logic' not in params


@pytest.mark.parametrize('wire', ['maintenance=DepMismatch', 'build:rva20=failed', 'check:requires=failed'])
def test_shorthand_and_json_share_api_results_and_normalized_query(scoped_client, wire):
    query = FilterQuery.decode(wire)
    for route in ('/api/v2/packages', '/api/ui/packages'):
        simple = scoped_client.get(route, params={'filters': wire})
        native = scoped_client.get(route, params={'filters': query.model_dump_json()})
        assert simple.status_code == native.status_code == 200
        assert simple.json() == native.json()
        normalized = simple.json()['filters'] if route == '/api/v2/packages' else simple.json()['controls']['editor']['query']
        assert normalized == query.model_dump(mode='json')


@pytest.mark.parametrize('wire', ['', 'maintenance', '=Advisory', 'maintenance=', 'a' * 101 + '=A', 'maintenance=' + 'a' * 101])
def test_shorthand_is_validated_and_rejected_without_truncation(scoped_client, wire):
    for route in ('/api/v2/packages', '/api/ui/packages'):
        response = scoped_client.get(route, params={'filters': wire})
        assert response.status_code == 422


def test_complex_wire_omits_defaults_without_losing_connectors():
    query = expression('and', ('and', (A,)), ('or', (B, C)))
    assert FilterQuery.decode(query.encode()) == query
    assert len(query.encode()) < len(query.model_dump_json())
    assert '"logic":"and"' not in query.encode()
    assert '"logic":"or"' in query.encode()


def test_openapi_describes_filter_text_not_json_only(scoped_client):
    spec = scoped_client.get('/openapi.json').json()
    for route in ('/api/v2/packages', '/api/ui/packages'):
        parameter = next(p for p in spec['paths'][route]['get']['parameters'] if p['name'] == 'filters')
        assert parameter['schema']['type'] == 'string'
        assert 'contentMediaType' not in parameter['schema']
        assert 'dimension=value' in parameter['description']
