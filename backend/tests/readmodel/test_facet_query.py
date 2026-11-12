"""Selection is explicit; omitted facets do not constrain the package set."""
from itertools import product
from urllib.parse import parse_qsl

import pytest
from pydantic import ValidationError

from tracker.api import ListingQuery
from tracker.readmodel.query import MAX_QUERY_NODES, Condition, Evaluation, FilterQuery, encode_parameters

INDEX = {'maintenance': {'Outdated': {0, 1}, 'Advisory': {1, 2}, 'EOL': set()},
         'build:rva23': {'failed': {0, 2}, 'succeeded': {1, 3}},
         'buildsystem': {'cmake': {0, 1}, 'meson': {2, 3}}}
ALL = frozenset(range(4))


def test_default_includes_every_package_without_selected_choices():
    query = FilterQuery()
    assert Evaluation(INDEX, ALL, query).matches == ALL
    assert not any(query.selected(d, v) for d, options in INDEX.items() for v in options)
    assert query.parameters() == []


def test_first_advisory_selection_includes_only_packages_with_advisories():
    query = FilterQuery().toggle('maintenance', 'Advisory')
    assert query.selections == {'maintenance': ('Advisory',)}
    assert query.selected('maintenance', 'Advisory')
    assert not query.selected('maintenance', 'Outdated')
    assert Evaluation(INDEX, ALL, query).matches == {1, 2}


def test_union_within_facets_intersection_between_facets_and_scope():
    query = FilterQuery(selections={'maintenance': ['Outdated', 'Advisory'],
                                  'build:rva23': ['failed']})
    result = Evaluation(INDEX, ALL, query)
    assert result.matches == {0, 2}
    assert result.count(Condition(dimension='maintenance', value='Outdated')) == 1
    assert result.count(Condition(dimension='maintenance', value='Advisory')) == 1
    assert result.count(Condition(dimension='build:rva23', value='succeeded')) == 1
    assert Evaluation(INDEX, {0, 1}, query).matches == {0}


def test_toggle_preserves_other_facets_and_last_removal_restores_no_constraint():
    query = FilterQuery(selections={'maintenance': ('Advisory',)})
    first = query.toggle('buildsystem', 'cmake')
    assert first.selections == {'maintenance': ('Advisory',), 'buildsystem': ('cmake',)}
    assert Evaluation(INDEX, ALL, first).matches == {1}
    both = first.toggle('buildsystem', 'meson')
    assert both.selections['buildsystem'] == ('cmake', 'meson')
    assert Evaluation(INDEX, ALL, both).matches == {1, 2}
    assert both.toggle('buildsystem', 'cmake').toggle('buildsystem', 'meson') == query
    cleared = query.toggle('maintenance', 'Advisory')
    assert cleared == FilterQuery()
    assert Evaluation(INDEX, ALL, cleared).matches == ALL


def test_empty_model_selections_are_normalized_but_empty_url_values_are_invalid():
    assert FilterQuery(selections={'maintenance': ()}) == FilterQuery()
    assert FilterQuery().select('maintenance', ()) == FilterQuery()
    for value in ('maintenance=', '=Advisory', 'maintenance', ''):
        with pytest.raises(ValueError, match='nonempty'):
            FilterQuery.extract([('select', value)])


def test_all_small_combinations_match_literal_set_algebra():
    choices = [None, (), ('a',), ('b',), ('a', 'b')]
    index = {'one': {'a': {0, 1}, 'b': {1, 2}}, 'two': {'a': {0, 2}, 'b': {1, 3}}}
    for first, second, scope in product(choices, choices, (ALL, frozenset({0, 2}))):
        selected = {d: v for d, v in [('one', first), ('two', second)] if v is not None}
        query = FilterQuery(selections=selected)
        expected = scope
        for dimension, values in selected.items():
            if values:
                expected &= set().union(*(index[dimension][value] for value in values))
        result = Evaluation(index, scope, query)
        assert result.matches == expected
        for dimension, options in index.items():
            eligible = scope
            for other, values in selected.items():
                if other != dimension and values:
                    eligible &= set().union(*(index[other][value] for value in values))
            for value in (*options, 'missing'):
                assert result.count(Condition(dimension=dimension, value=value)) == len(
                    eligible & options.get(value, set()))
        encoded = encode_parameters(query.parameters())
        assert FilterQuery.extract(parse_qsl(encoded, keep_blank_values=True))[0] == query


