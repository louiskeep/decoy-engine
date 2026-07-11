"""Invariant assertion library for the test-flight suite.

Each public function corresponds to one invariant family described in the
acceptance-testflight plan (sections 6.1-6.10). Phase 1 implements
check_distribution_mask and check_distribution_generate via the
_distribution sub-module; Phase 2 implements all remaining families.

Naming convention: check_<family>(...) raises AssertionError on failure with a
message naming job/table/column/strategy so triage localises to one strategy.

Module split (Phase 4 / LOW-4):
  distribution + chapter-preserve -> _distribution.py / _chapter.py
  computed-column invariants       -> _computed.py
  FK + remap-mask invariants       -> _fk_remap.py (Phase 4 extraction)
  strategy-coverage guard          -> _coverage.py (Phase 4 new module)
Module split (TH-2, same 600-line-cap rationale):
  determinism invariant            -> _determinism.py
  checksum invariant               -> _checksum.py
  safe_harbor invariant            -> _safe_harbor.py
This module re-exports from all sub-modules so existing callers are unchanged.
"""

from __future__ import annotations

from typing import Any

# Re-export sub-module entry points so existing callers that import from
# _invariants continue to work without change.
from ._chapter import check_chapter_preserve as check_chapter_preserve
from ._checksum import check_checksums as check_checksums
from ._computed import check_computed_columns as check_computed_columns
from ._correlation import check_correlation_through_masking as check_correlation_through_masking
from ._coverage import check_job_strategy_coverage as check_job_strategy_coverage
from ._determinism import check_determinism as check_determinism
from ._distribution import (
    FIXED_TS as FIXED_TS,
)
from ._distribution import (
    check_distribution_generate as check_distribution_generate,
)
from ._distribution import (
    check_distribution_mask as check_distribution_mask,
)
from ._fk_remap import _VALUE_CHANGING_STRATEGIES as _VALUE_CHANGING_STRATEGIES
from ._fk_remap import _check_remap_not_passthrough as _check_remap_not_passthrough
from ._fk_remap import check_fk_integrity as check_fk_integrity
from ._fk_remap import check_remap_masks_orphan as check_remap_masks_orphan
from ._fk_remap import (
    check_value_changing_not_passthrough as check_value_changing_not_passthrough,
)
from ._safe_harbor import check_safe_harbor as check_safe_harbor
from ._spec import (
    JointMaskConsistencySpec,
    MaskedCorrelationSpec,
    QuarantineSpec,
    SentinelSpec,
)

__all__ = [
    "FIXED_TS",
    "_VALUE_CHANGING_STRATEGIES",
    "JointMaskConsistencySpec",
    "MaskedCorrelationSpec",
    "check_chapter_preserve",
    "check_checksums",
    "check_computed_columns",
    "check_correlation_through_masking",
    "check_determinism",
    "check_distribution_generate",
    "check_distribution_mask",
    "check_fk_integrity",
    "check_job_strategy_coverage",
    "check_joint_mask_consistency",
    "check_quarantine",
    "check_remap_masks_orphan",
    "check_safe_harbor",
    "check_sentinels",
    "check_strategy_coverage",
    "check_value_changing_not_passthrough",
]  # fmt: skip

# ---------------------------------------------------------------------------
# 6.1 Determinism, 6.5 Checksums, 6.6 Safe Harbor
# ---------------------------------------------------------------------------
# Implementations moved to _determinism.py / _checksum.py / _safe_harbor.py
# (TH-2, same 600-line-cap rationale as the Phase 4 / LOW-4 split noted in
# this module's docstring) and re-exported above.


# ---------------------------------------------------------------------------
# 6.7 Quarantine counts
# ---------------------------------------------------------------------------


