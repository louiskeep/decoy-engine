"""Semantic eligibility for the unified generation core.

This module answers one question per column: can its generator produce the re-baselined
deterministic output under persistent per-batch state? The answer is a pure function of the
plan's configuration and the custom-provider registry. It never reads the byte budget, the sink,
or the host, so the VALUES a column produces cannot depend on the execution environment. How the
values are materialized (streamed to a bounded sink or accumulated in memory) is decided
elsewhere, from the same eligible set (`_streamed.py`).

Eligibility is whole-plan: one ineligible column sends the whole plan to the legacy whole-table
path, so every column lands in exactly one path regardless of host.

The eligible families are the prefix-stable ones: a stateful generator that draws `size=batch`
repeatedly reproduces one `size=n` draw (the Mersenne Twister and PCG64 streams advance
identically), which is the property CPython's `random.Random.choices` and NumPy's
`Generator.random`/`integers` document for sequential draws from one generator.

Declared Arrow types are fixed here from the configuration, so a batch is cast to a schema that
exists before any value is drawn. That removes value-dependent inference (an all-null column or a
zero-row table used to infer `null`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from faker import Faker

# Every generator kind `GenerateColumnConfig.type` allows, with its admission and the reason.
# `tests/unit/generation/test_fr0_admission.py` fails if a kind is added to the config Literal
# without a row here.
ADMISSION_MATRIX: dict[str, tuple[bool, str]] = {
    "sequence": (True, "value is start + step * global row ordinal; prefix-stable"),
    "categorical": (True, "one persistent random.Random stream; homogeneous scalar categories"),
    "faker": (True, "built-in providers only; per-row seeded or one persistent pooled stream"),
    "reference": (True, "without min/max_per_parent repair; parent key set built once"),
    "formula": (False, "whole-column pandas coercion"),
    "statistical": (False, "whole-column fit/condition_on, pinned-snapshot sampler"),
    "derived": (False, "reads sibling columns row by row; no sibling-input seam yet"),
    "derived_aggregate": (False, "whole-column reduction"),
    "grouped_series": (False, "group sort then restore; needs a bounded sort or two-pass design"),
    "windowed_date": (False, "anchor co-computation not admitted in this slice"),
    "group_key": (False, "group-scoped keys; needs a bounded two-pass design"),
}

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


@dataclass(frozen=True)
class ColumnAdmission:
    table: str
    column: str
    kind: str
    eligible: bool
    reason: str
    arrow_type: pa.DataType | None = None


@dataclass(frozen=True)
class PlanAdmission:
    columns: tuple[ColumnAdmission, ...]

    @property
    def eligible(self) -> bool:
        return all(c.eligible for c in self.columns)

    @property
    def declines(self) -> tuple[ColumnAdmission, ...]:
        return tuple(c for c in self.columns if not c.eligible)

    def reason(self) -> str:
        """First decline as `table.column:reason`, or `eligible`."""
        first = next(iter(self.declines), None)
        return "eligible" if first is None else f"{first.table}.{first.column}:{first.reason}"

    def schema(self, table: str) -> pa.Schema:
        return pa.schema(
            [pa.field(c.column, c.arrow_type) for c in self.columns if c.table == table]
        )


def _categorical_type(col: dict[str, Any]) -> tuple[pa.DataType | None, str]:
    cats = col.get("categories", ["Category A", "Category B"])
    if not isinstance(cats, list) or not cats:
        return None, "categories_not_a_nonempty_list"
    weights = col.get("weights")
    if weights is not None and (not isinstance(weights, list) or len(weights) != len(cats)):
        return None, "weights_do_not_match_categories"
    kinds = {type(c) for c in cats}
    if kinds == {str}:
        return pa.string(), "eligible"
    if kinds == {bool}:
        return pa.bool_(), "eligible"
    if kinds == {int}:
        if all(_INT64_MIN <= c <= _INT64_MAX for c in cats):
            return pa.int64(), "eligible"
        return None, "int_category_outside_int64"
    if kinds == {float}:
        return pa.float64(), "eligible"
    return None, "mixed_or_unsupported_category_types"


def faker_is_built_in(
    faker_type: str,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None,
) -> bool:
    """True when `faker_type` resolves to a reflected built-in (str-coerced) provider.

    A custom provider has no declared schema and no deterministic-output contract, so it stays
    on the legacy path. A probe call would only be diagnostic evidence (a provider can change
    type later, and probing a stateful provider changes its next output), so none is made.
    """
    from decoy_engine.generation.synthesize import _get_default_faker
    from decoy_engine.internal.faker_setup import has_custom_faker_override, resolve_pool_provider

    _, exact_name_available, custom = resolve_pool_provider(
        _get_default_faker(), faker_type, provider_snapshot=provider_snapshot
    )
    if custom or has_custom_faker_override(faker_type, provider_snapshot):
        return False
    if not exact_name_available:
        # Unknown names fall back to `word`; a custom `word` override would then supply the value.
        return not has_custom_faker_override("word", provider_snapshot)
    return True


def classify_plan(
    config: dict[str, Any],
    *,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
) -> PlanAdmission:
    """Classify every generate column of the plan's configuration."""
    tables = {t["name"]: t for t in (config.get("tables") or []) if t.get("generate_columns")}
    memo: dict[tuple[str, str], ColumnAdmission] = {}
    in_progress: set[tuple[str, str]] = set()

    def decline(table: str, col: dict[str, Any], reason: str) -> ColumnAdmission:
        return ColumnAdmission(table, col["name"], str(col.get("type")), False, reason)

    def admit(table: str, col: dict[str, Any], arrow_type: pa.DataType) -> ColumnAdmission:
        return ColumnAdmission(
            table, col["name"], str(col.get("type")), True, "eligible", arrow_type
        )

    def classify_reference(table: str, col: dict[str, Any]) -> ColumnAdmission:
        if int(col.get("min_per_parent") or 0) > 0 or int(col.get("max_per_parent") or 0) > 0:
            return decline(table, col, "reference_cardinality_repair")
        parent_name = col.get("reference_table")
        parent = tables.get(parent_name) if isinstance(parent_name, str) else None
        parent_col = next(
            (
                c
                for c in (parent or {}).get("generate_columns", [])
                if c["name"] == col.get("reference_column")
            ),
            None,
        )
        if parent is None or parent_col is None or not isinstance(parent_name, str):
            return decline(table, col, "reference_parent_unresolved")
        if parent_name == table:
            return decline(table, col, "reference_to_own_table")
        parent_adm = classify(parent_name, parent_col)
        if not parent_adm.eligible or parent_adm.arrow_type is None:
            return decline(table, col, "reference_parent_ineligible")
        return admit(table, col, parent_adm.arrow_type)

    def classify(table: str, col: dict[str, Any]) -> ColumnAdmission:
        key = (table, col["name"])
        if key in memo:
            return memo[key]
        if key in in_progress:
            return decline(table, col, "reference_cycle")
        in_progress.add(key)
        kind = col.get("type")
        if col.get("determinism") == "fresh":
            result = decline(table, col, "determinism_fresh")
        elif not ADMISSION_MATRIX.get(str(kind), (False, ""))[0]:
            result = decline(table, col, f"family_declined:{kind}")
        elif kind == "sequence":
            result = admit(table, col, pa.string())
        elif kind == "categorical":
            arrow_type, reason = _categorical_type(col)
            result = admit(table, col, arrow_type) if arrow_type else decline(table, col, reason)
        elif kind == "faker":
            faker_type = col.get("faker_type", "word")
            if isinstance(faker_type, str) and faker_is_built_in(faker_type, provider_snapshot):
                result = admit(table, col, pa.string())
            else:
                result = decline(table, col, "faker_custom_or_unresolved_provider")
        else:
            result = classify_reference(table, col)
        in_progress.discard(key)
        memo[key] = result
        return result

    columns: list[ColumnAdmission] = []
    for name, table in tables.items():
        seen: set[str] = set()
        for col in table["generate_columns"]:
            adm = classify(name, col)
            if col["name"] in seen:
                adm = decline(name, col, "duplicate_column_name")
            seen.add(col["name"])
            columns.append(adm)
    return PlanAdmission(tuple(columns))


__all__ = [
    "ADMISSION_MATRIX",
    "ColumnAdmission",
    "PlanAdmission",
    "classify_plan",
    "faker_is_built_in",
]
