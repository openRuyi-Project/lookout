"""Independent maintenance facts intersect; alternatives retain replacement semantics."""
from itertools import combinations, product
from urllib.parse import parse_qs, urlsplit

from tests.helpers.listing import facet_destination
from tracker.api import ListingQuery
from tracker.presentation.navigation import Links, listing_controls
from tracker.presentation.labels import appearance, palettes, priority
from tracker.readmodel.packages import PackageList


def rows():
    labels = ['Advisory', 'DepMismatch', 'Outdated', 'Untracked']
    return [{'name': f'pkg-{i}-{j}', 'monitors': {'fixture': {'dimensions': {
        'maintenance': list(group), 'buildsystem': ['cmake' if i % 2 else 'meson'],
        'version_signal': ['requires'] if i % 2 else [],
        'build:first': ['failed' if j % 2 else 'succeeded'],
    }}}} for i in range(5) for j, group in enumerate(combinations(labels, i))]


def test_all_combinations_match_a_scan_and_additive_counts():
    data = rows()
    index = PackageList(data, [{'id': 'first'}])
    before = repr(index.index)
    labels = ['Advisory', 'DepMismatch', 'Outdated', 'Untracked', 'absent']
    for length in range(4):
        for selected in combinations(labels, length):
            for system in ('', 'cmake'):
                filters = dict(view='all', maintenance=selected, buildsystem=system,
                               builds={}, page=1, per_page=100)
                result = index.select(**filters)
                expected = [r for r in data if set(selected) <= set(r['monitors']['fixture']['dimensions']['maintenance'])
                            and (not system or system in r['monitors']['fixture']['dimensions']['buildsystem'])]
                assert result['items'] == expected
                for label, count in result['maintenance_labels'].items():
                    assert count == sum(label in r['monitors']['fixture']['dimensions']['maintenance'] for r in expected)
                    combined = index.select(**{**filters, 'maintenance': [*selected, label]})
                    assert combined['total'] == count
    assert index.select(view='all', maintenance=['absent'], buildsystem='', builds={},
                        page=1, per_page=100)['maintenance_labels'] == dict.fromkeys(labels, 0)
    assert repr(index.index) == before


def test_single_value_facets_ignore_only_their_own_selection():
    targets = ['first', 'second', 'third']
    states = ['failed', 'succeeded']
    data = []
    for system, *builds in product(['cmake', 'meson'], *[states] * len(targets)):
        dimensions = {'buildsystem': [system], 'maintenance': ['Advisory'] if builds[0] == builds[1] else [],
                      **{f'build:{target}': [status] for target, status in zip(targets, builds)}}
        data.append({'name': f'pkg-{len(data)}', 'monitors': {'fixture': {'dimensions': dimensions}}})
    index = PackageList(data, [{'id': target} for target in targets])

    def select(filters):
        return index.select(view='all', buildsystem=filters.get('buildsystem', ''),
            maintenance=filters.get('maintenance', []), page=1, per_page=100,
            builds={target: filters[f'build:{target}'] for target in targets if f'build:{target}' in filters})

    for system, *builds in product(['', 'cmake'], *[['', *states]] * len(targets)):
        for labels in ([], ['Advisory']):
            filters = {'buildsystem': system, 'maintenance': labels,
                       **{f'build:{target}': status for target, status in zip(targets, builds)}}
            page = select(filters)
            for dimension in ['buildsystem', *[f'build:{target}' for target in targets]]:
                alternatives = ['cmake', 'meson'] if dimension == 'buildsystem' else states
                counts = (page['buildsystems'] if dimension == 'buildsystem' else
                          {s['value']: s['count'] for s in page['build_statuses'][dimension.removeprefix('build:')]})
                for alternative in alternatives:
                    expected = [row for row in data if all(
                        all(item in row['monitors']['fixture']['dimensions'][key] for item in
                            ([value] if isinstance(value, str) else value))
                        for key, value in (filters | {dimension: alternative}).items() if value)]
                    changed = select(filters | {dimension: alternative})
                    assert changed['items'] == expected
                    assert counts.get(alternative, 0) == len(expected)
                    changed_counts = (changed['buildsystems'] if dimension == 'buildsystem' else
                        {s['value']: s['count'] for s in changed['build_statuses'][dimension.removeprefix('build:')]})
                    assert {s: counts.get(s, 0) for s in alternatives} == {
                        s: changed_counts.get(s, 0) for s in alternatives}
    assert select({'build:first': 'failed'})['total'] > select({
        'build:first': 'failed', 'build:second': 'failed'})['total']


def test_repeated_query_survives_links_search_and_individual_removal():
    query = ListingQuery(maintenance=['Advisory', 'DepMismatch'], signal='requires',
                         build=['first:failed'], buildsystem='cmake', q='pkg').model_dump()
    links = Links(query)
    selected = parse_qs(urlsplit(links.toggle('maintenance', 'LicenseDiff', multiple=True)).query)
    assert selected['maintenance'] == ['Advisory', 'DepMismatch', 'LicenseDiff']
    assert parse_qs(urlsplit(links.toggle('maintenance', 'Advisory', multiple=True)).query)['maintenance'] == ['DepMismatch']
    payload = dict(section='results', counts={'updates': 0, 'untracked': 0},
        maintenance_labels={'Advisory': 1, 'DepMismatch': 1}, version_signals={'requires': 1},
        targets=[{'id': 'first', 'label': 'first'}],
        build_statuses={'first': [{'label': 'Failed', 'value': 'failed', 'count': 1}]},
        monitors=[{'id': 'requires', 'kind': 'requires', 'title': 'RuntimeDeps'}])
    controls = listing_controls(payload, query, None, links)
    assert [p.value for p in controls.hidden if p.name == 'maintenance'] == query['maintenance']
    chip = next(c for c in controls.active if c.label == 'Advisory')
    remaining = parse_qs(urlsplit(chip.href).query)
    assert remaining['maintenance'] == ['DepMismatch']
    assert remaining['signal'] == ['requires'] and remaining['build'] == ['first:failed']


