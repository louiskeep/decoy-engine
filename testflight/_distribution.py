"""Distribution-fidelity invariants for the test-flight suite (Phase 1).

Implements the two public entry points from plan sections 6.2 and 6.3:

  check_distribution_mask    -- mask table quality teeth.
  check_distribution_generate -- generate table distribution (config baseline).

Both raise AssertionError on failure with a message naming job/table/column/
strategy so triage localises to one strategy without reading the full report.

FIXED_TS is exported here and re-exported by _invariants so external callers
that import from _invariants continue to work unchanged.

Tooth index (check_distribution_mask):
  A -- constant-collapse guard for every preserve column.
       fpe: exact cardinality (1.0x) + per-column shape similarity >= 0.95.
       hash: near-bijection (0.99x) + per-column shape similarity >= 0.95.
       shuffle: marginal similarity >= 0.99.
       passthrough: value-identity similarity >= 0.98 AND
                    out_nunique >= 0.5 * src_nunique.
       generic-preserve: out_nunique >= 0.5 * src_nunique.
  B -- real-coarsening guard: coarsen columns must strictly reduce cardinality.
       Default derived from distribution_class (coarsen -> expected_coarsening).
  C -- correlation-preservation: declared joint pairs must meet corr_tol.
       Declared-but-uncomputed pair -> hard error (not a silent skip).
  D -- null-rate drift per preserve column + table-level diagnostic (dtype/
       kind-drift, row parity).
  E -- grade/shape floor for preserve-dominant tables.
       fpe/hash present: overall_shape_score >= 0.90 (shape preserved).
       No value-changing strategies: grade in A/B (value-identity preserved).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from decoy_engine.quality.policy import apply_quality_policy
from decoy_engine.quality.report import compute_quality_report

from ._spec import ColumnDistributionSpec

# Fixed timestamp for all compute_quality_report calls so two invocations
# on the same data produce byte-identical report dicts.
FIXED_TS = "2026-05-24T00:00:00+00:00"

# Bijective transforms: every unique source value -> unique output value but
# the output strings/numbers differ. Shape is preserved; value-identity is not.
_CARDINALITY_BIJECTIVE: frozenset[str] = frozenset({"fpe", "hash"})

# Transforms that permute the source values without changing the set.
_MARGINAL_PRESERVING: frozenset[str] = frozenset({"shuffle"})

# Per-column floor constants for Tooth A (HIGH-1).
_PASSTHROUGH_VALUE_IDENTITY_FLOOR: float = 0.98
_FPE_HASH_SHAPE_FLOOR: float = 0.95

# Table-level shape score floor for fpe/hash-bearing preserve-dominant tables (Tooth E).
_SHAPE_FLOOR_FPE_TABLE: float = 0.90

# Fraction of source cardinality that generic-preserve / passthrough columns
# must retain. Catches 2000->2 collapse that the old >=2 floor missed.
_CARDINALITY_FRACTION_FLOOR: float = 0.5


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
    explicit teeth described in the module docstring.

    Args:
        job_name: Job name for error messages.
        table: Table name for error messages.
        spec: ColumnDistributionSpec entries for this table.
        source_df: Pre-mask pandas DataFrame.
        output_df: Post-mask pandas DataFrame.
        strategy_map: Optional caller-provided {column: strategy_name} map.
            Merged with per-column strategy fields from spec; spec entries
            win on conflict.
        policy_config: Optional policy overrides merged into the fail-mode
            config.
        grade_floor_enabled: Whether to apply the grade/shape-floor assertion
            (Tooth E).

    Raises:
        AssertionError: On any distribution invariant failure. The message
            names the specific tooth, job/table/column, and strategy.
    """
    # --- collect declared joint pairs from all spec entries ---
    joint_cols: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for col_spec in spec:
        for pair in col_spec.joint_columns:
            if len(pair) >= 2:
                t: tuple[str, str] = (pair[0], pair[1])
                if t not in seen_pairs:
                    joint_cols.append(t)
                    seen_pairs.add(t)

    # --- MEDIUM-1: validate every declared column exists in both frames ---
    # A typo'd manifest column silently disables all its teeth without this check.
    for col_spec in spec:
        col_name = col_spec.column
        if col_name not in source_df.columns:
            raise AssertionError(
                f"[{job_name}/{table}/{col_name}] declared column absent from "
                f"source_df (present: {list(source_df.columns)}). "
                f"Check manifest or fixture for a typo."
            )
        if col_name not in output_df.columns:
            raise AssertionError(
                f"[{job_name}/{table}/{col_name}] declared column absent from "
                f"output_df (present: {list(output_df.columns)}). "
                f"The pipeline may have dropped or renamed this column."
            )
        for pair in col_spec.joint_columns:
            for member in pair[:2]:
                if member not in source_df.columns:
                    raise AssertionError(
                        f"[{job_name}/{table}] joint pair member {member!r} absent "
                        f"from source_df. Check joint_columns in the spec."
                    )
                if member not in output_df.columns:
                    raise AssertionError(
                        f"[{job_name}/{table}] joint pair member {member!r} absent "
                        f"from output_df. Check joint_columns in the spec."
                    )

    # --- call compute_quality_report with declared joints ---
    report = compute_quality_report(
        source_df,
        output_df,
        joint_columns=joint_cols if joint_cols else None,
        null_drift_threshold_pp=10.0,
        include_shape_fidelity=True,
        now_iso=FIXED_TS,
    )

    # --- MEDIUM-4: table-level diagnostic (dtype/kind-drift + row parity) ---
    # The diagnostic catches structural problems (dtype change, missing columns,
    # row count mismatch) that the distribution teeth cannot see. Assert it
    # before the distribution checks so structural regressions surface clearly.
    diag: dict[str, Any] = report.get("diagnostic") or {}
    if not diag.get("passed", True):
        failed_checks = [c for c in diag.get("checks", []) if not c.get("passed", True)]
        details = "; ".join(c.get("detail", str(c)) for c in failed_checks[:3])
        raise AssertionError(
            f"[{job_name}/{table}] diagnostic failed (dtype-kind drift or row parity): {details}"
        )

    # --- build effective strategy_map (spec entries merged with caller-provided) ---
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

    # --- per-column lookup from the marginal report ---
    col_entry_by_name: dict[str, dict[str, Any]] = {
        c["column"]: c
        for c in report.get("marginal", {}).get("columns", [])
        if isinstance(c, dict) and "column" in c
    }

    # --- per-column shape lookup from the shape_fidelity sub-report ---
    _shape_report: dict[str, Any] = report.get("shape_fidelity") or {}
    shape_col_by_name: dict[str, dict[str, Any]] = {
        c["column"]: c
        for c in (_shape_report.get("marginal") or {}).get("columns", [])
        if isinstance(c, dict) and "column" in c
    }

    # --- MEDIUM-2: multi-column joint requirement (per-column waiver scoping) ---
    # A multi-column preserve table without declared joint pairs means pairwise
    # correlation is never checked (the check is vacuous). Tables must either
    # declare >=1 joint pair or explicitly waive each column's requirement.
    #
    # Per-column scoping (HIGH-1 fix): a waiver on ONE column exempts only THAT
    # column from the requirement, not the whole table. If >= 2 preserve columns
    # are non-waived AND no joint_cols are declared, the requirement fires.
    # Previously, any(s.joints_waived) was used, which let one waived column
    # disable the joint requirement for all other (non-waived) columns.
    preserve_cols_in_spec = [s for s in spec if s.distribution_class == "preserve"]
    non_waived_preserve_cols = [s for s in preserve_cols_in_spec if not s.joints_waived]
    if len(non_waived_preserve_cols) >= 2 and not joint_cols:
        raise AssertionError(
            f"[{job_name}/{table}] multi-column mask table has "
            f"{len(non_waived_preserve_cols)} non-waived preserve columns but "
            f"no joint_columns declared. "
            f"Declare >=1 joint pair to check correlation, or set "
            f"joints_waived=True with a joints_waived_reason on each column spec "
            f"entry where correlation is not required."
        )

    # --- TOOTH A: constant-collapse guard (preserve class) ---
    for col_spec in spec:
        if col_spec.distribution_class != "preserve":
            continue
        col_name = col_spec.column
        strategy = effective_strategy_map.get(col_name)
        src_nunique = int(source_df[col_name].nunique())
        out_nunique = int(output_df[col_name].nunique())

        if strategy == "fpe":
            # FPE is a strict bijection: collisions are bugs (LOW-2).
            if out_nunique < src_nunique:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] constant-collapse guard "
                    f"(strategy=fpe): out_nunique={out_nunique} < "
                    f"src_nunique={src_nunique}. FPE is a strict bijection; "
                    f"any cardinality drop indicates a collision or collapse."
                )
            # HIGH-1: FPE must also preserve the frequency shape of the source.
            # A bijection that scrambles frequencies would produce a
            # shape_similarity below the floor even though cardinality is intact.
            shape_entry = shape_col_by_name.get(col_name)
            shape_sim = (
                shape_entry.get("shape_similarity") if isinstance(shape_entry, dict) else None
            )
            if shape_sim is not None and float(shape_sim) < _FPE_HASH_SHAPE_FLOOR:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] shape-floor guard "
                    f"(strategy=fpe): shape_similarity={shape_sim:.4f} < "
                    f"{_FPE_HASH_SHAPE_FLOOR}. FPE must preserve the frequency "
                    f"shape of the source column."
                )

        elif strategy == "hash":
            # Hash: near-bijection, 1% collision budget.
            floor = 0.99 * src_nunique
            if out_nunique < floor:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] constant-collapse guard "
                    f"(strategy=hash): out_nunique={out_nunique} < "
                    f"0.99 * src_nunique={src_nunique:.1f} (floor={floor:.1f}). "
                    f"Column may have collapsed to a constant."
                )
            # HIGH-1: hash must also preserve frequency shape.
            shape_entry = shape_col_by_name.get(col_name)
            shape_sim = (
                shape_entry.get("shape_similarity") if isinstance(shape_entry, dict) else None
            )
            if shape_sim is not None and float(shape_sim) < _FPE_HASH_SHAPE_FLOOR:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] shape-floor guard "
                    f"(strategy=hash): shape_similarity={shape_sim:.4f} < "
                    f"{_FPE_HASH_SHAPE_FLOOR}. Hash must preserve the frequency "
                    f"shape of the source column."
                )

        elif strategy in _MARGINAL_PRESERVING:
            # Shuffle: same value set + same frequencies; marginal similarity >= 0.99.
            entry = col_entry_by_name.get(col_name)
            sim = entry.get("similarity") if isinstance(entry, dict) else None
            if sim is not None and float(sim) < 0.99:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] constant-collapse guard "
                    f"(strategy=shuffle): marginal_similarity={sim} < 0.99. "
                    f"Shuffle must preserve the marginal distribution exactly."
                )

        elif strategy == "passthrough":
            # HIGH-1: tightened cardinality fraction floor is checked FIRST so that
            # a large collapse (e.g. 2000->2 distinct) surfaces the cardinality
            # tooth rather than the value-identity tooth. A value-shifted column
            # with unchanged cardinality then falls through to the value-identity
            # floor below, which is the correct tooth for that regression.
            if src_nunique > 1:
                cardinality_floor = max(2, int(_CARDINALITY_FRACTION_FLOOR * src_nunique))
                if out_nunique < cardinality_floor:
                    raise AssertionError(
                        f"[{job_name}/{table}/{col_name}] cardinality-fraction guard "
                        f"(strategy=passthrough): out_nunique={out_nunique} < "
                        f"floor={cardinality_floor} "
                        f"(>={_CARDINALITY_FRACTION_FLOOR:.0%} of "
                        f"src_nunique={src_nunique}). Column collapsed from "
                        f"{src_nunique} to {out_nunique} distinct values."
                    )
            # HIGH-1: passthrough must be near-identical to the source.
            # A doubled, shifted, or otherwise altered column fails the
            # value-identity floor even though cardinality may be unchanged.
            entry = col_entry_by_name.get(col_name)
            sim = entry.get("similarity") if isinstance(entry, dict) else None
            if sim is not None and float(sim) < _PASSTHROUGH_VALUE_IDENTITY_FLOOR:
                raise AssertionError(
                    f"[{job_name}/{table}/{col_name}] passthrough value-identity "
                    f"floor: similarity={float(sim):.4f} < "
                    f"{_PASSTHROUGH_VALUE_IDENTITY_FLOOR}. A passthrough column "
                    f"must be near-identical to the source; a doubled, shifted, or "
                    f"otherwise altered column fails this floor."
                )

        else:
            # Generic preserve (date_shift, or unknown): tightened cardinality
            # fraction floor (HIGH-1).
            if src_nunique > 1:
                cardinality_floor = max(2, int(_CARDINALITY_FRACTION_FLOOR * src_nunique))
                if out_nunique < cardinality_floor:
                    raise AssertionError(
                        f"[{job_name}/{table}/{col_name}] cardinality-fraction guard "
                        f"(strategy={strategy!r}): out_nunique={out_nunique} < "
                        f"floor={cardinality_floor} "
                        f"(>={_CARDINALITY_FRACTION_FLOOR:.0%} of "
                        f"src_nunique={src_nunique}). Column collapsed from "
                        f"{src_nunique} to {out_nunique} distinct values."
                    )

    # --- TOOTH B: real-coarsening guard (coarsen class) ---
    for col_spec in spec:
        if col_spec.distribution_class != "coarsen":
            continue
        # MEDIUM-5: derive expected_coarsening from distribution_class when unset.
        # A coarsen column with expected_coarsening=False (the model default) is
        # silently skipped by Tooth B, making it checked by nothing. Coarsen class
        # means coarsening is intended, so treat it as True.
        effective_coarsening = col_spec.expected_coarsening or (
            col_spec.distribution_class == "coarsen"
        )
        if not effective_coarsening:
            continue
        col_name = col_spec.column
        strategy = effective_strategy_map.get(col_name)
        src_nunique = int(source_df[col_name].nunique())
        out_nunique = int(output_df[col_name].nunique())
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
            fwd: tuple[str, str] = (pair[0], pair[1])
            rev: tuple[str, str] = (pair[1], pair[0])
            sim = joint_sims.get(fwd)
            if sim is None:
                sim = joint_sims.get(rev)
            if sim is None:
                # MEDIUM-2: a declared pair that compute_quality_report did not
                # produce a similarity for is a hard error. Both columns were
                # validated to exist (MEDIUM-1 passed), so None here means the
                # joint comparison was not computable (e.g. both columns had no
                # non-null data). Silently continuing would hide the gap.
                raise AssertionError(
                    f"[{job_name}/{table}/{col_spec.column}] declared pair "
                    f"{(pair[0], pair[1])} resolved to sim=None "
                    f"(declared-but-uncomputed). Both columns exist but "
                    f"compute_quality_report did not produce a joint similarity. "
                    f"Check that both columns have non-null, non-degenerate data."
                )
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

    # --- TOOTH E: grade/shape floor (preserve-dominant tables) ---
    if grade_floor_enabled:
        preserve_count = sum(1 for s in spec if s.distribution_class == "preserve")
        coarsen_count = sum(1 for s in spec if s.distribution_class == "coarsen")
        has_value_changing = any(
            effective_strategy_map.get(s.column) in _CARDINALITY_BIJECTIVE
            for s in spec
            if s.distribution_class == "preserve"
        )
        if preserve_count > coarsen_count:
            if has_value_changing:
                # HIGH-1: fpe/hash-bearing tables use a shape floor instead of the
                # value-identity grade. Value-identity TVD scores fpe/hash columns
                # near 0 by design, dragging the overall grade below B even for a
                # correct run. Shape-only score is the correct tooth: a bijection
                # must preserve the frequency distribution shape.
                s_report: dict[str, Any] = report.get("shape_fidelity") or {}
                shape_score = s_report.get("overall_shape_score")
                if shape_score is not None and float(shape_score) < _SHAPE_FLOOR_FPE_TABLE:
                    raise AssertionError(
                        f"[{job_name}/{table}] shape-floor (Tooth E): preserve-dominant "
                        f"table with fpe/hash columns has "
                        f"overall_shape_score={shape_score:.3f} < "
                        f"{_SHAPE_FLOOR_FPE_TABLE}. Bijective transforms must "
                        f"preserve the frequency distribution shape of the table."
                    )
            else:
                # Original grade floor for tables without value-changing strategies.
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

    Args:
        job_name: Job name for error messages.
        table: Table name for error messages.
        spec: ColumnDistributionSpec entries for this table.
        output_df: Post-generate pandas DataFrame.
        config_table: Raw pipeline config dict for this table. Must carry
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
