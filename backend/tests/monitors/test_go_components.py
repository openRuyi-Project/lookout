from copy import deepcopy

import pytest

from tests.monitors.test_source_release import prepare
from tracker import state
from tracker.monitors import runner
from tracker.monitors.model import validate_findings
from tracker.monitors.requires import go, monitor
from tracker.providers import go_components


ROOT = 'https://github.com/example/bundle/archive/refs/tags/'
IDENTITY = {'source': 'go_proxy', 'url': 'https://proxy.golang.org/example.org/bundle/@latest'}
INCLUDE = {'go': {'include': [{'module': 'example.org/bundle/sibling',
                             'from_module': 'example.org/bundle/a', 'path': 'sibling'}]}}


def package():
    return {'version': '1.0.0', 'identity': IDENTITY, 'source_archives': [
        {'number': 0, 'url': ROOT + 'a/v1.0.0.tar.gz'},
        {'number': 1, 'url': ROOT + 'b/v2.0.0.tar.gz'},
    ]}


class IO:
    def __init__(self, broken=None):
        self.urls = []
        self.broken = broken

    def text(self, url, **kwargs):
        self.urls.append(url)
        module = url.split('/')[-2]
        if module == self.broken:
            raise OSError('unavailable')
        minimum = {'a': '1.9', 'b': '1.24', 'sibling': '1.26'}.get(module, '1.20')
        return f'module example.org/bundle/{module}\ngo {minimum}\ntoolchain go1.30\n'


def test_source_tags_discover_modules_and_keep_current_and_upgrade_separate():
    io = IO()
    result = monitor.check({'version': '1.0.0', 'target_version': '2.0.0'}, go.inputs(package(), None), io)
    assert result['status'] == 'partial'
    assert result['scope_checks']['current']['status'] == 'ok'
    assert result['scope_checks']['upgrade']['status'] == 'unsupported'
    assert len(io.urls) == 2
    assert all('/refs/tags/' in url for url in io.urls)
    finding, = result['findings']
    assert finding['requirement']['kind'] == 'build'
    assert finding['requirement']['constraint']['expression'] == '1.24'
    assert len(finding['facts']) == 4
    validate_findings(result['findings'])


def test_bundled_module_follows_anchor_not_source_number():
    before = go.inputs(package(), INCLUDE)
    changed = package()
    changed['source_archives'].reverse()
    for i, item in enumerate(changed['source_archives']):
        item['number'] = i + 5
        item['url'] += '#/renamed.tar.gz'
    assert go.inputs(changed, INCLUDE) == before
    io = IO()
    result = monitor.check({'version': '1.0.0'}, before, io)
    assert result['status'] == 'ok'
    assert result['findings'][0]['requirement']['constraint']['expression'] == '1.26'
    assert io.urls[-1].endswith('/refs/tags/a/v1.0.0/sibling/go.mod')


def test_new_and_removed_sources_change_inventory_and_recompute_minimum():
    current = package()
    before = go.inputs(current, None)
    current['source_archives'].append({'number': 9, 'url': ROOT + 'sibling/v3.0.0.tar.gz'})
    after = go.inputs(current, None)
    assert before != after
    result = monitor.check({'version': '1.0.0'}, after, IO())
    assert result['findings'][0]['requirement']['constraint']['expression'] == '1.26'
    current['source_archives'] = current['source_archives'][:1]
    reduced = monitor.check({'version': '1.0.0'}, go.inputs(current, None), IO())
    assert reduced['findings'][0]['requirement']['constraint']['expression'] == '1.9'


def test_version_and_archive_changes_invalidate_saved_query(config, snapshot, tmp_path):
    source = prepare(config, snapshot, release='1.2.0', target='3.0.0')
    config['native']['binutils'] = IDENTITY
    snapshot['tracks']['binutils']['source'] = IDENTITY
    source['metadata']['sources'] = package()['source_archives']
    config['packages']['binutils'] = {'monitors': {'requires': INCLUDE}}
    before = runner.plan(config, snapshot, 'binutils', 'requires')
    assert before['status'] == 'pending'
    snapshot['monitors'] = {'binutils': {'requires': before}}
    db = tmp_path / 'state.sqlite3'
    state.commit(db, snapshot)
    assert state.read(db)['monitors']['binutils']['requires']['inputs'] == before['inputs']
    source['native_query']['spec_sha256'] = 'b' * 64
    assert runner.plan(config, snapshot, 'binutils', 'requires')['fingerprint'] == before['fingerprint']
    source['metadata']['sources'][0]['url'] = ROOT + 'a/v1.1.0.tar.gz'
    after = runner.plan(config, snapshot, 'binutils', 'requires')
    assert after['fingerprint'] != before['fingerprint']
    assert not runner.same_scope(after, before, 'current')
    source['metadata_error'] = 'retained metadata'
    assert runner.plan(config, snapshot, 'binutils', 'requires')['status'] == 'error'


