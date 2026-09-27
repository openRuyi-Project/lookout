"""Registry facts shared by License and Yanked; no maintainer decisions."""
from dataclasses import dataclass

from . import crates_metadata, pypi_metadata

BACKENDS = {"pypi": pypi_metadata, "cratesio": crates_metadata}
HOSTS = set().union(*(backend.HOSTS for backend in BACKENDS.values()))


@dataclass(frozen=True)
class Release:
    name: str
    source: str
    url: str
    license_expression: str | None
    license_declaration: str | None
    yanked: bool | None
    yanked_reason: str | None


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
    provider = next(iter(settings))
    backend = BACKENDS[provider]
    name = backend.project(settings)
    info, url = backend.release(name, version, io)
    if provider == "pypi":
        expression = info.get("license_expression")
        return Release(
            name=name, source="PyPI", url=url,
            license_expression=expression, license_declaration=expression,
            yanked=info.get("yanked"), yanked_reason=info.get("yanked_reason"),
        )
    declaration = info.get("license")
    # Cargo's deprecated slash operator means OR. Keep the declaration as evidence;
    # the monitor still applies strict SPDX validation and comparison budgets.
    # https://github.com/rust-lang/crates.io/blob/main/src/licenses.rs
    expression = declaration.replace("/", " OR ") if isinstance(declaration, str) else declaration
    return Release(
        name=name, source="crates.io", url=url,
        license_expression=expression, license_declaration=declaration,
        yanked=info.get("yanked"), yanked_reason=None,
    )
