"""Test-flight mutation-control suite (Phase 1: distribution teeth).

Anti-vacuity: an assertion that cannot fail on a real regression manufactures
false confidence. Each invariant family requires a known-bad mutation control
that applies a specific regression to a fixture and asserts the corresponding
invariant RAISES. This is the engine analogue of scripts/prove_regression.py.

Phase 1 fills in the distribution-fidelity controls (TestDistributionTeeth).
Phase 2+ fills in the remaining families.

Mutation controls per family (plan section 9):
  - Distribution constant-collapse: fpe column collapsed to one value;
    assert constant-collapse guard raises.
  - Fake coarsening: coarsen column left identical to source;
    assert real-coarsening guard raises (cardinality did not drop).
  - Correlation destroyed: declared preserve pair decorrelated;
    assert correlation-preservation guard raises.
  - Self-compare vacuity: prove compute_quality_report(df, df) scores 1.0
    and that the invariant compares source vs output (never output vs output).
  - Good input: a faithfully-masked output (fpe bijection + genuine coarsening
    + preserved correlation) -> invariant PASSES (proves no over-assertion).
  - FK break: Phase 2.
  - Quarantine miscount: Phase 2.
  - Sentinel leak: Phase 2.
  - Computed-column corruption: Phase 2.
  - Coverage rot: Phase 4.

All tests are marked testflight. Run via:
  pytest testflight -m testflight
  python scripts/test_flight.py --mutate <kind>
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from testflight._invariants import (
    FIXED_TS,
    check_distribution_generate,
    check_distribution_mask,
)
from testflight._spec import ColumnDistributionSpec

pytestmark = pytest.mark.testflight


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fpe_bijection(src_ids: list[str]) -> list[str]:
    """Simulate an FPE transform: same cardinality, completely different values."""
    return [f"MASKED_{v}" for v in src_ids]


def _build_correlated_frame(n: int, seed: int = 42) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build source + correlated-output frames for the correlation tests.

    Source: category in {X, Y, Z}, amount deterministic (100/200/300 by
    category). Output: both columns are passthrough (identical to source),
    so joint similarity = 1.0 and all correlation guards pass.
    """
    rng = np.random.default_rng(seed)
    cats = rng.choice(["X", "Y", "Z"], size=n)
    amounts = np.where(cats == "X", 100, np.where(cats == "Y", 200, 300))
    source_df = pd.DataFrame({"category": cats.tolist(), "amount": amounts.tolist()})
    output_df = source_df.copy()
    return source_df, output_df


# ---------------------------------------------------------------------------
# Distribution teeth
# ---------------------------------------------------------------------------


