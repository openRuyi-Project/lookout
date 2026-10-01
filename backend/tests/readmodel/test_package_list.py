

from tests.helpers.query import conjunction, query_url
from tests.conftest import ProjectedClient
from tracker import state
from tracker.api import create_app
from tracker.monitors.build import status as build_status
from tracker.readmodel import snapshot as view
from tracker.readmodel.packages import PackageList


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
    client = ProjectedClient(create_app(db))
    result = client.get(query_url('/api/v2/packages', {'view': 'problems'})).json()
    assert result['total'] == result['counts']['problems'] == 2
    assert {row['name'] for row in result['items']} == {'binutils', 'foo3'}
    result = client.get(query_url('/api/v2/packages', {'build:rva23': 'blocked', 'build:rva20': 'blocked'})).json()
    assert result['total'] == 1
    assert result['items'][0]['name'] == 'binutils'
    assert result['items'][0]['monitors']['build']['data']['targets'][0]['issue'] is True
    assert len(result['build_statuses']) == 3
    for query in ['build=absent:failed', 'build=rva23:failed&build=rva23:blocked', 'build=failed']:
        assert client.get('/api/v2/packages?' + query).status_code == 422


def test_search_view_and_empty_selected_option(snapshot):
    snapshot['builds']['binutils']['rva23']['raw_status'] = 'blocked'
    rows, _ = view.project_monitors(snapshot)
    index = PackageList(rows, snapshot['targets'], 'BIN')
    result = index.select(filters=conjunction({'view': 'updates', 'buildsystem': '', 'maintenance': '', **{'build:' + target: value for target, value in ({'rva23': 'failed'}).items()}}), page=999, per_page=1)
    assert result['total'] == 0 and result['page'] == 1
    assert {'value': 'failed', 'label': 'Failed', 'count': 0} in result['build_statuses']['rva23']
    assert {'value': 'blocked', 'label': 'Blocked', 'count': 0} in result['build_statuses']['rva23']
    assert all(count == 0 for count in result['counts'].values())


def test_build_choices_follow_obs_order_without_changing_aggregation_rank():
    codes = ['succeeded', 'failed', 'unresolvable', 'broken', 'blocked', 'dispatching',
             'scheduled', 'building', 'finished', 'signing', 'disabled', 'excluded', 'locked', 'deleting', 'unknown']
    rows = [{'name': code, 'monitors': {'fixture': {'dimensions': {'build:target': [code]}}}}
            for code in [*reversed(codes), 'future-state']]
    index = PackageList(rows, [{'id': 'target'}])
    for selected in ('', 'failed'):
        result = index.select(filters=conjunction({'buildsystem': '', 'maintenance': [], **{'build:' + target: value for target, value in ({'target': selected}).items()}}), page=1, per_page=100)
        assert [item['value'] for item in result['build_statuses']['target']] == [*codes, 'future-state']
        assert all(item['count'] == int(not selected or item['value'] == selected) for item in result['build_statuses']['target'])
    assert build_status.describe('failed').rank < build_status.describe('succeeded').rank
