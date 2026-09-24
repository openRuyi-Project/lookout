from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from tracker import monitor_model, presentation, state, view
from tracker.api import create_app
from tracker.presentation_model import Cell, Column, Row, Table


def add_evidence(snapshot, name='binutils', mid='external_signature', *, status='ok'):
    snapshot.setdefault('monitor_catalog', {})[mid] = {'title': 'Artifact signatures'}
    snapshot.setdefault('monitors', {}).setdefault(name, {})[mid] = {
        'status': status, 'checked_at': state.utcnow(), 'subject': monitor_model.subject(snapshot, name),
        'findings': [monitor_model.finding('signature-1', 'Signature', 'Signing key changed', [
            monitor_model.evidence('Issuer', 'Example CA', 'Registry', 'https://example.org/signature')
        ], 'https://example.org/signature')],
    }


def client_for(snapshot, tmp_path):
    db = tmp_path / 'state.db'
    state.commit(db, snapshot)
    return TestClient(create_app(db)), db


def test_new_monitor_uses_existing_document_primitives(snapshot, tmp_path):
    add_evidence(snapshot)
    client, db = client_for(snapshot, tmp_path)
    original = db.read_bytes()
    listing = client.get('/api/ui/packages?monitor=external_signature')
    assert listing.status_code == 200
    document = listing.json()
    assert document['title'] == 'Artifact signatures'
    assert document['total'] == 1
    assert [c['title'] for c in document['table']['columns']] == ['Package', 'Artifact signatures']
    assert document['table']['rows'][0]['cells'][1]['lines'][0][0]['text'] == 'Signing key changed'
    detail = client.get('/api/ui/packages/binutils').json()
    section = next(s for s in detail['sections'] if s['id'] == 'external_signature')
    entry = section['entries'][0]
    assert entry['heading'][0]['href'] == 'https://example.org/signature'
    assert entry['fields'][0]['values'][0]['text'] == 'Example CA'
    assert entry['fields'][0]['values'][1]['text'] == 'Registry'
    assert db.read_bytes() == original


def test_display_counts_and_links_use_same_intersection_as_data_api(snapshot, tmp_path):
    add_evidence(snapshot, 'foo3')
    client, _ = client_for(snapshot, tmp_path)
    query = 'monitor=external_signature&build=rva20:issues&section=results'
    facts = client.get('/api/v2/packages?' + query).json()
    page = client.get('/api/ui/packages?' + query).json()
    assert page['total'] == facts['total'] == 1
    assert [r['key'] for r in page['table']['rows']] == [p['name'] for p in facts['items']]
    controls = page['controls']
    build = next(f for f in controls['facets'] if f['id'] == 'build-rva20')
    assert {o['value'].split(':')[1]: o['count'] for o in build['options'][1:]} == {
        o['value']: o['count'] for o in facts['build_statuses']['rva20']}
    for choice in page['navigation']['choices']:
        assert parse_qs(urlsplit(choice['href']).query)['build'] == ['rva20:issues']
    removal = controls['active'][0]
    assert 'build' not in parse_qs(urlsplit(removal['href']).query)


def test_no_evidence_and_failed_check_are_not_reported_as_success(snapshot, tmp_path):
    snapshot['monitor_catalog'] = {'new': {'title': 'New check'}}
    client, _ = client_for(snapshot, tmp_path)
    results = client.get('/api/ui/packages?monitor=new').json()
    coverage = client.get('/api/ui/packages?monitor=new&section=coverage').json()
    assert results['total'] == 0
    assert coverage['total'] == 5
    assert all(row['cells'][1]['lines'][0][0]['text'] == 'Not yet checked' for row in coverage['table']['rows'])
    assert [c['title'] for c in coverage['table']['columns']] == ['Package', 'Check', 'Last checked']


def test_build_semantics_are_projected_not_reinterpreted_in_website(snapshot):
    source = snapshot['sources']['binutils']
    builds = snapshot['builds']['binutils']
    for index, target in enumerate(builds.values()):
        target['last_success'] = {'version': source['version'], 'srcmd5': source['srcmd5'], 'time': state.utcnow()}
    pkg = view.project_monitors(snapshot)[0][0]
    result = pkg['monitors']['build']
    for build in result['data']['targets']:
        assert len(presentation.build_cell(build, source['version'], pkg['detail_url']).lines) == 1
    target = result['data']['targets'][1]
    target.update(raw_status='failed', kind='error', text='Failed', matches_source=False)
    target['last_success']['version'] = 'previous.rva20'
    rendered = presentation.build_cell(target, source['version'], pkg['detail_url'])
    assert rendered.lines[0][0].text == 'Failed'
    assert rendered.lines[0][0].tone == 'negative'
    assert rendered.lines[1][0].text == 'previous.rva20'
    assert rendered.lines[1][0].href.endswith('#build')


