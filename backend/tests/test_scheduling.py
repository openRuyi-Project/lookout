"""Module-owned timing, bounded retries, and change-triggered work."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from tracker import collector, config as cfg, monitor, native_spec, obs, spec_git, state
from tracker.monitor_io import IO
from tracker.schedule import Schedule


def test_due_uses_inputs_attempts_and_bounded_failure_backoff():
    policy = Schedule(3600, 60, 300)
    at = datetime(2026, 9, 24, tzinfo=timezone.utc)
    old = {'fingerprint': 'same', 'status': 'ok', 'checked_at': at.isoformat()}
    assert not policy.due(old, 'same', at + timedelta(seconds=3599))
    assert policy.due(old, 'same', at + timedelta(seconds=3600))
    assert policy.due(old, 'changed', at)
    assert [policy.delay(n) for n in range(6)] == [3600, 60, 120, 240, 300, 300]
    old.update(status='error', failures=2, attempted_at=(at + timedelta(seconds=1)).isoformat())
    assert not policy.due(old, 'same', at + timedelta(seconds=120))
    assert policy.due(old, 'same', at + timedelta(seconds=121))
    old['attempted_at'] = 'invalid'
    assert policy.due(old, 'same', at)
    old['attempted_at'] = (at + timedelta(hours=1)).isoformat()
    assert policy.due(old, 'same', at)  # A clock correction must not freeze work forever.


@pytest.mark.parametrize('values', [
    {'interval_seconds': 0}, {'retry_seconds': True}, {'retry_seconds': 3601},
    {'interval_seconds': 604801}, {'arbitrary_script': 'run me'},
])
def test_invalid_refresh_settings_are_rejected(values):
    with pytest.raises(ValueError):
        Schedule(3600).override(values)


def test_module_policy_config_precedence_and_legacy_compatibility(config, snapshot):
    proposed = monitor.plan(config, snapshot, 'binutils', 'security')
    assert monitor.refresh_policy('security', proposed, {}, {}).interval_seconds == 21600
    assert monitor.refresh_policy('license', proposed, {}, {}).interval_seconds == 43200
    options = monitor.settings({'monitors': {'interval_seconds': 1800,
        'refresh': {'security': {'interval_seconds': 600, 'retry_seconds': 60}}}})
    security = monitor.refresh_policy('security', proposed, {}, options)
    assert security == Schedule(600, 60, 3600)
    assert monitor.refresh_policy('eol', proposed, {}, options).interval_seconds == 1800
    for values in ({'absent': {}}, {'security': {'interval_seconds': 86400}}):
        with pytest.raises(ValueError):
            monitor.settings({'monitors': {'refresh': values}})


def test_new_module_programs_its_schedule_without_a_runner_branch(config, snapshot, monkeypatch, tmp_path):
    at = datetime.now(timezone.utc).replace(microsecond=0)
    clock = [at]
    monkeypatch.setattr(monitor, 'datetime', SimpleNamespace(now=lambda tz: clock[0]))
    monkeypatch.setattr(state, 'utcnow', lambda: clock[0].isoformat())
    calls = Counter()
    for provider, interval in [('fast', 60), ('slow', 3600)]:
        def check(subject, inputs, io, provider=provider):
            calls[provider] += 1
            return {'status': 'ok', 'findings': [], 'note': None}
        monkeypatch.setitem(monitor.REGISTRY, provider, SimpleNamespace(
            VERSION=1, HOSTS=set(), inputs=lambda subject, configured: {}, check=check,
            refresh=lambda subject, inputs, previous, interval=interval:
                Schedule(interval if subject['version'] == '3.9.0' else 120)))
    snapshot['sources'] = {'binutils': snapshot['sources']['binutils']}
    config.update(monitors={'enabled': ['fast', 'slow']}, config_digest='test', nv_digest='test')
    monkeypatch.setattr(monitor.cfg, 'require_unchanged', lambda config, path: None)
    io = SimpleNamespace(for_hosts=lambda *args, **kwargs: None)
    db = tmp_path / 'snapshot.db'
    state.commit(db, snapshot)
    first = monitor.collect(config, 'unused', db, io=io)
    idle = monitor.collect(config, 'unused', db, io=io)
    assert idle['generation'] == first['generation']
    clock[0] += timedelta(seconds=61)
    second = monitor.collect(config, 'unused', db, io=io)
    assert calls == {'fast': 2, 'slow': 1}
    for provider in calls:
        assert first['monitors']['binutils'][provider]['changed_at'] == second['monitors']['binutils'][provider]['changed_at']
    second['sources']['binutils'].update(version='3.9.1', srcmd5='new-source')
    state.commit(db, second)
    third = monitor.collect(config, 'unused', db, io=io)
    assert calls == {'fast': 3, 'slow': 2}
    assert third['monitors']['binutils']['slow']['subject']['version'] == '3.9.1'
    clock[0] += timedelta(seconds=121)
    monitor.collect(config, 'unused', db, io=io)
    assert calls == {'fast': 4, 'slow': 3}


def test_retry_counters_survive_runs_and_reset_on_success_or_new_input(config, snapshot, monkeypatch):
    proposed = monitor.plan(config, snapshot, 'binutils', 'security')
    proposed.update(status='pending', inputs={})
    status = ['partial']
    def check(*args):
        if status[0] == 'error':
            raise OSError('offline')
        return {'status': status[0], 'findings': [], 'note': None}
    monkeypatch.setitem(monitor.REGISTRY, 'test', SimpleNamespace(HOSTS=set(), check=check))
    io = SimpleNamespace(for_hosts=lambda *args, **kwargs: None)
    first = monitor.execute('test', proposed, io)
    status[0] = 'error'
    failed = monitor.execute('test', proposed, io, first)
    assert failed['failures'] == 2 and failed['checked_at'] == first['checked_at']
    changed = monitor.execute('test', {**proposed, 'fingerprint': 'new'}, io, failed)
    assert changed['failures'] == 1 and changed['checked_at'] is None
    status[0] = 'ok'
    assert monitor.execute('test', proposed, io, failed)['failures'] == 0


def test_bad_module_policy_is_local(config, snapshot, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setitem(monitor.REGISTRY, 'fixture', SimpleNamespace(
        VERSION=1, HOSTS=set(), inputs=lambda *args: {},
        refresh=lambda subject, inputs, previous: 'invalid' if subject['name'] == 'binutils' else Schedule(3600),
        check=lambda subject, *args: calls.append(subject['name']) or
            {'status': 'ok', 'findings': [], 'note': None}))
    config.update(monitors={'enabled': ['fixture']}, config_digest='test', nv_digest='test')
    monkeypatch.setattr(monitor.cfg, 'require_unchanged', lambda config, path: None)
    db = tmp_path / 'snapshot.db'
    state.commit(db, snapshot)
    result = monitor.collect(config, 'unused', db, io=SimpleNamespace(for_hosts=lambda *args, **kwargs: None))
    assert result['monitors']['binutils']['fixture']['status'] == 'error'
    assert set(calls) == {'foo3', 'foo4', 'untracked'}


def test_disk_cache_cannot_hide_a_shorter_module_refresh(monkeypatch, tmp_path):
    from tracker import monitor_io
    now = [1000.0]
    monkeypatch.setattr(monitor_io.time, 'time', lambda: now[0])
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={'revision': len(calls)})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        owner = IO(tmp_path, client=client)
        short = owner.for_hosts({'example.org'}, max_age=60)
        assert short.json('GET', 'https://example.org/') == {'revision': 1}
        now[0] += 59
        assert short.json('GET', 'https://example.org/') == {'revision': 1}
        now[0] += 1
        new_run = IO(tmp_path, client=client)
        assert new_run.for_hosts({'example.org'}, max_age=60).json('GET', 'https://example.org/') == {'revision': 2}
        assert new_run.for_hosts({'example.org'}, max_age=3600).json('GET', 'https://example.org/') == {'revision': 2}
    assert len(calls) == 2


@pytest.mark.parametrize('phase,function,owner', [
    ('builds', 'collect_builds', 'builds'), ('specs', 'check_specs', 'spec_git'),
    ('obs-metadata', 'collect_obs', 'inventory'), ('monitors', None, None),
])
def test_unrelated_failure_does_not_change_a_lanes_retry(config, snapshot, monkeypatch, capsys, phase, function, owner):
    snapshot['components']['nvchecker']['error'] = 'different provider'
    monkeypatch.setattr(collector.cfg, 'load', lambda _: config)
    monkeypatch.setattr(collector if function else monitor, function or 'collect', lambda *args: snapshot)
    monkeypatch.setattr(sys, 'argv', ['collector', '--config', 'unused', '--db', 'unused', '--only', phase])
    assert collector.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['errors'] == [] and result['collection_errors'] == ['different provider']
    if owner:
        snapshot['components'][owner] = {'error': 'this lane failed'}
        assert collector.main() == 2
        assert json.loads(capsys.readouterr().out)['errors'] == ['this lane failed']


def test_git_heartbeat_reuses_unchanged_head_and_invalidates_real_changes(config, snapshot, monkeypatch, tmp_path):
    repo = tmp_path / 'git'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout
    git('init', '-q', '-b', 'main')
    package = repo / 'SPECS/binutils/binutils.spec'
    package.parent.mkdir(parents=True)
    def commit(text):
        package.write_text(text)
        git('add', '.')
        git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'SPEC fixture')
    commit('Version: 1.0\n')
    config['spec'] = dict(repo=str(repo), macro_package=None, changelog_limit=20,
                          fetch_timeout_seconds=30, interval_seconds=60)
    snapshot['sources'] = {'binutils': snapshot['sources']['binutils']}
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    # Only the remote boundary is substituted; HEAD, traversal and reads use real Git.
    monkeypatch.setattr(spec_git, 'fetch', lambda *args, **kwargs: (True, None))
    calls = []
    changelogs = spec_git.changelogs
    def history(*args, **kwargs):
        calls.append('history')
        return changelogs(*args, **kwargs)
    monkeypatch.setattr(spec_git, 'changelogs', history)
    def describe(data, macros=()):
        calls.append('parse')
        return {'metadata': {'name': 'binutils', 'version': data.decode().split()[1]},
                'native_query': {'context': {'resolver': native_spec.RESOLVER, 'additional_macros': []}}}
    first = collector.check_specs(config, db, describe)
    second = collector.check_specs(config, db, describe)
    assert calls == ['history', 'parse']
    assert first['specs']['binutils']['head'] == second['specs']['binutils']['head']
    assert second['builds'] == snapshot['builds']
    commit('Version: 2.0\n')
    changed = collector.check_specs(config, db, describe)
    assert changed['specs']['binutils']['metadata']['version'] == '2.0'
    assert calls == ['history', 'parse', 'history', 'parse']
    config['spec']['changelog_limit'] = 10
    collector.check_specs(config, db, describe)
    assert calls[-1] == 'history' and calls.count('history') == 3
    old = deepcopy(state.read(db)['specs'])
    monkeypatch.setattr(spec_git, 'fetch', lambda *args, **kwargs: (False, 'offline'))
    failed = collector.check_specs(config, db, describe)
    assert failed['specs'] == old
    assert failed['components']['spec_git']['error'] == 'offline'
    assert calls.count('history') == 3


def test_shipped_fast_polling_retains_a_bulk_request_budget():
    root = Path(__file__).resolve().parents[2]
    config = cfg.load(root / 'config/tracker.toml')
    assert config['collector']['build_interval_seconds'] == 15
    assert config['spec']['interval_seconds'] == 60
    assert obs.polling(config)['builds'] == Schedule(15, 30, 300)
    assert spec_git.polling(config['spec']) == Schedule(60, 120, 900)
