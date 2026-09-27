"""Bounded fact queries share projections; no request collects or rebuilds data."""
from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from tracker.api import create_app
from tracker.readmodel.packages import PackageList
from tracker.readmodel import snapshot as view


@pytest.fixture
def query_client(snapshot, tmp_path, monkeypatch):
    snapshot['monitor_catalog'] = {'fixture': {'title': 'Fixture evidence'}}
    rows, collection = view.project_monitors(snapshot)
    for row in rows:
        if row['name'] != 'foo3':
            continue
        source = row['monitors']['source']['data']
        source['metadata'] = dict(name='foo3', version='3.10.0', summary='A searchable summary',
                                  license='MIT', url='https://example.org/foo3', description='Fixture description')
        build = row['monitors']['build']['data']['targets'][0]['flavors'][0]
        build['details'] = 'missing dependency lib-fixture >= 7'
        evidence = row['monitors']['fixture']
        evidence['check'].update(status='ok', error=None)
        evidence['data'].update(finding_count=1, labels=[dict(label='Fixture', count=1, stale=False)], findings=[{
            'id': 'fixture:advisory', 'label': 'Fixture', 'title': 'CVE-FIXTURE-1234',
            'evidence_url': 'https://example.org/advisory', 'scope': 'current', 'target_version': None,
            'stale': False, 'monitor': 'fixture', 'tags': [], 'facts': [
                {'key': 'Aliases', 'code': 'aliases', 'value': ['GHSA-fixture-alias'],
                 'source': 'Fixture', 'url': 'https://example.org/advisory', 'status': 'observed'}],
        }])
        evidence['dimensions'].update({'findings:fixture': ['yes'], 'check:fixture': ['ok']})
    index = PackageList(rows, snapshot['targets'])
    app = create_app(tmp_path / 'never-created.sqlite3')
    calls = []
    def read():
        calls.append(True)
        return snapshot, index, collection
    monkeypatch.setattr(app.state.projection, 'read', read)
    return TestClient(app), rows, calls


def test_include_is_response_selection_not_a_filter(query_client):
    client, _, _ = query_client
    all_fields = client.get('/api/v2/packages').json()
    selected = client.get('/api/v2/packages?include=version&include=source').json()
    assert selected['total'] == all_fields['total']
    assert selected['counts'] == all_fields['counts']
    for complete, partial in zip(all_fields['items'], selected['items']):
        assert partial['monitors'] == {mid: complete['monitors'][mid] for mid in ('version', 'source')}
    assert len(client.get('/api/v2/packages?include=version&include=version').json()['items'][0]['monitors']) == 1


def test_full_list_and_batch_match_single_package_and_one_snapshot(query_client):
    client, rows, calls = query_client
    before = deepcopy(rows)
    full = client.get('/api/v2/packages?detail=full&per_page=20&include=fixture&include=build').json()
    names = [row['name'] for row in reversed(full['items'])]
    calls.clear()
    batch = client.get('/api/v2/packages:batchGet', params=[
        *[('names', name) for name in names], ('include', 'fixture'), ('include', 'build')])
    assert batch.status_code == 200, batch.text
    assert len(calls) == 1
    assert [row['name'] for row in batch.json()['items']] == names
    for item in batch.json()['items']:
        single = client.get('/api/v2/packages/' + item['name'] + '?include=fixture&include=build').json()
        assert item == {key: value for key, value in single.items() if key != 'presentation'}
        assert item == next(row for row in full['items'] if row['name'] == item['name'])
    assert rows == before
    assert not client.app.state.projection.db.exists()


@pytest.mark.parametrize('query', ['CVE-FIXTURE', 'ghsa-fixture-alias', 'searchable summary', 'lib-fixture'])
def test_observation_search_finds_visible_evidence_and_metadata(query_client, query):
    client, _, _ = query_client
    assert client.get('/api/v2/packages', params={'q': query}).json()['total'] == 0
    found = client.get('/api/v2/packages', params={'q': query, 'search': 'observations'})
    assert found.status_code == 200, found.text
    assert [row['name'] for row in found.json()['items']] == ['foo3']
    assert found.json()['counts']['all'] == 1


