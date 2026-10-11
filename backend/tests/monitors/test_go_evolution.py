"""Go input changes invalidate only the observations they affect."""
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from tests.monitors.test_source_release import prepare
from tracker import state
from tracker.monitors import runner
from tracker.monitors.requires import monitor
from tracker.monitors.requires.model import Requirement, project, unsatisfied
from tracker.monitors.source import release
from tracker.monitors.version import compare
from tracker.presentation.requires import requirement_values
from tracker.presentation.values import version_value
from tracker.readmodel import snapshot as view


def setup(config, snapshot, version='1.0.1+patch7', tag='v1.0.1-patch-7'):
    source = prepare(config, snapshot, target='1.1.0')
    source['version'] = source['metadata']['version'] = version
    source['metadata']['sources'][0]['url'] = f'https://github.com/example/module/archive/refs/tags/{tag}.tar.gz'
    rule = {'source': 'go_proxy', 'url': 'https://proxy.golang.org/github.com/example/module/@latest'}
    config['native']['binutils'] = rule
    snapshot['tracks']['binutils']['source'] = deepcopy(rule)
    return source


@pytest.mark.parametrize('provider', ['requires', 'yanked', 'license', 'security'])
def test_source_tag_projection_is_shared_without_overwriting_rpm(config, snapshot, provider):
    setup(config, snapshot)
    proposal = runner.plan(config, snapshot, 'binutils', provider)
    assert proposal['status'] == 'pending'
    assert proposal['subject']['version'] == '1.0.1-patch-7'
    assert compare.evaluate(snapshot, 'binutils').source['version'] == '1.0.1+patch7'
    rows, _ = view.project_monitors(snapshot)
    row = next(row for row in rows if row['name'] == 'binutils')
    assert 'Go 1.0.1-patch-7' in [v.text for v in version_value(row)]


@pytest.mark.parametrize('case', ['identity', 'host', 'credentials', 'query', 'version', 'hash', 'tag', 'multiple'])
def test_source_tag_projection_rejects_unproven_relationships(config, snapshot, case):
    source = setup(config, snapshot)
    identity = {'ecosystem': 'Go', 'name': 'github.com/example/module'}
    url = source['metadata']['sources'][0]
    if case == 'identity':
        identity['name'] += '/v2'
    elif case == 'host':
        url['url'] = url['url'].replace('github.com/', 'github.com.evil/')
    elif case == 'credentials':
        url['url'] = url['url'].replace('https://', 'https://user@')
    elif case == 'query':
        url['url'] += '?token=secret'
    elif case == 'version':
        source['version'] = source['metadata']['version'] = '1.0.1+patch8'
    elif case == 'hash':
        source['native_query']['spec_sha256'] = ''
    elif case == 'tag':
        url['url'] = url['url'].replace('v1.0.1-patch-7', 'main')
    else:
        source['metadata']['sources'].append(deepcopy(url))
    assert release.from_source(source, identity) is None


def test_existing_inputs_reuse_cache_but_new_backend_or_identity_does_not(config, snapshot, monkeypatch, tmp_path):
    setup(config, snapshot)
    backend = monitor.BACKENDS['go']
    monkeypatch.delitem(monitor.BACKENDS, 'go')
    old = runner.plan(config, snapshot, 'binutils', 'requires')
    assert old['status'] == 'not_configured'
    snapshot['monitors'] = {'binutils': {'requires': old}}
    db = tmp_path / 'state.sqlite3'
    state.commit(db, snapshot)
    snapshot = state.read(db)
    assert snapshot['monitors']['binutils']['requires']['fingerprint'] == old['fingerprint']
    monkeypatch.setitem(monitor.BACKENDS, 'go', backend)
    new = runner.plan(config, snapshot, 'binutils', 'requires')
    assert new['fingerprint'] != old['fingerprint']
    saved = {**new, 'status': 'ok', 'checked_at': state.utcnow(), 'findings': []}
    snapshot['monitors']['binutils']['requires'] = saved
    state.commit(db, snapshot)
    snapshot = state.read(db)
    saved = snapshot['monitors']['binutils']['requires']
    policy = runner.refresh_policy('requires', new, saved, {})
    assert not policy.due(saved, new['fingerprint'], datetime.now(UTC))
    snapshot['specs']['binutils']['revision'] = 'spec:unrelated-edit'
    assert runner.plan(config, snapshot, 'binutils', 'requires')['fingerprint'] == new['fingerprint']
    config['packages']['binutils'] = {'monitors': {'requires': {'go': 'example.org/other'}}}
    changed = runner.plan(config, snapshot, 'binutils', 'requires')
    assert policy.due(saved, changed['fingerprint'], datetime.now(UTC))
    assert not runner.same_scope(changed, saved, 'current')