def check_quarantine(
    job_name: str,
    spec: QuarantineSpec,
    result: Any,
    source_tables: dict[str, Any] | None = None,
) -> str:
    """Assert quarantine row count, file presence, and absence from main output.

    Asserts:
    - result.quality_metrics["quarantine"]["total_quarantined"] == expected.
    - The JSONL quarantine file exists and has the correct line count.
    - result.quality_metrics["validation"]["validators"]["findings"] includes
      a finding for the expected_validator.
    - LOW-1: the quarantined rows are ABSENT from every output table's main
      columns (main_rows == source_rows - quarantined, and the known bad column
      values -- if provided via source_tables -- do not appear in the output).

    Args:
        job_name: Job name for error messages.
        spec: QuarantineSpec from the manifest invariants.
        result: ExecutionResult carrying quality_metrics and output tables.
        source_tables: Optional dict[table_name, pa.Table] of source frames.
            When provided, asserts that the row count of the quarantine-affected
            table in result.outputs is source_rows - quarantined (not just the
            quarantine count; a double-quarantine bug ships wrong row count).

    Returns:
        Short evidence string with expected/found counts.

    Raises:
        AssertionError: If quarantine count mismatches, file is absent, or
            quarantined rows are present in the main output.
    """
    qm = result.quality_metrics

    # Check quarantine block presence.
    assert "quarantine" in qm, (
        f"[{job_name}] quarantine: quality_metrics missing 'quarantine' key. "
        f"Expected {spec.expected_total_quarantined} quarantined rows. "
        f"Available keys: {list(qm.keys())}"
    )
    q_data = qm["quarantine"]

    actual_quarantined: int = q_data["total_quarantined"]
    assert actual_quarantined == spec.expected_total_quarantined, (
        f"[{job_name}] quarantine: total_quarantined={actual_quarantined} != "
        f"expected={spec.expected_total_quarantined}. "
        f"counts_by_trigger={q_data.get('counts_by_trigger', {})}."
    )

    # Check the JSONL file exists and has the correct line count.
    import pathlib

    q_path = pathlib.Path(q_data["output_path"])
    assert q_path.exists(), f"[{job_name}] quarantine: JSONL file does not exist: {q_path}."
    lines = q_path.read_text(encoding="utf-8").splitlines()
    non_empty = [ln for ln in lines if ln.strip()]
    assert len(non_empty) == spec.expected_total_quarantined, (
        f"[{job_name}] quarantine: JSONL file has {len(non_empty)} lines, "
        f"expected {spec.expected_total_quarantined}."
    )

    # Check the validator findings include expected_validator.
    v_data = qm.get("validation", {})
    validators_dict = v_data.get("validators", {}) if v_data else {}
    findings = validators_dict.get("findings", []) if validators_dict else []
    fired_validators = {f["validator"] for f in findings if isinstance(f, dict)}
    assert spec.expected_validator in fired_validators, (
        f"[{job_name}] quarantine: expected_validator '{spec.expected_validator}' "
        f"not found in validation findings. "
        f"Validators that fired: {fired_validators}."
    )

    # LOW-1: direct row-count assertion: main output must have source_rows -
    # quarantined rows. A finding that total_quarantined == N is necessary but
    # not sufficient: a pipeline that quarantines N rows but ALSO emits them to
    # the main output would pass the count check but fail here.
    # Also assert: output table row count == source row count - quarantined.
    if source_tables is not None:
        # The quarantine-affected table is the one referenced in findings.
        affected_tables: set[str] = {
            f.get("table", "") for f in findings if isinstance(f, dict) and f.get("table")
        }
        for affected_table in affected_tables:
            src_tbl = source_tables.get(affected_table)
            out_tbl = result.outputs.get(affected_table)
            if src_tbl is not None and out_tbl is not None:
                src_rows = src_tbl.num_rows
                out_rows = out_tbl.num_rows
                expected_out_rows = src_rows - spec.expected_total_quarantined
                assert out_rows == expected_out_rows, (
                    f"[{job_name}] quarantine LOW-1: {affected_table} output has "
                    f"{out_rows} rows but expected {expected_out_rows} "
                    f"(source={src_rows} minus quarantined={spec.expected_total_quarantined}). "
                    f"Quarantined rows must not appear in the main output."
                )

    return (
        f"expected={spec.expected_total_quarantined} "
        f"found={actual_quarantined} "
        f"(validator={spec.expected_validator})"
    )


# ---------------------------------------------------------------------------
# 6.8 Sentinel no-leakage
# ---------------------------------------------------------------------------


def check_sentinels(
    job_name: str,
    spec: list[SentinelSpec],
    result: Any,
) -> None:
    """Assert planted raw-PII sentinel strings are absent from all output.

    Scans EVERY column of EVERY output table (not just the planted column) for
    each sentinel value as an exact string and as a substring. A hit means a
    column was accidentally left on passthrough or masking did not apply.

    Args:
        job_name: Job name for error messages.
        spec: List of SentinelSpec from the manifest invariants.
        result: ExecutionResult carrying all output tables.

    Raises:
        AssertionError: If any sentinel string appears in any output column.
    """
    for sentinel in spec:
        sv = sentinel.value
        for table_name, tbl in result.outputs.items():
            schema = tbl.schema
            for col_name in schema.names:
                col_values: list[Any] = tbl.column(col_name).to_pylist()
                for row_idx, v in enumerate(col_values):
                    if v is None:
                        continue
                    str_v = str(v)
                    if sv in str_v:
                        raise AssertionError(
                            f"[{job_name}] sentinels: sentinel value "
                            f"{sv!r} (planted in source {sentinel.table}.{sentinel.column}) "
                            f"found in output {table_name}.{col_name} row {row_idx}: "
                            f"{str_v!r}. "
                            f"The masking strategy did not transform this value."
                        )