def test_partial_enrichment_and_alias_facts_remain_traceable(snapshot):
    add_evidence(snapshot)
    facts = snapshot['monitors']['binutils']['external_signature']['findings'][0]['facts']
    facts.extend([
        monitor_model.evidence('Changed wording', 0.004, 'FIRST', 'https://example.org/epss', code='epss_probability'),
        monitor_model.evidence('Model date', '2026-09-19', 'FIRST', 'https://example.org/epss'),
        monitor_model.evidence('KEV', None, 'CISA', 'https://example.org/kev', status='unavailable'),
        monitor_model.evidence('False value', False, 'Registry', 'https://example.org/')])
    pkg = view.project_monitors(snapshot)[0][0]
    before = deepcopy(pkg)
    result = presentation.detail(pkg)
    entry = next(s for s in result.sections if s.id == 'external_signature').entries[0]
    assert next(f for f in entry.fields if f.label == 'Changed wording').values[0].text == '0.4%'
    assert any(f.label == 'Model date' and f.values[0].text == '2026-09-19' for f in entry.fields)
    assert any(f.label == 'KEV' and f.values[0].text == 'unavailable' for f in entry.fields)
    assert any(f.label == 'False value' and f.values[0].text == 'No' for f in entry.fields)
    assert pkg == before


def test_navigation_escapes_values_and_preserves_repeated_filters():
    links = presentation.Links({'q': 'a&b', 'build': ['a:failed', 'b:blocked'], 'monitor': 'anything', 'page': 9})
    parsed = parse_qs(urlsplit(links.to(maintenance='A & B')).query)
    assert parsed['q'] == ['a&b']
    assert parsed['maintenance'] == ['A & B']
    assert parsed['build'] == ['a:failed', 'b:blocked']
    assert parsed['page'] == ['1']
    assert parse_qs(urlsplit(links.without('build', 'a:failed')).query)['build'] == ['b:blocked']


def test_document_contract_rejects_ragged_tables_and_unknown_markup():
    with pytest.raises(ValidationError):
        Table(label='Bad', columns=[Column(title='A')], rows=[Row(key='a', cells=[])])
    with pytest.raises(ValidationError):
        Cell.model_validate({'lines': [], 'html': '<script>alert(1)</script>'})


@pytest.mark.parametrize('query', ['monitor=not-registered', 'check=ok', 'page=-2', 'build=absent:failed'])
def test_display_and_data_routes_share_validation(snapshot, tmp_path, query):
    client, _ = client_for(snapshot, tmp_path)
    assert client.get('/api/ui/packages?' + query).status_code == 422
    assert client.get('/api/v2/packages?' + query).status_code == 422


def test_empty_build_choices_from_browser_are_noop(snapshot, tmp_path):
    client, _ = client_for(snapshot, tmp_path)
    page = client.get('/api/ui/packages?build=rva23:&build=rva20:issues').json()
    assert page['total'] == 1


def test_projection_retains_domain_api_and_theme_contract(snapshot, tmp_path):
    snapshot['presentation'] = {'buildsystems': {'unfamiliar': {'background': '#123456', 'foreground': '#ffffff'}}}
    client, _ = client_for(snapshot, tmp_path)
    old = client.get('/api/v2/packages/binutils').json()
    assert old['monitors']['build']['data']['kind'] == 'build'
    assert client.get('/api/ui/theme').json()['appearances']['buildsystem:unfamiliar']['background'] == '#123456'
    assert client.get('/api/ui/packages/absent').status_code == 404


def test_version_display_uses_decision_not_independent_comparison(snapshot):
    pkg = view.project_monitors(snapshot)[0][0]
    version = pkg['monitors']['version']['data']
    build = pkg['monitors']['build']['data']
    version.update(current='source.version', latest='target.version', relation='outdated', track='reviewed')
    build['source_success'] = False
    values = presentation.version_value(pkg)
    assert [value.text for value in values] == ['source.version', '→', 'target.version']
    assert values[0].tone == 'negative' and values[2].tone == 'positive'
    for relation in ('ahead', 'current', 'unknown', 'not_applicable'):
        version['relation'] = relation
        assert [value.text for value in presentation.version_value(pkg)] == ['source.version']
    version.update(track=None, relation='untracked')
    assert presentation.version_value(pkg)[0].tone == 'muted'
    assert presentation.version_value(pkg)[0].title == 'Upstream is not tracked'


@pytest.mark.parametrize('matches,version', [(False, '3.9.0'), (None, '3.9.0'), (True, '3.9.0.arch')])
def test_last_success_is_omitted_only_when_fully_inferable(snapshot, matches, version):
    build = view.project_monitors(snapshot)[0][0]['monitors']['build']['data']['targets'][0]
    build.update(matches_source=matches, last_success={'version': version, 'time': state.utcnow(), 'srcmd5': 'previous'})
    assert presentation.build_cell(build, '3.9.0', '/packages/binutils').lines[1][0].text == version
    build['last_success'] = None
    assert len(presentation.build_cell(build, '3.9.0', '/packages/binutils').lines) == 1