def test_search_focus_filtering_and_inclusion_are_independent(query_client):
    client, _, _ = query_client
    parameters = {'q': 'GHSA-fixture-alias', 'search': 'observations', 'monitor': 'fixture',
                  'detail': 'full', 'per_page': 20, 'include': 'fixture'}
    response = client.get('/api/v2/packages', params=parameters).json()
    assert response['total'] == response['result_count'] == 1
    assert response['items'][0]['monitors']['fixture']['data']['findings'][0]['facts'][0]['value'] == ['GHSA-fixture-alias']
    assert client.get('/api/v2/packages', params={**parameters, 'monitor': 'version'}).json()['total'] == 0
    assert client.get('/api/v2/packages', params={**parameters, 'build': 'rva20:succeeded'}).json()['total'] == 0
    assert client.get('/api/v2/packages', params={**parameters, 'q': 'foo3', 'monitor': 'version'}).json()['total'] == 1


@pytest.mark.parametrize('path', [
    '/api/v2/packages?include=missing', '/api/v2/packages?include=',
    '/api/v2/packages?detail=full', '/api/v2/packages?detail=full&per_page=21',
    '/api/v2/packages?detail=bogus', '/api/v2/packages?search=bogus',
    '/api/v2/packages?per_pgae=5', '/api/v2/packages/foo3?include=missing',
    '/api/v2/packages/foo3?include=version&detail=full',
    '/api/v2/packages:batchGet', '/api/v2/packages:batchGet?names=',
    '/api/v2/packages:batchGet?names=foo3&names=foo3',
    '/api/v2/packages:batchGet?names=foo3&include=missing',
    '/api/v2/packages:batchGet?names=foo3&page=2',
    '/api/v2/packages:batchGet?' + '&'.join('names=p' + str(n) for n in range(21)),
])
def test_invalid_queries_are_explicit_errors(query_client, path):
    client, _, _ = query_client
    response = client.get(path)
    assert response.status_code == 422, response.text
    assert response.headers['cache-control'] == 'no-store'


def test_batch_missing_package_is_atomic(query_client):
    client, _, calls = query_client
    result = client.get('/api/v2/packages:batchGet?names=foo3&names=absent')
    assert result.status_code == 404
    assert 'items' not in result.json()
    assert len(calls) == 1


@pytest.mark.parametrize('path', [
    '/api/v2/packages?detail=full&per_page=20',
    '/api/v2/packages/foo3?include=version',
    '/api/v2/packages:batchGet?names=foo3',
])
def test_queries_do_not_invent_observations_before_snapshot(tmp_path, path):
    client = TestClient(create_app(tmp_path / 'absent.sqlite3'))
    response = client.get(path)
    assert response.status_code == 503
    assert 'items' not in response.json()
    assert not client.app.state.projection.db.exists()


def test_full_response_retains_failed_check_and_stale_evidence(query_client):
    client, rows, _ = query_client
    row = next(row for row in rows if row['name'] == 'foo3')
    observation = row['monitors']['fixture']
    observation['check'].update(status='error', stale=True, error='Fixture provider unavailable')
    observation['data']['findings'][0]['stale'] = True
    response = client.get('/api/v2/packages:batchGet?names=foo3&include=fixture').json()
    result = response['items'][0]['monitors']['fixture']
    assert {key: result['check'][key] for key in observation['check']} == observation['check']
    assert result['data']['findings'][0]['stale'] is True
    assert result['data']['findings'][0]['id'] == 'fixture:advisory'


def test_openapi_describes_response_shapes_and_bounds(query_client):
    client, _, _ = query_client
    spec = client.get('/openapi.json').json()
    listing = spec['paths']['/api/v2/packages']['get']
    params = {p['name']: p['schema'] for p in listing['parameters']}
    assert params['detail']['enum'] == ['summary', 'full']
    assert params['per_page']['maximum'] == 200
    batch = spec['paths']['/api/v2/packages:batchGet']['get']
    names = next(p for p in batch['parameters'] if p['name'] == 'names')
    assert names['required'] and names['schema']['maxItems'] == 20
    assert {'MonitoredPackage', 'MonitoredObservation'} == {
        variant['$ref'].rsplit('/', 1)[-1]
        for variant in spec['components']['schemas']['MonitoredList']['properties']['items']['items']['anyOf']}
