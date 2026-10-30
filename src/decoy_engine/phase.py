"""Phase-boundary contract: `phase-result/v1` and `phase-event/v1`.

Pre-scan, Transform and Post-scan report through one typed envelope. Internal
assembly stays mutable in the orchestrator; `freeze` is the single export
boundary, and the public types declare only `tuple`/`frozenset` collections so
immutability holds at the type level, not just at export.

Safe output: `to_wire` is the only serializer. It accepts primitives, tuples and
frozen mappings (tuples of key/value pairs) and refuses anything else, so a raw
exception, DataFrame or arbitrary object cannot reach ordinary evidence.
Reasons are registry identities (`decoy_engine.reasons`), never formatted text.

Every envelope, coverage and reason field is type-checked at construction and
again at export (a frozen dataclass can still be forced with `object.__setattr__`),
so nothing is accepted that later fails `canonical_bytes`, and every malformed
envelope surfaces as `PhaseContractError`, never a stray `TypeError`.

Gate rule: only a `passed` result of a known schema can satisfy a required gate;
unknown schema, status or reason identity fails closed. Pattern: MLIR's split
between analyses that inspect input and transforms that change it, applied as
"assessment returns new evidence values and never mutates what it inspects".
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Literal, TypeVar

from decoy_engine.errors import DecoyError
from decoy_engine.reasons import ReasonRef, UnknownReasonError, require_known

_F = TypeVar("_F", bound=Callable[..., Any])

# Canonical JSON must round-trip through every consumer, so integers are bounded to signed
# 64-bit (also keeps int -> str clear of CPython's digit-conversion limit) and nesting is
# bounded well inside the interpreter recursion limit.
INT_MIN = -(2**63)
INT_MAX = 2**63 - 1
MAX_DEPTH = 64

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


class FrozenMap(tuple[tuple[str, Any], ...]):
    """An immutable JSON object: a tuple of `(str, FrozenValue)` pairs sorted by key.

    Validated at construction, not at export: keys must be exact, unique `str` and values
    already frozen, so a direct `FrozenMap(...)` can never collide on serialization, drop a
    field, or pass the gate and then fail `canonical_bytes`.
    """

    __slots__ = ()

    def __new__(cls, pairs: Any = ()) -> FrozenMap:
        try:
            items = tuple(pairs)
        except Exception:
            raise PhaseContractError("FrozenMap needs an iterable of (key, value) pairs") from None
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, tuple) or len(item) != 2:
                raise PhaseContractError("FrozenMap entries must be (key, value) pairs")
            key, value = item
            if type(key) is not str:
                raise PhaseContractError("FrozenMap keys must be str")
            if key in seen:
                raise PhaseContractError("duplicate key in FrozenMap")
            seen.add(key)
            _require_frozen(value)
        return super().__new__(cls, sorted(items, key=lambda kv: kv[0]))


class PhaseContractError(DecoyError, ValueError):
    """A phase envelope that is malformed, unsafe to export, or of an unknown version."""


def _contained(fn: _F) -> _F:
    """Turn any foreign failure (hostile `__iter__`, recursion, ...) into a fixed-text contract error."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except PhaseContractError:
            raise
        except Exception:
            raise PhaseContractError("malformed phase evidence") from None

    return wrapper  # type: ignore[return-value]


def _check_int(value: int) -> None:
    if not INT_MIN <= value <= INT_MAX:
        raise PhaseContractError("integer outside the 64-bit canonical range")


def _check_depth(depth: int) -> None:
    if depth > MAX_DEPTH:
        raise PhaseContractError("phase evidence nested too deeply")


def _require_frozen(value: Any, depth: int = 0) -> None:
    _check_depth(depth)
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        _check_int(value)
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PhaseContractError("non-finite number in phase evidence")
        return
    if isinstance(value, FrozenMap):
        for _, v in value:
            _require_frozen(v, depth + 1)
        return
    if type(value) is tuple:
        for v in value:
            _require_frozen(v, depth + 1)
        return
    raise PhaseContractError(f"unfrozen {type(value).__name__} in FrozenMap")


