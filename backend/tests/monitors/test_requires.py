from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import tomlkit

from tests.conftest import ProjectedClient
from tracker import config as cfg, state
from tracker.api import create_app
from tracker.monitors import model as monitor_model, runner as monitor
from tracker.monitors.requires import (
    compare as requirement_versions,
    model as requirements,
    monitor as monitor_requires,
)
from tracker.providers.client import IO


def change():
    return dict(dependency='python', name='Python', kind='runtime', scheme='pep440',
                current=dict(expression='>=3.8', source='PyPI', url='https://pypi.org/pypi/widget/1/json'),
                target=dict(expression='>=3.10', source='PyPI', url='https://pypi.org/pypi/widget/2/json'))


def dependency(snapshot, version='3.11.8'):
    snapshot['dependency_packages'] = {'python': 'runtime-package'}
    snapshot['sources']['runtime-package'] = state.success({}, {'version': version, 'srcmd5': 'python-hash'}, state.utcnow())


@pytest.mark.parametrize('version,expected', [('3.11.8', 'satisfied'), ('3.9.8', 'satisfied'),
                                             ('3.10rc1', 'unsatisfied'), ('3.14.0', 'satisfied')])
def test_satisfaction_compares_observed_source_not_host_python(snapshot, version, expected):
    dependency(snapshot, version)
    result = requirements.assess(change(), snapshot, datetime.now(timezone.utc))
    assert result['satisfaction'] == expected
    assert result['observed']['version'] == version
    assert result['package'] == 'runtime-package'
    requirements.RequirementAssessment.model_validate(result)


@pytest.mark.parametrize('case,reason', [
    ('unmapped', 'dependency_not_mapped'), ('missing', 'dependency_unavailable'),
    ('expired', 'dependency_unavailable'), ('error', 'dependency_unavailable'),
    ('invalid', 'unsupported_version'), ('scheme', 'unsupported_comparison'),
    ('constraint', 'unsupported_version'), ('stale_requirement', 'requirement_unavailable'),
])
def test_unknown_does_not_become_a_checkmark_or_failure(snapshot, case, reason):
    dependency(snapshot)
    fact = snapshot['sources']['runtime-package']
    declaration = change()
    if case == 'unmapped':
        snapshot['dependency_packages'] = {}
    elif case == 'missing':
        del snapshot['sources']['runtime-package']
    elif case == 'expired':
        fact['fetched_at'] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    elif case == 'error':
        fact['error'] = 'fetch failed'
    elif case == 'invalid':
        fact['version'] = '3.11~vendor'
    elif case == 'scheme':
        declaration['scheme'] = 'rpm'
    elif case == 'constraint':
        declaration['current']['expression'] = 'Python latest'
    result = requirements.assess(declaration, snapshot, datetime.now(timezone.utc), stale=case == 'stale_requirement')
    assert (result['satisfaction'], result['reason']) == ('unknown', reason)


def test_spec_owns_dependency_version_and_error_does_not_fall_back_to_obs(snapshot):
    dependency(snapshot, '3.14')
    snapshot['specs']['runtime-package'] = state.success({}, {
        'metadata': {'version': '3.7'}, 'head': 'source-commit',
        'native_query': {'spec_sha256': 'expanded-spec'}}, state.utcnow())
    result = requirements.assess(change(), snapshot, datetime.now(timezone.utc))
    assert result['satisfaction'] == 'unsatisfied'
    assert result['observed']['origin'] == 'spec' and result['observed']['revision'].startswith('spec:')
    snapshot['specs']['runtime-package']['error'] = 'parse failed'
    assert requirements.assess(change(), snapshot, datetime.now(timezone.utc))['satisfaction'] == 'unknown'


def test_constraint_changes_share_a_domain_across_backends(monkeypatch):
    def read(version, settings, io):
        return [requirements.Requirement('rust', 'Rust', 'build', 'semver',
                '>=1.80' if version == '1' else '>=1.82', version, 'Registry', 'https://example.org/' + version)]
    adapter = SimpleNamespace(read=read, inputs=lambda package, configured: configured)
    monkeypatch.setitem(monitor_requires.BACKENDS, 'registry', adapter)
    inputs = monitor_requires.inputs({}, {'registry': 'widget'})
    result = monitor_requires.check({'version': '1', 'target_version': '2'}, inputs, None)
    item = result['findings'][0]
    assert item['label'] == 'Requires'
    assert item['requirement']['dependency'] == 'rust'
    assert item['requirement']['kind'] == 'build'
    assert item['requirement']['constraint']['expression'] == '>=1.80'
    # An unsupported comparator is not borrowed from another ecosystem.
    snapshot = {'dependency_packages': {'rust': 'rust'}, 'sources': {
        'rust': state.success({}, {'version': '1.84', 'srcmd5': 'hash'}, state.utcnow())}}
    assert requirements.assess({**{k: v for k, v in item['requirement'].items() if k != 'constraint'},
                                'current': item['requirement']['constraint'], 'target': None}, snapshot, datetime.now(timezone.utc))['satisfaction'] == 'unknown'


