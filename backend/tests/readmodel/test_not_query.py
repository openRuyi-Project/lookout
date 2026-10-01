"""Set subtraction agrees with independent row-by-row Boolean evaluation."""
from itertools import product

import pytest

from tracker.presentation.query_editor import QueryEditor
from tracker.readmodel.query import Condition, Evaluation, FilterQuery, Group

MODES = ('and', 'or', 'not')
TERMS = tuple(Condition(dimension='maintenance', value=value) for value in 'ABCD')
INDEX = {'maintenance': {value: {i for i, bits in enumerate(product((False, True), repeat=4)) if bits[n]}
                         for n, value in enumerate('ABCD')}}


def truth(terms):
    runs, current = [], None
    for logic, value in terms:
        if current is None:
            current = not value if logic == 'not' else value
        elif logic == 'or':
            runs.append(current)
            current = value
        else:
            current = current and (not value if logic == 'not' else value)
    return True if current is None else any([*runs, current])


def oracle(query, number):
    return truth((group.logic, truth((term.logic if i else 'and', number in INDEX['maintenance'][term.value])
                 for i, term in enumerate(group.conditions))) for group in query.groups if group.conditions)


@pytest.mark.parametrize('scope', [range(16), (0, 2, 5, 8, 11), ()])
def test_not_results_and_candidate_counts_match_boolean_oracle(scope):
    for first, inner, second, active, mode in product(MODES, MODES, MODES, range(3), MODES):
        query = FilterQuery(groups=(Group(logic=first, conditions=(TERMS[0], TERMS[1].model_copy(update={'logic': inner}))),
                                    Group(logic=second, conditions=(TERMS[2],)), Group()))
        evaluation = Evaluation(INDEX, scope, query, active, mode)
        assert evaluation.matches == {i for i in scope if oracle(query, i)}
        for term in TERMS:
            groups = list(query.groups)
            groups[active] = groups[active].append(term, mode)
            edited = FilterQuery(groups=tuple(groups))
            combined = sum(oracle(edited, i) for i in scope)
            assert evaluation.count(term) == min(combined, len(set(scope) & INDEX['maintenance'][term.value]))


def test_not_editor_mode_group_and_cancel_roundtrip():
    editor = QueryEditor(FilterQuery()).mode('not').toggle(TERMS[0])
    assert Evaluation(INDEX, range(16), editor.query).matches == set(range(16)) - INDEX['maintenance']['A']
    assert FilterQuery.extract(editor.query.parameters())[0] == editor.query
    assert editor.toggle(TERMS[0]).query == FilterQuery()
    grouped = editor.mode('and').add().toggle(TERMS[1]).mode('not').toggle(TERMS[2])
    assert grouped.query.groups[0] == editor.query.groups[0]
    assert Evaluation(INDEX, range(16), grouped.query).matches == (
        set(range(16)) - INDEX['maintenance']['A']) & INDEX['maintenance']['B'] - INDEX['maintenance']['C']
    assert grouped.clear(1).query.groups[0] == editor.query.groups[0]


def test_not_http_roundtrip_and_operator_link(scoped_client):
    params = [('AND-maintenance', 'Outdated'), ('NOT-maintenance', 'Advisory'), ('next_logic', 'not')]
    response = scoped_client.get('/api/ui/packages', params=params)
    assert response.status_code == 200
    data = response.json()
    editor = data['controls']['editor']
    assert next(choice for choice in editor['operators'] if choice['label'] == 'NOT')['selected']
    serialized = scoped_client.get('/api/ui/packages', params={'filters': FilterQuery.extract(params)[0].encode(), 'next_logic': 'not'})
    assert serialized.json() == data


def test_or_expands_positive_row_but_not_group_reverses_inclusion():
    left, right = TERMS[:2]
    for connector in ('and', 'or', 'not'):
        before = FilterQuery(groups=(Group(logic=connector, conditions=(left,)),))
        after = FilterQuery(groups=(before.groups[0].append(right, 'or'),))
        first = Evaluation(INDEX, range(16), before).matches
        second = Evaluation(INDEX, range(16), after).matches
        union = INDEX['maintenance']['A'] | INDEX['maintenance']['B']
        assert second == (set(range(16)) - union if connector == 'not' else union)
        assert second <= first if connector == 'not' else first <= second
        for mode in MODES:
            assert Evaluation(INDEX, range(16), after, next_logic=mode).matches == second
