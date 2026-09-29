"""A category has one name in facts, summaries, controls and saved URLs."""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.helpers.documents import client_for
from tracker import state
from tracker.monitors import model as monitor_model


def test_labels_count_once_per_finding_without_hiding_distinct_signals():
    findings = [dict(label='LicenseDiff', tags=['LicenseDiff', 'LicenseDiff'], stale=False),
                dict(label='LicenseDiff', tags=[], stale=True),
                dict(label='Advisory', tags=['Advisory', 'KEV'], stale=False)]
    before = deepcopy(findings)
    assert monitor_model.summarize(findings) == [
        dict(label='LicenseDiff', count=2, stale=True),
        dict(label='Advisory', count=1, stale=False),
        dict(label='KEV', count=1, stale=False),
    ]
    assert findings == before


def test_current_category_links_match_their_filters(snapshot, tmp_path):
    stamp = state.utcnow()
    snapshot['monitors'] = {}
    snapshot['monitor_catalog'] = {'license': {'title': 'LicenseDiff'}}
    for name, label in [('binutils', 'LicenseDiff'), ('foo3', 'LicenseDiff')]:
        snapshot['monitors'][name] = {'license': {
            'subject': monitor_model.subject(snapshot, name), 'status': 'ok', 'checked_at': stamp,
            'findings': [monitor_model.finding(name, label, 'MIT → ISC', [], 'https://example.org/')],
        }}
    before = deepcopy(snapshot)
    client, _ = client_for(snapshot, tmp_path)
    raw = client.get('/api/v2/packages?maintenance=LicenseDiff').json()
    assert {row['name'] for row in raw['items']} == {'binutils', 'foo3'}
    assert raw['maintenance_labels'] == {'LicenseDiff': 2, 'Outdated': 1, 'Untracked': 0}
    for row in raw['items']:
        assert row['monitors']['license']['data']['labels'][0]['label'] == 'LicenseDiff'
        detail = client.get('/api/v2/packages/' + row['name']).json()
        assert detail['monitors']['license']['data']['findings'][0]['label'] == 'LicenseDiff'
    page = client.get('/api/ui/packages?maintenance=LicenseDiff').json()
    facet = next(n for n in page['controls']['choice_rows'] if n['label'] == 'Alerts')
    assert {option['label'] for option in facet['choices']} >= {'Outdated', 'LicenseDiff', 'Untracked'}
    assert next(option for option in facet['choices'] if option['selected'])['count'] == 2
    for row in page['table']['rows']:
        labels = [value for line in row['cells'][0]['lines'] for value in line if value['kind'] == 'tag']
        license_label, = [value for value in labels if value['text'].startswith('LicenseDiff')]
        assert parse_qs(urlsplit(license_label['href']).query)['maintenance'] == ['LicenseDiff']
    assert snapshot == before


@pytest.mark.parametrize('monitor,old,new', [
    ('license', 'License', 'LicenseDiff'), ('security', 'Security', 'Advisory'),
])
def test_saved_categories_use_current_names_everywhere_without_mutation(snapshot, tmp_path, monitor, old, new):
    snapshot['monitor_catalog'] = {monitor: {'title': old}}
    snapshot['monitors'] = {'binutils': {monitor: {
        'subject': monitor_model.subject(snapshot, 'binutils'), 'status': 'ok',
        'checked_at': state.utcnow(), 'evidence_revision': 'unchanged',
        'findings': [monitor_model.finding('fixture', old, 'Fixture evidence', [],
                    'https://example.org/', tags=[old, new])],
    }}}
    before = deepcopy(snapshot)
    client, db = client_for(snapshot, tmp_path)
    raw = client.get('/api/v2/packages', params={'maintenance': new}).json()
    assert raw['total'] == 1
    assert raw['maintenance_labels'] == {new: 1, 'Outdated': 1, 'Untracked': 0}
    result = raw['items'][0]['monitors'][monitor]
    assert result['title'] == new and result['data']['labels'][0]['label'] == new
    page = client.get('/api/ui/packages', params={'maintenance': new}).json()
    badge, = [value for line in page['table']['rows'][0]['cells'][0]['lines']
              for value in line if value['text'] == new]
    assert parse_qs(urlsplit(badge['href']).query)['maintenance'] == [new]
    assert badge['appearance'] == 'label:' + new and badge['variant'] == 'outline'
    detail = client.get('/api/ui/packages/binutils').json()
    section = next(section for section in detail['sections'] if section['id'] == monitor)
    assert section['title'] == new + ' · 1'
    checks = next(section for section in detail['sections'] if section['id'] == 'checks')
    row = next(row for row in checks['table']['rows'] if row['key'] == monitor)
    assert row['cells'][0]['lines'][0][0]['text'] == new
    assert state.read(db)['monitors'] == before['monitors']
    assert snapshot == before


def test_same_word_from_another_monitor_is_not_reinterpreted():
    from tracker.monitors.issues import observation_label
    assert observation_label('custom', 'License') == 'License'
    assert observation_label('security', 'CustomAdvisory') == 'CustomAdvisory'


def test_unmapped_buildsystem_is_last_and_identity_style_is_separate():
    from tracker.presentation.navigation import global_navigation, Links
    from tracker.presentation.values import buildsystem
    payload = {'buildsystems': {'_not_detected': 4, 'cmake': 2, 'meson': 3}}
    navigation, = global_navigation(payload, {}, Links())
    assert [choice.label for choice in navigation.choices] == ['cmake', 'meson', 'Undetected']
    assert navigation.choices[-1].count == 4
    assert parse_qs(urlsplit(navigation.choices[-1].href).query)['buildsystem'] == ['_not_detected']
    assert buildsystem('cmake', Links()).variant == 'solid'


def test_declared_icons_follow_identity_without_frontend_category_rules():
    from tracker.presentation.navigation import global_navigation, Links
    payload = {'buildsystems': {'new-tool': 3, '_not_detected': 1},
               'presentation': {'buildsystems': {'new-tool': {'icon': 'gopher'}}}}
    choices = global_navigation(payload, {}, Links())[0].choices
    assert choices[0].icon == 'gopher'
    assert choices[1].icon is None
    payload['presentation']['buildsystems']['new-tool']['icon'] = 'rust'
    changed = global_navigation(payload, {}, Links())[0].choices
    assert changed[0].icon == 'rust' and changed[0].href == choices[0].href
