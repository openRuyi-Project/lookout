# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Discovery cannot turn a name guess or failed request into tracking coverage."""
from copy import deepcopy
import json
from pathlib import Path
import tomllib

import pytest

from tracker import config, discover, nv


def candidate():
    return {'name': 'widget', 'homepage': 'https://github.com/team/widget', 'current': '1.9.17p2'}


def response():
    return {'total_items': 1, 'items': [{'id': 123, 'name': 'widget',
            'homepage': 'http://github.com/Team/Widget/',
            'versions': ['2.0rc1', '1.9.17p2', '1.9.17'],
            'stable_versions': ['1.9.17p2', '1.9.17']}]}


def snapshot():
    return {'sources': {'widget': {'version': '1.9.17p2'}},
            'specs': {'widget': {'metadata': {'name': 'widget', 'version': '1.9.17p2',
                       'url': 'https://github.com/team/widget'},
                       'head': 'fixed-head', 'native_query': {'spec_sha256': 'a' * 64}}}}


def test_verified_identity_uses_provider_order_not_largest_number():
    found = discover.match(candidate(), response())
    assert found['reason'] is None
    assert found['expected_version'] == '1.9.17p2'
    assert found['entry'] == {'source': 'jq',
        'url': 'https://release-monitoring.org/api/v2/versions/?project_id=123',
        'filter': 'first(.stable_versions[])', 'prefix': 'v'}
    assert not any(v in str(found['entry']) for v in ('1.9.17', '2.0rc1'))


@pytest.mark.parametrize('change', [
    lambda d: d['items'][0].update(homepage='https://github.com/other/widget'),
    lambda d: d['items'][0].update(homepage='https://github.com/team/widget-extra'),
    lambda d: d['items'][0].update(versions=['0.1', '0.2']),
    lambda d: d['items'][0].update(stable_versions=[]),
    lambda d: d['items'][0].update(stable_versions=['release-2.0']),
    lambda d: d['items'][0].update(id=True),
    lambda d: d['items'][0].update(versions=None),
])
def test_identity_history_release_or_schema_mismatch_stays_untracked(change):
    data = response(); change(data)
    assert discover.match(candidate(), data)['reason'] == 'no_unique_identity_and_version_match'


def test_same_homepage_multiple_projects_is_ambiguous():
    data = response(); item = deepcopy(data['items'][0]); item['id'] = 456
    data['items'].append(item); data['total_items'] = 2
    assert discover.match(candidate(), data)['matching_ids'] == [123, 456]


@pytest.mark.parametrize('data', [{}, [], {'items': [], 'total_items': 2}])
def test_truncated_search_never_looks_unique(data):
    assert discover.match(candidate(), data)['reason'] == 'incomplete_project_search'


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'https://token@github.com/team/widget',
    'https://github.com/team/widget?token=secret', 'https://github.com/team/widget#readme',
    'https://github.com/team/widget/tree/main/subproject', 'https://github.com/team',
    'https://github.com:8000/team/widget', 'https://github.com/team/%2e%2e', None])
def test_url_identity_is_conservative(url):
    assert discover.identity_url(url) is None


def test_normalization_does_not_merge_non_github_path_case():
    assert discover.identity_url('http://www.example.org/Widget/') == 'example.org/Widget'
    assert discover.identity_url('https://github.com/Team/Widget.git/') == 'github.com/team/widget'
    assert discover.identity_url('https://example.org/widget') != discover.identity_url('https://example.org/Widget')


def test_shared_repo_compat_packages_require_explicit_line_mapping():
    data = snapshot(); data['specs']['widget-compat'] = deepcopy(data['specs']['widget'])
    rows = discover.candidates({'native': {}, 'packages': {}}, data)
    assert rows[0]['reason'] == 'shared_homepage_requires_mapping_review'


@pytest.mark.parametrize('cfg', [
    {'native': {'widget': {}}, 'packages': {}},
    {'native': {}, 'packages': {'widget': {'compare': 'different-track'}}},
    {'native': {}, 'packages': {'widget': {'compare': None}}},
    {'native': {}, 'packages': {'widget': {'not_applicable': True}}},
])
def test_operator_authority_is_not_overridden(cfg):
    assert discover.candidates(cfg, snapshot()) == []


@pytest.mark.parametrize(('mutation', 'reason'), [
    (lambda d: d['specs']['widget'].update(metadata=None), 'spec_metadata_unverified'),
    (lambda d: d['specs']['widget'].update(error='failed'), 'spec_metadata_unverified'),
    (lambda d: d['specs']['widget'].update(native_query={}), 'spec_metadata_unverified'),
    (lambda d: d['sources']['widget'].update(version='2.0'), 'obs_spec_version_disagrees'),
    (lambda d: d['sources']['widget'].update(error='failed'), 'obs_spec_version_disagrees'),
])
def test_unverified_local_facts_are_not_identity_proof(mutation, reason):
    data = snapshot(); mutation(data)
    assert discover.candidates({'native': {}, 'packages': {}}, data)[0]['reason'] == reason


