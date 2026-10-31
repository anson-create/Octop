"""Evidence ports: the only way facts enter the system.

A port returns envelopes (see ``domain.evidence``) or raises ``PortUnavailable``.
It never invents an excerpt: when a source cannot be reached, the caller writes
"未检索" into the trace, which G-P1-03 accepts as an explicit statement of
absence — and G-P1-01 still requires a locator for anything claimed as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class PortError(RuntimeError):
    pass


class PortUnavailable(PortError):
    """The source is not configured or not reachable right now."""


@dataclass
class PortResult:
    port: str
    envelopes: list[dict[str, Any]] = field(default_factory=list)
    unavailable_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.unavailable_reason is None

    def trace_sources(self) -> list[str]:
        return [self.port] if self.ok else [f"{self.port}:未检索"]


class EvidencePort(Protocol):
    name: str

    def available(self) -> bool: ...

    def fetch(self, *, query: str, task_ref: str, limit: int = 10) -> PortResult: ...
