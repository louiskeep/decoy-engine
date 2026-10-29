"""Phase-boundary contract: `phase-result/v1` and `phase-event/v1`.

Pre-scan, Transform and Post-scan report through one typed envelope. Internal
assembly stays mutable in the orchestrator; `freeze` is the single export
boundary, and the public types declare only `tuple`/`frozenset` collections so
immutability holds at the type level, not just at export.

Safe output: `to_wire` is the only serializer. It accepts primitives, tuples and
frozen mappings (tuples of key/value pairs) and refuses anything else, so a raw
exception, DataFrame or arbitrary object cannot reach ordinary evidence.
Reasons are registry identities (`decoy_engine.reasons`), never formatted text.

Gate rule: only a `passed` result of a known schema can satisfy a required gate;
unknown schema, status or reason identity fails closed. Pattern: MLIR's split
between analyses that inspect input and transforms that change it, applied as
"assessment returns new evidence values and never mutates what it inspects".
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from decoy_engine.errors import DecoyError
from decoy_engine.reasons import ReasonRef, UnknownReasonError, require_known

RESULT_SCHEMA = "phase-result/v1"
EVENT_SCHEMA = "phase-event/v1"

Phase = Literal["pre_scan", "transform", "post_scan"]
Status = Literal[
    "completed", "passed", "failed", "error", "unavailable", "skipped", "not_applicable"
]

PHASES: frozenset[str] = frozenset({"pre_scan", "transform", "post_scan"})
STATUSES: frozenset[str] = frozenset(
    {"completed", "passed", "failed", "error", "unavailable", "skipped", "not_applicable"}
)

# Frozen JSON: scalars, tuples of frozen values, and `FrozenMap` (a tuple of key/value pairs).
# A mapping is a `FrozenMap`, never a plain tuple, so thawing never guesses list vs object.
FrozenScalar = str | int | float | bool | None
FrozenValue = FrozenScalar | tuple["FrozenValue", ...]


class FrozenMap(tuple):  # type: ignore[type-arg]
    """An immutable JSON object: a tuple of `(str, FrozenValue)` pairs sorted by key."""

    __slots__ = ()


class PhaseContractError(DecoyError, ValueError):
    """A phase envelope that is malformed, unsafe to export, or of an unknown version."""


def _str_tuple(value: Any, name: str) -> tuple[str, ...]:
    items = tuple(value)
    if not all(isinstance(i, str) for i in items):
        raise PhaseContractError(f"{name} must contain only str")
    return items


def freeze(value: Any) -> FrozenValue:
    """Deep-copy plain JSON-like data into the immutable representation.

    Lossless for JSON: objects become `FrozenMap`, arrays become tuples. Mapping keys must
    already be `str` (JSON object keys are strings; coercing `1` to `"1"` could silently merge
    two entries). Sets freeze to a sorted tuple because JSON has no set. NaN/inf are refused
    because they have no canonical JSON form.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PhaseContractError("non-finite number in phase evidence")
        return value
    if isinstance(value, FrozenMap):
        return FrozenMap((k, freeze(v)) for k, v in value)
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise PhaseContractError("phase evidence mapping keys must be str")
        return FrozenMap((k, freeze(v)) for k, v in sorted(value.items()))
    if isinstance(value, (set, frozenset)):
        return tuple(freeze(v) for v in sorted(value, key=repr))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    raise PhaseContractError(f"cannot freeze {type(value).__name__} into phase evidence")


