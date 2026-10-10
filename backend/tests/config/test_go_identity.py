"""One reviewed module rule supplies identity to all registry observations."""
import pytest

from tracker.monitors import license, yanked
from tracker.monitors.requires import monitor as requires
from tracker.monitors.security import monitor as security


@pytest.mark.parametrize('module,escaped', [
    ('example.org/widget/v3', 'example.org/widget/v3'),
    ('example.org/NewOwner/widget/v2', 'example.org/!new!owner/widget/v2'),
    ('example.org/widget/submodule', 'example.org/widget/submodule'),
])
def test_native_module_identity_is_shared_without_rpm_name_inference(module, escaped):
    package = {'name': 'unrelated-rpm-name', 'version': '3.2.1',
               'identity': {'source': 'go_proxy', 'url': f'https://proxy.golang.org/{escaped}/@latest'}}
    for adapter in (license, yanked, requires):
        assert adapter.inputs(package, None) == {'go': module}
    assert security.inputs(package, None) == {'ecosystem': 'Go', 'name': module}


def test_explicit_monitor_override_is_not_overwritten_by_catalog_identity():
    package = {'version': '3.2.1', 'identity': {
        'source': 'go_proxy', 'url': 'https://proxy.golang.org/example.org/widget/v3/@latest'}}
    for adapter in (license, yanked, requires):
        assert adapter.inputs(package, {'go': 'example.org/operator'}) == {'go': 'example.org/operator'}
    assert security.inputs(package, {'ecosystem': 'Go', 'name': 'example.org/operator'}) == {
        'ecosystem': 'Go', 'name': 'example.org/operator'}
