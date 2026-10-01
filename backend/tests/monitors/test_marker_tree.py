"""Shared traversal preserves distinct applicability and feature-gating semantics."""
from itertools import product
from types import SimpleNamespace

from packaging.markers import Marker
import pytest

from tracker.monitors.requires import markers, pypi


@pytest.mark.parametrize('values', list(product((False, None, True), repeat=3)))
def test_three_valued_reductions(values):
    assert markers.all_of(values) is (False if False in values else None if None in values else True)
    assert markers.any_of(values) is (True if True in values else None if None in values else False)


@pytest.mark.parametrize('expression,environment,applies,optional', [
    ('extra == "fast" and sys_platform == "linux"', {'sys_platform': 'linux'}, None, True),
    ('extra == "fast" or sys_platform == "linux"', {'sys_platform': 'linux'}, True, False),
    ('extra == "fast" and sys_platform == "linux"', {'sys_platform': 'win32'}, False, True),
    ('extra == "fast" or sys_platform == "linux"', {'sys_platform': 'win32'}, None, False),
])
def test_same_tree_has_different_analysis_semantics(expression, environment, applies, optional):
    assert markers.applies(expression, environment) is applies
    assert pypi._optional(Marker(expression)) is optional


@pytest.mark.parametrize('shape', ['empty', 'operator', 'arity', 'nodes', 'depth'])
def test_unknown_and_excessive_trees_fail_conservatively(shape):
    leaf = Marker('extra == "fast"')._markers[0]
    trees = {'empty': [], 'operator': [leaf, 'xor', leaf], 'arity': (1, 2),
             'nodes': [leaf, *['and', leaf] * 256]}
    deep = [leaf]
    for _ in range(33):
        deep = [deep]
    trees['depth'] = deep
    marker = SimpleNamespace(_markers=trees[shape])
    with pytest.raises(ValueError):
        markers.fold(marker, lambda _: True, markers.all_of, markers.any_of)
    assert pypi._optional(marker) is None
