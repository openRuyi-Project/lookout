import copy
from pathlib import Path

import pytest

from tracker.monitors.version.onboarding import propose

CONFIG = Path(__file__).resolve().parents[3] / 'config/tracker.toml'


def snapshot(url, version='6.29.0'):
    return {'sources': {'test-component': {'version': version}}, 'specs': {'test-component': {
        'native_query': {'spec_sha256': 'a'*64, 'context': {'resolver': 7}},
        'metadata': {'version': version, 'sources': [{'number': 0, 'url': url}]}}}}


@pytest.mark.parametrize('url',[
    'https://download.kde.org.evil/stable/frameworks/6.29/karchive-6.29.0.tar.xz',
    'https://download.kde.org/stable/frameworks/5.29/karchive-6.29.0.tar.xz',
    'https://download.kde.org/stable/frameworks/6.29/karchive-6.28.0.tar.xz',
    'https://download.kde.org/unstable/frameworks/6.29/karchive-6.29.0.tar.xz'])
def test_no_unsafe_family_join(url):
    assert propose(CONFIG,'test-component',snapshot(url))['entry'] is None


def test_mismatched_native_version_refused():
    s=snapshot('https://example.org/x');s['sources']['test-component']['version']='6.28.0'
    with pytest.raises(ValueError):propose(CONFIG,'test-component',s)


def test_registry_component_no_prefix_guess():
    p=propose(CONFIG,'test-component',snapshot('https://static.crates.io/crates/cbindgen/0.29.4/download#/file.tar.gz','0.29.4'))
    assert p['entry']['cratesio']=='cbindgen'
    assert p['entry']['include_regex']==r'^0\.29\.[0-9]+$'


@pytest.mark.parametrize('origin', [
    'https://static.crates.io/crates/',
    'https://crates.io/api/v1/crates/',
])
@pytest.mark.parametrize('tail', ['0.29.4/download', 'upstream_name-0.29.4.crate'])
def test_registry_proposal_uses_source_identity_not_package_name(origin, tail):
    from tracker.monitors.version import candidates as discover_sources

    data = snapshot(origin + 'upstream_name/' + tail, '0.29.4')
    row = {'current': '0.29.4', 'spec_sha256': 'a' * 64}
    row.update(discover_sources.hints(row, data['specs']['test-component']))
    assert propose(CONFIG, 'test-component', data)['entry'] == discover_sources.registry_entry(row)
    assert discover_sources.registry_entry(row) == {
        'source': 'cratesio', 'cratesio': 'upstream_name', 'include_regex': r'^0\.29\.[0-9]+$',
    }


@pytest.mark.parametrize(('url', 'version'), [
    ('https://static.crates.io/crates/widget/1.2.3-rc.1/download', '1.2.3-rc.1'),
    ('https://static.crates.io/crates/widget/1.2.4/download', '1.2.3'),
    ('https://static.crates.io/crates/widget/another-1.2.3.crate', '1.2.3'),
    ('https://static.crates.io.evil/crates/widget/1.2.3/download', '1.2.3'),
    ('https://token@static.crates.io/crates/widget/1.2.3/download', '1.2.3'),
    ('https://static.crates.io/crates/widget/1.2.3/download?token=secret', '1.2.3'),
])
def test_registry_proposal_rejects_mismatch_or_invalid_rpm_version(url, version):
    assert propose(CONFIG, 'test-component', snapshot(url, version))['entry'] is None


def test_prerelease_archive_can_propose_a_formal_release_track():
    result = propose(CONFIG, 'test-component', snapshot(
        'https://static.crates.io/crates/widget/1.2.3-rc.1/download', '1.2.3'))
    assert result['entry'] == {
        'source': 'cratesio', 'cratesio': 'widget', 'include_regex': r'^1\.[0-9]+\.[0-9]+$',
    }
