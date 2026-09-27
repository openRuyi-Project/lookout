"""Reader roles and visible filter context, using only synthetic observations."""
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

from conftest import make_snapshot
from tracker import monitor_model, monitor_views, presentation, state, view
from tracker.api import create_app
from tracker.package_list import PackageList


NOW = datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def prepared(config, monkeypatch):
    monkeypatch.setattr(state, 'compare', lambda current, latest, *args:
                        'unknown' if not current or not latest else 'current' if current == latest else 'outdated')
    snapshot = make_snapshot(config, NOW.isoformat())
    snapshot['monitor_catalog'] = {'requires': {'title': 'Requires'}}
    snapshot['specs']['binutils'] = state.success({}, {
        'metadata': {'name': 'binutils', 'version': '3.9.0', 'summary': 'Fixture summary',
                     'description': 'Fixture long description', 'license': 'Fixture-License',
                     'url': 'https://upstream.example/fixture', 'buildsystem': 'fixture-build'},
        'head': 'fixture-source-commit',
        'source_origin': {'url': 'https://gitlab.example.org/team/packaging.git'},
        'changelog': [{'commit': 'a' * 40, 'author': 'Fixture author', 'date': NOW.isoformat(),
                       'subject': 'Fixture source change', 'signed_off_by': []}],
    }, NOW.isoformat())
    rows, collection = view.project_monitors(snapshot, NOW)
    return snapshot, rows, collection


@pytest.fixture
def client(prepared, monkeypatch, tmp_path):
    snapshot, rows, collection = prepared
    app = create_app(tmp_path / 'unused.db')
    index = PackageList(rows, snapshot['targets'])
    monkeypatch.setattr(app.state.projection, 'read', lambda: (snapshot, index, collection))
    return TestClient(app)


def parsed(href):
    return parse_qs(urlsplit(href).query)


def listing_payload(prepared):
    snapshot, rows, collection = prepared
    selected = PackageList(rows, snapshot['targets']).select(
        view='all', buildsystem='', maintenance='', builds={}, page=1, per_page=100)
    return {**selected, 'section': 'results', 'collection': collection, 'targets': snapshot['targets'],
            'monitors': [module.describe() for module in monitor_views.registry(snapshot)]}


def test_source_is_context_not_a_second_primary_version_view(client):
    response = client.get('/api/ui/packages')
    assert response.status_code == 200
    document = response.json()
    labels = [choice['label'] for choice in document['navigation']['choices']]
    assert 'Source' not in labels and 'Version' in labels and 'Build' in labels
    assert 'Source' not in [column['title'] for column in document['table']['columns']]
    first = next(row for row in document['table']['rows'] if row['key'] == 'binutils')
    assert any(value['text'] == 'fixture-build' for line in first['cells'][0]['lines'] for value in line)


def test_explicit_source_ui_link_is_coverage_without_a_fake_results_mode(client):
    response = client.get('/api/ui/packages?monitor=source&section=results&q=bin')
    assert response.status_code == 200
    document = response.json()
    assert [column['title'] for column in document['table']['columns']] == ['Package', 'Check', 'Last checked']
    assert document['table']['rows'][0]['cells'][1]['lines'][0][0]['text'] == 'Checked'
    assert document['table']['rows'][0]['cells'][2]['lines'][0][0]['datetime'] == NOW.isoformat()
    assert not any(choice['label'] == 'Results' for nav in document['controls']['navigation']
                   for choice in nav['choices'])
    hidden = {item['name']: item['value'] for item in document['controls']['hidden']}
    assert hidden['monitor'] == 'source' and hidden['section'] == 'coverage'
    choices = [choice for nav in document['controls']['navigation'] for choice in nav['choices']]
    assert [(choice['label'], choice['count']) for choice in choices] == [
        ('Uncovered', 0), ('Failed', 0)]


def test_source_v2_fields_and_detail_provenance_remain_available(client):
    listing = client.get('/api/v2/packages?monitor=source&section=results&q=bin').json()
    assert listing['section'] == 'results'  # Reader-role changes do not rewrite the public data API.
    assert any(module['id'] == 'source' for module in listing['monitors'])
    source = listing['items'][0]['monitors']['source']['data']
    assert (source['kind'], source['version'], source['buildsystem']) == ('source', '3.9.0', 'fixture-build')
    raw = client.get('/api/v2/packages/binutils').json()['monitors']['source']
    assert raw['data']['metadata']['license'] == 'Fixture-License'
    assert raw['data']['source_path'] == 'SPECS/binutils'
    assert raw['data']['changelog'][0]['subject'] == 'Fixture source change'

    document = client.get('/api/ui/packages/binutils').json()
    assert document['subtitle'] == 'Fixture summary'
    assert not any(section['id'] == 'source' for section in document['sections'])
    context = next(section for section in document['context'] if section['id'] == 'source')
    assert context['notes'] == ['Fixture long description']
    assert next(field for field in context['fields'] if field['label'] == 'License')['values'][0]['text'] == 'Fixture-License'
    assert any(link['href'] == raw['data']['source_url'] for link in document['links'])
    assert any(link['href'] == 'https://upstream.example/fixture' for link in document['links'])
    assert any(section['id'] == 'changelog' for section in document['sections'])
    checks = next(section for section in document['sections'] if section['id'] == 'checks')
    source_check = next(row for row in checks['table']['rows'] if row['key'] == 'source')
    assert source_check['cells'][1]['lines'][0][0]['text'] == 'Checked'


