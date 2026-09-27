"""A category has one name in facts, summaries, controls and saved URLs."""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from tests.helpers.documents import client_for
from tracker import state
from tracker.monitors import model as monitor_model


def test_labels_count_once_per_finding_without_hiding_distinct_signals():
    findings = [dict(label='License', tags=['License', 'License'], stale=False),
                dict(label='License', tags=[], stale=True),
                dict(label='Security', tags=['Security', 'KEV'], stale=False)]
    before = deepcopy(findings)
    assert monitor_model.summarize(findings) == [
        dict(label='License', count=2, stale=True),
        dict(label='Security', count=1, stale=False),
        dict(label='KEV', count=1, stale=False),
    ]
    assert findings == before


def test_current_category_links_match_their_filters(snapshot, tmp_path):
    stamp = state.utcnow()
    snapshot['monitors'] = {}
    snapshot['monitor_catalog'] = {'license': {'title': 'License'}}
    for name, label in [('binutils', 'License'), ('foo3', 'License')]:
        snapshot['monitors'][name] = {'license': {
            'subject': monitor_model.subject(snapshot, name), 'status': 'ok', 'checked_at': stamp,
            'findings': [monitor_model.finding(name, label, 'MIT → ISC', [], 'https://example.org/')],
        }}
    before = deepcopy(snapshot)
    client, _ = client_for(snapshot, tmp_path)
    raw = client.get('/api/v2/packages?maintenance=License').json()
    assert {row['name'] for row in raw['items']} == {'binutils', 'foo3'}
    assert raw['maintenance_labels'] == {'License': 2}
    for row in raw['items']:
        assert row['monitors']['license']['data']['labels'][0]['label'] == 'License'
        detail = client.get('/api/v2/packages/' + row['name']).json()
        assert detail['monitors']['license']['data']['findings'][0]['label'] == 'License'
    page = client.get('/api/ui/packages?maintenance=License').json()
    facet, = [facet for facet in page['controls']['facets'] if facet['name'] == 'maintenance']
    assert [option['label'] for option in facet['options']] == ['All', 'License']
    assert next(option for option in facet['options'] if option['selected'])['count'] == 2
    for row in page['table']['rows']:
        labels = [value for line in row['cells'][0]['lines'] for value in line if value['kind'] == 'tag']
        license_label, = [value for value in labels if value['text'].startswith('License')]
        assert parse_qs(urlsplit(license_label['href']).query)['maintenance'] == ['License']
    assert snapshot == before
