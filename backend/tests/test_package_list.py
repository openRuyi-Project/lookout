from itertools import product

import pytest
from fastapi.testclient import TestClient

from tracker import build_status, state, view
from tracker.api import create_app
from tracker.package_list import PackageList


def test_issue_semantics_do_not_depend_on_display_color():
    assert {code for code, status in build_status.STATES.items() if status.issue} == {
        'failed', 'unresolvable', 'broken', 'blocked',
    }
    assert build_status.describe('blocked').kind == 'working'
    assert not build_status.describe('new-obs-status').issue


def test_blocked_is_an_issue_once_per_source_package(snapshot, tmp_path):
    snapshot['builds']['binutils']['rva23']['raw_status'] = 'blocked'
    snapshot['builds']['binutils']['rva20']['raw_status'] = 'blocked'
    snapshot['builds']['foo4']['x86_64']['raw_status'] = 'scheduled'
    db = tmp_path / 'snapshot.db'
    state.commit(db, snapshot)
    client = TestClient(create_app(db))
    result = client.get('/api/v1/packages?view=problems').json()
    assert result['total'] == result['counts']['problems'] == 2
    assert {row['name'] for row in result['items']} == {'binutils', 'foo3'}
    result = client.get('/api/v1/packages', params=[('build', 'rva23:issues'), ('build', 'rva20:blocked')]).json()
    assert result['total'] == 1
    assert result['items'][0]['name'] == 'binutils'
    assert result['items'][0]['builds'][0]['issue'] is True
    assert len(result['build_statuses']) == 3
    for query in ['build=absent:failed', 'build=rva23:failed&build=rva23:blocked', 'build=failed']:
        assert client.get('/api/v1/packages?' + query).status_code == 422


def test_all_facets_match_an_independent_row_scan(snapshot):
    rows, _ = view.project_monitors(snapshot)
    oracle = [view.legacy_package(row) for row in rows]
    for index, (row, expected) in enumerate(zip(rows, oracle)):
        system = ['cmake', 'meson', None][index % 3]
        labels = ['Security', 'EOL'] if index % 2 else ['Security']
        expected['buildsystem'] = system
        expected['maintenance'] = [{'label': label} for label in labels]
        row['monitors']['source']['dimensions']['buildsystem'] = [system or '_not_detected']
        row['monitors']['fixture'] = {'id': 'fixture', 'dimensions': {'maintenance': labels}}
        for offset, build in enumerate(expected['builds']):
            build['raw_status'] = ['blocked', 'failed', 'building', 'succeeded', 'disabled'][(index + offset) % 5]
            build['issue'] = build['raw_status'] in {'blocked', 'failed'}
            row['monitors']['build']['dimensions']['build:' + build['target']] = [build['raw_status']] + (['issues'] if build['issue'] else [])
    index = PackageList(rows, snapshot['targets'])

    def matches(row, system, maintenance, first, second):
        return ((not system or (row['buildsystem'] or '_not_detected') == system)
                and (not maintenance or any(f['label'] == maintenance for f in row['maintenance']))
                and (not first or row['builds'][0]['raw_status'] == first)
                and (not second or row['builds'][1]['raw_status'] == second))

    for system, maintenance, first, second in product(
        ['', 'cmake', 'meson', '_not_detected'], ['', 'Security', 'EOL'],
        ['', 'blocked', 'failed'], ['', 'failed', 'building'],
    ):
        result = index.select(view='all', buildsystem=system, maintenance=maintenance,
                              builds={'rva23': first, 'rva20': second}, page=1, per_page=2)
        expected = [r for r in oracle if matches(r, system, maintenance, first, second)]
        assert result['total'] == result['counts']['all'] == len(expected)
        assert [r['name'] for r in result['items']] == [r['name'] for r in expected[:2]]
        for value, count in result['buildsystems'].items():
            assert count == sum(matches(r, value, maintenance, first, second) for r in oracle)
        for value, count in result['maintenance_labels'].items():
            assert count == sum(matches(r, system, value, first, second) for r in oracle)
        for option in result['build_statuses']['rva23']:
            context = [r for r in oracle if matches(r, system, maintenance, '', second)]
            if option['value'] == 'issues':
                expected_count = sum(r['builds'][0]['issue'] for r in context)
            else:
                expected_count = sum(r['builds'][0]['raw_status'] == option['value'] for r in context)
            assert option['count'] == expected_count


def test_search_view_and_empty_selected_option(snapshot):
    snapshot['builds']['binutils']['rva23']['raw_status'] = 'blocked'
    rows, _ = view.project_monitors(snapshot)
    index = PackageList(rows, snapshot['targets'], 'BIN')
    result = index.select(view='updates', buildsystem='', maintenance='',
                          builds={'rva23': 'failed'}, page=999, per_page=1)
    assert result['total'] == 0 and result['page'] == 1
    assert {'value': 'failed', 'label': 'Failed', 'count': 0} in result['build_statuses']['rva23']
    assert {'value': 'blocked', 'label': 'Blocked', 'count': 1} in result['build_statuses']['rva23']
    assert all(count == 0 for count in result['counts'].values())