def test_context_role_follows_kind_instead_of_hard_coded_source_id(prepared):
    payload = listing_payload(prepared)
    for descriptor in payload['monitors']:
        if descriptor['kind'] == 'source':
            descriptor.update(id='package_context', title='Package provenance')
    for row in payload['items']:
        context = row['monitors'].pop('source')
        context.update(id='package_context', title='Package provenance')
        row['monitors']['package_context'] = context
    listing = presentation.listing(payload, {})
    assert 'Package provenance' not in [choice.label for choice in listing.navigation.choices]
    explicit = presentation.listing(payload, {'monitor': 'package_context', 'section': 'results'})
    assert [column.title for column in explicit.table.columns] == ['Package', 'Check', 'Last checked']
    package = next(row for row in payload['items'] if row['name'] == 'binutils')
    document = presentation.detail(package)
    assert any(section.id == 'package_context' for section in document.context)
    assert not any(section.id == 'package_context' for section in document.sections)
    checks = next(section for section in document.sections if section.id == 'checks')
    assert any(row.key == 'package_context' for row in checks.table.rows)


@pytest.mark.parametrize('focus,allowed', [
    ('build', {'build'}), ('requires', {'requires'}), ('version', {'view'}),
])
def test_inherited_selections_not_supported_by_the_page_are_removed(client, focus, allowed):
    query = (f'monitor={focus}&requires=unmet&view=updates&q=bin&per_page=1&page=2'
             '&maintenance=FixtureSignal&build=rva23:failed&build=rva20:succeeded')
    document = client.get('/api/ui/packages?' + query).json()
    supported = '&build=rva23:failed&build=rva20:succeeded' if focus == 'build' else (
        '&requires=unmet' if focus == 'requires' else '&view=updates')
    expected = client.get(f'/api/ui/packages?monitor={focus}&q=bin&per_page=1&page=2' + supported).json()
    assert document == expected
    controls = document['controls']
    assert not any(facet['name'] == 'maintenance' for facet in controls['facets'])
    build_rows = [row for row in controls['choice_rows'] if row['label'] in {'rva23', 'rva20', 'x86_64'}]
    assert bool(build_rows) == ('build' in allowed)
    hidden = {parameter['name'] for parameter in controls['hidden']}
    assert not (hidden & ({'view', 'requires'} - allowed))
    assert not any(choice['label'].startswith(('View:', 'Requires:', 'Maintenance:'))
                   for choice in controls['active'])


def test_empty_unselected_facets_are_not_all_only_controls(prepared):
    payload = listing_payload(prepared)
    payload.update(buildsystems={}, maintenance_labels={},
                   build_statuses={target['id']: [] for target in payload['targets']})
    controls = presentation.listing_controls(payload, {}, None, presentation.Links())
    assert controls.facets == [] and controls.active == []


def test_selected_zero_count_facets_remain_visible_and_removable(prepared):
    payload = listing_payload(prepared)
    payload.update(buildsystems={'fixture-system': 0}, maintenance_labels={'FixtureSignal': 0},
                   build_statuses={target['id']: [] for target in payload['targets']})
    target = payload['targets'][0]['id']
    payload['build_statuses'][target] = [{'value': 'failed', 'label': 'Failed', 'count': 0}]
    query = {'buildsystem': 'fixture-system', 'maintenance': 'FixtureSignal', 'build': [target + ':failed']}
    controls = presentation.listing_controls(payload, query, None, presentation.Links(query))
    assert {facet.name for facet in controls.facets} == {'maintenance', 'build'}
    sidebar, = presentation.global_navigation(payload, query, presentation.Links(query))
    assert sidebar.label == 'Build system'
    assert [(c.label, c.count) for c in sidebar.choices if c.selected] == [('fixture-system', 0)]
    assert not controls.active
    assert 'buildsystem' not in parsed(sidebar.choices[0].href)
    for facet in controls.facets:
        selected = [option for option in facet.options if option.selected]
        assert len(selected) == 1 and selected[0].count == 0
        all_option = next(option for option in facet.options if option.label == 'All')
        cleared = presentation.filter_link(presentation.Links(query), facet.name, all_option.value)
        assert facet.name not in parsed(cleared)


