import pytest

from tracker.monitors.requires import monitor
from tracker.monitors.requires.model import RequirementDeclaration, project


class IO:
    def __init__(self, document, canonical='v1.2.3'):
        self.document, self.canonical, self.urls = document, canonical, []

    def json(self, method, url, **kwargs):
        self.urls.append(url)
        return {'Version': self.canonical}

    def text(self, url, **kwargs):
        self.urls.append(url)
        return self.document


def test_exact_release_minimum_is_build_only_and_not_toolchain_suggestion():
    io = IO('module example.org/Widget\ngo 1.23.0\ntoolchain go1.24.0\n')
    result = monitor.check({'version': '1.2.3'}, {'go': 'example.org/Widget'}, io)
    assert result['status'] == 'ok'
    fact = result['findings'][0]['requirement']
    RequirementDeclaration.model_validate(fact)
    assert (fact['kind'], fact['dependency'], fact['constraint']['expression']) == ('build', 'go', '1.23.0')
    assert io.urls[-1] == 'https://proxy.golang.org/example.org/!widget/@v/v1.2.3.mod'
    assert not any('@latest' in url for url in io.urls)
    assert project(result['findings'], {}, None) == []


@pytest.mark.parametrize('document,status', [
    ('module example.org/m', 'unsupported'),
    ('module example.org/m\ngo 1.24rc1', 'unsupported'),
    ('module example.org/m\ngo 1', 'unsupported'),
    ('module example.org/m\ngo 1.023', 'unsupported'),
    ('module example.org/m\ngo (\n1.23\n)', 'error'),
    ('module example.org/m\ngo 1.23\ngo 1.24', 'unsupported'),
    ('module another.org/m\ngo 1.23', 'error'),
    ('module example.org/m\ngo 1.23\nrequire (', 'error'),
])
def test_missing_or_unverifiable_requirement_is_not_success(document, status):
    result = monitor.check({'version': '1.2.3'}, {'go': 'example.org/m'}, IO(document))
    assert result['status'] == status
    assert result['findings'] == []


def test_current_and_upgrade_read_their_own_module_not_latest():
    class Releases(IO):
        def json(self, method, url, **kwargs):
            return {'Version': 'v2.0.0' if 'v2.0.0' in url else 'v1.2.3'}

        def text(self, url, **kwargs):
            return 'module example.org/m\ngo ' + ('1.24' if 'v2.0.0' in url else '1.23')
    result = monitor.check({'version': '1.2.3', 'target_version': '2.0.0'},
                           {'go': 'example.org/m'}, Releases(''))
    assert result['status'] == 'ok'
    assert [(f['scope'], f['requirement']['constraint']['expression']) for f in result['findings']] == [
        ('current', '1.23'), ('upgrade', '1.24')]


def test_wrong_release_is_rejected_before_reading_module():
    io = IO('module example.org/m\ngo 1.23', 'v1.2.4')
    assert monitor.check({'version': '1.2.3'}, {'go': 'example.org/m'}, io)['status'] == 'error'
    assert len(io.urls) == 1


def test_go_identity_reuses_native_provider_without_package_name_guessing():
    assert monitor.inputs({'identity': {'source': 'go_proxy', 'url': 'https://proxy.golang.org/example.org/m/@latest'}}, None) == {'go': 'example.org/m'}
    assert monitor.inputs({'name': 'go-example-m'}, None) is None
