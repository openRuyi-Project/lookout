"""Security remains a periodically checked current-release fact, not upgrade advice."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tracker import monitor, monitor_model, state, version_status


class AdvisoryIO:
    def __init__(self):
        self.entries = []
        self.queries = []
        self.cache_ages = []

    def for_hosts(self, hosts, *, max_age=None):
        assert 'api.osv.dev' in hosts
        self.cache_ages.append(max_age)
        return self

    def json(self, method, url, body=None):
        assert (method, url) == ('POST', 'https://api.osv.dev/v1/query')
        self.queries.append(deepcopy(body))
        return {'vulns': deepcopy(self.entries)}


@pytest.fixture
def latest_package(config, snapshot, monkeypatch):
    name = 'binutils'
    config['packages'][name] = {'monitors': {
        'security': {'ecosystem': 'PyPI', 'name': 'synthetic-security-fixture'},
    }}
    snapshot['sources'] = {name: snapshot['sources'][name]}
    snapshot['tracks'][name]['version'] = snapshot['sources'][name]['version']

    # The native comparator has its own tests; this fixture only supplies its
    # equality result, while checking that the two observed versions are equal.
    def same_version(current, latest, comparable=True):
        assert current == latest
        return 'current'

    monkeypatch.setattr(state, 'compare', same_version)
    return name


def test_latest_release_still_queries_security(config, snapshot, latest_package):
    version = version_status.evaluate(snapshot, latest_package)
    assert version.relation == 'current' and not version.upgrading
    io = AdvisoryIO()
    io.entries = [{'id': 'GHSA-synthetic-fixture', 'affected': []}]

    result = monitor.check(config, snapshot, latest_package, 'security', io)

    assert result['status'] == 'ok'
    assert result['scope'] == 'current'
    assert 'target_version' not in result['subject']
    assert io.queries == [{
        'package': {'ecosystem': 'PyPI', 'name': 'synthetic-security-fixture'},
        'version': snapshot['sources'][latest_package]['version'],
    }]
    assert [finding['scope'] for finding in result['findings']] == ['current']
    assert [finding['id'] for finding in result['findings']] == ['GHSA-synthetic-fixture']


def test_version_provider_failure_does_not_gate_current_security(
    config, snapshot, latest_package,
):
    snapshot['tracks'][latest_package]['error'] = 'version-provider-unavailable'
    version = version_status.evaluate(snapshot, latest_package)
    assert version.relation == 'unknown' and not version.upgrading
    io = AdvisoryIO()

    result = monitor.check(config, snapshot, latest_package, 'security', io)

    assert result['status'] == 'ok'
    assert len(io.queries) == 1
    assert io.queries[0]['version'] == version.source['version']


def test_latest_release_heartbeat_discovers_new_advisory_without_version_change(
    config, snapshot, latest_package, monkeypatch, tmp_path,
):
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(monitor, 'datetime', SimpleNamespace(now=lambda tz: clock[0]))
    monkeypatch.setattr(state, 'utcnow', lambda: clock[0].isoformat())
    config.update(monitors={'enabled': ['security'], 'workers': 1},
                  config_digest='fixture', nv_digest='fixture')
    monkeypatch.setattr(monitor.cfg, 'require_unchanged', lambda *args: None)
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    io = AdvisoryIO()

    first = monitor.collect(config, 'unused', db, io=io)
    original = first['monitors'][latest_package]['security']
    assert original['findings'] == [] and len(io.queries) == 1

    clock[0] += timedelta(seconds=21600 - 1)
    idle = monitor.collect(config, 'unused', db, io=io)
    assert idle['generation'] == first['generation']
    assert len(io.queries) == 1

    io.entries = [{'id': 'GHSA-new-synthetic-advisory', 'affected': []}]
    clock[0] += timedelta(seconds=1)
    refreshed = monitor.collect(config, 'unused', db, io=io)
    latest = refreshed['monitors'][latest_package]['security']
    assert len(io.queries) == 2 and io.queries[0] == io.queries[1]
    assert io.cache_ages == [21600, 21600]
    assert latest['fingerprint'] == original['fingerprint']
    assert latest['evidence_revision'] != original['evidence_revision']
    assert latest['checked_at'] == clock[0].isoformat()
    evidence = monitor_model.project(refreshed, latest_package, clock[0])
    assert [finding['title'] for finding in evidence['findings']] == ['GHSA-new-synthetic-advisory']
    assert not evidence['findings'][0]['stale']
