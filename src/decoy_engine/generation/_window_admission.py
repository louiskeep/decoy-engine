"""Class-A admission for the windowed `generate_rows` primitive (GEN-API Slice 0).

A windowed read computes row `i` directly from `(column seed, i)`, which is only sound for
columns whose value at row `i` has no prefix state. That is Class A: statistical
(numeric / categorical / datetime / freetext), sequence, per-row built-in Faker, and
windowed_date over a Class A anchor. Everything else is Class B (pooled Faker, the ordinary
`categorical` pool, null injection, reference, derived, grouped) and is not served here.

The classifier is fail-closed and runs BEFORE any value is drawn. Two outcomes besides Class A:

- Refused (`GenerationAdmissionError`, coded): the column cannot be generated safely at all,
  or would crash or blow memory mid-window (`formula`, an overflowing numeric span, an
  unrepresentable year, an oversized cell, the degenerate stats `sample_column` raises on).
- Class B (`WindowAdmission.klass == "B"`): well-formed, just not seekable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any, NoReturn

import pandas as pd

from decoy_engine.errors import DecoyError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from faker import Faker

    from decoy_engine.generation.statistical import StatisticalSpec

# Longest single cell, in characters, a windowed read will materialize. An operator lowers or
# raises it per call; the default is large enough for real freetext yet far below a memory risk.
DEFAULT_MAX_CELL_CHARS = 65_536

CODE_UNBOUNDED = "generation_unbounded_column"
CODE_DEGENERATE = "statistical_stats_degenerate"
CODE_NOT_REPRODUCIBLE = "generation_not_reproducible"
CODE_CONDITION_UNAVAILABLE = "statistical_condition_column_unavailable"
CODE_FLAG_TOKEN = "statistical_flag_token_invalid"  # noqa: S105 -- an error code, not a credential
CODE_NOT_CLASS_A = "generation_not_class_a"
CODE_TABLE_UNKNOWN = "generation_table_unknown"
CODE_WINDOW_INVALID = "generation_window_invalid"
CODE_TIMEOUT = "generation_window_timeout"

_MIN_YEAR = 1
_MAX_SAMPLED_YEAR = 9998  # datetime(year + 1, 1, 1) must be representable
# Digits of a sequence value a window can reach (offset + count < 2**64, step bounded by config).
_SEQUENCE_DIGIT_ALLOWANCE = 40


class GenerationWindowError(DecoyError):
    """Base for the coded errors the windowed primitive raises."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class GenerationAdmissionError(GenerationWindowError):
    """A column was refused (or a Class B table was asked for). Names the column and property."""

    def __init__(
        self, *, code: str, message: str, table: str, column: str | None, prop: str | None
    ) -> None:
        self.table = table
        self.column = column
        self.prop = prop
        where = f"{table}.{column}" if column else table
        super().__init__(code=code, message=f"{where}: {message}")


class GenerationWindowTimeoutError(GenerationWindowError):
    """The per-request wall-clock deadline passed before the window finished."""


@dataclass(frozen=True)
class WindowAdmission:
    """Verdict for one table. `klass == "A"` is servable; "B" lists why not."""

    table: str
    klass: str
    class_b_reasons: tuple[tuple[str, str], ...] = ()

    @property
    def is_class_a(self) -> bool:
        return self.klass == "A"


def _refuse(table: str, col: dict[str, Any], code: str, prop: str, message: str) -> NoReturn:
    raise GenerationAdmissionError(
        code=code, message=message, table=table, column=str(col.get("name")), prop=prop
    )


def _floats(raw: Any) -> list[float]:
    return [float(x) for x in raw or []]


