"""A registry port returns the existing contract, without editing monitor branches."""
from types import SimpleNamespace

import pytest

from tracker.providers import release
from tracker.providers.model import Release


def test_third_registry_normalizes_its_own_release(monkeypatch):
    expected = Release('fixture', 'Registry', 'https://registry.example/fixture/1',
                       'MIT', 'MIT', False, None)
    calls = []
    def metadata(settings, version, io):
        calls.append((settings, version, io))
        return expected
    backend = SimpleNamespace(HOSTS={'registry.example'},
                              inputs=lambda package, configured: configured,
                              metadata=metadata)
    monkeypatch.setitem(release.BACKENDS, 'fixture_registry', backend)
    io = object()
    assert release.read({'fixture_registry': 'fixture'}, '1', io) is expected
    assert calls == [({'fixture_registry': 'fixture'}, '1', io)]


@pytest.mark.parametrize('configured', [{}, {'a': 'x', 'b': 'y'}, {'unknown': 'x'}, []])
def test_registry_identity_rejects_ambiguous_or_unsupported_configuration(configured):
    from tracker.providers.model import resolve_inputs

    with pytest.raises(ValueError):
        resolve_inputs({}, {}, configured)


def test_registry_inference_stops_at_the_first_supported_identity():
    from tracker.providers.model import resolve_inputs

    calls = []
    def provider(name, result):
        def inputs(package, configured):
            calls.append((name, package, configured))
            return result
        return SimpleNamespace(inputs=inputs)
    backends = {'first': provider('first', None), 'second': provider('second', {'second': 'widget'}),
                'third': provider('third', {'third': 'wrong'})}
    package = {'name': 'fixture'}
    assert resolve_inputs(backends, package, None) == {'second': 'widget'}
    assert calls == [('first', package, None), ('second', package, None)]
