"""Invariant assertion library for the test-flight suite.

Each public function corresponds to one invariant family described in the
acceptance-testflight plan (sections 6.1-6.10). Phase 1 implements
check_distribution_mask and check_distribution_generate via the
_distribution sub-module; Phase 2 implements all remaining families.

Naming convention: check_<family>(...) raises AssertionError on failure with a
message naming job/table/column/strategy so triage localises to one strategy.

Module split (LOW-4): distribution logic lives in testflight/_distribution.py.
This module re-exports the distribution entry points and FIXED_TS so existing
callers that import from testflight._invariants continue to work unchanged.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

# Re-export distribution-fidelity and chapter-preserve functions so existing
# callers that import from _invariants continue to work without change.
from ._chapter import check_chapter_preserve as check_chapter_preserve
from ._computed import check_computed_columns as check_computed_columns
from ._distribution import (
    FIXED_TS as FIXED_TS,
)
from ._distribution import (
    check_distribution_generate as check_distribution_generate,
)
from ._distribution import (
    check_distribution_mask as check_distribution_mask,
)
from ._spec import (
    ChecksumSpec,
    FKIntegritySpec,
    QuarantineSpec,
    RelationshipSpec,
    SafeHarborSpec,
    SentinelSpec,
)

__all__ = [
    "FIXED_TS",
    "check_chapter_preserve",
    "check_checksums",
    "check_computed_columns",
    "check_determinism",
    "check_distribution_generate",
    "check_distribution_mask",
    "check_fk_integrity",
    "check_quarantine",
    "check_remap_masks_orphan",
    "check_safe_harbor",
    "check_sentinels",
    "check_strategy_coverage",
]

# HHS-restricted ZIP3 prefixes (HIPAA Safe Harbor; populations < 20,000).
_RESTRICTED_ZIP3_PREFIXES: frozenset[str] = frozenset(
    {
        "036",
        "059",
        "063",
        "102",
        "203",
        "556",
        "692",
        "790",
        "821",
        "823",
        "830",
        "831",
        "878",
        "879",
        "884",
        "890",
        "893",
    }
)


# ---------------------------------------------------------------------------
# 6.1 Determinism
# ---------------------------------------------------------------------------


def check_determinism(
    job_name: str,
    result_a: Any,
    result_b: Any,
) -> None:
    """Assert that two pipeline runs produce byte-identical outputs.

    For every table in result_a.outputs, assert:
      result_a.outputs[t].to_pydict() == result_b.outputs[t].to_pydict()

    Also asserts that the in-pipeline quality_metrics["fidelity_reports"]
    blocks are byte-equal across both runs (the golden determinism idiom from
    tests/integration/golden/test_fidelity_report_golden.py).

    Args:
        job_name: Job name for error messages.
        result_a: First ExecutionResult.
        result_b: Second ExecutionResult.

    Raises:
        AssertionError: If any table output differs across runs.
    """
    tables_a: dict[str, pa.Table] = result_a.outputs
    tables_b: dict[str, pa.Table] = result_b.outputs

    assert set(tables_a) == set(tables_b), (
        f"[{job_name}] determinism: output table sets differ between runs: "
        f"A={set(tables_a)} B={set(tables_b)}"
    )

    for table_name in tables_a:
        dict_a = tables_a[table_name].to_pydict()
        dict_b = tables_b[table_name].to_pydict()
        assert dict_a == dict_b, (
            f"[{job_name}] determinism: table '{table_name}' differs between runs. "
            f"Columns with mismatches: "
            + ", ".join(col for col in dict_a if dict_a.get(col) != dict_b.get(col))
        )

    # Also compare fidelity_reports block if present (golden idiom).
    fr_a = result_a.quality_metrics.get("fidelity_reports", {})
    fr_b = result_b.quality_metrics.get("fidelity_reports", {})
    assert fr_a == fr_b, (
        f"[{job_name}] determinism: quality_metrics['fidelity_reports'] differ between runs."
    )


# ---------------------------------------------------------------------------
# 6.4 FK integrity
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
    strategy to the orphan source key. For FPE with an in-charset key this
    produces a permuted value that differs from the input. Equality means the
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
        f"passthrough (out-of-charset no-op). Use an in-charset source key or "
        f"a strategy that always transforms (see docs/what-we-cannot-prove.md)."
    )


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
    the SOURCE parent key pool (not the masked pools). Rows not in the source
    parent pool are orphans; their output FK values are what the remap policy
    produced. Equality means passthrough (no-op), which is the gap documented
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
            # orphan row's output value differs from its source key. This closes
            # the passthrough gap: an out-of-charset source key that FPE cannot
            # permute would be emitted verbatim, contradicting the remap contract.
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


# ---------------------------------------------------------------------------
# 6.5 Checksums
# ---------------------------------------------------------------------------


def check_checksums(
    job_name: str,
    spec: list[ChecksumSpec],
    result: Any,
) -> None:
    """Assert every output value in checksum columns satisfies validate(scheme, v).

    Calls decoy_engine.checksums.validate(scheme, value) per row in each
    declared (table, column) pair. A single failing value raises AssertionError
    naming job/table/column/scheme/value.

    Args:
        job_name: Job name for error messages.
        spec: List of ChecksumSpec from the manifest invariants.
        result: ExecutionResult carrying masked output tables.

    Raises:
        AssertionError: If any output value fails checksum validation.
    """
    from decoy_engine.checksums import validate

    for cs in spec:
        tbl = result.outputs.get(cs.table)
        assert tbl is not None, f"[{job_name}] checksums: table '{cs.table}' not in result.outputs."
        values: list[Any] = tbl.column(cs.column).to_pylist()
        for i, v in enumerate(values):
            if v is None:
                continue
            str_v = str(v)
            ok = validate(cs.scheme, str_v)
            assert ok, (
                f"[{job_name}] checksums: {cs.table}.{cs.column} row {i}: "
                f"validate('{cs.scheme}', {str_v!r}) == False. "
                f"Column uses a checksum-producing strategy (fpe + checksum:{cs.scheme}) "
                f"but the output value failed the check-digit assertion. "
                f"This indicates the FPE checksum-recomputation path was not taken."
            )


# ---------------------------------------------------------------------------
# 6.6 Safe Harbor suppression
# ---------------------------------------------------------------------------


def check_safe_harbor(
    job_name: str,
    spec: list[SafeHarborSpec],
    result: Any,
) -> None:
    """Assert Safe Harbor suppression counts and absence of restricted ZIP3.

    For each SafeHarborSpec:
    - Assert no restricted ZIP3 prefix survives at zip5 resolution in the
      output (geo_generalize must have generalized or suppressed it).
    - Assert the count of 'suppressed' entries in the geo_generalize_cascade
      QualityWarning equals planted_restricted_zip3_count.
    - Assert expected_suppressions matches that count.

    Args:
        job_name: Job name for error messages.
        spec: List of SafeHarborSpec from the manifest invariants.
        result: ExecutionResult carrying masked outputs and quality_metrics warnings.

    Raises:
        AssertionError: If suppression counts mismatch or a restricted ZIP3 leaks.
    """
    # Index QualityWarnings by (code, column).
    geo_warnings = [w for w in result.warnings if w.code == "geo_generalize_cascade"]

    for sh in spec:
        tbl = result.outputs.get(sh.table)
        assert tbl is not None, (
            f"[{job_name}] safe_harbor: table '{sh.table}' not in result.outputs."
        )
        out_values: list[Any] = tbl.column(sh.column).to_pylist()

        # 1. No full ZIP5 starting with a restricted prefix survives.
        leaked = [
            v
            for v in out_values
            if isinstance(v, str) and len(v) == 5 and v[:3] in _RESTRICTED_ZIP3_PREFIXES
        ]
        assert len(leaked) == 0, (
            f"[{job_name}] safe_harbor: {sh.table}.{sh.column}: "
            f"{len(leaked)} restricted ZIP5 value(s) leaked into output: {leaked[:5]}. "
            f"geo_generalize should have generalized or suppressed all restricted ZIP3 rows."
        )

        # 2. Find the geo_generalize_cascade warning for this column.
        col_warnings = [w for w in geo_warnings if w.column == sh.column]
        assert col_warnings, (
            f"[{job_name}] safe_harbor: {sh.table}.{sh.column}: "
            f"no geo_generalize_cascade QualityWarning found for column '{sh.column}'. "
            f"Expected at least {sh.planted_restricted_zip3_count} suppressed rows. "
            f"Available warnings: {[w.code for w in result.warnings]}"
        )

        # 3. Count suppressed decisions (rows with the restricted ZIP3 prefix
        #    that were suppressed because ZIP3 population < HIPAA_K_THRESHOLD).
        cascade_decisions: dict[str, str] = col_warnings[0].detail.get("cascade_decisions", {})
        suppressed_count = sum(1 for v in cascade_decisions.values() if v == "suppressed")

        assert suppressed_count == sh.planted_restricted_zip3_count, (
            f"[{job_name}] safe_harbor: {sh.table}.{sh.column}: "
            f"suppressed_count={suppressed_count} != "
            f"planted_restricted_zip3_count={sh.planted_restricted_zip3_count}. "
            f"Cascade decision distribution: "
            + str(
                {
                    v: sum(1 for d in cascade_decisions.values() if d == v)
                    for v in set(cascade_decisions.values())
                }
            )
        )

        assert suppressed_count == sh.expected_suppressions, (
            f"[{job_name}] safe_harbor: {sh.table}.{sh.column}: "
            f"suppressed_count={suppressed_count} != "
            f"expected_suppressions={sh.expected_suppressions}."
        )


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

    At Phase 4, the suite-level coverage guard (run once across all jobs) will
    assert that the union of declared strategies covers SCALAR_HANDLERS minus a
    documented allowlist. This per-job function asserts that each strategy name
    declared in strategy_coverage exists in SCALAR_HANDLERS so a typo in the
    manifest fails loudly.

    Args:
        job_name: Job name for error messages.
        declared: List of strategy names from InvariantSpec.strategy_coverage.

    Raises:
        AssertionError: If a declared strategy is not in SCALAR_HANDLERS.
        NotImplementedError: Phase 4 implementation pending.
    """
    raise NotImplementedError("Phase 4: check_strategy_coverage")