def _check_histogram(
    table: str,
    col: dict[str, Any],
    edges_key: str,
    counts_key: str,
    stats: Mapping[str, Any],
    *,
    strictly_ordered: bool,
) -> list[float]:
    """The degenerate-stats and bounded-span checks shared by numeric and freetext. Returns edges."""
    try:
        edges = _floats(stats.get(edges_key))
        counts = _floats(stats.get(counts_key))
    except (TypeError, ValueError):
        _refuse(table, col, CODE_DEGENERATE, edges_key, f"{edges_key}/{counts_key} not numeric")
    if len(edges) < 2 or len(counts) != len(edges) - 1:
        _refuse(
            table,
            col,
            CODE_DEGENERATE,
            edges_key,
            f"histogram is degenerate (edges={len(edges)}, counts={len(counts)})",
        )
    total = sum(counts)
    if any(c < 0 or not math.isfinite(c) for c in counts) or not math.isfinite(total) or total <= 0:
        _refuse(
            table, col, CODE_DEGENERATE, counts_key, "counts are negative, non-finite or sum <= 0"
        )
    if not all(math.isfinite(e) for e in edges):
        _refuse(table, col, CODE_UNBOUNDED, edges_key, "edges are not all finite")
    if strictly_ordered and any(b <= a for a, b in pairwise(edges)):
        _refuse(table, col, CODE_UNBOUNDED, edges_key, "edges are not strictly increasing")
    # lo + u * (hi - lo): an adjacent span that overflows to inf poisons the draw.
    if any(not math.isfinite(b - a) for a, b in pairwise(edges)):
        _refuse(table, col, CODE_UNBOUNDED, edges_key, "an adjacent edge span overflows")
    return edges


def _check_datetime(table: str, col: dict[str, Any], spec: StatisticalSpec) -> None:
    year_bins = spec.stats.get("year_bins") or []
    try:
        years = [int(b["year"]) for b in year_bins]
        weights = [float(b["count"]) for b in year_bins]
    except (TypeError, ValueError, KeyError):
        _refuse(table, col, CODE_DEGENERATE, "year_bins", "year_bins entries are malformed")
    total = sum(weights)
    if not years or any(w < 0 or not math.isfinite(w) for w in weights) or not total > 0:
        _refuse(table, col, CODE_DEGENERATE, "year_bins", "year_bins empty or counts unusable")
    bad = sorted(
        {
            y
            for y, w in zip(years, weights, strict=True)
            if w > 0 and not _MIN_YEAR <= y <= _MAX_SAMPLED_YEAR
        }
    )
    if bad:
        _refuse(
            table,
            col,
            CODE_UNBOUNDED,
            "year_bins",
            f"sampled year(s) {bad[:3]} outside {_MIN_YEAR}..{_MAX_SAMPLED_YEAR}",
        )
    for bound in ("min", "max"):
        try:
            parsed = datetime.fromisoformat(str(spec.stats[bound]))
        except (KeyError, ValueError):
            _refuse(
                table, col, CODE_UNBOUNDED, bound, f"datetime {bound} is missing or unparseable"
            )
        if parsed.tzinfo is not None:
            _refuse(table, col, CODE_UNBOUNDED, bound, "timezone-aware datetime bound")


def _check_categorical(
    table: str, col: dict[str, Any], spec: StatisticalSpec, max_cell_chars: int
) -> None:
    from decoy_engine.generation.statistical._sample import (
        _categorical_tables,
        _conditional_tables,
    )
    from decoy_engine.generation.statistical._spec import StatisticalSpecError

    try:
        values, weights = _categorical_tables(spec)
    except StatisticalSpecError as exc:
        _refuse(table, col, CODE_FLAG_TOKEN, "top_values", exc.message)
    except (TypeError, ValueError, KeyError):
        _refuse(table, col, CODE_DEGENERATE, "top_values", "top_values entries are malformed")
    if not values or any(w < 0 or not math.isfinite(w) for w in weights) or not sum(weights) > 0:
        _refuse(table, col, CODE_DEGENERATE, "top_values", "top_values empty or counts unusable")
    # BR-2: a category token is materialized per cell before any byte meter sees it.
    tokens = [str(v) for v in values]
    conditional = _conditional_tables(spec) if spec.condition_on else {}
    for kids, wts in conditional.values():
        tokens.extend(kids)
        if any(w < 0 or not math.isfinite(w) for w in wts) or not sum(wts) > 0:
            _refuse(table, col, CODE_DEGENERATE, "joint", "a conditional row has no usable weight")
    if any(len(t) > max_cell_chars for t in tokens):
        _refuse(
            table,
            col,
            CODE_UNBOUNDED,
            "top_values",
            f"a category token is longer than max_cell_chars={max_cell_chars}",
        )


