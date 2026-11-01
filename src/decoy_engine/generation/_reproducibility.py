"""Reproducibility eligibility and the pinned input envelope.

Reproducibility is asserted only over an explicit, complete input set (the envelope). A column is
reproducibility-EXCLUDED when its output is not a deterministic function of that envelope, and the
exclusion propagates over the dependency closure: a reference, formula, derived, aggregate or
grouped column whose inputs include an excluded column is excluded too. The closure is computed on
the plan's configuration alone, so it holds on every execution path (a column that runs on the
legacy whole-table path is still excluded). Excluded columns are never re-frozen into a golden and
carry no reproducibility or route-independence assertion.

This axis is independent of semantic eligibility (`_admission.py`, which values a generator can
produce) and of streaming feasibility (`_streamed.py`, how the values are materialized).

Directly excluded: `determinism: fresh` (deliberate `os.urandom`), a custom Faker provider (a
pinned callable snapshot freezes the callable, not its mutable state, clock reads, or external
randomness, and no provider can declare a deterministic-output contract today), and a
clock-dependent provider or formula helper when no controlled clock is recorded in the envelope.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from decoy_engine import reasons

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from faker import Faker

# Faker providers that read the wall clock when called without explicit bounds. `date_of_birth`
# is excluded from this set on purpose: it is bound to a fixed anchor (`_dob_anchor.py`).
_CLOCK_FAKER_EXACT = frozenset(
    {
        "date",
        "date_object",
        "time",
        "time_object",
        "iso8601",
        "unix_time",
        "time_series",
        "date_time",
        "date_time_ad",
        "date_time_between",
        "date_time_between_dates",
        "date_between",
        "date_between_dates",
    }
)
_CLOCK_FAKER_PREFIXES = ("date_time_this_", "date_this_", "past_", "future_")
_FORMULA_CLOCK = re.compile(r"\b(now|today|days_from_now|months_from_now|years_from_now)\b")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Config keys that name a sibling column this column reads.
_SIBLING_KEYS = ("group_by", "order_by", "column", "anchor", "condition_on", "references")

REASON_FRESH = reasons.route.DETERMINISM_FRESH
REASON_CUSTOM_PROVIDER = reasons.route.CUSTOM_PROVIDER_WITHOUT_DETERMINISTIC_CONTRACT
REASON_CLOCK = reasons.route.CLOCK_DEPENDENT_WITHOUT_CONTROLLED_CLOCK
REASON_DEPENDS = reasons.route.DEPENDS_ON_EXCLUDED_COLUMN


def is_clock_dependent_faker_type(faker_type: str) -> bool:
    return faker_type in _CLOCK_FAKER_EXACT or faker_type.startswith(_CLOCK_FAKER_PREFIXES)


def _direct_reason(
    col: dict[str, Any],
    *,
    controlled_clock: bool,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None,
) -> str | None:
    from decoy_engine.internal.faker_setup import has_custom_faker_override

    if col.get("determinism") == "fresh":
        return REASON_FRESH
    kind = col.get("type")
    if kind == "faker":
        faker_type = col.get("faker_type", "word")
        if not isinstance(faker_type, str):
            return REASON_CUSTOM_PROVIDER
        if has_custom_faker_override(faker_type, provider_snapshot):
            return REASON_CUSTOM_PROVIDER
        if not controlled_clock and is_clock_dependent_faker_type(faker_type):
            return REASON_CLOCK
    if kind == "formula" and not controlled_clock:
        formula = col.get("formula")
        if isinstance(formula, str) and _FORMULA_CLOCK.search(formula):
            return REASON_CLOCK
    return None


def _sibling_inputs(col: dict[str, Any], siblings: set[str]) -> set[str]:
    names: set[str] = set()
    for key in _SIBLING_KEYS:
        value = col.get(key)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and item in siblings:
                names.add(item)
    expression = col.get("expression")
    if isinstance(expression, str):
        names |= set(_IDENTIFIER.findall(expression)) & siblings
    return names


def reproducibility_exclusions(
    config: dict[str, Any],
    *,
    controlled_clock: bool = False,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
) -> dict[tuple[str, str], str]:
    """`{(table, column): reason}` for every reproducibility-excluded generate column."""
    tables = {t["name"]: t for t in (config.get("tables") or []) if t.get("generate_columns")}
    excluded: dict[tuple[str, str], str] = {}
    inputs: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for tname, table in tables.items():
        siblings = {c["name"] for c in table["generate_columns"]}
        for col in table["generate_columns"]:
            key = (tname, col["name"])
            reason = _direct_reason(
                col, controlled_clock=controlled_clock, provider_snapshot=provider_snapshot
            )
            if reason is not None:
                excluded[key] = reason
            deps = {(tname, s) for s in _sibling_inputs(col, siblings - {col["name"]})}
            if col.get("type") == "reference":
                deps.add((str(col.get("reference_table")), str(col.get("reference_column"))))
            inputs[key] = deps
    changed = True
    while changed:
        changed = False
        for key, deps in inputs.items():
            if key not in excluded and any(d in excluded for d in deps):
                excluded[key] = REASON_DEPENDS
                changed = True
    return excluded


def engine_minor_version() -> str:
    import decoy_engine

    major, minor, *_ = str(decoy_engine.__version__).split(".")
    return f"{major}.{minor}"


@dataclass(frozen=True)
class PinnedEnvelope:
    """Every input reproducibility is asserted over. Two runs with equal digests must agree."""

    job_seed: int
    config_digest: str
    derive_key_digest: str | None
    custom_provider_names: tuple[str, ...]
    pinned_input_digests: tuple[str, ...]
    column_locales: tuple[tuple[str, str, str], ...]
    controlled_clock: str | None
    engine_minor: str

    def digest(self) -> str:
        payload = json.dumps(
            [
                self.job_seed,
                self.config_digest,
                self.derive_key_digest,
                list(self.custom_provider_names),
                list(self.pinned_input_digests),
                [list(t) for t in self.column_locales],
                self.controlled_clock,
                self.engine_minor,
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_envelope(
    config: dict[str, Any],
    *,
    derive_key_digest: str | None = None,
    instance_default_locale: str | None = None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
    pinned_input_digests: Iterable[str] = (),
    controlled_clock: str | None = None,
) -> PinnedEnvelope:
    """Assemble the envelope for a plan's configuration.

    `derive_key_digest` is supplied by the caller: the key material itself must never be hashed
    or stored here, so the caller names the key by a digest it already holds. The effective
    per-column locale resolves the platform default (`instance_default_locale`) the same way the
    generators do, so a changed instance default changes the envelope.
    """
    from decoy_engine.plan._seed import _normalize_job_seed_int

    locales: list[tuple[str, str, str]] = []
    for table in config.get("tables") or []:
        for col in table.get("generate_columns") or []:
            if col.get("type") == "faker":
                effective = col.get("locale") or instance_default_locale or "default"
                locales.append((table["name"], col["name"], str(effective)))
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return PinnedEnvelope(
        job_seed=_normalize_job_seed_int(config),
        config_digest=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        derive_key_digest=derive_key_digest,
        custom_provider_names=tuple(sorted(provider_snapshot or ())),
        pinned_input_digests=tuple(sorted(pinned_input_digests)),
        column_locales=tuple(locales),
        controlled_clock=controlled_clock,
        engine_minor=engine_minor_version(),
    )


__all__ = [
    "REASON_CLOCK",
    "REASON_CUSTOM_PROVIDER",
    "REASON_DEPENDS",
    "REASON_FRESH",
    "PinnedEnvelope",
    "build_envelope",
    "engine_minor_version",
    "is_clock_dependent_faker_type",
    "reproducibility_exclusions",
]
