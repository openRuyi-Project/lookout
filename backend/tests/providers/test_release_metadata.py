"""A registry port returns the existing contract, without editing monitor branches."""
from types import SimpleNamespace

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
