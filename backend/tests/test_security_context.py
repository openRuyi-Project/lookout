"""Provider context is evidence, never generated remediation or priority."""
from copy import deepcopy

import pytest

from tracker import monitor, monitor_model, monitor_security, state


CVE = 'CVE-2026-12345'
VECTOR = 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H'
SETTINGS = {'ecosystem': 'PyPI', 'name': 'fixture'}
SUBJECT = {'version': '1.0'}


class FixtureIO:
    def __init__(self, entries, kev=None, error=None):
        self.entries = entries
        self.kev = kev if kev is not None else [{'cveID': CVE}]
        self.error = error
        self.calls = []

    def for_hosts(self, hosts, *, max_age=None):
        return self

    def json(self, method, url, body=None):
        self.calls.append(url)
        if '/v1/query' in url:
            if self.error:
                raise self.error
            return {'vulns': deepcopy(self.entries)}
        if 'cisa.gov' in url:
            return {'vulnerabilities': deepcopy(self.kev)}
        if 'first.org' in url:
            return {'data': [{'cve': CVE, 'epss': '0.004', 'date': '2026-09-25'}]}
        pytest.fail('Unexpected request: ' + url)


def advisory(**extra):
    return {'id': 'GHSA-fixture', 'aliases': [CVE], 'affected': [], **extra}


def facts(result, code):
    return [fact for finding in result['findings'] for fact in finding['facts'] if fact.get('code') == code]


def test_security_context_preserves_source_facts_without_advice():
    entry = advisory(summary='Unbounded TLS\n handshake goroutines',
        severity=[{'type': 'CVSS_V3', 'score': VECTOR, 'source': 'NVD'}],
        references=[{'type': 'FIX', 'url': 'https://github.com/example/project/commit/abc'},
                    {'type': 'ADVISORY', 'url': 'https://example.org/advisory'}])
    io = FixtureIO([entry], [{'cveID': CVE, 'dateAdded': '2026-09-24',
                              'knownRansomwareCampaignUse': 'Known',
                              'requiredAction': 'Vendor upgrade advice', 'dueDate': '2026-10-01'}])
    result = monitor_security.check(SUBJECT, SETTINGS, io)
    assert result['status'] == 'ok'
    assert facts(result, 'summary')[0]['value'] == 'Unbounded TLS handshake goroutines'
    assert facts(result, 'summary')[0]['source'] == 'OSV'
    vector = facts(result, 'cvss_vector')[0]
    assert (vector['key'], vector['value'], vector['source']) == ('CVSS_V3 · NVD', VECTOR, 'OSV')
    assert vector['url'].endswith('GHSA-fixture')
    assert facts(result, 'kev_added')[0]['value'] == '2026-09-24'
    assert facts(result, 'kev_ransomware')[0]['value'] == 'Known'
    assert facts(result, 'kev_ransomware')[0]['source'] == 'CISA'
    assert len(io.calls) == 3  # Reference URLs are never requested.
    assert 'Vendor upgrade advice' not in str(result) and 'dueDate' not in str(result)
    assert not {'severity', 'resolution', 'detail', 'priority'} & result['findings'][0].keys()
    assert facts(result, 'epss_probability')[0]['value'] == 0.004


@pytest.mark.parametrize('url', [
    'javascript:alert(1)', 'http://example.org/fix', 'file:///etc/passwd',
    'https://user:secret@example.org/fix', 'https://127.0.0.1/fix',
    'https://[::1]/fix', 'https://192.168.1.2/fix', 'https://169.254.169.254/fix',
    'https://2130706433/fix', 'https://0x7f000001/fix', 'https://127.1/fix',
    'https://localhost/fix', 'https://x.local/fix', 'https://x.internal/fix',
    'https://example.org:wrong/fix', 'https://[bad/fix',
    'https://example.org/\nfix', 'https://example.org/\\evil',
    'https://example.org/"onclick="evil', 'https://example.org/<script>', None,
])
def test_unsafe_optional_reference_does_not_destroy_advisory(url):
    io = FixtureIO([advisory(references=[{'type': 'FIX', 'url': url},
                                       {'type': 'FIX', 'url': 'https://example.org/fix'}])])
    result = monitor_security.check(SUBJECT, SETTINGS, io)
    assert result['status'] == 'ok'
    assert [fact['value'] for fact in facts(result, 'reference')] == ['https://example.org/fix']
    assert len(io.calls) == 3


def test_html_summary_remains_literal_plain_text():
    summary = '<img src=x onerror=alert(1)> **not markdown**'
    result = monitor_security.check(SUBJECT, SETTINGS, FixtureIO([advisory(summary=summary)]))
    assert facts(result, 'summary')[0]['value'] == summary
    monitor_model.validate_findings(result['findings'])


