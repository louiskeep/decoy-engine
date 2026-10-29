"""Typed, versioned progress events and their serializers (F29).

The event dataclasses, the typed retry, checkpoint and publication groups, the numeric and
identifier validation, and the two projections (`event_to_dict`, version 1, and
`event_to_legacy_dict`, version 0). The channel, sinks and bindings live in
`decoy_engine.progress`, which re-exports everything here: import from there.

Every field is typed and bounded, and an event can only be serialized through an explicit
per-variant allowlist, so an arbitrary attribute never rides out of the process.
"""

from __future__ import annotations

import builtins
import math
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from decoy_engine import reasons
from decoy_engine.reasons import Operation, ReasonRecord, approve_name, approve_ref
from decoy_engine.reasons._record import CleanupState, PublicationState

EVENT_SCHEMA_VERSION = reasons.EVENT_SCHEMA_VERSION
LEGACY_SCHEMA_VERSION = 0

ByteBasis = Literal["arrow_logical", "stored_output"]
_BYTE_BASES = frozenset({"arrow_logical", "stored_output"})
RetryScope = Literal["operation", "attempt", "route"]
RetryState = Literal["not_scheduled", "scheduled", "running", "exhausted"]
CheckpointState = Literal["unsupported", "none", "writing", "committed", "failed"]

# ---------------------------------------------------------------------------
# Validation (strict at construction; the channel sanitizes the values it computes)
# ---------------------------------------------------------------------------


def _check_count(name: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or None")


def _check_seconds(name: str, value: object) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite non-negative number or None")


def _fixed_or(value: object, fallback: str) -> str:
    """A fixed engine identifier (a phase, route or lane name), else ``fallback``."""
    return value if isinstance(value, str) and approve_ref(value) is not None else fallback


def _sanitize_names(event: Any, optional: tuple[str, ...]) -> None:
    for name in optional:
        value = getattr(event, name)
        if value is not None and approve_name(value) is None:
            object.__setattr__(event, name, None)


def _safe_error_names() -> frozenset[str]:
    from decoy_engine import errors
    from decoy_engine.reason_adapter import registered_families

    names = {
        n for n, v in vars(builtins).items() if isinstance(v, type) and issubclass(v, BaseException)
    }
    names |= {c.__name__ for c in registered_families()}
    names |= {
        n for n, v in vars(errors).items() if isinstance(v, type) and issubclass(v, BaseException)
    }
    return frozenset(names)


_SAFE_NAMES: frozenset[str] | None = None


def safe_error_class(name: str | None) -> str | None:
    """An exception class name safe to emit: an engine or builtin class, else ``"Exception"``.

    A class name is chosen by whoever defined the exception; a user callback's exception can
    carry data in its name. Only classes the engine itself defines (or Python's builtins) are
    echoed, so a hostile name is never a disclosure route.
    """
    global _SAFE_NAMES
    if name is None:
        return None
    if _SAFE_NAMES is None:
        _SAFE_NAMES = _safe_error_names()
    return name if name in _SAFE_NAMES else "Exception"


def _safe_registered_code(code: str | None) -> str | None:
    """``code`` when the registry knows it, else the fixed ``unregistered_reason`` wire string."""
    if code is None:
        return None
    return code if reasons.is_registered(code) else str(reasons.decoy.UNREGISTERED_REASON)


# ---------------------------------------------------------------------------
# Typed groups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryInfo:
    """A retry owner's report. Attempt numbers start at one; ``delay_s`` only when scheduled."""

    scope: RetryScope
    attempt: int
    retries_completed: int
    state: RetryState
    delay_s: float | None = None

    def __post_init__(self) -> None:
        if self.scope not in ("operation", "attempt", "route"):
            raise ValueError("retry scope is not a known scope")
        if self.state not in ("not_scheduled", "scheduled", "running", "exhausted"):
            raise ValueError("retry state is not a known state")
        _check_count("attempt", self.attempt)
        _check_count("retries_completed", self.retries_completed)
        if self.attempt < 1:
            raise ValueError("attempt numbers start at one")
        if self.retries_completed > self.attempt:
            raise ValueError("retries_completed cannot exceed the attempt number")
        _check_seconds("delay_s", self.delay_s)
        if self.delay_s is not None and self.state != "scheduled":
            raise ValueError("delay_s is only reported for a scheduled retry")

    def to_payload(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "attempt": self.attempt,
            "retries_completed": self.retries_completed,
            "state": self.state,
            "delay_s": self.delay_s,
        }


@dataclass(frozen=True)
class CheckpointInfo:
    """A durability owner's report. Only the owner can claim ``committed``, with a reference."""

    state: CheckpointState
    reference: str | None = None
    sequence: int | None = None
    rows: int | None = None
    bytes: int | None = None

    def __post_init__(self) -> None:
        if self.state not in ("unsupported", "none", "writing", "committed", "failed"):
            raise ValueError("checkpoint state is not a known state")
        _check_count("sequence", self.sequence)
        _check_count("rows", self.rows)
        _check_count("bytes", self.bytes)
        if self.reference is not None and approve_ref(self.reference) is None:
            raise ValueError("checkpoint reference is not an opaque approved identifier")
        if self.state == "committed" and (self.reference is None or self.sequence is None):
            raise ValueError("a committed checkpoint needs its reference and sequence")
        if self.state in ("unsupported", "none") and any(
            v is not None for v in (self.reference, self.sequence, self.rows, self.bytes)
        ):
            raise ValueError("an unsupported or empty checkpoint carries no progress")

    def to_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reference": self.reference,
            "sequence": self.sequence,
            "rows": self.rows,
            "bytes": self.bytes,
        }


