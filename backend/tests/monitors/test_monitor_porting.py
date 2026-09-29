from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from tests.conftest import ProjectedClient
from tracker import state
from tracker.api import create_app
from tracker.monitors import (
    model as monitor_model,
    registry as monitor_registry,
    runner as monitor,
    yanked as monitor_yanked,
)
from tracker.providers.client import IO


def test_port_runs_through_heartbeat_storage_api_and_facets(config, snapshot, monkeypatch, tmp_path):
    config['native']['binutils'] = {'source': 'pypi', 'pypi': 'upstream-fixture'}
    config.update(monitors={'enabled': ['yanked']}, config_digest='fixture', nv_digest='fixture')
    monkeypatch.setitem(monitor_registry.REGISTRY, 'yanked', monitor_yanked)
    monkeypatch.setattr(monitor.cfg, 'require_unchanged', lambda config, path: None)
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    response = {'info': {'name': 'upstream-fixture', 'version': '3.9.0', 'yanked': True}}
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if isinstance(response, Exception):
            raise response
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        io = IO(client=client, ttl=0)
        collected = monitor.collect(config, 'unused', db, io=io)
        original = deepcopy(collected['monitors']['binutils']['yanked'])
        assert original['status'] == 'ok'
        assert calls == ['https://pypi.org/pypi/upstream-fixture/3.9.0/json']
        assert collected['sources'] == snapshot['sources']
        api = ProjectedClient(create_app(db))
        listing = api.get('/api/v2/packages?maintenance=Yanked').json()
        assert listing['total'] == listing['maintenance_labels']['Yanked'] == 1
        assert listing['items'][0]['name'] == 'binutils'
        assert api.get('/api/v2/packages/binutils').json()['monitors']['yanked']['data']['findings'][0]['facts'][0]['value'] is True

        # Exercise the real runner with the same port, not a fake retention branch.
        response = httpx.ConnectError('offline')
        plan = monitor.plan(config, collected, 'binutils', 'yanked')
        failed = monitor.execute('yanked', plan, io, original)
        assert failed['status'] == 'error'
        assert failed['findings'] == original['findings']
        assert failed['checked_at'] == original['checked_at']
        collected['monitors']['binutils']['yanked'] = failed
        projection = monitor_model.project(collected, 'binutils', datetime.now(timezone.utc))
        assert projection['findings'][0]['stale']

        collected['sources']['binutils']['version'] = '4.0'
        changed = monitor.plan(config, collected, 'binutils', 'yanked')
        assert monitor.execute('yanked', changed, io, original)['findings'] == []
        assert monitor_model.project(collected, 'binutils', datetime.now(timezone.utc))['findings'] == []

        response = {'info': {'name': 'upstream-fixture', 'version': '3.9.0', 'yanked': False}}
        healthy = monitor.execute('yanked', plan, io, original)
        assert healthy['status'] == 'ok' and healthy['findings'] == []


@pytest.mark.parametrize('bad_inputs', [
    lambda *args: 42,
    lambda *args: {'invalid': object()},
    lambda *args: {'invalid': float('nan')},
])
def test_input_contract_errors_are_local(config, snapshot, monkeypatch, bad_inputs):
    monkeypatch.setitem(monitor_registry.REGISTRY, 'fixture', SimpleNamespace(
        VERSION=1, HOSTS=set(), inputs=bad_inputs, check=lambda *args: pytest.fail('invalid inputs')))
    result = monitor.check(config, snapshot, 'binutils', 'fixture', None)
    assert result['status'] == 'error' and result['error'] in ('ValueError', 'TypeError')


