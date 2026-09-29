"""Inline controls use the same query/index."""
from urllib.parse import parse_qs, urlsplit

from tests.helpers.listing import document, follow, modes, facet_destination, inline_navigation
from tracker.presentation import navigation as presentation_navigation


def parameters(choice):
    return parse_qs(urlsplit(choice['href']).query)


def test_build_rows_replace_only_their_own_architecture(scoped_client):
    page = document(scoped_client, {'monitor': 'build', 'buildsystem': 'cmake',
                                   'build': ['rva23:succeeded', 'rva20:failed']})
    controls = page['controls']
    assert all(row['icon'] for row in controls['choice_rows'])
    assert [row['label'] for row in [row for row in controls['choice_rows'] if row['icon']]] == ['rva23', 'rva20', 'x86_64']
    hidden = [(p['name'], p['value']) for p in controls['hidden']]
    assert ('build', 'rva23:succeeded') in hidden and ('build', 'rva20:failed') in hidden
    assert ('buildsystem', 'cmake') in hidden
    for row in [row for row in controls['choice_rows'] if row['icon']]:
        assert sum(choice['selected'] for choice in row['choices']) == (row['label'] != 'x86_64')
        for choice in row['choices']:
            facet_destination(scoped_client, page, choice)
            query = parameters(choice)
            chosen = query.get('build', [])
            target = row['label']
            assert len([value for value in chosen if value.startswith(target + ':')]) <= 1
            if choice['count'] == 0:
                assert len(chosen) == 1 and chosen[0].startswith(target + ':')
                continue
            for other in ('rva23:succeeded', 'rva20:failed'):
                if not other.startswith(target + ':'):
                    assert other in chosen


def test_overview_uses_only_observed_target_statuses(scoped_client):
    page = document(scoped_client, {})
    build_filters = [row for row in page['controls']['choice_rows'] if row['icon']]
    assert len(build_filters) == 3
    raw = scoped_client.get('/api/v2/packages').json()
    observed = {item['label'] for statuses in raw['build_statuses'].values() for item in statuses}
    assert all({option['label'] for option in facet['choices']} == observed for facet in build_filters)


def test_sidebar_and_package_badge_share_appearance_identity(scoped_client):
    page = document(scoped_client, {'buildsystem': 'cmake'})
    sidebar, = page['global_navigation']
    selected, = [choice for choice in sidebar['choices'] if choice['selected']]
    assert selected['appearance'] == 'buildsystem:cmake'
    badges = [value for row in page['table']['rows'] for line in row['cells'][0]['lines']
              for value in line if value['text'] == 'cmake']
    assert badges and all(value['appearance'] == selected['appearance'] for value in badges)
    empty = document(scoped_client, {'buildsystem': 'missing'})
    selected, = [choice for choice in empty['global_navigation'][0]['choices'] if choice['selected']]
    assert selected['count'] == 0
    assert follow(scoped_client, empty['controls']['active'][0]['href'])['total'] > 0


def test_build_link_removal_preserves_other_selected_targets():
    links = presentation_navigation.Links({'build': ['rva23:failed', 'rva20:blocked'], 'monitor': 'build'})
    removed = parse_qs(urlsplit(presentation_navigation.filter_link(links, 'build', 'rva23:')).query)
    assert removed['build'] == ['rva20:blocked']


def test_selected_chips_clear_only_one_dimension(scoped_client):
    query = {'buildsystem': 'cmake', 'maintenance': 'Signature',
             'build': ['rva23:succeeded', 'rva20:failed'], 'q': 'foo', 'per_page': 1}
    page = document(scoped_client, query)
    assert {chip['label'] for chip in page['controls']['active']} == {
        'BuildSystem: cmake', 'Signature', 'rva23: Succeeded', 'rva20: Failed', 'Search: foo'}
    for chip in page['controls']['active']:
        remaining = parameters(chip)
        assert remaining['page'] == ['1'] and remaining['per_page'] == ['1']
        removed = {'Search: foo': 'q', 'BuildSystem: cmake': 'buildsystem',
                   'Signature': 'maintenance'}.get(chip['label'])
        if removed:
            assert removed not in remaining
            assert remaining['build'] == query['build']
        else:
            target = chip['label'].split(':')[0]
            assert remaining['build'] == [v for v in query['build'] if not v.startswith(target + ':')]
        for key in ('q', 'buildsystem', 'maintenance'):
            if key != removed:
                assert remaining[key] == [query[key]]
        assert follow(scoped_client, chip['href'])['total'] >= page['total']


