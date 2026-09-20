from pathlib import Path
import copy
import pytest
from tracker.onboarding import propose

CONFIG = Path(__file__).resolve().parents[2] / 'config/tracker.toml'


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