class TestDistributionTeeth:
    """Mutation controls for the distribution-fidelity invariant family.

    Each control constructs source + broken/good DataFrames inline (no full
    pipeline needed) and calls check_distribution_mask directly. The controls
    prove each tooth bites: broken input -> invariant raises; good input ->
    invariant passes.

    Red-before / green-after reasoning is stated per test so reviewers can
    confirm the tooth is catching the right regression class.
    """

    # ------------------------------------------------------------------
    # Tooth A: constant-collapse guard
    # ------------------------------------------------------------------

    def test_constant_collapse_detected(self) -> None:
        """FPE column collapsed to one value must trip the constant-collapse guard.

        RED: output customer_id is a single constant (0). The constant-collapse
        guard requires out_nunique >= src_nunique (exact bijection) for fpe.
        Even though the policy floor for fpe is 0.05 (value-identity TVD near
        0 by design), the cardinality guard catches this independently.

        Integer IDs are used so that collapsing to a single integer does not
        change the column kind (both source and output remain numeric), keeping
        the diagnostic from firing before Tooth A.

        GREEN: test_good_input_passes below shows a correct fpe bijection passes.
        """
        n = 200
        src_ids = list(range(n))  # 0..199 distinct integers
        source_df = pd.DataFrame({"customer_id": src_ids})
        # Broken: fpe collapsed every value to the same integer constant.
        output_df = pd.DataFrame({"customer_id": [0] * n})

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="customer_id",
                distribution_class="preserve",
                strategy="fpe",
                tolerance=0.05,
                joints_waived=True,
                joints_waived_reason="single-column spec, no joint needed",
            ),
        ]
        with pytest.raises(AssertionError, match="constant-collapse"):
            check_distribution_mask(
                "control_job",
                "t",
                spec,
                source_df,
                output_df,
                strategy_map={"customer_id": "fpe"},
            )

    # ------------------------------------------------------------------
    # Tooth B: real-coarsening guard
    # ------------------------------------------------------------------

    def test_fake_coarsening_detected(self) -> None:
        """Coarsen column left identical to source must trip the real-coarsening guard.

        RED: salary_tier in the output is IDENTICAL to source (passthrough
        instead of bucketize). The real-coarsening guard requires
        out_nunique < src_nunique strictly when expected_coarsening=True. With
        identical outputs, out_nunique == src_nunique -> guard fires.

        The value-identity check would actually PASS (output is the source),
        proving the coarsening guard catches a regression the similarity metric
        cannot see.
        """
        rng = np.random.default_rng(42)
        n = 200
        tiers = rng.choice(["tier1", "tier2", "tier3"], size=n).tolist()
        source_df = pd.DataFrame({"salary_tier": tiers})
        # Broken: passthrough instead of bucketize (no coarsening happened).
        output_df = source_df.copy()

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="salary_tier",
                distribution_class="coarsen",
                strategy="bucketize",
                tolerance=0.10,
                expected_coarsening=True,
            ),
        ]
        with pytest.raises(AssertionError, match="real-coarsening"):
            check_distribution_mask(
                "control_job",
                "t",
                spec,
                source_df,
                output_df,
                strategy_map={"salary_tier": "bucketize"},
            )

    # ------------------------------------------------------------------
    # Tooth C: correlation-preservation guard
    # ------------------------------------------------------------------

    def test_correlation_destroyed_detected(self) -> None:
        """Declared preserve pair decorrelated must trip the correlation-preservation guard.

        RED: source has a strong deterministic correlation (amount = 100 when
        category=X, 200 when Y, 300 when Z). The output keeps category intact
        but shuffles amount independently, destroying the correlation. The joint
        TVD similarity between (category, amount) drops well below corr_tol=0.70.

        This proves that declaring joint_columns is NOT vacuous: if we skipped
        declaring the pair, no pairwise check would run and the decorrelation
        would be invisible.
        """
        rng = np.random.default_rng(42)
        n = 300
        cats = rng.choice(["X", "Y", "Z"], size=n)
        # Deterministic amounts (3 distinct values) so the joint distribution
        # has clear structure and TVD is high when decorrelated.
        amounts = np.where(cats == "X", 100, np.where(cats == "Y", 200, 300))
        source_df = pd.DataFrame({"category": cats.tolist(), "amount": amounts.tolist()})
        output_df = source_df.copy()
        # Broken: amount shuffled independently -> no longer correlated with category.
        output_df["amount"] = rng.permutation(amounts).tolist()

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="category",
                distribution_class="preserve",
                strategy="passthrough",
                tolerance=0.05,
                joint_columns=[["category", "amount"]],
                corr_tol=0.70,
            ),
            ColumnDistributionSpec(
                table="t",
                column="amount",
                distribution_class="preserve",
                strategy="passthrough",
                tolerance=0.05,
            ),
        ]
        with pytest.raises(AssertionError, match="correlation-preservation"):
            check_distribution_mask(
                "control_job",
                "t",
                spec,
                source_df,
                output_df,
                strategy_map={"category": "passthrough", "amount": "passthrough"},
            )

    # ------------------------------------------------------------------
    # Self-compare vacuity control
    # ------------------------------------------------------------------

    def test_self_compare_vacuity(self) -> None:
        """Prove compute_quality_report(df, df) scores 1.0 AND the invariant
        compares source_df vs output_df (never output vs output).

        Section 9.B construction rule: a harness that self-compares always
        passes even on broken output, manufacturing false confidence.

        This control shows:
        1. compute_quality_report(df, df) gives overall_score=1.0 (grade A),
           confirming self-comparison is vacuous.
        2. check_distribution_mask(source_df, broken_output) RAISES because it
           compares the two DIFFERENT frames, not the output against itself.
           A self-comparing implementation would pass step 2 vacuously.
        """
        from decoy_engine.quality.report import compute_quality_report

        rng = np.random.default_rng(7)
        n = 100
        df = pd.DataFrame(
            {
                "x": rng.integers(0, 10, size=n).tolist(),
            }
        )
        # Vacuity proof: self-comparison always scores 1.0.
        self_report = compute_quality_report(df, df, now_iso=FIXED_TS)
        assert self_report["overall_score"] == 1.0, (
            f"Self-compare must score 1.0; got {self_report['overall_score']}"
        )
        assert self_report["grade"] == "A"

        # Anti-vacuity proof: the invariant compares source_df vs broken_output.
        # If the invariant self-compared, it would pass vacuously because
        # broken_output vs broken_output scores 1.0. But it does NOT self-compare,
        # so the constant-collapse guard catches the broken output.
        broken_output = pd.DataFrame({"x": [0] * n})  # collapsed to constant
        spec = [
            ColumnDistributionSpec(
                table="t",
                column="x",
                distribution_class="preserve",
                strategy="fpe",
                tolerance=0.05,
            ),
        ]
        # This must raise because source_df has 10 unique values but broken_output
        # has 1. A self-comparing harness would NOT raise here.
        with pytest.raises(AssertionError, match="constant-collapse"):
            check_distribution_mask(
                "control_job",
                "t",
                spec,
                df,
                broken_output,
                strategy_map={"x": "fpe"},
            )

    # ------------------------------------------------------------------
    # Good-input: no over-assertion
    # ------------------------------------------------------------------

    def test_good_input_passes(self) -> None:
        """A faithfully-masked output passes all distribution teeth.

        Proves the invariant does NOT over-assert. The output contains:
        - customer_id: fpe-like bijection (same cardinality, different values).
          Tooth A passes (out_nunique == src_nunique).
        - category, amount: passthrough (identical to source), with a declared
          joint pair. Tooth C passes (joint similarity = 1.0 >= corr_tol).
        - salary_tier: genuine coarsening (3 source tiers collapsed to 2).
          Tooth B passes (out_nunique=2 < src_nunique=3).
        - Tooth D: no nulls -> drift = 0 <= null_pp.
        - Tooth E: skipped because customer_id uses fpe (value-changing) ->
          grade floor not enforced for tables with bijective-preserve columns.
        - Policy: fpe/passthrough not in strategy expectations; policy skips
          all columns (no violations -> verdict=pass).

        All teeth pass; check_distribution_mask must NOT raise.
        """
        rng = np.random.default_rng(99)
        n = 300
        cats = rng.choice(["X", "Y", "Z"], size=n)
        amounts = np.where(cats == "X", 100, np.where(cats == "Y", 200, 300))
        src_ids = [f"ID{i:04d}" for i in range(n)]
        # salary_tier: 3 source tiers
        src_tiers = rng.choice(["tier1", "tier2", "tier3"], size=n).tolist()

        source_df = pd.DataFrame(
            {
                "customer_id": src_ids,
                "category": cats.tolist(),
                "amount": amounts.tolist(),
                "salary_tier": src_tiers,
            }
        )

        # Good output:
        # - customer_id: bijection (different values, same cardinality)
        out_ids = _fpe_bijection(src_ids)
        # - category + amount: passthrough (correlation preserved)
        # - salary_tier: coarsened to 2 tiers (tier3 -> "upper", others -> "lower")
        out_tiers = ["upper" if t == "tier3" else "lower" for t in src_tiers]

        output_df = pd.DataFrame(
            {
                "customer_id": out_ids,
                "category": cats.tolist(),
                "amount": amounts.tolist(),
                "salary_tier": out_tiers,
            }
        )

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="customer_id",
                distribution_class="preserve",
                strategy="fpe",
                tolerance=0.05,
                # no joint_columns: only cardinality guard applies
            ),
            ColumnDistributionSpec(
                table="t",
                column="category",
                distribution_class="preserve",
                strategy="passthrough",
                tolerance=0.05,
                joint_columns=[["category", "amount"]],
                corr_tol=0.80,
            ),
            ColumnDistributionSpec(
                table="t",
                column="amount",
                distribution_class="preserve",
                strategy="passthrough",
                tolerance=0.05,
            ),
            ColumnDistributionSpec(
                table="t",
                column="salary_tier",
                distribution_class="coarsen",
                # strategy not set -> not added to effective_strategy_map
                # -> policy does not check salary_tier value-identity
                # (coarsen tooth B still runs from expected_coarsening=True)
                tolerance=0.30,
                expected_coarsening=True,
            ),
        ]
        # Must NOT raise: all teeth pass on this correctly-masked output.
        check_distribution_mask(
            "control_job",
            "t",
            spec,
            source_df,
            output_df,
            strategy_map={
                "customer_id": "fpe",
                "category": "passthrough",
                "amount": "passthrough",
                # salary_tier deliberately omitted: coarsen tooth runs via spec,
                # policy skips (no strategy in effective_strategy_map for that col)
            },
        )