def test_missing_dependency_coverage_is_not_reported_as_removal(snapshot):
    declaration = requirements.Requirement('python', 'Python', 'runtime', 'pep440',
        '>=3.8', '>=3.8', 'PyPI', 'https://pypi.org/').fact()
    result = requirements.project([{'requirement': declaration, 'scope': 'current', 'stale': False}],
                                  snapshot, datetime.now(timezone.utc))
    assert result[0]['current']['expression'] == '>=3.8'
    assert result[0]['target'] is None
    assert result[0]['changed'] is False
    assert result[0]['target_satisfaction'] == 'unknown'


@pytest.mark.parametrize('case,mapping,package', [
    ('mapped', 'mapped', 'runtime-package'),
    ('explicit_absent', 'not_packaged', 'runtime-package'),
    ('unmapped', 'not_mapped', None),
    ('ambiguous', 'ambiguous', None),
])
def test_mapping_is_independent_of_conditional_satisfaction(snapshot, case, mapping, package):
    dependency(snapshot)
    item = {**change(), 'condition': 'feature selected', 'optional': True,
            'identity': {'ecosystem': 'PyPI', 'name': 'runtime-upstream'}}
    if case == 'explicit_absent':
        del snapshot['sources']['runtime-package']
    elif case in ('unmapped', 'ambiguous'):
        snapshot['dependency_packages'] = {}
    if case == 'ambiguous':
        for name in ('binutils', 'foo3'):
            snapshot['bindings'][name] = {'compare': name}
            snapshot['native_ids'].append(name)
            snapshot['tracks'][name] = {'source': {'source': 'pypi', 'pypi': 'runtime-upstream'}}
    result = requirements.assess(item, snapshot, datetime.now(timezone.utc))
    assert result['mapping'] == mapping and result['package'] == package
    assert result['satisfaction'] == 'unknown' and result['reason'] == 'condition_not_evaluated'
    assert (result['observed'] is not None) == (mapping == 'mapped')
    requirements.RequirementAssessment.model_validate(result)


def test_reviewed_identity_resolves_without_guessing_rpm_name(snapshot):
    item = {**change(), 'dependency': 'pypi.runtime-upstream',
            'identity': {'ecosystem': 'PyPI', 'name': 'runtime-upstream'}}
    snapshot['tracks']['binutils']['source'] = {'source': 'pypi', 'pypi': 'Runtime_Upstream'}
    result = requirements.assess(item, snapshot, datetime.now(timezone.utc))
    assert result['mapping'] == 'mapped' and result['package'] == 'binutils'


def test_inventory_presence_does_not_need_a_successful_source_observation(snapshot):
    dependency(snapshot)
    del snapshot['sources']['runtime-package']
    snapshot['inventory']['runtime-package'] = 'runtime-package'
    result = requirements.assess(change(), snapshot, datetime.now(timezone.utc))
    assert result['mapping'] == 'mapped' and result['package'] == 'runtime-package'
    assert result['satisfaction'] == 'unknown' and result['reason'] == 'dependency_unavailable'


@pytest.mark.parametrize('failure', ['missing', 'expired', 'error'])
def test_package_absence_requires_a_current_complete_inventory(snapshot, failure):
    dependency(snapshot)
    del snapshot['sources']['runtime-package']
    now = datetime.now(timezone.utc)
    if failure == 'missing':
        del snapshot['components']['inventory']
    elif failure == 'expired':
        snapshot['components']['inventory']['fetched_at'] = (now - timedelta(days=3)).isoformat()
    else:
        snapshot['components']['inventory']['error'] = 'inventory request failed'
    result = requirements.assess(change(), snapshot, now)
    assert result['mapping'] == 'not_mapped' and result['package'] == 'runtime-package'
    assert result['satisfaction'] == 'unknown'