@dataclass(frozen=True)
class PublicationInfo:
    """Where an output stands: the operation, an approved storage reference and the two states."""

    operation: Operation | None = None
    storage_ref: str | None = None
    publication_state: PublicationState | None = None
    cleanup_state: CleanupState | None = None

    def __post_init__(self) -> None:
        if self.storage_ref is not None and approve_ref(self.storage_ref) is None:
            raise ValueError("storage_ref is not an approved identifier")

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value if self.operation else None,
            "storage_ref": self.storage_ref,
            "publication_state": self.publication_state.value if self.publication_state else None,
            "cleanup_state": self.cleanup_state.value if self.cleanup_state else None,
        }


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _V1Fields:
    """The version-1 fields every event can carry. Keyword-only, so legacy positional
    constructors (`PhaseEvent("mask", "start", "t")`) are unchanged."""

    sequence: int | None = field(default=None, kw_only=True)
    phase: str | None = field(default=None, kw_only=True)
    object_ref: str | None = field(default=None, kw_only=True)
    bytes_done: int | None = field(default=None, kw_only=True)
    bytes_total_estimate: int | None = field(default=None, kw_only=True)
    byte_basis: str | None = field(default=None, kw_only=True)
    reason: ReasonRecord | None = field(default=None, kw_only=True)
    retry: RetryInfo | None = field(default=None, kw_only=True)
    checkpoint: CheckpointInfo | None = field(default=None, kw_only=True)
    publication: PublicationInfo | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        _check_count("sequence", self.sequence)
        _check_count("bytes_done", self.bytes_done)
        _check_count("bytes_total_estimate", self.bytes_total_estimate)
        if self.byte_basis is not None and self.byte_basis not in _BYTE_BASES:
            raise ValueError("byte_basis is not a known basis")
        if self.byte_basis is None and (
            self.bytes_done is not None or self.bytes_total_estimate is not None
        ):
            raise ValueError("byte counters need their basis; bases are never mixed")
        if self.object_ref is not None and approve_ref(self.object_ref) is None:
            raise ValueError("object_ref is not an approved identifier")
        if self.phase is not None and approve_ref(self.phase) is None:
            raise ValueError("phase is not a fixed identifier")
        # Exact types: a subclass could override its own serializer. Each group re-validates on
        # serialize, so an instance forged past its constructor is caught there too.
        for name, group in (
            ("reason", ReasonRecord),
            ("retry", RetryInfo),
            ("checkpoint", CheckpointInfo),
            ("publication", PublicationInfo),
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not group:
                raise ValueError(f"{name} must be a {group.__name__} or None")


@dataclass(frozen=True)
class PhaseEvent(_V1Fields):
    """A named phase (resolve, mask, generate, finalize, publish) starting,
    finishing or erroring, optionally scoped to one table."""

    name: str
    status: Literal["start", "finish", "error"]
    table: str | None = None
    rows_in: int | None = None
    rows_out: int | None = None
    elapsed_s: float | None = None
    error_class: str | None = None
    ts: float = field(default_factory=time.time)
    kind: str = field(default="phase", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_count("rows_in", self.rows_in)
        _check_count("rows_out", self.rows_out)
        _check_seconds("elapsed_s", self.elapsed_s)
        if self.status not in ("start", "finish", "error"):
            raise ValueError("phase status is not a known status")
        object.__setattr__(self, "name", _fixed_or(self.name, "unknown"))
        _sanitize_names(self, ("table",))
        object.__setattr__(self, "error_class", safe_error_class(self.error_class))


@dataclass(frozen=True)
class RateSample(_V1Fields):
    """Rows done so far on one table and the running rate, emitted at most once
    per `interval_s` per table plus once at the end of that table's stream.

    ``final`` means observation of that table's stream ended (it ran to the end, or the
    consumer closed it early). It does not mean the output was published."""

    table: str
    rows_done: int
    rows_per_sec: float
    elapsed_s: float
    rows_total_estimate: int | None = None
    final: bool = False
    ts: float = field(default_factory=time.time)
    kind: str = field(default="rate", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_count("rows_done", self.rows_done)
        _check_count("rows_total_estimate", self.rows_total_estimate)
        _check_seconds("rows_per_sec", self.rows_per_sec)
        _check_seconds("elapsed_s", self.elapsed_s)
        if approve_name(self.table) is None:
            object.__setattr__(self, "table", "unknown")


@dataclass(frozen=True)
class RouteEvent(_V1Fields):
    """The execution route the planner chose and, when it declined a lane, the
    coded reason."""

    route: str
    lane: str | None = None
    reason_code: str | None = None
    table: str | None = None
    ts: float = field(default_factory=time.time)
    kind: str = field(default="route", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "route", _fixed_or(self.route, "unknown"))
        object.__setattr__(
            self, "lane", None if self.lane is None else _fixed_or(self.lane, "unknown")
        )
        _sanitize_names(self, ("table",))
        object.__setattr__(self, "reason_code", _safe_registered_code(self.reason_code))


@dataclass(frozen=True)
class WarningEvent(_V1Fields):
    """A coded warning surfaced mid-run (never its message text)."""

    code: str
    table: str | None = None
    column: str | None = None
    ts: float = field(default_factory=time.time)
    kind: str = field(default="warning", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _sanitize_names(self, ("table", "column"))
        object.__setattr__(self, "code", _safe_registered_code(self.code))


@dataclass(frozen=True)
class TerminalEvent(_V1Fields):
    """The run's end: `ok` or `error` with the exception class name.

    An ``ok`` event carries no reason. An ``error`` event carries the run's first causal
    failure as a reason record."""

    status: Literal["ok", "error"]
    error_class: str | None = None
    elapsed_s: float | None = None
    sink_failures: int = 0
    ts: float = field(default_factory=time.time)
    kind: str = field(default="terminal", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_seconds("elapsed_s", self.elapsed_s)
        _check_count("sink_failures", self.sink_failures)
        if self.status not in ("ok", "error"):
            raise ValueError("terminal status is not a known status")
        object.__setattr__(self, "error_class", safe_error_class(self.error_class))


@dataclass(frozen=True)
class RetryEvent(_V1Fields):
    """A retry owner's report (never emitted by a retry classification alone)."""

    table: str | None = None
    ts: float = field(default_factory=time.time)
    kind: str = field(default="retry", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _sanitize_names(self, ("table",))
        if self.retry is None:
            raise ValueError("a retry event carries its retry group")


@dataclass(frozen=True)
class CheckpointEvent(_V1Fields):
    """A durability owner's checkpoint report."""

    table: str | None = None
    ts: float = field(default_factory=time.time)
    kind: str = field(default="checkpoint", init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        _sanitize_names(self, ("table",))
        if self.checkpoint is None:
            raise ValueError("a checkpoint event carries its checkpoint group")


ProgressEvent = (
    PhaseEvent
    | RateSample
    | RouteEvent
    | WarningEvent
    | TerminalEvent
    | RetryEvent
    | CheckpointEvent
)

# -- serialization -------------------------------------------------------------------------------
# The legacy (version 0) keys of each variant, exactly as `dataclasses.asdict` produced them.
_LEGACY_FIELDS: dict[type, tuple[str, ...]] = {
    PhaseEvent: (
        "name",
        "status",
        "table",
        "rows_in",
        "rows_out",
        "elapsed_s",
        "error_class",
        "ts",
        "kind",
    ),
    RateSample: (
        "table",
        "rows_done",
        "rows_per_sec",
        "elapsed_s",
        "rows_total_estimate",
        "final",
        "ts",
        "kind",
    ),
    RouteEvent: ("route", "lane", "reason_code", "table", "ts", "kind"),
    WarningEvent: ("code", "table", "column", "ts", "kind"),
    TerminalEvent: ("status", "error_class", "elapsed_s", "sink_failures", "ts", "kind"),
    RetryEvent: ("table", "ts", "kind"),
    CheckpointEvent: ("table", "ts", "kind"),
}
_IDENTITY = ("event_schema_version", "registry_version", "sequence", "phase", "object_ref")
_BYTES = ("bytes_done", "bytes_total_estimate", "byte_basis")
# What version 1 adds on top of the legacy keys, per variant. Nothing outside this is emitted.
_V1_EXTRA: dict[type, tuple[str, ...]] = {
    PhaseEvent: (*_IDENTITY, *_BYTES, "reason", "retry", "checkpoint", "publication"),
    RateSample: (*_IDENTITY, *_BYTES, "retry"),
    RouteEvent: (*_IDENTITY, "reason"),
    WarningEvent: (*_IDENTITY, "reason"),
    TerminalEvent: (*_IDENTITY, "reason", "publication"),
    RetryEvent: (*_IDENTITY, "retry", "reason"),
    CheckpointEvent: (*_IDENTITY, "checkpoint", "bytes_done", "byte_basis", "reason"),
}


_GROUPS: dict[str, Any] = {
    "reason": ReasonRecord,
    "retry": RetryInfo,
    "checkpoint": CheckpointInfo,
    "publication": PublicationInfo,
}


def _field_value(event: Any, name: str) -> Any:
    if name == "event_schema_version":
        return EVENT_SCHEMA_VERSION
    if name == "registry_version":
        return reasons.REGISTRY_VERSION
    value = getattr(event, name)
    if value is None or name not in _GROUPS:
        return value
    try:
        if type(value) is not _GROUPS[name]:
            raise ValueError(name)
        return value.to_payload()
    except Exception:
        # A group forged past its constructor never reaches the wire; the fixed diagnostic does.
        if name == "reason":
            return ReasonRecord.for_reason(reasons.decoy.REASON_RECORD_INVALID).to_payload()
        return None


def event_to_dict(event: ProgressEvent) -> dict[str, Any]:
    """The version-1 JSON-ready dict of the event (`kind` names the type).

    A strict additive superset of `event_to_legacy_dict`: every legacy key is present with
    the same meaning. Only the per-variant allowlisted fields are emitted."""
    variant = type(event)
    legacy = _LEGACY_FIELDS[variant]
    extra = _V1_EXTRA[variant]
    return {name: _field_value(event, name) for name in (*legacy, *extra)}


def event_to_legacy_dict(event: ProgressEvent) -> dict[str, Any]:
    """The version-0 projection: exactly the pre-F29 keys, through the same safe boundary."""
    return {name: _field_value(event, name) for name in _LEGACY_FIELDS[type(event)]}
