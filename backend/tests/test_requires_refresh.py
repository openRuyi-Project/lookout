"""Compatible metadata enrichment must not invalidate saved observations."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tracker import monitor, monitor_requires, requirements, state


class OfflineIO:
    def for_hosts(self, hosts, *, max_age):
        return self


@pytest.fixture
def legacy_observations(config, snapshot, monkeypatch):
    now = datetime.now(timezone.utc)
    clock = [now - timedelta(minutes=2)]
    calls = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    def read(version, settings, io):
        calls.append(version)
        return [requirements.Requirement('runtime', 'Runtime', 'runtime', 'pep440', '>=1', None,
            'Registry', 'https://example.org/releases/' + version, optional=False)]

    backend = SimpleNamespace(read=read, inputs=lambda package, configured: configured)
    monkeypatch.setitem(monitor_requires.BACKENDS, 'fixture', backend)
    monkeypatch.setattr(state, 'compare', lambda *args: 'outdated')
    monkeypatch.setattr(state, 'utcnow', lambda: clock[0].isoformat())
    monkeypatch.setattr(monitor, 'datetime', Clock)
    monkeypatch.setattr(monitor.cfg, 'require_unchanged', lambda *args: None)
    config['monitors'] = {'enabled': ['requires'], 'batch_size': 1, 'workers': 1}
    snapshot['monitors'] = {}
    for name in ('binutils', 'untracked'):
        config['packages'][name] = {'monitors': {'requires': {'fixture': name}}}
        # This historical contract predates optionality, not a live provider fact.
        with monkeypatch.context() as older:
            older.setattr(monitor_requires, 'VERSION', 2)
            proposed = monitor.plan(config, snapshot, name, 'requires')
            observed = monitor.execute('requires', proposed, OfflineIO())
        for finding in observed['findings']:
            del finding['requirement']['optional']
        observed['evidence_revision'] = monitor.evidence_revision(
            {'input_fingerprint': observed['fingerprint']}, observed['findings'])
        snapshot['monitors'][name] = {'requires': observed}
        clock[0] += timedelta(seconds=1)
    clock[0] = now
    calls.clear()
    return backend, calls, clock


def test_backfill_is_due_without_clearing_packages_outside_the_batch(config, snapshot, legacy_observations, tmp_path):
    _, calls, clock = legacy_observations
    prior = deepcopy(snapshot['monitors']['untracked']['requires'])
    proposed = monitor.plan(config, snapshot, 'untracked', 'requires')
    policy = monitor.refresh_policy('requires', proposed, prior, monitor.settings(config))
    assert policy.interval_seconds == 30
    assert policy.due(prior, proposed['fingerprint'], clock[0])
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    collected = monitor.collect(config, 'unused', db, io=OfflineIO())
    refreshed = collected['monitors']['binutils']['requires']
    queued = collected['monitors']['untracked']['requires']
    assert calls == ['3.9.0', '3.10.0']
    assert all('optional' in item['requirement'] for item in refreshed['findings'])
    assert queued['findings'] == prior['findings']
    assert queued['checked_at'] == prior['checked_at']
    assert queued['scope_checks'] == prior['scope_checks']
    assert queued['evidence_revision'] == prior['evidence_revision']
    following = monitor.collect(config, 'unused', db, io=OfflineIO())
    completed = following['monitors']['untracked']['requires']
    assert calls == ['3.9.0', '3.10.0', '1.0']
    assert all('optional' in item['requirement'] for item in completed['findings'])
    assert monitor_requires.refresh({}, {}, completed).interval_seconds == 43200


def test_failed_backfill_retains_dated_evidence_and_existing_retry_backoff(config, snapshot, legacy_observations):
    backend, _, clock = legacy_observations
    prior = snapshot['monitors']['binutils']['requires']

    def unavailable(*args):
        raise OSError('provider unavailable')

    backend.read = unavailable
    proposed = monitor.plan(config, snapshot, 'binutils', 'requires')
    result = monitor.execute('requires', proposed, OfflineIO(), prior)
    assert result['status'] == 'error'
    assert result['findings'] == prior['findings']
    assert result['checked_at'] == prior['checked_at']
    assert result['evidence_revision'] == prior['evidence_revision']
    assert result['changed_at'] == prior['changed_at']
    assert {scope: check['checked_at'] for scope, check in result['scope_checks'].items()} == {
        scope: check['checked_at'] for scope, check in prior['scope_checks'].items()}
    policy = monitor.refresh_policy('requires', proposed, result, monitor.settings(config))
    assert policy.delay(1) == 300 and policy.delay(2) == 600
    assert not policy.due(result, proposed['fingerprint'], clock[0] + timedelta(seconds=30))
    assert policy.due(result, proposed['fingerprint'], clock[0] + timedelta(seconds=300))


@pytest.mark.parametrize('optional', [True, False, None])
def test_observed_optionality_restores_normal_refresh_even_when_unclassifiable(optional):
    previous = {'findings': [{'requirement': {'optional': optional}}]}
    assert monitor_requires.refresh({}, {}, previous).interval_seconds == 43200
    assert monitor_requires.refresh({}, {}, {'findings': []}).interval_seconds == 43200


@pytest.mark.parametrize('status,delay', [('unsupported', 43200), ('error', 300), ('partial', 300)])
def test_failed_backfill_does_not_poll_permanently_at_heartbeat_frequency(status, delay):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    previous = {'status': status, 'fingerprint': 'same-input', 'attempted_at': now.isoformat(),
                'findings': [{'requirement': {'dependency': 'runtime'}}]}
    policy = monitor_requires.refresh({}, {}, previous)
    assert not policy.due(previous, 'same-input', now + timedelta(seconds=30))
    assert not policy.due(previous, 'same-input', now + timedelta(seconds=delay - 1))
    assert policy.due(previous, 'same-input', now + timedelta(seconds=delay))


def test_backfill_respects_existing_operator_refresh_overrides(config, snapshot, legacy_observations):
    prior = snapshot['monitors']['binutils']['requires']
    proposed = monitor.plan(config, snapshot, 'binutils', 'requires')
    config['monitors']['refresh'] = {'requires': {'interval_seconds': 120}}
    policy = monitor.refresh_policy('requires', proposed, prior, monitor.settings(config))
    assert policy.interval_seconds == 120