# ---------------------------------------------------------------------------
# Phase 2+ stubs (FK, quarantine, sentinel, computed-column, coverage)
# ---------------------------------------------------------------------------


class TestFKIntegrityTeeth:
    """Mutation controls for the FK integrity invariant family."""

    def test_fk_break_detected(self) -> None:
        """Deleting a relationship must trip the FK-integrity invariant.

        Phase 2: build a job config without a declared relationship, run the
        pipeline, and assert check_fk_integrity raises AssertionError with the
        expected vs found orphan count differing.
        """
        pytest.skip("Phase 2: FK-break mutation control pending.")


class TestQuarantineTeeth:
    """Mutation controls for the quarantine invariant family."""

    def test_quarantine_miscount_detected(self) -> None:
        """Injecting an extra bad row not in the manifest must trip the quarantine
        count invariant.

        Phase 2: plant one more invalid-checksum row than expected_total_quarantined
        in the manifest declares, run the pipeline, and assert check_quarantine
        raises AssertionError with the count mismatch.
        """
        pytest.skip("Phase 2: quarantine-miscount mutation control pending.")


class TestSentinelTeeth:
    """Mutation controls for the sentinel no-leakage invariant family."""

    def test_sentinel_leak_detected(self) -> None:
        """Setting a masked column to passthrough must trip the sentinel scan.

        Phase 2: modify a job config so a column carrying a sentinel SSN string
        uses passthrough instead of fpe, run the pipeline, and assert
        check_sentinels raises AssertionError naming the output column and the
        leaked sentinel value.
        """
        pytest.skip("Phase 2: sentinel-leak mutation control pending.")


class TestComputedColumnTeeth:
    """Mutation controls for the computed-column correctness invariant family."""

    def test_formula_corruption_detected(self) -> None:
        """Perturbing a derived formula must trip the computed-column invariant.

        Phase 2: modify a derived column expression so its output is off by one
        (e.g. multiply by units + 1 instead of units), run the pipeline, and assert
        check_computed_columns raises AssertionError naming the affected column.
        """
        pytest.skip("Phase 2: formula-corruption mutation control pending.")


class TestCoverageRotTeeth:
    """Mutation controls for the strategy-coverage guard."""

    def test_coverage_rot_detected(self) -> None:
        """Registering a fake strategy key must trip the coverage guard.

        Phase 4: monkeypatch SCALAR_HANDLERS to add a fake key not in any
        manifest's strategy_coverage, then assert check_strategy_coverage raises
        AssertionError identifying the uncovered strategy. This proves the guard
        reads the LIVE registry rather than a static list.
        """
        pytest.skip("Phase 4: coverage-rot mutation control pending.")


# ---------------------------------------------------------------------------
# HIGH-1: vacuity hole closed -- fpe-table + broken passthrough amount
# ---------------------------------------------------------------------------


