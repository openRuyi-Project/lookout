"""Filter contexts are reusable within a request, not mutable shared state."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from itertools import product

from tracker.monitors import model as monitor_model
from tracker.readmodel.packages import PackageList, _Selection


def test_equivalent_facet_contexts_share_one_immutable_result():
    scope = frozenset(range(4))
    filters = {'buildsystem': 'cmake', 'maintenance': ''}
    index = {'buildsystem': {'cmake': frozenset({0, 2})}}
    selection = _Selection(index, scope, filters)
    selected = selection.matching()
    assert selected == frozenset({0, 2})
    assert selection.matching(without='maintenance') is selected
    assert selection.matching(without='new-monitor') is selected
    assert selection.matching(without='buildsystem') == scope
    # Empty results must also be memoized, not mistaken for a cache miss.
    empty = _Selection(index, scope, {'buildsystem': 'absent'})
    assert empty.matching() == frozenset()
    assert empty.matching() is empty.matching(without='maintenance')
    filters['buildsystem'] = 'absent'
    assert selection.matching() == selected


def rows():
    result = []
    for number, (system, status, finding, retained, requirement) in enumerate(product(
        ['cmake', 'meson'], ['ok', 'unsupported', 'error'], [False, True], [False, True], ['unmet', 'changes'],
    )):
        result.append({'name': f'package-{number:02d}', 'monitors': {'fixture': {'dimensions': {
            'buildsystem': [system], 'check:fixture': [status],
            'findings:fixture': ['yes'] if finding else [],
            'retained:fixture': ['yes'] if retained else [], 'requires': [requirement],
        }}}})
    return result


def test_coverage_results_retention_and_facets_match_independent_row_scan():
    packages = rows()
    before = deepcopy(packages)
    index = PackageList(packages, [])

    def matches(row, filters):
        dimensions = row['monitors']['fixture']['dimensions']
        for dimension, value in filters.items():
            if not value:
                continue
            values = dimensions.get(dimension, [])
            if dimension == 'check:fixture' and value in monitor_model.CHECK_GROUPS:
                if not set(values).intersection(monitor_model.CHECK_GROUPS[value]):
                    return False
            elif value not in values:
                return False
        return True

    for system, check, finding, freshness, requirement in product(
        ['', 'cmake', 'absent'], ['', 'error', 'uncovered'], [False, True], ['', 'retained'], ['', 'unmet'],
    ):
        selected = {'buildsystem': system, 'check:fixture': check,
                    'retained:fixture': 'yes' if freshness else '', 'requires': requirement}
        coverage = {key: value for key, value in selected.items() if key != 'check:fixture'}
        result = index.select(view='all', buildsystem=system, maintenance='', builds={},
                              monitor='fixture', check=check, findings_only=finding, freshness=freshness,
                              requires=requirement, page=99, per_page=7)
        assert result['coverage_count'] == sum(matches(row, coverage) for row in packages)
        assert result['result_count'] == sum(matches(row, {**coverage, 'findings:fixture': 'yes'}) for row in packages)
        for status, count in result['check_statuses'].items():
            assert count == sum(matches(row, {**coverage, 'check:fixture': status}) for row in packages)
        if finding:
            selected['findings:fixture'] = 'yes'
        expected = [row for row in packages if matches(row, selected)]
        assert result['total'] == len(expected)
        assert result['items'] == expected[(result['page'] - 1) * 7:result['page'] * 7]
        assert result['retained_count'] == sum(matches(row, {**selected, 'retained:fixture': 'yes'}) for row in packages)
        assert result['requires_counts']['all'] == sum(matches(row, {**selected, 'requires': ''}) for row in packages)
        for value, count in result['buildsystems'].items():
            assert count == sum(matches(row, {**selected, 'buildsystem': value}) for row in packages)
        for value in ('unmet', 'changes'):
            assert result['requires_counts'][value] == sum(matches(row, {**selected, 'requires': value}) for row in packages)
    assert packages == before


def test_one_index_can_serve_concurrent_selections_without_leaking_contexts():
    index = PackageList(rows(), [])

    def select(system):
        return index.select(view='all', buildsystem=system, maintenance='', builds={},
                            monitor='fixture', findings_only=True, page=1, per_page=100)

    systems = ['cmake', 'meson', 'absent', '']
    expected = {system: select(system) for system in systems}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(select, systems * 5))
    assert results == [expected[system] for system in systems * 5]


def test_empty_inventory_keeps_requested_zero_options():
    result = PackageList([], []).select(
        view='all', buildsystem='selected', maintenance='', builds={},
        monitor='fixture', check='error', page=9, per_page=20,
    )
    assert result['items'] == [] and result['page'] == result['pages'] == 1
    assert result['check_statuses'] == {'error': 0}
    assert result['buildsystems'] == {'selected': 0}
