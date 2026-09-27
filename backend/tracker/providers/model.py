"""Normalized release facts shared by registry adapters."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Release:
    """Registry metadata, before domain validation or comparison.

    license_expression may normalize registry syntax; license_declaration keeps
    its source spelling for evidence. A missing assertion is None, including
    yanked: absence is not a provider assertion of False.
    """
    name: str
    source: str
    url: str
    license_expression: str | None
    license_declaration: str | None
    yanked: bool | None
    yanked_reason: str | None
