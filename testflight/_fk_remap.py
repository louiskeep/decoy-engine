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

from typing import Any, NamedTuple

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

# Maximum tolerated PER-POSITION across-row character retention for an FPE
# column.  FPE is format-preserving: output has the same length as the source,
# so a per-character-position comparison across rows is well defined.  For each
# position we measure identical_fraction = the fraction of rows whose output
# character equals the source character at that position.  A genuinely-permuting
# FPE over an alphabet of size k retains ~1/k per position (empirically <=0.13
# for every correctly-masked test-flight identity column, including digit
# columns where 1/k=0.1); a position emitted VERBATIM (charset undercoverage)
# sits far higher -- at 1.0 for a structurally out-of-charset position, or at
# P(out-of-charset character) for a position that is out-of-charset only some of
# the time (empirically ~0.56-0.70 for the uppercase-MRN no-op under alphanum).
#
# TH-1: this REPLACES an earlier MEAN-over-positions statistic.  The mean
# dilutes a leak confined to a few positions: dennis's concrete false negative
# is an "AB123456" column (2 per-subject-varying uppercase + 6 digits) masked
# with charset:alphanum -- the 2 uppercase leak verbatim (per-position ~1.0)
# while the 6 digits permute (~0.1), so the mean is ~0.33 and shipped GREEN even
# though two informative PII characters leak per subject.  Testing EACH position
# independently catches the narrow leak the mean misses, at ANY value width,
# while still catching the diffuse uppercase-MRN leak (its per-position maxima
# ~0.70 are well above this floor).
#
# The 0.5 floor sits in the wide empirical gap between correctly-masked
# positions (<=0.13) and any leaked position (>=0.56 diffuse, ~1.0 verbatim).
# It is intentionally below dennis's illustrative 0.95: a fixed verbatim leak
# does sit at ~1.0, but the live uppercase-MRN leak is per-row-probabilistic and
# caps near 0.70, so a 0.95 cut would regress the existing members.mrn
# detection.  0.5 catches both regimes with >=3.8x margin over the genuine
# baseline.
_FPE_MAX_POSITIONAL_RETENTION: float = 0.5

# Minimum SOURCE-alphabet size (distinct source characters at a position, across
# the compared rows) for a position to be treated as informative.  A position
# whose source takes very few distinct values is low-entropy structure, not a
# subject-identifying character, and a correct mask may legitimately fix it: the
# NPI leading digit is always 1 or 2 (k=2) and a checksum-aware FPE preserves it
# (identical_fraction ~1.0) -- that is format, not a leak, so flagging it would
# be a false positive.  Requiring k>=4 excludes such positions while keeping
# every real leaked position eligible (digit leaks have k=10, uppercase leaks
# k=26, mixed alphanumeric k=36) and keeping the genuine self-map baseline 1/k
# (<=0.25 at k=4) safely under the 0.5 floor.
_FPE_MIN_INFORMATIVE_ALPHABET: int = 4

# Minimum comparable (equal-length, non-null) rows before the positional-
# retention check is statistically meaningful.  Below this the check is SKIPPED
# with an explicit status (LOW-1: a silent pass on a small table would let the
# privacy floor evaporate) rather than silently passing.
_FPE_RETENTION_MIN_ROWS: int = 20


class _PositionalLeak(NamedTuple):
    """Result of the per-position FPE leak scan.

    ``worst_fraction`` is ``None`` when no informative position was evaluated;
    ``max_group_rows`` and ``n_informative`` then tell the caller WHY (too few
    comparable rows vs. no informative position) so the SKIP status is accurate.
    """

    worst_fraction: float | None
    worst_position: int
    worst_k: int
    n_comparable: int
    n_informative: int
    n_low_entropy: int
    max_group_rows: int


