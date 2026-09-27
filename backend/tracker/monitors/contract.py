"""Structural contract for trusted, independently scheduled evidence adapters."""
from __future__ import annotations

from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from tracker.providers.client import ProviderIO


class Adapter(Protocol):
    """Structural interface implemented by trusted modules, not subclasses.

    Optional query_subject(subject, inputs) narrows only the observation
    fingerprint; check() still receives the full subject. Omit a subject field
    only if changing it cannot change the observation's meaning.
    Optional refresh(subject, inputs, previous) returns Schedule; otherwise the
    runner uses DEFAULT_REFRESH. Both hooks are pure.
    """
    VERSION: int
    HOSTS: set[str]

    def inputs(self, package: dict, configured: dict | None) -> dict | None: ...

    def check(self, subject: dict, inputs: dict, io: ProviderIO) -> dict: ...
