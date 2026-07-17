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

Anchor source (Codex R1 P1 #1): the anchor value is read from a PRE-MASK
snapshot threaded through `ctx.group_anchor_snapshots[(table, group_by)]`,
NOT from the live frame. Reading `df[group_by]` directly was wrong: if the
group column masks before this node (node order is lexicographic) or is
when-gated so only some of an entity's rows are rewritten, two rows of one
patient would anchor on different values (one masked, one raw) and shift
apart -- exactly the interval the feature preserves. Each adapter copies the
column pre-mask; the same requirement (immutable source value + lossless
int-with-null typing) as an FK parent key, so the same `fk_columns_for_table`
/ snapshot machinery carries it. On every SUPPORTED path a date_shift+group_by
column masks the FULL table frame (`when`, FK-participation, and `nested` are
all compile-rejected for group_by), so the snapshot -- built from that same
pre-mask frame -- shares its index exactly. The handler REQUIRES that index
identity and fails closed on any mismatch, rather than silently reindexing an
unexpected (filtered/reordered) frame -- so an unaccounted-for route becomes a
loud fail-closed error, never a silent mis-anchor. FAIL CLOSED likewise if a
configured `group_by` has no snapshot, rather than falling back to the live
(mutable) frame.

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
        # column read from the PRE-MASK snapshot in ctx, not the live frame
        # (see module docstring "Anchor source"). Existence + a non-empty
        # string ref are plan-compile checks (check_date_shift_group_by_refs).
        group_by = cfg.get("group_by")
        anchor_col: pd.Series | None = None
        if group_by:
            snapshot = ctx.group_anchor_snapshots.get((ctx.current_table, group_by))
            if snapshot is None:
                raise StrategyError(
                    code="date_shift_group_anchor_snapshot_missing",
                    strategy="date_shift",
                    message=(
                        f"column {column!r} uses date_shift group_by {group_by!r} "
                        "but no pre-mask anchor snapshot was provided for it; the "
                        "run cannot anchor the shift deterministically. This is an "
                        "adapter wiring error, not a config error."
                    ),
                )
            # The snapshot must line up ROW-FOR-ROW with the frame this handler
            # sees. On every SUPPORTED path a date_shift+group_by column gets the
            # full table frame -- a `when` gate, an FK-participating column, and a
            # nested child are ALL compile-rejected for group_by -- so the snapshot,
            # built from that same pre-mask frame, has an identical index. Require
            # that identity and FAIL CLOSED otherwise instead of silently
            # reindexing: a future/unknown route that handed a filtered or
            # reordered frame (a fresh RangeIndex) would otherwise mis-anchor
            # UNDETECTABLY -- the exact wrong-output class this snapshot exists to
            # prevent (Dennis R2 LOW-1). This backstops any execution route not yet
            # accounted for at the handler, turning a silent-wrong-output into a
            # loud fail-closed raise.
            if not snapshot.index.equals(col.index):
                raise StrategyError(
                    code="date_shift_group_anchor_snapshot_misaligned",
                    strategy="date_shift",
                    message=(
                        f"column {column!r} date_shift group_by {group_by!r}: the "
                        "pre-mask anchor snapshot does not row-align with the frame "
                        "being masked (indexes differ). Anchoring would be wrong, so "
                        "the run fails closed. This indicates an execution route not "
                        "yet supported for group_by, not a config error."
                    ),
                )
            anchor_col = snapshot
            if pd.api.types.is_extension_array_dtype(anchor_col.dtype):
                anchor_col = anchor_col.astype(object)

        parsed = pd.to_datetime(col, format=fmt, errors="coerce")
        unusable = parsed.isna().to_numpy()  # null source OR unparseable date
        source_null = col.isna().to_numpy()

        shifts: list[int] = []
        for i, value in enumerate(col):
            if unusable[i]:
                shifts.append(0)
                continue
            if anchor_col is not None:
                group_value = anchor_col.iloc[i]
                if pd.isna(group_value):
                    # Null group value: self-anchor on this row's own date
                    # value instead (see module docstring's null policy).
                    anchor = value
                else:
                    # Normalize a numpy scalar (e.g. `numpy.bool_`/`numpy.int64`
                    # from a null-free numpy-backed chunk) to its Python
                    # equivalent, so the canonical digest input is identical
                    # whether the anchor column materialized as a numpy dtype or
                    # a Python-scalar object/extension/polars dtype. Without this
                    # the SAME anchor value hashes differently across a chunk
                    # boundary (null-free chunk vs nullable full frame) and across
                    # substrates (`numpy.bool_(True)` -> b"True" vs `True` ->
                    # b"\x01"), breaking the deterministic parity guarantee
                    # (Codex R3 P1). Python scalars have no `.item()` and pass
                    # through unchanged.
                    anchor = group_value.item() if hasattr(group_value, "item") else group_value
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
