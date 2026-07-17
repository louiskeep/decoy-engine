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

Every coercible cell -- in-range or tail -- renders through `str()`, same as
bucketize's whole-column `.astype(str)`: a column that kept in-range cells as
native Python numerics while tail cells became string labels would carry TWO
Python types in one pandas object column, and the engine's single Arrow<->
pandas conversion site (`_pandas_adapter.py` S9 spec Sec 3/7) infers ONE Arrow
type per column -- `pa.Table.from_pandas` raises `ArrowInvalid` the instant a
column holds both a kept int and a generalized str, which is the HIPAA
age>89 column's ordinary shape (most ages are in-range, a few are 90+).
Formatting every cell independently of what its neighbors need also keeps
top_code's output a pure function of ONE value (never of the batch/chunk it
happens to ride in), which is the same invariant `CHUNK_SAFE_STRATEGIES`
membership requires. The in-range cell's numeric CONTENT is still exactly
preserved -- only its Python type changes, from numeric to its string
rendering -- so utility on the untouched majority of the column survives.
"""

from __future__ import annotations

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

        # A null or non-null-uncoercible cell is carried through byte-for-byte
        # unchanged (null passthrough is not a leak; the uncoercible cell is
        # the trap-T4 case handled by the RowError above, never silently
        # rewritten). Every coercible cell -- in range or tail -- renders as a
        # string (see module docstring for why this can't be per-column
        # conditional): in-range via `str()` of the original value, tail via
        # the configured label.
        result = col.copy()
        result = result.where(~in_range_mask, col.astype(str))
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
        under_label = cfg.get("under_label")
        if not isinstance(under_label, str) or not under_label:
            return None, None
        return floor, under_label