def test_optional_metadata_does_not_create_a_new_dependency_or_upgrade_diff(snapshot):
    item = requirements.Requirement('helper', 'Helper', 'runtime', 'pep440', '>=1', None,
        'Registry', 'https://example.org/fixture', condition='feature selected').fact()
    before = {key: value for key, value in item.items() if key != 'optional'}
    after = {**item, 'optional': True}
    assert requirements.key(before) == requirements.key(after)
    result = requirements.project([
        {'requirement': before, 'scope': 'current', 'stale': False},
        {'requirement': after, 'scope': 'upgrade', 'stale': False},
    ], snapshot, datetime.now(timezone.utc))
    assert len(result) == 1 and result[0]['changed'] is False
    requirements.RequirementAssessment.model_validate(result[0])


@pytest.mark.parametrize('version,expected', [('1.9', False), ('2.0', True), ('2.1', True), ('2.0~rc1', False)])
def test_rpm_constraint_for_a_non_python_dependency(snapshot, version, expected):
    declaration = change()
    declaration.update(dependency='libwidget', name='libwidget', kind='build', scheme='rpm_version')
    declaration['current']['expression'] = declaration['target']['expression'] = '>= 2.0'
    snapshot['dependency_packages'] = {'libwidget': 'widget-library'}
    snapshot['sources']['widget-library'] = state.success({}, {'version': version, 'srcmd5': 'library-source'}, state.utcnow())
    result = requirements.assess(declaration, snapshot, datetime.now(timezone.utc))
    assert result['satisfaction'] == ('satisfied' if expected else 'unsatisfied')


@pytest.mark.parametrize('constraint', ['>= 1:2.0', '>= 2.0-4', '(libwidget >= 2.0 or libother)'])
def test_rpm_version_comparison_does_not_guess_unobserved_evr_or_capabilities(constraint):
    assert requirement_versions.satisfies('rpm_version', constraint, '2.1')[0] is None


def test_new_comparison_syntax_does_not_change_requires_or_renderers(snapshot, monkeypatch):
    monkeypatch.setitem(requirement_versions.COMPARATORS, 'fixture', lambda required, observed: required == observed)
    dependency(snapshot, '2.0')
    declaration = change()
    declaration.update(scheme='fixture', name='Other runtime')
    declaration['current']['expression'] = declaration['target']['expression'] = '2.0'
    assert requirements.assess(declaration, snapshot, datetime.now(timezone.utc))['satisfaction'] == 'satisfied'


def add_requires(snapshot):
    dependency(snapshot)
    subject = {**monitor_model.subject(snapshot, 'binutils'), 'target_version': '3.10.0'}
    comparison = change()
    identity = {key: value for key, value in comparison.items() if key not in {'current', 'target'}}
    findings = [monitor_model.finding('requires:python:' + scope, 'Requires', 'display text may change', [],
        comparison[key]['url'], scope=scope, target_version='3.10.0' if scope == 'upgrade' else None,
        requirement={**identity, 'constraint': comparison[key]})
        for scope, key in [('current', 'current'), ('upgrade', 'target')]]
    snapshot['monitor_catalog'] = {'requires': {'title': 'Requires'}}
    snapshot['monitors'] = {'binutils': {'requires': {
        'subject': subject, 'scope': 'current_and_upgrade', 'status': 'ok', 'checked_at': state.utcnow(),
        'scope_checks': {scope: {'status': 'ok', 'checked_at': state.utcnow()} for scope in ('current', 'upgrade')},
        'changed_at': '2026-09-20T00:00:00Z', 'evidence_revision': 'same-evidence', 'findings': findings}}}


def test_dependency_changes_reproject_without_rechecking_upstream(snapshot, tmp_path, monkeypatch):
    monkeypatch.setattr(state, 'compare', lambda *args: 'outdated')
    add_requires(snapshot)
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    original = deepcopy(snapshot['monitors'])
    client = ProjectedClient(create_app(db))
    route = '/api/v2/packages/binutils'
    result = client.get(route)
    assert result.status_code == 200
    data = result.json()['monitors']['requires']['data']
    assert data['kind'] == 'requires' and data['requirements'][0]['satisfaction'] == 'satisfied'
    assert data['current_version'] == '3.9.0' and data['target_version'] == '3.10.0'
    # The external edge identifies the dependency, exact constraint, comparator and source observation.
    assert data['requirements'][0]['package'] == 'runtime-package'
    snapshot['sources']['runtime-package']['version'] = '3.7.8'
    state.commit(db, snapshot)
    assert client.get(route).json()['monitors']['requires']['data']['requirements'][0]['satisfaction'] == 'unsatisfied'
    assert state.read(db)['monitors'] == original
    returned = client.get(route).json()['monitors']['requires']['data']['findings']
    assert [f['requirement']['constraint']['expression'] for f in returned] == ['>=3.8', '>=3.10']
    listing = client.get('/api/v2/packages?monitor=requires&section=results').json()
    assert listing['total'] == listing['maintenance_labels']['Requires'] == 1
    assert listing['items'][0]['monitors']['requires']['data']['requirements'][0]['satisfaction'] == 'unsatisfied'


