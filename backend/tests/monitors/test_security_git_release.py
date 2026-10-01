"""Archive protocols supply identities; package names never supply repositories."""
import pytest

from tracker.monitors.security import monitor
from tracker.monitors.source.release import pinned_tag


def source(url, version='1.2.3'):
    return {'origin': 'spec', 'version': version,
            'metadata': {'version': version, 'sources': [{'number': 0, 'url': url}]},
            'native_query': {'spec_sha256': 'a' * 64, 'context': {'resolver': 10}}}


@pytest.mark.parametrize('url,tag', [
    ('https://github.com/example/component/archive/refs/tags/v1.2.3.tar.gz', 'v1.2.3'),
    ('https://github.com/example/component/archive/1.2.3/component-1.2.3.tar.gz', '1.2.3'),
    ('https://github.com/example/component/releases/download/R_1_2_3/component.tar.xz', 'R_1_2_3'),
    ('https://codeload.github.com/example/component/tar.gz/refs/tags/v1.2.3', 'v1.2.3'),
    ('https://gitlab.example.org/team/component/-/archive/v1.2.3/component-v1.2.3.tar.bz2', 'v1.2.3'),
])
def test_tag_identity_flows_to_the_provider_request(url, tag):
    data = pinned_tag(source(url))
    assert data['tag'] == tag
    if 'github' in url:
        assert data['repository'] == 'https://github.com/example/component'
    settings = monitor.inputs({'version': '1.2.3', 'source_tag': data}, None)
    assert settings == {'ecosystem': 'GIT', 'name': data['repository'], 'tag': tag}
    assert monitor.query({'version': '1.2.3'}, settings) == {
        'package': {'ecosystem': 'GIT', 'name': data['repository']}, 'version': tag}


@pytest.mark.parametrize('url', [
    'https://github.com/example/component/archive/main.tar.gz',
    'https://github.com/example/component/archive/v11.2.3.tar.gz',
    'https://github.com/example/component/archive/v1.2.4.tar.gz',
    'https://github.com/example/component/archive/' + 'a' * 40 + '.tar.gz',
    'https://github.com/example/component/archive/refs/heads/1.2.3.tar.gz',
    'https://user:token@github.com/example/component/archive/v1.2.3.tar.gz',
    'https://github.com/example/../component/archive/v1.2.3.tar.gz',
])
def test_ambiguous_or_mismatched_archive_does_not_become_a_release(url):
    assert pinned_tag(source(url)) is None


def test_unconfined_or_failed_source_cannot_supply_a_tag():
    data = source('https://github.com/example/component/archive/v1.2.3.tar.gz')
    data['native_query']['context']['resolver'] = 0
    assert pinned_tag(data) is None
    data['native_query']['context']['resolver'] = 10
    data['error'] = 'parse failed'
    assert pinned_tag(data) is None


def test_registry_and_reviewed_exception_keep_precedence():
    package = {'version': '1.2.3', 'source_tag': {'repository': 'https://github.com/example/component', 'tag': 'v1.2.3'},
               'identity': {'source': 'pypi', 'pypi': 'upstream-component'}}
    assert monitor.inputs(package, None) == {'ecosystem': 'PyPI', 'name': 'upstream-component'}
    assert monitor.inputs(package, {'vendor': 'fixture', 'product': 'component'}) == {
        'source': 'nvd', 'vendor': 'fixture', 'product': 'component'}


def test_git_fixed_events_are_bound_to_the_queried_repository():
    settings = {'ecosystem': 'GIT', 'name': 'https://github.com/example/component.git'}
    ranges = [{'type': 'GIT', 'repo': 'https://github.com/example/component', 'events': [{'fixed': 'a' * 40}]},
              {'type': 'GIT', 'repo': 'https://github.com/other/component', 'events': [{'fixed': 'b' * 40}]}]
    assert monitor.matching_ranges({'ranges': ranges}, settings) == ranges[:1]


def test_static_tag_cannot_query_an_older_release_after_the_package_upgrades():
    settings = {'ecosystem': 'GIT', 'name': 'https://github.com/example/component', 'tag': 'v1.2.3'}
    assert monitor.query({'version': '1.2.3'}, settings)['version'] == 'v1.2.3'
    assert monitor.query({'version': '1.2.4'}, settings) is None
    assert monitor.query({'version': '11.2.3'}, settings) is None


@pytest.mark.parametrize('mismatch', ['commit', 'repository', 'missing_source'])
def test_static_commit_cannot_outlive_the_corroborating_source(mismatch):
    settings = {'repository': 'https://github.com/example/component.git', 'commit': 'a' * 40}
    pinned = {'repository': 'https://github.com/example/component', 'commit': 'a' * 40}
    package = {'version': '0+git20260101.aaaaaaa', 'source_commit': pinned}
    assert monitor.inputs(package, settings) == settings
    if mismatch == 'commit':
        pinned['commit'] = 'b' * 40
    elif mismatch == 'repository':
        pinned['repository'] = 'https://github.com/example/other'
    else:
        package['source_commit'] = None
    with pytest.raises(ValueError):
        monitor.inputs(package, settings)


@pytest.mark.parametrize('url', ['http://example.org/repo', 'https://localhost/repo', 'https://127.0.0.1/repo'])
def test_private_or_non_https_repository_is_rejected(url):
    with pytest.raises(ValueError):
        monitor.query({'version': '1.2.3'}, {'ecosystem': 'GIT', 'name': url})


@pytest.mark.parametrize('host,path,repository', [
    ('github.com', 'team/pkg', 'https://github.com/team/pkg'),
    ('github.com', 'team/pkg.git', 'https://github.com/team/pkg'),
    ('github.com', 'team/subgroup/pkg', None),
    ('github.com', 'team/../pkg', None),
    ('github.com', 'team/./pkg', None),
    ('forge.example.org', 'team/subgroup/pkg', 'https://forge.example.org/team/subgroup/pkg'),
    ('forge.example.org', 'team//pkg', None),
    ('forge.example.org', 'team/pkg!', None),
])
def test_commit_and_tag_archives_share_repository_validation(host, path, repository):
    from tracker.monitors.source.release import pinned_revision

    commit = 'a' * 40
    tag = pinned_tag(source(f'https://{host}/{path}/archive/v1.2.3.tar.gz'))
    revision = pinned_revision(source(f'https://{host}/{path}/archive/{commit}.tar.gz',
                                     '0+git20260901.' + commit[:7]))
    if repository is None:
        assert tag is revision is None
    else:
        assert tag['repository'] == revision.repository == repository