@pytest.mark.parametrize('case', ['fetch', 'missing_anchor', 'duplicate_module', 'wrong_include', 'missing_go'])
def test_incomplete_or_ambiguous_sources_do_not_publish_an_aggregate(case):
    inputs = go.inputs(package(), INCLUDE)

    class Failure(IO):
        def text(self, url, **kwargs):
            data = super().text(url, **kwargs)
            if case == 'missing_anchor':
                return data.replace('bundle/a', 'bundle/renamed')
            if case == 'duplicate_module':
                return data.replace('bundle/b', 'bundle/a')
            if case == 'wrong_include':
                return data.replace('bundle/sibling', 'bundle/other')
            if case == 'missing_go' and '/b/go.mod' in url:
                return 'module example.org/bundle/b\n'
            return data

    result = monitor.check({'version': '1.0.0'}, inputs, Failure('b' if case == 'fetch' else None))
    assert result['status'] in ('error', 'unsupported')
    assert result['findings'] == []


@pytest.mark.parametrize('suffix', ['../a/v1.0.0.tar.gz', 'a%2fb/v1.0.0.tar.gz',
                                   'a/main.tar.gz', 'v1.0.0.tar.gz', 'a/v1.0.0.tar.gz?token=secret'])
def test_unsafe_or_non_submodule_sources_are_not_discovery_candidates(suffix):
    assert go_components.source_tag(ROOT + suffix) is None


@pytest.mark.parametrize('url', [ROOT.replace('github.com', 'github.com.evil'),
                               ROOT.replace('https://', 'https://user@'),
                               ROOT.replace('/tags/', '/heads/')])
def test_discovery_requires_public_github_tag_archive(url):
    assert go_components.source_tag(url + 'a/v1.0.0.tar.gz') is None


@pytest.mark.parametrize('path', ['../a', '/a', 'a//b', 'a/..', 'a%2fb', 'a?x', 'a#x'])
def test_include_path_cannot_escape_source(path):
    configured = deepcopy(INCLUDE)
    configured['go']['include'][0]['path'] = path
    with pytest.raises(ValueError):
        go.inputs(package(), configured)


def test_explicit_single_module_and_non_go_packages_do_not_auto_discover():
    assert go.inputs(package(), {'go': 'example.org/explicit'}) == {'go': 'example.org/explicit'}
    value = package()
    value['identity'] = {'source': 'pypi', 'pypi': 'bundle'}
    assert go.inputs(value, None) is None
    with pytest.raises(ValueError):
        go.inputs(value, INCLUDE)


def test_duplicate_archive_aliases_do_not_duplicate_modules():
    value = package()
    value['source_archives'].append({'number': 4, 'url': value['source_archives'][0]['url'] + '#/alias.tar.gz'})
    assert go.inputs(value, None) == go.inputs(package(), None)


def test_component_budget_matches_finding_contract():
    value = package()
    value['source_archives'] = [{'number': i, 'url': ROOT + f'm{i}/v1.0.0.tar.gz'} for i in range(129)]
    with pytest.raises(ValueError, match='256-fact'):
        go.inputs(value, None)
    value['source_archives'].pop()
    result = monitor.check({'version': '1.0.0'}, go.inputs(value, None), IO())
    assert result['status'] == 'ok'
    assert len(result['findings'][0]['facts']) == 256
    validate_findings(result['findings'])


def test_module_identity_is_read_from_document_including_major_suffix():
    class Major(IO):
        def text(self, url, **kwargs):
            return super().text(url, **kwargs).replace('bundle/a', 'bundle/a/v2')
    result = monitor.check({'version': '1.0.0'}, go.inputs(package(), None), Major())
    assert result['status'] == 'ok'
    assert result['findings'][0]['facts'][0]['value'][0] == 'example.org/bundle/a/v2'


def test_unchanged_component_documents_reuse_shared_http_cache(tmp_path, monkeypatch):
    import httpx

    from tracker.providers.client import IO as ProviderClient

    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, text=IO().text(str(request.url)))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        owner = ProviderClient(tmp_path, client=client)
        monkeypatch.setattr(owner, 'wait_for_host', lambda *_: None)
        io = owner.for_hosts(go.HOSTS)
        value = package()
        assert monitor.check({'version': value['version']}, go.inputs(value, None), io)['status'] == 'ok'
        value['version'] = '1.1.0'
        value['source_archives'][0]['url'] = ROOT + 'a/v1.1.0.tar.gz'
        assert monitor.check({'version': value['version']}, go.inputs(value, None), io)['status'] == 'ok'
    assert len(requests) == 3
    assert sum('/b/v2.0.0/' in url for url in requests) == 1


@pytest.mark.parametrize('suffix', ['', '/v4', '/v10'])
def test_established_single_module_keeps_proxy_and_upgrade_support(suffix):
    value = package()
    value['source_archives'] = value['source_archives'][:1]
    value['identity'] = {'source': 'go_proxy', 'url': 'https://proxy.golang.org/example.org/bundle/a' + suffix + '/@latest'}
    assert go.inputs(value, None) == {'go': 'example.org/bundle/a' + suffix}
    value['source_archives'].append({'number': 1, 'url': ROOT + 'b/v2.0.0.tar.gz'})
    assert isinstance(go.inputs(value, None)['go'], dict)
