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

entity_key (`transforms/dates/entity_offset.py`): the per-entity offset in its own derivation
domain. The digest input is `0xFF || b"date-shift-entity/v1" || 0x00 || text(entity)`, which no
canonicalised cell can equal, so a `hash` or `group_key` column on the entity key in the same
namespace cannot reveal the offset (it can for `group_by`). Integer, integral-Decimal and text
keys agree, so one entity keeps one offset across tables whatever the key's storage type. The
anchor is the same pre-mask snapshot `group_by` uses. A null or blank entity falls back to the
per-value shift of the row's own date and is counted (`date_shift_entity_fallback`); an entity
of another type fails the run with a value-free code; a shift that would leave pandas' date range
is a `format_error` row instead of an overflow crash. No native or out-of-core executor runs it.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from decoy_engine import reasons
from decoy_engine.determinism._derive import DeriveContext
from decoy_engine.execution._adapter import (
    StrategyContext,
    StrategyOption,
    option,
    provider_config_to_dict,
)
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._row_errors import RowError
from decoy_engine.generation.pool._canonicalize import _canonicalize_source
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.date_preserve import preserve_set_from_config
from decoy_engine.transforms.date_shift import _detect_format
from decoy_engine.transforms.dates.entity_offset import (
    Anchor,
    AnchorDomain,
    EntityKeyFault,
    FaultKind,
    UnsupportedEntityValue,
    date_shift_anchor,
    entity_key_present,
    entity_source,
    parse_entity_key,
)

# A raw plan (no compile) can still carry a bad entity_key; a combination that would drop the
# anchor gets its own code, every other fault the generic config code.
_ENTITY_FAULT_RUNTIME_CODE: dict[FaultKind, str] = {
    FaultKind.KEY_INVALID: reasons.runtime.DATE_SHIFT_INVALID_CONFIG,
    FaultKind.BOUND_INVALID: reasons.runtime.DATE_SHIFT_INVALID_CONFIG,
    FaultKind.WITH_GROUP_BY: reasons.runtime.DATE_SHIFT_ENTITY_KEY_UNSUPPORTED_COMBINATION,
    FaultKind.WITH_PRESERVE_VALUES: reasons.runtime.DATE_SHIFT_ENTITY_KEY_UNSUPPORTED_COMBINATION,
}
_SECONDS_PER_UNIT = {"s": 1, "ms": 10**3, "us": 10**6, "ns": 10**9}
_INT64_MIN_VALID = -(2**63) + 1  # -2**63 is NaT
_INT64_MAX = 2**63 - 1


