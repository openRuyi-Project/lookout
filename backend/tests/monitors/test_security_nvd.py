"""Synthetic NVD transport/range records; no test pins real CVE coverage counts."""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from tracker.monitors.security import monitor, nvd


SETTINGS = {'source': 'nvd', 'vendor': 'fixture', 'product': 'component'}
SUBJECT = {'version': '1.2.3'}
CVE = 'CVE-2026-12345'


def record(identity=CVE):
    return {'id': identity, 'vulnStatus': 'Analyzed',
            'descriptions': [{'lang': 'en', 'value': 'Synthetic provider assertion'}],
            'configurations': [{'nodes': [{'cpeMatch': [{
                'vulnerable': True, 'criteria': 'cpe:2.3:a:fixture:component:*:*:*:*:*:*:*:*',
                'versionStartIncluding': '1.0', 'versionEndExcluding': '1.3'}]}]}],
            'references': [{'url': 'https://example.org/fix', 'tags': ['Patch']}]}


class IO:
    def __init__(self, pages=None):
        self.pages = pages if pages is not None else [[record()]]
        self.requests = []

    def json(self, method, url, body=None, *, min_interval=0):
        self.requests.append((method, url, min_interval))
        if 'nvd.nist.gov' in url:
            index = len([r for r in self.requests if 'nvd.nist.gov' in r[1]]) - 1
            return {'totalResults': sum(map(len, self.pages)),
                    'startIndex': sum(map(len, self.pages[:index])),
                    'vulnerabilities': [{'cve': deepcopy(r)} for r in self.pages[index]]}
        if 'cisa.gov' in url:
            return {'vulnerabilities': []}
        if 'first.org' in url:
            return {'data': []}
        pytest.fail('Unexpected provider: ' + url)


def test_exact_cpe_version_is_sent_to_nvd_without_local_version_guessing():
    io = IO()
    result = nvd.read(SUBJECT, SETTINGS, io)
    params = parse_qs(urlsplit(io.requests[0][1]).query, keep_blank_values=True)
    assert params['cpeName'] == ['cpe:2.3:a:fixture:component:1.2.3:*:*:*:*:*:*:*']
    assert params['isVulnerable'] == [''] and params['noRejected'] == ['']
    assert io.requests[0][2] >= 6
    assert result[0]['id'] == CVE
    assert '>=' in result[0]['cpe_matches'][0] and '< 1.3' in result[0]['cpe_matches'][0]


def test_nvd_evidence_has_its_own_provenance_and_no_invented_fixed_version():
    result = monitor.check(SUBJECT, SETTINGS, IO())
    assert result['status'] == 'ok'
    finding = result['findings'][0]
    assert finding['id'] == CVE and finding['label'] == 'Advisory'
    assert not {'severity', 'resolution', 'detail'} & finding.keys()
    assert any(f.get('code') == 'cpe_match' and f['source'] == 'NVD' for f in finding['facts'])
    assert any(f.get('code') == 'reference' and f['source'] == 'NVD' for f in finding['facts'])
    assert not any(f['source'] == 'OSV' or f.get('code') == 'fixed_events' for f in finding['facts'])


def test_complete_pagination_and_empty_result():
    io = IO([[record()], [record('CVE-2026-54321')]])
    assert len(nvd.read(SUBJECT, SETTINGS, io)) == 2
    assert parse_qs(urlsplit(io.requests[1][1]).query)['startIndex'] == ['1']
    assert nvd.read(SUBJECT, SETTINGS, IO([[]])) == []


@pytest.mark.parametrize('mode', ['vendor', 'part', 'not_vulnerable', 'negated', 'missing'])
def test_unrelated_or_negative_cpe_cannot_become_a_finding(mode):
    data = record()
    configuration = data['configurations'][0]
    match = configuration['nodes'][0]['cpeMatch'][0]
    if mode == 'vendor':
        match['criteria'] = match['criteria'].replace(':fixture:', ':other:')
    elif mode == 'part':
        match['criteria'] = match['criteria'].replace('2.3:a:', '2.3:o:')
    elif mode == 'not_vulnerable':
        match['vulnerable'] = False
    elif mode == 'negated':
        configuration['negate'] = True
    else:
        data['configurations'] = []
    with pytest.raises(ValueError):
        nvd.normalize(data, SETTINGS)


@pytest.mark.parametrize('version', ['0+git20260101.abcdef', '%version', '1:*:*', None, ''])
def test_unresolved_or_snapshot_version_is_not_coerced_to_a_release(version):
    assert nvd.read({'version': version}, SETTINGS, IO()) is None


@pytest.mark.parametrize('settings', [
    {'vendor': 'fixture:other', 'product': 'component'},
    {'vendor': 'fixture', 'product': 'component', 'part': '*'},
    {'vendor': 'fixture', 'product': 'component', 'command': 'run'},
])
def test_identity_injection_is_rejected(settings):
    with pytest.raises(ValueError):
        nvd.cpe(settings, '1.2.3')


def test_pagination_drift_is_an_error_not_partial_success():
    class Changed(IO):
        def json(self, *args, **kwargs):
            response = super().json(*args, **kwargs)
            if len(self.requests) == 2:
                response['totalResults'] += 1
            return response
    with pytest.raises(ValueError):
        nvd.read(SUBJECT, SETTINGS, Changed([[record()], [record('CVE-2026-54321')]]))


def test_nvd_failure_retries_more_slowly_than_osv_without_disabling_the_heartbeat():
    policy = monitor.refresh(SUBJECT, SETTINGS, {})
    assert policy.interval_seconds == 21600
    assert policy.retry_seconds == 900 and policy.delay(2) == 1800
    assert monitor.refresh(SUBJECT, {'ecosystem': 'PyPI', 'name': 'fixture'}, {}).retry_seconds == 300
