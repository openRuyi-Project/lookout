from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from fixtures import monitor_yanked
from tracker import monitor, monitor_model, state
from tracker.api import create_app
from tracker.monitor_io import IO


def test_port_runs_through_heartbeat_storage_api_and_facets(config, snapshot, monkeypatch, tmp_path):
    config['native']['binutils'] = {'source': 'pypi', 'pypi': 'upstream-fixture'}
    config.update(monitors={'enabled': ['yanked']}, config_digest='fixture', nv_digest='fixture')
    monkeypatch.setitem(monitor.REGISTRY, 'yanked', monitor_yanked)
    monkeypatch.setattr(monitor.cfg, 'load', lambda path: config)
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    response = {'urls': [{'yanked': True}, {'yanked': True}]}
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
        api = TestClient(create_app(db))
        listing = api.get('/api/v1/packages?maintenance=Yanked').json()
        assert listing['total'] == listing['maintenance_labels']['Yanked'] == 1
        assert listing['items'][0]['name'] == 'binutils'
        assert api.get('/api/v1/packages/binutils').json()['maintenance_findings'][0]['facts'][0]['value'] is True

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

        collected['sources']['binutils']['srcmd5'] = 'new-source'
        changed = monitor.plan(config, collected, 'binutils', 'yanked')
        assert monitor.execute('yanked', changed, io, original)['findings'] == []
        assert monitor_model.project(collected, 'binutils', datetime.now(timezone.utc))['findings'] == []

        response = {'urls': [{'yanked': False}]}
        healthy = monitor.execute('yanked', plan, io, original)
        assert healthy['status'] == 'ok' and healthy['findings'] == []


@pytest.mark.parametrize('bad_inputs', [
    lambda *args: 42,
    lambda *args: {'invalid': object()},
    lambda *args: {'invalid': float('nan')},
])
def test_input_contract_errors_are_local(config, snapshot, monkeypatch, bad_inputs):
    monkeypatch.setitem(monitor.REGISTRY, 'fixture', SimpleNamespace(
        VERSION=1, HOSTS=set(), inputs=bad_inputs, check=lambda *args: pytest.fail('invalid inputs')))
    result = monitor.check(config, snapshot, 'binutils', 'fixture', None)
    assert result['status'] == 'error' and result['error'] in ('ValueError', 'TypeError')


def test_one_broken_input_does_not_stop_other_packages(config, snapshot, monkeypatch, tmp_path):
    def inputs(package, configured):
        if package['name'] == 'binutils':
            raise ValueError('invalid identity')
        return {}

    checked = []
    monkeypatch.setitem(monitor.REGISTRY, 'fixture', SimpleNamespace(
        VERSION=1, HOSTS=set(), inputs=inputs,
        check=lambda subject, *args: checked.append(subject['name']) or
              {'status': 'ok', 'findings': [], 'note': None}))
    config.update(monitors={'enabled': ['fixture']}, config_digest='fixture', nv_digest='fixture')
    monkeypatch.setattr(monitor.cfg, 'load', lambda path: config)
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    result = monitor.collect(config, 'unused', db, io=SimpleNamespace(for_hosts=lambda hosts, **kwargs: None))
    assert result['monitors']['binutils']['fixture']['status'] == 'error'
    assert set(checked) == {'foo3', 'foo4', 'untracked'}


def test_additive_evidence_code_does_not_discard_older_structured_facts():
    old = monitor_model.evidence('A display label', True, 'fixture', 'https://example.org/')
    new = monitor_model.evidence('A different display label', True, 'fixture', 'https://example.org/', code='stable_key')
    for fact in (old, new):
        monitor_model.finding('test', 'Signal', 'test', [fact], 'https://example.org/')
    with pytest.raises(ValueError, match='code'):
        monitor_model.finding('test', 'Signal', 'test', [{**new, 'code': 'Display text'}], 'https://example.org/')


def test_v2_port_catalog_checks_and_facets_share_the_same_observation(config, snapshot, monkeypatch, tmp_path):
    config['native']['binutils'] = {'source': 'pypi', 'pypi': 'upstream-fixture'}
    config.update(monitors={'enabled': ['yanked']}, config_digest='fixture', nv_digest='fixture')
    monkeypatch.setitem(monitor.REGISTRY, 'yanked', monitor_yanked)
    monkeypatch.setattr(monitor_yanked, 'TITLE', 'Release files', raising=False)
    monkeypatch.setattr(monitor.cfg, 'load', lambda path: config)
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'urls': [{'yanked': True}]}))) as client:
        collected = monitor.collect(config, 'unused', db, io=IO(client=client))
        # Metadata is published with observations; an idle heartbeat writes neither.
        generation = collected['generation']
        assert monitor.collect(config, 'unused', db, io=IO(client=client))['generation'] == generation
    api = TestClient(create_app(db))
    listing = api.get('/api/v2/packages?monitor=yanked').json()
    assert {'id': 'yanked', 'title': 'Release files', 'kind': 'evidence'} in listing['monitors']
    assert listing['total'] == 5  # Focusing is not silently excluding unconfigured packages.
    assert listing['check_statuses'] == {'not_configured': 4, 'ok': 1}
    assert listing['maintenance_labels'] == {'Yanked': 1}
    result = listing['items'][0]['monitors']['yanked']
    assert result['data'] == {'kind': 'evidence', 'labels': [{'label': 'Yanked', 'count': 1, 'stale': False}]}
    assert result['check']['status'] == 'ok'
    selected = api.get('/api/v2/packages?monitor=yanked&check=not_configured').json()
    assert selected['total'] == 4 and selected['maintenance_labels'] == {}
    assert selected['check_statuses'] == {'not_configured': 4, 'ok': 1}
    one = api.get('/api/v2/packages?monitor=yanked&maintenance=Yanked').json()
    assert one['total'] == one['counts']['all'] == 1
    assert one['check_statuses'] == {'ok': 1}
    detail = api.get('/api/v2/packages/binutils').json()['monitors']['yanked']
    assert detail['data']['findings'][0]['facts'][0]['value'] is True
    assert detail['check'] == result['check']
    assert api.get('/api/v2/packages?monitor=not-registered').status_code == 422
    assert api.get('/api/v2/packages?check=ok').status_code == 422
