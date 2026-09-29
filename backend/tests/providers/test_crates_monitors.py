from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import time

import httpx
import pytest

from tracker import state
from tracker.monitors import license as monitor_license, yanked as monitor_yanked
from tracker.monitors.requires import (
    compare as requirement_versions,
    model as requirements,
    monitor as monitor_requires,
)
from tracker.providers import cratesio as crates_metadata
from tracker.providers.client import IO


SUBJECT = {"name": "rust-not-an-identity", "version": "1.0.0", "target_version": "2.0.0"}
SETTINGS = {"cratesio": "widget"}


def response():
    return {"crate": {"id": "widget"}, "versions": [
        {"crate": "widget", "num": "1.0.0", "yanked": True, "license": "MIT", "rust_version": "1.80"},
        {"crate": "widget", "num": "2.0.0", "yanked": False, "license": "Apache-2.0", "rust_version": "1.82"},
    ]}


class RegistryIO:
    def __init__(self, data=None):
        self.data = response() if data is None else data

    def json(self, method, url, *, min_interval):
        assert method == 'GET' and url == 'https://crates.io/api/v1/crates/widget'
        assert min_interval == 1.0
        return deepcopy(self.data)


@pytest.mark.parametrize('adapter', [monitor_license, monitor_yanked, monitor_requires])
def test_crates_identity_is_reused_not_inferred_from_name(adapter):
    package = {'name': 'rust-wrong-name', 'identity': {'source': 'cratesio', 'cratesio': 'widget'}}
    assert adapter.inputs(package, None) == SETTINGS
    package['identity'] = {'source': 'regex', 'url': 'https://index.crates.io/wi/dg/widget'}
    assert adapter.inputs(package, None) == SETTINGS
    package['identity'] = {'source': 'regex', 'url': 'https://unrelated.org/widget'}
    assert adapter.inputs(package, None) is None
    assert adapter.inputs(package, SETTINGS) == SETTINGS


def test_one_registry_response_serves_three_monitors_and_version_lines(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        owner = IO(tmp_path, client=client)
        adapters = [monitor_license, monitor_yanked, monitor_requires]
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda a: a.check(SUBJECT, SETTINGS, owner.for_hosts(a.HOSTS)), adapters))
        assert len(calls) == 1
        assert [r['findings'][0]['label'] for r in results] == ['LicenseDiff', 'Yanked', 'Dependencies']
        assert all(r['status'] == 'ok' for r in results)
        assert 'https://github.com/Jingwiw/openRuyi-monitor' in calls[0].headers['User-Agent']
        other_run = IO(tmp_path, client=client)
        assert monitor_yanked.check({**SUBJECT, 'version': '2.0.0'}, SETTINGS, other_run.for_hosts({'crates.io'}))['findings'] == []
        assert len(calls) == 1


@pytest.mark.parametrize('case', ['project', 'release', 'missing', 'duplicate', 'versions'])
def test_reject_wrong_or_ambiguous_registry_identity(case):
    data = response()
    if case == 'project': data['crate']['id'] = 'different'
    elif case == 'release': data['versions'][0]['crate'] = 'different'
    elif case == 'missing': data['versions'][0]['num'] = '1.0.0+different'
    elif case == 'duplicate': data['versions'].append(data['versions'][0])
    else: data['versions'] = {}
    with pytest.raises(ValueError):
        crates_metadata.release('widget', '1.0.0', RegistryIO(data))


@pytest.mark.parametrize('declaration', [None, '', '>=1.80', '1.80-beta', '1.80.0.1', '01.80', 80])
def test_missing_or_invalid_minimum_is_not_an_empty_constraint(declaration):
    data = response(); data['versions'][0]['rust_version'] = declaration
    result = monitor_requires.check(SUBJECT, SETTINGS, RegistryIO(data))
    assert result['status'] == 'partial'
    assert result['scope_checks']['current']['status'] == 'unsupported'
    assert result['scope_checks']['current']['note']
    assert result['scope_checks']['upgrade'] == {'status': 'ok', 'note': None}
    finding, = result['findings']
    assert finding['scope'] == 'upgrade'
    assert finding['requirement']['constraint']['expression'] == '1.82'


def test_build_minimum_is_retained_as_upstream_fact_without_runtime_warning(snapshot):
    result = monitor_requires.check(SUBJECT, SETTINGS, RegistryIO())
    assert result['status'] == 'ok'
    current, target = result['findings']
    assert (current['scope'], target['scope']) == ('current', 'upgrade')
    for finding, expression in ((current, '1.80'), (target, '1.82')):
        fact = finding['requirement']
        assert (fact['dependency'], fact['kind'], fact['scheme']) == ('rust', 'build', 'numeric_minimum')
        assert fact['constraint']['expression'] == expression  # Preserve the provider's bare declaration.
        assert fact['constraint']['source'] == 'crates.io'
        assert fact['constraint']['url'] == 'https://crates.io/api/v1/crates/widget'
    snapshot['dependency_packages'] = {'rust': 'compiler-source'}
    snapshot['sources']['compiler-source'] = state.success({}, {'version': '1.0', 'srcmd5': 'hash'}, state.utcnow())
    assert requirements.project([{**finding, 'stale': False} for finding in result['findings']],
                                snapshot, datetime.now(timezone.utc)) == []


