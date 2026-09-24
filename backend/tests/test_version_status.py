from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tracker import config as cfg, monitor, state, version_status, view


@pytest.mark.parametrize('case,expected', [
    ('upgrade', 'outdated'), ('same', 'current'), ('older', 'ahead'),
    ('not_applicable', 'not_applicable'), ('incomparable', 'unknown'),
    ('untracked', 'untracked'), ('source_error', 'unknown'), ('source_expired', 'unknown'),
    ('upstream_error', 'unknown'), ('upstream_expired', 'unknown'), ('invalid', 'unknown'),
])
def test_one_decision_drives_view_and_upgrade_jobs(case, expected, config, snapshot, monkeypatch):
    now = datetime.now(timezone.utc)
    binding = config['packages'].setdefault('binutils', {})
    source, target = snapshot['sources']['binutils'], snapshot['tracks']['binutils']
    if case == 'same':
        target['version'] = source['version']
    elif case == 'older':
        target['version'] = '1.0'
    elif case == 'not_applicable':
        binding['not_applicable'] = True
    elif case == 'incomparable':
        binding['comparable'] = False
    elif case == 'untracked':
        binding['compare'] = None
    elif case.startswith('source_'):
        source.update({'error': 'offline'} if case.endswith('error') else
                      {'fetched_at': (now - timedelta(days=2)).isoformat()})
    elif case.startswith('upstream_'):
        target.update({'error': 'offline'} if case.endswith('error') else
                      {'fetched_at': (now - timedelta(days=2)).isoformat()})
    elif case == 'invalid':
        target['version'] = 'MACRO'
    calls = []
    monkeypatch.setitem(monitor.REGISTRY, 'fixture', SimpleNamespace(
        VERSION=1, SCOPE='upgrade', HOSTS=set(), inputs=lambda *args: {},
        check=lambda *args: calls.append(1) or {'status': 'ok', 'findings': [], 'note': None}))
    version = version_status.evaluate(snapshot, 'binutils', now)
    row = next(r for r in view.project(snapshot, now)[0] if r['name'] == 'binutils')
    plan = monitor.plan(config, snapshot, 'binutils', 'fixture', version=version)
    monitor.execute('fixture', plan, SimpleNamespace(for_hosts=lambda hosts, **kwargs: None))
    assert version.relation == row['relation'] == expected
    assert bool(calls) == version.upgrading == (expected == 'outdated')
    assert plan['subject']['version'] == row['current']
    assert plan['subject']['target_version'] == row['latest']


def test_config_change_waits_for_saved_policy_before_upgrade(config, snapshot, monkeypatch):
    snapshot['bindings'] = deepcopy(snapshot['bindings'])
    config['packages']['binutils'] = {'compare': 'widget@4'}
    monkeypatch.setitem(monitor.REGISTRY, 'fixture', SimpleNamespace(
        VERSION=1, SCOPE='upgrade', HOSTS=set(), inputs=lambda *args: {},
        check=lambda *args: pytest.fail('unpublished policy cannot start a check')))
    result = monitor.check(config, snapshot, 'binutils', 'fixture', None)
    assert result['status'] == 'unsupported'
    assert result['subject']['target_version'] == '3.10.0'
    snapshot['bindings']['binutils'] = cfg.binding(config, 'binutils')
    result = monitor.plan(config, snapshot, 'binutils', 'fixture')
    assert result['status'] == 'pending'
    assert result['subject']['target_version'] == '4.2.0'


def test_batch_resolves_versions_once_per_package(config, snapshot, monkeypatch):
    calls = []
    compare = state.compare

    def count(*args):
        calls.append(args)
        return compare(*args)

    monkeypatch.setattr(state, 'compare', count)
    versions = version_status.evaluate_all(snapshot)
    for provider in ('eol', 'security', 'license'):
        for name, version in versions.items():
            monitor.plan(config, snapshot, name, provider, version=version)
    assert len(calls) == len(snapshot['sources'])
