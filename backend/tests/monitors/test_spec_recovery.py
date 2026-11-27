# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Retry stored source inputs without claiming a successful remote refresh."""
import subprocess

import pytest

from tracker import collector, state
from tracker.monitors.source import git as spec_git
from tracker.monitors.source import rpm


@pytest.fixture
def repository(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init', '-qb', 'main')
    path = repo / 'SPECS/widget/widget.spec'
    path.parent.mkdir(parents=True)
    def commit(version):
        path.write_text(version)
        git('add', '.')
        git('-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')
        return git('rev-parse', 'HEAD')
    commit('1')
    config = {'collector': {}, 'spec': dict(repo=str(repo), url='https://example.invalid/source.git',
        branch='main', macro_package=None, changelog_limit=20, fetch_timeout_seconds=10)}
    clock = ['2026-01-01T00:00:00+00:00']
    monkeypatch.setattr(state, 'utcnow', lambda: clock[0])
    monkeypatch.setattr(spec_git, 'fetch', lambda *a, **k: (True, None))
    db = tmp_path / 'state.db'
    state.commit(db, state.empty())
    return config, db, commit, clock


def describe(data, macros=()):
    return {'metadata': {'name': 'widget', 'version': data.decode()}, 'metadata_error': None,
            'native_query': {'context': {'resolver': rpm.RESOLVER, 'additional_macros': []}}}


def test_failed_fetch_retries_parse_at_confirmed_head(repository, monkeypatch):
    config, db, commit, clock = repository
    first = collector.check_specs(config, db, describe)
    new_head = commit('2')
    clock[0] = '2026-01-02T00:00:00+00:00'
    failed = collector.check_specs(config, db, lambda *a, **k: {
        'metadata': None, 'metadata_error': 'native SPEC parse failed'})
    assert failed['specs']['widget']['metadata'] == first['specs']['widget']['metadata']
    assert failed['specs']['widget']['head'] != new_head
    monkeypatch.setattr(spec_git, 'fetch', lambda *a, **k: (False, 'git fetch timeout'))
    clock[0] = '2026-01-03T00:00:00+00:00'
    repaired = collector.check_specs(config, db, describe)
    fact = repaired['specs']['widget']
    assert fact['metadata']['version'] == '2' and fact['head'] == new_head
    assert fact['error'] is None
    assert fact['fetched_at'] == '2026-01-02T00:00:00+00:00'
    assert fact['attempted_at'] == clock[0]
    component = repaired['components']['spec_git']
    assert component['error'] == 'git fetch timeout'
    assert component['fetched_at'] == failed['components']['spec_git']['fetched_at']
    assert component['head'] == new_head and component['selected_packages'] == 1
    idle = collector.check_specs(config, db, lambda *a, **k: pytest.fail('already repaired'))
    assert idle['specs'] == repaired['specs']


def test_offline_parser_change_preserves_observation_time(repository, monkeypatch):
    config, db, _, clock = repository
    first = collector.check_specs(config, db, describe)
    monkeypatch.setattr(spec_git, 'fetch', lambda *a, **k: (False, 'git fetch exited 128 (authentication)'))
    monkeypatch.setattr(rpm, 'RESOLVER', rpm.RESOLVER + 1)
    clock[0] = '2026-01-02T00:00:00+00:00'
    repaired = collector.check_specs(config, db, describe)
    assert repaired['components']['spec_git']['mode'] == 'full'
    assert repaired['specs']['widget']['native_query']['context']['resolver'] == rpm.RESOLVER
    assert repaired['specs']['widget']['fetched_at'] == first['specs']['widget']['fetched_at']


@pytest.mark.parametrize('changed', ['head', 'origin', 'unconfirmed'])
def test_offline_parse_requires_confirmed_source(repository, monkeypatch, changed):
    config, db, commit, _ = repository
    first = collector.check_specs(config, db, describe)
    if changed == 'head':
        commit('2')
    elif changed == 'origin':
        config['spec']['url'] = 'https://example.invalid/other.git'
    else:
        first['components']['spec_git'].pop('fetched_at')
        state.commit(db, first)
    monkeypatch.setattr(spec_git, 'fetch', lambda *a, **k: (False, 'git fetch timeout'))
    actual = collector.check_specs(config, db, lambda *a, **k: pytest.fail('unconfirmed source'))
    assert actual['specs'] == first['specs']
    assert actual['components']['spec_git']['error'] == 'git fetch timeout'


def test_head_change_during_parse_does_not_publish_partial_revision(repository):
    config, db, commit, _ = repository
    first = collector.check_specs(config, db, describe)
    commit('2')
    def changed(data, macros=()):
        commit('3')
        return describe(data, macros)
    actual = collector.check_specs(config, db, changed)
    assert actual['specs'] == first['specs']
    assert actual['components']['spec_git']['head'] == first['components']['spec_git']['head']
    assert actual['components']['spec_git']['error'] == 'SPEC HEAD changed during parsing'


@pytest.mark.parametrize('online', [True, False])
def test_history_failure_does_not_block_current_spec_recovery(repository, monkeypatch, online):
    config, db, commit, clock = repository
    first = collector.check_specs(config, db, describe)
    head = commit('2')
    clock[0] = '2026-01-02T00:00:00+00:00'
    failed = collector.check_specs(config, db, lambda *a, **k: {
        'metadata': None, 'metadata_error': 'native SPEC parse failed'})
    history = spec_git.changelogs
    calls = []
    def unavailable_history(*args, **kwargs):
        calls.append(kwargs)
        return {}, 'git log timeout'
    monkeypatch.setattr(spec_git, 'changelogs', unavailable_history)
    monkeypatch.setattr(spec_git, 'fetch', lambda *a, **k: (
        (True, None) if online else (False, 'git fetch timeout')))
    clock[0] = '2026-01-03T00:00:00+00:00'
    repaired = collector.check_specs(config, db, describe)
    fact = repaired['specs']['widget']
    assert len(calls) == 1 and calls[0].get('names') is None
    assert fact['metadata']['version'] == '2'
    assert fact['head'] == head
    assert fact['changelog'] == first['specs']['widget']['changelog']
    assert fact['error'] is None
    assert fact['fetched_at'] == (clock[0] if online else failed['components']['spec_git']['fetched_at'])
    assert 'git log timeout' in repaired['components']['spec_git']['error']
    collector.check_specs(config, db, lambda *a, **k: pytest.fail('unchanged tree reparsed'))
    monkeypatch.setattr(spec_git, 'changelogs', history)
    completed = collector.check_specs(config, db, describe)
    assert completed['specs']['widget']['changelog'][0]['commit'] == head
    assert not completed['components']['spec_git'].get('history_error')
    assert completed['components']['spec_git']['error'] == (None if online else 'git fetch timeout')


def test_merge_only_change_is_reparsed_and_survives_next_poll(repository):
    from pathlib import Path
    config, db, _, _ = repository
    repo = Path(config['spec']['repo'])
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture',
                               '-c', 'user.email=test@example.invalid', *args],
                              capture_output=True, text=True, check=True).stdout.strip()
    git('branch', 'side')
    (repo / 'README').write_text('main')
    git('add', '.'); git('commit', '-qm', 'main documentation')
    collector.check_specs(config, db, describe)
    git('checkout', '-q', 'side')
    (repo / 'OTHER').write_text('side')
    git('add', '.'); git('commit', '-qm', 'side documentation')
    git('checkout', '-q', 'main')
    git('merge', '--no-ff', '--no-commit', 'side')
    (repo / 'SPECS/widget/widget.spec').write_text('2')
    git('add', '.'); git('commit', '-qm', 'merge resolution changes SPEC')
    merged = collector.check_specs(config, db, describe)
    assert merged['specs']['widget']['metadata']['version'] == '2'
    assert merged['specs']['widget']['head'] == git('rev-parse', 'HEAD')
    assert merged['components']['spec_git']['selected_packages'] == 1
    final = collector.check_specs(config, db, lambda *a, **k: pytest.fail('unchanged input'))
    assert final['specs']['widget']['metadata'] == merged['specs']['widget']['metadata']


