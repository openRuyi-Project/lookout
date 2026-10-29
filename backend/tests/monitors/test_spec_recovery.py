# SPDX-FileCopyrightText: (C) 2026 openRuyi Project Contributors
# SPDX-License-Identifier: MulanPSL-2.0
"""Retry stored source inputs without claiming a successful remote refresh."""
import subprocess

import pytest

from tracker import collector, state
from tracker.monitors.source import git as spec_git, rpm


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
