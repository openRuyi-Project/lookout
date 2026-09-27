"""Normalized release facts shared by registry adapters."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Release:
    name: str
    source: str
    url: str
    license_expression: str | None
    license_declaration: str | None
    yanked: bool | None
    yanked_reason: str | None
