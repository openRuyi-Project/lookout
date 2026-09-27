"""A distribution version is not necessarily the upstream release identity."""
from copy import deepcopy

import pytest

from tracker import config as cfg, monitor, presentation, state, version_status, view


def source(version='1.2.0', release='1.2.0-rc.2', name='widget'):
    return dict(version=version, revision='spec:fixture', origin='spec', error=None,
        fetched_at=state.utcnow(), native_query=dict(spec_sha256='a' * 64, context={'resolver': 7}),
        metadata=dict(version=version, sources=[dict(number=0,
            url=f'https://static.crates.io/crates/{name}/{release}/download#/source.tar.gz')]))


def prepare(config, snapshot, *, release='1.2.0-rc.2', target='1.2.0'):
    spec = source(release=release)
    spec.update(head='fixture', metadata_error=None)
    snapshot['specs'] = {'binutils': spec}
    rule = {'source': 'cratesio', 'cratesio': 'widget', 'include_regex': r'^1\.[0-9]+\.[0-9]+$'}
    config['native']['binutils'] = rule
    config['packages']['binutils'] = {}
    snapshot['bindings']['binutils'] = cfg.binding(config, 'binutils')
    snapshot['tracks']['binutils'] = state.success({}, dict(version=target, source=rule), state.utcnow())
    return spec


def test_prerelease_to_identical_rpm_base_is_an_upgrade(config, snapshot):
    prepare(config, snapshot)
    version = version_status.evaluate(snapshot, 'binutils')
    assert version.source['version'] == '1.2.0'
    assert version.subject['version'] == '1.2.0-rc.2'
    assert version.relation == 'outdated'
    rows, _ = view.project_monitors(snapshot)
    row = next(r for r in rows if r['name'] == 'binutils')
    data = row['monitors']['version']['data']
    assert data['current'] == data['latest'] == '1.2.0'
    assert data['source_release']['version'] == '1.2.0-rc.2'
    text = [v.text for v in presentation.version_value(row)]
    assert text == ['1.2.0', '(rc.2)', '→', '1.2.0']


@pytest.mark.parametrize('provider', ['security', 'license', 'yanked', 'requires'])
def test_all_registry_monitors_query_exact_source_release(config, snapshot, provider):
    prepare(config, snapshot)
    proposed = monitor.plan(config, snapshot, 'binutils', provider)
    assert proposed['status'] == 'pending'
    assert proposed['subject']['version'] == '1.2.0-rc.2'
    assert proposed['inputs'] == ({'ecosystem': 'crates.io', 'name': 'widget'}
                                  if provider == 'security' else {'cratesio': 'widget'})


def test_current_release_identity_does_not_require_an_update_rule(config, snapshot):
    prepare(config, snapshot)
    del config['native']['binutils']
    snapshot['native_ids'].remove('binutils')
    snapshot['bindings']['binutils'] = cfg.binding(config, 'binutils')
    for provider in ('security', 'yanked', 'requires'):
        proposed = monitor.plan(config, snapshot, 'binutils', provider)
        assert proposed['status'] == 'pending'
        assert proposed['subject']['version'] == '1.2.0-rc.2'


def test_native_rule_for_a_different_crate_cannot_relabel_source(config, snapshot):
    prepare(config, snapshot)
    config['native']['binutils']['cratesio'] = 'another'
    snapshot['tracks']['binutils']['source'] = deepcopy(config['native']['binutils'])
    version = version_status.evaluate(snapshot, 'binutils')
    assert version.relation == 'unknown'
    for provider in ('security', 'license', 'yanked', 'requires'):
        assert monitor.plan(config, snapshot, 'binutils', provider)['status'] == 'unsupported'


def test_new_config_identity_conflict_is_blocked_before_the_next_collection(config, snapshot):
    prepare(config, snapshot)
    config['native']['binutils'] = {'source': 'cratesio', 'cratesio': 'unrelated'}
    for provider in ('security', 'license', 'yanked', 'requires'):
        assert monitor.plan(config, snapshot, 'binutils', provider)['status'] == 'unsupported'


@pytest.mark.parametrize(('release', 'target', 'expected'), [
    ('1.2.0-rc-6', '1.2.0', 'outdated'),
    ('1.2.0-rc.2', '1.1.9', 'ahead'),
    ('1.2.0-rc.2', '1.3.0', 'outdated'),
    ('1.2.0+metadata', '1.2.0', 'current'),
    ('1.2.0-rc.2', '1.2.0-rc.3', 'unknown'),
])
def test_release_comparison_preserves_stable_policy(config, snapshot, release, target, expected):
    prepare(config, snapshot, release=release, target=target)
    assert version_status.evaluate(snapshot, 'binutils').relation == expected


@pytest.mark.parametrize('case', ['host', 'credentials', 'query', 'version', 'hash', 'resolver', 'duplicate'])
def test_untrusted_or_inconsistent_source_does_not_establish_a_release(case):
    from tracker import source_release

    value = source()
    url = value['metadata']['sources'][0]
    if case == 'host':
        url['url'] = url['url'].replace('static.crates.io', 'static.crates.io.evil')
    elif case == 'credentials':
        url['url'] = url['url'].replace('https://', 'https://secret@')
    elif case == 'query':
        url['url'] = url['url'].split('#')[0] + '?token=secret'
    elif case == 'version':
        value['metadata']['version'] = '2.0.0'
    elif case == 'hash':
        value['native_query']['spec_sha256'] = 'missing'
    elif case == 'resolver':
        value['native_query']['context']['resolver'] = 1
    else:
        value['metadata']['sources'].append(deepcopy(url))
    assert source_release.from_source(value) is None


def test_registry_parser_is_shared_by_runtime_and_offline_candidates():
    from tracker import discover_sources, source_release

    value = source()
    release = source_release.from_source(value)
    row = dict(current='1.2.0', source_url=value['metadata']['sources'][0]['url'].split('#')[0])
    entry = discover_sources.registry_entry(row)
    assert entry['cratesio'] == release.name == 'widget'
    assert entry['include_regex'] == r'^1\.[0-9]+\.[0-9]+$'
    assert 'use_pre_release' not in entry
