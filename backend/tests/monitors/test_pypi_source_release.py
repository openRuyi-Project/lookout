from copy import deepcopy

import pytest

from tests.monitors.test_source_release import prepare
from tracker.monitors import runner
from tracker.monitors.source import release
from tracker.monitors.version import candidates, compare


def pypi_source(config, snapshot, *, current='1.2.3~post4', upstream='1.2.3.post4', target='1.2.3.post4'):
    source = prepare(config, snapshot, target=target)
    source['version'] = source['metadata']['version'] = current
    source['metadata']['sources'][0]['url'] = (
        f'https://files.pythonhosted.org/packages/source/w/widget-name/widget_name-{upstream}.tar.gz')
    rule = {'source': 'pypi', 'pypi': 'Widget_Name'}
    config['native']['binutils'] = rule
    snapshot['tracks']['binutils']['source'] = deepcopy(rule)
    return source


@pytest.mark.parametrize('provider', ['security', 'yanked', 'license', 'requires'])
def test_shared_source_projection_preserves_rpm_display(config, snapshot, provider):
    pypi_source(config, snapshot, target='1.2.3.post5')
    result = runner.plan(config, snapshot, 'binutils', provider)
    assert result['status'] == 'pending'
    assert result['subject']['version'] == '1.2.3.post4'
    assert result['inputs'] == ({'ecosystem': 'PyPI', 'name': 'widget-name'} if provider == 'security'
                                else {'pypi': 'widget-name'})
    assert compare.evaluate(snapshot, 'binutils').source['version'] == '1.2.3~post4'


@pytest.mark.parametrize('target,expected', [('1.2.3.post4', 'current'), ('1.2.3.post5', 'outdated'),
                                           ('1.2.3', 'ahead'), ('unresolved', 'unknown')])
def test_observed_pep440_version_controls_comparison(config, snapshot, target, expected):
    pypi_source(config, snapshot, target=target)
    assert compare.evaluate(snapshot, 'binutils').relation == expected


@pytest.mark.parametrize('current', ['1.2.3', '1.2.4~post4', '1.2.3+git20260101.abcdef0', None])
def test_unproven_version_relationship_does_not_establish_release(config, snapshot, current):
    source = pypi_source(config, snapshot, current=current)
    assert release.from_source(source) is None


@pytest.mark.parametrize('change', ['host', 'credentials', 'query', 'wheel', 'escape', 'no_hash', 'stale_metadata'])
def test_untrusted_source_is_rejected(config, snapshot, change):
    source = pypi_source(config, snapshot)
    item = source['metadata']['sources'][0]
    if change == 'host':
        item['url'] = item['url'].replace('files.pythonhosted.org', 'files.pythonhosted.org.example.org')
    elif change == 'credentials':
        item['url'] = item['url'].replace('https://', 'https://user@')
    elif change == 'query':
        item['url'] += '?token=fixture'
    elif change == 'wheel':
        item['url'] = item['url'].replace('.tar.gz', '.whl')
    elif change == 'escape':
        item['url'] = item['url'].replace('/source/', '/%2e%2e/')
    elif change == 'no_hash':
        source['native_query']['spec_sha256'] = ''
    else:
        source['metadata']['version'] = '1.2.3'
    assert release.from_source(source) is None


def test_new_registry_must_not_become_a_cratesio_discovery_rule(config, snapshot):
    source = pypi_source(config, snapshot, current='1.2.3', upstream='1.2.3')
    assert candidates.registry_entry({'current': '1.2.3', 'source_url': source['metadata']['sources'][0]['url']}) is None


def test_identity_conflict_still_blocks_all_monitor_queries(config, snapshot):
    pypi_source(config, snapshot)
    config['native']['binutils']['pypi'] = 'different-project'
    for provider in ['security', 'yanked', 'license', 'requires']:
        assert runner.plan(config, snapshot, 'binutils', provider)['status'] == 'unsupported'


def test_unchanged_normal_release_reuses_query_fingerprint(config, snapshot):
    source = pypi_source(config, snapshot, current='1.2.3', upstream='1.2.3', target='1.2.4')
    projected = {p: runner.plan(config, snapshot, 'binutils', p)['fingerprint']
                 for p in ['security', 'yanked', 'license', 'requires']}
    source['metadata']['sources'] = []
    assert projected == {p: runner.plan(config, snapshot, 'binutils', p)['fingerprint'] for p in projected}


@pytest.mark.parametrize('version', ['1.2.3+git20260101.abcdef0', '1.2.3+downstream.1'])
def test_local_build_is_unsupported_without_network_or_stripping(version):
    from tracker.monitors import yanked
    from tracker.monitors.requires import monitor as requires

    class NoNetwork:
        def json(self, *args, **kwargs):
            pytest.fail('local versions cannot identify a PyPI release')

    for adapter in (yanked, requires):
        result = adapter.check({'version': version}, {'pypi': 'widget'}, NoNetwork())
        assert result['status'] == 'unsupported'
        assert result['findings'] == []
    assert release.from_url(f'https://files.pythonhosted.org/packages/source/w/widget/widget-{version}.tar.gz') is None


def test_local_pypi_version_uses_proven_commit_for_security_not_public_release():
    from tracker.monitors.security import monitor as security

    revision = {'repository': 'https://github.com/example/widget', 'commit': 'a' * 40}
    package = {'version': '1.2.3+git20260101.aaaaaaa',
               'identity': {'source': 'pypi', 'pypi': 'widget'}, 'source_commit': revision}
    assert security.inputs(package, None) == revision
    assert security.query_version(package['version'], 'PyPI') is False
    assert security.query_version('1.2.3.post4', 'PyPI') is True


@pytest.mark.parametrize('compact', [True, False])
@pytest.mark.parametrize('current', ['1.2.3~post4', '1.2.3.post4'])
def test_source_version_marker_keeps_rpm_and_query_identity_distinct(config, snapshot, compact, current):
    from tracker.presentation.values import version_value
    from tracker.readmodel import snapshot as view

    source = pypi_source(config, snapshot, current=current)
    rows, _ = view.project_monitors(snapshot)
    row = next(row for row in rows if row['name'] == 'binutils')
    values = version_value(row, compact=compact)
    expected = [current] if current == '1.2.3.post4' else [current, 'PyPI 1.2.3.post4']
    assert [value.text for value in values] == expected
    if len(values) > 1:
        assert values[1].href == source['metadata']['sources'][0]['url']
        assert values[1].tone == 'muted'