class TestHighOneVacuityHole:
    """Prove the HIGH-1 vacuity hole is closed.

    The hole: any fpe/hash column in a preserve table caused the whole grade
    floor (Tooth E) to be skipped. A passthrough column on the same table
    could be DOUBLED, COLLAPSED, or VARIANCE-FLATTENED without detection.

    These controls build an fpe-bearing table (fpe id column + passthrough
    numeric amount column) and inject each broken-amount pattern. Each
    broken case must now RAISE; the good case must NOT raise.

    Tooth A catches each broken case:
    - DOUBLED amount: per-column passthrough value-identity floor (similarity
      < 0.98 for a doubled numeric).
    - COLLAPSED amount (200->2 distinct): cardinality-fraction floor
      (2 < 0.5 * 200 = 100).
    - FLATTENED amount (all same value): cardinality-fraction floor
      (1 < 0.5 * 200 = 100).
    """

    def _build_base(
        self, n: int = 200, seed: int = 77
    ) -> tuple[pd.DataFrame, list[ColumnDistributionSpec]]:
        """Return (source_df, spec) for the fpe+passthrough table."""
        rng = np.random.default_rng(seed)
        src_ids = [f"ID{i:04d}" for i in range(n)]
        # Each row has a distinct float amount so src_nunique = n.
        src_amounts = rng.integers(1, 1000, size=n).astype(float).tolist()
        source_df = pd.DataFrame({"id": src_ids, "amount": src_amounts})
        spec = [
            ColumnDistributionSpec(
                table="t",
                column="id",
                distribution_class="preserve",
                strategy="fpe",
                # joints_waived: id and amount have no meaningful correlation
                # (id is an opaque bijective-masked key; amount is passthrough).
                joints_waived=True,
                joints_waived_reason=(
                    "fpe-masked id has no meaningful correlation with passthrough "
                    "amount; cardinality and shape guards cover id, value-identity "
                    "floor covers amount"
                ),
            ),
            ColumnDistributionSpec(
                table="t",
                column="amount",
                distribution_class="preserve",
                strategy="passthrough",
            ),
        ]
        return source_df, spec

    def test_doubled_amount_raises(self) -> None:
        """Doubling a passthrough amount column on an fpe table must raise.

        RED (pre-fix): Tooth E skipped the grade floor because id uses fpe.
        No other tooth checked the passthrough amount column for value change.
        A 2x-inflation bug shipped GREEN.

        GREEN (post-fix): Tooth A's passthrough value-identity floor requires
        similarity >= 0.98. A doubled numeric column scores far below 0.98 on
        the quantile-RMSE similarity metric.

        This is the central HIGH-1 proof: the hole is now closed.
        """
        source_df, spec = self._build_base()
        output_df = source_df.copy()
        # Inject bug: amount doubled (all values * 2, same cardinality, different values).
        output_df["amount"] = output_df["amount"] * 2
        # Also apply fpe-like bijection on id so it looks correct.
        output_df["id"] = [f"MASKED_{v}" for v in source_df["id"]]

        with pytest.raises(AssertionError, match="passthrough value-identity floor"):
            check_distribution_mask(
                "high1_control",
                "t",
                spec,
                source_df,
                output_df,
                strategy_map={"id": "fpe", "amount": "passthrough"},
            )

    def test_collapsed_amount_raises(self) -> None:
        """Collapsing a passthrough amount from 200->2 distinct values must raise.

        RED (pre-fix): the generic-preserve cardinality floor required only
        out_nunique >= 2. A collapse from 200 to 2 passed this floor silently.

        GREEN (post-fix): Tooth A's cardinality-fraction floor requires
        out_nunique >= 0.5 * src_nunique. With src_nunique=200, the floor is
        100, so out_nunique=2 trips it.
        """
        source_df, spec = self._build_base()
        output_df = source_df.copy()
        # Inject bug: amount collapsed to just {0.0, 1.0} (2 distinct values).
        output_df["amount"] = [0.0 if i % 2 == 0 else 1.0 for i in range(len(source_df))]
        output_df["id"] = [f"MASKED_{v}" for v in source_df["id"]]

        with pytest.raises(AssertionError, match="cardinality-fraction guard"):
            check_distribution_mask(
                "high1_control",
                "t",
                spec,
                source_df,
                output_df,
                strategy_map={"id": "fpe", "amount": "passthrough"},
            )

    def test_variance_flattened_amount_raises(self) -> None:
        """Flattening a passthrough amount column (all same value) must raise.

        RED (pre-fix): the generic-preserve guard required out_nunique >= 2.
        With out_nunique=1, the guard fired, but ONLY for the case where
        src_nunique > 1 -- AND the early-skip on fpe presence in Tooth E
        bypassed the check entirely in older code paths. With the tightened
        fraction floor, this cannot escape.

        GREEN (post-fix): out_nunique=1 < 0.5 * src_nunique (100 minimum),
        so the cardinality-fraction floor fires clearly.
        """
        source_df, spec = self._build_base()
        output_df = source_df.copy()
        # Inject bug: amount flattened to a single constant (variance = 0).
        output_df["amount"] = 500.0
        output_df["id"] = [f"MASKED_{v}" for v in source_df["id"]]

        with pytest.raises(AssertionError, match="cardinality-fraction guard"):
            check_distribution_mask(
                "high1_control",
                "t",
                spec,
                source_df,
                output_df,
                strategy_map={"id": "fpe", "amount": "passthrough"},
            )

    def test_good_fpe_table_passes(self) -> None:
        """A faithfully-masked fpe table with passthrough amount must NOT raise.

        Proves the HIGH-1 fix does not over-assert. The output has:
        - id: fpe bijection (same cardinality, different values, same shape).
          Tooth A: cardinality 200 == 200 (exact bijection, passes). Shape
          similarity 1.0 >= 0.95 (passes).
        - amount: passthrough (identical to source). Tooth A: similarity 1.0
          >= 0.98 (passes); cardinality 200 >= 100 (passes).
        - Tooth E (shape floor): overall_shape_score for id (1.0) + amount
          (1.0) = 1.0 >= 0.90 (passes).
        - MEDIUM-2: joints_waived=True on id column (explicit opt-out).
        """
        source_df, spec = self._build_base()
        output_df = source_df.copy()
        # Correct fpe bijection: same cardinality, completely different values.
        output_df["id"] = [f"MASKED_{v}" for v in source_df["id"]]
        # amount: passthrough (identical to source)

        # Must NOT raise on a correct output.
        check_distribution_mask(
            "high1_control",
            "t",
            spec,
            source_df,
            output_df,
            strategy_map={"id": "fpe", "amount": "passthrough"},
        )