@pytest.mark.parametrize('version,expected', [('1.81.9', False), ('1.82', True), ('1.82.0', True), ('1.90.1', True)])
def test_numeric_minimum_comparison_is_not_python_coupled(version, expected):
    assert requirement_versions.satisfies('numeric_minimum', '1.82', version) == (expected, None)


def test_format_only_minimum_and_equivalent_spdx_do_not_create_work():
    data = response()
    data['versions'][0].update(rust_version='1.82.0', license='MIT OR Apache-2.0')
    data['versions'][1]['license'] = 'Apache-2.0 OR MIT'
    result = monitor_requires.check(SUBJECT, SETTINGS, RegistryIO(data))
    assert result['status'] == 'ok' and len(result['findings']) == 2
    current, target = (finding['requirement']['constraint']['expression'] for finding in result['findings'])
    assert (current, target) == ('1.82.0', '1.82')
    assert requirements.equivalent('numeric_minimum', current, target)
    assert monitor_license.check(SUBJECT, SETTINGS, RegistryIO(data))['findings'] == []


def test_unavailable_license_does_not_block_other_domains():
    data = response(); data['versions'][0]['license'] = None
    assert monitor_license.check(SUBJECT, SETTINGS, RegistryIO(data))['status'] == 'unsupported'
    assert monitor_yanked.check(SUBJECT, SETTINGS, RegistryIO(data))['status'] == 'ok'
    assert monitor_requires.check(SUBJECT, SETTINGS, RegistryIO(data))['status'] == 'ok'


@pytest.mark.parametrize('declaration', ['MIT/Apache-2.0', 'MIT / Apache-2.0', '(MIT/Apache-2.0)'])
def test_cargo_legacy_slash_is_or_not_a_license_change(declaration):
    data = response()
    data['versions'][0]['license'] = declaration
    data['versions'][1]['license'] = 'Apache-2.0 OR MIT'
    result = monitor_license.check(SUBJECT, SETTINGS, RegistryIO(data))
    assert result['status'] == 'ok' and result['findings'] == []


def test_cargo_normalization_preserves_raw_evidence_and_and_semantics():
    data = response()
    data['versions'][0]['license'] = 'MIT/Apache-2.0'
    data['versions'][1]['license'] = 'MIT AND Apache-2.0'
    result = monitor_license.check(SUBJECT, SETTINGS, RegistryIO(data))
    assert result['status'] == 'ok'
    finding, = result['findings']
    assert finding['title'] == 'MIT OR Apache-2.0 → MIT AND Apache-2.0'
    assert [fact['value'] for fact in finding['facts']] == ['MIT/Apache-2.0', 'MIT AND Apache-2.0']
    assert all(fact['source'] == 'crates.io' for fact in finding['facts'])


@pytest.mark.parametrize('declaration', [
    'MIT/', '/MIT', 'MIT//Apache-2.0', 'MIT/something unknown',
    '/'.join(['MIT'] * 65), '(' * 17 + 'MIT/ISC' + ')' * 17,
])
def test_cargo_slash_compatibility_keeps_invalid_input_and_budget_checks(declaration):
    data = response()
    data['versions'][0]['license'] = declaration
    result = monitor_license.check(SUBJECT, SETTINGS, RegistryIO(data))
    assert result['status'] == 'unsupported' and result['findings'] == []


def test_rate_limit_is_shared_between_workers_but_not_cache_hits():
    starts = []

    def handler(request):
        starts.append(time.monotonic())
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        owner = IO(client=client)
        def read(i): return owner.for_hosts({'crates.io'}).json('GET', f'https://crates.io/{i}', min_interval=0.025)
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(read, range(3)))
        read(0)
        assert len(starts) == 3
        assert all(b - a >= 0.020 for a, b in zip(starts, starts[1:]))


@pytest.mark.parametrize('status', [429, 503])
def test_retry_after_suppresses_new_requests_on_that_host_only(status):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, headers={'Retry-After': '3600'})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        owner = IO(client=client)
        with pytest.raises(httpx.HTTPStatusError):
            owner.json('GET', 'https://crates.io/first', min_interval=1)
        with pytest.raises(ValueError, match='cooldown'):
            owner.json('GET', 'https://crates.io/second', min_interval=1)
        assert calls == ['https://crates.io/first']
        with pytest.raises(httpx.HTTPStatusError):
            owner.json('GET', 'https://pypi.org/unrelated')
        assert len(calls) == 2


@pytest.mark.parametrize('version', ['1.82~rc1', '1:1.82', '1.82-4', '1.82+vendor', '1.82-nightly'])
def test_comparison_never_guesses_rpm_or_prerelease_normalization(version):
    assert requirement_versions.satisfies('numeric_minimum', '1.82', version) == (None, 'unsupported_version')
