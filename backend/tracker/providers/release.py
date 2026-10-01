"""Registry facts shared by License and Yanked; no maintainer decisions."""
from typing import Protocol

from tracker.providers import cpan, cratesio, go, pypi
from tracker.providers.model import IdentityProvider, Release, resolve_inputs


class Backend(IdentityProvider, Protocol):
    def metadata(self, settings: dict, version: str, io, /) -> Release: ...


BACKENDS: dict[str, Backend] = {"pypi": pypi, "cratesio": cratesio, "cpan": cpan, "go": go}
HOSTS = set().union(*(backend.HOSTS for backend in BACKENDS.values()))


def inputs(package, configured, *, capability='metadata'):
    backends = {key: value for key, value in BACKENDS.items() if callable(getattr(value, capability, None))}
    return resolve_inputs(backends, package, configured)


def read(settings, version, io, *, capability='metadata'):
    """Normalize provider fields here, before domain comparisons or rendering."""
    inputs({}, settings, capability=capability)
    return getattr(BACKENDS[next(iter(settings))], capability)(settings, version, io)