# ---------------------------------------------------------------------------
# MEDIUM-1: declared-but-absent column -> RAISES
# ---------------------------------------------------------------------------


class TestMediumOneMissingColumn:
    """Mutation controls for the declared-but-absent column check (MEDIUM-1).

    A typo'd manifest column silently disables all its teeth because the old
    code did `if col not in df: continue`. The fix asserts column existence
    before any teeth run.
    """

    def test_missing_column_in_source_raises(self) -> None:
        """A spec column absent from source_df must raise before any teeth run.

        RED (pre-fix): the old `if col not in df: continue` silently disabled
        all teeth for the missing column. A typo in the manifest went unnoticed.

        GREEN (post-fix): MEDIUM-1 asserts column existence and raises with a
        clear message naming the missing column.
        """
        source_df = pd.DataFrame({"x": [1, 2, 3]})
        output_df = pd.DataFrame({"x": [1, 2, 3], "amount": [10.0, 20.0, 30.0]})

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="amount",  # "amount" does NOT exist in source_df
                distribution_class="preserve",
                strategy="passthrough",
                joints_waived=True,
                joints_waived_reason="single-column spec, no joint needed",
            ),
        ]
        with pytest.raises(AssertionError, match="absent from source_df"):
            check_distribution_mask("medium1_control", "t", spec, source_df, output_df)

    def test_missing_column_in_output_raises(self) -> None:
        """A spec column absent from output_df must raise before any teeth run."""
        source_df = pd.DataFrame({"amount": [10.0, 20.0, 30.0]})
        output_df = pd.DataFrame({"x": [1, 2, 3]})  # "amount" dropped from output

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="amount",
                distribution_class="preserve",
                strategy="passthrough",
                joints_waived=True,
                joints_waived_reason="single-column spec, no joint needed",
            ),
        ]
        with pytest.raises(AssertionError, match="absent from output_df"):
            check_distribution_mask("medium1_control", "t", spec, source_df, output_df)


# ---------------------------------------------------------------------------
# MEDIUM-2: joint_columns mandatory; declared-but-uncomputed pair -> RAISES
# ---------------------------------------------------------------------------


