"""Candidate counts preserve scoped set semantics."""
from hypothesis import given, strategies as st
from tracker.readmodel.query import Condition, Evaluation, FilterQuery, Group


@given(scope=st.sets(st.integers(0, 31)), a=st.sets(st.integers(0, 31)),
       b=st.sets(st.integers(0, 31)), mode=st.sampled_from(['basic', 'advanced']),
       logic=st.sampled_from(['and', 'or', 'not']), grouped=st.booleans())
def test_count_matches_scoped_set_reference(scope, a, b, mode, logic, grouped):
    term = Condition(dimension='build:a', value='a')
    candidate = Condition(dimension='build:a', value='b')
    filters = (FilterQuery(groups=(Group(logic=logic, conditions=(term,)),))
               if mode == 'advanced' and grouped else FilterQuery(mode=mode, tail=(term,)))
    evaluation = Evaluation({'build:a': {'a': frozenset(a), 'b': frozenset(b)}}, scope, filters, logic)
    members = scope & b
    if mode == 'advanced' and logic == 'or':
        expected = len(members - evaluation.matches)
    elif mode == 'basic':
        expected = len((evaluation.alternative_scopes['build:a'] & members) - evaluation.matches)
    else:
        expected = len(evaluation.matches & members)
    assert evaluation.count(candidate) == expected
