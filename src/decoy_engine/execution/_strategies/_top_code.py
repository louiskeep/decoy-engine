"""top_code strategy (HC-3b): numeric top-coding / bottom-coding generalization.

Established SDC (statistical disclosure control) methodology: top-coding and
bottom-coding a rare distribution tail into a single aggregate category, per
the U.S. Census Bureau's disclosure-avoidance handbooks and the Eurostat SDC
guidelines. The motivating requirement is HIPAA Safe Harbor
Sec. 164.514(b)(2)(i)(C): every age over 89 must be aggregated into a single
"90 or older" category -- shipped here as the "hipaa_age" preset
(cap=89, over_label="90+").

Mirrors bucketize's (`_bucketize.py`) coercion + fail-closed pattern exactly:
a config with no resolvable bound must never let the column pass through
unmasked (`_resolve_top_bound` returning `None` raises `StrategyError`, backed
by `plan/_checks_top_code.py` at compile time), and a non-null cell that fails
numeric coercion is a recorded `RowError`, never a silent keep-original.

Every coercible cell -- in-range or tail -- renders to a string, so the column
never carries two Python types at once: a column that kept in-range cells as
native numerics while tail cells became string labels would hold both an int
and a str in one pandas object column, and the engine's single Arrow<->pandas
conversion site (`_pandas_adapter.py` S9 spec Sec 3/7) infers ONE Arrow type
per column -- `pa.Table.from_pandas` raises `ArrowInvalid` the instant a column
holds both a kept int and a generalized str, which is the HIPAA age>89 column's
ordinary shape (most ages are in-range, a few are 90+).

CRITICAL -- the in-range string is CANONICAL, not `raw_column.astype(str)`: an
integral value renders WITHOUT a trailing ".0" (67, not 67.0), derived from the
coerced numeric rather than the raw column's inferred dtype. This is load-
bearing for the `CHUNK_SAFE_STRATEGIES` membership. On the chunked self-masking
route a non-FK numeric column ingests as pandas `int64` in a null-free chunk but
widens to `float64` in a null-bearing one (the Arrow int64+null widening; see
`_chunked_fk.py`), so `raw.astype(str)` would emit "67" in one chunk and "67.0"
in another for the SAME value -- output depending on the chunk boundary, which
breaks the byte-identical-to-full-frame guarantee `run_mask_pipeline_chunked`
makes and splits GROUP BY/JOIN groups. Rendering from the coerced numeric with
integral values normalized (mirroring bucketize's `.astype("Int64")`
normalization, not its raw whole-column `.astype(str)`) makes `str(value)` a
pure function of the value -- identical across chunk boundaries and across the
pandas/polars substrates. The in-range cell's numeric CONTENT is preserved
exactly; only its Python type changes, so utility on the untouched majority
survives.
"""

from __future__ import annotations

import math
from typing import Any, TypeGuard

import numpy as np
import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._row_errors import RowError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed

# Single source of truth for known presets, reused by the compile-time check
# (plan/_checks_top_code.py) so the two can never drift out of sync.
_PRESETS: dict[str, dict[str, Any]] = {
    "hipaa_age": {"cap": 89, "over_label": "90+"},
}

# float64 represents every integer up to 2**53 exactly; beyond it consecutive
# integers collapse (2**53 and 2**53+1 both round to 2**53). A cap/floor at or
# past this magnitude cannot be compared exactly against a null-bearing column
# (Arrow int64+null widens to float64 on ingest), so a true tail value could
# round DOWN to the cap and escape generalization -- a silent leak Codex
# reproduced. Bounds must stay strictly inside [-2**53, 2**53]; the check module
# rejects the rest at compile, this is the handler backstop. Shared with
# plan/_checks_top_code.py as the single source of truth for the threshold.
_MAX_EXACT_INT: int = 2**53