class TestMediumTwoJointMandatory:
    """Mutation controls for the joint-pair mandate and uncomputed-pair error.

    Two invariants:
    (a) Multi-column preserve table without joint_columns and no joints_waived
        -> RAISES (correlation check would be vacuously absent).
    (b) Declared pair that compute_quality_report cannot compute (sim=None)
        -> RAISES instead of silently continuing.
    """

    def test_no_joints_declared_raises(self) -> None:
        """Multi-column preserve table with no joint pair declared must raise.

        RED (pre-fix): Tooth C only ran for declared joint pairs; with no pairs,
        pairwise correlation was never checked. The invariant was vacuous.

        GREEN (post-fix): MEDIUM-2 checks that multi-column preserve tables
        declare >=1 joint pair or set joints_waived=True. Without either,
        the assertion fires before any teeth.
        """
        rng = np.random.default_rng(42)
        n = 100
        source_df = pd.DataFrame(
            {
                "x": rng.choice(["A", "B", "C"], size=n).tolist(),
                "y": rng.integers(0, 10, size=n).tolist(),
            }
        )
        output_df = source_df.copy()

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="x",
                distribution_class="preserve",
                strategy="passthrough",
                # No joint_columns declared, no joints_waived
            ),
            ColumnDistributionSpec(
                table="t",
                column="y",
                distribution_class="preserve",
                strategy="passthrough",
            ),
        ]
        with pytest.raises(AssertionError, match="no joint_columns"):
            check_distribution_mask(
                "medium2_control",
                "t",
                spec,
                source_df,
                output_df,
                strategy_map={"x": "passthrough", "y": "passthrough"},
            )

    def test_joints_waived_skips_requirement(self) -> None:
        """Setting joints_waived=True on any column entry bypasses the mandate.

        Proves the opt-out works: the same two-column preserve table with
        joints_waived=True must NOT raise due to the joint requirement.
        (Other teeth may still raise; this test uses passthrough=source so
        all teeth pass.)
        """
        rng = np.random.default_rng(42)
        n = 100
        source_df = pd.DataFrame(
            {
                "x": rng.choice(["A", "B", "C"], size=n).tolist(),
                "y": rng.integers(0, 10, size=n).tolist(),
            }
        )
        output_df = source_df.copy()

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="x",
                distribution_class="preserve",
                strategy="passthrough",
                joints_waived=True,
                joints_waived_reason="unit-test control: single-column-like independence",
            ),
            ColumnDistributionSpec(
                table="t",
                column="y",
                distribution_class="preserve",
                strategy="passthrough",
            ),
        ]
        # Must NOT raise due to the joint requirement; all other teeth pass
        # because output_df is identical to source_df.
        check_distribution_mask(
            "medium2_control",
            "t",
            spec,
            source_df,
            output_df,
            strategy_map={"x": "passthrough", "y": "passthrough"},
        )

    def test_joints_waived_without_reason_rejected(self) -> None:
        """joints_waived=True with no reason must be REJECTED at construction.

        RED (pre-fix): the docstring promised a reason was required, but no
        validator enforced it, so a manifest could silence the correlation tooth
        with reason=None. The model_validator now makes the docstring true.
        """
        with pytest.raises(ValidationError, match="joints_waived_reason"):
            ColumnDistributionSpec(
                table="t",
                column="x",
                distribution_class="preserve",
                strategy="passthrough",
                joints_waived=True,
                joints_waived_reason=None,
            )
        # blank-after-strip is also rejected (not just None).
        with pytest.raises(ValidationError, match="joints_waived_reason"):
            ColumnDistributionSpec(
                table="t",
                column="x",
                distribution_class="preserve",
                strategy="passthrough",
                joints_waived=True,
                joints_waived_reason="   ",
            )

    def test_declared_pair_uncomputed_raises(self) -> None:
        """A declared joint pair that compute_quality_report cannot compute must raise.

        RED (pre-fix): Tooth C had `if sim is None: continue`, so a pair that
        produced no similarity (degenerate data) silently passed.

        GREEN (post-fix): Tooth C raises when sim is None for a declared pair.

        Mechanism: source_df has column "x" with all NaN values. The crosstab
        of (x, y) has no non-null rows, so compute_quality_report returns
        sim=None for that joint. MEDIUM-1 passes because both columns exist.
        Tooth C then raises for the declared-but-uncomputed pair.
        """
        n = 50
        # x is all-NaN: crosstab of (x, y) is empty -> sim=None
        source_df = pd.DataFrame(
            {
                "x": [float("nan")] * n,
                "y": list(range(n)),
            }
        )
        output_df = source_df.copy()

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="x",
                distribution_class="preserve",
                strategy="passthrough",
                joint_columns=[["x", "y"]],
                corr_tol=0.90,
            ),
            ColumnDistributionSpec(
                table="t",
                column="y",
                distribution_class="preserve",
                strategy="passthrough",
            ),
        ]
        with pytest.raises(AssertionError, match="declared pair.*uncomputed|sim=None"):
            check_distribution_mask(
                "medium2_control",
                "t",
                spec,
                source_df,
                output_df,
                strategy_map={"x": "passthrough", "y": "passthrough"},
            )


# ---------------------------------------------------------------------------
# MEDIUM-3: check_distribution_generate controls
# ---------------------------------------------------------------------------


class TestMediumThreeGenerateControls:
    """Mutation controls for check_distribution_generate (MEDIUM-3).

    The function was implemented but had zero controls. These controls prove
    that TVD over tolerance raises for skewed categorical output and that
    mean/std band breach raises for shifted statistical output.
    """

    def test_skewed_categorical_raises(self) -> None:
        """Output all-A when weights are {A:0.5, B:0.3, C:0.2} must raise.

        TVD = 0.5 * (|1.0-0.5| + |0.0-0.3| + |0.0-0.2|) = 0.5 (>> tol=0.05).
        """
        config_table: dict[str, Any] = {
            "generate_columns": [
                {
                    "name": "event_type",
                    "type": "categorical",
                    "weights": {"A": 0.5, "B": 0.3, "C": 0.2},
                }
            ]
        }
        output_df = pd.DataFrame({"event_type": ["A"] * 200})
        spec = [
            ColumnDistributionSpec(
                table="t",
                column="event_type",
                distribution_class="synthetic",
                tolerance=0.05,
            )
        ]
        with pytest.raises(AssertionError, match="generate categorical TVD"):
            check_distribution_generate("gen_job", "t", spec, output_df, config_table)

    def test_shifted_statistical_mean_raises(self) -> None:
        """Output mean=200 vs declared mean=100 must raise (band=5.0 << 100).

        |200 - 100| = 100 > tol * max(|100|, 1.0) = 0.05 * 100 = 5.0.
        """
        rng = np.random.default_rng(44)
        config_table = {
            "generate_columns": [
                {
                    "name": "amount",
                    "type": "statistical",
                    "params": {"mean": 100.0, "std": 10.0},
                }
            ]
        }
        # Output shifted: mean ≈ 200, far outside the 5.0 band.
        output_df = pd.DataFrame({"amount": rng.normal(200.0, 10.0, size=500).tolist()})
        spec = [
            ColumnDistributionSpec(
                table="t",
                column="amount",
                distribution_class="synthetic",
                tolerance=0.05,
            )
        ]
        with pytest.raises(AssertionError, match="generate statistical mean"):
            check_distribution_generate("gen_job", "t", spec, output_df, config_table)

    def test_good_generate_output_passes(self) -> None:
        """Output matching declared weights and params must NOT raise.

        Proves check_distribution_generate does not over-assert. With n=1000
        and seeded sampling, TVD and mean/std are within tolerance.
        """
        rng = np.random.default_rng(55)
        n = 1000
        cats = rng.choice(["A", "B", "C"], size=n, p=[0.5, 0.3, 0.2]).tolist()
        amounts = rng.normal(100.0, 10.0, size=n).tolist()
        output_df = pd.DataFrame({"event_type": cats, "amount": amounts})

        config_table = {
            "generate_columns": [
                {
                    "name": "event_type",
                    "type": "categorical",
                    "weights": {"A": 0.5, "B": 0.3, "C": 0.2},
                },
                {
                    "name": "amount",
                    "type": "statistical",
                    "params": {"mean": 100.0, "std": 10.0},
                },
            ]
        }
        spec = [
            ColumnDistributionSpec(
                table="t",
                column="event_type",
                distribution_class="synthetic",
                tolerance=0.05,
            ),
            ColumnDistributionSpec(
                table="t",
                column="amount",
                distribution_class="synthetic",
                tolerance=0.05,
            ),
        ]
        # Must NOT raise: output matches declared distribution within tolerance.
        check_distribution_generate("gen_job", "t", spec, output_df, config_table)


