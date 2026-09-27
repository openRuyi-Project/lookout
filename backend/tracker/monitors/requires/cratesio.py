"""Cargo's published minimum toolchain declaration, not a build guarantee."""
from tracker.monitors.requires.compare import numeric_release
from tracker.monitors.requires.model import Requirement, UnsupportedRequirements
from tracker.providers.cratesio import HOSTS, inputs, project, release


def read(version, settings, io):
    info, url = release(project(settings), version, io)
    declaration = info.get("rust_version")
    try:
        normalized = numeric_release(declaration)
    except ValueError as error:
        raise UnsupportedRequirements("Comparable rust-version declarations are missing or invalid.") from error
    # Cargo defines this bare numeric declaration as a minimum. Preserve the
    # original value; the scheme identifies the operation without inventing text.
    return [Requirement("rust", "Rust", "build", "numeric_minimum", declaration,
                        normalized, "crates.io", url)]
