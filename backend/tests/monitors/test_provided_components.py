"""Component evidence resolves dependency versions, not archive version aliases."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from tests.conftest import ProjectedClient
from tracker import state
from tracker.api import create_app
from tracker.monitors import runner
from tracker.monitors.requires import model, monitor


COMPONENT = dict(identity={'ecosystem': 'CPANModule', 'name': 'Example::Part'},
                 version='0.4', source='MetaCPAN', url='https://metacpan.org/release/AUTHOR/Example-12')


def saved(snapshot):
    source = state.current_source(snapshot, 'binutils')
    at = state.utcnow()
    observation = dict(subject={'name': 'binutils', 'version': source['version'], 'revision': source['revision']},
        status='ok', scope='current_and_upgrade', input_status='pending', findings=[], checked_at=at,
        scope_checks={'current': {'status': 'ok', 'note': None, 'checked_at': at, 'provides': [deepcopy(COMPONENT)]}})
    snapshot.setdefault('monitors', {})['binutils'] = {'requires': observation}
    snapshot['monitor_catalog'] = {'requires': {'title': 'RuntimeDeps'}}
    return observation


def declaration():
    return dict(dependency='example.part', name='Example::Part', kind='runtime', scheme='perl_version',
        version_scope='component', identity=COMPONENT['identity'],
        current={'expression': '>= 1', 'source': 'MetaCPAN', 'url': COMPONENT['url']}, target=None)


def test_component_version_is_not_distribution_version(snapshot):
    saved(snapshot)
    result = model.assess(declaration(), snapshot, datetime.now(timezone.utc))
    assert result['package'] == 'binutils'
    assert result['observed']['version'] == '0.4'
    assert result['observed']['evidence_url'] == COMPONENT['url']
    assert result['satisfaction'] == 'unsatisfied'  # The archive's 3.9.0 would give the wrong answer.
    model.RequirementAssessment.model_validate(result)


@pytest.mark.parametrize('case', ['stale', 'source_changed', 'source_error', 'check_error', 'upgrade_only',
                                  'input_unavailable', 'duplicate', 'invalid'])
def test_unavailable_components_do_not_resolve(snapshot, case):
    observation = saved(snapshot)
    check = observation['scope_checks']['current']
    if case == 'stale':
        check['checked_at'] = '2000-01-01T00:00:00+00:00'
    elif case == 'source_changed':
        observation['subject']['revision'] = 'old'
    elif case == 'source_error':
        snapshot['sources']['binutils']['error'] = 'failed'
    elif case == 'check_error':
        check['status'] = 'error'
    elif case == 'upgrade_only':
        observation['scope_checks'] = {'upgrade': check}
    elif case == 'input_unavailable':
        observation['input_status'] = 'unsupported'
    elif case == 'duplicate':
        check['provides'] *= 2
    else:
        check['provides'][0]['version'] = None
    result = model.assess(declaration(), snapshot, datetime.now(timezone.utc))
    assert result['mapping'] == 'not_mapped'
    assert result['satisfaction'] == 'unknown'


def test_explicit_owner_does_not_supply_a_missing_component_version(snapshot):
    snapshot['dependency_packages'] = {'example.part': 'binutils'}
    result = model.assess(declaration(), snapshot, datetime.now(timezone.utc))
    assert result['mapping'] == 'mapped'
    assert result['observed']['version'] is None
    assert result['satisfaction'] == 'unknown'


def test_two_component_owners_are_ambiguous(snapshot):
    observation = saved(snapshot)
    source = state.current_source(snapshot, 'foo3')
    second = deepcopy(observation)
    second['subject'] = {'name': 'foo3', 'version': source['version'], 'revision': source['revision']}
    snapshot['monitors']['foo3'] = {'requires': second}
    assert model.assess(declaration(), snapshot, datetime.now(timezone.utc))['mapping'] == 'ambiguous'


def test_component_evidence_is_scoped_retained_and_revisioned(config, snapshot, monkeypatch):
    config['native']['binutils'] = {'source': 'pypi', 'pypi': 'example'}
    monkeypatch.setattr(state, 'compare', lambda *args: 'current')
    proposed = runner.plan(config, snapshot, 'binutils', 'requires')
    output = {'status': 'ok', 'findings': [], 'note': None, 'scope_checks': {
        'current': {'status': 'ok', 'note': None, 'provides': [deepcopy(COMPONENT)]}}}
    monkeypatch.setattr(monitor, 'check', lambda *args: deepcopy(output))
    class IO:
        def for_hosts(self, *args, **kwargs):
            return self
    old = runner.execute('requires', proposed, IO())
    assert old['status'] == 'ok'
    same = runner.execute('requires', proposed, IO(), old)
    assert (same['evidence_revision'], same['changed_at']) == (old['evidence_revision'], old['changed_at'])
    output['scope_checks']['current']['provides'][0]['version'] = '1.1'
    changed = runner.execute('requires', proposed, IO(), old)
    assert changed['evidence_revision'] != old['evidence_revision']
    output.update(status='error', scope_checks={'current': {'status': 'error', 'note': 'unavailable'}})
    failed = runner.execute('requires', proposed, IO(), changed)
    assert failed['scope_checks']['current']['provides'] == changed['scope_checks']['current']['provides']
    assert failed['scope_checks']['current']['checked_at'] == changed['scope_checks']['current']['checked_at']
    assert failed['scope_checks']['current']['status'] == 'error'


def test_component_link_is_visible_without_frontend_provider_code(snapshot, tmp_path, monkeypatch):
    from tracker.monitors.model import finding
    monkeypatch.setattr(state, 'compare', lambda *args: 'current')
    observation = saved(snapshot)
    required = declaration()
    required['constraint'] = required.pop('current')
    required.pop('target')
    observation['findings'] = [finding('part', 'Dependencies', 'Example::Part >= 1', [], COMPONENT['url'], requirement=required)]
    db = tmp_path / 'snapshot.sqlite3'
    state.commit(db, snapshot)
    client = ProjectedClient(create_app(db))
    response = client.get('/api/v2/packages/binutils')
    assert response.status_code == 200
    value = response.json()['monitors']['requires']['data']['requirements'][0]
    assert value['observed']['version'] == '0.4'
    assert value['version_scope'] == 'component'
    document = client.get('/api/ui/packages/binutils').json()
    section = next(s for s in document['sections'] if s['id'] == 'requires')
    assert section['table']['rows'][0]['cells'][2]['lines'][0][0]['href'] == COMPONENT['url']