def test_budget_counts_raw_repeats_before_deduplication():
    pairs = [('select', 'maintenance=Outdated')] * MAX_QUERY_NODES
    query, remaining = FilterQuery.extract(pairs)
    assert query.selections == {'maintenance': ('Outdated',)}
    assert not remaining
    with pytest.raises(ValueError, match='exceeds'):
        FilterQuery.extract([*pairs, pairs[0]])
    with pytest.raises(ValueError, match='exceeds'):
        FilterQuery(selections={'maintenance': ['Outdated'] * (MAX_QUERY_NODES + 1)})
    query = FilterQuery(selections={'maintenance': tuple(f'issue-{i}' for i in range(MAX_QUERY_NODES))})
    with pytest.raises(ValueError, match='exceeds'):
        query.toggle('buildsystem', 'cmake')
    assert len(query.toggle('maintenance', 'issue-0').conditions) == MAX_QUERY_NODES - 1


def test_parameters_and_dimension_names_are_validated():
    for pairs in ([('unknown-option', 'value')], [('filters', '{}')]):
        with pytest.raises((ValueError, ValidationError)):
            ListingQuery.from_parameters(pairs)
    for dimension, value in [('', 'value'), ('a' * 101, 'value'), ('maintenance', 'a' * 101)]:
        with pytest.raises(ValueError):
            FilterQuery(selections={dimension: (value,)})
    with pytest.raises(ValueError, match='Unknown filter dimension'):
        Evaluation(INDEX, ALL, FilterQuery(selections={'missing': ['a']}))


def test_repeated_values_deduplicate_and_reserved_characters_roundtrip():
    query = FilterQuery(selections={'maintenance': ('Advisory', 'Advisory'),
                                   'custom': ('a&b=c', '日本語', 'x+y')})
    assert query.selections['maintenance'] == ('Advisory',)
    encoded = encode_parameters([*query.parameters(), ('q', 'a+b & c')])
    decoded, remaining = FilterQuery.extract(parse_qsl(encoded, keep_blank_values=True))
    assert decoded == query
    assert remaining == [('q', 'a+b & c')]


@pytest.mark.parametrize('values,expected', [
    (['custom'], {0}),
    (['_not_detected'], {1, 2}),
    (['cmake'], {3}),
    (['custom', '_not_detected'], {0, 1, 2}),
])
def test_declared_custom_and_fallback_remain_distinct_after_url_roundtrip(values, expected):
    index = {'buildsystem': {'custom': {0}, '_not_detected': {1, 2}, 'cmake': {3}}}
    selected = FilterQuery(selections={'buildsystem': values})
    parsed, remaining = FilterQuery.extract(parse_qsl(encode_parameters(selected.parameters())))
    assert not remaining
    assert parsed == selected
    assert Evaluation(index, ALL, parsed).matches == expected


def test_unknown_and_empty_result_values_do_not_restore_the_universe():
    for value in ('future', 'EOL'):
        query = FilterQuery().toggle('maintenance', value)
        assert query.selected('maintenance', value)
        assert Evaluation(INDEX, ALL, query).matches == set()


def test_selecting_every_known_issue_does_not_include_packages_without_issues():
    query = FilterQuery(selections={'maintenance': tuple(INDEX['maintenance'])})
    assert Evaluation(INDEX, ALL, query).matches == {0, 1, 2}