class DateShiftStrategyHandler:
    """Shift dates by a deterministic per-value offset within a bounded range.

    Optional `group_by` (HC-3a): anchor the offset to a sibling entity
    column instead of the date value itself, so every date belonging to
    the same entity shifts by one consistent offset. See module docstring.
    """

    name: str = "date_shift"
    namespace_required: bool = True
    join_safe: bool = False
    wizard_options: tuple[StrategyOption, ...] = (
        option("min_days", "int", -30),
        option("max_days", "int", 30),
        option("date_format", "str", "%Y-%m-%d"),
    )
    offer_in_wizard: bool = True

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        if plan.namespace is None:
            raise StrategyError(
                code=reasons.runtime.DATE_SHIFT_REQUIRES_NAMESPACE,
                strategy="date_shift",
                message=f"column {column!r} uses date_shift but has no namespace.",
            )
        cfg = provider_config_to_dict(plan.provider_config)
        entity = parse_entity_key(cfg)
        if isinstance(entity, EntityKeyFault):
            raise StrategyError(
                code=_ENTITY_FAULT_RUNTIME_CODE[entity.kind],
                strategy="date_shift",
                message=(
                    f"date_shift column {column!r}: invalid entity_key configuration "
                    f"(option {entity.option!r})."
                ),
            )
        min_days = int(cfg.get("min_days", -365))
        max_days = int(cfg.get("max_days", 365))
        if min_days > max_days:
            min_days, max_days = max_days, min_days
        range_size = max_days - min_days + 1
        try:
            preserve = preserve_set_from_config(cfg)
        except ValueError as exc:
            raise StrategyError(
                code=reasons.runtime.DATE_SHIFT_INVALID_CONFIG,
                strategy="date_shift",
                message=f"date_shift column {column!r}: {exc}",
            ) from exc
        anchor = date_shift_anchor(cfg)
        if preserve is not None and anchor is not None:
            raise StrategyError(
                code=reasons.runtime.DATE_PRESERVE_VALUES_WITH_GROUP_BY_UNSUPPORTED,
                strategy="date_shift",
                message=(
                    f"date_shift column {column!r} sets both preserve_values and group_by. "
                    "The entity anchor must row-align with the frame being shifted, which "
                    "excluding sentinel rows breaks. Remove one of the two."
                ),
            )

        col = df[column]
        if pd.api.types.is_extension_array_dtype(col.dtype):
            col = col.astype(object)
        fmt = cfg.get("date_format") or _detect_format(col)

        # The anchor (group_by or entity_key) is read from the PRE-MASK snapshot in ctx, not the
        # live frame (see module docstring "Anchor source"). Existence + a non-empty string ref
        # are plan-compile checks.
        anchor_col: pd.Series | None = None
        if anchor is not None:
            anchor_col = _anchor_snapshot(ctx, column, anchor, col.index)

        # group_by and entity_key are not native; admission declines them, so the seam books the
        # row and returns None (reporting the honest python_scalar backend).
        seq = getattr(ctx, "seq_native", None)
        if seq is not None:
            native_out = seq.date_shift_column(
                ctx,
                df,
                column,
                namespace=plan.namespace,
                mask_key=ctx.mask_key,
                min_days=min_days,
                max_days=max_days,
                date_format=fmt,
                preserve=preserve,
            )
            if native_out is not None:
                if entity_key_present(cfg):
                    # The native gate declines every entity_key column; output here would carry
                    # per-value offsets and silently break every entity's intervals.
                    raise StrategyError(
                        code=reasons.runtime.DATE_SHIFT_ENTITY_KEY_NOT_NATIVE,
                        strategy="date_shift",
                        message=(
                            f"date_shift column {column!r}: engine error, a native executor "
                            "returned output for an entity_key column, which only the Python "
                            "handler may run. The output is refused; the configuration is not "
                            "at fault."
                        ),
                    )
                df[column] = native_out
                return df, []

        if anchor is not None and anchor.domain is AnchorDomain.ENTITY and anchor_col is not None:
            out, error_rows, overflow_rows = _shift_entity_rows(
                col, fmt, anchor_col, ctx, plan.namespace, min_days, range_size, column
            )
            _record_format_errors(ctx, column, error_rows)
            for i in overflow_rows:
                ctx.row_errors.append(
                    RowError(
                        column=column,
                        row_index=int(i),
                        trigger="format_error",
                        reason="shifted date is outside the supported date range",
                    )
                )
            df[column] = out
            return df, []

        preserved = preserve.mask(col) if preserve is not None else None
        if preserve is None or preserved is None or not preserved.any():
            out, error_rows = _shift_rows(
                col, fmt, anchor_col, ctx, plan.namespace, min_days, range_size
            )
        else:
            # Sentinel rows never reach parsing, derivation or the whole-column strftime: the
            # operator runs on the remainder alone and the sentinel text is scattered back.
            rest = np.flatnonzero(~preserved)
            cells: list[Any] = [None] * len(col)
            error_rows = np.empty(0, dtype=np.intp)
            if rest.size:
                sub_out, sub_errors = _shift_rows(
                    col.iloc[rest], fmt, None, ctx, plan.namespace, min_days, range_size
                )
                for position, value in zip(rest, sub_out, strict=True):
                    cells[position] = value
                error_rows = rest[sub_errors]
            for position, text in zip(
                np.flatnonzero(preserved), preserve.render(col, preserved), strict=True
            ):
                cells[position] = text
            out = cells
            ctx.record_date_preserved(column, int(preserved.sum()))
        _record_format_errors(ctx, column, error_rows)
        df[column] = out
        return df, []


def _record_format_errors(ctx: StrategyContext, column: str, error_rows: Any) -> None:
    for i in error_rows:
        ctx.row_errors.append(
            RowError(
                column=column,
                row_index=int(i),
                trigger="format_error",
                reason="value is not a parseable date under date_shift",
            )
        )


