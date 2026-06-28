"""Invariant assertion library for the test-flight suite.

Each public function corresponds to one invariant family described in the
acceptance-testflight plan (sections 6.1-6.10). Phase 1 implements
check_distribution_mask and check_distribution_generate; remaining families
carry Phase 2+ stubs.

Naming convention: check_<family>(...) raises AssertionError on failure with a
message naming job/table/column/strategy so triage localises to one strategy.

Distribution note (6.2): the authoritative quality measurement calls
compute_quality_report directly on the source frame and masked output
(NOT on the in-pipeline fidelity_report result), passing the manifest's
declared joint_columns. The in-pipeline fidelity block is still asserted
byte-stable across the two runs as a determinism check.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from decoy_engine.quality.policy import apply_quality_policy
from decoy_engine.quality.report import compute_quality_report

from ._spec import (
    ChecksumSpec,
    ColumnDistributionSpec,
    ComputedColumnSpec,
    FKIntegritySpec,
    QuarantineSpec,
    SafeHarborSpec,
    SentinelSpec,
)

# Fixed timestamp injected into all compute_quality_report calls so two
# invocations on the same data produce byte-identical report dicts.
FIXED_TS = "2026-05-24T00:00:00+00:00"

# Strategies whose output value set is bijectively different from the source
# (every unique source value maps to a unique output value, but the output
# strings/numbers differ). The constant-collapse guard uses cardinality
# (out_nunique >= 0.99 * src_nunique) for these because value-identity TVD
# would be near 0.0 even for a correct run.
_CARDINALITY_BIJECTIVE: frozenset[str] = frozenset({"fpe", "hash"})

# Strategies that permute the source values without changing the set; the
# output value frequencies match the source exactly.
_MARGINAL_PRESERVING: frozenset[str] = frozenset({"shuffle"})


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
        NotImplementedError: Phase 1 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_determinism")


# ---------------------------------------------------------------------------
# 6.2 Distribution fidelity (mask tables)
# ---------------------------------------------------------------------------


def check_distribution_mask(
    job_name: str,
    table: str,
    spec: list[ColumnDistributionSpec],
    source_df: pd.DataFrame,
    output_df: pd.DataFrame,
    strategy_map: dict[str, str] | None = None,
    policy_config: dict[str, Any] | None = None,
    grade_floor_enabled: bool = True,
) -> None:
    """Assert distribution fidelity for one masked table.

    Calls compute_quality_report(source_df, output_df) with the declared
    joint_columns and apply_quality_policy in fail mode. Then applies the
    explicit teeth the policy alone does not give:

    Tooth A -- constant-collapse guard (preserve class):
      For fpe/hash columns: out_nunique >= 0.99 * src_nunique.
      For shuffle columns: marginal similarity >= 0.99.
      For other preserve columns: out_nunique >= 2 when src_nunique > 1.
      Catches a strategy that silently collapses a column to a constant.

    Tooth B -- real-coarsening guard (coarsen class, expected_coarsening=True):
      out_nunique STRICTLY < src_nunique. A coarsening strategy that did
      nothing (identity / passthrough) would fail this guard even though
      value-identity would look "fine" (it is the source itself).

    Tooth C -- correlation-preservation:
      For each declared joint pair, the pairwise TVD similarity >=
      col_spec.corr_tol (default 0.90). Undeclared pairs are out of scope.

    Tooth D -- null-rate drift per preserve column:
      abs(null_rate_out - null_rate_in) <= col_spec.null_pp (in pp).

    Tooth E -- grade floor (preserve-dominant tables):
      When preserve columns outnumber coarsen columns AND no preserve column
      uses a value-changing strategy (fpe/hash), assert grade in {A, B}.
      Tables with fpe/hash columns are excluded because value-identity TVD
      scores those columns near 0 by design; the cardinality guard (A) is the
      correct tooth there.

    Args:
        job_name: Job name for error messages.
        table: Table name for error messages.
        spec: ColumnDistributionSpec entries for this table.
        source_df: Pre-mask pandas DataFrame.
        output_df: Post-mask pandas DataFrame.
        strategy_map: Optional caller-provided {column: strategy_name} map. Merged
            with per-column strategy fields from spec; spec entries win on conflict.
        policy_config: Optional policy overrides merged into the fail-mode config.
        grade_floor_enabled: Whether to apply the grade-floor assertion (E).

    Raises:
        AssertionError: On any distribution invariant failure. The message names
            the specific tooth, job/table/column, and strategy.
    """
    # --- collect declared joint pairs from all spec entries in this table ---
    joint_cols: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for col_spec in spec:
        for pair in col_spec.joint_columns:
            if len(pair) >= 2:
                t: tuple[str, str] = (pair[0], pair[1])
                if t not in seen_pairs:
                    joint_cols.append(t)
                    seen_pairs.add(t)

    # --- call compute_quality_report with declared joints ---
    report = compute_quality_report(
        source_df,
        output_df,
        joint_columns=joint_cols if joint_cols else None,
        null_drift_threshold_pp=10.0,
        include_shape_fidelity=True,
        now_iso=FIXED_TS,
    )

    # --- build effective strategy_map (caller-provided merged with spec entries) ---
    # Spec entries with a declared strategy seed the map; the caller-provided
    # strategy_map can add or override (useful in mutation controls).
    effective_strategy_map: dict[str, str] = {}
    for col_spec in spec:
        if col_spec.strategy is not None:
            effective_strategy_map[col_spec.column] = col_spec.strategy
    if strategy_map:
        effective_strategy_map.update(strategy_map)

    # --- apply strategy-aware quality policy in fail mode ---
    effective_policy: dict[str, Any] = dict(policy_config or {})
    effective_policy["mode"] = "fail"
    policy_result = apply_quality_policy(
        report,
        policy_config=effective_policy,
        strategy_map=effective_strategy_map or None,
    )
    if policy_result["verdict"] != "pass":
        violations = policy_result.get("violations", [])
        lines = [v.get("detail", str(v)) for v in violations[:3]]
        raise AssertionError(
            f"[{job_name}/{table}] quality policy verdict="
            f"{policy_result['verdict']!r}: " + "; ".join(lines)
        )

    # --- build per-column lookup from the marginal report ---
    col_entry_by_name: dict[str, dict[str, Any]] = {
        c["column"]: c
        for c in report.get("marginal", {}).get("columns", [])
        if isinstance(c, dict) and "column" in c
    }

    # --- TOOTH A: constant-collapse guard (preserve class) ---
    for col_spec in spec:
        if col_spec.distribution_class != "preserve":
            continue
        col_name = col_spec.column
        if col_name not in source_df.columns or col_name not in output_df.columns:
            continue
        strategy = effective_strategy_map.get(col_name)

        if strategy in _CARDINALITY_BIJECTIVE:
            # FPE / hash: every distinct source value maps to a distinct output
            # value. If out_nunique drops below 0.99 * src_nunique, a collision
            # or constant-collapse occurred.
            src_nunique = source_df[col_name].nunique()
            out_nunique = output_df[col_name].nunique()
            floor = 0.99 * src_nunique
            if out_nunique < floor:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] constant-collapse guard "
                    f"(strategy={strategy!r}): out_nunique={out_nunique} < "
                    f"0.99 * src_nunique={src_nunique:.1f} (floor={floor:.1f}). "
                    f"Column may have collapsed to a constant."
                )
        elif strategy in _MARGINAL_PRESERVING:
            # Shuffle: same value set + same frequencies. Marginal TVD similarity
            # must be >= 0.99. Any drift means shuffle changed the value distribution.
            entry = col_entry_by_name.get(col_name)
            sim = entry.get("similarity") if isinstance(entry, dict) else None
            if sim is not None and float(sim) < 0.99:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] constant-collapse guard "
                    f"(strategy=shuffle): marginal_similarity={sim} < 0.99. "
                    f"Shuffle must preserve the marginal distribution exactly."
                )
        else:
            # Generic preserve (date_shift, passthrough, or unknown): apply a
            # weak guard -- column must not collapse to a single value when the
            # source had multiple.
            src_nunique = source_df[col_name].nunique()
            out_nunique = output_df[col_name].nunique()
            if src_nunique > 1 and out_nunique < 2:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] constant-collapse guard "
                    f"(strategy={strategy!r}): column collapsed to "
                    f"out_nunique={out_nunique} from src_nunique={src_nunique}."
                )

    # --- TOOTH B: real-coarsening guard (coarsen class, expected_coarsening=True) ---
    for col_spec in spec:
        if col_spec.distribution_class != "coarsen":
            continue
        if not col_spec.expected_coarsening:
            continue
        col_name = col_spec.column
        if col_name not in source_df.columns or col_name not in output_df.columns:
            continue
        strategy = effective_strategy_map.get(col_name)
        src_nunique = source_df[col_name].nunique()
        out_nunique = output_df[col_name].nunique()
        if out_nunique >= src_nunique:
            raise AssertionError(
                f"[{job_name}/{table}/{col_name}] real-coarsening guard "
                f"(strategy={strategy!r}): out_nunique={out_nunique} >= "
                f"src_nunique={src_nunique}. A coarsening strategy must "
                f"STRICTLY reduce cardinality; got no reduction."
            )

    # --- TOOTH C: correlation-preservation (declared joint pairs) ---
    joint_sims: dict[tuple[str, str], float | None] = {}
    for joint_entry in report.get("pairwise", {}).get("joints", []):
        if not isinstance(joint_entry, dict):
            continue
        cols = joint_entry.get("columns", [])
        if len(cols) == 2:
            joint_sims[(cols[0], cols[1])] = joint_entry.get("similarity")

    for col_spec in spec:
        for pair in col_spec.joint_columns:
            if len(pair) < 2:
                continue
            # Try both orderings (snapshot normalises alphabetically).
            fwd: tuple[str, str] = (pair[0], pair[1])
            rev: tuple[str, str] = (pair[1], pair[0])
            sim = joint_sims.get(fwd)
            if sim is None:
                sim = joint_sims.get(rev)
            if sim is None:
                # Pair was not computed (both columns must be in the frames).
                continue
            if float(sim) < col_spec.corr_tol:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_spec.column}] "
                    f"correlation-preservation: "
                    f"joint pair {(pair[0], pair[1])} similarity={sim:.4f} < "
                    f"corr_tol={col_spec.corr_tol}."
                )

    # --- TOOTH D: null-rate drift per preserve column ---
    for col_spec in spec:
        if col_spec.distribution_class != "preserve":
            continue
        col_name = col_spec.column
        if col_name not in source_df.columns or col_name not in output_df.columns:
            continue
        src_null_pp = source_df[col_name].isnull().mean() * 100.0
        out_null_pp = output_df[col_name].isnull().mean() * 100.0
        drift = abs(src_null_pp - out_null_pp)
        if drift > col_spec.null_pp:
            strategy = effective_strategy_map.get(col_name)
            raise AssertionError(
                f"[{job_name}/{table}/{col_name}] null-rate drift "
                f"(strategy={strategy!r}): drift={drift:.2f}pp > "
                f"tolerance={col_spec.null_pp}pp."
            )

    # --- TOOTH E: grade floor (preserve-dominant tables without value-changing strats) ---
    if grade_floor_enabled:
        preserve_count = sum(1 for s in spec if s.distribution_class == "preserve")
        coarsen_count = sum(1 for s in spec if s.distribution_class == "coarsen")
        # Skip grade floor when any preserve column uses a bijective transform
        # (fpe/hash): value-identity TVD is near 0 by design for those columns,
        # dragging the overall grade below B. The cardinality guard (A) is the
        # correct tooth for bijective-preserve columns.
        has_value_changing = any(
            effective_strategy_map.get(s.column) in _CARDINALITY_BIJECTIVE
            for s in spec
            if s.distribution_class == "preserve"
        )
        if preserve_count > coarsen_count and not has_value_changing:
            grade = report.get("grade", "unavailable")
            if grade not in ("A", "B"):
                raise AssertionError(
                    f"[{job_name}/{table}] grade-floor: preserve-dominant table "
                    f"expected grade A or B, got {grade!r} "
                    f"(overall_score={report.get('overall_score')})."
                )


# ---------------------------------------------------------------------------
# 6.3 Distribution fidelity (generate tables)
# ---------------------------------------------------------------------------


def check_distribution_generate(
    job_name: str,
    table: str,
    spec: list[ColumnDistributionSpec],
    output_df: pd.DataFrame,
    config_table: dict[str, Any],
) -> None:
    """Assert distribution fidelity for one generated table.

    Generate tables have no source frame; the baseline is the configured
    weights / params (OWNER DECISION Q3: config-derived only, no committed
    golden snapshots). Checks:

    - Categorical generate columns: TVD between output value-frequency vector
      and declared weights <= col_spec.tolerance (default 0.05). At multi-
      thousand rows, sampling noise is small enough that this is meaningful.

    - Statistical numeric generate columns: assert output mean within
      col_spec.tolerance * max(|declared_mean|, 1.0) of declared mean, and
      output std within col_spec.tolerance * max(declared_std, 1.0) of
      declared std.

    - Determinism: covered by check_determinism (the generate table is in
      result.outputs and compared byte-identically across two runs).

    Args:
        job_name: Job name for error messages.
        table: Table name for error messages.
        spec: ColumnDistributionSpec entries for this table.
        output_df: Post-generate pandas DataFrame.
        config_table: The raw pipeline config dict for this table. Must carry
            generate_columns (list of dicts with name, type, and type-specific
            params like weights or params.mean/std).

    Raises:
        AssertionError: If any column's output distribution deviates from its
            declared weights / params beyond tolerance.
    """
    spec_by_col = {s.column: s for s in spec}
    gen_cols: list[dict[str, Any]] = config_table.get("generate_columns", [])

    for gen_col in gen_cols:
        if not isinstance(gen_col, dict):
            continue
        col_name = gen_col.get("name")
        if not isinstance(col_name, str) or col_name not in output_df.columns:
            continue
        col_spec = spec_by_col.get(col_name)
        tol = col_spec.tolerance if col_spec is not None else 0.05
        col_type = str(gen_col.get("type", ""))

        if col_type == "categorical":
            weights: dict[str, float] = gen_col.get("weights") or {}
            if not weights:
                continue
            weight_sum = sum(float(v) for v in weights.values())
            if weight_sum <= 0:
                continue
            norm_weights = {k: float(v) / weight_sum for k, v in weights.items()}
            n_total = len(output_df)
            if n_total == 0:
                continue
            out_freq = output_df[col_name].value_counts(normalize=True).to_dict()
            all_keys = set(norm_weights) | set(out_freq)
            tvd = 0.5 * sum(
                abs(norm_weights.get(k, 0.0) - float(out_freq.get(k, 0.0))) for k in all_keys
            )
            if tvd > tol:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] generate categorical TVD: "
                    f"tvd={tvd:.4f} > tolerance={tol} "
                    f"(declared weights vs output frequencies). "
                    f"Declared: {norm_weights}. "
                    f"Output: { {k: round(float(v), 4) for k, v in out_freq.items()} }."
                )

        elif col_type == "statistical":
            params: dict[str, Any] = gen_col.get("params") or {}
            declared_mean = params.get("mean")
            declared_std = params.get("std")
            if declared_mean is None or declared_std is None:
                continue
            series = output_df[col_name].dropna()
            if len(series) == 0:
                continue
            out_mean = float(series.mean())
            out_std = float(series.std())
            d_mean = float(declared_mean)
            d_std = float(declared_std)
            mean_band = tol * max(abs(d_mean), 1.0)
            std_band = tol * max(d_std, 1.0)
            if abs(out_mean - d_mean) > mean_band:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] generate statistical mean: "
                    f"out_mean={out_mean:.4f}, declared_mean={d_mean}, "
                    f"band={mean_band:.4f} (tol={tol}). "
                    f"Output mean outside declared parameter band."
                )
            if abs(out_std - d_std) > std_band:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] generate statistical std: "
                    f"out_std={out_std:.4f}, declared_std={d_std}, "
                    f"band={std_band:.4f} (tol={tol}). "
                    f"Output std outside declared parameter band."
                )


# ---------------------------------------------------------------------------
# 6.4 FK integrity
# ---------------------------------------------------------------------------


def check_fk_integrity(
    job_name: str,
    spec: list[FKIntegritySpec],
    result: Any,
    sources: dict[str, Any],
) -> None:
    """Assert FK integrity for all declared relationships.

    For each RelationshipSpec: assert every non-null child key in the masked
    output exists in the parent's masked key set (no orphans except planted
    ones). Checks both the built-in engine validators (fk_intact,
    no_orphan_children via quality_metrics) AND a direct set-membership
    assertion (belt-and-suspenders). Composite tuples are matched as a unit.

    Args:
        job_name: Job name for error messages.
        spec: List of FKIntegritySpec from the manifest invariants.
        result: ExecutionResult (carries outputs and quality_metrics).
        sources: dict[table_name, pa.Table] of source frames.

    Raises:
        AssertionError: If orphan count does not match expected or FK is broken.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_fk_integrity")


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
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_checksums")


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
    - Assert suppressed/generalized row count == planted_restricted_zip3_count.
    - Assert the geo_generalize_cascade QualityWarning is present with the
      expected aggregate count.

    Args:
        job_name: Job name for error messages.
        spec: List of SafeHarborSpec from the manifest invariants.
        result: ExecutionResult carrying masked outputs and quality_metrics warnings.

    Raises:
        AssertionError: If suppression counts mismatch or a restricted ZIP3 leaks.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_safe_harbor")