# ---------------------------------------------------------------------------
# 6.10 Strategy coverage guard
# ---------------------------------------------------------------------------


def check_strategy_coverage(
    job_name: str,
    declared: list[str],
) -> None:
    """Assert all declared strategies are present in the live SCALAR_HANDLERS registry.

    Per-job validation: every name in strategy_coverage must be a real key in
    SCALAR_HANDLERS (no typos).  "from_parent" is a FK column marker, not a
    registered strategy handler, and is skipped when present.

    The suite-level guard (check_suite_strategy_coverage in _coverage.py) runs
    once after all jobs and asserts that the union of all declared strategies
    covers SCALAR_HANDLERS minus the documented allowlist.  This per-job
    function is the per-manifest half of that contract.

    Args:
        job_name: Job name for error messages.
        declared: List of strategy names from InvariantSpec.strategy_coverage.

    Raises:
        AssertionError: If a declared strategy is not in SCALAR_HANDLERS.
    """
    check_job_strategy_coverage(job_name, declared)


# ---------------------------------------------------------------------------
# SP-08 joint_mask consistency
# ---------------------------------------------------------------------------


def check_joint_mask_consistency(
    job_name: str,
    spec: list[JointMaskConsistencySpec],
    result: Any,
) -> str:
    """Assert each output joint_mask tuple is a real row in the reference table.

    SP-08 consistency property: joint_mask replaces a group of coupled columns
    with values from a SINGLE reference-table row. Each output tuple must
    therefore be an actual row in the reference table; a row that mixes
    values from different reference-table rows (a Frankenstein combination)
    is a bug in the transform.

    This check loads the shipped reference table, builds the set of all valid
    tuples for the declared columns, then asserts every output row's tuple is
    a member of that set.

    Args:
        job_name: Job name for error messages.
        spec: List of JointMaskConsistencySpec entries (one per joint_mask group).
        result: ExecutionResult carrying masked output tables.

    Returns:
        Short evidence string with per-group row counts and valid-tuple counts.

    Raises:
        AssertionError: If any output tuple is not a valid reference-table row.
    """
    from decoy_engine.reference_tables import load_table

    evidence_parts: list[str] = []
    for s in spec:
        out_pa = result.outputs.get(s.table)
        assert out_pa is not None, (
            f"[{job_name}] joint_mask_consistency: table '{s.table}' not in result.outputs."
        )

        ref = load_table(s.reference)
        # Build the set of valid (col_0, col_1, ...) tuples from the reference table.
        valid_tuples: set[tuple[Any, ...]] = set()
        for row_idx in range(ref.row_count):
            row = ref.row(row_idx)
            valid_tuples.add(tuple(row.get(c) for c in s.columns))

        out_df = out_pa.to_pandas()
        n_rows = len(out_df)
        n_checked = 0
        for i in range(n_rows):
            values = tuple(out_df[c].iloc[i] for c in s.columns)
            # All-null tuples occur only when key_by is null (null-key fallback);
            # the fallback picks a seeded random row, which is a valid reference row.
            # Skip degenerate all-null rows: they cannot be validated against the table.
            if all(v is None or (isinstance(v, float) and v != v) for v in values):
                continue
            n_checked += 1
            assert values in valid_tuples, (
                f"[{job_name}] joint_mask_consistency: {s.table} row {i}: "
                f"tuple {dict(zip(s.columns, values, strict=True))} is not a real row in "
                f"reference table {s.reference!r}. "
                f"joint_mask must write all target columns from a single reference-table "
                f"row; mixing values from different rows (Frankenstein combination) "
                f"violates the SP-08 consistency contract."
            )

        evidence_parts.append(
            f"{s.table}.({','.join(s.columns)}): {n_checked}/{n_rows} rows "
            f"all-valid ({len(valid_tuples)} valid tuples in {s.reference})"
        )

    return "; ".join(evidence_parts)
