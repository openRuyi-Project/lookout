"""Structural contract for trusted, independently scheduled evidence adapters."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

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
    __name__: str
    __file__: str

    @property
    def VERSION(self) -> int:
        raise NotImplementedError

    @property
    def HOSTS(self) -> set[str]:
        raise NotImplementedError

    def inputs(self, package: dict, configured: dict | None) -> dict | None:
        raise NotImplementedError

    def check(self, subject: dict, settings: dict, io: ProviderIO, /) -> dict:
        raise NotImplementedError