def test_query_fact_does_not_turn_an_unrelated_monitor_into_security(snapshot):
    add_evidence(snapshot)
    finding = snapshot['monitors']['binutils']['external_signature']['findings'][0]
    finding['facts'].append(monitor_model.evidence('Lookup', 'artifact', 'Registry', 'https://example.org/', code='query'))
    pkg = view.project_monitors(snapshot)[0][0]
    section = next(s for s in presentation.detail(pkg).sections if s.id == 'external_signature')
    assert section.notes == []
    assert section.fields[0].values[0].text == 'artifact'


def test_upgrade_context_does_not_require_a_named_frontend_monitor(snapshot):
    add_evidence(snapshot)
    pkg = view.project_monitors(snapshot)[0][0]
    result = pkg['monitors']['external_signature']
    result['data']['findings'][0].update(scope='upgrade', target_version='3.10.0')
    rendered = presentation.evidence_cells(pkg, result, presentation.Links())[0]
    assert rendered.lines[0][0].text == pkg['monitors']['version']['data']['current']
    section = presentation.evidence_section(result, presentation.Links())[0]
    assert section.entries[0].fields[0].values[0].text == '3.10.0'


def test_watch_failure_does_not_replace_primary_version(snapshot):
    pkg = view.project_monitors(snapshot)[0][0]
    result = pkg['monitors']['version']
    result['data']['watch'] = [{'id': 'other@preview', 'version': '9.0rc1', 'error': 'Timeout', 'stale': True}]
    primary = presentation.version_value(pkg)
    section = presentation.version_sections(result, presentation.Links())[0]
    assert section.entries[0].heading[0].href == '/api/v1/tracks/other%40preview'
    assert section.entries[0].fields[0].label == 'Last observed'
    assert section.entries[0].fields[1].values[0].text == 'Timeout'
    assert all(value.text != '9.0rc1' for value in primary)


def test_check_notes_and_attempts_are_retained(snapshot):
    add_evidence(snapshot)
    pkg = view.project_monitors(snapshot)[0][0]
    pkg['monitors']['external_signature']['check'].update(
        note='Identity is missing', status='input_unavailable',
        checked_at='2026-09-18T01:00:00Z', attempted_at='2026-09-19T01:00:00Z')
    checks = next(s for s in presentation.detail(pkg).sections if s.id == 'checks')
    row = next(r for r in checks.table.rows if r.key == 'external_signature')
    assert row.cells[1].lines[0][0].text == 'Input unavailable'
    assert row.cells[1].lines[1][0].text == 'Identity is missing'
    assert row.cells[2].lines[0][0].datetime == '2026-09-18T01:00:00Z'
    assert row.cells[2].lines[1][1].datetime == '2026-09-19T01:00:00Z'


def test_ordinary_version_does_not_repeat_same_name_track(snapshot):
    pkg = view.project_monitors(snapshot)[0][0]
    result = pkg['monitors']['version']
    result['data'].update(track=pkg['name'], relation='current', watch=[])
    assert presentation.version_sections(result, presentation.Links()) == []
    result['data']['relation'] = 'unknown'
    section = presentation.version_sections(result, presentation.Links())[0]
    assert [f.label for f in section.fields] == ['Comparison']
    assert section.fields[0].values[0].text == 'Cannot compare current observations'


def test_evidence_is_not_duplicated_as_summary_and_keeps_all_sources(snapshot):
    add_evidence(snapshot)
    finding = snapshot['monitors']['binutils']['external_signature']['findings'][0]
    finding['facts'] = [monitor_model.evidence(
        'Provider fixed events', ['2.4', '3.1'], 'Registry', url, code='fixed_events')
        for url in ['https://example.org/one', 'https://example.org/two', 'https://example.org/one']]
    pkg = view.project_monitors(snapshot)[0][0]
    section = next(s for s in presentation.detail(pkg).sections if s.id == 'external_signature')
    entry = section.entries[0]
    assert len(entry.fields) == 1
    assert entry.fields[0].values[0].text == '2.4, 3.1'
    assert [v.href for v in entry.fields[0].values[1:]] == ['https://example.org/one', 'https://example.org/two']
    assert set(entry.model_dump()) == {'heading', 'fields'}


