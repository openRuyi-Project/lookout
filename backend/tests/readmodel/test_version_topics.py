"""Version is a composed topic; collectors keep their own evidence and cadence."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest

from tracker import state
from tracker.api import create_app
from tracker.monitors import model as monitor_model
from tracker.monitors.version import compare as version_status
from tracker.readmodel import monitors as monitor_views, snapshot as view
from tracker.readmodel.packages import PackageList


@pytest.fixture
def version():
    return version_status.VersionStatus(
        name='widget', binding={}, source={'version': '1.0', 'revision': 'fixture'},
        track='widget', upstream={'version': '2.0'}, source_stale=False,
        upstream_stale=False, relation='outdated', last_known_relation='outdated', error=None)


def finding(monitor, identity='one', *, scope='upgrade', target='2.0', stale=False):
    return dict(id=monitor + ':' + identity, monitor=monitor, label='ProviderSpecificLabel',
                title='Provider-specific wording is not parsed', facts=[],
                evidence_url='https://example.org/' + identity, scope=scope,
                target_version=target if scope == 'upgrade' else None, tags=[], stale=stale)


def results(**evidence):
    projected = {'version': dict(data=dict(kind='version'), dimensions={})}
    for monitor, findings in evidence.items():
        projected[monitor] = dict(title=monitor.title(), data=dict(kind='evidence', findings=findings))
    return projected


def compose(version, **evidence):
    projected = results(**evidence)
    monitor_views.compose_version(projected, version)
    return projected['version']


def test_current_security_and_yanked_do_not_depend_on_upgrade(version):
    current = replace(version, relation='current', last_known_relation='current', upstream={'version': '1.0'})
    advisory = finding('security', scope='current')
    output = compose(current, security=[advisory, advisory, finding('security', 'two', scope='current')],
                     yanked=[finding('yanked', scope='current')],
                     eol=[finding('eol', scope='current')], license=[])
    assert output['dimensions'] == {'version_signal': ['security', 'yanked']}
    annotations = output['data']['annotations']
    assert [(item['label'], item['count']) for item in annotations] == [('Security', 2), ('Yanked', 1)]
    assert all(item['scope'] == 'current' and item['target_version'] is None for item in annotations)


def test_withdrawn_current_release_is_not_a_withdrawn_target(version):
    output = compose(version, yanked=[finding('yanked', scope='current')])
    assert output['data']['annotations'] == [dict(
        monitor='yanked', label='Yanked', count=1, scope='current', target_version=None,
        stale=False, finding_ids=['yanked:one'])]


def test_generic_upgrade_facts_join_by_scope_and_target_not_display_text(version):
    output = compose(version, license=[finding('license')], abi=[finding('abi')],
                     unrelated=[finding('unrelated', scope='current')],
                     wrong_target=[finding('wrong_target', target='3.0')])
    assert output['dimensions']['version_signal'] == ['abi', 'license']
    assert [item['label'] for item in output['data']['annotations']] == ['Abi', 'License']
    assert all(item['scope'] == 'upgrade' and item['target_version'] == '2.0'
               for item in output['data']['annotations'])


def test_retained_upgrade_evidence_is_not_promoted_to_fresh(version):
    unavailable = replace(version, relation='unknown', upstream_stale=True)
    output = compose(unavailable, license=[finding('license')],
                     security=[finding('security', scope='current', stale=True)])
    assert all(item['stale'] for item in output['data']['annotations'])
    not_an_upgrade = replace(version, relation='ahead', last_known_relation='ahead')
    assert compose(not_an_upgrade, license=[finding('license')])['data']['annotations'] == []


def requirement(name, changed, satisfaction='satisfied'):
    return dict(dependency=name, kind='runtime', scheme='numeric_minimum',
                changed=changed, satisfaction=satisfaction)


def test_requires_joins_changed_declarations_not_current_unmet(version):
    projected = results()
    items = [requirement('interpreter', True), requirement('library', True),
             requirement('unmet', False, 'unsatisfied')]
    findings = [dict(**finding('requires', item['dependency']), requirement=item) for item in items]
    projected['requires'] = dict(title='Requires', data=dict(
        kind='requires', requirements=items, findings=findings))
    monitor_views.compose_version(projected, version)
    assert projected['version']['data']['annotations'] == [dict(
        monitor='requires', label='Requires', count=2, scope='upgrade', target_version='2.0',
        stale=False, finding_ids=['requires:interpreter', 'requires:library'])]
    for item in items:
        item['changed'] = False
    monitor_views.compose_version(projected, version)
    assert projected['version']['data']['annotations'] == []


def test_requires_has_no_upgrade_label_when_version_is_not_fresh(version):
    projected = results()
    projected['requires'] = dict(title='Requires', data=dict(
        kind='requires', requirements=[requirement('library', True)], findings=[]))
    monitor_views.compose_version(projected, replace(version, relation='unknown', upstream_stale=True))
    assert projected['version']['data']['annotations'] == []


def test_composition_keeps_original_evidence_and_has_no_network_work(version):
    projected = results(license=[finding('license')], security=[finding('security', scope='current')])
    before = deepcopy(projected)
    monitor_views.compose_version(projected, version)
    assert {key: value for key, value in projected.items() if key != 'version'} == {
        key: value for key, value in before.items() if key != 'version'}
    assert monitor_views.summary(dict(id='version', title='Version', check={}, **projected['version']))[
        'data']['annotations'] == projected['version']['data']['annotations']


def test_projection_rejects_changed_subject_and_target_before_topic_join(snapshot, monkeypatch):
    monkeypatch.setattr(state, 'compare', lambda *args: 'outdated')
    now = datetime.now(timezone.utc)
    subject = version_status.evaluate(snapshot, 'binutils', now).subject
    snapshot['monitor_catalog'] = {'license': {'title': 'License'}}
    snapshot['monitors'] = {'binutils': {'license': dict(
        scope='upgrade', subject={**subject, 'target_version': '3.9.9'}, status='ok',
        checked_at=state.utcnow(), findings=[monitor_model.finding(
            'license', 'License', 'Fixture license', [], 'https://example.org/',
            scope='upgrade', target_version='3.9.9')])}}
    rows, _ = view.project_monitors(snapshot, now)
    row = next(row for row in rows if row['name'] == 'binutils')
    assert row['monitors']['license']['check']['status'] == 'input_changed'
    assert row['monitors']['version']['data']['annotations'] == []
    assert row['monitors']['version']['dimensions']['version_signal'] == []


def test_signal_counts_are_disjunctive_package_counts_and_filter_by_stable_id():
    rows = [dict(name=name, monitors={'version': dict(dimensions=dict(
        version_signal=signals, buildsystem=[system]))})
        for name, signals, system in [('a', ['security', 'security', 'license'], 'cmake'),
                                     ('b', ['license'], 'cmake'), ('c', ['yanked'], 'meson')]]
    index = PackageList(rows, [])
    filters = dict(view='all', buildsystem='cmake', maintenance='', builds={}, page=1, per_page=100)
    result = index.select(signal='security', **filters)
    assert result['total'] == 1
    assert result['version_signals'] == {'license': 2, 'security': 1}
    for signal, count in result['version_signals'].items():
        assert index.select(signal=signal, **filters)['total'] == count


def test_v2_filter_and_schema_expose_the_join_without_repeating_evidence(snapshot, tmp_path, monkeypatch):
    rows, collection = view.project_monitors(snapshot)
    for row in rows:
        if row['name'] == 'binutils':
            row['monitors']['version']['data']['annotations'] = [dict(
                monitor='security', label='Security', count=2, scope='current', target_version=None,
                stale=False, finding_ids=['security:one', 'security:two'])]
            row['monitors']['version']['dimensions']['version_signal'] = ['security']
    index = PackageList(rows, snapshot['targets'])
    app = create_app(tmp_path / 'unused.db')
    monkeypatch.setattr(app.state.projection, 'read', lambda: (snapshot, index, collection))
    client = TestClient(app)
    response = client.get('/api/v2/packages?monitor=version&signal=security')
    assert response.status_code == 200
    output = response.json()
    assert output['total'] == 1 and output['version_signals'] == {'security': 1}
    annotation = output['items'][0]['monitors']['version']['data']['annotations'][0]
    assert annotation['monitor'] == 'security' and annotation['count'] == 2
    assert annotation['finding_ids'] == ['security:one', 'security:two']
    assert 'facts' not in annotation


def test_current_security_reaches_version_even_when_latest_and_disappears_on_subject_change(snapshot, monkeypatch):
    monkeypatch.setattr(state, 'compare', lambda *args: 'current')
    now = datetime.now(timezone.utc)
    subject = version_status.evaluate(snapshot, 'binutils', now).subject
    snapshot['monitor_catalog'] = {'security': {'title': 'Security'}}
    snapshot['monitors'] = {'binutils': {'security': dict(
        scope='current', subject=subject, status='ok', checked_at=state.utcnow(),
        findings=[monitor_model.finding('CVE-fixture', 'Security', 'CVE-fixture', [],
                                        'https://example.org/advisory')])}}
    rows, _ = view.project_monitors(snapshot, now)
    row = next(row for row in rows if row['name'] == 'binutils')
    assert row['monitors']['version']['data']['relation'] == 'current'
    assert row['monitors']['version']['data']['annotations'] == [dict(
        monitor='security', label='Security', count=1, scope='current', target_version=None,
        stale=False, finding_ids=['security:CVE-fixture'])]
    snapshot['sources']['binutils']['srcmd5'] = 'new-source-revision'
    rows, _ = view.project_monitors(snapshot, now)
    row = next(row for row in rows if row['name'] == 'binutils')
    assert row['monitors']['version']['data']['annotations'] == []
