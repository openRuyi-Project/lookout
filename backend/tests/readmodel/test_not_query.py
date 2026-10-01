"""Indexed expression evaluation agrees with independent Boolean truth tables."""
from itertools import product

import pytest

from tracker.presentation.query_editor import QueryEditor
from tracker.readmodel.query import Condition, Evaluation, FilterQuery, Group

MODES = ('and', 'or', 'not')
TERMS = tuple(Condition(dimension='maintenance', value=value) for value in 'ABCD')
INDEX = {'maintenance': {value: {i for i, bits in enumerate(product((False, True), repeat=4)) if bits[n]}
                         for n, value in enumerate('ABCD')}}


def truth(terms):
    result = True
    for logic, value in terms:
        if logic == 'and':
            result = result and value
        elif logic == 'not':
            result = result and not value
        else:
            result = result or value
    return result


def oracle(query, number):
    def term_value(term):
        return number in INDEX['maintenance'][term.value]
    operands = [(group.logic, truth((term.logic, term_value(term)) for term in group.conditions))
                for group in query.groups]
    operands.extend((term.logic, term_value(term)) for term in query.tail)
    return truth(operands)


@pytest.mark.parametrize('scope', [range(16), (0, 2, 5, 8, 11), ()])
def test_results_and_counts_match_boolean_oracle(scope):
    for outer, first, inner, pending, mode in product(MODES, repeat=5):
        query = FilterQuery(groups=(Group(logic=outer, conditions=(
            TERMS[0].model_copy(update={'logic': first}), TERMS[1].model_copy(update={'logic': inner}))),),
            tail=(TERMS[2].model_copy(update={'logic': pending}),))
        evaluation = Evaluation(INDEX, scope, query, mode)
        assert evaluation.matches == {i for i in scope if oracle(query, i)}
        for term in TERMS:
            members = set(scope) & INDEX['maintenance'][term.value]
            predicted = ((evaluation.matches | members) if mode == 'or' else
                         (evaluation.matches - members) if mode == 'not' else evaluation.matches & members)
            expected = (len(predicted) - len(evaluation.matches) if mode == 'or' else
                        len(evaluation.matches) - len(predicted) if mode == 'not' else len(predicted))
            assert evaluation.count(term) == expected




def test_multiple_sealed_operands_follow_order_with_pending_terms():
    for first, second, inner, pending, mode in product(MODES, repeat=5):
        query = FilterQuery(groups=(
            Group(logic=first, conditions=(TERMS[0], TERMS[1].model_copy(update={'logic': inner}))),
            Group(logic=second, conditions=(TERMS[2],))),
            tail=(TERMS[3].model_copy(update={'logic': pending}),))
        evaluated = Evaluation(INDEX, range(16), query, mode)
        assert evaluated.matches == {i for i in range(16) if oracle(query, i)}
        for term in TERMS:
            members = INDEX['maintenance'][term.value]
            assert evaluated.count(term) == len(members - evaluated.matches if mode == 'or'
                                                else members & evaluated.matches)

def test_leading_not_and_group_not_have_independent_scope():
    index = {'maintenance': {'Outdated': {0, 1}, 'Yanked': {1, 2}}}
    pairs = [('Outdated', 'NOT'), ('Yanked', 'AND')]
    query, _ = FilterQuery.extract(pairs)
    assert Evaluation(index, range(4), query).matches == {2}
    sealed, _ = FilterQuery.extract(pairs + [('Group', 'NOT')])
    assert Evaluation(index, range(4), sealed).matches == {0, 1, 3}
    assert sealed.groups[0].logic == sealed.groups[0].conditions[0].logic == 'not'
    assert FilterQuery.extract(sealed.parameters())[0] == sealed


def test_unsealed_tail_has_no_implicit_parentheses():
    pairs = [('A', 'AND'), ('Group', 'AND'),
             ('B', 'AND'), ('C', 'OR')]
    query, _ = FilterQuery.extract(pairs)
    a, b, c = (INDEX['maintenance'][value] for value in 'ABC')
    assert Evaluation(INDEX, range(16), query).matches == (a & b) | c
    sealed, _ = FilterQuery.extract(pairs + [('Group', 'AND')])
    assert Evaluation(INDEX, range(16), sealed).matches == a & (b | c)


@pytest.mark.parametrize('mode', MODES)
def test_addition_to_empty_tail_after_group_matches_count(mode):
    query = FilterQuery(groups=(Group(logic='not', conditions=TERMS[:2]),))
    evaluation = Evaluation(INDEX, range(16), query, mode)
    edited = QueryEditor(query, mode).toggle(TERMS[2]).query
    predicted = len(Evaluation(INDEX, range(16), edited).matches)
    expected = (predicted - len(evaluation.matches) if mode == 'or' else
                len(evaluation.matches) - predicted if mode == 'not' else predicted)
    assert evaluation.count(TERMS[2]) == expected
    assert Evaluation(INDEX, range(16), FilterQuery(), mode).count(TERMS[0]) == (0 if mode == 'or' else 8)


def test_each_expression_starts_from_all_and_preserves_first_operator():
    index = {'maintenance': {'A': {0, 1}, 'B': {1, 2}}}
    universe = {0, 1, 2, 3}
    for logic, expected in [('AND', {0, 1}), ('OR', universe), ('NOT', {2, 3})]:
        query, _ = FilterQuery.extract([('A', logic)])
        assert Evaluation(index, universe, query).matches == expected
        sealed = QueryEditor(query).group().query
        assert sealed.groups[0].conditions == query.tail
        assert Evaluation(index, universe, sealed).matches == expected
        assert FilterQuery.extract(sealed.parameters())[0] == sealed
    query, _ = FilterQuery.extract([('A', 'OR'), ('B', 'NOT')])
    assert Evaluation(index, universe, query).matches == {0, 3}


def test_or_then_and_and_or_then_not_use_the_accumulated_set():
    index = {'maintenance': {'A': {0, 1}, 'B': {1, 2}}}
    for logic, expected in [('AND', {1, 2}), ('NOT', {0, 3})]:
        query, _ = FilterQuery.extract([('A', 'OR'), ('B', logic)])
        assert Evaluation(index, range(4), query).matches == expected
    index['maintenance']['C'] = {2, 3}
    query, _ = FilterQuery.extract([('A', 'AND'), ('B', 'OR'), ('C', 'AND')])
    assert Evaluation(index, range(4), query).matches == {2}
