"""Aggregate reading contracts on synthetic facts, not live package counts."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from tracker.presentation.build import build_note
from tracker.presentation.labels import caption, palettes
from tracker.presentation.model import Cell, Column, Row, RowNote, Table
from tracker.presentation.navigation import Links
from tracker.presentation.requires import requires_preview
from tests.presentation.test_requires_presentation import changed, result


def test_runtime_preview_keeps_only_unsatisfied_subconditions():
    requirements = result(
        changed(name='ok'),
        changed(name='current', satisfaction='unsatisfied'),
        changed(name='target', target={'expression': '>=3', 'url': 'https://example.org/v2'},
                changed=True, target_satisfaction='unsatisfied'),
        changed(name='conditional', condition='platform == "fixture"', satisfaction='unknown'),
        changed(name='missing', mapping='not_packaged', satisfaction='unknown'),
    )
    before = deepcopy(requirements)
    lines = requires_preview({'detail_url': '/packages/fixture'}, requirements, Links())
    assert [[v.text for v in line[:2]] for line in lines] == [
        ['RuntimeDeps:', 'current'], ['RuntimeDeps:', 'target']]
    assert '→' in [v.text for v in lines[1]]
    assert '✓' in [v.text for v in lines[1]] and '✗' in [v.text for v in lines[1]]
    assert requirements == before


def test_runtime_preview_keeps_every_matching_dependency_and_optional_scope():
    requirements = result(*(changed(name=f'lib{i}', dependency=f'lib{i}', satisfaction='unsatisfied',
                                    optional=i == 0) for i in range(5)))
    lines = requires_preview({'detail_url': '/packages/fixture'}, requirements, Links())
    assert len(lines) == 5 and lines[0][0].text == 'RuntimeDeps:'
    assert lines[0][1].text == 'lib1'  # Required declarations precede optional ones.
    optional = requires_preview({}, result(requirements['data']['requirements'][0]), Links())
    assert optional[0][0].text == 'RuntimeDeps:' and optional[0][-1].text == 'Optional'
    assert {line[1].text for line in lines} == {f'lib{i}' for i in range(5)}
    assert lines[-1][-1].text == 'Optional'


def test_version_dependency_filter_explains_satisfied_changes():
    requirements = result(changed(name='changed', changed=True,
        target={'expression': '>=2', 'url': 'https://example.org/v2'}, target_satisfaction='satisfied'))
    package = {'detail_url': '/packages/fixture'}
    assert requires_preview(package, requirements, Links()) == []
    lines = requires_preview(package, requirements, Links({'signal': 'requires'}))
    assert [value.text for value in lines[0]] == ['RuntimeDeps:', 'changed', '>=1', '✓', '→', '>=2', '✓']


def test_build_reasons_group_identical_targets_without_omitting_details():
    def target(label, reason):
        return dict(label=label, raw_status='blocked', text='Blocked', issue=True, details=reason)
    prefix = 'waiting for ' + 'fixture-dependency, ' * 20
    data = dict(data={'targets': [target('one', prefix + 'A'), target('two', prefix + 'A'),
                                 target('three', prefix + 'B')]})
    note, other = build_note({'detail_url': '/packages/fixture'}, data, 2)
    assert (note.column, note.span) == (2, 3)
    assert note.values[0].text == 'one, two:'
    assert note.values[1].text == prefix + 'A'
    assert other.values[0].text == 'three:'
    assert other.values[1].text == prefix + 'B'
    assert note.values[1].href == '/packages/fixture#build'


def test_row_note_cannot_escape_the_table():
    with pytest.raises(ValidationError):
        Table(label='fixture', columns=[Column(title='A')], rows=[
            Row(key='fixture', cells=[Cell()], notes=[RowNote(column=1, span=1, values=[])])])


def test_display_names_do_not_rewrite_observation_or_query_identifiers():
    assert caption('RuntimeDeps') == 'RuntimeDeps'
    assert caption('DepMismatch') == 'DepMismatch'
    assert caption('Out of date') == 'Stale'
    assert caption('KEV') == 'KEV'
    assert 'label:DepMismatch' in palettes()
    assert Links().to(maintenance='DepMismatch').endswith('maintenance=DepMismatch')
    assert caption('ThirdParty') == 'ThirdParty'


def test_label_palette_text_has_readable_contrast():
    def luminance(color):
        rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
        return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))
    for name, palette in palettes().items():
        high, low = sorted(map(luminance, palette.values()), reverse=True)
        assert (high + .05) / (low + .05) >= 4.5, name
