"""A category has one name in facts, summaries, controls and saved URLs."""
from tests.helpers.query import conjunction, query_url, terms_in
from tracker.presentation.labels import appearance, palettes, priority
from copy import deepcopy
from datetime import datetime
from urllib.parse import urlsplit

import pytest

from tests.helpers.documents import client_for
from tests.conftest import make_snapshot
from tracker import state
from tracker.monitors import model as monitor_model
from tracker.readmodel.cache import ProjectionCache


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
    assert raw['maintenance_labels']['LicenseDiff'] == 2
    for row in raw['items']:
        assert row['monitors']['license']['data']['labels'][0]['label'] == 'LicenseDiff'
        detail = client.get('/api/v2/packages/' + row['name']).json()
        assert detail['monitors']['license']['data']['findings'][0]['label'] == 'LicenseDiff'
    page = client.get(query_url('/api/ui/packages', {'maintenance': 'LicenseDiff'})).json()
    choice = next(choice for group in page['controls']['choice_rows']
                  for choice in group['choices'] if choice['label'] == 'LicenseDiff')
    assert choice['selected'] and choice['count'] == 2
    for row in page['table']['rows']:
        labels = [value for line in row['cells'][0]['lines'] for value in line if value['kind'] == 'tag']
        license_label, = [value for value in labels if value['text'].startswith('LicenseDiff')]
        assert terms_in(license_label['href']) == [('maintenance', 'LicenseDiff')]
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
    assert raw['maintenance_labels'][new] == 1
    result = raw['items'][0]['monitors'][monitor]
    assert result['title'] == new and result['data']['labels'][0]['label'] == new
    page = client.get('/api/ui/packages', params=dict(conjunction({'maintenance': new}).parameters())).json()
    badge, = [value for line in page['table']['rows'][0]['cells'][0]['lines']
              for value in line if value['text'] == new + ' 1']
    assert terms_in(badge['href']) == [('maintenance', new)]
    assert badge['appearance'] == 'label:' + new and badge['size'] == 'normal'
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
    assert buildsystem('cmake', Links()).size == 'small'


def test_declared_custom_links_do_not_select_the_fallback(config, tmp_path, monkeypatch):
    stamp = '2026-01-01T12:00:00+00:00'
    monkeypatch.setattr(ProjectionCache, 'now', staticmethod(lambda: datetime.fromisoformat(stamp)))
    snapshot = make_snapshot(config, stamp)
    snapshot['specs']['foo3'] = state.success({}, {'metadata': {
        'name': 'foo3', 'version': '3.10.0', 'summary': 'Fixture', 'license': 'MIT',
        'url': 'https://example.org', 'description': 'Fixture', 'buildsystem': 'custom',
    }}, stamp)
    client, _ = client_for(snapshot, tmp_path)

    def follow(href, route='/api/ui/packages'):
        response = client.get(route + '?' + urlsplit(href).query)
        assert response.status_code == 200, response.text
        return response.json()

    def choices(document):
        navigation, = document['table']['columns'][0]['navigation']
        return {option['label']: option for option in navigation['choices']}

    initial = choices(follow('/?'))
    declared, fallback = initial['custom'], initial['❔ custom']
    assert declared['count'] == 1 and fallback['count'] == 4
    assert terms_in(declared['href']) == [('buildsystem', 'custom')]
    assert terms_in(fallback['href']) == [('buildsystem', '_not_detected')]
    assert {row['name'] for row in follow(declared['href'], '/api/v2/packages')['items']} == {'foo3'}
    assert {row['name'] for row in follow(fallback['href'], '/api/v2/packages')['items']} == {
        'binutils', 'foo4', 'unknown', 'untracked',
    }
    selected = follow(declared['href'])
    assert selected['total'] == declared['count']
    selected_choices = choices(selected)
    assert selected_choices['custom']['selected'] and not selected_choices['❔ custom']['selected']
    cleared = follow(selected_choices['custom']['href'])
    assert cleared['total'] == 5
    assert not any(option['selected'] for option in choices(cleared).values())


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
              ('DepChanges', 'LicenseDiff', 'CheckFailed'), ('Untracked',)]
    theme = palettes()
    for rank, labels in enumerate(groups):
        assert {priority(label) for label in labels} == {rank}
        assert {priority(appearance(label)) for label in labels} == {rank}
        assert len({theme[appearance(label)]['background'] for label in labels}) == 1
    assert len({theme[appearance(group[0])]['background'] for group in groups}) == 3


def test_palette_rejects_trailing_css_and_is_readable_on_white():
    from tracker.presentation.labels import tag_palette
    assert tag_palette('#123456;display:none') is None
    assert tag_palette('#fff') is None
    for source in ('#ffffff', '#000000', '#e9ce3b', '#3776ab'):
        palette = tag_palette(source)
        channels = [int(palette['background'][i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
        luminance = sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))
        assert 1.05 / (luminance + .05) >= 4.5