def _fpe_positional_leak(source_vals: pd.Series, output_vals: pd.Series) -> _PositionalLeak:
    """Detect a per-position verbatim leak across rows of an FPE column.

    Rows are grouped by (equal) length -- FPE preserves length, so a per-position
    comparison is only aligned within a length group.  For every position in a
    length group with at least ``_FPE_RETENTION_MIN_ROWS`` rows, compute
    ``identical_fraction`` (fraction of rows whose output character equals the
    source character at that position) and ``k`` (number of distinct SOURCE
    characters at that position).  A position is INFORMATIVE when
    ``k >= _FPE_MIN_INFORMATIVE_ALPHABET``.  The worst informative position (the
    one with the highest identical_fraction) is reported.

    ``worst_fraction`` is ``None`` when no informative position could be
    evaluated -- either because no length group reached the row floor
    (``max_group_rows < _FPE_RETENTION_MIN_ROWS``) or because every position was
    low-entropy structure (``n_informative == 0``, e.g. a one-character flag).
    The caller distinguishes these to emit an accurate SKIP status (LOW-1).
    """
    from collections import defaultdict

    groups: dict[int, list[tuple[str, str]]] = defaultdict(list)
    n_comparable = 0
    for src_val, out_val in zip(source_vals, output_vals, strict=False):
        if src_val is None or out_val is None:
            continue
        s = str(src_val)
        o = str(out_val)
        if len(s) == 0 or len(s) != len(o):
            continue
        groups[len(s)].append((s, o))
        n_comparable += 1

    worst_fraction: float | None = None
    worst_position = -1
    worst_k = 0
    n_informative = 0
    n_low_entropy = 0
    max_group_rows = 0
    for length, rows in groups.items():
        n = len(rows)
        max_group_rows = max(max_group_rows, n)
        if n < _FPE_RETENTION_MIN_ROWS:
            continue
        for pos in range(length):
            k = len({s[pos] for s, _ in rows})
            if k < _FPE_MIN_INFORMATIVE_ALPHABET:
                # Excluded: at low k, a legitimately-preserved structural char
                # (e.g. NPI leading digit) is indistinguishable from a leak by
                # retention fraction alone. Counted + disclosed, never silently
                # dropped (see what-we-cannot-prove.md).
                n_low_entropy += 1
                continue
            n_informative += 1
            frac = sum(1 for s, o in rows if s[pos] == o[pos]) / n
            if worst_fraction is None or frac > worst_fraction:
                worst_fraction = frac
                worst_position = pos
                worst_k = k
    return _PositionalLeak(
        worst_fraction,
        worst_position,
        worst_k,
        n_comparable,
        n_informative,
        n_low_entropy,
        max_group_rows,
    )


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
        f"passthrough. DE-01 (2026-07-14): an all-out-of-charset key now FAILS "
        f"CLOSED under FPE (was the covering hash, fix #42); the residual "
        f"partial-out-of-charset prefix leak is documented in "
        f"docs/discussions/2026-07-14-de01-vault-token-for-fpe.md."
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
) -> str:
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

    2. Per-position across-row retention check (FPE only).  The set check above
       is blind to a PARTIAL passthrough: a charset that covers only some of the
       data's characters (e.g. charset=alphanum on an uppercase-plus-digit value)
       permutes the in-charset characters while emitting the out-of-charset ones
       verbatim, so every whole value differs (set check passes) yet informative
       characters leak in place.  FPE is format-preserving, so for each character
       position we measure the across-row fraction of rows whose output character
       equals the source character.  ANY informative position (source alphabet
       >= _FPE_MIN_INFORMATIVE_ALPHABET) whose identical fraction reaches
       _FPE_MAX_POSITIONAL_RETENTION is a verbatim leak at that position.  This
       catches both the diffuse members.mrn (charset=alphanum) live bug and a
       NARROW leak -- a handful of verbatim positions among many permuted ones
       -- that a mean-over-positions statistic would dilute below any floor.

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

    Returns:
        A short status string for the evidence report: ``"checked"`` when the
        positional check ran, or a ``"SKIP: ..."`` note (LOW-1) when an FPE
        column had too few comparable rows to evaluate the positional check
        (a distinct SKIP, not a silent PASS, so the privacy floor cannot
        quietly evaporate on a small table).

    Raises:
        AssertionError: If output value-set equals source value-set (complete
            no-op), or if an FPE column retains a source character verbatim at
            an informative position across the rows (partial passthrough /
            charset undercoverage).
    """
    if strategy not in _VALUE_CHANGING_STRATEGIES:
        return "n/a"
    if column not in source_df.columns or column not in output_df.columns:
        return "n/a"  # Column-presence is checked by check_correlation_through_masking.
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
    # permutes the in-charset characters while leaving the out-of-charset ones in
    # place, so every whole value differs and the set check passes even though
    # informative characters leak verbatim.  FPE is format-preserving, so a
    # per-position across-row comparison detects this: a correctly-charset'd FPE
    # retains ~1/k of characters at any informative position, while an
    # undercovered-charset leak retains an out-of-charset character verbatim
    # there.  Testing each position independently (not the mean over positions)
    # catches a NARROW leak of a few verbatim positions that the mean would
    # dilute.
    if strategy != "fpe":
        return "checked (set-only; non-fpe value-changing strategy)"

    leak = _fpe_positional_leak(source_df[column], output_df[column])
    if leak.worst_fraction is None:
        # LOW-1: no informative position could be evaluated -- surface an explicit
        # SKIP (never a silent pass) and say WHY.
        if leak.max_group_rows < _FPE_RETENTION_MIN_ROWS:
            reason = (
                f"only {leak.n_comparable} comparable rows "
                f"(< {_FPE_RETENTION_MIN_ROWS} in every equal-length group)"
            )
        else:
            reason = (
                f"no informative position (all positions have < "
                f"{_FPE_MIN_INFORMATIVE_ALPHABET} distinct source characters, "
                f"e.g. a short/low-entropy code); {leak.n_comparable} comparable rows"
            )
        return (
            f"SKIP fpe positional check: column {column!r} -- {reason}; "
            f"set-check passed, positional leak detection not run"
        )
    assert leak.worst_fraction < _FPE_MAX_POSITIONAL_RETENTION, (
        f"[{job_name}/{table}] fpe partial-passthrough: column {column!r} "
        f"leaks the source character verbatim at position {leak.worst_position} in "
        f"{leak.worst_fraction:.1%} of rows (>= {_FPE_MAX_POSITIONAL_RETENTION:.0%} "
        f"floor; source alphabet k={leak.worst_k} there, so a genuine permutation "
        f"would retain ~{1.0 / leak.worst_k:.0%}).  The configured FPE charset does "
        f"not cover the data's characters at that position, so an out-of-charset "
        f"character is emitted verbatim.  Declare charset:ALPHANUM to cover "
        f"uppercase+digits, or the specific charset that spans every character "
        f"class present in {column!r}."
    )
    # MED (dennis re-verify): a mixed column with some informative and some
    # low-entropy positions must NOT read as fully vetted -- disclose the
    # excluded low-k positions the positional check could not evaluate.
    excluded = (
        f"; {leak.n_low_entropy} low-entropy pos (k<{_FPE_MIN_INFORMATIVE_ALPHABET}) "
        f"NOT leak-checked (see what-we-cannot-prove.md)"
        if leak.n_low_entropy
        else ""
    )
    return (
        f"checked (worst pos {leak.worst_position}: "
        f"{leak.worst_fraction:.0%} retained, k={leak.worst_k}){excluded}"
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
