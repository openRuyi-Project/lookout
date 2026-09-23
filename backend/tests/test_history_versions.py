"""Bounded, build-bound RPM header fallback; successful observation dates."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from xml.etree import ElementTree as ET
import pytest
from tracker import collector, obs, state, view
from test_core import FakeOBS

FIXTURE = Path(__file__).parent / 'fixtures/accounts-qml-builds.json'


def success(name='binutils', count='13', end='1788813623', digest='new-binutils'):
    data = (f'<jobhistlist><jobhist package="{name}" rev="6" bcnt="{count}" '
            f'code="succeeded" srcmd5="{digest}" versrel="0.7+git20231216.MACRO-6" endtime="{end}"/></jobhistlist>')
    return obs.last_successes(data.encode())[name]


def fileinfo(target, name='binutils', fact=None, version='0.7+git20231216.05e79eb'):
    fact = fact or success(name)
    release = '6.' + fact['build_identity']['bcnt'] + '.or'
    stamp = int(datetime.fromisoformat(fact['time']).timestamp()) + 1
    return (f'<fileinfo filename="source.src.rpm"><version>{version}</version><release>{release}</release>'
            f'<arch>src</arch><disturl>obs://private/openruyi/{target["repository"]}/{fact["srcmd5"]}-{name}</disturl>'
            f'<mtime>{stamp}</mtime></fileinfo>').encode()


def test_real_accounts_qml_header_version_for_each_target():
    for case in json.loads(FIXTURE.read_text()):
        target = {k:case[k] for k in ('repository', 'architecture')}
        fact = obs.last_successes(case['history_xml'].encode())['accounts-qml-module']
        assert fact['version'] is None
        filename = ET.fromstring(case['fileinfo_xml']).get('filename')
        resolved = obs.binary_success_version(case['fileinfo_xml'].encode(), filename, 'openruyi', target, 'accounts-qml-module', fact)
        assert resolved['version'] == '0.7+git20231216.05e79eb'
        assert resolved['binary_provenance']['srcmd5'] == fact['srcmd5']
        assert resolved['binary_provenance']['repository'] == target['repository']
        assert resolved['version_basis'] == 'OBS RPM fileinfo/version'


@pytest.mark.parametrize('before,after', [
    ('new-binutils', 'other-source'), ('/openruyi/', '/other-project/'),
    ('/riscv64/', '/other-repository/'), ('-binutils<', '-other-flavor<'),
    ('6.13.or', '6.130.or'), ('6.13.or', '6.14.or'),
    ('<arch>src</arch>', '<arch>aarch64</arch>'),
    ('0.7+git20231216.05e79eb', '0.7+git20231216.MACRO'),
    ('source.src.rpm', 'other.src.rpm'),
])
def test_wrong_artifact_cannot_fill_history(config, before, after):
    target = config['targets'][0]
    data = fileinfo(target).replace(before.encode(), after.encode())
    with pytest.raises(ValueError):
        obs.binary_success_version(data, 'source.src.rpm', 'openruyi', target, 'binutils', success())


def test_history_build_identity_required_before_any_network(config):
    class NoNetwork:
        def get(self, path):
            raise AssertionError('identity-free history tried network')
    fact = success(); fact.pop('build_identity')
    with pytest.raises(ValueError, match='identity'):
        obs.resolve_success_version(NoNetwork(), 'openruyi', config['targets'][0], 'binutils', fact)


def test_binary_selection_is_bounded_and_unambiguous():
    assert obs.success_binary(b'<binaryversionlist><binary name="one.src.rpm"/><binary name="one.x86_64.rpm"/></binaryversionlist>', 'x86_64') == 'one.src.rpm'
    for data in (b'<binaryversionlist/>', b'<binaryversionlist><binary name="one.src.rpm"/><binary name="two.src.rpm"/></binaryversionlist>', b'<binaryversionlist><binary name="../bad.src.rpm"/></binaryversionlist>'):
        with pytest.raises(ValueError):obs.success_binary(data, 'x86_64')


class VersionOBS(FakeOBS):
    def __init__(self, config, names=('binutils',)):
        super().__init__(config, names=names, new_hash='new-binutils')
        self.requests = []; self.failed_binary = False; self.count = '13'; self.end = '1788813623'
    def get(self, path):
        self.requests.append(path)
        if '_jobhistory' in path:
            entries = ''.join(f'<jobhist package="{name}" rev="6" bcnt="{self.count}" code="succeeded" '
                f'srcmd5="new-{name}" versrel="0.7+git20231216.MACRO-6" endtime="{self.end}"/>' for name in self.names)
            return ('<jobhistlist>' + entries + '</jobhistlist>').encode()
        if '?view=binaryversions' in path:
            if self.failed_binary:raise TimeoutError()
            return b'<binaryversionlist><binary name="source.src.rpm"/></binaryversionlist>'
        if '?view=fileinfo' in path:
            target = next(t for t in self.config['targets'] if '/'+t['repository']+'/'+t['architecture']+'/' in path)
            name = path.split('/')[5]
            # Distinct architecture versions are valid; no cross-target sharing.
            version = '1.0.' + target['id']
            return fileinfo(target, name, success(name, self.count, self.end, 'new-'+name), version)
        return super().get(path)


def test_target_versions_cached_by_success_identity_not_source_version(config, snapshot):
    now = datetime.now(timezone.utc); client = VersionOBS(config)
    first = collector.collect(config, snapshot, client, now.isoformat())
    facts = first['builds']['binutils']
    assert {k:v['last_success']['version'] for k,v in facts.items()} == {t['id']:'1.0.'+t['id'] for t in config['targets']}
    assert first['sources']['binutils']['version'] == '3.11.0'  # never used as fallback
    assert sum('view=fileinfo' in p for p in client.requests) == 3
    client.requests.clear(); client.failed_binary = True
    second = collector.collect(config, first, client, (now+timedelta(seconds=301)).isoformat())
    assert not any('view=fileinfo' in p or 'view=binaryversions' in p for p in client.requests)
    assert second['builds']['binutils']['rva23']['last_success'] == facts['rva23']['last_success']
    # Same source hash, different completed build: do not transfer old RPM VERSION.
    client.count = '14'; client.end = '1788814623'
    third = collector.collect(config, second, client, (now+timedelta(seconds=602)).isoformat())
    for fact in third['builds']['binutils'].values():
        assert fact['last_success']['version'] is None
        assert fact['last_success']['build_identity']['bcnt'] == '14'
        assert 'TimeoutError' in fact['last_success']['version_error']


def test_fallback_requests_bounded_fair_and_retries_delayed(config, snapshot, monkeypatch):
    monkeypatch.setattr(collector, 'HISTORY_VERSION_BATCH_SIZE', 2)
    names = ('a', 'b', 'c'); client = VersionOBS(config, names=names)
    snapshot['builds'] = {name:{t['id']:{'last_success':success(name, digest='new-'+name)} for t in config['targets']} for name in names}
    now = datetime.now(timezone.utc); client.failed_binary = True
    collector.refresh_success_versions(config, snapshot, client, now.isoformat())
    assert len(client.requests) == 2
    first_attempts = {(n,t) for n,values in snapshot['builds'].items() for t,f in values.items() if f.get('version_attempted_at')}
    client.requests.clear(); client.failed_binary = False
    collector.refresh_success_versions(config, snapshot, client, (now+timedelta(seconds=60)).isoformat())
    assert len(client.requests) == 4
    successes = {(n,t) for n,values in snapshot['builds'].items() for t,f in values.items() if f['last_success'].get('version')}
    assert len(successes) == 2 and successes.isdisjoint(first_attempts)


def test_no_fallback_network_for_resolved_or_identity_free_history(config, snapshot):
    client = VersionOBS(config)
    collector.refresh_success_versions(config, snapshot, client, state.utcnow())
    assert client.requests == []
    for values in snapshot['builds'].values():
        for fact in values.values():fact['last_success'] = {**success(), 'version':'1.2'}
    collector.refresh_success_versions(config, snapshot, client, state.utcnow())
    assert client.requests == []


def test_successful_observation_dates_do_not_use_attempts(snapshot):
    old = '2026-01-01T00:00:00+00:00'; recent = '2026-02-01T00:00:00+00:00'
    snapshot['components']['builds'] = {'fetched_at':old, 'attempted_at':recent, 'error':'timeout'}
    snapshot['components']['nvchecker'] = {'fetched_at':old, 'attempted_at':recent, 'error':'timeout'}
    snapshot['tracks']['widget@3']['fetched_at'] = old
    snapshot['tracks']['widget@3']['attempted_at'] = recent
    snapshot['builds']['foo3:tools']['rva23']['fetched_at'] = old
    rows, collection = view.project(snapshot)
    row = next(r for r in rows if r['name']=='foo3')
    assert collection['obs_updated_at'] == old and collection['upstream_updated_at'] == old
    assert row['upstream_updated_at'] == old
    assert row['builds'][0]['updated_at'] == old
    assert row['builds'][0]['flavors'][1]['updated_at'] == old
    snapshot['builds']['foo3:tools']['rva23'].pop('fetched_at')
    snapshot['components'].pop('source_index')
    rows, collection = view.project(snapshot)
    assert collection['obs_updated_at'] is None
    assert next(r for r in rows if r['name']=='foo3')['builds'][0]['updated_at'] is None
    assert next(r for r in rows if r['name']=='untracked')['upstream_updated_at'] is None


@pytest.mark.parametrize('value', [None, '', 'not-a-date', '2026-01-01T00:00:00'])
def test_missing_invalid_or_naive_success_time_is_unknown(value):
    assert view.observed_at([{'fetched_at':value}]) is None


def test_partial_build_result_keeps_prior_successful_collection_time(config, snapshot):
    prior = snapshot['components']['builds']['fetched_at']
    class PartialOBS(VersionOBS):
        def get(self, path):
            data = super().get(path)
            if path.endswith('/_result'):
                root = ET.fromstring(data); root.remove(root[1]); return ET.tostring(root)
            return data
    new = collector.refresh_builds(config, snapshot, PartialOBS(config), (datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat())
    assert new['components']['builds']['fetched_at'] == prior
    assert new['components']['builds']['error']


@pytest.mark.parametrize('mtime', ['1788815382', '', 'unavailable', '-1'])
def test_artifact_publication_time_is_not_build_completion(config, mtime):
    target = config['targets'][0]
    fact = success()
    data = fileinfo(target).replace(b'1788813624', mtime.encode())
    actual = obs.binary_success_version(data, 'source.src.rpm', 'openruyi', target, 'binutils', fact)
    assert actual['version'] == '0.7+git20231216.05e79eb'
    assert actual['binary_provenance']['mtime'] == (int(mtime) if mtime.isdigit() else None)
    assert fact['time'] == '2026-09-07T20:40:23+00:00'


def test_real_trinity_delayed_publication_keeps_matching_success_version():
    case = json.loads((FIXTURE.parent / 'trinity-build.json').read_text())
    fact = case['success']
    target = {'repository': 'rva20', 'architecture': 'riscv64'}
    root = ET.fromstring(case['fileinfo_xml'])
    actual = obs.binary_success_version(case['fileinfo_xml'].encode(), root.get('filename'), 'openruyi', target, 'trinity', fact)
    assert actual['version'] == '1.9+git20260225.294c465'
    assert actual['binary_provenance']['release'] == '2.17.or20'
    assert actual['binary_provenance']['srcmd5'] == fact['srcmd5']
    assert actual['binary_provenance']['mtime'] - datetime.fromisoformat(fact['time']).timestamp() == 1759
    assert fact['time'] == '2026-09-04T11:25:45+00:00'