@_contained
def _identity_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, (Mapping, tuple, list)):
        raise PhaseContractError("identity must be a mapping or a tuple of (str, str) pairs")
    items = value.items() if isinstance(value, Mapping) else value
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise PhaseContractError("identity entries must be (str, str) pairs")
        key, val = item
        if type(key) is not str or type(val) is not str:
            raise PhaseContractError("identity keys and values must be str")
        if key in seen:
            raise PhaseContractError("duplicate identity key")
        seen.add(key)
        pairs.append((key, val))
    return tuple(pairs)


@_contained
def _str_tuple(value: Any, name: str) -> tuple[str, ...]:
    # A bare str is iterable; accepting it would silently explode "abc" into ("a", "b", "c").
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (tuple, list)):
        raise PhaseContractError(f"{name} must be a tuple of str")
    items = tuple(value)
    if not all(type(i) is str for i in items):
        raise PhaseContractError(f"{name} must contain only str")
    return items


def _require_str(value: Any, name: str) -> None:
    if type(value) is not str:
        raise PhaseContractError(f"{name} must be str")


def _require_opt_str(value: Any, name: str) -> None:
    if value is not None:
        _require_str(value, name)


def _require_reason(value: Any) -> None:
    if type(value) is not ReasonRef:
        raise PhaseContractError("reasons must be ReasonRef")
    _require_str(value.namespace, "reason namespace")
    _require_str(value.code, "reason code")


@_contained
def _reason_tuple(value: Any) -> tuple[ReasonRef, ...]:
    if not isinstance(value, (tuple, list)):
        raise PhaseContractError("reasons must be a tuple of ReasonRef")
    items = tuple(value)
    for ref in items:
        _require_reason(ref)
    return items


def _set_sort_key(value: Any) -> tuple[int, Any]:
    # Rank by exact type so only same-typed primitives are ever compared with `<`; a foreign
    # `__lt__`/`__repr__` is unreachable because exact-type validation precedes sorting.
    if value is None:
        return (0, 0)
    t = type(value)
    if t is bool:
        return (1, value)
    if t is int:
        _check_int(value)
        return (2, value)
    if t is float:
        if not math.isfinite(value):
            raise PhaseContractError("non-finite number in phase evidence")
        return (3, value)
    if t is str:
        return (4, value)
    raise PhaseContractError("set elements must be None, bool, int, float or str")


def _freeze(value: Any, depth: int) -> FrozenValue:
    _check_depth(depth)
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        _check_int(value)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PhaseContractError("non-finite number in phase evidence")
        return value
    if isinstance(value, FrozenMap):
        return FrozenMap((k, _freeze(v, depth + 1)) for k, v in value)
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise PhaseContractError("phase evidence mapping keys must be str")
        return FrozenMap((k, _freeze(v, depth + 1)) for k, v in sorted(value.items()))
    if isinstance(value, (set, frozenset)):
        ordered = sorted(((_set_sort_key(v), v) for v in value), key=lambda kv: kv[0])
        return tuple(v for _, v in ordered)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v, depth + 1) for v in value)
    raise PhaseContractError("cannot freeze this type into phase evidence")


@_contained
def freeze(value: Any) -> FrozenValue:
    """Deep-copy plain JSON-like data into the immutable representation.

    Lossless for JSON: objects become `FrozenMap`, arrays become tuples. Mapping keys must
    already be `str` (JSON object keys are strings; coercing `1` to `"1"` could silently merge
    two entries). Sets of primitives freeze to a tuple ordered by (type rank, value) because
    JSON has no set; other set elements are refused before any comparison or repr runs.
    NaN/inf and integers outside signed 64-bit are refused because they have no canonical form.
    """
    return _freeze(value, 0)


def _thaw(value: Any, depth: int = 0) -> Any:
    _check_depth(depth)
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        _check_int(value)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PhaseContractError("non-finite number in phase evidence")
        return value
    if isinstance(value, FrozenMap):
        return {k: _thaw(v, depth + 1) for k, v in value}
    if isinstance(value, tuple):
        return [_thaw(v, depth + 1) for v in value]
    raise PhaseContractError("unsafe value in phase evidence")


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
        _check_coverage(self)


