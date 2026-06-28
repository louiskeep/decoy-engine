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

import numpy as np
import pandas as pd
import pytest

from testflight._invariants import FIXED_TS, check_distribution_mask
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

        RED: output customer_id is a single constant ("MASKED"). The
        constant-collapse guard requires out_nunique >= 0.99 * src_nunique for
        fpe-class columns. Even though the policy floor for fpe is 0.05
        (value-identity TVD is near 0 by design), the cardinality guard catches
        this independently.

        GREEN: test_good_input_passes below shows a correct fpe bijection passes.
        """
        rng = np.random.default_rng(42)
        n = 200
        src_ids = [f"ID{i:04d}" for i in range(n)]
        source_df = pd.DataFrame(
            {
                "customer_id": src_ids,
                "category": rng.choice(["A", "B", "C"], size=n).tolist(),
            }
        )
        # Broken: fpe collapsed every value to the same constant.
        output_df = source_df.copy()
        output_df["customer_id"] = "MASKED"

        spec = [
            ColumnDistributionSpec(
                table="t",
                column="customer_id",
                distribution_class="preserve",
                strategy="fpe",
                tolerance=0.05,
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
