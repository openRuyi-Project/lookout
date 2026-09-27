"""Commit tracking is identity comparison, not a release or date comparator."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from tracker import config as cfg, state
from tracker.monitors import registry as monitor_registry, runner as monitor
from tracker.monitors.source import release as source_release
from tracker.monitors.version import compare as version_status, nvchecker as nv
from tracker.presentation import values as presentation_values, version as presentation_version
from tracker.readmodel import snapshot as view


CURRENT = '123456' + 'a' * 34
LATEST = 'abcdef' + 'b' * 34
REPO = 'https://github.com/example/widget'


def prepare(config, snapshot, latest=LATEST):
    version = '0+git20000101.' + CURRENT[:7]
    spec = dict(version=version, head='fixture', error=None, fetched_at=state.utcnow(),
                native_query=dict(spec_sha256='a' * 64, context={'resolver': 8}),
                metadata=dict(version=version, sources=[dict(number=0,
                    url=f'{REPO}/archive/{CURRENT}.tar.gz#/widget.tar.gz')]))
    snapshot['specs'] = {'binutils': spec}
    entry = dict(source='git', git=REPO + '.git', use_commit=True, branch='main')
    config['native']['binutils'] = entry
    snapshot['tracks']['binutils'] = state.success({}, dict(version=latest, source=entry), state.utcnow())
    snapshot['bindings']['binutils'] = cfg.binding(config, 'binutils')
    return spec


@pytest.mark.parametrize(('latest', 'relation'), [(CURRENT, 'current'), (LATEST, 'changed')])
def test_full_commit_comparison_and_compact_reading(config, snapshot, latest, relation, monkeypatch):
    spec = prepare(config, snapshot, latest)
    monkeypatch.setattr(state, 'compare', lambda *args: pytest.fail('hash is not an RPM version'))
    decision = version_status.evaluate(snapshot, 'binutils')
    assert decision.relation == relation
    assert not decision.upgrading
    assert decision.subject['version'] == spec['version']
    assert decision.revision.public(latest) == dict(repository=REPO, branch='main', current=CURRENT, latest=latest,
        packaged_date='2000-01-01', latest_committed_at=None,
        links={'current': REPO + '/commit/' + CURRENT, 'latest': REPO + '/commit/' + latest,
               'branch': REPO + '/tree/main'})
    pkg = {'monitors': {'version': {'data': dict(kind='version', current=spec['version'], latest=latest,
        track='binutils', relation=relation, revision=decision.revision.public(latest))}}}
    values = presentation_values.version_value(pkg)
    display = '20000101.' + CURRENT[:6]
    assert [v.text for v in values] == ([display] if relation == 'current' else [display, '→', LATEST[:6]])
    assert values[0].href == REPO + '/commit/' + CURRENT
    assert CURRENT in values[0].title
    assert [v.text for v in presentation_values.version_value(pkg, compact=False)] == [spec['version']]


def test_same_six_characters_are_not_equal_commits(config, snapshot):
    latest = CURRENT[:6] + 'b' * 34
    prepare(config, snapshot, latest)
    decision = version_status.evaluate(snapshot, 'binutils')
    assert decision.relation == 'changed'
    assert presentation_values.short_commit(CURRENT, latest) == CURRENT[:7]
    assert presentation_values.short_commit(latest, CURRENT) == latest[:7]
    assert presentation_values.short_commit(CURRENT, CURRENT) == CURRENT[:6]


def test_revision_changes_join_updates_but_not_release_jobs(config, snapshot, monkeypatch):
    prepare(config, snapshot)
    monkeypatch.setitem(monitor_registry.REGISTRY, 'fixture', SimpleNamespace(
        VERSION=1, SCOPE='upgrade', HOSTS=set(), inputs=lambda *args: {}))
    proposed = monitor.plan(config, snapshot, 'binutils', 'fixture')
    assert proposed['status'] == 'not_applicable'
    for provider in ('license', 'requires'):
        proposed = monitor.plan(config, snapshot, 'binutils', provider)
        assert proposed['subject'].get('target_version') is None
    rows, _ = view.project_monitors(snapshot)
    row = next(r for r in rows if r['name'] == 'binutils')
    result = row['monitors']['version']
    assert result['check']['status'] == 'ok'
    assert 'updates' in result['dimensions']['view']
    fields = presentation_version.version_sections(result, None)[0].fields
    assert any(f.label == 'Source commit' and f.values[0].text == CURRENT for f in fields)
    assert any(f.label == 'Observed branch tip' and f.values[0].text == LATEST for f in fields)


@pytest.mark.parametrize('case', ['short_source', 'wrong_repo', 'wrong_rpm', 'tag', 'credentials',
                                 'missing_branch', 'unverified', 'duplicate', 'malformed_target'])
def test_ambiguous_identity_never_falls_back_to_rpm_order(case, config, snapshot):
    spec = prepare(config, snapshot)
    entry = snapshot['tracks']['binutils']['source']
    item = spec['metadata']['sources'][0]
    if case == 'short_source':
        item['url'] = item['url'].replace(CURRENT, CURRENT[:7])
    elif case == 'wrong_repo':
        entry['git'] = 'https://github.com/another/widget.git'
    elif case == 'wrong_rpm':
        spec['version'] = spec['metadata']['version'] = '0+git20000101.fffffff'
    elif case == 'tag':
        item['url'] = item['url'].replace(CURRENT, 'refs/tags/v1.0')
    elif case == 'credentials':
        item['url'] = item['url'].replace('https://', 'https://secret@')
    elif case == 'missing_branch':
        entry.pop('branch')
    elif case == 'unverified':
        spec['native_query']['spec_sha256'] = 'missing'
    elif case == 'duplicate':
        spec['metadata']['sources'].append(deepcopy(item))
    else:
        snapshot['tracks']['binutils']['version'] = 'not-a-commit'
    value = version_status.evaluate(snapshot, 'binutils')
    assert value.relation == 'unknown'
    assert not value.upgrading
    assert value.error == 'Source0 commit and tracked branch cannot be reliably compared'


def test_failed_query_retains_evidence_without_fresh_change(config, snapshot):
    prepare(config, snapshot)
    old = snapshot['tracks']['binutils']
    entry = config['native']['binutils']
    old['configuration_fingerprint'] = cfg.track_fingerprint(entry)
    facts, _ = nv.import_events('', {'binutils': entry}, {'binutils': old}, state.utcnow(), 'offline')
    snapshot['tracks'].update(facts)
    value = version_status.evaluate(snapshot, 'binutils')
    assert value.relation == 'unknown'
    assert value.last_known_relation == 'changed'
    assert value.revision.public(value.upstream['version'])['latest'] == LATEST


def test_native_event_keeps_revision_semantics_without_arbitrary_configuration(config, snapshot):
    prepare(config, snapshot)
    entry = {**config['native']['binutils'], 'token': 'private', 'cmd': 'do not expose'}
    event = json.dumps(dict(name='binutils', event='updated', version=LATEST))
    facts, _ = nv.import_events(event, {'binutils': entry}, {}, state.utcnow())
    snapshot['tracks'].update(facts)
    assert facts['binutils']['source'] == config['native']['binutils']
    assert version_status.evaluate(snapshot, 'binutils').relation == 'changed'


def test_codeload_identity_and_packaging_date_not_commit_time(config, snapshot):
    spec = prepare(config, snapshot)
    spec['metadata']['sources'][0]['url'] = f'https://codeload.github.com/example/widget/tar.gz/{CURRENT}'
    spec['version'] = spec['metadata']['version'] = '0+git20991231.' + CURRENT[:7]
    assert version_status.evaluate(snapshot, 'binutils').relation == 'changed'


def test_formal_release_rule_is_not_replaced_by_snapshot_heuristics(config, snapshot):
    prepare(config, snapshot)
    snapshot['tracks']['binutils']['source'].pop('use_commit')
    assert version_status.evaluate(snapshot, 'binutils').revision is None


def github_observation(config, snapshot, *, commit=LATEST, date='2001-02-03T12:13:14Z'):
    prepare(config, snapshot)
    entry = dict(source='github', github='example/widget', branch='main')
    config['native']['binutils'] = entry
    event = json.dumps(dict(name='binutils', event='updated', version='20010203.121314',
                           rich_result=dict(revision=commit, revision_creation_time=date)))
    facts, _ = nv.import_events(event, {'binutils': entry}, {}, state.utcnow())
    snapshot['tracks'].update(facts)
    return entry


def test_native_github_date_and_revision_survive_import_and_render(config, snapshot):
    github_observation(config, snapshot)
    rows, _ = view.project_monitors(snapshot)
    row = next(r for r in rows if r['name'] == 'binutils')
    assert [t.text for t in presentation_values.version_value(row)] == ['20000101.123456', '→', '20010203.abcdef']
    data = row['monitors']['version']['data']
    assert data['revision']['latest'] == LATEST
    assert data['revision']['latest_committed_at'] == '2001-02-03T12:13:14+00:00'
    assert version_status.evaluate(snapshot, 'binutils').target_version is None


def test_commit_equality_ignores_dates_and_timestamp_changes(config, snapshot):
    github_observation(config, snapshot, commit=CURRENT, date='2099-12-31T23:59:59Z')
    assert version_status.evaluate(snapshot, 'binutils').relation == 'current'
    snapshot['tracks']['binutils']['revision'] = LATEST
    assert version_status.evaluate(snapshot, 'binutils').relation == 'changed'


def test_timestamp_without_revision_cannot_reuse_old_hash(config, snapshot):
    entry = github_observation(config, snapshot)
    event = json.dumps(dict(name='binutils', event='up-to-date', version='20010203.121314'))
    facts, _ = nv.import_events(event, {'binutils': entry}, snapshot['tracks'], state.utcnow())
    snapshot['tracks'].update(facts)
    assert version_status.evaluate(snapshot, 'binutils').relation == 'unknown'
    assert facts['binutils']['revision'] is None


@pytest.mark.parametrize('date', [None, 'not-a-date', '2001-02-03T12:13:14'])
def test_missing_or_unzoned_commit_time_is_not_replaced_by_poll_time(config, snapshot, date):
    github_observation(config, snapshot, date=date)
    rows, _ = view.project_monitors(snapshot)
    row = next(r for r in rows if r['name'] == 'binutils')
    assert [t.text for t in presentation_values.version_value(row)] == ['20000101.123456', '→', 'abcdef']


def test_native_release_flags_and_component_scope_do_not_become_repository_tracking(config, snapshot):
    entry = github_observation(config, snapshot)
    entry['use_latest_release'] = True
    snapshot['tracks']['binutils']['source'] = cfg.public_source(entry)
    assert version_status.evaluate(snapshot, 'binutils').revision is None
    entry.pop('use_latest_release')
    entry['path'] = 'unrelated-submodule'
    snapshot['tracks']['binutils']['source'] = cfg.public_source(entry)
    assert version_status.evaluate(snapshot, 'binutils').relation == 'unknown'


@pytest.mark.parametrize(('entry', 'url', 'suffix'), [
    ({'source': 'github', 'github': 'example/widget'},
     f'{REPO}/archive/{CURRENT}/widget-{CURRENT}.tar.gz', '/commit/'),
    ({'source': 'gitlab', 'gitlab': 'group/sub/widget', 'host': 'forge.example.org'},
     f'https://forge.example.org/group/sub/widget/-/archive/{CURRENT}/widget-{CURRENT}.tar.gz', '/-/commit/'),
    ({'source': 'gitea', 'gitea': 'group/widget', 'host': 'forge.example.org'},
     f'https://forge.example.org/group/widget/archive/{CURRENT}.tar.gz', '/commit/'),
    ({'source': 'git', 'git': 'https://git.example.org/widget', 'use_commit': True},
     f'https://git.example.org/widget/snapshot/widget-{CURRENT}.tar.xz', '/commit/?id='),
])
def test_native_forge_sources_share_one_commit_comparison(config, snapshot, entry, url, suffix):
    spec = prepare(config, snapshot)
    spec['metadata']['sources'][0]['url'] = url
    entry = {**entry, 'branch': 'stable/next'}
    version = LATEST if entry['source'] == 'git' else '2001-02-03T12:13:14Z'
    event = json.dumps(dict(name='binutils', event='updated', version=version,
        rich_result=dict(revision=LATEST, revision_creation_time='2001-02-03T12:13:14Z')))
    facts, error = nv.import_events(event, {'binutils': entry}, {}, state.utcnow())
    assert error is None
    snapshot['tracks'].update(facts)
    value = version_status.evaluate(snapshot, 'binutils')
    assert value.relation == 'changed'
    revision = value.revision.public(LATEST)
    assert revision['links']['latest'] == revision['repository'] + suffix + LATEST
    assert 'stable%2Fnext' in revision['links']['branch']
    assert value.target_version is None and not value.upgrading


def test_gitea_reuses_native_committer_time_without_an_additional_request(config, snapshot):
    entry = dict(source='gitea', gitea='example/widget', host='forge.example.org', branch='main')
    event = json.dumps(dict(name='binutils', event='updated', version='2001-02-03T14:13:14+02:00',
                           rich_result=dict(revision=LATEST)))
    facts, _ = nv.import_events(event, {'binutils': entry}, {}, state.utcnow())
    assert facts['binutils']['revision_creation_time'] == '2001-02-03T12:13:14+00:00'
    entry['use_max_tag'] = True
    facts, _ = nv.import_events(event, {'binutils': entry}, {}, state.utcnow())
    assert facts['binutils']['revision_creation_time'] is None
    assert source_release.observed_commit(facts['binutils']) is None


@pytest.mark.parametrize('version', ['0+git200001001.123456a', '0+git20001301.123456a', '0+git123456a'])
def test_bad_or_missing_packaging_date_does_not_invalidate_a_proven_commit(config, snapshot, version):
    spec = prepare(config, snapshot)
    spec['version'] = spec['metadata']['version'] = version
    value = version_status.evaluate(snapshot, 'binutils')
    assert value.relation == 'changed'
    assert value.revision.packaged_date is None


@pytest.mark.parametrize('url', [
    f'{REPO}/archive/{CURRENT[:12]}.tar.gz',
    f'{REPO}/archive/refs/tags/v1.0.tar.gz',
    f'{REPO}/archive/{CURRENT}.tar.gz?ref={LATEST}',
    f'https://forge.example.org/../widget/-/archive/{CURRENT}/widget.tar.gz',
    f'https://forge.example.org/group%2Fwidget/archive/{CURRENT}.tar.gz',
    'widget-snapshot.tar.gz',
])
def test_discovery_does_not_turn_ambiguous_archive_identity_into_a_rule(config, snapshot, url):
    spec = prepare(config, snapshot)
    spec['metadata']['sources'][0]['url'] = url
    source = state.current_source(snapshot, 'binutils')
    assert source_release.pinned_revision(source) is None


def test_non_github_repository_paths_are_case_sensitive(config, snapshot):
    spec = prepare(config, snapshot)
    spec['metadata']['sources'][0]['url'] = f'https://forge.example.org/Group/widget/-/archive/{CURRENT}/widget.tar.gz'
    entry = dict(source='gitlab', gitlab='group/widget', host='forge.example.org', branch='main')
    assert source_release.revision(state.current_source(snapshot, 'binutils'), entry) is None


def test_native_rate_limit_survives_generic_no_result_without_leaking_exception():
    entries = {'widget': {'source': 'github', 'github': 'example/widget', 'branch': 'main'}}
    events = [dict(name='widget', level='error', event='unexpected error happened',
                   error="RuntimeError('rate limited: private-token')"),
              dict(name='widget', level='error', event='no-result')]
    facts, _ = nv.import_events('\n'.join(map(json.dumps, events)), entries, {}, state.utcnow())
    assert facts['widget']['error'] == 'nvchecker rate limited'
