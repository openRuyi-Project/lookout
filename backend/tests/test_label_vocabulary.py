"""A category has one name in facts, summaries, controls and saved URLs."""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from tracker import monitor_model, state
from test_presentation import client_for


def test_aliases_coalesce_once_per_finding_without_hiding_distinct_signals():
    findings = [dict(label='LicenseChange', tags=['License', 'LicenseChange'], stale=False),
                dict(label='License', tags=[], stale=True),
                dict(label='SecurityReview', tags=['Security', 'KEV'], stale=False)]
    before = deepcopy(findings)
    assert monitor_model.summarize(findings) == [
        dict(label='License', count=2, stale=True),
        dict(label='Security', count=1, stale=False),
        dict(label='KEV', count=1, stale=False),
    ]
    assert findings == before
    assert monitor_model.canonical_label('FixtureSignal') == 'FixtureSignal'


def test_legacy_snapshot_and_filter_use_the_current_category(snapshot, tmp_path):
    stamp = state.utcnow()
    snapshot['monitors'] = {}
    for name, label in [('binutils', 'LicenseChange'), ('foo3', 'License')]:
        snapshot['monitors'][name] = {'license': {
            'subject': monitor_model.subject(snapshot, name), 'status': 'ok', 'checked_at': stamp,
            'findings': [monitor_model.finding(name, label, 'MIT → ISC', [], 'https://example.org/')],
        }}
    before = deepcopy(snapshot)
    client, _ = client_for(snapshot, tmp_path)
    raw = client.get('/api/v2/packages?maintenance=License').json()
    assert {row['name'] for row in raw['items']} == {'binutils', 'foo3'}
    assert raw['maintenance_labels'] == {'License': 2}
    assert client.get('/api/v2/packages?maintenance=LicenseChange').json() == raw
    for row in raw['items']:
        assert row['monitors']['license']['data']['labels'][0]['label'] == 'License'
        detail = client.get('/api/v2/packages/' + row['name']).json()
        assert detail['monitors']['license']['data']['findings'][0]['label'] == 'License'
    page = client.get('/api/ui/packages?maintenance=License').json()
    assert client.get('/api/ui/packages?maintenance=LicenseChange').json() == page
    facet, = [facet for facet in page['controls']['facets'] if facet['name'] == 'maintenance']
    assert [option['label'] for option in facet['options']] == ['All', 'License']
    assert next(option for option in facet['options'] if option['selected'])['count'] == 2
    for choice in page['controls']['active']:
        assert 'LicenseChange' not in choice['label']
    for row in page['table']['rows']:
        labels = [value for line in row['cells'][0]['lines'] for value in line if value['kind'] == 'tag']
        license_label, = [value for value in labels if value['text'].startswith('License')]
        assert parse_qs(urlsplit(license_label['href']).query)['maintenance'] == ['License']
    assert snapshot == before
