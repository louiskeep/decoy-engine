"""The closed ``decoy-telemetry/v1`` vocabulary and the value type every span is built from.

Pattern: OpenTelemetry semantic conventions (low-cardinality span names, ``error.type`` from a
closed set and only on failure) and the OWASP Logging Cheat Sheet rule that a monitoring
backend gets identifiers and counts, never data. The closed-label shape follows the Platform's
OBS-8 metric labels. Everything a span may hold is enumerated here, so an exporter built on
these records cannot widen what reaches a collector.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from decoy_engine import reasons
from decoy_engine.progress import safe_error_class
from decoy_engine.reasons import Phase

TELEMETRY_SCHEMA = "decoy-telemetry/v1"

PHASE_VALUES = frozenset(p.value for p in Phase)

SPAN_NAMES = frozenset({"decoy.job", "decoy.run", "decoy.read"}) | frozenset(
    f"decoy.phase.{p}" for p in PHASE_VALUES
)

ATTRIBUTE_KEYS = frozenset(
    {
        "decoy.mode",
        "decoy.outcome",
        "decoy.phase",
        "decoy.reason",
        "error.type",
        "decoy.rows_in",
        "decoy.rows_out",
        "decoy.bytes",
        "decoy.elapsed_s",
        "decoy.spans_dropped",
        "decoy.job.id",
        "decoy.engine.version",
    }
)

RESOURCE_KEYS = frozenset({"service.name", "service.version"})

# Fixed string sets. Modes are the job kinds a trace can start for;
# ``incomplete`` is the outcome of a run that ended without a terminal status.
STRING_VOCABULARIES: dict[str, frozenset[str]] = {
    "decoy.mode": frozenset({"mask", "generate", "convert", "graph"}),
    "decoy.outcome": frozenset({"success", "failed", "cancelled", "incomplete"}),
    "decoy.phase": PHASE_VALUES,
}

# String keys whose closed set is owned elsewhere in the engine, checked by rule.
RULE_VOCABULARIES: dict[str, str] = {
    "decoy.reason": "registered_namespace_code",
    "error.type": "safe_error_class",
    "decoy.engine.version": "engine_version",
}

INTEGER_KEYS = frozenset(
    {"decoy.rows_in", "decoy.rows_out", "decoy.bytes", "decoy.spans_dropped", "decoy.job.id"}
)
NUMBER_KEYS = frozenset({"decoy.elapsed_s"})


def reason_value(namespace: object, code: object) -> str | None:
    """``namespace.code`` when the registry knows that identity, else ``None``."""
    if type(namespace) is not str or type(code) is not str:
        return None
    if reasons.find(namespace, code) is None:
        return None
    return f"{namespace}.{code}"


def _rule_allows(key: str, value: str) -> bool:
    rule = RULE_VOCABULARIES[key]
    if rule == "registered_namespace_code":
        namespace, _, code = value.partition(".")
        return reason_value(namespace, code) == value
    if rule == "safe_error_class":
        return safe_error_class(value) == value
    from decoy_engine.release import __version__

    return value == __version__


def _check_attribute(key: object, value: object) -> None:
    if type(key) is not str or key not in ATTRIBUTE_KEYS:
        raise ValueError("span attribute key is not in ATTRIBUTE_KEYS")
    if type(value) is str:
        allowed = STRING_VOCABULARIES.get(key)
        if allowed is not None and value in allowed:
            return
        if key in RULE_VOCABULARIES and _rule_allows(key, value):
            return
        raise ValueError(f"span attribute {key} holds a string outside its vocabulary")
    if key in INTEGER_KEYS:
        ok = type(value) is int and value >= 0
    elif key in NUMBER_KEYS:
        ok = (type(value) is int or type(value) is float) and math.isfinite(value) and value >= 0
    else:
        ok = False
    if not ok:
        raise ValueError(f"span attribute {key} must be a finite non-negative number")


@dataclass(frozen=True)
class SpanRecord:
    """One finished span: a closed name, epoch-nanosecond bounds, ok or not, closed attributes."""

    name: str
    start_ns: int
    end_ns: int
    ok: bool
    attributes: tuple[tuple[str, str | int | float], ...] = ()

    def __post_init__(self) -> None:
        # Exact types, as the progress events: a subclass could override its own comparisons.
        if type(self.name) is not str or self.name not in SPAN_NAMES:
            raise ValueError("span name is not in SPAN_NAMES")
        if type(self.start_ns) is not int or type(self.end_ns) is not int:
            raise ValueError("span bounds must be integers")
        if not 0 <= self.start_ns <= self.end_ns:
            raise ValueError("span bounds must be non-negative and ordered")
        if type(self.ok) is not bool:
            raise ValueError("span ok must be a bool")
        if type(self.attributes) is not tuple:
            raise ValueError("span attributes must be a tuple of pairs")
        seen: set[str] = set()
        for pair in self.attributes:
            if type(pair) is not tuple or len(pair) != 2:
                raise ValueError("span attributes must be a tuple of pairs")
            key, value = pair
            _check_attribute(key, value)
            if key in seen:
                raise ValueError("span attribute key is repeated")
            seen.add(key)
