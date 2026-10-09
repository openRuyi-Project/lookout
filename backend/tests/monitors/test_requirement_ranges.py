"""Dependency changes compare accepted versions, not redundant clause spelling."""
from datetime import datetime, timezone

import pytest

from tracker.monitors.requires import model as requirements


@pytest.mark.parametrize('before,after,equal', [
    ('>=3.8,>=3.7', '>=3.8', True),
    ('>=1,<4,<3', '>=1,<3', True),
    ('~=1.4.5', '>=1.4.5,==1.4.*', True),
    ('>=2,!=1', '>=2', True),
    ('>=2,<1', '>=3,<2', True),
    ('>=1,!=1.5', '>=1', False),
    ('>=1,<3', '>=1,<2', False),
    ('>=1,<2', '>=1,<3', False),
    ('==1.0', '==1.0+local', False),
    ('===1.0', '===1.0.0', False),
    ('>=1!1.0', '>=1.0', False),
    ('<=1.0', '<1.0.post0.dev0', False),
    ('>=1.0,>=0.9rc1', '>=1.0', False),
    ('>=1.0rc1,>=0.9', '>=1.0rc1', True),
    ('===legacy', '===legacy', True),
    ('', '', True),
    ('invalid', 'invalid', False),
])
def test_dependency_range_equivalence(before, after, equal):
    assert requirements.equivalent('pep440', before, after) is equal
    assert requirements.equivalent('pep440', after, before) is equal


@pytest.mark.parametrize('target,changed', [('>=3.8', False), ('>=3.9', True)])
def test_assessment_keeps_evidence_but_ignores_redundant_constraints(target, changed):
    current = {'expression': '>=3.8,>=3.7', 'source': 'PyPI', 'url': 'https://example.org/current'}
    future = {'expression': target, 'source': 'PyPI', 'url': 'https://example.org/target'}
    declaration = {'dependency': 'python', 'name': 'Python', 'kind': 'runtime',
                   'scheme': 'pep440', 'current': current, 'target': future}
    result = requirements.assess(declaration, {}, datetime.now(timezone.utc))
    assert result['changed'] is changed
    assert result['current'] == current and result['target'] == future
    assert result['satisfaction'] == 'unknown'
    assert result['target_satisfaction'] == 'unknown'


@pytest.mark.parametrize('scheme,before,after,equal', [
    ('numeric_minimum', '1.2', '1.2.0', True),
    ('semver', '>=1,>=0', '>=1', False),
    ('rpm_version', '>=1', '>=1', True),
])
def test_range_optimization_does_not_reinterpret_other_syntaxes(scheme, before, after, equal):
    assert requirements.equivalent(scheme, before, after) is equal
