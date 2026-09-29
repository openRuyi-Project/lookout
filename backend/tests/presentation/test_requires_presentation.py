"""Runtime summaries remain concise without discarding conditional evidence."""
from copy import deepcopy

import pytest

from tracker.presentation import navigation as presentation_navigation, requires as presentation_requires




def assessment(**changes):
    return dict(dependency='fixture', name='Fixture dependency', kind='runtime', scheme='pep440',
                identity={'ecosystem': 'Fixture', 'name': 'fixture'}, extras=[], optional=False,
                condition=None, package='fixture-package', mapping='mapped',
                current=dict(expression='>=1', source='Fixture', url='https://example.org/current'),
                target=None, observed={'version': '2'}, satisfaction='satisfied', reason=None,
                target_satisfaction='unknown', target_reason='requirement_not_observed', changed=False,
                **changes)


def changed(**changes):
    result = assessment()
    result.update(changes)
    return result


def result(*requirements):
    return dict(id='requires', data={'requirements': list(requirements)})


def texts(cell):
    return [[value.text for value in line] for line in cell.lines]


def test_list_groups_runtime_and_optional_without_marker_programs():
    marker = '(platform == "one" and platform != "two") and feature == "speedups"'
    items = result(changed(condition='platform == "one"', satisfaction='unknown', reason='condition_not_evaluated'),
                   changed(name='Optional library', optional=True, condition=marker,
                           satisfaction='unknown', reason='condition_not_evaluated'))
    before = deepcopy(items)
    cells = presentation_requires.requires_cells({}, items, presentation_navigation.Links())
    lines = texts(cells[0])
    assert lines[0] == ['RuntimeDeps'] and lines[2] == ['Optional RuntimeDeps']
    assert 'Fixture dependency' in lines[1] and 'Optional library' in lines[3]
    assert not any('platform' in value or 'speedups' in value for line in lines for value in line)
    assert items == before

    sections = presentation_requires.requires_sections(items, presentation_navigation.Links())
    assert [(s.id, s.collapsible) for s in sections] == [
        ('requires', False), ('requires-optional', False), ('requires-conditions', True)]
    clauses = [value.text for entry in sections[-1].entries for field in entry.fields for value in field.values]
    assert clauses == ['platform == "one"', marker]


def test_condition_variants_share_a_summary_but_keep_each_clause():
    items = result(*(changed(optional=True, condition=condition, satisfaction='unknown',
                             reason='condition_not_evaluated')
                     for condition in ('feature == "one"', 'feature == "two"', 'feature == "one"')))
    cells = presentation_requires.requires_cells({}, items, presentation_navigation.Links())
    assert len(cells[0].lines) == 2  # Optional heading plus one assessment.
    sections = presentation_requires.requires_sections(items, presentation_navigation.Links())
    assert len(sections[0].table.rows) == 1
    assert [value.text for value in sections[1].entries[0].fields[0].values] == [
        'feature == "one"', 'feature == "two"']
    assert len(items['data']['requirements']) == 3


@pytest.mark.parametrize('different', [
    {'identity': {'ecosystem': 'Other', 'name': 'fixture'}}, {'extras': ['tls']},
    {'current': {'expression': '>=2', 'source': 'Fixture', 'url': 'https://example.org/current'}},
    {'current': {'expression': '>=1', 'source': 'Fixture', 'url': 'https://example.org/other'}},
    {'observed': {'version': '3'}}, {'reason': 'dependency_unavailable'},
    {'mapping': 'ambiguous'}, {'optional': None}, {'kind': 'build'},
])
def test_semantically_different_assessments_are_not_merged(different):
    first = changed(optional=True, condition='feature == "one"')
    second = {**first, 'condition': 'feature == "two"', **different}
    assert len(presentation_requires.requirement_groups([first, second])) == 2


def test_unconditional_and_unknown_legacy_conditions_are_not_reclassified():
    first = changed(optional=None, condition=None, satisfaction='unknown', reason='requirement_unavailable')
    second = {**first, 'condition': 'opaque-feature-expression'}
    assert len(presentation_requires.requirement_groups([first, second])) == 2
    sections = presentation_requires.requires_sections(result(first, second), presentation_navigation.Links())
    assert not any(s.id == 'requires-optional' for s in sections)
    assert sections[-1].entries[0].fields[-1].values[0].text == 'Not observed'


@pytest.mark.parametrize('mapping,label', [('not_mapped', 'Unmapped'),
                                         ('not_packaged', 'NotPackaged'),
                                         ('ambiguous', 'Ambiguous')])
def test_missing_package_evidence_is_explicit_not_an_unmet_mark(mapping, label):
    item = changed(package=None, observed=None, mapping=mapping, satisfaction='unknown',
                   reason='condition_not_evaluated', condition='platform == "one"')
    values = [value.text for value in presentation_requires.requirement_values(item)]
    assert label in values and '?' not in values and '✗' not in values


def test_dependency_extras_remain_visible_without_long_conditions():
    values = presentation_requires.requirement_values(changed(extras=['tls'], optional=False))
    assert values[0].text == 'Fixture dependency[tls]'


def test_explicit_but_absent_package_does_not_link_to_a_missing_detail():
    item = changed(package='missing-package', mapping='not_packaged', satisfaction='unknown', observed=None)
    values = presentation_requires.requirement_values(item)
    assert values[0].href is None
    assert values[-1].text == 'NotPackaged'


def test_unspecified_version_is_omitted_only_from_unchanged_list_summary():
    item = changed(current=dict(expression='', source='Fixture', url='https://example.org/current'))
    assert 'any version' not in [value.text for value in presentation_requires.requirement_values(item, compact=True)]
    assert 'any version' in [value.text for value in presentation_requires.requirement_values(item)]


def test_mixed_dependency_preview_orders_runtime_before_build_and_keeps_detail_evidence():
    target = dict(expression='>=3', source='Registry', url='https://example.org/target')
    items = result(changed(kind='build', name='Compiler', target=target, changed=True,
                           target_satisfaction='unsatisfied'),
                   changed(name='Library', target=target, changed=True, target_satisfaction='unsatisfied'))
    links = presentation_navigation.Links()
    lines = presentation_requires.requires_preview({'detail_url': '/packages/fixture'}, items, links)
    assert [[v.text for v in line] for line in lines] == [
        ['RuntimeDeps:', 'Library', '>=1', '✓', '→', '>=3', '✗'],
        ['BuildDeps:', 'Compiler', '>=1', '✓', '→', '>=3', '✗']]
    sections = presentation_requires.requires_sections(items, links)
    assert [(s.id, s.title) for s in sections] == [('requires', 'RuntimeDeps'), ('requires-build', 'BuildDeps')]
    for section in sections:
        assert not section.collapsible
        values = section.table.rows[0].cells[1].lines[0]
        assert values[0].href == 'https://example.org/current'
        assert values[3].href == 'https://example.org/target'
    build_only = presentation_requires.requires_sections(result(items['data']['requirements'][0]), links)
    assert build_only[0].id == 'requires' and build_only[0].title == 'BuildDeps'
