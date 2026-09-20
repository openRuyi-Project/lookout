# SPDX-FileCopyrightText: (C) 2026 Institute of Software, Chinese Academy of Sciences (ISCAS)
# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""refresh_specs: the single-clone core driven by one changelog map. Metadata is
re-parsed only where a package's head commit changed (or a prior parse failed); every
other package refreshes its changelog and reuses stored metadata. Fakes, no repo."""
import pytest
from tracker import collector, spec_git, native_spec, state


@pytest.fixture
def config():
    return {'spec': {'repo': '/r', 'macro_package': None,
                     'changelog_limit': 20, 'fetch_timeout_seconds': 300}}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(spec_git, 'read_spec', lambda repo, name, git='git': b'spec-' + name.encode())
    monkeypatch.setattr(spec_git, 'read_macros', lambda repo, pkg, git='git': [])


def cl(commit, subject='s'):
    return {'commit': commit, 'date': '2026-01-01T00:00:00+00:00', 'author': 'A',
            'subject': subject, 'signed_off_by': []}


def test_first_run_parses_all(config):
    logs = {'bash': [cl('cB')], 'gcc': [cl('cG')]}
    calls = []
    def describe(spec, macros=()):
        calls.append(spec); return {'metadata': {'name': spec.decode()}, 'metadata_error': None}
    specs = collector.refresh_specs(config, {}, ['bash', 'gcc'], logs, 'now', describe)
    assert sorted(calls) == [b'spec-bash', b'spec-gcc']
    assert specs['bash']['head'] == 'cB' and specs['gcc']['head'] == 'cG'


def test_unchanged_head_skips_parse_but_refreshes_changelog(config):
    logs = {'bash': [cl('cB2', 'newer'), cl('cB1')]}
    calls = []
    def describe(spec, macros=()):
        calls.append(spec); return {'metadata': {'name': 'x'}, 'metadata_error': None}
    old = {'bash': {'head': 'cB2', 'metadata': {'name': 'bash'}, 'changelog': [cl('cB2', 'newer')], 'error': None}}
    old['bash']['native_query'] = {'context': {'resolver': native_spec.RESOLVER, 'additional_macros': []}}
    specs = collector.refresh_specs(config, old, ['bash'], logs, 'now', describe)
    assert calls == []                                     # head unchanged -> no parse
    assert specs['bash']['metadata'] == {'name': 'bash'}   # reused
    assert len(specs['bash']['changelog']) == 2            # changelog still refreshed


def test_changed_head_reparses(config):
    logs = {'bash': [cl('cB2', 'bump')]}
    calls = []
    def describe(spec, macros=()):
        calls.append(spec); return {'metadata': {'name': 'bash', 'version': '9'}, 'metadata_error': None}
    old = {'bash': {'head': 'cB1', 'metadata': {'name': 'bash'}, 'changelog': [], 'error': None}}
    specs = collector.refresh_specs(config, old, ['bash'], logs, 'now', describe)
    assert calls == [b'spec-bash'] and specs['bash']['metadata']['version'] == '9'
    assert specs['bash']['head'] == 'cB2'


def test_prior_error_retried_even_if_head_unchanged(config):
    logs = {'bash': [cl('cB')]}
    calls = []
    def describe(spec, macros=()):
        calls.append(spec); return {'metadata': {'name': 'bash'}, 'metadata_error': None}
    old = {'bash': {'head': 'cB', 'metadata': None, 'error': 'prior failure'}}
    specs = collector.refresh_specs(config, old, ['bash'], logs, 'now', describe)
    assert calls == [b'spec-bash'] and specs['bash']['error'] is None


def test_parse_failure_keeps_changelog_and_marks(config):
    logs = {'bad': [cl('c9', 'x')]}
    describe = lambda spec, macros=(): {'metadata': None, 'metadata_error': 'cannot parse'}
    specs = collector.refresh_specs(config, {}, ['bad'], logs, 'now', describe)
    assert specs['bad']['head'] == 'c9'
    assert specs['bad']['changelog'][0]['commit'] == 'c9'
    assert specs['bad']['error'] == 'cannot parse'


def test_package_without_history_has_none_head(config):
    describe = lambda spec, macros=(): {'metadata': {'name': 'x'}, 'metadata_error': None}
    specs = collector.refresh_specs(config, {}, ['nohist'], {}, 'now', describe)
    assert specs['nohist']['head'] is None


def test_disabled_source_returns_unchanged():
    cfg = {'spec': {'repo': None, 'macro_package': None, 'changelog_limit': 20, 'fetch_timeout_seconds': 300}}
    old = {'bash': {'head': 'c'}}
    assert collector.refresh_specs(cfg, old, ['bash'], {}, 'now') is old


@pytest.mark.parametrize('context', [None, {}, {'resolver': -1, 'additional_macros': []},
    {'resolver': native_spec.RESOLVER, 'additional_macros': [{'sha256': 'old'}]}])
def test_parser_or_macro_change_invalidates_same_head(config, context):
    calls = []
    provenance = {'context': {'resolver': native_spec.RESOLVER, 'additional_macros': []}}
    def describe(spec, macros=()):
        calls.append(spec)
        return {'metadata': {'name': 'bash'}, 'metadata_error': None, 'native_query': provenance}
    old = {'bash': {'head': 'same', 'metadata': {'name': 'bash'}, 'error': None}}
    if context is not None:
        old['bash']['native_query'] = {'context': context}
    result = collector.refresh_specs(config, old, ['bash'], {'bash': [cl('same')]}, 'now', describe)
    assert calls == [b'spec-bash']
    assert result['bash']['native_query'] == provenance


def test_spec_phase_publishes_interval_without_rewriting_other_freshness(config):
    config['spec']['interval_seconds'] = 7200
    old = state.empty()
    old.update(obs_stale_after_seconds=300, stale_after_seconds=86400)
    merged = collector.merge_specs(config, old, {}, None, 'now')
    assert merged['spec_interval_seconds'] == 7200
    assert merged['obs_stale_after_seconds'] == 300
    assert merged['stale_after_seconds'] == 86400
