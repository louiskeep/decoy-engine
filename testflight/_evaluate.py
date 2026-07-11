"""Invariant-dispatch body for the test-flight runner.

Extracted from _runner.py (Phase 5 carry-forward) to keep both modules
within the 600-line limit. Re-exported from _runner.py so callers that
import from there are unchanged.

`InvariantResult` and `evaluate_invariants` are the only public names.

Design:
- `evaluate_invariants` delegates each invariant family to the
  corresponding function in _invariants.py via a local _run() helper
  that converts AssertionError to a failed InvariantResult so the
  runner always gets a flat list back, never an exception.
- Each invariant family is isolated: a failure in family X does not
  prevent families Y and Z from running, giving the evidence report a
  complete picture.
- The distribution block routes each table to check_distribution_mask
  (mask tables, quality-report based) or check_distribution_generate
  (generate tables, config-derived baseline), per OWNER DECISION Q3.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from ._invariants import (
    _VALUE_CHANGING_STRATEGIES,
    check_chapter_preserve,
    check_checksums,
    check_computed_columns,
    check_correlation_through_masking,
    check_determinism,
    check_distribution_generate,
    check_distribution_mask,
    check_fk_integrity,
    check_generate_row_count,
    check_joint_mask_consistency,
    check_quarantine,
    check_safe_harbor,
    check_sentinels,
    check_strategy_coverage,
    check_value_changing_not_passthrough,
)
from ._spec import FlightManifest


@dataclass
class InvariantResult:
    """Outcome of one invariant-family evaluation for one job.

    `family` matches the InvariantSpec field name (e.g. 'determinism',
    'fk_integrity', 'distribution', ...). `passed` is the verdict.
    `detail` is a human-readable summary for the evidence report.

    `skipped` (TH-4.1) marks a result that could not actually be evaluated
    (e.g. Cramers V undefined on a degenerate column) so the report renders
    a distinct SKIP mark instead of folding it into an ordinary PASS -- a
    check that never ran must never read the same as one that ran and
    passed. Skipped results still carry `passed=True` (an un-evaluable
    check is not itself a regression), but `_report.py` surfaces `skipped`
    separately so it cannot be mistaken for real coverage.
    """

    family: str
    passed: bool
    detail: str = ""
    skipped: bool = False


def _masked_correlation_result(family_name: str, evidence: dict[str, Any]) -> InvariantResult:
    """Turn check_correlation_through_masking's evidence dict into an InvariantResult.

    Cramers V is undefined for a degenerate column (fewer than 2 distinct
    non-null values); check_correlation_through_masking returns diff=None
    rather than asserting in that case (see _correlation.py). Folding that
    straight into an ordinary `passed=True` reads as a full green check in
    the evidence report even though nothing was actually compared -- a future
    pair declared on a synthetic/coarsen column that degenerates could vanish
    silently. TH-4.1: surface it as an explicit, distinct SKIP instead.

    Args:
        family_name: The InvariantResult.family value for this pair.
        evidence: Return value of check_correlation_through_masking (has
            keys v_src, v_out, diff; diff is None iff degenerate).

    Returns:
        A passed InvariantResult with skipped=True when V was undefined, or
        an ordinary passed InvariantResult carrying the v_src/v_out/diff
        detail otherwise.
    """
    if evidence.get("diff") is None:
        return InvariantResult(
            family=family_name,
            passed=True,
            skipped=True,
            detail=(
                "SKIP: Cramers V undefined (degenerate: a declared column has "
                f"<2 distinct non-null values). v_src={evidence.get('v_src')} "
                f"v_out={evidence.get('v_out')}"
            ),
        )
    return InvariantResult(
        family=family_name,
        passed=True,
        detail=(
            f"v_src={evidence['v_src']:.4f} v_out={evidence['v_out']:.4f} "
            f"diff={evidence['diff']:.4f}"
        ),
    )


def evaluate_invariants(
    manifest: FlightManifest,
    result_a: Any,
    result_b: Any,
    sources: dict[str, pa.Table],
) -> list[InvariantResult]:
    """Step 6: Evaluate every InvariantSpec against ExecutionResult.

    Delegates each invariant family to the corresponding function in
    _invariants.py. Returns a flat list of InvariantResult; any assertion
    failure is caught and turned into a failed InvariantResult with the
    AssertionError message in the detail.

    Args:
        manifest: Validated job manifest (carries InvariantSpec).
        result_a: First run ExecutionResult.
        result_b: Second run ExecutionResult (for determinism check).
        sources: dict[table_name, pa.Table] of source frames.

    Returns:
        List of InvariantResult, one per invariant family checked.
    """
    inv = manifest.invariants
    job_name = manifest.job_name
    results: list[InvariantResult] = []

    def _run(family: str, fn: Any, *args: Any, **kwargs: Any) -> None:
        try:
            evidence = fn(*args, **kwargs)
            # Capture optional return-value evidence (expected-vs-found counts)
            # for the report's PASS lines (LOW-2).
            detail = str(evidence) if evidence is not None else ""
            results.append(InvariantResult(family=family, passed=True, detail=detail))
        except (AssertionError, NotImplementedError) as exc:
            msg = str(exc)
            results.append(
                InvariantResult(
                    family=family,
                    passed=False,
                    detail=msg[:500],
                )
            )

    # 6.1 Determinism
    if inv.determinism:
        _run("determinism", check_determinism, job_name, result_a, result_b)

    # 6.4 FK integrity.
    # Pass source_frames so check_fk_integrity can verify that remap-policy
    # orphans produce output values that differ from their source keys.
    if inv.fk_integrity:
        _run(
            "fk_integrity",
            check_fk_integrity,
            job_name,
            inv.fk_integrity,
            result_a,
            manifest.relationships,
            sources,  # enables remap-masks-orphan check
        )

    # 6.4b Value-changing-passthrough guard (TH-1.1).
    # Every mask column whose declared strategy is value-changing (fpe, hash,
    # code_set) must genuinely change its data.  Derived from the LIVE pipeline
    # config columns (not the hand-declared distribution spec) so EVERY such
    # column is covered -- including columns absent from the distribution spec
    # (e.g. members.mrn in Job A).  A COMPLETE no-op trips the value-set check;
    # an FPE charset-undercoverage partial passthrough trips the positional
    # retention check.
    def _run_value_changing_guard() -> str:
        checked: list[str] = []
        skipped: list[str] = []
        for t in manifest.config.get("tables", []):
            if not isinstance(t, dict):
                continue
            table_name = t.get("name")
            if not isinstance(table_name, str):
                continue
            src_pa = sources.get(table_name)
            out_pa = result_a.outputs.get(table_name)
            if src_pa is None or out_pa is None:
                continue  # generate tables have no source; nothing to compare
            src_df_v = src_pa.to_pandas()
            out_df_v = out_pa.to_pandas()
            for col in t.get("columns", []):
                if not isinstance(col, dict):
                    continue
                strat = col.get("strategy")
                col_name = col.get("name")
                if strat not in _VALUE_CHANGING_STRATEGIES or not isinstance(col_name, str):
                    continue
                status = check_value_changing_not_passthrough(
                    job_name, table_name, col_name, strat, src_df_v, out_df_v
                )
                checked.append(f"{table_name}.{col_name}({strat})")
                if status.startswith("SKIP"):
                    skipped.append(f"{table_name}.{col_name}: {status}")
        if not checked:
            return "no value-changing columns"
        evidence = "checked=" + ",".join(checked)
        if skipped:
            evidence += " | " + "; ".join(skipped)
        return evidence

    _run("value_changing_passthrough", _run_value_changing_guard)

    # 6.3b Generate-table row count (TH-4.3 / P2).
    # Unconditional and independent of inv.distribution: a generate table's
    # row count was checked against nothing, so a 0-row (or any wrong-count)
    # generate table passed the whole suite incidentally -- every other
    # invariant just operates on however many rows happen to exist. This is
    # the RUNTIME counterpart to TableSpec's own schema validator (a 0-row
    # table must declare kind=generate): that validator only proves the SPEC
    # is well-formed, not that the ENGINE produced the declared count.
    for ts in manifest.tables:
        if ts.kind != "generate":
            continue
        family_name = f"generate_row_count:{ts.name}"
        out_pa = result_a.outputs.get(ts.name)
        if out_pa is None:
            results.append(
                InvariantResult(
                    family=family_name,
                    passed=False,
                    detail=f"generate table '{ts.name}' not in result.outputs.",
                )
            )
            continue
        _run(
            family_name,
            check_generate_row_count,
            job_name,
            ts.name,
            ts.row_count,
            out_pa.to_pandas(),
        )

    # 6.2/6.3 Distribution fidelity.
    # Mask tables: check_distribution_mask (quality-report based).
    # Generate tables: check_distribution_generate (config-derived baseline only;
    # OWNER DECISION Q3: no committed golden snapshots for generate tables).
    if inv.distribution:
        # Group specs by table name.
        specs_by_table: dict[str, list[Any]] = defaultdict(list)
        for col_spec in inv.distribution:
            specs_by_table[col_spec.table].append(col_spec)

        # Build a set of generate table names from the manifest so we can route
        # each table to the correct check (mask vs. generate).
        generate_table_names: set[str] = {
            ts.name for ts in manifest.tables if ts.kind == "generate"
        }
        # Build a lookup of config_table dicts for generate tables (needed by
        # check_distribution_generate to read declared weights/params).
        config_tables: dict[str, dict[str, Any]] = {
            t["name"]: t for t in manifest.config.get("tables", []) if isinstance(t, dict)
        }

        # If quarantine is configured, we need to trim the source for the
        # quarantine-affected table so source_rows == output_rows (the
        # diagnostic row-parity check compares source vs masked output).
        # The quarantine rows are planted at the END of the source by fixture
        # design, so we trim `n_quarantined` rows from the tail of the source.
        quarantine_table: str | None = None
        n_quarantined: int = 0
        if inv.quarantine is not None and inv.quarantine.expected_total_quarantined > 0:
            # Identify the quarantine-affected table from the validators config.
            validators_block = manifest.config.get("validators", [])
            for v in validators_block:
                cols_block = v.get("columns", {})
                if cols_block:
                    quarantine_table = next(iter(cols_block))
                    break
            n_quarantined = inv.quarantine.expected_total_quarantined

        for table_name, table_specs in specs_by_table.items():
            try:
                if table_name in generate_table_names:
                    # 6.3: generate table -- no source frame; check against config.
                    out_pa = result_a.outputs.get(table_name)
                    if out_pa is None:
                        results.append(
                            InvariantResult(
                                family=f"distribution:{table_name}",
                                passed=False,
                                detail=(
                                    f"generate table '{table_name}' not in "
                                    "result.outputs. Pipeline did not produce it."
                                ),
                            )
                        )
                        continue
                    out_df = out_pa.to_pandas()
                    config_table = config_tables.get(table_name, {})
                    check_distribution_generate(
                        job_name=job_name,
                        table=table_name,
                        spec=table_specs,
                        output_df=out_df,
                        config_table=config_table,
                    )
                    results.append(
                        InvariantResult(
                            family=f"distribution:{table_name}",
                            passed=True,
                        )
                    )
                    continue

                # 6.2: mask table -- source frame required.
                src_pa = sources.get(table_name)
                if src_pa is None:
                    results.append(
                        InvariantResult(
                            family=f"distribution:{table_name}",
                            passed=False,
                            detail=f"source table '{table_name}' not in sources dict.",
                        )
                    )
                    continue
                src_df = src_pa.to_pandas()

                # Trim source for quarantine-affected tables so row parity holds.
                if table_name == quarantine_table and n_quarantined > 0:
                    src_df = src_df.iloc[: len(src_df) - n_quarantined].copy()

                out_pa = result_a.outputs.get(table_name)
                if out_pa is None:
                    results.append(
                        InvariantResult(
                            family=f"distribution:{table_name}",
                            passed=False,
                            detail=f"output table '{table_name}' not in result.outputs.",
                        )
                    )
                    continue
                out_df = out_pa.to_pandas()

                # Build combined strategy_map from all specs for this table.
                strategy_map: dict[str, str] = {}
                for col_spec in table_specs:
                    if col_spec.strategy:
                        strategy_map[col_spec.column] = col_spec.strategy

                check_distribution_mask(
                    job_name=job_name,
                    table=table_name,
                    spec=table_specs,
                    source_df=src_df,
                    output_df=out_df,
                    strategy_map=strategy_map,
                    policy_config=inv.policy,
                    grade_floor_enabled=inv.grade_floor_enabled,
                )
                results.append(
                    InvariantResult(
                        family=f"distribution:{table_name}",
                        passed=True,
                    )
                )
            except (AssertionError, Exception) as exc:
                results.append(
                    InvariantResult(
                        family=f"distribution:{table_name}",
                        passed=False,
                        detail=str(exc)[:500],
                    )
                )

    # 6.5 Checksums
    if inv.checksums:
        _run("checksums", check_checksums, job_name, inv.checksums, result_a)

    # 6.6 Safe Harbor
    if inv.safe_harbor:
        _run("safe_harbor", check_safe_harbor, job_name, inv.safe_harbor, result_a)

    # 6.7 Quarantine (pass sources so LOW-1 direct row-count assertion runs)
    if inv.quarantine is not None:
        _run("quarantine", check_quarantine, job_name, inv.quarantine, result_a, sources)

    # 6.8 Sentinels
    if inv.sentinels:
        _run("sentinels", check_sentinels, job_name, inv.sentinels, result_a)

    # 6.9 Computed columns
    if inv.computed_columns:
        _run(
            "computed_columns",
            check_computed_columns,
            job_name,
            inv.computed_columns,
            result_a,
        )

    # 6.10 Per-job strategy coverage: declared names must exist in SCALAR_HANDLERS.
    # The suite-level union-coverage guard runs once in run_suite (after all jobs).
    if inv.strategy_coverage:
        _run("strategy_coverage", check_strategy_coverage, job_name, inv.strategy_coverage)

    # 6.11 chapter_preserve
    if inv.chapter_preserve:
        _run(
            "chapter_preserve",
            check_chapter_preserve,
            job_name,
            inv.chapter_preserve,
            result_a,
            sources,
        )

    # SP-08 joint_mask consistency: each output tuple must be a real reference-table row.
    if inv.joint_mask_consistency:
        _run(
            "joint_mask_consistency",
            check_joint_mask_consistency,
            job_name,
            inv.joint_mask_consistency,
            result_a,
        )

    # Phase 3c: relabel-invariant masked-correlation checks.
    # Each MaskedCorrelationSpec declares a pair of value-changing-masked columns
    # (fpe, code_set, etc.). Cramers V is computed over contingency COUNTS on
    # both source and output; assert abs(v_out - v_src) <= tol.
    for mc_spec in inv.masked_correlations:
        family_name = f"masked_correlation:{mc_spec.table}:{mc_spec.col_a}:{mc_spec.col_b}"
        try:
            src_pa = sources.get(mc_spec.table)
            out_pa = result_a.outputs.get(mc_spec.table)
            if src_pa is None:
                results.append(
                    InvariantResult(
                        family=family_name,
                        passed=False,
                        detail=f"source table '{mc_spec.table}' not in sources dict.",
                    )
                )
                continue
            if out_pa is None:
                results.append(
                    InvariantResult(
                        family=family_name,
                        passed=False,
                        detail=f"output table '{mc_spec.table}' not in result.outputs.",
                    )
                )
                continue
            src_df = src_pa.to_pandas()
            out_df = out_pa.to_pandas()
            # Tooth: each value-changing-masked column must produce at least one
            # changed value. A complete no-op (output value-set == source value-set)
            # means the charset does not cover the data (e.g. alphanum on uppercase).
            for _col, _strat in [
                (mc_spec.col_a, mc_spec.strategy_a),
                (mc_spec.col_b, mc_spec.strategy_b),
            ]:
                if _strat is not None:
                    check_value_changing_not_passthrough(
                        job_name, mc_spec.table, _col, _strat, src_df, out_df
                    )
            evidence = check_correlation_through_masking(
                job_name,
                mc_spec.table,
                mc_spec.col_a,
                mc_spec.col_b,
                src_df,
                out_df,
                tol=mc_spec.tol,
                min_assoc=mc_spec.min_assoc,
                strategy_a=mc_spec.strategy_a,
                strategy_b=mc_spec.strategy_b,
            )
            results.append(_masked_correlation_result(family_name, evidence))
        except (AssertionError, ValueError) as exc:
            results.append(
                InvariantResult(
                    family=family_name,
                    passed=False,
                    detail=str(exc)[:500],
                )
            )

    return results
