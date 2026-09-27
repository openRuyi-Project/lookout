"""Link-driven controls reuse the same query/index as dropdowns."""
from urllib.parse import parse_qs, urlsplit

from tracker import presentation
from test_listing_scope import scoped_client, document, follow


def parameters(choice):
    return parse_qs(urlsplit(choice['href']).query)


def test_build_rows_replace_only_their_own_architecture(scoped_client):
    page = document(scoped_client, {'monitor': 'build', 'buildsystem': 'cmake',
                                   'build': ['rva23:succeeded', 'rva20:failed']})
    controls = page['controls']
    assert controls['facets'] == []
    assert [row['label'] for row in controls['choice_rows']] == ['rva23', 'rva20', 'x86_64']
    hidden = [(p['name'], p['value']) for p in controls['hidden']]
    assert ('build', 'rva23:succeeded') in hidden and ('build', 'rva20:failed') in hidden
    assert ('buildsystem', 'cmake') in hidden
    for row in controls['choice_rows']:
        assert sum(choice['selected'] for choice in row['choices']) == 1
        for choice in row['choices']:
            query = parameters(choice)
            chosen = query.get('build', [])
            target = row['label']
            assert len([value for value in chosen if value.startswith(target + ':')]) <= 1
            for other in ('rva23:succeeded', 'rva20:failed'):
                if not other.startswith(target + ':'):
                    assert other in chosen
            assert choice['label'] != 'Issues'
            assert follow(scoped_client, choice['href'])['total'] == choice['count']


def test_overview_keeps_dropdowns_but_not_synthetic_issues(scoped_client):
    page = document(scoped_client, {})
    facets = [facet for facet in page['controls']['facets'] if facet['name'] == 'build']
    assert len(facets) == 3 and not page['controls']['choice_rows']
    assert all(option['label'] != 'Issues' for facet in facets for option in facet['options'])
    for monitor in ('', 'build'):
        assert document(scoped_client, {'monitor': monitor, 'build': 'rva20:issues'}) == document(
            scoped_client, {'monitor': monitor})


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
    assert follow(scoped_client, empty['global_navigation'][0]['choices'][0]['href'])['total'] > 0


def test_build_link_removal_preserves_other_selected_targets():
    links = presentation.Links({'build': ['rva23:failed', 'rva20:blocked'], 'monitor': 'build'})
    removed = parse_qs(urlsplit(presentation.filter_link(links, 'build', 'rva23:')).query)
    assert removed['build'] == ['rva20:blocked']


def test_visible_selections_are_not_repeated_as_active_chips(scoped_client):
    page = document(scoped_client, {'buildsystem': 'cmake', 'maintenance': 'Signature',
                                   'build': 'rva20:failed'})
    assert not page['controls']['active']
    selected, = [choice for choice in page['global_navigation'][0]['choices'] if choice['selected']]
    assert selected['label'] == 'cmake'
    for facet in page['controls']['facets']:
        assert sum(option['selected'] for option in facet['options']) == 1
        assert any(option['label'] == 'All' for option in facet['options'])


def test_exact_check_without_a_selector_keeps_a_removable_indicator(scoped_client):
    page = document(scoped_client, {'monitor': 'fixture_signature', 'check': 'error', 'buildsystem': 'cmake'})
    indicator, = page['controls']['active']
    assert indicator['label'] == 'Check: Failed'
    query = parameters(indicator)
    assert 'check' not in query and query['buildsystem'] == ['cmake']
    assert follow(scoped_client, indicator['href'])['total'] > page['total']