def test_optional_metadata_absence_is_not_negative_or_invented():
    result = monitor_security.check(SUBJECT, SETTINGS, FixtureIO([advisory()]))
    for code in ('summary', 'reference', 'cvss_vector', 'kev_added', 'kev_ransomware'):
        assert facts(result, code) == []
    result = monitor_security.check(SUBJECT, SETTINGS, FixtureIO([advisory()], [
        {'cveID': CVE, 'dateAdded': 'invalid', 'knownRansomwareCampaignUse': 'Unknown'}]))
    assert facts(result, 'kev_added') == []
    assert facts(result, 'kev_ransomware')[0]['value'] == 'Unknown'
    assert facts(result, 'kev_ransomware')[0]['value'] is not False


def test_malformed_optional_context_is_ignored_and_cvss_scoped_to_query():
    result = monitor_security.check(SUBJECT, SETTINGS, FixtureIO([advisory(
        summary={'html': 'ignored'}, references=[None, {'type': [], 'url': 'https://example.org/'}],
        severity=[None, {'type': [], 'score': VECTOR}, {'type': 'CVSS_V3', 'score': '<script>'},
                  {'type': 'CVSS_V2', 'score': VECTOR}, {'type': 'Urgent', 'score': '9.8'}],
        affected=[{'package': {'ecosystem': 'npm', 'name': 'other'},
                   'severity': [{'type': 'CVSS_V3', 'score': VECTOR}]},
                  {'package': SETTINGS, 'severity': [{'type': ' cvss_v3 ', 'score': VECTOR}]}])]))
    assert result['status'] == 'ok'
    assert facts(result, 'summary') == [] and facts(result, 'reference') == []
    assert len(facts(result, 'cvss_vector')) == 1
    assert facts(result, 'cvss_vector')[0]['key'] == 'CVSS_V3'


def test_duplicate_alias_payloads_and_provider_order_keep_changed_time(monkeypatch):
    reference = 'https://example.org/fix'
    entry = advisory(summary='TLS goroutines', severity=[{'type': 'CVSS_V3', 'score': VECTOR}],
                     references=[{'type': 'WEB', 'url': reference}, {'type': 'FIX', 'url': reference}])
    second = {**entry, 'id': CVE}
    io = FixtureIO([entry, second])
    proposed = {'status': 'pending', 'subject': SUBJECT, 'inputs': SETTINGS,
                'scope': 'current', 'fingerprint': 'same'}
    times = iter(['2026-09-25T00:00:00+00:00', '2026-09-25T06:00:00+00:00'])
    monkeypatch.setattr(state, 'utcnow', lambda: next(times))
    before = monitor.execute('security', proposed, io)
    assert before['status'] == 'ok'
    assert len(facts(before, 'reference')) == 1
    assert facts(before, 'reference')[0]['key'] == 'FIX'
    io.entries = [deepcopy(second), deepcopy(entry), deepcopy(entry)]
    for item in io.entries:
        item['references'].reverse()
        item['aliases'].reverse()
    after = monitor.execute('security', proposed, io, before)
    assert after['status'] == 'ok' and after['checked_at'] != before['checked_at']
    assert after['findings'] == before['findings']
    assert after['evidence_revision'] == before['evidence_revision']
    assert after['changed_at'] == before['changed_at']


def test_reference_and_summary_limits_report_omitted_information():
    entry = advisory(summary='x' * 800,
                     references=[{'type': 'FIX', 'url': f'https://example.org/fix/{n:02}'} for n in range(20)])
    result = monitor_security.check(SUBJECT, SETTINGS, FixtureIO([entry]))
    assert len(facts(result, 'summary')[0]['value']) == 601
    assert len(facts(result, 'reference')) == 12
    omitted = {f['key']: f['value'] for f in facts(result, 'truncated')}
    assert omitted == {'Summary characters omitted': 200, 'Additional references': 8}


def test_alias_group_fact_limit_is_explicit_and_deterministic():
    entries = [advisory(id=f'GHSA-fixture-{n:03}', summary=f'Original summary {n}') for n in range(200)]
    before = monitor_security.check(SUBJECT, SETTINGS, FixtureIO(entries))
    after = monitor_security.check(SUBJECT, SETTINGS, FixtureIO(list(reversed(entries))))
    assert before == after
    assert len(before['findings']) == 1 and len(before['findings'][0]['facts']) == 256
    assert any(f['key'] == 'Additional evidence fields' and f['value'] > 0
               for f in facts(before, 'truncated'))
    monitor_model.validate_findings(before['findings'])


def test_fetch_failure_retains_context_only_for_same_fingerprint(monkeypatch):
    proposed = {'status': 'pending', 'subject': SUBJECT, 'inputs': SETTINGS,
                'scope': 'current', 'fingerprint': 'same'}
    previous = monitor.execute('security', proposed, FixtureIO([advisory(summary='Provider summary')]))
    failed = monitor.execute('security', proposed, FixtureIO([], error=TimeoutError()), previous)
    assert failed['status'] == 'error'
    for key in ('findings', 'changed_at', 'checked_at', 'evidence_revision'):
        assert failed[key] == previous[key]
    changed = monitor.execute('security', {**proposed, 'fingerprint': 'different'},
                              FixtureIO([], error=TimeoutError()), previous)
    assert changed['status'] == 'error' and changed['findings'] == []
