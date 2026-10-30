"""Status counts describe the full snapshot, independently of selected results."""
from copy import deepcopy
from urllib.parse import urlsplit

from tests.helpers.documents import client_for
from tracker.presentation.status import data_status


def test_status_links_do_not_inherit_result_filters(snapshot, tmp_path):
    client, _ = client_for(snapshot, tmp_path)
    all_rows = client.get('/api/ui/packages').json()
    filtered = client.get('/api/ui/packages?q=binutils&per_page=1').json()
    assert filtered['total'] < all_rows['total']
    assert filtered['data_status'] == all_rows['data_status']
    status = filtered['data_status']
    assert status['fields'][0]['values'][0]['text'] == 'All 5 packages'
    assert status['table']['rows']
    for row in status['table']['rows']:
        for line in row['cells'][1]['lines']:
            for value in line:
                if value['href']:
                    assert 'q=' not in urlsplit(value['href']).query
                    assert 'per_page=' not in urlsplit(value['href']).query
                    assert client.get('/api/ui/packages' + value['href'][1:]).status_code == 200
    detail = client.get('/api/ui/packages/binutils').json()
    assert detail['data_status'] == all_rows['data_status']


def test_status_preserves_errors_and_observation_times_without_mutation():
    collection = dict(packages=3, errors=['refresh failed', 'refresh failed'],
        obs_updated_at='2026-01-01T00:00:00Z', upstream_updated_at=None,
        last_attempt='2026-01-02T00:00:00Z')
    before = deepcopy(collection)
    result = data_status(collection, {'custom': {'error': 2, 'pending': 1}},
                         [{'id': 'custom', 'title': 'ThirdParty'}])
    assert result.title == 'Data status · Degraded'
    assert result.notes == ['refresh failed']
    assert result.fields[1].values[0].datetime == collection['obs_updated_at']
    assert result.fields[2].values[0].text == '—'
    assert result.fields[3].values[0].datetime == collection['last_attempt']
    assert result.table.rows[0].cells[0].lines[0][0].text == 'ThirdParty'
    assert collection == before
