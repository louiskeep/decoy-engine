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
from typing import Any

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

        over_mask = nums.notna() & (nums > cap)
        if floor is not None and under_label is not None:
            under_mask = nums.notna() & (nums < floor)
        else:
            under_mask = pd.Series(False, index=nums.index)
        in_range_mask = nums.notna() & ~over_mask & ~under_mask

        # Canonical, dtype-independent string for the in-range cells (see the
        # module docstring's CRITICAL note): render from the COERCED numeric,
        # with integral values normalized to carry no trailing ".0", so the
        # SAME value renders identically whether the column ingested as int64
        # (null-free chunk) or float64 (null-bearing chunk / cross-substrate).
        # `raw.astype(str)` would emit "67" vs "67.0" depending on the chunk's
        # null content -- the CHUNK_SAFE byte-identity break Dennis reproduced.
        n_float = nums.astype("float64")
        integral = nums.notna() & np.isfinite(n_float) & (n_float == np.floor(n_float))
        in_range_str = nums.astype(str)  # fractional / non-finite fallback
        # integral+finite -> plain integer string (never "67.0"); the boolean
        # mask selects only those positions, so the int64 cast is always safe.
        in_range_str[integral] = n_float[integral].astype("int64").astype(str)

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
            resolved = _PRESETS.get(preset)
            if resolved is None:
                return None
            return resolved["cap"], resolved["over_label"]
        cap = cfg.get("cap")
        if isinstance(cap, bool) or not isinstance(cap, (int, float)):
            return None
        # A non-finite cap (NaN/inf) is a silent fail-open: `nums > nan`/`> inf`
        # is always False, so nothing generalizes and the whole column passes
        # through in the clear -- exactly the unresolvable-bound leak this guard
        # exists to prevent. Reject it (compile-check mirrors this).
        if not math.isfinite(cap):
            return None
        over_label = cfg.get("over_label")
        if not isinstance(over_label, str) or not over_label:
            return None
        return cap, over_label

    @staticmethod
    def _resolve_bottom_bound(cfg: dict[str, Any]) -> tuple[int | float | None, str | None]:
        """Resolve (floor, under_label). Both None when `floor` is absent or
        the pairing is malformed -- the bottom tail is optional, so a
        malformed `floor`/`under_label` degrades to "no floor" here rather
        than raising; `plan/_checks_top_code.py` rejects that shape at
        compile time so it never reaches a real run.
        """
        floor = cfg.get("floor")
        if floor is None or isinstance(floor, bool) or not isinstance(floor, (int, float)):
            return None, None
        # Non-finite floor (NaN/inf) is the same silent fail-open as cap: `nums <
        # nan`/`< inf` never fires, so the bottom tail is never generalized.
        if not math.isfinite(floor):
            return None, None
        under_label = cfg.get("under_label")
        if not isinstance(under_label, str) or not under_label:
            return None, None
        return floor, under_label