# ---------------------------------------------------------------------------
# MEDIUM-4: diagnostic check catches dtype/kind-drift
# ---------------------------------------------------------------------------


class TestMediumFourDiagnostic:
    """Mutation controls for the diagnostic check (MEDIUM-4).

    Tooth D was missing the table-level diagnostic assertion. The diagnostic
    catches dtype/kind-drift and row-count parity problems that the distribution
    teeth cannot see (they work on per-column statistics, not metadata).
    """

    def test_dtype_change_raises_via_diagnostic(self) -> None:
        """A column changing dtype from int to str must raise via the diagnostic.

        RED (pre-fix): Tooth D only checked per-column null drift. A column
        changing from numeric to string (kind drift) was not caught; it silently
        skipped the numeric similarity computation.

        GREEN (post-fix): MEDIUM-4 asserts report["diagnostic"]["passed"] early
        in check_distribution_mask. The diagnostic's kind-drift check fires for
        the int->str change, making passed=False, which triggers the assertion.
        """
        rng = np.random.default_rng(66)
        n = 100
        amounts = rng.integers(1, 500, size=n).tolist()
        source_df = pd.DataFrame({"amount": amounts})
        # Broken: output has amount as string instead of int (dtype change).
        output_df = pd.DataFrame({"amount": [str(v) for v in amounts]})

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="amount",
                distribution_class="preserve",
                strategy="passthrough",
                joints_waived=True,
                joints_waived_reason="single-column spec, no joint needed",
            ),
        ]
        with pytest.raises(AssertionError, match="diagnostic failed"):
            check_distribution_mask(
                "medium4_control",
                "t",
                spec,
                source_df,
                output_df,
                strategy_map={"amount": "passthrough"},
            )


# ---------------------------------------------------------------------------
# MEDIUM-5: coarsen class implies expected_coarsening
# ---------------------------------------------------------------------------


class TestMediumFiveCoarsenDefault:
    """Mutation controls for coarsening-default derivation (MEDIUM-5).

    A coarsen column with expected_coarsening unset (default False) was silently
    skipped by Tooth B. The fix derives expected_coarsening from distribution_class
    so that all coarsen columns are checked by Tooth B regardless of the field.
    """

    def test_coarsen_column_without_explicit_flag_raises(self) -> None:
        """A coarsen column without expected_coarsening=True must still raise.

        RED (pre-fix): Tooth B had `if not col_spec.expected_coarsening: continue`.
        A coarsen column with the default (False) was invisible to Tooth B.

        GREEN (post-fix): effective_coarsening = col_spec.expected_coarsening or
        (col_spec.distribution_class == "coarsen"). For a coarsen column with
        expected_coarsening=False, effective_coarsening = True, so Tooth B runs.
        With out_nunique == src_nunique (no coarsening happened), it raises.
        """
        rng = np.random.default_rng(42)
        n = 150
        tiers = rng.choice(["low", "mid", "high", "premium", "elite"], size=n).tolist()
        source_df = pd.DataFrame({"salary_tier": tiers})
        # Broken: passthrough instead of bucketize (no coarsening).
        output_df = source_df.copy()

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="salary_tier",
                distribution_class="coarsen",
                strategy="bucketize",
                tolerance=0.10,
                # expected_coarsening NOT set (defaults to False).
                # Post-fix: Tooth B still fires because distribution_class="coarsen".
            ),
        ]
        with pytest.raises(AssertionError, match="real-coarsening"):
            check_distribution_mask(
                "medium5_control",
                "t",
                spec,
                source_df,
                output_df,
                strategy_map={"salary_tier": "bucketize"},
            )


# ---------------------------------------------------------------------------
# LOW-2: fpe exact bijection floor (1.0x, not 0.99x)
# ---------------------------------------------------------------------------