def evidence_result(scopes):
    facts = [monitor_model.evidence('Queried release', 'fixture-current', 'Fixture',
                                    'https://example.org/query', code='query')]
    findings = []
    for number, (scope, target) in enumerate(scopes):
        finding = monitor_model.finding('fixture-' + str(number), 'Signal', 'Fixture finding ' + str(number),
            deepcopy(facts), 'https://example.org/finding/' + str(number), scope=scope, target_version=target)
        findings.append({**finding, 'stale': False})
    return {'id': 'fixture_monitor', 'title': 'Fixture evidence', 'data': {'findings': findings}}


def test_shared_upgrade_target_is_attributed_once_at_section_level():
    result = evidence_result([('upgrade', '2.0'), ('upgrade', '2.0')])
    before = deepcopy(result)
    section, = presentation.evidence_section(result, presentation.Links())
    targets = [field for field in section.fields if field.label == 'Target']
    assert len(targets) == 1 and targets[0].values[0].text == '2.0'
    assert not any(field.label == 'Target' for entry in section.entries for field in entry.fields)
    assert len(section.entries) == 2 and result == before
    assert sum(field.label == 'Queried release' for field in section.fields) == 1


@pytest.mark.parametrize('scopes,expected', [
    ([('current', None), ('upgrade', '2.0')], [None, '2.0']),
    ([('upgrade', '2.0'), ('upgrade', '3.0')], ['2.0', '3.0']),
    ([('current', None), ('current', None)], [None, None]),
])
def test_nonshared_upgrade_targets_remain_conditioned_on_each_entry(scopes, expected):
    section, = presentation.evidence_section(evidence_result(scopes), presentation.Links())
    assert not any(field.label == 'Target' for field in section.fields)
    actual = []
    for entry in section.entries:
        target = next((field for field in entry.fields if field.label == 'Target'), None)
        actual.append(target.values[0].text if target else None)
    assert actual == expected


@pytest.mark.parametrize('stale,section_markers,entry_markers', [
    ([False, False], 0, [0, 0]),
    ([True, True], 1, [0, 0]),
    ([False, True], 0, [0, 1]),
])
def test_retained_evidence_is_marked_at_its_narrowest_shared_scope(stale, section_markers, entry_markers):
    result = evidence_result([('upgrade', '2.0'), ('upgrade', '2.0')])
    for finding, retained in zip(result['data']['findings'], stale):
        finding['stale'] = retained
    before = deepcopy(result)

    section, = presentation.evidence_section(result, presentation.Links())

    assert section.title.count('Out of date') == section_markers
    assert [sum(value.text == 'Out of date' for value in entry.heading)
            for entry in section.entries] == entry_markers
    assert not any(field.label == 'Evidence' for field in section.fields)
    assert 'Previous observation' not in section.model_dump_json()
    assert section.fields[0].label == 'Target' and section.fields[0].values[0].text == '2.0'
    assert [entry.heading[0].href for entry in section.entries] == [
        finding['evidence_url'] for finding in result['data']['findings']]
    for entry in section.entries:
        for marker in entry.heading[1:]:
            assert marker.href == '#checks' and marker.tone == 'notice'
    assert result == before


@pytest.mark.parametrize('monitor_id', ['security', 'fixture_monitor'])
@pytest.mark.parametrize('stale', [[False, False, False], [True, True, True], [False, True, True]])
def test_list_groups_retained_evidence_without_repeating_a_field_per_finding(monitor_id, stale):
    result = evidence_result([('current', None)] * len(stale))
    result['id'] = monitor_id
    for finding, retained in zip(result['data']['findings'], stale):
        finding['stale'] = retained
    before = deepcopy(result)

    rendered, = presentation.evidence_cells({'detail_url': '/packages/fixture'}, result, presentation.Links())
    values = [value for line in rendered.lines for value in line]
    markers = [value for value in values if value.text == 'Out of date']

    assert len(markers) == int(any(stale))
    for marker in markers:
        query = parse_qs(urlsplit(marker.href).query)
        assert query['monitor'] == [monitor_id]
        assert query['freshness'] == ['retained']
    assert 'Previous observation' not in rendered.model_dump_json()
    for finding in result['data']['findings']:
        matching = [value for value in values if value.text == finding['title']]
        assert len(matching) == 1 and matching[0].href == finding['evidence_url']
        assert matching[0].tone == ('notice' if finding['stale'] else 'normal')
    assert result == before


@pytest.mark.parametrize('relation', ['current', 'outdated'])
@pytest.mark.parametrize('stale', [False, True])
def test_upgrade_arrow_does_not_substitute_for_evidence_freshness(relation, stale):
    result = evidence_result([('upgrade', '2.0')])
    result['data']['findings'][0]['stale'] = stale
    package = {'detail_url': '/packages/fixture', 'monitors': {'version': {'data': {
        'kind': 'version', 'current': '1.0', 'latest': '2.0', 'track': 'fixture', 'relation': relation,
    }}}}

    rendered, = presentation.evidence_cells(package, result, presentation.Links())
    values = [value.text for line in rendered.lines for value in line]

    assert ('→' in values) == (relation == 'outdated')
    assert ('Out of date' in values) == stale