def _check_coverage(cov: Any) -> None:
    if type(cov) is not PhaseCoverage:
        raise PhaseContractError("coverage must be PhaseCoverage")
    for name in ("requested_scope", "observed_scope", "limits", "omissions"):
        value = getattr(cov, name)
        if type(value) is not tuple or not all(type(i) is str for i in value):
            raise PhaseContractError(f"{name} must be a tuple of str")
    # bool is an int subclass; a count of True is a type confusion, not a count.
    if cov.rows_examined is not None and type(cov.rows_examined) is not int:
        raise PhaseContractError("rows_examined must be int or None")
    if cov.rows_examined is not None:
        _check_int(cov.rows_examined)
    if type(cov.exact) is not bool:
        raise PhaseContractError("exact must be bool")
    _require_opt_str(cov.sampling_method, "sampling_method")


@dataclass(frozen=True)
class PhaseResult:
    phase: Phase
    check_id: str
    status: Status
    method: str
    method_version: str
    coverage: PhaseCoverage = field(default_factory=PhaseCoverage)
    reasons: tuple[ReasonRef, ...] = ()
    identity: tuple[tuple[str, str], ...] = ()
    payload: FrozenValue = None
    schema_version: str = RESULT_SCHEMA

    def __post_init__(self) -> None:
        # Coerce at construction so a caller's list/dict can never alias evidence that was
        # already hashed: frozen dataclass fields alone only block re-assignment.
        object.__setattr__(self, "reasons", _reason_tuple(self.reasons))
        object.__setattr__(self, "identity", _identity_pairs(self.identity))
        object.__setattr__(self, "payload", freeze(self.payload))
        # Types only: unknown phase/status/schema values stay constructible so the gate can
        # report them as fail-closed evidence rather than crash the producer.
        _check_result_fields(self)


def _check_result_fields(item: PhaseResult) -> None:
    for name in ("phase", "check_id", "status", "method", "method_version", "schema_version"):
        _require_str(getattr(item, name), name)
    _check_coverage(item.coverage)
    if type(item.reasons) is not tuple:
        raise PhaseContractError("reasons must be a tuple of ReasonRef")
    for ref in item.reasons:
        _require_reason(ref)
    if type(item.identity) is not tuple:
        raise PhaseContractError("identity must be a tuple of (str, str) pairs")
    _identity_pairs(item.identity)


@dataclass(frozen=True)
class PhaseEvent:
    phase: Phase
    event: str
    status: Status
    reason: ReasonRef | None = None
    schema_version: str = EVENT_SCHEMA

    def __post_init__(self) -> None:
        _check_event_fields(self)


def _check_event_fields(item: PhaseEvent) -> None:
    for name in ("phase", "event", "status", "schema_version"):
        _require_str(getattr(item, name), name)
    if item.reason is not None:
        _require_reason(item.reason)


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


def _encode(wire: dict[str, Any]) -> bytes:
    try:
        return json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
    except Exception:
        raise PhaseContractError("phase envelope has no canonical encoding") from None


@_contained
def to_wire(item: PhaseResult | PhaseEvent) -> dict[str, Any]:
    """The one serializer: validates versions and reasons, thaws to JSON-safe data.

    Exact-type dispatch: a duck-typed or subclassed look-alike is not an envelope. The wire
    form is proven canonically encodable before it is returned, so a caller (and the gate)
    never holds an envelope that later fails hashing or persistence.
    """
    wire = _wire(item)
    _encode(wire)
    return wire


def _wire(item: PhaseResult | PhaseEvent) -> dict[str, Any]:
    if type(item) is PhaseResult:
        _check_result_fields(item)
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
    if type(item) is not PhaseEvent:
        raise PhaseContractError("not a phase envelope")
    _check_event_fields(item)
    _check_header(item.phase, item.status, item.schema_version, EVENT_SCHEMA)
    return {
        "schema_version": item.schema_version,
        "phase": item.phase,
        "event": item.event,
        "status": item.status,
        "reason": None if item.reason is None else _reason_wire(item.reason),
    }


@_contained
def canonical_bytes(item: PhaseResult | PhaseEvent) -> bytes:
    return _encode(_wire(item))


def satisfies_required_gate(item: PhaseResult) -> bool:
    """Fail-closed: only a `passed` result of a known schema with known reasons counts."""
    if type(item) is not PhaseResult:
        return False
    try:
        canonical_bytes(item)
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
