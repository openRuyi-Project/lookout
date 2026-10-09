"""Normalized release facts shared by registry adapters."""
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


class IdentityProvider(Protocol):
    @property
    def HOSTS(self) -> set[str]:
        raise NotImplementedError

    def inputs(self, package: dict, configured: dict | None, /) -> dict | None:
        raise NotImplementedError


def resolve_inputs(providers: Mapping[str, IdentityProvider], package: dict, configured: dict | None) -> dict | None:
    """Explicit identities select one provider; inference stops at the first match."""
    if configured is not None:
        if not isinstance(configured, dict) or len(configured) != 1:
            raise ValueError('Expected one supported registry identity')
        provider = providers.get(next(iter(configured)))
        if provider is None:
            raise ValueError('Unsupported release registry')
        return provider.inputs(package, configured)
    for provider in providers.values():
        resolved = provider.inputs(package, None)
        if resolved is not None:
            return resolved
    return None


class UnsupportedRelease(ValueError):
    """The provider input cannot establish a comparable release assertion."""


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
    license_field: str | None = None
    version: str | None = None
    withdrawal_field: str = 'yanked'