def test_shared_query_context_appears_once_and_distinct_inputs_are_not_hidden(snapshot):
    add_evidence(snapshot)
    findings = snapshot['monitors']['binutils']['external_signature']['findings']
    common = monitor_model.evidence('Query package', 'reviewed identity', 'Registry', 'https://example.org/query', code='query')
    findings[0]['facts'].append(common)
    findings.append({**deepcopy(findings[0]), 'id': 'signature-2', 'title': 'Another key'})
    pkg = view.project_monitors(snapshot)[0][0]
    section = next(s for s in presentation.detail(pkg).sections if s.id == 'external_signature')
    assert [f.label for f in section.fields] == ['Query package']
    assert not any(f.label == 'Query package' for e in section.entries for f in e.fields)
    findings[1]['facts'][-1]['value'] = 'another identity'
    pkg = view.project_monitors(snapshot)[0][0]
    section = next(s for s in presentation.detail(pkg).sections if s.id == 'external_signature')
    assert section.fields == []
    assert sum(f.label == 'Query package' for e in section.entries for f in e.fields) == 2


def test_package_context_and_changelog_remain_visible_without_empty_sections(snapshot):
    pkg = view.project_monitors(snapshot)[0][0]
    source = pkg['monitors']['source']['data']
    source['metadata'] = {'license': 'MIT', 'summary': 'Short summary', 'description': 'Useful description'}
    source['changelog'] = [{'subject': 'Fix build', 'commit': 'a' * 40, 'author': 'Packager',
                           'date': '2026-09-19T01:00:00Z', 'signed_off_by': ['Packager']}]
    document = presentation.detail(pkg)
    assert document.context[0].notes == ['Useful description']
    assert document.context[0].fields[0].values[0].text == 'MIT'
    assert document.sections[-1].title == 'Changelog'
    assert document.sections[-1].entries[0].fields[-1].values[0].text == 'Packager'
    assert all('folded' not in section.model_dump() for section in document.sections + document.context)


def test_compact_labels_do_not_hide_different_cve_subjects(snapshot):
    add_evidence(snapshot)
    pkg = view.project_monitors(snapshot)[0][0]
    result = pkg['monitors']['external_signature']
    finding = result['data']['findings'][0]
    finding.update(title='CVE-2026-1000', stale=True, facts=[
        monitor_model.evidence('KEV · CVE-2026-1000', False, 'CISA', 'https://example.org/kev'),
        monitor_model.evidence('KEV · CVE-2026-1001', None, 'CISA', 'https://example.org/kev', status='unavailable')])
    section = presentation.evidence_section(result, presentation.Links())[0]
    assert [f.label for f in section.entries[0].fields] == ['KEV', 'KEV · CVE-2026-1001']
    assert [v.text for v in section.entries[0].heading] == ['CVE-2026-1000']
    assert section.fields[-1].values[0].text == 'Previous observation'


def test_meaningful_release_line_is_not_lost_with_redundant_track(snapshot):
    pkg = view.project_monitors(snapshot)[0][0]
    result = pkg['monitors']['version']
    result['data'].update(relation='current', watch=[], track_label='3.x')
    section = presentation.version_sections(result, presentation.Links())[0]
    assert [(f.label, f.values[0].text) for f in section.fields] == [('Release line', '3.x')]


def test_equal_formatted_values_do_not_merge_distinct_provider_assertions():
    facts = [monitor_model.evidence('Probability', value, 'FIRST', 'https://example.org/' + str(i), code='epss_probability')
             for i, value in enumerate([0.001231, 0.001232])]
    fields = presentation.fact_fields(facts)
    assert len(fields) == 2
    assert fields[0].values[0].text == fields[1].values[0].text
    assert fields[0].values[1].href != fields[1].values[1].href


def test_page_size_survives_navigation_and_filter_forms(snapshot, tmp_path):
    client, _ = client_for(snapshot, tmp_path)
    document = client.get('/api/ui/packages?per_page=2').json()
    assert len(document['table']['rows']) == 2
    assert {'name': 'per_page', 'value': '2'} in document['controls']['hidden']
    links = presentation.Links({'per_page': 2, 'page': 1, 'q': 'foo'})
    for link in (links.to(page=2), links.to(monitor='build'), links.without('q', 'foo')):
        assert parse_qs(urlsplit(link).query)['per_page'] == ['2']
    assert client.get(links.to(page=2).replace('/?', '/api/ui/packages?')).status_code == 200


def test_monitor_data_contract_is_discriminated_by_kind(snapshot):
    from tracker.api import MonitorObservation, MonitorSummary
    row = view.project_monitors(snapshot)[0][0]
    source = row['monitors']['source']
    for model in (MonitorObservation, MonitorSummary):
        parsed = model.model_validate(source)
        assert parsed.data.kind == 'source'
        broken = deepcopy(source)
        broken['data']['kind'] = 'version'
        with pytest.raises(ValidationError):
            model.model_validate(broken)
        schema = model.model_json_schema()['properties']['data']
        assert schema['discriminator']['propertyName'] == 'kind'
        assert set(schema['discriminator']['mapping']) == {'source', 'version', 'build', 'evidence'}
