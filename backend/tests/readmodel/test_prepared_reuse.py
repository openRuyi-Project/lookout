"""Reuse must preserve facets, searchable clocks and live failure notices."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from hypothesis import given, strategies as st

from tracker import api, state
from tracker.readmodel import snapshot as view
from tracker.readmodel.packages import PackageList
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


def projected(snapshot, monkeypatch):
    monkeypatch.setattr(state, 'compare', lambda *args: 'current')
    return view.project_monitors(snapshot)[0]


def test_clock_refresh_reuses_facets_but_updates_searchable_times(snapshot, monkeypatch):
    rows = projected(snapshot, monkeypatch)
    before = PackageList(rows, snapshot['targets'])
    newer = deepcopy(snapshot)
    stamp = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    newer['components']['builds']['fetched_at'] = stamp
    updated, _ = view.refresh_build_clock(newer, rows, datetime.now(UTC))
    after = PackageList(updated, snapshot['targets'], previous=before)
    fresh = PackageList(updated, snapshot['targets'])
    assert after.index is before.index
    assert after.observations == fresh.observations
    assert after.by_name['binutils'] is next(row for row in updated if row['name'] == 'binutils')
    assert after.observations != before.observations
    for query in ('', 'binutils', stamp):
        assert after.select(query=query, search='observations') == fresh.select(query=query, search='observations')
    assert all(after.observations[i]['version'] is before.observations[i]['version'] for i in range(len(rows)))


def test_changed_dimensions_and_package_order_rebuild_facets(snapshot, monkeypatch):
    rows = projected(snapshot, monkeypatch)
    before = PackageList(rows, snapshot['targets'])
    for changed in (list(reversed(rows)), deepcopy(rows), rows[:-1]):
        if len(changed) == len(rows) and changed[0]['name'] == rows[0]['name']:
            changed[0]['monitors']['build']['dimensions']['maintenance'] = ['CheckFailed']
        after = PackageList(changed, snapshot['targets'], previous=before)
        assert after.index is not before.index
        assert after.select() == PackageList(changed, snapshot['targets']).select()


def test_status_requests_do_not_reaggregate_and_failure_notice_is_live(snapshot, tmp_path, monkeypatch):
    monkeypatch.setattr(state, 'compare', lambda *args: 'current')
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    app = api.create_app(db)
    cache = app.state.projection
    cache.refresh()
    client = TestClient(app)
    before = client.get('/api/v2/status').json()
    def unexpected(*args):
        raise AssertionError('request repeated projection statistics')
    monkeypatch.setattr(view, 'upstream_failures', unexpected)
    monkeypatch.setattr(PackageList, 'monitor_coverage', unexpected)
    for _ in range(3):
        assert client.get('/api/v2/status').json() == before
    db.unlink()
    cache.refresh()
    after = client.get('/api/v2/status').json()
    assert after['projection_notice']
    assert after['source_versions'] == before['source_versions']
    assert after['monitor_coverage'] == before['monitor_coverage']


def test_target_changes_do_not_reuse_facet_schema(snapshot, monkeypatch):
    rows = projected(snapshot, monkeypatch)
    before = PackageList(rows, snapshot['targets'])
    targets = [*snapshot['targets'], {'id': 'new-target'}]
    after = PackageList(rows, targets, previous=before)
    assert after.index is not before.index
    assert 'build:new-target' in after.index