def _check_statistical(
    table: str, col: dict[str, Any], spec: StatisticalSpec, max_cell_chars: int
) -> None:
    if spec.kind == "numeric":
        _check_histogram(table, col, "bin_edges", "bin_counts", spec.stats, strictly_ordered=True)
    elif spec.kind == "freetext":
        edges = _check_histogram(
            table, col, "length_bin_edges", "length_bin_counts", spec.stats, strictly_ordered=False
        )
        if int(edges[-1]) > max_cell_chars:
            _refuse(
                table,
                col,
                CODE_UNBOUNDED,
                "length_bin_edges",
                f"longest length {int(edges[-1])} exceeds max_cell_chars={max_cell_chars}",
            )
    elif spec.kind == "datetime":
        _check_datetime(table, col, spec)
    elif spec.kind == "categorical":
        _check_categorical(table, col, spec, max_cell_chars)
    else:
        _refuse(
            table, col, CODE_UNBOUNDED, "kind", f"statistical kind {spec.kind!r} has no sampler"
        )


def _check_sequence(table: str, col: dict[str, Any], max_cell_chars: int) -> None:
    width = (
        len(str(col.get("prefix", "")))
        + len(str(col.get("suffix", "")))
        + max(int(col.get("pad_length", 0)), _SEQUENCE_DIGIT_ALLOWANCE)
    )
    if width > max_cell_chars:
        _refuse(table, col, CODE_UNBOUNDED, "pad_length", f"cell width {width} > {max_cell_chars}")


def _windowed_anchor_ok(
    table: str,
    col: dict[str, Any],
    anchor: dict[str, Any],
    specs: Mapping[tuple[str, str], StatisticalSpec],
) -> bool:
    """True when the anchor is a statically range-safe date source.

    An invalid anchor raises mid-window (`WindowedDateError`), so only anchors whose whole output
    range provably lies inside the supported window (shifted by the largest offset) are admitted.
    """
    from decoy_engine.transforms.windowed_date import SUPPORTED_MAX, SUPPORTED_MIN

    if anchor.get("type") == "faker":
        return anchor.get("faker_type") == "date_of_birth"
    spec = specs.get((table, str(anchor.get("name"))))
    if anchor.get("type") != "statistical" or spec is None or spec.kind != "datetime":
        return False
    lo_pad = timedelta(days=max(0, -int(col.get("min_days", 0))))
    hi_pad = timedelta(days=max(0, int(col.get("max_days", 0))))
    lo = pd.Timestamp(str(spec.stats["min"]))
    hi = pd.Timestamp(str(spec.stats["max"]))
    if not (SUPPORTED_MIN + lo_pad <= lo and hi <= SUPPORTED_MAX - hi_pad):
        _refuse(
            table,
            col,
            CODE_UNBOUNDED,
            "anchor",
            "windowed_date anchor range plus offset leaves the supported date range",
        )
    return True


