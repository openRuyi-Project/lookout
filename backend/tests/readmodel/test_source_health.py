import json
import sys

import pytest

from tests.conftest import ProjectedClient
from tracker import collector, state
from tracker.api import create_app


@pytest.fixture
def source_snapshot():
    now = state.utcnow()
    snapshot = state.empty()
    snapshot.update(generation=1, last_attempt=now, targets=[], spec_interval_seconds=60)
    snapshot['components'] = {
        key: state.success({}, {}, now)
        for key in ('inventory', 'source_index', 'targets', 'builds', 'nvchecker')
    }
    snapshot['components']['spec_git'] = state.success({}, {'catalog_head': 'a' * 40}, now)
    snapshot['specs'] = {
        name: state.success({}, {'head': 'a' * 40, 'metadata': {'name': name, 'version': '1'}}, now)
        for name in ('widget', 'gadget')
    }
    return snapshot


def source_client(tmp_path, snapshot):
    database = tmp_path / 'state.sqlite3'
    state.commit(database, snapshot)
    return ProjectedClient(create_app(database))


@pytest.mark.parametrize('failed_count', [0, 1, 2])
def test_readiness_accounts_for_source_check_failures(tmp_path, source_snapshot, failed_count):
    for name in list(source_snapshot['specs'])[:failed_count]:
        source_snapshot['specs'][name].update(metadata=None, error='SPEC input rejected')
    client = source_client(tmp_path, source_snapshot)
    response = client.get('/readyz')
    assert response.status_code == 200
    assert response.json()['status'] == ('degraded' if failed_count else 'ready')
    status = client.get('/api/v2/status').json()
    assert status['source_versions'] == 2 - failed_count
    assert status['errors'] == ([f'Source checks failed for {failed_count} of 2 packages'] if failed_count else [])
    assert client.get('/healthz').json() == {'status': 'ok'}


def test_repository_failure_does_not_count_retained_source_facts_as_parse_errors(tmp_path, source_snapshot):
    source_snapshot['components']['spec_git']['error'] = 'git fetch timeout'
    client = source_client(tmp_path, source_snapshot)
    status = client.get('/api/v2/status').json()
    assert status['source_versions'] == 2
    assert status['errors'] == ['git fetch timeout']
    assert client.get('/readyz').json()['status'] == 'degraded'


def test_disabled_spec_collection_does_not_report_parse_failure(tmp_path, source_snapshot):
    source_snapshot['specs'] = {}
    source_snapshot['components'].pop('spec_git')
    source_snapshot['sources'] = {'widget': {}}
    client = source_client(tmp_path, source_snapshot)
    assert client.get('/api/v2/status').json()['errors'] == []
    assert client.get('/readyz').json()['status'] == 'ready'


@pytest.mark.parametrize('metadata', [None, {'version': '1'}])
def test_cli_and_api_count_effective_source_not_obs(tmp_path, source_snapshot, monkeypatch, capsys, metadata):
    source_snapshot['specs']['widget'].update(metadata=metadata, error='native SPEC parse failed')
    source_snapshot['sources'] = {name: {'version': '9'} for name in ('widget', 'gadget', 'obs-only')}
    client = source_client(tmp_path, source_snapshot)
    monkeypatch.setattr(collector.cfg, 'load', lambda _: {'native': {}})
    monkeypatch.setattr(collector, 'check_specs', lambda *a: source_snapshot)
    monkeypatch.setattr(sys, 'argv', ['collector', '--only', 'specs', '--config', 'unused', '--db', 'unused'])
    assert collector.main() == 0
    cli = json.loads(capsys.readouterr().out)
    status = client.get('/api/v2/status').json()
    assert cli['source_versions'] == status['source_versions'] == (2 if metadata else 1)
