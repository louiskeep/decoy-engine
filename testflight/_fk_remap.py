"""FK integrity and remap-mask invariants for the test-flight suite.

Extracted from _invariants.py (LOW-4 / Phase 4 module split) to keep each
module under the 600-line limit.  Re-exported from _invariants.py so all
existing callers continue to work unchanged.

Functions:
  check_remap_masks_orphan         -- per-orphan value-change assertion
  check_value_changing_not_passthrough -- column-level value-set change check
  _check_remap_not_passthrough     -- inner helper; finds orphans via source frames
  check_fk_integrity               -- suite-facing FK integrity + orphan-count check
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pyarrow as pa

from ._spec import FKIntegritySpec, RelationshipSpec

# Strategies that must change every source value they touch.  An FPE / hash /
# code_set column whose output value-set equals the source value-set is a
# silent no-op (e.g. FPE with a charset that does not cover the data).
#
# Aligned with docs/what-we-cannot-prove.md (the carry-forward items that the
# bijective-relabelling limitation affects).  Candidates not listed here
# (shuffle, categorical, derived) have their own correctness teeth.
_VALUE_CHANGING_STRATEGIES: frozenset[str] = frozenset({"fpe", "hash", "code_set"})

# Maximum tolerated mean positional-character retention for an FPE column.
# FPE is format-preserving: output has the same length as the source, so a
# character-position comparison is meaningful.  A correctly-charset'd FPE
# permutes (nearly) every in-charset character, so the mean fraction of
# in-position identical characters is low (empirically <=0.23 for the
# test-flight identity columns).  The canonical charset-undercoverage bug
# (charset=alphanum applied to uppercase-containing values) leaves every
# out-of-charset character in place: the whole value still differs by the one
# permuted character, so the value-set check above passes, but the mean
# positional retention is high (empirically ~0.65 for the uppercase-MRN no-op).
# The 0.5 floor sits in the wide gap between the two regimes.
_FPE_MAX_POSITIONAL_RETENTION: float = 0.5

# Minimum comparable (equal-length, non-null) rows before the positional-
# retention check is statistically meaningful.  Below this the check is skipped
# to avoid noise on tiny or ragged columns.
_FPE_RETENTION_MIN_ROWS: int = 20


def _fpe_positional_retention(source_vals: pd.Series, output_vals: pd.Series) -> tuple[float, int]:
    """Return (mean in-position character retention, comparable row count).

    For each aligned (source, output) pair that is non-null and equal-length,
    compute the fraction of character positions holding an identical character,
    then average across rows.  Rows with differing lengths or nulls are not
    comparable (FPE preserves length, so a length change is a different signal)
    and are excluded from both the numerator and the denominator.
    """
    total = 0.0
    n = 0
    for src_val, out_val in zip(source_vals, output_vals, strict=False):
        if src_val is None or out_val is None:
            continue
        s = str(src_val)
        o = str(out_val)
        if len(s) == 0 or len(s) != len(o):
            continue
        same = sum(1 for a, b in zip(s, o, strict=True) if a == b) / len(s)
        total += same
        n += 1
    return (total / n if n else 0.0), n


# ---------------------------------------------------------------------------
# check_remap_masks_orphan
# ---------------------------------------------------------------------------


def check_remap_masks_orphan(
    job_name: str,
    orphan_source_key: Any,
    orphan_output_val: Any,
    child_table: str,
    child_col: str,
) -> None:
    """Assert that a remapped orphan FK value differs from its source key.

    When orphan_policy=remap, the engine re-applies the parent column's masking
    strategy to the orphan source key.  For FPE with an in-charset key this
    produces a permuted value that differs from the input.  Equality means the
    strategy left the key on passthrough (out-of-charset no-op), which is a
    privacy gap: the orphan key is emitted verbatim in the output.

    This check is the invariant tooth that enforces "remap genuinely masked it."
    It can only run when the caller supplies both the source orphan key and the
    corresponding output value; callers that have only output frames should call
    check_fk_integrity with source_frames to wire this automatically.

    Args:
        job_name: Job name for error messages.
        orphan_source_key: The FK value in the source that had no parent match.
        orphan_output_val: The value that appears in the masked output for that row.
        child_table: Child table name (for error messages).
        child_col: Child FK column name (for error messages).

    Raises:
        AssertionError: If orphan_output_val equals orphan_source_key.
    """
    assert orphan_output_val != orphan_source_key, (
        f"[{job_name}] fk_integrity remap-masks: "
        f"{child_table}.{child_col} orphan source key {orphan_source_key!r} "
        f"equals its output value {orphan_output_val!r}. "
        f"orphan_policy=remap must produce an output value that differs from "
        f"the source key; equality means the masking strategy left it on "
        f"passthrough. An all-out-of-charset key is now covered by the covering "
        f"hash under preserve_separators=True (fix #42); the residual cases are a "
        f"partial-out-of-charset key or =False (see docs/what-we-cannot-prove.md)."
    )


# ---------------------------------------------------------------------------
# check_value_changing_not_passthrough
# ---------------------------------------------------------------------------


def check_value_changing_not_passthrough(
    job_name: str,
    table: str,
    column: str,
    strategy: str,
    source_df: pd.DataFrame,
    output_df: pd.DataFrame,
) -> None:
    """Assert that a value-changing-masked column's output value-set differs from source.

    A value-changing strategy (fpe, hash, code_set) must produce output values
    that differ from the source values at the column level.  If the output
    value-set equals the source value-set, the strategy is a silent no-op: the
    mask left the column on passthrough.

    The canonical bug this catches is FPE configured with a charset that does not
    cover the data's actual characters.  FPE extracts in-charset characters,
    permutes them, and writes them back; if no characters are in-charset, every
    value is returned verbatim.  A column with charset:alphanum (lowercase) applied
    to uppercase-only values (e.g. "EL", "HI") passes through unchanged.

    Two checks (TH-1.1 / P0-1):

    1. Column-level set check (all value-changing strategies).  A set equality
       means NO value was changed at all -- a complete no-op.  A single changed
       value passes this check.

    2. Positional character-retention check (FPE only).  The set check above is
       blind to a PARTIAL passthrough: a charset that covers only some of the
       data's characters (e.g. charset=alphanum on an uppercase-plus-digit MRN)
       permutes one character while emitting the rest verbatim, so every whole
       value differs (set check passes) yet most characters leak in place.  FPE
       is format-preserving, so a per-position character comparison detects this:
       the mean fraction of in-position identical characters must stay below
       _FPE_MAX_POSITIONAL_RETENTION.  This is the exact members.mrn
       (charset=alphanum) live bug.

    Only applied when strategy is in _VALUE_CHANGING_STRATEGIES.  Other strategies
    (passthrough, categorical, derived, geo_generalize, date_shift, text_redact)
    have their own correctness teeth and are not subject to this check.

    Args:
        job_name: Job name for error messages.
        table: Table name for error messages.
        column: Column name to check.
        strategy: Masking strategy declared for this column.
        source_df: Pre-mask pandas DataFrame.
        output_df: Post-mask pandas DataFrame.

    Raises:
        AssertionError: If output value-set equals source value-set (complete
            no-op), or if an FPE column retains too many source characters in
            position (partial passthrough / charset undercoverage).
    """
    if strategy not in _VALUE_CHANGING_STRATEGIES:
        return
    if column not in source_df.columns or column not in output_df.columns:
        return  # Column-presence is checked by check_correlation_through_masking.
    src_vals = set(source_df[column].dropna().unique())
    out_vals = set(output_df[column].dropna().unique())
    assert src_vals != out_vals, (
        f"[{job_name}/{table}] value-changing-mask passthrough: column {column!r} "
        f"strategy={strategy!r} output value-set equals source value-set. "
        f"The mask is a no-op (e.g. FPE charset does not cover the data characters). "
        f"A {strategy!r} column must produce at least one changed value. "
        f"Source values (up to 10): {sorted(str(v) for v in src_vals)[:10]}. "
        f"Declare charset:ALPHANUM for uppercase data, charset:alpha for lowercase, "
        f"or charset:digits for numeric data."
    )

    # Partial-passthrough guard (FPE only).  The value-set check above only
    # catches a COMPLETE no-op; a charset that covers *some* of the data (e.g.
    # charset=alphanum masking the digits of an uppercase-plus-digit MRN)
    # permutes one character while leaving the rest in place, so every whole
    # value differs and the set check passes even though most characters leak
    # verbatim.  FPE is format-preserving, so a positional character comparison
    # detects this: a correctly-charset'd FPE retains few in-position
    # characters; the undercovered-charset bug retains most of them.
    if strategy == "fpe":
        mean_retention, n_comparable = _fpe_positional_retention(
            source_df[column], output_df[column]
        )
        if n_comparable >= _FPE_RETENTION_MIN_ROWS:
            assert mean_retention < _FPE_MAX_POSITIONAL_RETENTION, (
                f"[{job_name}/{table}] fpe partial-passthrough: column {column!r} "
                f"retains {mean_retention:.1%} of source characters in position "
                f"(> {_FPE_MAX_POSITIONAL_RETENTION:.0%} floor) across "
                f"{n_comparable} rows.  The configured FPE charset does not cover "
                f"the data's characters, so out-of-charset characters are emitted "
                f"verbatim (only in-charset characters are permuted).  Declare "
                f"charset:ALPHANUM to cover uppercase+digits, or the specific "
                f"charset that spans every character class present in {column!r}."
            )


# ---------------------------------------------------------------------------
# _check_remap_not_passthrough (inner helper)
# ---------------------------------------------------------------------------


def _check_remap_not_passthrough(
    job_name: str,
    parent_table: str,
    parent_cols: list[str],
    child_table: str,
    child_cols: list[str],
    source_frames: dict[str, pa.Table],
    child_out: pa.Table,
) -> None:
    """Inner check: for each orphan row (source child key not in SOURCE parent pool),
    verify that the output child FK value differs from the source key.

    Orphan rows are identified by matching the SOURCE child FK values against
    the SOURCE parent key pool (not the masked pools).  Rows not in the source
    parent pool are orphans; their output FK values are what the remap policy
    produced.  Equality means passthrough (no-op), which is the gap documented
    in what-we-cannot-prove.md.

    Only single-column FKs are checked here; composite-FK remap checks are
    left for future extension if needed.
    """
    if len(child_cols) != 1 or len(parent_cols) != 1:
        return  # Composite FK remap check not yet implemented.

    child_col = child_cols[0]
    parent_col = parent_cols[0]

    src_child = source_frames.get(child_table)
    src_parent = source_frames.get(parent_table)
    if src_child is None or src_parent is None:
        return  # Source frames not provided for this table.

    # Build source parent key pool.
    src_parent_keys: set[Any] = set(src_parent.column(parent_col).to_pylist())

    src_child_vals = src_child.column(child_col).to_pylist()
    out_child_vals = child_out.column(child_col).to_pylist()

    for src_val, out_val in zip(src_child_vals, out_child_vals, strict=True):
        if src_val is None:
            continue
        if src_val in src_parent_keys:
            continue  # Normal FK -- not an orphan.
        # This row is an orphan (source key not in source parent pool).
        check_remap_masks_orphan(job_name, src_val, out_val, child_table, child_col)


# ---------------------------------------------------------------------------
# check_fk_integrity
# ---------------------------------------------------------------------------


def check_fk_integrity(
    job_name: str,
    spec: list[FKIntegritySpec],
    result: Any,
    relationships: list[RelationshipSpec],
    source_frames: dict[str, pa.Table] | None = None,
) -> None:
    """Assert FK integrity for all declared relationships.

    For each FKIntegritySpec:
    - Looks up the relationship by spec.relationship_name (matching the
      RelationshipSpec.namespace field).
    - Performs a direct set-membership assertion: every non-null child FK
      value must exist in the parent's masked output key set (belt-and-
      suspenders on top of the engine's built-in fk_intact / no_orphan_children
      validators).
    - Asserts orphan count == spec.expected_orphans.
    - When policy=remap and source_frames is supplied, also calls
      check_remap_masks_orphan for each orphan row: the remapped output value
      must differ from the source key (remap genuinely masked it, not passthrough).

    Args:
        job_name: Job name for error messages.
        spec: List of FKIntegritySpec from the manifest invariants.
        result: ExecutionResult (carries outputs and quality_metrics).
        relationships: List of RelationshipSpec from the manifest (used to
            look up parent/child table names and column names by namespace).
        source_frames: Optional dict[table_name, pa.Table] of source frames.
            When provided and a spec entry has policy=remap, the function
            identifies orphan rows in the source and verifies that their output
            values differ from their source keys.

    Raises:
        AssertionError: If orphan count does not match expected or FK is broken,
            or (with source_frames + remap) if any orphan output == source key.
    """
    # Build a namespace -> RelationshipSpec lookup.
    ns_to_rel: dict[str, RelationshipSpec] = {}
    for r in relationships:
        if r.namespace is not None:
            ns_to_rel[r.namespace] = r

    for fk_spec in spec:
        rel_found = ns_to_rel.get(fk_spec.relationship_name)
        assert rel_found is not None, (
            f"[{job_name}] fk_integrity: relationship_name "
            f"'{fk_spec.relationship_name}' not found in manifest relationships. "
            f"Known: {list(ns_to_rel)}"
        )
        rel: RelationshipSpec = rel_found

        parent_table = rel.parent.table
        parent_cols = rel.parent.columns

        # Every declared child table shares the same parent FK check.
        for child_end in rel.children:
            child_table = child_end.table
            child_cols = child_end.columns

            parent_out = result.outputs.get(parent_table)
            child_out = result.outputs.get(child_table)
            assert parent_out is not None, (
                f"[{job_name}] fk_integrity: parent table '{parent_table}' not in result.outputs."
            )
            assert child_out is not None, (
                f"[{job_name}] fk_integrity: child table '{child_table}' not in result.outputs."
            )

            # Build parent key set (tuple for composite, scalar for single).
            if len(parent_cols) == 1:
                parent_keys: set[Any] = set(parent_out.column(parent_cols[0]).to_pylist())
            else:
                parent_keys = set(
                    zip(*[parent_out.column(c).to_pylist() for c in parent_cols], strict=True)
                )

            # Count child orphans.
            if len(child_cols) == 1:
                child_fk_vals = child_out.column(child_cols[0]).to_pylist()
                orphan_count = sum(
                    1 for v in child_fk_vals if v is not None and v not in parent_keys
                )
            else:
                child_fk_vals_multi = list(
                    zip(*[child_out.column(c).to_pylist() for c in child_cols], strict=True)
                )
                orphan_count = sum(
                    1
                    for v in child_fk_vals_multi
                    if any(x is not None for x in v) and v not in parent_keys
                )

            assert orphan_count == fk_spec.expected_orphans, (
                f"[{job_name}] fk_integrity: {parent_table}.{parent_cols} -> "
                f"{child_table}.{child_cols}: "
                f"orphan_count={orphan_count}, expected={fk_spec.expected_orphans}. "
                f"Parent key pool size: {len(parent_keys)}, "
                "child FK value count: "
                f"{len(child_fk_vals if len(child_cols) == 1 else child_fk_vals_multi)}."
            )

            # When policy=remap AND source frames are available, verify that each
            # orphan row's output value differs from its source key.
            if fk_spec.policy == "remap" and source_frames and fk_spec.expected_orphans > 0:
                _check_remap_not_passthrough(
                    job_name,
                    parent_table,
                    parent_cols,
                    child_table,
                    child_cols,
                    source_frames,
                    child_out,
                )