@pytest.mark.parametrize('status,mark,tone', [('satisfied', '✓', 'positive'), ('unsatisfied', '✗', 'negative'),
                                            ('unknown', 'Not mapped', 'muted')])
def test_requires_document_is_small_and_status_has_text_meaning(snapshot, tmp_path, monkeypatch, status, mark, tone):
    monkeypatch.setattr(state, 'compare', lambda *args: 'outdated')
    add_requires(snapshot)
    if status == 'unsatisfied':
        snapshot['sources']['runtime-package']['version'] = '3.7'
    elif status == 'unknown':
        snapshot['dependency_packages'] = {}
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    client = ProjectedClient(create_app(db))
    page = client.get('/api/ui/packages/binutils').json()
    section = next(s for s in page['sections'] if s['id'] == 'requires')
    assert section['title'] == 'Requires' and not section['collapsible']
    assert section['fields'] == section['entries'] == section['notes'] == []
    row = section['table']['rows'][0]['cells']
    assert row[0]['lines'][0][0]['text'] == 'Python'
    assert row[1]['lines'][0][0]['text'] == '>=3.8'
    assert [value['text'] for value in row[1]['lines'][0] if value['kind'] == 'code'] == ['>=3.8', '>=3.10']
    rendered = next(value for value in row[1]['lines'][0] if value['text'] == mark)
    assert (rendered['text'], rendered['tone']) == (mark, tone)
    if status != 'unknown':
        assert rendered['title']  # non-colour meaning, not just a green/red glyph
    assert [s['id'] for s in page['sections'] if s['collapsible']] == ['checks']
    focused = client.get('/api/ui/packages?monitor=requires').json()
    assert focused['total'] == 1
    lines = focused['table']['rows'][0]['cells'][1]['lines']
    assert [v['text'] for v in lines[0]] == ['3.9.0', '→', '3.10.0']
    assert lines[1][-1]['text'] == mark


@pytest.mark.parametrize('mapping', [{'python': '../path'}, {'Upper': 'python'}, {'python': True}, []])
def test_distribution_mapping_is_validated(configured_path, mapping):
    document = tomlkit.parse(configured_path.read_text())
    document['openruyi'] = {'dependencies': mapping}
    configured_path.write_text(tomlkit.dumps(document))
    with pytest.raises(ValueError, match='openruyi.dependencies'):
        cfg.load(configured_path)


def test_dependency_mapping_is_published_even_on_an_idle_heartbeat(config, snapshot, monkeypatch, tmp_path):
    config.update(monitors={'enabled': ['requires']}, openruyi={'dependencies': {'python': 'runtime-package'}})
    config['native']['binutils'] = {'source': 'pypi', 'pypi': 'widget'}
    monkeypatch.setattr(state, 'compare', lambda *args: 'outdated')
    monkeypatch.setattr(monitor.cfg, 'require_unchanged', lambda *args: None)
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={'info': {'requires_python': '>=3.8' if '/3.9.0/' in str(request.url) else '>=3.10'}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        io = IO(client=client)
        first = monitor.collect(config, 'unused', db, io=io)
        assert first['dependency_packages'] == {'python': 'runtime-package'}
        assert monitor.collect(config, 'unused', db, io=io)['generation'] == first['generation']
        config['openruyi']['dependencies']['python'] = 'reviewed-other-package'
        second = monitor.collect(config, 'unused', db, io=io)
    assert second['dependency_packages'] == {'python': 'reviewed-other-package'}
    assert second['monitors'] == first['monitors'] and len(calls) == 2


def test_requirement_contract_rejects_unattributed_links():
    comparison = change()
    declaration = {key: value for key, value in comparison.items() if key not in {'current', 'target'}}
    declaration['constraint'] = {**comparison['target'], 'url': 'javascript:alert(1)'}
    with pytest.raises(ValueError, match='HTTPS'):
        monitor_model.finding('requires', 'Requires', 'test', [], 'https://example.org/',
                              scope='upgrade', target_version='2', requirement=declaration)