def test_emphasis_has_one_palette_and_order_not_provider_severity():
    groups = [('Outdated', 'Advisory', 'DepMismatch', 'EOL', 'Yanked'),
              ('DepChanges', 'LicenseDiff'), ('Untracked', 'CheckFailed')]
    theme = palettes()
    for rank, labels in enumerate(groups):
        assert {priority(label) for label in labels} == {rank}
        assert {priority(appearance(label)) for label in labels} == {rank}
        assert len({theme[appearance(label)]['background'] for label in labels}) == 1
    assert len({theme[appearance(group[0])]['background'] for group in groups}) == 3


def test_api_and_document_preserve_multiple_issue_parameters(snapshot, tmp_path):
    from tests.helpers.documents import client_for
    from tracker import state
    from tracker.monitors import model
    snapshot['monitor_catalog'] = {'fixture': {'title': 'Fixture'}}
    snapshot['monitors'] = {}
    for name, labels in [('binutils', ['Advisory', 'DepMismatch', 'LicenseDiff']),
                         ('foo3', ['Advisory']), ('foo4', ['LicenseDiff'])]:
        snapshot['monitors'][name] = {'fixture': {
            'subject': model.subject(snapshot, name), 'status': 'ok', 'checked_at': state.utcnow(),
            'findings': [model.finding(label, label, label, [], 'https://example.org/') for label in labels],
        }}
    client, _ = client_for(snapshot, tmp_path)
    query = 'maintenance=Advisory&maintenance=DepMismatch'
    response = client.get('/api/v2/packages?' + query)
    assert response.status_code == 200
    assert [row['name'] for row in response.json()['items']] == ['binutils']
    document = client.get('/api/ui/packages?' + query).json()
    assert [row['key'] for row in document['table']['rows']] == ['binutils']
    nav = next(n for n in document['controls']['choice_rows'] if n['label'] == 'Alerts')
    assert {c['label'] for c in nav['choices'] if c['selected']} == {'Advisory', 'DepMismatch'}
    for choice in nav['choices']:
        facet_destination(client, document, choice)
    cleared = next(c['href'] for c in document['controls']['active'] if c['label'] == 'DepMismatch')
    result = client.get('/api/ui/packages?' + urlsplit(cleared).query).json()
    assert {r['key'] for r in result['table']['rows']} == {'binutils', 'foo3'}
    for path in ('/api/v2/packages', '/api/ui/packages'):
        assert client.get(path, params=[('maintenance', 'a' * 41)]).status_code == 422
        assert client.get(path, params=[('maintenance', 'a')] * 17).status_code == 422


def test_inline_shortcut_replaces_issues_while_menu_toggles():
    query = dict(maintenance=['Advisory', 'DepMismatch'], signal='requires', view='updates',
                 buildsystem='cmake', build=['first:failed'], q='pkg')
    links = Links(query)
    shortcut = parse_qs(urlsplit(links.only_filter(maintenance=['LicenseDiff'])).query)
    assert shortcut['maintenance'] == ['LicenseDiff']
    assert not {'view', 'signal', 'monitor'} & shortcut.keys()
    assert shortcut['q'] == ['pkg']
    assert not {'buildsystem', 'build'} & shortcut.keys()
    from tracker.presentation.navigation import filter_link
    assert 'build' not in parse_qs(urlsplit(filter_link(links, 'build', 'first:failed')).query)
    assert parse_qs(urlsplit(filter_link(links, 'build', 'first:succeeded')).query)['build'] == ['first:succeeded']


def test_zero_count_restarts_even_when_that_option_was_selected():
    links = Links(dict(q='pkg', per_page=20, buildsystem='cmake', build=['first:failed'],
                       maintenance=['Advisory', 'DepMismatch'], view='updates', signal='requires'))
    for value in ('Advisory', 'LicenseDiff'):
        restarted = parse_qs(urlsplit(links.choose('maintenance', value, 0, multiple=True)).query)
        assert restarted == {'q': ['pkg'], 'per_page': ['20'], 'page': ['1'], 'maintenance': [value]}
    added = parse_qs(urlsplit(links.choose('maintenance', 'LicenseDiff', 2, multiple=True)).query)
    assert added['maintenance'] == ['Advisory', 'DepMismatch', 'LicenseDiff']
    assert added['buildsystem'] == ['cmake'] and added['build'] == ['first:failed']
    removed = parse_qs(urlsplit(links.choose('maintenance', 'Advisory', 2, multiple=True)).query)
    assert removed['maintenance'] == ['DepMismatch']
    system = parse_qs(urlsplit(links.choose('buildsystem', 'meson', 2)).query)
    assert system['buildsystem'] == ['meson'] and system['maintenance'] == ['Advisory', 'DepMismatch']