def _anchor_snapshot(
    ctx: StrategyContext, column: str, anchor: Anchor, index: pd.Index
) -> pd.Series:
    """The pre-mask anchor column, row-aligned with the frame being masked, or fail closed."""
    snapshot = ctx.group_anchor_snapshots.get((ctx.current_table, anchor.column))
    if snapshot is None:
        raise StrategyError(
            code=reasons.runtime.DATE_SHIFT_GROUP_ANCHOR_SNAPSHOT_MISSING,
            strategy="date_shift",
            message=(
                f"column {column!r} uses date_shift {anchor.option} {anchor.column!r} "
                "but no pre-mask anchor snapshot was provided for it; the "
                "run cannot anchor the shift deterministically. This is an "
                "adapter wiring error, not a config error."
            ),
        )
    # The snapshot must line up ROW-FOR-ROW with the frame this handler
    # sees. On every SUPPORTED path an anchored date_shift column gets the
    # full table frame -- a `when` gate, an FK-participating column, and a
    # nested child are ALL compile-rejected for an anchor -- so the snapshot,
    # built from that same pre-mask frame, has an identical index. Require
    # that identity and FAIL CLOSED otherwise instead of silently
    # reindexing: a future/unknown route that handed a filtered or
    # reordered frame (a fresh RangeIndex) would otherwise mis-anchor
    # UNDETECTABLY.
    if not snapshot.index.equals(index):
        raise StrategyError(
            code=reasons.runtime.DATE_SHIFT_GROUP_ANCHOR_SNAPSHOT_MISALIGNED,
            strategy="date_shift",
            message=(
                f"column {column!r} date_shift {anchor.option} {anchor.column!r}: the "
                "pre-mask anchor snapshot does not row-align with the frame "
                "being masked (indexes differ). Anchoring would be wrong, so "
                "the run fails closed. This indicates an execution route not "
                f"yet supported for {anchor.option}, not a config error."
            ),
        )
    if pd.api.types.is_extension_array_dtype(snapshot.dtype):
        snapshot = snapshot.astype(object)
    return snapshot


def _shift_rows(
    col: pd.Series,
    fmt: str | None,
    anchor_col: pd.Series | None,
    ctx: StrategyContext,
    namespace: str,
    min_days: int,
    range_size: int,
) -> tuple[list[Any], np.ndarray[Any, Any]]:
    """Shift every parseable date of ``col``; return ``(cells, format_error_positions)``.

    Positions are within ``col``, so a caller that passed a subset rebases them.
    """
    parsed = pd.to_datetime(col, format=fmt, errors="coerce")
    unusable = parsed.isna().to_numpy()  # null source OR unparseable date
    source_null = col.isna().to_numpy()

    # Canonical anchor bytes for usable rows only, in row order, so the
    # batched derivation sees the same sources the old per-row loop did.
    # `list(col)` (not `col.iloc[i]`) keeps the iteration scalars the old
    # loop used, e.g. a Python int rather than numpy.int64 for numeric dtypes.
    values = list(col)
    usable_idx = np.flatnonzero(~unusable)
    sources: list[bytes] = []
    for i in usable_idx:
        value = values[i]
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
                # a Python-scalar object/extension dtype. Without this the
                # SAME anchor value hashes differently across a chunk
                # boundary -- null-free numpy chunk vs nullable full frame
                # (`numpy.bool_(True)` -> b"True" vs `True` -> b"\x01") --
                # breaking the deterministic parity guarantee (Codex R3 P1).
                # Python scalars have no `.item()` and pass
                # through unchanged.
                anchor = group_value.item() if hasattr(group_value, "item") else group_value
        else:
            anchor = value
        sources.append(_canonicalize_source(anchor))

    # One keyed context per column: the HKDF key and the HMAC prefix are
    # constant across rows, so only the per-row finish remains.
    shifts = [0] * len(values)
    if sources:
        digests = DeriveContext.for_column(ctx.mask_key, namespace).derive_sources(
            namespace, sources
        )
        for i, d in zip(usable_idx, digests, strict=True):
            shifts[i] = min_days + (int.from_bytes(d[:8], "big") % range_size)

    out = _render(col, parsed, shifts, fmt, unusable)
    return out, np.flatnonzero(unusable & ~source_null)


