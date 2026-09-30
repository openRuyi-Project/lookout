"""Real runner/cache boundaries, with synthetic provider records and a virtual clock."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from tracker import state
from tracker.monitors import model, runner
from tracker.monitors.observations import visible
from tracker.providers import client as transport
from tracker.providers.client import IO
from tests.monitors.test_security_nvd import record


@pytest.mark.parametrize('provider,settings', [
    ('security', {'ecosystem': 'PyPI', 'name': 'fixture-component'}),
    ('security', {'vendor': 'fixture', 'product': 'component'}),
    ('security', {'ecosystem': 'GIT', 'name': 'https://github.com/example/component'}),
    ('license', {'pypi': 'fixture-component'}),
    ('requires', {'pypi': 'fixture-component'}),
    ('yanked', {'pypi': 'fixture-component'}),
    ('eol', {'product': 'fixture-component', 'cycle_parts': 2}),
])
def test_package_version_change_invalidates_saved_results_immediately(
    config, snapshot, monkeypatch, provider, settings,
):
    # RPM comparison is exercised by native tests; this test isolates query keys.
    monkeypatch.setattr(state, 'compare', lambda *args: 'outdated')
    config['packages']['binutils'] = {'monitors': {provider: settings}}
    before = runner.plan(config, snapshot, 'binutils', provider)
    assert before['status'] == 'pending'
    previous = {**before, 'status': 'ok', 'attempted_at': state.utcnow(),
                'checked_at': state.utcnow(), 'findings': [model.finding(
                    'old', 'Advisory', 'Old release evidence', [], 'https://example.org/advisory')]}

    snapshot['sources']['binutils']['version'] = '4.0.0'
    after = runner.plan(config, snapshot, 'binutils', provider)
    assert after['status'] == 'pending'
    assert after['fingerprint'] != before['fingerprint']
    assert after['query_fingerprint'] != before['query_fingerprint']
    policy = runner.refresh_policy(provider, after, previous, {})
    assert policy.due(previous, after['fingerprint'], datetime.now(timezone.utc))
    pending = runner.retain_current(after, previous)
    assert pending['last_result'] == previous
    assert visible(pending)['findings'] == []
    assert not visible(pending).get('checked_at')


def test_eol_patch_release_reuses_only_the_unchanged_release_cycle(config, snapshot):
    config['packages']['binutils'] = {'monitors': {
        'eol': {'product': 'fixture-component', 'cycle_parts': 2}}}
    before = runner.plan(config, snapshot, 'binutils', 'eol')
    snapshot['sources']['binutils']['version'] = '3.9.1'
    after = runner.plan(config, snapshot, 'binutils', 'eol')
    assert before['subject']['version'] != after['subject']['version']
    assert before['fingerprint'] == after['fingerprint']
    assert before['query_fingerprint'] == after['query_fingerprint']


def test_nvd_cache_heartbeat_failure_and_new_version_keep_distinct_evidence(monkeypatch):
    clock = [datetime.now(timezone.utc)]
    requests, failing = [], [False]

    def respond(request):
        requests.append(request)
        if 'nvd.nist.gov' in request.url.host:
            if failing[0]:
                return httpx.Response(503, headers={'Retry-After': '900'})
            return httpx.Response(200, json={'totalResults': 1, 'startIndex': 0,
                                             'vulnerabilities': [{'cve': record()}]})
        if 'cisa.gov' in request.url.host:
            return httpx.Response(200, json={'vulnerabilities': []})
        return httpx.Response(200, json={'data': []})

    monkeypatch.setattr(state, 'utcnow', lambda: clock[0].isoformat())
    monkeypatch.setattr(transport, 'time', SimpleNamespace(
        time=lambda: clock[0].timestamp(), monotonic=lambda: clock[0].timestamp(),
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds))))
    settings = {'source': 'nvd', 'vendor': 'fixture', 'product': 'component'}
    proposed = {'subject': {'version': '1.2.3'}, 'inputs': settings, 'scope': 'current',
                'status': 'pending', 'fingerprint': 'first', 'query_fingerprint': 'first'}
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        owner = IO(client=http)
        first = runner.execute('security', proposed, owner)
        assert first['status'] == 'ok' and len(requests) == 3
        clock[0] += timedelta(seconds=30)
        cached = runner.execute('security', proposed, owner, first)
        assert len(requests) == 3
        assert cached['findings'] == first['findings']
        assert cached['changed_at'] == first['changed_at']
        assert cached['evidence_revision'] == first['evidence_revision']
        assert cached['checked_at'] == first['checked_at']

        clock[0] += timedelta(hours=6)
        failing[0] = True
        failed = runner.execute('security', proposed, owner, cached)
        assert failed['status'] == 'error'
        assert failed['findings'] == first['findings']
        assert failed['checked_at'] == first['checked_at']
        next_version = {**proposed, 'subject': {'version': '1.2.4'},
                        'fingerprint': 'second', 'query_fingerprint': 'second'}
        clock[0] += timedelta(seconds=901)
        new_failure = runner.execute('security', next_version, owner, failed)
        assert new_failure['status'] == 'error' and new_failure['findings'] == []
        assert new_failure['checked_at'] is None
        nvd_requests = [r for r in requests if 'nvd.nist.gov' in r.url.host]
        assert nvd_requests[-1].url.params['cpeName'].split(':')[5] == '1.2.4'
        assert nvd_requests[0].url != nvd_requests[-1].url


def test_osv_version_is_part_of_the_response_cache_key():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'vulns': []})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        owner = IO(client=http)
        scoped = owner.for_hosts({'api.osv.dev'})
        base = {'package': {'ecosystem': 'PyPI', 'name': 'fixture-component'}}
        for version in ('1.2.3', '1.2.3', '1.2.4'):
            scoped.json('POST', 'https://api.osv.dev/v1/query', {**base, 'version': version})
    assert len(requests) == 2 and requests[0].content != requests[1].content


def test_inflight_old_response_cannot_be_published_as_a_new_package_version(
    config, snapshot, tmp_path, monkeypatch,
):
    from tests.monitors.test_security_current_scope import AdvisoryIO
    config['packages']['binutils'] = {'monitors': {
        'security': {'ecosystem': 'PyPI', 'name': 'fixture-component'}}}
    config['monitors'] = {'enabled': ['security'], 'workers': 1}
    snapshot['sources'] = {'binutils': snapshot['sources']['binutils']}
    old_version = snapshot['sources']['binutils']['version']
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    monkeypatch.setattr(runner.cfg, 'require_unchanged', lambda *args: None)

    class UpgradedDuringRequest(AdvisoryIO):
        def json(self, method, url, body=None):
            if not self.queries:
                with state.writer_lock(db):
                    changed = state.read(db)
                    changed['sources']['binutils']['version'] = '4.0.0'
                    state.commit(db, changed)
            self.entries = [{'id': 'GHSA-fixture-' + body['version'], 'affected': []}]
            return super().json(method, url, body)

    io = UpgradedDuringRequest()
    first = runner.collect(config, 'unused', db, io=io)
    pending = first['monitors']['binutils']['security']
    assert pending['subject']['version'] == '4.0.0' and pending['status'] == 'pending'
    assert pending['findings'] == [] and not pending.get('checked_at')
    assert pending['last_result']['subject']['version'] == old_version
    assert model.project(first, 'binutils', datetime.now(timezone.utc))['findings'] == []

    second = runner.collect(config, 'unused', db, io=io)
    completed = second['monitors']['binutils']['security']
    assert completed['status'] == 'ok' and completed['subject']['version'] == '4.0.0'
    assert completed['findings'][0]['id'] == 'GHSA-fixture-4.0.0'
    assert 'last_result' not in completed
    assert [q['version'] for q in io.queries] == [old_version, '4.0.0']