def test_new_source_version_drops_old_current_and_upgrade_evidence(config, snapshot):
    setup(config, snapshot)
    old = runner.plan(config, snapshot, 'binutils', 'requires')
    setup(config, snapshot, version='1.0.1+patch8', tag='v1.0.1-patch-8')
    new = runner.plan(config, snapshot, 'binutils', 'requires')
    assert old['fingerprint'] != new['fingerprint']
    assert not runner.same_scope(new, old, 'current')
    assert not runner.same_scope(new, old, 'upgrade')


@pytest.mark.parametrize('target,stale,expected', [('1.24', False, 1), ('1.23', False, 0),
                                                ('1.24', True, 0), (None, False, 0)])
def test_build_requirements_show_only_fresh_proven_changes(target, stale, expected):
    facts = []
    for scope, version in [('current', '1.23'), ('upgrade', target)]:
        if version:
            facts.append({'scope': scope, 'stale': stale, 'requirement': Requirement(
                'go', 'Go', 'build', 'numeric_minimum', version, version,
                'Go module proxy', 'https://proxy.golang.org/example.org/m/@v/v1.0.0.mod').fact()})
    result = project(facts, {}, None)
    assert len(result) == expected
    if result:
        assert result[0]['changed'] and not unsatisfied(result[0])
        assert '→' in [v.text for v in requirement_values(result[0])]


@pytest.mark.parametrize('target,expected', [('1.0.1-patch-7', 'current'), ('v1.0.1-patch-7', 'current'),
                                           ('1.0.1', 'outdated'), ('1.0.0', 'ahead'), ('bad', 'unknown')])
def test_go_comparison_uses_exact_source_identity(config, snapshot, target, expected):
    setup(config, snapshot)
    snapshot['tracks']['binutils']['version'] = target
    assert compare.evaluate(snapshot, 'binutils').relation == expected


def test_upgrade_change_preserves_only_matching_current_scope(config, snapshot):
    setup(config, snapshot)
    old = runner.plan(config, snapshot, 'binutils', 'requires')
    old.update(findings=[{'scope': 'current', 'id': 'current'}, {'scope': 'upgrade', 'id': 'upgrade'}],
               scope_checks={'current': {'status': 'ok'}, 'upgrade': {'status': 'ok'}})
    snapshot['tracks']['binutils']['version'] = '2.0.0'
    new = runner.plan(config, snapshot, 'binutils', 'requires')
    assert runner.same_scope(new, old, 'current')
    assert not runner.same_scope(new, old, 'upgrade')
    retained = runner.failed(new, old, OSError('offline'), state.utcnow())
    assert retained['findings'] == [{'scope': 'current', 'id': 'current'}]
    assert set(retained['scope_checks']) == {'current'}


def test_retraction_catalog_identity_mismatch_never_becomes_a_negative_result():
    from tracker.providers import go

    class IO:
        def text(self, url, **kwargs):
            if url.endswith('/@v/list'):
                return 'v1.0.0\nv1.1.0\n'
            assert url.endswith('/@v/v1.1.0.mod')
            return 'module example.org/renamed\n'

    with pytest.raises(ValueError, match='identity'):
        go.withdrawal({'go': 'example.org/original'}, '1.0.0', IO())


@pytest.mark.parametrize('current,tag', [('1.2.3~vendor', 'v1.2.3-vendor'), ('1.2.3', 'v1.2.3')])
def test_observed_go_tag_spelling_requires_matching_identity(config, snapshot, current, tag):
    source = setup(config, snapshot, version=current, tag=tag)
    identity = {'ecosystem': 'Go', 'name': 'github.com/example/module'}
    observed = release.from_source(source, identity)
    assert observed.version == tag.removeprefix('v')
    assert release.from_source(source) is None