def _thaw(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PhaseContractError("non-finite number in phase evidence")
        return value
    if isinstance(value, FrozenMap):
        return {k: _thaw(v) for k, v in value}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    raise PhaseContractError(f"unsafe value {type(value).__name__} in phase evidence")


@dataclass(frozen=True)
class PhaseCoverage:
    requested_scope: tuple[str, ...] = ()
    observed_scope: tuple[str, ...] = ()
    rows_examined: int | None = None
    exact: bool = True
    sampling_method: str | None = None
    limits: tuple[str, ...] = ()
    omissions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("requested_scope", "observed_scope", "limits", "omissions"):
            object.__setattr__(self, name, _str_tuple(getattr(self, name), name))


@dataclass(frozen=True)
class PhaseResult:
    phase: Phase
    check_id: str
    status: Status
    method: str
    method_version: str
    coverage: PhaseCoverage = PhaseCoverage()
    reasons: tuple[ReasonRef, ...] = ()
    identity: tuple[tuple[str, str], ...] = ()
    payload: FrozenValue = None
    schema_version: str = RESULT_SCHEMA

    def __post_init__(self) -> None:
        # Coerce at construction so a caller's list/dict can never alias evidence that was
        # already hashed: frozen dataclass fields alone only block re-assignment.
        object.__setattr__(self, "reasons", tuple(self.reasons))
        ident = self.identity.items() if isinstance(self.identity, Mapping) else self.identity
        pairs = tuple((str(k), str(v)) for k, v in ident)
        object.__setattr__(self, "identity", pairs)
        object.__setattr__(self, "payload", freeze(self.payload))


@dataclass(frozen=True)
class PhaseEvent:
    phase: Phase
    event: str
    status: Status
    reason: ReasonRef | None = None
    schema_version: str = EVENT_SCHEMA


def _check_header(phase: str, status: str, schema: str, expected: str) -> None:
    if schema != expected:
        raise PhaseContractError(f"unknown schema_version {schema!r}")
    if phase not in PHASES:
        raise PhaseContractError(f"unknown phase {phase!r}")
    if status not in STATUSES:
        raise PhaseContractError(f"unknown status {status!r}")


def _reason_wire(ref: ReasonRef) -> dict[str, str]:
    try:
        spec = require_known(ref)
    except UnknownReasonError as exc:
        raise PhaseContractError(str(exc)) from None
    return {"namespace": spec.namespace, "code": spec.code}


def to_wire(item: PhaseResult | PhaseEvent) -> dict[str, Any]:
    """The one serializer: validates versions and reasons, thaws to JSON-safe data."""
    if isinstance(item, PhaseResult):
        _check_header(item.phase, item.status, item.schema_version, RESULT_SCHEMA)
        cov = item.coverage
        return {
            "schema_version": item.schema_version,
            "phase": item.phase,
            "check_id": item.check_id,
            "status": item.status,
            "method": item.method,
            "method_version": item.method_version,
            "identity": {k: v for k, v in item.identity},
            "coverage": {
                "requested_scope": list(cov.requested_scope),
                "observed_scope": list(cov.observed_scope),
                "rows_examined": cov.rows_examined,
                "exact": cov.exact,
                "sampling_method": cov.sampling_method,
                "limits": list(cov.limits),
                "omissions": list(cov.omissions),
            },
            "reasons": [_reason_wire(r) for r in item.reasons],
            "payload": _thaw(item.payload),
        }
    _check_header(item.phase, item.status, item.schema_version, EVENT_SCHEMA)
    return {
        "schema_version": item.schema_version,
        "phase": item.phase,
        "event": item.event,
        "status": item.status,
        "reason": None if item.reason is None else _reason_wire(item.reason),
    }


def canonical_bytes(item: PhaseResult | PhaseEvent) -> bytes:
    return json.dumps(to_wire(item), sort_keys=True, separators=(",", ":")).encode()


def satisfies_required_gate(item: PhaseResult) -> bool:
    """Fail-closed: only a `passed` result of a known schema with known reasons counts."""
    try:
        to_wire(item)
    except PhaseContractError:
        return False
    return item.status == "passed"


def error_result(
    phase: Phase,
    check_id: str,
    method: str,
    method_version: str,
    reason: ReasonRef,
    exc: BaseException,
) -> PhaseResult:
    """A failed check as evidence: registry reason plus the exception type name only.

    Raw exception text can carry paths and cell values, so it is never captured.
    """
    return PhaseResult(
        phase=phase,
        check_id=check_id,
        status="error",
        method=method,
        method_version=method_version,
        reasons=(reason,),
        payload=freeze({"error_type": type(exc).__name__}),
    )