def test_legacy_checkpoint_is_reconciled_once(repository):
    config, db, _, _ = repository
    current = collector.check_specs(config, db, describe)
    current['specs']['widget']['metadata']['version'] = 'incorrect old cached version'
    current['specs']['widget'].pop('input_fingerprint')
    current['components']['spec_git']['input_fingerprint'] = 'legacy-history-selector'
    state.commit(db, current)
    repaired = collector.check_specs(config, db, describe)
    assert repaired['specs']['widget']['metadata']['version'] == '1'
    assert repaired['components']['spec_git']['mode'] == 'full'
    collector.check_specs(config, db, lambda *a, **k: pytest.fail('already reconciled'))


def test_tree_diff_failure_does_not_advance_success(repository, monkeypatch):
    config, db, commit, clock = repository
    before = collector.check_specs(config, db, describe)
    commit('2'); clock[0] = '2026-01-02T00:00:00+00:00'
    monkeypatch.setattr(spec_git, 'changed_packages', lambda *a: (set(), 'tree unavailable'))
    after = collector.check_specs(config, db, lambda *a, **k: pytest.fail('unverified changes'))
    assert after['specs'] == before['specs']
    assert after['components']['spec_git']['error'] == 'tree unavailable'
    assert after['components']['spec_git']['fetched_at'] == before['components']['spec_git']['fetched_at']


def test_runtime_change_reparses_even_with_identical_git_tree(repository, monkeypatch):
    config, db, _, _ = repository
    monkeypatch.setattr(rpm, 'runtime_identity', lambda: 'image-one')
    collector.check_specs(config, db, describe)
    calls = []
    def updated(data, **kwargs):
        calls.append(data)
        return describe(data, **kwargs)
    monkeypatch.setattr(rpm, 'runtime_identity', lambda: 'image-two')
    actual = collector.check_specs(config, db, updated)
    assert calls == [b'1']
    assert actual['components']['spec_git']['mode'] == 'full'


def test_unreadable_runtime_retains_successful_metadata(repository, monkeypatch):
    config, db, _, _ = repository
    before = collector.check_specs(config, db, describe)
    def broken():
        raise rpm.MacroInputError('Native parser runtime identity unavailable')
    monkeypatch.setattr(rpm, 'runtime_identity', broken)
    after = collector.check_specs(config, db, describe)
    assert after['specs'] == before['specs']
    assert after['components']['spec_git']['error'] == 'Native parser runtime identity unavailable'
