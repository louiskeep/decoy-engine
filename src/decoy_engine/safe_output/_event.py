"""`SafeEvent`: one closed, validated occurrence with no free text.

Pattern source: RFC 9457 and the F29 reason records separate a stable problem identity from its
occurrence detail and never echo raw error text; the OWASP Logging Cheat Sheet's "log the event,
never the data". An event is a registry reason plus closed occurrence fields: a severity, a kind,
a closed exception label, an approved step name, closed counts and flags. There is no string,
mapping or ``Any`` field, so an exception message or a cell value has nowhere to ride.

Every field is checked by exact type (a subclass could override its own serializer), as the
progress events check their groups. Validation runs at construction and again in ``to_dict``, so
a frozen instance forged past ``__init__`` is caught on serialize.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from decoy_engine.reasons import (
    COUNT_KEYS,
    MAX_COUNTS,
    ReasonRef,
    Severity,
    approve_name,
    require_known,
)
from decoy_engine.safe_output._exception import SafeErrorType

SCHEMA = "decoy-safe-output/v1"
MAX_SEQ = 2**53 - 1
# 9999-12-31T23:59:59.999Z: a renderer can always turn the stamp into a datetime.
MAX_TS_MS = 253_402_300_799_999

JsonValue = str | int | list[str] | dict[str, str] | dict[str, int]


class EventKind(str, Enum):
    NARRATIVE = "narrative"
    STEP = "step"
    TERMINAL = "terminal"
    WITHHELD = "withheld"
    INVALID = "invalid"


class Flag(str, Enum):
    TRUNCATED = "truncated"


@dataclass(frozen=True)
class StepRef:
    """A step (node) name that passed ``approve_name``; a refused name raises, never degrades."""

    name: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self.name) is not str or approve_name(self.name) is None:
            raise ValueError("step is not an approved name")


def _check_bounded_int(name: str, value: object, upper: int) -> None:
    if type(value) is not int or not 0 <= value <= upper:
        raise ValueError(f"{name} must be an integer in [0, {upper}]")


def _check_counts(counts: object) -> None:
    if type(counts) is not tuple or len(counts) > MAX_COUNTS:
        raise ValueError(f"counts is a tuple of at most {MAX_COUNTS} pairs")
    previous: str | None = None
    for pair in counts:
        if type(pair) is not tuple or len(pair) != 2:
            raise ValueError("counts are (key, value) pairs")
        key, value = pair
        if type(key) is not str or key not in COUNT_KEYS:
            raise ValueError("count key is not a known count")
        if type(value) is not int or value < 0:
            raise ValueError("count value is a non-negative integer")
        if previous is not None and key <= previous:
            raise ValueError("counts are strictly sorted by key")
        previous = key


@dataclass(frozen=True)
class SafeEvent:
    """One safe-output occurrence. ``counts`` is the sorted pair form ``(("errors", 1),)``."""

    seq: int
    ts_ms: int
    sev: Severity
    kind: EventKind
    reason: ReasonRef
    err: SafeErrorType | None = None
    step: StepRef | None = None
    counts: tuple[tuple[str, int], ...] = ()
    flags: frozenset[Flag] = frozenset()

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        _check_bounded_int("seq", self.seq, MAX_SEQ)
        _check_bounded_int("ts_ms", self.ts_ms, MAX_TS_MS)
        if type(self.sev) is not Severity:
            raise ValueError("sev is not a Severity")
        if type(self.kind) is not EventKind:
            raise ValueError("kind is not an EventKind")
        if type(self.reason) is not ReasonRef:
            raise ValueError("reason is not a ReasonRef")
        if type(self.reason.namespace) is not str or type(self.reason.code) is not str:
            raise ValueError("reason identity is not plain text")
        require_known(self.reason)
        if self.err is not None and type(self.err) is not SafeErrorType:
            raise ValueError("err is not a SafeErrorType")
        if self.step is not None:
            if type(self.step) is not StepRef:
                raise ValueError("step is not a StepRef")
            self.step.validate()
        _check_counts(self.counts)
        if type(self.flags) is not frozenset or any(type(f) is not Flag for f in self.flags):
            raise ValueError("flags is a frozenset of Flag")

    def to_dict(self) -> dict[str, JsonValue]:
        """The JSON-ready wire dict, from re-validated fields only (depth at most 2)."""
        self.validate()
        spec = require_known(self.reason)
        out: dict[str, JsonValue] = {
            "s": SCHEMA,
            "seq": self.seq,
            "ts": self.ts_ms,
            "sev": self.sev.value,
            "kind": self.kind.value,
            "reason": {"ns": spec.namespace, "code": spec.code},
        }
        if self.err is not None:
            out["err"] = self.err.value
        if self.step is not None:
            out["step"] = self.step.name
        if self.counts:
            out["counts"] = dict(self.counts)
        if self.flags:
            out["flags"] = sorted(f.value for f in self.flags)
        return out