def _is_usable_bound(value: Any) -> TypeGuard[int | float]:
    """A cap/floor is usable only if it is a real, finite number strictly inside
    the exactly-representable integer range. Rejects bool, non-numeric, NaN/inf,
    and |value| >= 2**53. `math.isfinite` is only called on floats so a huge
    Python int (e.g. 10**400) is classified by magnitude, never an OverflowError.

    A `TypeGuard` (not a plain `bool`) so mypy narrows a validated `cap`/`floor`
    from `Any` to `int | float` at the call site -- the resolver return types
    stay honest without a cast."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return abs(value) < _MAX_EXACT_INT


class TopCodeStrategyHandler:
    """Generalize the rare tail(s) of a numeric column into an aggregate label."""

    name: str = "top_code"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        bound = self._resolve_top_bound(cfg)
        if bound is None:
            # Fail closed (mirrors bucketize's `_resolve_width is None` raise):
            # an unresolvable bound must never leave the column unmasked.
            raise StrategyError(
                code="top_code_bounds_unresolvable",
                strategy="top_code",
                message=(
                    f"column {column!r} uses top_code but neither a known "
                    f"preset ({cfg.get('preset')!r}) nor a resolvable numeric "
                    f"cap ({cfg.get('cap')!r}) with a non-empty over_label is "
                    "configured."
                ),
            )
        cap, over_label = bound

        floor, under_label = self._resolve_bottom_bound(cfg)

        col = df[column]
        nums = pd.to_numeric(col, errors="coerce")

        # A non-null cell that fails numeric coercion is a per-row format
        # error, NOT a silent keep-original (trap T4): the original value is
        # left in place in the frame, but the failure is recorded so it never
        # reaches the main output unflagged (`_pipeline.py`'s quarantine gate).
        non_null_and_uncoercible = col.notna() & nums.isna()
        for i in np.flatnonzero(non_null_and_uncoercible.to_numpy()):
            ctx.row_errors.append(
                RowError(
                    column=column,
                    row_index=int(i),
                    trigger="format_error",
                    reason="value is not numeric under top_code",
                )
            )

        # Fail closed on any value we cannot reason about EXACTLY (Codex R3
        # BLOCKER). An integer-dtype column carries Python-int / nullable-Int64
        # values exactly at any magnitude (lossless FK-safe ingest keeps it int),
        # but a NON-integer column -- a genuine float, or an object/string/Decimal
        # column that `pd.to_numeric` coerced through float64 -- collapses every
        # value with |v| >= 2**53 onto the nearest representable double. Two
        # distinct source values then render to the SAME string (utility
        # corruption), and a huge-negative value with no floor configured lands
        # in-range and renders from that collapsed double. We only emit a
        # generalized/rendered value when we can render it exactly, so a coerced
        # value at or beyond float64's exact-integer range is quarantined as a
        # format error rather than silently corrupted. The HIPAA age use case
        # (small integer ages) never trips this; it fires only on pathological
        # out-of-range magnitudes on non-integer columns.
        inexact_mask = pd.Series(False, index=nums.index)
        if not pd.api.types.is_integer_dtype(nums.dtype):
            nf = nums.astype("float64")
            inexact_mask = nums.notna() & np.isfinite(nf) & (np.abs(nf) >= _MAX_EXACT_INT)
            for i in np.flatnonzero(inexact_mask.to_numpy()):
                ctx.row_errors.append(
                    RowError(
                        column=column,
                        row_index=int(i),
                        trigger="format_error",
                        reason=(
                            "value magnitude is at or beyond 2**53 on a non-integer "
                            "column; top_code cannot compare or render it exactly"
                        ),
                    )
                )

        usable = nums.notna() & ~inexact_mask
        over_mask = usable & (nums > cap)
        if floor is not None and under_label is not None:
            under_mask = usable & (nums < floor)
        else:
            under_mask = pd.Series(False, index=nums.index)
        in_range_mask = usable & ~over_mask & ~under_mask

        # Canonical, dtype-independent string for the in-range cells (see the
        # module docstring's CRITICAL note): render from the COERCED numeric so
        # the SAME value renders identically whether the column ingested as int64
        # (null-free chunk) or float64 (null-bearing chunk / cross-substrate).
        # `raw.astype(str)` would emit "67" vs "67.0" depending on the chunk's
        # null content -- the CHUNK_SAFE byte-identity break Dennis reproduced.
        if pd.api.types.is_integer_dtype(nums.dtype):
            # Already ".0"-free and arbitrary-precision; no normalization needed.
            in_range_str = nums.astype(str)
        else:
            # Float dtype (nulls widened it, or genuine floats): an integral
            # value renders "67.0", so strip the ".0" via Python's
            # arbitrary-precision int -- NOT a fixed-width `.astype("int64")`,
            # which SILENTLY overflows/wraps negative above 2^63 (Dennis R2 LOW).
            n_float = nums.astype("float64")
            integral = nums.notna() & np.isfinite(n_float) & (n_float == np.floor(n_float))
            in_range_str = nums.astype(str)  # fractional / non-finite fallback
            in_range_str[integral] = n_float[integral].map(lambda v: str(int(v)))

        # A null or non-null-uncoercible cell is carried through byte-for-byte
        # unchanged (null passthrough is not a leak; the uncoercible cell is the
        # trap-T4 case handled by the RowError above, never silently rewritten).
        result = col.astype(object).copy()
        result = result.where(~in_range_mask, in_range_str)
        result = result.where(~over_mask, over_label)
        result = result.where(~under_mask, under_label)
        df[column] = result

        over_positions = np.flatnonzero(over_mask.to_numpy())
        under_positions = np.flatnonzero(under_mask.to_numpy())

        # The audit warning's row indices are LOCAL to this handler invocation
        # (this frame). On the chunked route each chunk is a separate invocation,
        # so the indices are chunk-local and the per-(code,column) warning
        # dedup in _chunked.py collapses them -- the concatenated audit log
        # then under-reports generalized rows. The MASKED OUTPUT is unaffected
        # and correct (every tail cell is generalized); only this evidence
        # sidecar is chunk-boundary-dependent. Tracked as a known limitation
        # shared with any warning-emitting chunk-safe strategy; see
        # docs/backlog/chunked-audit-evidence-row-indices.md.
        warnings: list[QualityWarning] = []
        if len(over_positions) or len(under_positions):
            generalized: dict[str, str] = {}
            for i in over_positions:
                generalized[f"row_{int(i)}"] = "over"
            for i in under_positions:
                generalized[f"row_{int(i)}"] = "under"
            warnings.append(
                QualityWarning(
                    code="top_code_generalized",
                    provider="top_code",
                    column=column,
                    detail={
                        "generalized": generalized,
                        "over_count": len(over_positions),
                        "under_count": len(under_positions),
                    },
                )
            )

        return df, warnings

    @staticmethod
    def _resolve_top_bound(cfg: dict[str, Any]) -> tuple[int | float, str] | None:
        """Resolve (cap, over_label), or None when unresolvable.

        `preset` takes precedence over manual `cap`/`over_label` (same
        precedence rule as bucketize's `preset` vs `width`).
        """
        preset = cfg.get("preset")
        if preset is not None:
            # `preset` must be a hashable str before the `in`/`.get` lookup: a
            # list/dict preset would raise `TypeError: unhashable type`.
            if not isinstance(preset, str):
                return None
            resolved = _PRESETS.get(preset)
            if resolved is None:
                return None
            return resolved["cap"], resolved["over_label"]
        cap = cfg.get("cap")
        # Rejects bool/non-numeric, NaN/inf, and |cap| >= 2**53 (past which the
        # tail comparison cannot be exact on a null-widened column -- a silent
        # leak). All three are unresolvable bounds; the compile check mirrors it.
        if not _is_usable_bound(cap):
            return None
        over_label = cfg.get("over_label")
        if not isinstance(over_label, str) or not over_label:
            return None
        return cap, over_label

    @staticmethod
    def _resolve_bottom_bound(cfg: dict[str, Any]) -> tuple[int | float | None, str | None]:
        """Resolve (floor, under_label). (None, None) ONLY when `floor` is
        absent (no bottom tail -- a legitimate, common shape).

        When `floor` IS present the operator intends bottom-coding, so a
        malformed floor or a missing/invalid `under_label` FAILS CLOSED here
        (raises), rather than silently degrading to "no floor". Silent degrade
        was a leak (Codex R2 HIGH): the compile check (`_checks_top_code.py`)
        rejects those shapes, but a `Plan` that bypasses it -- e.g. one
        deserialized from YAML straight into execution -- would otherwise drop
        the bottom tail and let a value that should be `under_label` pass through.

        Absence is keyed on `"floor" not in cfg`, NOT on `floor is None`: an
        explicit `floor: None` is a PRESENT-but-malformed bound (operator typo /
        templating miss), so it fails closed via `_is_usable_bound(None)` rather
        than being read as "no bottom tail" and silently disabling bottom-coding
        (Codex R3 HIGH -- `.get` can't tell absent from explicit-None).
        """
        if "floor" not in cfg:
            return None, None
        floor = cfg.get("floor")
        if not _is_usable_bound(floor):
            raise StrategyError(
                code="top_code_bounds_unresolvable",
                strategy="top_code",
                message=(
                    f"top_code floor {floor!r} is not a usable bound (must be a "
                    "finite number with magnitude below 2**53). Refusing to run "
                    "rather than silently disable bottom-coding."
                ),
            )
        under_label = cfg.get("under_label")
        if not isinstance(under_label, str) or not under_label:
            raise StrategyError(
                code="top_code_bounds_unresolvable",
                strategy="top_code",
                message=(
                    f"top_code sets floor {floor!r} but no non-empty under_label "
                    f"(got {under_label!r}). Refusing to run rather than silently "
                    "drop the bottom tail."
                ),
            )
        return floor, under_label
