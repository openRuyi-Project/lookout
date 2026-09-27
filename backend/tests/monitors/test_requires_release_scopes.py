"""Requires release independence and read-side truth, without native RPM or network."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.conftest import ProjectedClient
from tracker import state
from tracker.api import create_app
from tracker.monitors import model as monitor_model, runner as monitor
from tracker.monitors.requires import model as requirements, monitor as monitor_requires
from tracker.monitors.version import compare as version_status
from tracker.readmodel import snapshot as view
from tracker.readmodel.cache import ProjectionCache


NOW = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


class OfflineIO:
    def for_hosts(self, hosts, *, max_age):
        return self


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(state, 'utcnow', lambda: NOW.isoformat())
    monkeypatch.setattr(monitor, 'datetime', FrozenDateTime)
    monkeypatch.setattr(version_status, 'datetime', FrozenDateTime)
    monkeypatch.setattr(view, 'datetime', FrozenDateTime)
    monkeypatch.setattr(ProjectionCache, 'now', staticmethod(lambda: NOW))
    monkeypatch.setattr(state, 'compare', lambda *args: 'outdated')


def declaration(version, *, kind='runtime', expression=None, identity=None, condition=None, extras=()):
    expression = expression if expression is not None else '>=3.8' if version == '3.9.0' else '>=3.10'
    return requirements.Requirement('python', 'Python', kind, 'pep440', expression, expression,
        'Public upstream metadata', 'https://example.org/releases/' + version,
        identity=identity, condition=condition, extras=extras)


@pytest.fixture
def release_setup(config, snapshot, monkeypatch):
    calls = []

    def read(version, settings, io):
        calls.append(version)
        return [declaration(version)]

    backend = SimpleNamespace(read=read, inputs=lambda package, configured: configured)
    monkeypatch.setitem(monitor_requires.BACKENDS, 'fixture', backend)
    config.update(monitors={'enabled': ['requires']},
                  openruyi={'dependencies': {'python': 'runtime-package'}})
    config['packages']['binutils'] = {'monitors': {'requires': {'fixture': 'widget'}}}
    snapshot['dependency_packages'] = {'python': 'runtime-package'}
    snapshot['sources']['runtime-package'] = state.success({},
        {'version': '3.11.8', 'srcmd5': 'runtime-source'}, NOW.isoformat())
    snapshot['monitor_catalog'] = {'requires': {'title': 'Requires'}}
    return backend, calls


def execute(config, snapshot, previous=None):
    proposed = monitor.plan(config, snapshot, 'binutils', 'requires')
    return monitor.execute('requires', proposed, OfflineIO(), previous)


def save_observation(snapshot, observation):
    snapshot['monitors'] = {'binutils': {'requires': observation}}


def assessment(snapshot, observation=None, now=NOW):
    if observation is not None:
        save_observation(snapshot, observation)
    rows, _ = view.project_monitors(snapshot, now)
    return next(row for row in rows if row['name'] == 'binutils')['monitors']['requires']


@pytest.mark.parametrize('relation', ['current', 'ahead', 'unknown'])
def test_current_runtime_runs_without_a_confirmed_upgrade(config, snapshot, release_setup, monkeypatch, relation):
    monkeypatch.setattr(state, 'compare', lambda *args: relation)
    proposed = monitor.plan(config, snapshot, 'binutils', 'requires')
    assert proposed['status'] == 'pending'
    assert proposed['subject']['target_version'] is None
    observed = execute(config, snapshot)
    assert observed['status'] == 'ok'
    assert release_setup[1] == ['3.9.0']
    result = assessment(snapshot, observed)
    row = result['data']['requirements'][0]
    assert row['current']['expression'] == '>=3.8'
    assert row['satisfaction'] == 'satisfied'
    assert row['target'] is None and row['changed'] is False


def test_untracked_configured_package_still_checks_current_runtime(config, snapshot, release_setup):
    config['native'].pop('binutils')
    snapshot['native_ids'].remove('binutils')
    observed = execute(config, snapshot)
    assert observed['status'] == 'ok'
    assert release_setup[1] == ['3.9.0']
    assert assessment(snapshot, observed)['data']['requirements'][0]['satisfaction'] == 'satisfied'


def test_target_fetch_failure_does_not_corrupt_current_evidence(config, snapshot, release_setup):
    backend, calls = release_setup

    def read(version, settings, io):
        calls.append(version)
        if version == '3.10.0':
            raise OSError('target metadata unavailable')
        return [declaration(version)]

    backend.read = read
    observed = execute(config, snapshot)
    assert observed['status'] == 'partial'
    assert observed['scope_checks']['current']['status'] == 'ok'
    assert observed['scope_checks']['upgrade']['status'] == 'error'
    result = assessment(snapshot, observed)
    assert len(result['data']['findings']) == 1
    assert result['data']['findings'][0]['stale'] is False
    row = result['data']['requirements'][0]
    assert row['satisfaction'] == 'satisfied'
    assert row['target'] is None and row['target_satisfaction'] == 'unknown'


def test_current_failure_retains_old_current_without_refreshing_it(config, snapshot, release_setup, monkeypatch):
    backend, _ = release_setup
    first = execute(config, snapshot)
    later = NOW + timedelta(seconds=30)
    monkeypatch.setattr(state, 'utcnow', lambda: later.isoformat())

    def read(version, settings, io):
        if version == '3.9.0':
            raise OSError('current metadata unavailable')
        return [declaration(version)]

    backend.read = read
    second = execute(config, snapshot, first)
    assert second['status'] == 'partial'
    assert second['scope_checks']['current']['checked_at'] == first['scope_checks']['current']['checked_at']
    assert second['scope_checks']['upgrade']['checked_at'] == later.isoformat()
    result = assessment(snapshot, second, later)
    row = result['data']['requirements'][0]
    assert row['current']['expression'] == '>=3.8'
    assert (row['satisfaction'], row['reason']) == ('unknown', 'requirement_unavailable')
    assert row['target_satisfaction'] == 'satisfied' and row['changed'] is False
    assert 'unmet' not in result['dimensions']['requires']


@pytest.mark.parametrize('upstream_change', ['version', 'error', 'expired'])
def test_target_reprojection_never_hides_current(config, snapshot, release_setup, upstream_change):
    observed = execute(config, snapshot)
    if upstream_change == 'version':
        snapshot['tracks']['binutils']['version'] = '3.12.0'
    elif upstream_change == 'error':
        snapshot['tracks']['binutils']['error'] = 'provider failed'
    else:
        snapshot['tracks']['binutils']['fetched_at'] = (NOW - timedelta(days=2)).isoformat()
    result = assessment(snapshot, observed)
    current = next(f for f in result['data']['findings'] if f['scope'] == 'current')
    assert current['stale'] is False
    row = result['data']['requirements'][0]
    assert row['satisfaction'] == 'satisfied'
    if upstream_change == 'version':
        assert row['target'] is None
    else:
        assert row['target_satisfaction'] == 'unknown' and row['changed'] is False


def test_target_changes_during_publication_preserve_completed_current(config, snapshot, release_setup,
                                                                    monkeypatch, tmp_path):
    backend, calls = release_setup
    monkeypatch.setattr(monitor.cfg, 'require_unchanged', lambda *args: None)
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)

    def read(version, settings, io):
        calls.append(version)
        if version == '3.10.0':
            with state.writer_lock(db):
                latest = state.read(db)
                tracks = deepcopy(latest['tracks'])
                tracks['binutils']['version'] = '3.12.0'
                state.commit(db, state.merge(latest, 'upstreams', {'tracks': tracks}))
        return [declaration(version)]

    backend.read = read
    collected = monitor.collect(config, 'unused', db, io=OfflineIO())
    observed = collected['monitors']['binutils']['requires']
    assert calls == ['3.9.0', '3.10.0']
    assert observed['subject']['target_version'] == '3.12.0'
    assert observed['scope_checks']['current']['checked_at'] == NOW.isoformat()
    assert [f['scope'] for f in observed['findings']] == ['current']
    row = assessment(collected)['data']['requirements'][0]
    assert row['satisfaction'] == 'satisfied' and row['target'] is None


def test_dependency_only_change_reprojects_unmet_and_preserves_upstream_facts(config, snapshot, release_setup, tmp_path):
    observed = execute(config, snapshot)
    save_observation(snapshot, observed)
    original = deepcopy(snapshot['monitors'])
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    client = ProjectedClient(create_app(db))
    route = '/api/v2/packages?monitor=requires&requires=unmet'
    first = client.get(route)
    assert first.status_code == 200 and first.json()['total'] == 0
    snapshot['sources']['runtime-package']['version'] = '3.7'
    state.commit(db, snapshot)
    second = client.get(route)
    assert second.status_code == 200 and second.json()['total'] == 1
    assert second.json()['requires_counts']['unmet'] == 1
    row = second.json()['items'][0]['monitors']['requires']['data']['requirements'][0]
    assert row['satisfaction'] == 'unsatisfied' and row['target_satisfaction'] == 'unsatisfied'
    assert state.read(db)['monitors'] == original
    assert release_setup[1] == ['3.9.0', '3.10.0']
    document = client.get('/api/ui/packages?monitor=requires&q=binutils').json()
    unmet = next(choice for navigation in document['controls']['navigation']
                 for choice in navigation['choices'] if choice['label'] == 'Unmet')
    query = parse_qs(urlsplit(unmet['href']).query)
    assert query['requires'] == ['unmet'] and query['monitor'] == ['requires']
    assert query['q'] == ['binutils'] and unmet['count'] == 1


def test_declaration_scopes_have_independent_expiry_and_cache_boundary(config, snapshot, release_setup):
    observed = execute(config, snapshot)
    snapshot['monitor_stale_after_seconds'] = 60
    observed['scope_checks']['current']['checked_at'] = (NOW - timedelta(seconds=50)).isoformat()
    save_observation(snapshot, observed)
    boundary = NOW + timedelta(seconds=10, microseconds=1)
    assert view.next_transition(snapshot, NOW) == boundary
    result = assessment(snapshot, now=boundary)
    row = result['data']['requirements'][0]
    assert row['satisfaction'] == 'unknown' and row['reason'] == 'requirement_unavailable'
    assert row['target_satisfaction'] == 'satisfied'
    assert 'unmet' not in result['dimensions']['requires']


def test_dependency_expiry_is_unknown_not_unmet(config, snapshot, release_setup):
    observed = execute(config, snapshot)
    snapshot['sources']['runtime-package'].update(version='3.7',
        fetched_at=(NOW - timedelta(days=2)).isoformat())
    result = assessment(snapshot, observed)
    row = result['data']['requirements'][0]
    assert row['satisfaction'] == row['target_satisfaction'] == 'unknown'
    assert row['reason'] == 'dependency_unavailable'
    assert 'unmet' not in result['dimensions']['requires']


def test_refresh_clocks_do_not_change_evidence_revision(config, snapshot, release_setup, monkeypatch):
    first = execute(config, snapshot)
    later = (NOW + timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(state, 'utcnow', lambda: later)
    second = execute(config, snapshot, first)
    assert second['checked_at'] == later
    assert second['scope_checks']['current']['checked_at'] == later
    assert second['scope_checks']['upgrade']['checked_at'] == later
    assert second['evidence_revision'] == first['evidence_revision']
    assert second['changed_at'] == first['changed_at']
    assert second['findings'] == first['findings']


def test_duplicate_adapter_clause_is_rejected_without_partial_release_facts(config, snapshot, release_setup):
    backend, _ = release_setup

    def read(version, settings, io):
        if version == '3.9.0':
            return [declaration(version), declaration(version, expression='<4')]
        return [declaration(version)]

    backend.read = read
    observed = execute(config, snapshot)
    assert observed['scope_checks']['current']['status'] != 'ok'
    assert not any(f['scope'] == 'current' for f in observed['findings'])
    assert observed['scope_checks']['upgrade']['status'] == 'ok'


def test_condition_and_upstream_identity_changes_do_not_overwrite_clauses(config, snapshot, release_setup):
    backend, _ = release_setup
    backend.read = lambda version, *args: [
        declaration(version, identity={'ecosystem': 'PyPI', 'name': 'first' if version == '3.9.0' else 'second'}),
        declaration(version, identity={'ecosystem': 'PyPI', 'name': 'optional'}, condition='python_version < "4"'),
    ]
    observed = execute(config, snapshot)
    assert observed['status'] == 'ok'
    assert len(observed['findings']) == len({f['id'] for f in observed['findings']}) == 4
    rows = assessment(snapshot, observed)['data']['requirements']
    assert len(rows) == 3
    identities = {r['identity']['name']: r for r in rows}
    assert identities['first']['target'] is None
    assert identities['second']['current'] is None
    assert identities['optional']['reason'] == 'condition_not_evaluated'
    assert identities['first']['changed'] is identities['second']['changed'] is False
    # A declaration change is observable even when its environmental condition
    # prevents a local satisfaction verdict.
    assert identities['optional']['changed'] is True




def test_build_declarations_are_saved_without_current_runtime_warnings(config, snapshot, release_setup):
    backend, _ = release_setup
    backend.read = lambda version, *args: [declaration(version, kind='build')]
    observed = execute(config, snapshot)
    assert observed['status'] == 'ok'
    assert {f['scope'] for f in observed['findings']} == {'current', 'upgrade'}
    assert all(f['requirement']['kind'] == 'build' for f in observed['findings'])
    result = assessment(snapshot, observed)
    assert result['data']['requirements'] == []
    assert result['data']['labels'] == []
    assert result['dimensions']['requires'] == []


def test_dependency_spec_version_is_not_replaced_with_obs_or_artifact_truth(config, snapshot, release_setup):
    observed = execute(config, snapshot)
    snapshot['specs']['runtime-package'] = state.success({}, {
        'metadata': {'version': '3.7', 'requires': ['unrelated >= 999']},
        'head': 'source-commit', 'native_query': {'spec_sha256': 'spec-digest'}}, NOW.isoformat())
    result = assessment(snapshot, observed)
    row = result['data']['requirements'][0]
    assert row['observed']['origin'] == 'spec' and row['observed']['version'] == '3.7'
    assert row['satisfaction'] == 'unsatisfied'
    assert row['current']['expression'] == '>=3.8'
    assert row['current']['source'] == 'Public upstream metadata'
    assert row['observed']['revision'].startswith('spec:')
    snapshot['specs']['runtime-package']['error'] = 'SPEC observation failed'
    row = assessment(snapshot)['data']['requirements'][0]
    assert row['observed']['origin'] == 'spec'
    assert row['satisfaction'] == 'unknown' and row['reason'] == 'dependency_unavailable'


def test_framework_failure_after_target_change_retains_dated_current(config, snapshot, release_setup):
    first = execute(config, snapshot)
    snapshot['tracks']['binutils']['version'] = '3.12.0'
    proposed = monitor.plan(config, snapshot, 'binutils', 'requires')

    class BrokenIO(OfflineIO):
        def for_hosts(self, hosts, *, max_age):
            raise OSError('transport setup failed')

    second = monitor.execute('requires', proposed, BrokenIO(), first)
    assert second['status'] == 'error'
    assert len(second['findings']) == 1
    assert second['findings'][0]['scope'] == 'current'
    assert second['scope_checks']['current']['checked_at'] == first['scope_checks']['current']['checked_at']
    assert assessment(snapshot, second)['data']['requirements'][0]['satisfaction'] == 'unknown'


@pytest.mark.parametrize('source_failure', ['error', 'expired'])
def test_unavailable_source_retains_dated_current_when_upgrade_is_gated(config, snapshot, release_setup,
                                                                      source_failure):
    first = execute(config, snapshot)
    pristine = deepcopy(first)
    source = snapshot['sources']['binutils']
    if source_failure == 'error':
        source['error'] = 'temporary source observation failure'
    else:
        source['fetched_at'] = (NOW - timedelta(days=2)).isoformat()
    proposed = monitor.plan(config, snapshot, 'binutils', 'requires')
    assert proposed['status'] == 'unsupported' and proposed['subject']['target_version'] is None
    gated = monitor.execute('requires', proposed, OfflineIO(), first)
    assert gated['findings'] == [f for f in first['findings'] if f['scope'] == 'current']
    assert gated['scope_checks'] == {'current': first['scope_checks']['current']}
    assert gated['checked_at'] == first['scope_checks']['current']['checked_at']
    result = assessment(snapshot, gated)
    row = result['data']['requirements'][0]
    assert result['check']['status'] == 'input_unavailable'
    assert (row['satisfaction'], row['reason']) == ('unknown', 'requirement_unavailable')
    assert row['target'] is None and row['changed'] is False
    assert 'unmet' not in result['dimensions']['requires']
    assert first == pristine and release_setup[1] == ['3.9.0', '3.10.0']

    # Recovering the source can reuse the exact current declaration while the
    # newly eligible upgrade awaits its own check, without inventing a new clock.
    source.update(error=None, fetched_at=NOW.isoformat())
    pending = monitor.retain_current(monitor.plan(config, snapshot, 'binutils', 'requires'), gated)
    assert pending['scope_checks']['current'] == first['scope_checks']['current']
    recovered = assessment(snapshot, pending)['data']['requirements'][0]
    assert recovered['satisfaction'] == 'satisfied' and recovered['target'] is None


def test_unavailable_new_source_version_never_inherits_old_current_declarations(config, snapshot, release_setup):
    first = execute(config, snapshot)
    snapshot['sources']['binutils'].update(version='3.9.1', error='temporary source failure')
    gated = execute(config, snapshot, first)
    assert gated['status'] == 'unsupported' and gated['findings'] == []
    assert assessment(snapshot, gated)['data']['requirements'] == []


def test_stored_requirements_accept_release_declarations_not_derived_comparisons():
    current = declaration('3.9.0').fact()
    monitor_model.finding('current', 'Requires', 'Runtime', [], current['constraint']['url'], requirement=current)
    comparison = {key: value for key, value in current.items() if key != 'constraint'}
    comparison.update(current=current['constraint'], target=current['constraint'])
    with pytest.raises(ValueError):
        monitor_model.finding('derived', 'Requires', 'Runtime', [], current['constraint']['url'], requirement=comparison)
