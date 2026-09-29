"""Registry facts shared by License and Yanked; no maintainer decisions."""
from typing import Protocol

from tracker.providers import cpan, cratesio, go, pypi
from tracker.providers.model import Release


class Backend(Protocol):
    HOSTS: set[str]

    def inputs(self, package: dict, configured: dict | None) -> dict | None: ...

    def metadata(self, settings: dict, version: str, io) -> Release: ...


BACKENDS: dict[str, Backend] = {"pypi": pypi, "cratesio": cratesio, "cpan": cpan, "go": go}
HOSTS = set().union(*(backend.HOSTS for backend in BACKENDS.values()))


def inputs(package, configured, *, capability='metadata'):
    backends = {key: value for key, value in BACKENDS.items() if callable(getattr(value, capability, None))}
    if configured is not None:
        if not isinstance(configured, dict) or len(configured) != 1:
            raise ValueError("release monitor needs one registry identity")
        backend = backends.get(next(iter(configured)))
        if backend is None:
            raise ValueError("unsupported release registry")
        return backend.inputs(package, configured)
    for backend in backends.values():
        result = backend.inputs(package, None)
        if result is not None:
            return result
    return None


def read(settings, version, io, *, capability='metadata'):
    """Normalize provider fields here, before domain comparisons or rendering."""
    inputs({}, settings, capability=capability)
    return getattr(BACKENDS[next(iter(settings))], capability)(settings, version, io)