def test_exact_check_without_a_selector_keeps_a_removable_indicator(scoped_client):
    page = document(scoped_client, {'monitor': 'fixture_signature', 'check': 'error', 'buildsystem': 'cmake'})
    indicator, = [chip for chip in page['controls']['active'] if chip['label'].startswith('Check:')]
    assert indicator['label'] == 'Check: CheckFailed'
    query = parameters(indicator)
    assert 'check' not in query and query['buildsystem'] == ['cmake']
    assert follow(scoped_client, indicator['href'])['total'] > page['total']


def test_inline_destinations_match_counts_and_search_preserves_filters(scoped_client):
    query = {'buildsystem': 'cmake', 'maintenance': 'Signature',
             'build': ['rva23:succeeded', 'rva20:failed'], 'q': 'foo', 'per_page': 1}
    page = document(scoped_client, query)
    hidden = page['controls']['hidden']
    assert [p['value'] for p in hidden if p['name'] == 'build'] == query['build']
    assert next(p['value'] for p in hidden if p['name'] == 'maintenance') == 'Signature'
    for menu in inline_navigation(page):
        for choice in menu['choices']:
            facet_destination(scoped_client, page, choice)
            destination = parameters(choice)
            assert destination['q'] == ['foo'] and destination['page'] == ['1']
            assert destination['per_page'] == ['1']
            if choice['count'] == 0:
                continue
            assert destination['buildsystem'] == ['cmake']
            if not destination.get('check'):
                if menu['label'] != 'Alerts':
                    assert destination['maintenance'] == ['Signature']
                for value in query['build']:
                    if not value.startswith(menu['label'] + ':'):
                        assert value in destination.get('build', [])

    plain = document(scoped_client, {})
    assert all(not c['selected'] and c['label'] != 'Clear filter'
               for menu in inline_navigation(plain) for c in menu['choices'])


def test_version_updates_selects_newer_versions_and_keeps_global_scope(scoped_client):
    for scope, expected in [({}, {'binutils', 'foo4'}),
                            ({'buildsystem': 'cmake'}, {'binutils'}),
                            ({'q': 'foo'}, {'foo4'}),
                            ({'q': 'foo', 'buildsystem': 'cmake'}, set())]:
        for mode in ({}, {'check': 'uncovered'}, {'check': 'failed'},
                     {'freshness': 'retained'}, {'signal': 'security'}):
            page = document(scoped_client, {'monitor': 'version', **scope, **mode})
            choice, = [choice for choice in modes(page) if choice['label'] == 'Outdated']
            selected = follow(scoped_client, choice['href'])
            assert choice['count'] == selected['total'] == len(expected)
            assert {row['key'] for row in selected['table']['rows']} == expected
            assert next(c for c in modes(selected) if c['label'] == 'Outdated')['selected']
            query = parameters(choice)
            assert not {'check', 'freshness', 'signal'} & query.keys()
            assert all(query[key] == [value] for key, value in scope.items())
            chip, = [c for c in selected['controls']['active'] if c['label'] == 'Outdated']
            cleared = parameters(chip)
            assert 'view' not in cleared
            assert cleared['monitor'] == ['version']
            assert all(cleared[key] == [value] for key, value in scope.items())
            assert follow(scoped_client, chip['href']) == document(scoped_client, {'monitor': 'version', **scope})
    assert not modes(document(scoped_client, {}))


def test_version_updates_composes_with_related_filters(scoped_client):
    page = document(scoped_client, {'monitor': 'version', 'view': 'updates',
                                   'signal': 'security', 'buildsystem': 'cmake'})
    assert page['total'] == 0
    updates_chip = next(c for c in page['controls']['active'] if c['label'] == 'Outdated')
    assert parameters(updates_chip)['signal'] == ['security']
    related_chip = next(c for c in page['controls']['active'] if c['label'] == 'security')
    assert parameters(related_chip)['view'] == ['updates']
    assert follow(scoped_client, related_chip['href'])['total'] == 1
