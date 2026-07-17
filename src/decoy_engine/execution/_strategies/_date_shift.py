"""date_shift strategy (engine-v2 S9): shift each date by a keyed offset.

Re-keyed onto S3 (S9 spec §4 row 5): the per-value offset in
``[min_days, max_days]`` is `min_days + (int.from_bytes(derive(job_seed,
namespace, _canonicalize_source(value))[:8], "big") % range_size)` -- NOT the
legacy HMAC(column_key)/MD5 path. Same source date -> same shift within a
namespace; byte-stable across runs. Format detection + the vectorized
datetime parse/reformat are reused from V1 `transforms/date_shift._detect_format`.
Null + parse-failed positions restore the original value.

Sprint 2 honesty pack (2026-07-04, D7/D8, discovery 0.1): a non-null cell
that fails `pd.to_datetime` parsing used to silently keep the ORIGINAL
source value in the output -- the same per-value leak class as bucketize
(#13). `run` now records a `RowError` (trigger "format_error") for such
cells via `_row_errors.py`'s shared channel; it does NOT null or rewrite the
cell (trap T4). Source-null cells are unaffected (null passthrough is not a
leak); only a NON-null cell that fails to parse is a format error.

HC-3(a) (2026-07-17): optional `group_by` config names an ENTITY column
(e.g. `patient_id`). When set, the per-row digest input is the GROUP
column's value instead of the date value, so every row sharing a group
value gets the SAME offset and intra-entity intervals (e.g. admission ->
discharge length-of-stay) survive the shift. This is the standard
entity-anchored date-shift de-identification technique (cf. HIPAA Safe
Harbor date-shift guidance: shift consistently per patient, not per
event). `group_by` is None by default, and the digest input is then
byte-identical to the pre-HC-3(a) behavior (the date value itself).

Null group-by value policy: `_canonicalize_source` hard-errors on a raw
null (None) and on the float NaN sentinel pandas normally produces for a
missing object-dtype cell (see generation/pool/_canonicalize.py), so
feeding a null group value straight through would crash non-deterministically
depending on column dtype. Instead, a row whose group value is null falls
back to anchoring on ITS OWN date value (the no-`group_by` behavior for
that one row): deterministic, no crash, and every null-group row is *not*
forced to collide on one shared offset (each keeps its own date-derived
offset, same as an ungrouped column).
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.determinism import derive
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._row_errors import RowError
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.date_shift import _detect_format


class DateShiftStrategyHandler:
    """Shift dates by a deterministic per-value offset within a bounded range.

    Optional `group_by` (HC-3a): anchor the offset to a sibling entity
    column instead of the date value itself, so every date belonging to
    the same entity shifts by one consistent offset. See module docstring.
    """

    name: str = "date_shift"

    def required_sibling_columns(self, plan: ColumnSeed) -> list[str]:
        """Duck-typed hook (S12 polars port): sibling columns this handler
        needs beyond `column` itself. `PandasStrategyPort` calls this (if
        present) to widen the pandas slice it extracts from the polars
        frame; absent/empty is byte-identical to the pre-hook behavior.
        """
        cfg = provider_config_to_dict(plan.provider_config)
        group_by = cfg.get("group_by")
        return [group_by] if group_by else []

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        if plan.namespace is None:
            raise StrategyError(
                code="date_shift_requires_namespace",
                strategy="date_shift",
                message=f"column {column!r} uses date_shift but has no namespace.",
            )
        cfg = provider_config_to_dict(plan.provider_config)
        min_days = int(cfg.get("min_days", -365))
        max_days = int(cfg.get("max_days", 365))
        if min_days > max_days:
            min_days, max_days = max_days, min_days
        range_size = max_days - min_days + 1

        col = df[column]
        if pd.api.types.is_extension_array_dtype(col.dtype):
            col = col.astype(object)
        fmt = cfg.get("date_format") or _detect_format(col)

        # HC-3(a): group_by anchors the digest input to a sibling entity
        # column instead of the date value. Existence of `group_by` in the
        # same table is a plan-compile check (check_date_shift_group_by_refs);
        # execution-time reads it directly, same convention as
        # windowed_date's `anchor` (compile-time is the validation layer).
        group_by = cfg.get("group_by")
        group_col = df[group_by] if group_by else None
        if group_col is not None and pd.api.types.is_extension_array_dtype(group_col.dtype):
            group_col = group_col.astype(object)

        parsed = pd.to_datetime(col, format=fmt, errors="coerce")
        unusable = parsed.isna().to_numpy()  # null source OR unparseable date
        source_null = col.isna().to_numpy()

        shifts: list[int] = []
        for i, value in enumerate(col):
            if unusable[i]:
                shifts.append(0)
                continue
            if group_col is not None:
                group_value = group_col.iloc[i]
                # Null group value: self-anchor on this row's own date
                # value instead (see module docstring's null policy).
                anchor = value if pd.isna(group_value) else group_value
            else:
                anchor = value
            digest = derive(ctx.mask_key, plan.namespace, _canonicalize_source(anchor))
            shifts.append(min_days + (int.from_bytes(digest[:8], "big") % range_size))

        shifted = parsed + pd.to_timedelta(shifts, unit="D")
        formatted = shifted.dt.strftime(fmt) if fmt else shifted.astype(str)

        # Sprint 2 honesty pack (D7): split `unusable` into source-null (keep
        # original, no error -- unchanged behavior) vs non-null-unparseable
        # (a per-row format error; the original value is still LEFT in the
        # frame per trap T4, the pipeline-level rule guarantees it never
        # reaches the main output).
        out = [col.iloc[i] if unusable[i] else formatted.iloc[i] for i in range(len(col))]
        for i in range(len(col)):
            if unusable[i] and not source_null[i]:
                ctx.row_errors.append(
                    RowError(
                        column=column,
                        row_index=i,
                        trigger="format_error",
                        reason="value is not a parseable date under date_shift",
                    )
                )
        df[column] = out
        return df, []