# ---------------------------------------------------------------------------
# 6.7 Quarantine counts
# ---------------------------------------------------------------------------


def check_quarantine(
    job_name: str,
    spec: QuarantineSpec,
    result: Any,
) -> None:
    """Assert quarantine row count and file presence.

    Asserts:
    - result.quality_metrics["quarantine"].total_quarantined == expected.
    - Main output row count reduced by exactly expected_total_quarantined.
    - JSONL quarantine file exists with matching line count.
    - quality_metrics["validation"]["validators"] reports expected_validator failing.

    Args:
        job_name: Job name for error messages.
        spec: QuarantineSpec from the manifest invariants.
        result: ExecutionResult carrying quality_metrics and output tables.

    Raises:
        AssertionError: If quarantine count mismatches or file is absent.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_quarantine")


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
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_sentinels")


# ---------------------------------------------------------------------------
# 6.9 Computed-column correctness
# ---------------------------------------------------------------------------


def check_computed_columns(
    job_name: str,
    spec: list[ComputedColumnSpec],
    result: Any,
) -> None:
    """Assert derived / case_when / derived_aggregate columns are correct.

    For each ComputedColumnSpec, recomputes the expected value in pure Python
    from the output's input columns and asserts equality. For case_when columns
    with branch_count > 0, also asserts that every branch is exercised by at
    least one output row (branch-coverage guard: an unused branch could hide a
    bug). For derived_aggregate, asserts the single scalar equals the Python
    aggregate of the sibling column broadcast to all rows.

    Args:
        job_name: Job name for error messages.
        spec: List of ComputedColumnSpec from the manifest invariants.
        result: ExecutionResult carrying all output tables.

    Raises:
        AssertionError: If a computed value is wrong or a branch is unexercised.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError("Phase 2: check_computed_columns")


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