def test_one_broken_input_does_not_stop_other_packages(config, snapshot, monkeypatch, tmp_path):
    def inputs(package, configured):
        if package['name'] == 'binutils':
            raise ValueError('invalid identity')
        return {}

    checked = []
    monkeypatch.setitem(monitor_registry.REGISTRY, 'fixture', SimpleNamespace(
        VERSION=1, HOSTS=set(), inputs=inputs,
        check=lambda subject, *args: checked.append(subject['name']) or
              {'status': 'ok', 'findings': [], 'note': None}))
    config.update(monitors={'enabled': ['fixture']}, config_digest='fixture', nv_digest='fixture')
    monkeypatch.setattr(monitor.cfg, 'require_unchanged', lambda config, path: None)
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    result = monitor.collect(config, 'unused', db, io=SimpleNamespace(for_hosts=lambda hosts, **kwargs: None))
    assert result['monitors']['binutils']['fixture']['status'] == 'error'
    assert set(checked) == {'foo3', 'foo4', 'untracked'}


def test_optional_machine_code_is_independent_of_display_label():
    old = monitor_model.evidence('A display label', True, 'fixture', 'https://example.org/')
    new = monitor_model.evidence('A different display label', True, 'fixture', 'https://example.org/', code='stable_key')
    for fact in (old, new):
        monitor_model.finding('test', 'Signal', 'test', [fact], 'https://example.org/')
    with pytest.raises(ValueError, match='code'):
        monitor_model.finding('test', 'Signal', 'test', [{**new, 'code': 'Display text'}], 'https://example.org/')


def test_v2_port_catalog_checks_and_facets_share_the_same_observation(config, snapshot, monkeypatch, tmp_path):
    config['native']['binutils'] = {'source': 'pypi', 'pypi': 'upstream-fixture'}
    config.update(monitors={'enabled': ['yanked']}, config_digest='fixture', nv_digest='fixture')
    monkeypatch.setitem(monitor_registry.REGISTRY, 'yanked', monitor_yanked)
    monkeypatch.setattr(monitor_yanked, 'TITLE', 'Release files', raising=False)
    monkeypatch.setattr(monitor.cfg, 'require_unchanged', lambda config, path: None)
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'info': {'name': 'upstream-fixture', 'version': '3.9.0', 'yanked': True}}))) as client:
        collected = monitor.collect(config, 'unused', db, io=IO(client=client))
        # Metadata is published with observations; an idle heartbeat writes neither.
        generation = collected['generation']
        assert monitor.collect(config, 'unused', db, io=IO(client=client))['generation'] == generation
    api = ProjectedClient(create_app(db))
    listing = api.get('/api/v2/packages?monitor=yanked&section=coverage').json()
    assert {'id': 'yanked', 'title': 'Yanked', 'kind': 'evidence'} in listing['monitors']
    assert listing['total'] == 5  # Focusing is not silently excluding unconfigured packages.
    assert listing['check_statuses'] == {'not_configured': 4, 'ok': 1}
    assert listing['maintenance_labels'] == {'Yanked': 1, 'Outdated': 2, 'Untracked': 2}
    result = listing['items'][0]['monitors']['yanked']
    assert result['data'] == {
        'kind': 'evidence', 'finding_count': 1,
        'labels': [{'label': 'Yanked', 'count': 1, 'stale': False}],
        'entries': [{'id': 'yanked:release-yanked', 'title': 'upstream-fixture 3.9.0', 'stale': False,
                     'scope': 'current', 'target_version': None, 'tags': [],
                     'evidence_url': 'https://pypi.org/pypi/upstream-fixture/3.9.0/json'}],
    }
    assert result['check']['status'] == 'ok'
    selected = api.get('/api/v2/packages?monitor=yanked&check=not_configured').json()
    assert selected['total'] == 4 and selected['maintenance_labels'] == {'Outdated': 1, 'Untracked': 2}
    assert selected['check_statuses'] == {'not_configured': 4, 'ok': 1}
    one = api.get('/api/v2/packages?monitor=yanked&maintenance=Yanked').json()
    assert one['total'] == one['counts']['all'] == 1
    assert one['check_statuses'] == {'ok': 1}
    detail = api.get('/api/v2/packages/binutils').json()['monitors']['yanked']
    assert detail['data']['findings'][0]['facts'][0]['value'] is True
    assert detail['check'] == result['check']
    assert api.get('/api/v2/packages?monitor=not-registered').status_code == 422
    assert api.get('/api/v2/packages?check=ok').status_code == 422
