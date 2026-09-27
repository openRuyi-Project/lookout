"""Structural contract for trusted, independently scheduled evidence adapters."""
from __future__ import annotations

from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from tracker.providers.client import ProviderIO


class Adapter(Protocol):
    """Module contract, without inheritance. Optional refresh(subject, inputs,
    previous) returns Schedule; modules without it use DEFAULT_REFRESH.
    Optional query_subject(subject, inputs) selects the subject fields used by
    check(). The safe default includes the complete source context.
    """
    VERSION: int
    HOSTS: set[str]

    def inputs(self, package: dict, configured: dict | None) -> dict | None: ...

    def check(self, subject: dict, inputs: dict, io: ProviderIO) -> dict: ...
