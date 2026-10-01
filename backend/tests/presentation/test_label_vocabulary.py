"""A category has one name in facts, summaries, controls and saved URLs."""
from tests.helpers.query import conjunction, query_url, terms_in
from tracker.presentation.labels import appearance, palettes, priority
from copy import deepcopy

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
    raw = client.get(query_url('/api/v2/packages', {'maintenance': 'LicenseDiff'})).json()
    assert {row['name'] for row in raw['items']} == {'binutils', 'foo3'}
    assert {k: v for k, v in raw['maintenance_labels'].items() if v} == {'LicenseDiff': 2, 'Outdated': 1}
    for row in raw['items']:
        assert row['monitors']['license']['data']['labels'][0]['label'] == 'LicenseDiff'
        detail = client.get('/api/v2/packages/' + row['name']).json()
        assert detail['monitors']['license']['data']['findings'][0]['label'] == 'LicenseDiff'
    page = client.get(query_url('/api/ui/packages', {'maintenance': 'LicenseDiff'})).json()
    facet = next(n for n in page['controls']['choice_rows'] if n['label'] == 'Alerts')
    assert {option['label'] for option in facet['choices']} >= {'Outdated', 'LicenseDiff', 'Untracked'}
    assert next(option for option in facet['choices'] if option['selected'])['count'] == 2
    for row in page['table']['rows']:
        labels = [value for line in row['cells'][0]['lines'] for value in line if value['kind'] == 'tag']
        license_label, = [value for value in labels if value['text'].startswith('LicenseDiff')]
        assert terms_in(license_label['href']) == []
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
    raw = client.get('/api/v2/packages', params=dict(conjunction({'maintenance': new}).parameters())).json()
    assert raw['total'] == 1
    assert {k: v for k, v in raw['maintenance_labels'].items() if v} == {new: 1, 'Outdated': 1}
    result = raw['items'][0]['monitors'][monitor]
    assert result['title'] == new and result['data']['labels'][0]['label'] == new
    page = client.get('/api/ui/packages', params=dict(conjunction({'maintenance': new}).parameters())).json()
    badge, = [value for line in page['table']['rows'][0]['cells'][0]['lines']
              for value in line if value['text'] == new]
    assert terms_in(badge['href']) == []
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


@pytest.mark.parametrize('selected', [False, True])
def test_custom_fallback_keeps_its_identity_and_last_position(selected):
    from tracker.presentation.navigation import global_navigation, Links
    from tracker.presentation.values import buildsystem
    payload = {'buildsystems': {'_not_detected': 4, 'cmake': 2, 'custom': 1, 'meson': 3}}
    query = {'filters': conjunction({'buildsystem': '_not_detected'}).model_dump()} if selected else {}
    navigation, = global_navigation(payload, Links(query))
    assert [choice.label for choice in navigation.choices] == ['cmake', 'custom', 'meson', '❔ custom']
    assert navigation.choices[-1].count == 4
    choice = navigation.choices[-1]
    assert choice.selected is selected
    assert choice.icon is None
    assert terms_in(choice.href) == ([] if selected else [('buildsystem', '_not_detected')])
    assert buildsystem('cmake', Links()).variant == 'solid'


@pytest.mark.parametrize('status', ['not_declared', 'unknown'])
def test_custom_fallback_detail_retains_observation_status(status):
    from tracker.presentation.source import source_sections
    section, = source_sections({'id': 'source', 'data': {'buildsystem_status': status}}, None)
    value, = section.fields[0].values
    assert (section.fields[0].label, value.text) == ('BuildSystem', '❔ custom')
    assert value.title == ('Not declared' if status == 'not_declared' else 'Not observed')


def test_declared_icons_follow_identity_without_frontend_category_rules():
    from tracker.presentation.navigation import global_navigation, Links
    payload = {'buildsystems': {'new-tool': 3, '_not_detected': 1},
               'presentation': {'buildsystems': {'new-tool': {'icon': 'gopher'}}}}
    choices = global_navigation(payload, Links())[0].choices
    assert choices[0].icon == 'gopher'
    assert choices[1].icon is None
    payload['presentation']['buildsystems']['new-tool']['icon'] = 'rust'
    changed = global_navigation(payload, Links())[0].choices
    assert changed[0].icon == 'rust' and changed[0].href == choices[0].href


def test_emphasis_has_one_palette_and_order_not_provider_severity():
    groups = [('Outdated', 'Advisory', 'DepMismatch', 'EOL', 'Yanked'),
              ('DepChanges', 'LicenseDiff'), ('Untracked', 'CheckFailed')]
    theme = palettes()
    for rank, labels in enumerate(groups):
        assert {priority(label) for label in labels} == {rank}
        assert {priority(appearance(label)) for label in labels} == {rank}
        assert len({theme[appearance(label)]['background'] for label in labels}) == 1
    assert len({theme[appearance(group[0])]['background'] for group in groups}) == 3