def test_append_preserves_operator_rules_and_rejects_collision():
    text = '# operator comment\n[existing]\nsource="pypi"\npypi="example"\n\n'
    rule = discover.match(candidate(), response())['entry']
    output = discover.append_entries(text, {'new"name': rule})
    assert output.startswith(text)
    assert tomllib.loads(output) == {**tomllib.loads(text), 'new"name': rule}
    with pytest.raises(ValueError, match='overwrite'):
        discover.append_entries(text, {'existing': rule})


def test_verification_reuses_native_selected_cli_without_relocating_keyfile(tmp_path, monkeypatch):
    native = tmp_path / 'native.toml'; native.write_text('[__config__]\nkeyfile="keys.toml"\n[existing]\nsource="pypi"\n')
    import hashlib
    cfg = {'nvpath': str(native), 'native': {'existing': {'source': 'pypi'}},
           'nv_digest': hashlib.sha256(native.read_bytes()).hexdigest()}
    row = {**candidate(), **discover.match(candidate(), response())}
    before = native.read_bytes()
    def run(actual, previous, now, tracks):
        assert actual['nvpath'] == str(native)
        assert actual['native']['existing'] == cfg['native']['existing']
        assert tracks == ['widget'] and previous == {}
        return {'widget': {'version': '1.9.17p2'}}, None
    monkeypatch.setattr(nv, 'run', run)
    assert discover.verify(cfg, [row])[0]['widget']['version'] == '1.9.17p2'
    assert native.read_bytes() == before
    native.write_text(before.decode() + '# changed\n')
    with pytest.raises(ValueError, match='changed'):
        discover.verify(cfg, [row])


def test_cli_only_emits_actually_verified_rules_and_no_live_write(tmp_path, monkeypatch):
    import hashlib
    native = tmp_path / 'native.toml'; native.write_text('[existing]\nsource="pypi"\npypi="existing"\n')
    cfg = {'nvpath': str(native), 'native': {'existing': {'source': 'pypi', 'pypi': 'existing'}},
           'nv_digest': hashlib.sha256(native.read_bytes()).hexdigest(), 'packages': {}}
    data = snapshot(); data['generation'] = 42
    monkeypatch.setattr(config, 'load', lambda p, **kwargs: cfg)
    monkeypatch.setattr(discover.state, 'read', lambda p: data)
    monkeypatch.setattr(discover, 'fetch_project', lambda n: {'body': response()})
    entry = discover.match(candidate(), response())['entry']
    fact = {'version': '1.9.17p2', 'error': None, 'fetched_at': '2026-09-20T00:00:00Z',
            'configuration_fingerprint': config.track_fingerprint(entry)}
    monkeypatch.setattr(discover, 'verify', lambda c, p: ({'widget': fact}, 'nvchecker exited 2'))
    before = native.read_bytes(); out = tmp_path / 'output'
    assert discover.main(['--config', 'unused', '--db', 'unused', '--output', str(out), '--verify']) == 0
    report = json.loads((out / 'report.json').read_text())
    assert out.stat().st_mode & 0o777 == 0o700
    assert report['verified'] == 1 and report['live_state_modified'] is False
    assert tomllib.loads((out / 'candidate.nvchecker.toml').read_text())['widget'] == entry
    assert native.read_bytes() == before
    fact['error'] = 'request timeout'
    out2 = tmp_path / 'failed'
    discover.main(['--config', 'unused', '--db', 'unused', '--output', str(out2), '--verify'])
    assert json.loads((out2 / 'report.json').read_text())['verified'] == 0
    assert tomllib.loads((out2 / 'candidate.nvchecker.toml').read_text()) == tomllib.loads(before.decode())
    out3 = tmp_path / 'shadow'
    discover.main(['--config', 'unused', '--db', 'unused', '--output', str(out3)])
    assert not (out3 / 'candidate.nvchecker.toml').exists()


def test_reviewed_release_policy_uses_upstream_contract_not_pinned_version():
    import jq
    native = __import__('tracker.version_rules',fromlist=['load']).load(Path(__file__).resolve().parents[2] / 'config/versions/groups.toml').entries
    rule = native['bind']
    payload = {'stable_versions': ['9.23.1', '9.22.2', '9.21.26', '9.20.29']}
    assert jq.compile(rule['filter']).input_value(payload).first() == '9.22.2'
    payload['stable_versions'] = ['9.25.3', '9.24.4', '9.22.2']
    assert jq.compile(rule['filter']).input_value(payload).first() == '9.24.4'
    assert native['safeint'] == {'source': 'github', 'github': 'dcleblanc/SafeInt',
                                 'use_latest_release': True, 'prefix': 'v'}
    assert native['keybinder']['url'].endswith('project_id=13401')  # Source archive identifies keybinder-3.0, not the ambiguous sibling1506