def _render(
    col: pd.Series,
    parsed: pd.Series,
    shifts: list[int],
    fmt: str | None,
    unusable: np.ndarray[Any, Any],
) -> list[Any]:
    shifted = parsed + pd.to_timedelta(shifts, unit="D")
    formatted = shifted.dt.strftime(fmt) if fmt else shifted.astype(str)

    # Sprint 2 honesty pack (D7): split `unusable` into source-null (keep
    # original, no error -- unchanged behavior) vs non-null-unparseable
    # (a per-row format error; the original value is still LEFT in the
    # frame per trap T4, the pipeline-level rule guarantees it never
    # reaches the main output).
    #
    # Assemble as a Python list: pandas infers the column dtype from the
    # exact scalar objects, so unusable cells must be the very objects
    # `col.iloc[i]` returns (a boxed or np.where'd source changes the
    # dtype of all-null float32/float16, None, NaT and NA-only columns).
    out: list[Any] = formatted.to_numpy(dtype=object).tolist()
    for i in np.flatnonzero(unusable):
        out[i] = col.iloc[i]
    return out


def _shift_entity_rows(
    col: pd.Series,
    fmt: str | None,
    anchor_col: pd.Series,
    ctx: StrategyContext,
    namespace: str,
    min_days: int,
    range_size: int,
    column: str,
) -> tuple[list[Any], np.ndarray[Any, Any], np.ndarray[Any, Any]]:
    """Shift every parseable date by its entity's offset.

    Returns ``(cells, format_error_positions, overflow_positions)``. A row with no entity (null or
    blank key) takes the plain per-value shift of its own date and is counted as a fallback.
    """
    parsed = pd.to_datetime(col, format=fmt, errors="coerce")
    unusable = parsed.isna().to_numpy()
    source_null = col.isna().to_numpy()
    values = list(col)
    usable_idx = np.flatnonzero(~unusable)
    sources: list[bytes] = []
    fallback = 0
    unsupported: str | None = None
    for i in usable_idx:
        src = entity_source(anchor_col.iloc[i])
        if isinstance(src, UnsupportedEntityValue):
            unsupported = src.type_name
            break
        if src is None:
            fallback += 1
            src = _canonicalize_source(values[i])
        sources.append(src)
    if unsupported is not None:
        raise StrategyError(
            code=reasons.runtime.DATE_SHIFT_ENTITY_KEY_UNSUPPORTED_VALUE,
            strategy="date_shift",
            message=(
                f"date_shift column {column!r} in table {ctx.current_table!r}: an entity_key "
                f"value of type {unsupported} has no stable text form. Use integer, string, "
                "decimal, UUID or date entity keys."
            ),
        )
    if fallback:
        ctx.record_date_shift_entity_fallback(column, fallback)

    shifts = [0] * len(values)
    if sources:
        digests = DeriveContext.for_column(ctx.mask_key, namespace).derive_sources(
            namespace, sources
        )
        for i, d in zip(usable_idx, digests, strict=True):
            shifts[i] = min_days + (int.from_bytes(d[:8], "big") % range_size)
    overflow = _overflow_positions(parsed, shifts, usable_idx)
    for i in overflow:
        shifts[i] = 0
    unusable[overflow] = True
    out = _render(col, parsed, shifts, fmt, unusable)
    errors = np.flatnonzero(parsed.isna().to_numpy() & ~source_null)
    return out, errors, overflow


def _overflow_positions(
    parsed: pd.Series, shifts: list[int], positions: np.ndarray[Any, Any]
) -> np.ndarray[Any, Any]:
    """Positions whose shifted value would leave pandas' Timestamp range.

    Computed in the unit the sum is taken in (the finer of the parsed Series' unit and the
    day timedelta's), so a non-nanosecond Series is bounded correctly; NaT rows are not in
    ``positions``.
    """
    if positions.size == 0 or not pd.api.types.is_datetime64_any_dtype(parsed):
        return np.empty(0, dtype=np.intp)
    unit = str(parsed.dt.unit)
    td_unit = str(pd.to_timedelta([0], unit="D").unit)
    result_unit = unit if _SECONDS_PER_UNIT[unit] >= _SECONDS_PER_UNIT[td_unit] else td_unit
    widen = _SECONDS_PER_UNIT[result_unit] // _SECONDS_PER_UNIT[unit]
    per_day = 86_400 * _SECONDS_PER_UNIT[result_unit]
    raw = parsed.array.asi8
    bad = [
        i
        for i in positions
        if not _INT64_MIN_VALID <= int(raw[i]) * widen + shifts[i] * per_day <= _INT64_MAX
    ]
    return np.asarray(bad, dtype=np.intp)