def classify_window_table(
    config: dict[str, Any],
    table: str,
    *,
    statistical_specs: Mapping[tuple[str, str], StatisticalSpec],
    max_cell_chars: int = DEFAULT_MAX_CELL_CHARS,
    instance_default_locale: str | None = None,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None = None,
) -> WindowAdmission:
    """Classify one generate table for a windowed read. Raises on any refusal, else A or B."""
    from decoy_engine.generation._faker_pool import pool_eligible
    from decoy_engine.generation._reproducibility import reproducibility_exclusions

    spec_table = next(
        (
            t
            for t in config.get("tables") or []
            if t.get("name") == table and t.get("generate_columns")
        ),
        None,
    )
    if spec_table is None:
        raise GenerationWindowError(
            code=CODE_TABLE_UNKNOWN, message=f"{table!r} is not a generate table of this plan"
        )
    cols: list[dict[str, Any]] = spec_table["generate_columns"]
    rows = int(spec_table.get("row_count") or 0)
    exclusions = reproducibility_exclusions(config, provider_snapshot=provider_snapshot)
    names = [c["name"] for c in cols]
    if len(set(names)) != len(names):
        raise GenerationAdmissionError(
            code=CODE_UNBOUNDED,
            message="duplicate column names",
            table=table,
            column=None,
            prop="name",
        )
    # Refusals are checked on every column before any Class B verdict, so a table that is both
    # refusable and Class B reports the refusal.
    for col in cols:
        if col.get("type") == "formula":
            _refuse(table, col, CODE_UNBOUNDED, "type", "formula columns cannot be bounded")
        reason = exclusions.get((table, col["name"]))
        if reason is not None:
            _refuse(table, col, CODE_NOT_REPRODUCIBLE, "determinism", reason)
        kind = col.get("type")
        if kind == "statistical":
            spec = statistical_specs.get((table, col["name"]))
            if spec is None:
                _refuse(table, col, "statistical_spec_not_pinned", "snapshot", "no pinned spec")
            else:
                _check_statistical(table, col, spec, max_cell_chars)
        elif kind == "sequence":
            _check_sequence(table, col, max_cell_chars)

    verdicts: dict[str, str | None] = {}  # column -> Class B reason, None for Class A
    for idx, col in enumerate(cols):
        verdicts[col["name"]] = _class_b_reason(
            table,
            col,
            cols[:idx],
            verdicts,
            statistical_specs,
            rows,
            instance_default_locale,
            pool_eligible,
        )
    reasons = tuple((n, r) for n, r in verdicts.items() if r is not None)
    return WindowAdmission(table, "B" if reasons else "A", reasons)


def _class_b_reason(
    table: str,
    col: dict[str, Any],
    earlier: list[dict[str, Any]],
    verdicts: Mapping[str, str | None],
    specs: Mapping[tuple[str, str], StatisticalSpec],
    rows: int,
    instance_default_locale: str | None,
    pool_eligible: Callable[..., bool],
) -> str | None:
    kind = col.get("type")
    if float(col.get("null_probability") or 0.0) > 0:
        return "null_injection"
    if kind == "sequence":
        return None
    if kind == "faker":
        locale = col.get("locale") or instance_default_locale
        if pool_eligible(
            col.get("faker_type", "word"), locale, rows, opted_out=col.get("pooled") is False
        ):
            return "pooled_faker"
        return None
    if kind == "statistical":
        spec = specs[(table, col["name"])]
        if spec.condition_on is None:
            return None
        if spec.condition_on not in {c["name"] for c in earlier}:
            _refuse(
                table,
                col,
                CODE_CONDITION_UNAVAILABLE,
                "condition_on",
                f"{spec.condition_on!r} is not declared before this column",
            )
        return "condition_parent_class_b" if verdicts.get(spec.condition_on) else None
    if kind == "windowed_date":
        anchor = next((c for c in earlier if c["name"] == col.get("anchor")), None)
        if anchor is None:
            return "windowed_date_anchor_not_earlier"
        if verdicts.get(anchor["name"]):
            return "windowed_date_anchor_class_b"
        return (
            None if _windowed_anchor_ok(table, col, anchor, specs) else "windowed_date_anchor_type"
        )
    return f"family_class_b:{kind}"


__all__ = [
    "DEFAULT_MAX_CELL_CHARS",
    "GenerationAdmissionError",
    "GenerationWindowError",
    "GenerationWindowTimeoutError",
    "WindowAdmission",
    "classify_window_table",
]
