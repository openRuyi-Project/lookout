from datetime import datetime, timezone

import pytest

from tracker.monitors.requires.markers import applies
from tracker.monitors.requires import model
from tests.monitors.test_requires import change, dependency


@pytest.mark.parametrize('expression,expected', [
    ('sys_platform == "linux"', True),
    ('sys_platform == "win32"', False),
    ('platform_machine == "x86_64"', None),
    ('sys_platform == "linux" or extra == "speedups"', True),
    ('sys_platform == "win32" and extra == "speedups"', False),
    ('sys_platform == "linux" and extra == "speedups"', None),
    ('extra == "docs"', None),
    ('invalid syntax', None),
])
def test_partial_markers_never_use_host_defaults(expression, expected, monkeypatch):
    monkeypatch.setattr('packaging.markers.default_environment',
                        lambda: pytest.fail('collector host must not determine applicability'))
    assert applies(expression, {'sys_platform': 'linux'}) is expected


def test_assessment_uses_fresh_distribution_python_and_explicit_platform(snapshot):
    dependency(snapshot, '3.14.0')
    snapshot['dependency_environments'] = {'pep508': {'sys_platform': 'linux'}}
    declaration = {**change(), 'condition': 'python_version >= "3.14" and sys_platform == "linux"'}
    now = datetime.now(timezone.utc)
    assert model.assess(declaration, snapshot, now)['satisfaction'] == 'satisfied'
    declaration['condition'] = 'python_version < "3.10"'
    result = model.assess(declaration, snapshot, now)
    assert (result['satisfaction'], result['reason']) == ('not_applicable', 'condition_false')
    model.RequirementAssessment.model_validate(result)
    snapshot['sources']['runtime-package']['error'] = 'unavailable'
    assert model.assess(declaration, snapshot, now)['satisfaction'] == 'unknown'


def test_unknown_target_environment_does_not_activate_optional_features(snapshot):
    dependency(snapshot)
    snapshot['dependency_environments'] = {'pep508': {'sys_platform': 'linux'}}
    result = model.assess({**change(), 'condition': 'extra == "speedups"'}, snapshot, datetime.now(timezone.utc))
    assert result['satisfaction'] == 'unknown'
    assert not model.unsatisfied(result)
