"""Registry facts shared by License and Yanked; no maintainer decisions."""
from typing import Protocol

from tracker.providers import cratesio as crates_metadata, pypi as pypi_metadata
from tracker.providers.model import Release


class Backend(Protocol):
    HOSTS: set[str]

    def inputs(self, package: dict, configured: dict | None) -> dict | None: ...

    def metadata(self, settings: dict, version: str, io) -> Release: ...


BACKENDS: dict[str, Backend] = {"pypi": pypi_metadata, "cratesio": crates_metadata}
HOSTS = set().union(*(backend.HOSTS for backend in BACKENDS.values()))


def inputs(package, configured):
    if configured is not None:
        if not isinstance(configured, dict) or len(configured) != 1:
            raise ValueError("release monitor needs one registry identity")
        backend = BACKENDS.get(next(iter(configured)))
        if backend is None:
            raise ValueError("unsupported release registry")
        return backend.inputs(package, configured)
    for backend in BACKENDS.values():
        result = backend.inputs(package, None)
        if result is not None:
            return result
    return None


def read(settings, version, io):
    """Normalize provider fields here, before domain comparisons or rendering."""
    inputs({}, settings)
    return BACKENDS[next(iter(settings))].metadata(settings, version, io)
