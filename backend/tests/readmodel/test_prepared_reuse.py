"""Candidate counts and prepared status preserve query results."""
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
