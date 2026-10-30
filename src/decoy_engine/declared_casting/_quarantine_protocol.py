"""Protected row-quarantine protocol: sink, session, receipt and the portable retrieval token.

Access is a PORTABLE capability, not a process-private handle: after a run seals its artifact,
``get_retrieval_token`` issues a serializable, value-free ``OpaqueRetrievalToken`` that a remote
owner can present later to ``open``. The local reference adapter's guarantee is scoped to token
possession plus owner/run and key-version match; identity semantics such as ``tenant-job-owner``
are enforced only by an identity-bearing platform adapter implementing this same protocol.

Pattern sources: Fernet (authenticated encryption, ``cryptography``) and the capability-URL /
bearer-token model for a revocable, possession-based read right.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from decoy_engine.contracts.declared_casting import ProtectedQuarantineConfig

QUARANTINE_SCHEMA_VERSION = 1
PROTECTION_ENCRYPTED = "encrypted"


@dataclass(frozen=True)
class QuarantineAccessContext:
    """Who is asking. The local adapter checks ``owner`` against the artifact's owner."""

    owner: str


@dataclass(frozen=True)
class QuarantineRun:
    owner: str
    run_id: str


# The policy is the validated config block; one type, no parallel copy to drift.
QuarantinePolicy = ProtectedQuarantineConfig


@dataclass(frozen=True)
class QuarantineCapability:
    """What a sink proves before any source is read."""

    protection: str
    key_version: str
    qualified: bool
    detail: str = ""  # a fixed token (never a path or secret), e.g. "seed_key_provider"


@dataclass(frozen=True)
class OpaqueRetrievalToken:
    value: str = field(repr=False)

    def __repr__(self) -> str:
        return "OpaqueRetrievalToken(<redacted>)"

    def to_wire(self) -> str:
        return self.value

    @classmethod
    def from_wire(cls, wire: str) -> OpaqueRetrievalToken:
        if not isinstance(wire, str):
            raise TypeError("a retrieval token travels as text")
        return cls(wire)


@dataclass(frozen=True)
class QuarantineEnvelope:
    """One quarantined row. ``row`` holds the raw framed values (that is the point)."""

    table: str
    row_position: int
    reasons: tuple[Any, ...]
    row: Mapping[str, Any]


@dataclass(frozen=True)
class QuarantineRecord:
    table: str
    row_position: int
    reasons: tuple[str, ...]
    row: dict[str, Any]
    schema_version: int = QUARANTINE_SCHEMA_VERSION


@dataclass(frozen=True)
class QuarantineReceipt:
    """Value-free: opaque ref, counts, digest, times and protection."""

    ref: str
    rows: int
    bytes: int
    digest: str
    created_at: float
    expires_at: float
    protection: str
    key_version: str
    schema_version: int = QUARANTINE_SCHEMA_VERSION

    def to_evidence(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "rows": self.rows,
            "bytes": self.bytes,
            "digest": self.digest,
            "expires_at": self.expires_at,
            "protection": self.protection,
            "key_version": self.key_version,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class PurgeResult:
    purged: int
    failed: int
    retained: int


@dataclass
class QuarantineSession:
    """Opaque to callers; an adapter keeps its private state in ``state``."""

    session_id: str
    state: Any = field(default=None, repr=False)


@runtime_checkable
class ProtectedQuarantineSink(Protocol):
    @property
    def capability(self) -> QuarantineCapability: ...

    def begin(self, run: QuarantineRun, policy: QuarantinePolicy) -> QuarantineSession: ...

    def write_row(self, session: QuarantineSession, envelope: QuarantineEnvelope) -> None: ...

    def seal(self, session: QuarantineSession) -> QuarantineReceipt: ...

    def abort(self, session: QuarantineSession) -> str: ...

    def get_retrieval_token(
        self, receipt: QuarantineReceipt | str, access_context: QuarantineAccessContext
    ) -> OpaqueRetrievalToken: ...

    def open(
        self, token: OpaqueRetrievalToken, access_context: QuarantineAccessContext
    ) -> list[QuarantineRecord]: ...

    def purge_expired(self, now: float, authority: QuarantineAccessContext) -> PurgeResult: ...

    def discard(self, receipt: QuarantineReceipt) -> str:
        """Remove a sealed artifact whose run then failed; returns the cleanup state."""
        ...


@dataclass(frozen=True)
class ProtectedQuarantineRuntime:
    """The one runtime argument: the resolved sink and the owner it acts for."""

    sink: ProtectedQuarantineSink
    owner: str


def qualify_runtime(runtime: Any, policy: Mapping[str, Any]) -> str | None:
    """None when the runtime can honour ``policy``; else a fixed non-secret reason token."""
    sink = getattr(runtime, "sink", None)
    if runtime is None or sink is None or not isinstance(getattr(runtime, "owner", None), str):
        return "no_runtime"
    if not isinstance(sink, ProtectedQuarantineSink):
        return "sink_protocol"
    cap = sink.capability
    if not cap.qualified:
        return cap.detail or "unqualified"
    if cap.protection != policy.get("required_protection"):
        return "protection_mismatch"
    return None


Clock = Callable[[], float]