class TestLowTwoFpeExactBijection:
    """Mutation controls for the fpe exact-bijection cardinality floor (LOW-2).

    FPE is a strict bijection: any collision (out_nunique < src_nunique) is a
    bug. The old floor was 0.99x (1% collision budget); fpe now requires 1.0x.
    """

    def test_fpe_single_collision_raises(self) -> None:
        """A single fpe collision (299/300 unique values) must raise.

        OLD check: 299 >= 0.99 * 300 = 297 -> passes (collision hidden).
        NEW check (LOW-2): 299 < 300 -> raises.
        """
        n = 300
        src_ids = [f"ID{i:04d}" for i in range(n)]
        # Broken: 299 unique output values (one collision: two source IDs map to
        # the same masked value).
        out_ids = [f"MASKED_{i}" for i in range(n - 1)] + ["MASKED_0"]  # duplicate 0

        source_df = pd.DataFrame({"id": src_ids})
        output_df = pd.DataFrame({"id": out_ids})

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="id",
                distribution_class="preserve",
                strategy="fpe",
                joints_waived=True,
                joints_waived_reason="single-column spec, no joint needed",
            ),
        ]
        with pytest.raises(AssertionError, match="constant-collapse guard.*fpe"):
            check_distribution_mask(
                "low2_control",
                "t",
                spec,
                source_df,
                output_df,
                strategy_map={"id": "fpe"},
            )


# ---------------------------------------------------------------------------
# LOW-3: correlation controls at default corr_tol=0.90
# ---------------------------------------------------------------------------


class TestLowThreeCorrelationTol:
    """Correlation controls at the tighter DEFAULT corr_tol=0.90 (LOW-3).

    Also notes the crosstab-TVD limitation for continuous high-cardinality joints.
    """

    def test_correlation_destroyed_at_default_tol(self) -> None:
        """Decorrelation detected at the DEFAULT corr_tol=0.90.

        The existing test_correlation_destroyed_detected uses corr_tol=0.70
        (a caller-relaxed tolerance). This control uses the default 0.90 and
        proves the tooth still bites on the same regression.

        Setup: same as test_correlation_destroyed_detected (amount = f(category))
        but with corr_tol=0.90 (the spec default). After decorrelating amount,
        joint TVD similarity should be well below 0.90.
        """
        rng = np.random.default_rng(42)
        n = 300
        cats = rng.choice(["X", "Y", "Z"], size=n)
        amounts = np.where(cats == "X", 100, np.where(cats == "Y", 200, 300))
        source_df = pd.DataFrame({"category": cats.tolist(), "amount": amounts.tolist()})
        output_df = source_df.copy()
        # Inject: amount shuffled independently (correlation destroyed).
        output_df["amount"] = rng.permutation(amounts).tolist()

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="category",
                distribution_class="preserve",
                strategy="passthrough",
                joint_columns=[["category", "amount"]],
                corr_tol=0.90,  # default -- tighter than the 0.70 test
            ),
            ColumnDistributionSpec(
                table="t",
                column="amount",
                distribution_class="preserve",
                strategy="passthrough",
            ),
        ]
        with pytest.raises(AssertionError, match="correlation-preservation"):
            check_distribution_mask(
                "low3_control",
                "t",
                spec,
                source_df,
                output_df,
                strategy_map={"category": "passthrough", "amount": "passthrough"},
            )

    def test_continuous_high_cardinality_joint_note(self) -> None:
        """Document crosstab-TVD limitation for continuous high-cardinality joints.

        The quality module uses top-25-cell crosstab TVD for pairwise similarity.
        For continuous columns where every (x, y) pair is unique (each appearing
        with frequency 1/n), the top-25 cells of the source and a decorrelated
        output are both near-uniform at ~1/n per cell. TVD then approaches zero
        even for a fully decorrelated pair.

        This test does NOT assert a raise. It documents the known metric limitation:
        for continuous high-cardinality joints, TVD is too coarse to reliably
        distinguish correlated from decorrelated. Use bucketed or low-cardinality
        encodings for correlation testing with this invariant family.

        Triage note: if this test begins raising unexpectedly after a metric update,
        remove the pass-case assertion and replace with match="correlation".
        """
        rng = np.random.default_rng(99)
        n = 200
        # Strongly correlated: y = x + tiny noise (nearly perfect linear relation).
        x_vals = rng.uniform(0.0, 1.0, size=n)
        y_corr = x_vals + rng.normal(0.0, 0.005, size=n)
        # Decorrelated output: y replaced with independent uniform noise.
        y_decor = rng.uniform(0.0, 1.0, size=n)

        source_df = pd.DataFrame({"x": x_vals.tolist(), "y": y_corr.tolist()})
        output_df = pd.DataFrame({"x": x_vals.tolist(), "y": y_decor.tolist()})

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="x",
                distribution_class="preserve",
                strategy="passthrough",
                joint_columns=[["x", "y"]],
                corr_tol=0.90,
            ),
            ColumnDistributionSpec(
                table="t",
                column="y",
                distribution_class="preserve",
                strategy="passthrough",
            ),
        ]
        # The top-25-cell TVD may not discriminate for n=200 continuous floats.
        # We capture whichever outcome occurs without asserting a specific result.
        try:
            check_distribution_mask(
                "low3_continuous_note",
                "t",
                spec,
                source_df,
                output_df,
                strategy_map={"x": "passthrough", "y": "passthrough"},
            )
            # If no raise: TVD was too coarse to see the decorrelation.
            # This is the documented limitation for continuous high-cardinality joints.
        except AssertionError:
            # If raise: TVD discriminated even for continuous floats at this n.
            # Either outcome is acceptable; the test documents the behavior.
            pass
