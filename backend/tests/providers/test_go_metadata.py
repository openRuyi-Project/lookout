import pytest

from tracker.monitors import license, yanked
from tracker.providers import go


class IO:
    def __init__(self, **changes):
        self.changes = changes

    def json(self, method, url, **kwargs):
        version = url.rsplit('/', 1)[-1]
        return {'versionKey': {'system': 'GO', 'name': 'example.org/module', 'version': version},
                'licenses': ['MIT'], 'isDeprecated': True, **self.changes}


def test_exact_module_version_and_license_provenance():
    settings = {'go': 'example.org/module'}
    row = go.metadata(settings, '1.2.3', IO())
    assert row.license_expression == 'MIT'
    assert row.license_field == 'licenses (licensecheck)'
    assert row.yanked is None  # Deprecated is not a Go retraction assertion.
    with pytest.raises(ValueError, match='identity'):
        go.metadata(settings, '1.2.3', IO(versionKey={'system': 'GO', 'name': 'other', 'version': 'v1.2.3'}))


def test_unknown_license_relationship_is_not_invented():
    result = license.check({'version': '1.2.3', 'target_version': '1.3.0'}, {'go': 'example.org/module'},
                           IO(licenses=['MIT', 'Apache-2.0']))
    assert result['status'] == 'unsupported'
    assert result['findings'] == []


def test_go_withdrawal_uses_its_separate_source():
    package = {'identity': {'source': 'go_proxy', 'url': 'https://proxy.golang.org/example.org/module/@latest'}}
    assert license.inputs(package, None) == {'go': 'example.org/module'}
    assert yanked.inputs(package, None) == {'go': 'example.org/module'}
