"""Test-flight mutation-control suite (Phase 2: all non-coverage families).

Anti-vacuity: an assertion that cannot fail on a real regression manufactures
false confidence. Each invariant family requires a known-bad mutation control
that applies a specific regression to a fixture and asserts the corresponding
invariant RAISES. This is the engine analogue of scripts/prove_regression.py.

Phase 1 fills in the distribution-fidelity controls (TestDistributionTeeth).
Phase 2 fills in FK, quarantine, sentinel, and computed-column controls.
Phase 4 fills in coverage rot.

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
  - FK break: orphaned child FK row -> check_fk_integrity raises.
  - Quarantine miscount: wrong quarantine count -> check_quarantine raises.
  - Sentinel leak: raw PII value in output -> check_sentinels raises.
  - Computed-column corruption: wrong formula output -> check_computed_columns raises.
  - Coverage rot: Phase 4.

All tests are marked testflight. Run via:
  pytest testflight -m testflight
  python scripts/test_flight.py
"""

from __future__ import annotations

import pathlib
import tempfile
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest
from pydantic import ValidationError

from testflight._coverage import check_suite_strategy_coverage
from testflight._invariants import (
    FIXED_TS,
    check_chapter_preserve,
    check_computed_columns,
    check_correlation_through_masking,
    check_distribution_generate,
    check_distribution_mask,
    check_fk_integrity,
    check_quarantine,
    check_remap_masks_orphan,
    check_sentinels,
    check_value_changing_not_passthrough,
)
from testflight._spec import (
    ChapterPreserveSpec,
    ColumnDistributionSpec,
    ComputedColumnSpec,
    FKIntegritySpec,
    QuarantineSpec,
    RelationshipEndSpec,
    RelationshipSpec,
    SentinelSpec,
)

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
        """An orphaned child FK row must trip the FK-integrity invariant.

        RED: 100 parent members (masked ids P000..P099) + 11 child claims
        where the last row references FK "P999" which does not exist in the
        parent output. check_fk_integrity expects 0 orphans.

        GREEN: test_good_fk_passes (below) uses a clean child table and passes.

        This test verifies the invariant is not vacuous: a pipeline that somehow
        creates an orphan (e.g. masking deleted a parent key but left the child)
        is caught here, not silently accepted.
        """
        # Build parent table: 100 masked member IDs.
        parent_ids = [f"P{i:03d}" for i in range(100)]
        parent_tbl = pa.table({"member_id": parent_ids})

        # Build child table: 10 valid FK refs + 1 orphan ref to "P999".
        child_fk = [f"P{i:03d}" for i in range(10)] + ["P999"]
        child_tbl = pa.table({"claim_id": [f"C{i:03d}" for i in range(11)], "member_id": child_fk})

        result = SimpleNamespace(
            outputs={"members": parent_tbl, "claims": child_tbl},
            quality_metrics={},
        )

        relationships = [
            RelationshipSpec(
                parent=RelationshipEndSpec(table="members", columns=["member_id"]),
                children=[RelationshipEndSpec(table="claims", columns=["member_id"])],
                orphan_policy="fail",
                namespace="member_identity",
            )
        ]
        spec = [
            FKIntegritySpec(relationship_name="member_identity", expected_orphans=0, policy="fail")
        ]

        with pytest.raises(AssertionError, match="orphan_count=1"):
            check_fk_integrity("fk_control", spec, result, relationships)

    def test_good_fk_passes(self) -> None:
        """A fully-intact FK relationship must NOT raise.

        Proves check_fk_integrity does not over-assert. All child FK values
        reference an existing parent key.
        """
        parent_ids = [f"P{i:03d}" for i in range(100)]
        parent_tbl = pa.table({"member_id": parent_ids})
        child_fk = [f"P{i % 100:03d}" for i in range(200)]
        child_tbl = pa.table({"claim_id": [f"C{i:03d}" for i in range(200)], "member_id": child_fk})

        result = SimpleNamespace(
            outputs={"members": parent_tbl, "claims": child_tbl},
            quality_metrics={},
        )
        relationships = [
            RelationshipSpec(
                parent=RelationshipEndSpec(table="members", columns=["member_id"]),
                children=[RelationshipEndSpec(table="claims", columns=["member_id"])],
                orphan_policy="fail",
                namespace="member_identity",
            )
        ]
        spec = [
            FKIntegritySpec(relationship_name="member_identity", expected_orphans=0, policy="fail")
        ]

        # Must NOT raise: all child FK values are in the parent key pool.
        check_fk_integrity("fk_control_good", spec, result, relationships)


class TestQuarantineTeeth:
    """Mutation controls for the quarantine invariant family."""

    def test_quarantine_miscount_detected(self) -> None:
        """A quarantine count that does not match the manifest must raise.

        RED: quality_metrics reports 11 quarantined rows but the QuarantineSpec
        expects exactly 10. check_quarantine must raise with the mismatch.

        The JSONL file is also written with 11 lines so only the count assertion
        fires (not the file-line-count assertion), keeping the failure localised.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            q_path = pathlib.Path(tmpdir) / "quarantine.jsonl"
            # Write 11 lines (matching the WRONG actual count).
            q_path.write_text(
                "\n".join([f'{{"row": {i}}}' for i in range(11)]) + "\n",
                encoding="utf-8",
            )

            qm = {
                "quarantine": {
                    "enabled": True,
                    "output_path": str(q_path),
                    "counts_by_trigger": {"validation_fail": 11},
                    "total_quarantined": 11,  # one more than expected
                },
                "validation": {
                    "validators": {
                        "passed": False,
                        "validators_run": 1,
                        "findings": [
                            {
                                "validator": "luhn",
                                "table": "members",
                                "column": "card_no",
                                "failing_row_indices": [0],
                                "detail": {},
                            }
                        ],
                        "elapsed_ms": 5,
                    }
                },
            }
            result = SimpleNamespace(outputs={}, quality_metrics=qm)
            spec = QuarantineSpec(
                planted_bad_row_count=10,
                expected_total_quarantined=10,
                expected_validator="luhn",
            )

            with pytest.raises(AssertionError, match="total_quarantined=11"):
                check_quarantine("quarantine_control", spec, result)

    def test_good_quarantine_passes(self) -> None:
        """A quarantine count matching the manifest must NOT raise.

        Proves check_quarantine does not over-assert when the pipeline quarantines
        exactly the expected number of rows with the expected validator.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            q_path = pathlib.Path(tmpdir) / "quarantine.jsonl"
            q_path.write_text(
                "\n".join([f'{{"row": {i}}}' for i in range(10)]) + "\n",
                encoding="utf-8",
            )

            qm = {
                "quarantine": {
                    "enabled": True,
                    "output_path": str(q_path),
                    "counts_by_trigger": {"validation_fail": 10},
                    "total_quarantined": 10,
                },
                "validation": {
                    "validators": {
                        "passed": False,
                        "validators_run": 1,
                        "findings": [
                            {
                                "validator": "luhn",
                                "table": "members",
                                "column": "card_no",
                                "failing_row_indices": list(range(10)),
                                "detail": {},
                            }
                        ],
                        "elapsed_ms": 5,
                    }
                },
            }
            result = SimpleNamespace(outputs={}, quality_metrics=qm)
            spec = QuarantineSpec(
                planted_bad_row_count=10,
                expected_total_quarantined=10,
                expected_validator="luhn",
            )

            # Must NOT raise: count and validator both match.
            check_quarantine("quarantine_control_good", spec, result)


class TestSentinelTeeth:
    """Mutation controls for the sentinel no-leakage invariant family."""

    def test_sentinel_leak_detected(self) -> None:
        """A sentinel SSN appearing in any output column must trip the sentinel scan.

        RED: a passthrough bug leaves the source SSN column unmasked in the
        output. The sentinel SSN "8880088888" appears verbatim in members.ssn
        row 0. check_sentinels scans ALL output columns and raises immediately
        on finding it.

        GREEN: test_good_sentinels_passes (below) shows a masked output passes.

        Note: the sentinel scan checks for the string as a SUBSTRING so even a
        column that embeds the value in a longer string (e.g. "ID:8880088888")
        would be caught. This control uses the exact string to match the most
        common passthrough regression.
        """
        sentinel_ssn = "8880088888"

        # Leaked output: ssn column contains the raw sentinel value in row 0.
        # (Simulates a passthrough bug where fpe was not applied.)
        members_tbl = pa.table(
            {
                "member_id": ["P000", "P001"],
                "ssn": [sentinel_ssn, "1234567890"],  # row 0 leaked
                "name": ["Alice", "Bob"],
            }
        )

        result = SimpleNamespace(
            outputs={"members": members_tbl},
            quality_metrics={},
        )
        spec = [SentinelSpec(table="members", column="ssn", value=sentinel_ssn)]

        with pytest.raises(AssertionError, match=sentinel_ssn):
            check_sentinels("sentinel_control", spec, result)

    def test_good_sentinels_passes(self) -> None:
        """An output with no sentinel strings present must NOT raise.

        Proves check_sentinels does not over-assert. All SSN values in the
        output are masked (different from the sentinel value).
        """
        sentinel_ssn = "8880088888"

        # Properly masked output: ssn column contains transformed values.
        members_tbl = pa.table(
            {
                "member_id": ["P000", "P001"],
                "ssn": ["5551234567", "4449876543"],  # neither is the sentinel
                "name": ["Alice", "Bob"],
            }
        )

        result = SimpleNamespace(
            outputs={"members": members_tbl},
            quality_metrics={},
        )
        spec = [SentinelSpec(table="members", column="ssn", value=sentinel_ssn)]

        # Must NOT raise: sentinel is absent from all output columns.
        check_sentinels("sentinel_control_good", spec, result)


class TestComputedColumnTeeth:
    """Mutation controls for the computed-column correctness invariant family."""

    def test_formula_corruption_detected(self) -> None:
        """A corrupted line_total formula must trip the computed-column invariant.

        RED: line_total is computed WITHOUT the discount_tier factor (i.e.
        always line_amount * units * 1.0 regardless of tier). The control
        builds rows where discount_tier="copay" (factor 0.80) so the expected
        value (0.80 * line_amount * units) differs from the corrupted value
        (1.0 * line_amount * units).

        GREEN: test_good_computed_columns_passes (below) shows a correctly
        computed line_total passes.

        This test proves the branch-weight assertion in check_computed_columns
        catches a real formula regression (the discount factor silently dropped
        from the pipeline's derived expression).
        """
        # 3 rows: one per discount_tier branch. copay and preferred rows will
        # expose the formula corruption (factor != 1.0).
        rows = [
            {
                "line_amount": 100.0,
                "units": 2,
                "discount_tier": "copay",
                # corrupted: 100 * 2 * 1.0 = 200 (should be 100*2*0.80=160)
                "line_total": 200.0,
                "claim_line_sum": 300.0,
            },
            {
                "line_amount": 100.0,
                "units": 2,
                "discount_tier": "preferred",
                # corrupted: 100 * 2 * 1.0 = 200 (should be 100*2*0.90=180)
                "line_total": 200.0,
                "claim_line_sum": 300.0,
            },
            {
                "line_amount": 100.0,
                "units": 2,
                "discount_tier": "standard",
                # standard factor is 1.0 so corrupted == correct (200)
                "line_total": 200.0,
                "claim_line_sum": 300.0,
            },
        ]
        tbl = pa.table(
            {
                k: [r[k] for r in rows]
                for k in ["line_amount", "units", "discount_tier", "line_total", "claim_line_sum"]
            }
        )

        result = SimpleNamespace(
            outputs={"claim_lines": tbl},
            quality_metrics={},
        )
        # Engine-grammar formula: case_when with equality comparisons.
        # The rows have corrupted line_total (factor 1.0 instead of 0.80/0.90)
        # so the recomputed value differs from the stored output.
        formula = (
            "line_amount * units * case_when("
            'discount_tier == "copay", 0.80, '
            'discount_tier == "preferred", 0.90, '
            "1.0)"
        )
        spec = [
            ComputedColumnSpec(
                table="claim_lines",
                column="line_total",
                formula=formula,
                branch_count=3,
            )
        ]

        with pytest.raises(AssertionError, match="claim_lines.line_total"):
            check_computed_columns("computed_control", spec, result)

    def test_good_computed_columns_passes(self) -> None:
        """A correctly computed line_total and claim_line_sum must NOT raise.

        Proves check_computed_columns does not over-assert. The line_total
        values exactly match the case_when formula and claim_line_sum equals
        sum(line_amount) broadcast to all rows.
        """
        rows = [
            {
                "line_amount": 100.0,
                "units": 2,
                "discount_tier": "copay",
                "line_total": 160.0,
            },  # 100*2*0.80
            {
                "line_amount": 100.0,
                "units": 2,
                "discount_tier": "preferred",
                "line_total": 180.0,
            },  # 100*2*0.90
            {
                "line_amount": 100.0,
                "units": 2,
                "discount_tier": "standard",
                "line_total": 200.0,
            },  # 100*2*1.00
        ]
        total_la = 300.0  # sum(line_amount)
        tbl = pa.table(
            {
                "line_amount": [r["line_amount"] for r in rows],
                "units": [r["units"] for r in rows],
                "discount_tier": [r["discount_tier"] for r in rows],
                "line_total": [r["line_total"] for r in rows],
                "claim_line_sum": [total_la, total_la, total_la],
            }
        )

        result = SimpleNamespace(
            outputs={"claim_lines": tbl},
            quality_metrics={},
        )
        # Engine-grammar formulas (Phase 4 generalization).
        line_total_formula = (
            "line_amount * units * case_when("
            'discount_tier == "copay", 0.80, '
            'discount_tier == "preferred", 0.90, '
            "1.0)"
        )
        spec = [
            ComputedColumnSpec(
                table="claim_lines",
                column="line_total",
                formula=line_total_formula,
                branch_count=3,
            ),
            ComputedColumnSpec(
                table="claim_lines",
                column="claim_line_sum",
                formula="sum(line_amount)",
                branch_count=0,
            ),
        ]

        # Must NOT raise: all computed values are correct.
        check_computed_columns("computed_control_good", spec, result)

    def test_changed_formula_followed(self) -> None:
        """Changed formula string is picked up: check follows the new formula.

        Phase 4 control: proves the invariant is formula-driven, not column-name-
        dispatched.  Build a table where "custom_col" = qty * unit_price.  Pass
        two specs with different formula strings for the same column:

          spec_correct: formula "qty * unit_price"  -> PASSES (values match)
          spec_wrong:   formula "qty * unit_price * 2.0" -> FAILS (values differ)

        Before Phase 4 the dispatch was a hardcoded `if cs.column == "order_total"`
        chain; an unknown column name raised AssertionError unconditionally.  After
        Phase 4 the formula string drives evaluation so BOTH branches are reached
        by varying the formula, not the column name.
        """
        tbl = pa.table(
            {
                "qty": [3, 5, 2],
                "unit_price": [10.0, 20.0, 15.0],
                "custom_col": [30.0, 100.0, 30.0],  # qty * unit_price
            }
        )
        result = SimpleNamespace(outputs={"orders": tbl}, quality_metrics={})

        # Correct formula: values match -> must NOT raise.
        spec_correct = [
            ComputedColumnSpec(
                table="orders",
                column="custom_col",
                formula="qty * unit_price",
                branch_count=0,
            )
        ]
        check_computed_columns("formula_follow_good", spec_correct, result)

        # Wrong formula: values do NOT match -> must raise AssertionError.
        spec_wrong = [
            ComputedColumnSpec(
                table="orders",
                column="custom_col",
                formula="qty * unit_price * 2.0",
                branch_count=0,
            )
        ]
        with pytest.raises(AssertionError, match="orders.custom_col"):
            check_computed_columns("formula_follow_bad", spec_wrong, result)


class TestCoverageRotTeeth:
    """Mutation controls for the strategy-coverage guard."""

    def test_coverage_rot_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Registering a fake strategy key must trip the suite coverage guard.

        RED path: monkeypatch.setitem injects a fake key "zz_fake_strategy"
        into the live SCALAR_HANDLERS dict. It is not in any manifest's
        strategy_coverage list and not in _STRATEGY_ALLOWLIST. The unpatched
        call to check_suite_strategy_coverage (reading the live registry) must
        raise AssertionError naming the uncovered strategy.

        GREEN path (implicit): without the fake key the suite guard passes
        (verified by the full test_testflight.py run that exercises all three
        jobs with their declared strategy_coverage lists).

        monkeypatch.setitem is undone automatically after the test exits, so
        the live registry is not permanently mutated. This proves the guard
        reads SCALAR_HANDLERS at call time (not a module-load-time snapshot).
        """
        from decoy_engine.execution._strategies import SCALAR_HANDLERS
        from testflight._runner import discover_jobs, load_job

        # Load all manifests to supply to the guard.
        all_manifests = [load_job(m) for m in discover_jobs()]

        # Sanity: guard passes with the real (unpatched) registry.
        check_suite_strategy_coverage(all_manifests)

        # Inject a fake strategy into the live SCALAR_HANDLERS via monkeypatch.
        # monkeypatch.setitem restores the original dict after the test.
        fake_key = "zz_fake_strategy"
        assert fake_key not in SCALAR_HANDLERS, (
            f"'{fake_key}' already exists in SCALAR_HANDLERS -- choose a different fake key."
        )

        class _FakeHandler:
            name = fake_key

            def run(
                self,
                df: pd.DataFrame,
                column: str,
                plan: Any,
                ctx: Any,
            ) -> tuple[pd.DataFrame, list[Any]]:
                return df, []

        monkeypatch.setitem(SCALAR_HANDLERS, fake_key, _FakeHandler())

        # The guard reads the live SCALAR_HANDLERS; the fake key is not in any
        # manifest's strategy_coverage and not in _STRATEGY_ALLOWLIST -> RAISES.
        with pytest.raises(AssertionError, match=fake_key):
            check_suite_strategy_coverage(all_manifests)

    def test_fake_validator_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Registering a fake validator in the LIVE registry trips the guard (TH-1.2).

        RED: monkeypatch.setitem injects "zz_fake_validator" into the live
        validators._registry._REGISTRY. It is exercised by no job and absent
        from _VALIDATOR_ALLOWLIST, so the coverage guard -- which now reads the
        live registry, not a static frozenset -- must raise naming it. This
        proves the validator axis is derived live (P0-2 anti-rot).
        """
        from decoy_engine.validators import _registry
        from testflight._runner import discover_jobs, load_job

        all_manifests = [load_job(m) for m in discover_jobs()]
        check_suite_strategy_coverage(all_manifests)  # baseline passes

        fake = "zz_fake_validator"
        assert fake not in _registry._REGISTRY
        monkeypatch.setitem(_registry._REGISTRY, fake, lambda *a, **k: ())
        with pytest.raises(AssertionError, match=fake):
            check_suite_strategy_coverage(all_manifests)

    def test_fake_checksum_scheme_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Registering a fake checksum scheme in the LIVE registry trips the guard (TH-1.2).

        RED: inject "zz_fake_scheme" into the live checksums._VALIDATORS dict.
        Not declared by any job, not in _CHECKSUM_SCHEME_ALLOWLIST -> the guard
        (reading the live scheme registry) raises. Proves the checksum axis is
        live.
        """
        from decoy_engine import checksums
        from testflight._runner import discover_jobs, load_job

        all_manifests = [load_job(m) for m in discover_jobs()]
        check_suite_strategy_coverage(all_manifests)  # baseline passes

        fake = "zz_fake_scheme"
        assert fake not in checksums._VALIDATORS
        monkeypatch.setitem(checksums._VALIDATORS, fake, lambda v: True)
        with pytest.raises(AssertionError, match=fake):
            check_suite_strategy_coverage(all_manifests)

    def test_fake_generate_type_detected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Registering a fake generate type in the LIVE registry trips the guard (TH-1.2).

        RED: extend the live config._tables.GENERATE_TYPES set (derived from the
        GenerateColumnConfig.type Literal) with "zz_fake_gen". Not declared by
        any job, not in _GENERATE_TYPE_ALLOWLIST -> the guard (reading the live
        generate-type registry) raises. Proves the generate-type axis is live.
        """
        from decoy_engine.config import _tables
        from testflight._runner import discover_jobs, load_job

        all_manifests = [load_job(m) for m in discover_jobs()]
        check_suite_strategy_coverage(all_manifests)  # baseline passes

        fake = "zz_fake_gen"
        assert fake not in _tables.GENERATE_TYPES
        monkeypatch.setattr(_tables, "GENERATE_TYPES", _tables.GENERATE_TYPES | {fake})
        with pytest.raises(AssertionError, match=fake):
            check_suite_strategy_coverage(all_manifests)


# ---------------------------------------------------------------------------
# TH-1.1: value-changing-passthrough teeth (P0-1)
# ---------------------------------------------------------------------------


class TestValueChangingPassthroughTeeth:
    """Mutation controls for the value-changing-passthrough guard (TH-1.1).

    The guard runs for every mask column whose strategy is value-changing
    (fpe, hash, code_set). Two failure modes:
      - COMPLETE no-op: output value-set equals source value-set (the charset
        covers nothing) -> the set check raises.
      - PARTIAL passthrough (fpe only): a charset that covers *some* characters
        (e.g. alphanum on uppercase-plus-digit values) permutes one character
        while leaving the rest verbatim; every whole value differs so the set
        check passes, but the positional-retention check raises. This is the
        exact members.mrn (charset=alphanum) live bug.
    """

    def test_complete_noop_detected(self) -> None:
        """An fpe column whose output equals its source (complete no-op) raises.

        RED: output value-set == source value-set. A value-changing strategy
        that changes nothing is a silent passthrough; the set check catches it.
        """
        src = pd.DataFrame({"mrn": [f"ID{i:04d}" for i in range(50)]})
        out = src.copy()  # complete no-op: identical
        with pytest.raises(AssertionError, match="value-changing-mask passthrough"):
            check_value_changing_not_passthrough("control", "members", "mrn", "fpe", src, out)

    def test_fpe_partial_passthrough_detected(self) -> None:
        """An fpe column that leaks most characters in position raises (mrn bug).

        RED: source values are uppercase-plus-digit (like the fixture MRNs);
        the output permutes ONLY the two digits and leaves every uppercase
        letter in place. Each whole value differs (so the set check passes) but
        the six uppercase positions are retained verbatim across all rows, so
        the per-position leak check raises. This reproduces the charset=alphanum
        uppercase-MRN leak.
        """
        # 8-char values: 6 uppercase letters (positions 0-5) + 2 digits
        # (positions 6-7). The output keeps every uppercase letter and changes
        # only the two digits, so positions 0-5 each have identical_fraction=1.0
        # (informative: k=26 distinct uppercase) -> the per-position check fires.
        rng = np.random.default_rng(3)
        letters = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        src_vals: list[str] = []
        out_vals: list[str] = []
        for _ in range(60):
            body = "".join(rng.choice(letters, 6).tolist())
            d0, d1 = int(rng.integers(0, 5)), int(rng.integers(0, 5))
            src_vals.append(f"{body}{d0}{d1}")
            # change the two digits (uppercase preserved verbatim)
            out_vals.append(f"{body}{d0 + 4}{d1 + 4}")
        src = pd.DataFrame({"mrn": src_vals})
        out = pd.DataFrame({"mrn": out_vals})
        with pytest.raises(AssertionError, match="partial-passthrough"):
            check_value_changing_not_passthrough("control", "members", "mrn", "fpe", src, out)

    def test_narrow_minority_leak_detected(self) -> None:
        """A MINORITY of verbatim positions among many permuted ones raises (TH-1).

        This is dennis's concrete false-negative that shipped GREEN under the
        old mean-over-positions metric: an "AB123456"-shaped column (2 per-
        subject-varying uppercase + 6 digits) masked with charset:alphanum. The
        two uppercase characters leak VERBATIM in every row (per-position
        identical fraction 1.0) while the six digits permute (~0.1 each).

        RED-BEFORE / GREEN-AFTER pinning: the old metric averaged the eight
        positions to ~(2*1.0 + 6*0.1)/8 = 0.33, under the 0.5 floor, so two
        informative PII characters per subject leaked and the gate stayed green.
        The per-position metric flags position 0 (identical 1.0, k=26 distinct
        uppercase, well above the 1/26 genuine baseline) and raises. Two
        informative positions leaking can no longer ship green at any value
        width.
        """
        rng = np.random.default_rng(11)
        upper = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        digits = list("0123456789")
        src_vals: list[str] = []
        out_vals: list[str] = []
        for _ in range(200):
            # 2 varying uppercase (out-of-charset for alphanum -> verbatim) +
            # 6 varying digits (in-charset -> permuted).
            u = "".join(rng.choice(upper, 2).tolist())
            d = [int(x) for x in rng.choice(digits, 6).tolist()]
            src_vals.append(u + "".join(str(x) for x in d))
            # uppercase preserved verbatim; digits permuted (+3 mod 10).
            out_vals.append(u + "".join(str((x + 3) % 10) for x in d))
        src = pd.DataFrame({"acct": src_vals})
        out = pd.DataFrame({"acct": out_vals})
        with pytest.raises(AssertionError, match="partial-passthrough"):
            check_value_changing_not_passthrough("control", "members", "acct", "fpe", src, out)

    def test_narrow_leak_corrected_passes(self) -> None:
        """The SAME narrow shape passes once every position is permuted (charset fixed).

        GREEN-after companion to test_narrow_minority_leak_detected: with the
        charset corrected (ALPHANUM), the two uppercase characters are now
        in-charset and permuted too, so no position is retained verbatim. The
        guard must NOT raise -- proving the fix is a targeted leak detector, not
        a blanket rejection of the shape.
        """
        rng = np.random.default_rng(11)
        upper = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        digits = list("0123456789")
        src_vals: list[str] = []
        out_vals: list[str] = []
        for _ in range(200):
            u = [x for x in rng.choice(upper, 2).tolist()]
            d = [int(x) for x in rng.choice(digits, 6).tolist()]
            src_vals.append("".join(u) + "".join(str(x) for x in d))
            # ALL positions permuted: uppercase shifted A->B..Z->A, digits +3.
            u_out = "".join(chr((ord(c) - 65 + 1) % 26 + 65) for c in u)
            out_vals.append(u_out + "".join(str((x + 3) % 10) for x in d))
        src = pd.DataFrame({"acct": src_vals})
        out = pd.DataFrame({"acct": out_vals})
        # Must NOT raise: every informative position is permuted.
        check_value_changing_not_passthrough("control", "members", "acct", "fpe", src, out)

    def test_low_entropy_structural_position_not_flagged(self) -> None:
        """A deterministically-preserved LOW-ENTROPY position is not a leak (no false positive).

        The live members.npi column exposes this trap: NPI numbers always start
        with digit 1 or 2 (source alphabet k=2 at position 0), and the
        checksum-aware FPE legitimately preserves that leading digit
        (identical_fraction ~1.0 there) while permuting every other position.
        That is FORMAT, not a subject-identifying character, so the informative-
        alphabet gate (k >= 4) must exclude it and the guard must NOT raise.
        """
        rng = np.random.default_rng(5)
        digits = list("0123456789")
        src_vals: list[str] = []
        out_vals: list[str] = []
        for _ in range(200):
            lead = rng.choice(["1", "2"]).item()  # k=2 leading digit
            body = [int(x) for x in rng.choice(digits, 9).tolist()]
            src_vals.append(lead + "".join(str(x) for x in body))
            # lead preserved verbatim (structural); body fully permuted.
            out_vals.append(lead + "".join(str((x + 5) % 10) for x in body))
        src = pd.DataFrame({"npi": src_vals})
        out = pd.DataFrame({"npi": out_vals})
        # Must NOT raise: position 0 is k=2 (excluded); all other positions permute.
        check_value_changing_not_passthrough("control", "members", "npi", "fpe", src, out)

    def test_small_table_emits_skip_not_silent_pass(self) -> None:
        """An fpe column with < _FPE_RETENTION_MIN_ROWS rows returns an explicit SKIP (LOW-1).

        A silent pass on a small table would let the positional privacy floor
        evaporate. The guard runs the set check (still enforced) but reports a
        distinct SKIP status for the positional check instead of a silent PASS.
        """
        src = pd.DataFrame({"mrn": [f"AB{i:02d}CD" for i in range(5)]})  # 5 < 20
        out = pd.DataFrame({"mrn": [f"zx{i:02d}wq" for i in range(5)]})
        status = check_value_changing_not_passthrough("control", "members", "mrn", "fpe", src, out)
        assert status.startswith("SKIP"), f"expected an explicit SKIP status, got {status!r}"

    def test_good_fpe_passes(self) -> None:
        """A genuinely permuted fpe column passes both checks (no over-assertion).

        Every character position is remapped (retention ~0), and the value-set
        differs. The guard must NOT raise.
        """
        src = pd.DataFrame({"mrn": [f"AB{i:04d}CD" for i in range(60)]})
        # Fully different values, no in-position character retained.
        out = pd.DataFrame({"mrn": [f"zx{(i * 7) % 10000:04d}wq"[::-1] for i in range(60)]})
        check_value_changing_not_passthrough("control", "members", "mrn", "fpe", src, out)


# ---------------------------------------------------------------------------
# TH-1.3: independent computed-column recomputation (P0-3)
# ---------------------------------------------------------------------------


class TestIndependentRecomputationTeeth:
    """Mutation controls proving row-wise recomputation is engine-independent.

    Before TH-1.3 the harness recomputed row-wise formulas with the engine's
    own evaluate(), so a bug in that shared evaluator produced the same wrong
    value on both sides and the invariant passed vacuously. The harness now uses
    an independent Python-ast evaluator, so an engine-evaluator bug is caught.
    """

    def test_independent_of_engine_evaluator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sabotaged engine evaluator does not blind the harness (TH-1.3).

        RED-first construction: monkeypatch the engine's evaluate() to always
        return 0 (a stand-in for any shared-evaluator bug). The stored output
        column is what that buggy engine would emit (all zeros). A CIRCULAR
        harness would recompute with the same evaluate(), get 0, and match the
        stored 0 -> pass (blind spot). The independent ast evaluator recomputes
        qty * unit_price = the true nonzero values -> mismatch -> RAISES.
        """
        import decoy_engine.expressions._lark_parser as lp

        # NARRATIVE-ONLY patch (LOW-2): the harness recompute path in _computed.py
        # now calls only compile_expr (smoke check) and its OWN ast evaluator --
        # it no longer calls lp.evaluate at all, so this patch does not alter the
        # code under test. It is retained to make the blind-spot argument concrete:
        # the stored all-zeros output is exactly what this sabotaged evaluator
        # would emit, so a hypothetical same-evaluator harness would agree and
        # pass. The independent ast evaluator recomputes the true nonzero values
        # and raises -- which is what the assertion below proves.
        monkeypatch.setattr(lp, "evaluate", lambda compiled, ctx: 0)

        tbl = pa.table(
            {"qty": [3, 5, 2], "unit_price": [10.0, 20.0, 15.0], "order_total": [0.0, 0.0, 0.0]}
        )
        result = SimpleNamespace(outputs={"orders": tbl}, quality_metrics={})
        spec = [
            ComputedColumnSpec(
                table="orders", column="order_total", formula="qty * unit_price", branch_count=0
            )
        ]
        with pytest.raises(AssertionError, match="orders.order_total"):
            check_computed_columns("th13_control", spec, result)

    def test_case_when_independent_recomputation(self) -> None:
        """A wrong case_when branch value is caught by the independent evaluator.

        RED: stored tier uses the wrong threshold labels; the independent
        evaluator recomputes the correct case_when result and the mismatch
        raises. GREEN: the correct output (below) passes.
        """
        formula = 'case_when(order_total >= 1000.0, "premium", order_total >= 200.0, "standard", "economy")'
        # order_total 1500 -> premium, 500 -> standard, 50 -> economy
        good = pa.table(
            {"order_total": [1500.0, 500.0, 50.0], "tier": ["premium", "standard", "economy"]}
        )
        spec = [ComputedColumnSpec(table="orders", column="tier", formula=formula, branch_count=3)]
        check_computed_columns("th13_good", spec, SimpleNamespace(outputs={"orders": good}))

        bad = pa.table(
            {"order_total": [1500.0, 500.0, 50.0], "tier": ["economy", "economy", "economy"]}
        )
        with pytest.raises(AssertionError, match="orders.tier"):
            check_computed_columns("th13_bad", spec, SimpleNamespace(outputs={"orders": bad}))


# ---------------------------------------------------------------------------
# SP-08: joint_mask consistency teeth
# ---------------------------------------------------------------------------


class TestJointMaskConsistencyTeeth:
    """Mutation controls for the SP-08 joint_mask consistency invariant.

    SP-08 contract: apply_joint_mask writes ALL target columns from a single
    reference-table row. Each output (col_a, col_b, ...) tuple must therefore
    be an actual row in the reference table. A Frankenstein combination (city
    from one row, state from another) is a bug the invariant must catch.

    Controls:
      test_real_tuples_pass: output rows drawn directly from the reference table
        -> invariant PASSES (proves non-vacuity: valid data passes through).
      test_frankenstein_tuple_raises: a row with a city/state combo that does
        not exist in the reference table -> invariant RAISES (proves the check
        catches a Frankenstein mix, the failure mode joint_mask prevents).
    """

    def test_real_tuples_pass(self) -> None:
        """Tuples taken directly from the reference table pass the consistency check."""
        from decoy_engine.reference_tables import load_table
        from testflight._invariants import check_joint_mask_consistency
        from testflight._spec import JointMaskConsistencySpec

        ref = load_table("us_zip5_city_state")
        # Build an output DataFrame from the first 5 rows of the reference table.
        good_rows = [ref.row(i) for i in range(min(5, ref.row_count))]
        out_df = pd.DataFrame(
            {
                "city": [r["city"] for r in good_rows],
                "state": [r["state"] for r in good_rows],
            }
        )
        out_pa = pa.Table.from_pandas(out_df, preserve_index=False)

        spec = [
            JointMaskConsistencySpec(
                table="customers",
                columns=["city", "state"],
                reference="us_zip5_city_state",
            )
        ]
        result = SimpleNamespace(outputs={"customers": out_pa})

        # PASSES: every (city, state) tuple is a real reference-table row.
        evidence = check_joint_mask_consistency("test_job", spec, result)
        assert "5/5" in evidence or "all-valid" in evidence

    def test_frankenstein_tuple_raises(self) -> None:
        """A (city, state) pair that is not a reference-table row raises AssertionError.

        The mutation: take a real city name but combine it with a state
        abbreviation that does not exist in the reference table ("XX").
        This simulates a bug where city and state were selected from
        different reference rows, producing an impossible combination.
        The invariant must detect and reject this.
        """
        from decoy_engine.reference_tables import load_table
        from testflight._invariants import check_joint_mask_consistency
        from testflight._spec import JointMaskConsistencySpec

        ref = load_table("us_zip5_city_state")
        real_city = ref.row(0)["city"]  # a valid city from the table

        # "XX" is not a US state and cannot appear in us_zip5_city_state.
        bad_df = pd.DataFrame({"city": [real_city], "state": ["XX"]})
        bad_pa = pa.Table.from_pandas(bad_df, preserve_index=False)

        spec = [
            JointMaskConsistencySpec(
                table="customers",
                columns=["city", "state"],
                reference="us_zip5_city_state",
            )
        ]
        result = SimpleNamespace(outputs={"customers": bad_pa})

        # RAISES: (real_city, "XX") is not any row in us_zip5_city_state.
        with pytest.raises(AssertionError, match="not a real row in reference table"):
            check_joint_mask_consistency("test_job", spec, result)


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


# ---------------------------------------------------------------------------
# TestJobACorrelationBites: Tooth C (correlation-preservation) genuinely fires
# on the Job A (amount_band, diagnosis_chapter) joint when the output is
# decorrelated. The control test proves the joint is not vacuous.
# ---------------------------------------------------------------------------


@pytest.mark.testflight
class TestJobACorrelationBites:
    """Mutation controls for the (amount_band, diagnosis_chapter) joint.

    Proves that Tooth C fires when amount_band is shuffled in the output
    (decorrelation -> similarity < 0.90 -> AssertionError), and that a
    correct passthrough output passes (similarity = 1.0).
    """

    @staticmethod
    def _build_correlated_df(n: int = 2000, seed: int = 42) -> pd.DataFrame:
        """Return a DataFrame with amount_band correlated to diagnosis_chapter.

        amount_band and diagnosis_chapter are strongly correlated by construction:
          high -> chapter I or E (ICD high-cost chapters)
          low  -> chapter A or B (ICD low-cost chapters)
          mid  -> chapter J or K
        Matches the correlation pattern in fixture.build_claims().
        """
        rng = np.random.default_rng(seed)
        bands = rng.choice(["high", "low", "mid"], size=n, p=[0.35, 0.30, 0.35])
        chapter_map = {"high": list("IE"), "low": list("AB"), "mid": list("JK")}
        chapters = [rng.choice(chapter_map[str(b)]) for b in bands]
        return pd.DataFrame({"amount_band": bands, "diagnosis_chapter": chapters})

    @staticmethod
    def _make_spec() -> list[ColumnDistributionSpec]:
        return [
            ColumnDistributionSpec(
                table="claims",
                column="amount_band",
                distribution_class="preserve",
                strategy="passthrough",
                joint_columns=[["amount_band", "diagnosis_chapter"]],
                corr_tol=0.90,
            ),
            ColumnDistributionSpec(
                table="claims",
                column="diagnosis_chapter",
                distribution_class="preserve",
                strategy="passthrough",
                joints_waived=True,
                joints_waived_reason=(
                    "diagnosis_chapter is the target of the joint declared on "
                    "amount_band; waived here to avoid inflating non-waived count."
                ),
            ),
        ]

    def test_correct_passthrough_output_passes(self) -> None:
        """A passthrough output (amount_band unchanged) must pass Tooth C.

        Both amount_band and diagnosis_chapter are passthrough; the joint
        similarity between source and output is 1.0 because the values are
        identical. This proves the 'good path' is not over-asserted.
        """
        source_df = self._build_correlated_df()
        output_df = source_df.copy()  # passthrough: no change
        # Should not raise.
        check_distribution_mask(
            "job_a_corr_control",
            "claims",
            self._make_spec(),
            source_df,
            output_df,
            strategy_map={"amount_band": "passthrough", "diagnosis_chapter": "passthrough"},
        )

    def test_decorrelated_output_raises(self) -> None:
        """Shuffling amount_band in the output breaks the joint -> Tooth C fires.

        The source has a strong (amount_band, diagnosis_chapter) correlation
        (TVD similarity near 1.0). Shuffling amount_band independently of
        diagnosis_chapter destroys the joint distribution, bringing the
        joint similarity below corr_tol=0.90. The check must raise with a
        correlation-preservation message.
        """
        rng = np.random.default_rng(99)
        source_df = self._build_correlated_df()
        output_df = source_df.copy()
        # Shuffle amount_band independently of diagnosis_chapter.
        output_df["amount_band"] = rng.permutation(output_df["amount_band"].values)
        with pytest.raises(AssertionError, match="correlation-preservation"):
            check_distribution_mask(
                "job_a_corr_control",
                "claims",
                self._make_spec(),
                source_df,
                output_df,
                strategy_map={"amount_band": "passthrough", "diagnosis_chapter": "passthrough"},
            )


# ---------------------------------------------------------------------------
# TestChapterPreserve: check_chapter_preserve end-to-end mutation controls.
# ---------------------------------------------------------------------------


@pytest.mark.testflight
class TestChapterPreserve:
    """Mutation controls for check_chapter_preserve (invariant 6.11).

    Proves that check_chapter_preserve detects chapter mismatches and
    passes cleanly when chapters are preserved.
    """

    @staticmethod
    def _make_result(outputs: dict[str, pa.Table]) -> Any:
        return SimpleNamespace(outputs=outputs)

    def test_same_chapter_replacement_passes(self) -> None:
        """Masked codes in the same ICD-10 chapter must pass check.

        Source A01.0 -> output A02.0: same chapter (A). Should not raise.
        """
        src = pa.table(
            {
                "claim_id": ["C1", "C2", "C3", "C4"],
                "diagnosis": ["A01.0", "B20.0", "I10.0", "C50.0"],
            }
        )
        out = pa.table(
            {
                "claim_id": ["C1", "C2", "C3", "C4"],
                "diagnosis": ["A02.0", "B19.0", "I11.0", "C51.0"],  # same chapters
            }
        )
        spec = [ChapterPreserveSpec(table="claims", column="diagnosis")]
        check_chapter_preserve(
            "chapter_test",
            spec,
            self._make_result({"claims": out}),
            {"claims": src},
        )

    def test_chapter_mismatch_raises(self) -> None:
        """A masked code in a different chapter must raise.

        Source C50.0 (chapter C) -> output Z10.0 (chapter Z) is a chapter
        violation. check_chapter_preserve must raise with a 'chapter_preserve'
        message including the count of mismatches.
        """
        src = pa.table(
            {
                "claim_id": ["C1", "C2", "C3"],
                "diagnosis": ["A01.0", "B20.0", "C50.0"],
            }
        )
        out = pa.table(
            {
                "claim_id": ["C1", "C2", "C3"],
                # C50.0 -> Z10.0 is a chapter violation (C -> Z).
                "diagnosis": ["A02.0", "B19.0", "Z10.0"],
            }
        )
        spec = [ChapterPreserveSpec(table="claims", column="diagnosis")]
        with pytest.raises(AssertionError, match="chapter_preserve"):
            check_chapter_preserve(
                "chapter_test",
                spec,
                self._make_result({"claims": out}),
                {"claims": src},
            )

    def test_multiple_mismatches_raises(self) -> None:
        """Multiple chapter mismatches are reported in the AssertionError."""
        src = pa.table(
            {
                "claim_id": ["C1", "C2", "C3", "C4"],
                "diagnosis": ["A01.0", "B20.0", "I10.0", "C50.0"],
            }
        )
        out = pa.table(
            {
                "claim_id": ["C1", "C2", "C3", "C4"],
                # B20.0 -> A20.0 (B->A) and C50.0 -> Z10.0 (C->Z) are mismatches.
                "diagnosis": ["A02.0", "A20.0", "I11.0", "Z10.0"],
            }
        )
        spec = [ChapterPreserveSpec(table="claims", column="diagnosis")]
        with pytest.raises(AssertionError, match="chapter_preserve"):
            check_chapter_preserve(
                "chapter_test",
                spec,
                self._make_result({"claims": out}),
                {"claims": src},
            )


# ---------------------------------------------------------------------------
# TestPerColumnWaiverScoping: verifies the HIGH-1 per-column waiver fix.
# The bug was: any(s.joints_waived) disabled the joint requirement for the
# WHOLE table. The fix: only non-waived preserve columns contribute to the
# >=2 count that triggers the requirement.
# ---------------------------------------------------------------------------


@pytest.mark.testflight
class TestPerColumnWaiverScoping:
    """Mutation controls for the per-column waiver scoping fix (HIGH-1).

    Before the fix:
      - waiving column C also exempted columns A and B from the joint requirement.
    After the fix:
      - waiving C exempts only C; A and B still require a declared joint.
    """

    @staticmethod
    def _build_df(n: int = 200, seed: int = 42) -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        return pd.DataFrame(
            {
                "a": [str(v) for v in rng.integers(0, 5, n)],
                "b": [str(v) for v in rng.integers(0, 5, n)],
                "c": [str(v) for v in rng.integers(0, 5, n)],
            }
        )

    def test_two_non_waived_no_joints_raises(self) -> None:
        """With 2 non-waived preserve columns and no joints -> RAISES.

        Column c is waived, but columns a and b are non-waived. The per-column
        scoping fix ensures that c's waiver does NOT exempt a and b from the
        joint requirement. With no joint declared, the requirement fires.
        """
        df = self._build_df()
        spec = [
            ColumnDistributionSpec(
                table="t",
                column="a",
                distribution_class="preserve",
                strategy="passthrough",
            ),
            ColumnDistributionSpec(
                table="t",
                column="b",
                distribution_class="preserve",
                strategy="passthrough",
            ),
            ColumnDistributionSpec(
                table="t",
                column="c",
                distribution_class="preserve",
                strategy="passthrough",
                joints_waived=True,
                joints_waived_reason="c is waived; a and b are still non-waived",
            ),
        ]
        with pytest.raises(AssertionError, match="non-waived preserve"):
            check_distribution_mask(
                "waiver_scope_test",
                "t",
                spec,
                df,
                df.copy(),
                strategy_map={"a": "passthrough", "b": "passthrough", "c": "passthrough"},
            )

    def test_waived_column_with_joint_for_others_passes(self) -> None:
        """Waiving c does not block a declared joint for a and b.

        Adding joint_columns=[["a","b"]] to the spec for column a satisfies
        the requirement for the two non-waived preserve columns. The check
        must pass.
        """
        df = self._build_df()
        spec = [
            ColumnDistributionSpec(
                table="t",
                column="a",
                distribution_class="preserve",
                strategy="passthrough",
                joint_columns=[["a", "b"]],
                # 0.50 is the minimum allowed by the spec (ge=0.5). The test
                # verifies scoping, not TVD value; 0.5 is loose enough to pass
                # with uncorrelated random integer columns.
                corr_tol=0.50,
            ),
            ColumnDistributionSpec(
                table="t",
                column="b",
                distribution_class="preserve",
                strategy="passthrough",
                joints_waived=True,
                joints_waived_reason="b is the target of the joint declared on a",
            ),
            ColumnDistributionSpec(
                table="t",
                column="c",
                distribution_class="preserve",
                strategy="passthrough",
                joints_waived=True,
                joints_waived_reason="c is explicitly waived",
            ),
        ]
        # Should not raise: joint declared for the two non-waived specs' columns.
        check_distribution_mask(
            "waiver_scope_test",
            "t",
            spec,
            df,
            df.copy(),
            strategy_map={"a": "passthrough", "b": "passthrough", "c": "passthrough"},
        )

    def test_all_waived_no_joints_passes(self) -> None:
        """If ALL preserve columns are individually waived, no joints are needed.

        With 0 non-waived preserve columns, the >=2 requirement cannot fire.
        """
        df = self._build_df()
        spec = [
            ColumnDistributionSpec(
                table="t",
                column="a",
                distribution_class="preserve",
                strategy="passthrough",
                joints_waived=True,
                joints_waived_reason="test: all waived",
            ),
            ColumnDistributionSpec(
                table="t",
                column="b",
                distribution_class="preserve",
                strategy="passthrough",
                joints_waived=True,
                joints_waived_reason="test: all waived",
            ),
        ]
        # Should not raise: no non-waived columns -> no joint requirement.
        check_distribution_mask(
            "waiver_scope_test",
            "t",
            spec,
            df,
            df.copy(),
            strategy_map={"a": "passthrough", "b": "passthrough"},
        )


# ---------------------------------------------------------------------------
# Phase 3a: M2M FK integrity mutation controls (TestM2MFKIntegrityTeeth)
# ---------------------------------------------------------------------------


@pytest.mark.testflight
class TestM2MFKIntegrityTeeth:
    """Mutation controls for the M2M FK integrity invariant (Phase 3a, Job B).

    Job B has two FK parents for the orders junction table:
      customer_identity: customers.customer_id -> orders.customer_id
      product_identity:  products.product_id  -> orders.product_id

    These controls prove BOTH FK checks are active. Breaking the customer FK
    trips the customer_identity check; breaking the product FK trips the
    product_identity check. Only one parent needs to be broken at a time.

    The invariant checks orphan counts in the OUTPUT tables (set-membership
    on the masked parent key set). These controls bypass the pipeline and
    construct output tables directly.
    """

    @staticmethod
    def _build_m2m_outputs(
        n_customers: int = 50,
        n_products: int = 20,
        n_orders: int = 100,
        seed: int = 7,
        *,
        broken_customer: bool = False,
        broken_product: bool = False,
    ) -> tuple[Any, list[RelationshipSpec], list[FKIntegritySpec]]:
        """Build minimal M2M output tables and spec for FK integrity tests.

        Args:
            n_customers: Number of customer rows.
            n_products: Number of product rows.
            n_orders: Number of order rows.
            seed: RNG seed for reproducibility.
            broken_customer: If True, one order references a non-existent customer.
            broken_product: If True, one order references a non-existent product.

        Returns:
            (result_ns, relationships, fk_spec) ready for check_fk_integrity.
        """
        from types import SimpleNamespace

        rng = np.random.default_rng(seed)

        customer_ids = [f"MC{i:03d}" for i in range(n_customers)]
        product_ids = [f"MP{i:03d}" for i in range(n_products)]

        # Build valid order FK references.
        order_customer_ids = [
            customer_ids[int(rng.integers(0, n_customers))] for _ in range(n_orders)
        ]
        order_product_ids = [product_ids[int(rng.integers(0, n_products))] for _ in range(n_orders)]

        # Inject broken FK if requested.
        if broken_customer:
            order_customer_ids[0] = "MC999"  # does not exist
        if broken_product:
            order_product_ids[0] = "MP999"  # does not exist

        customers_tbl = pa.table({"customer_id": customer_ids})
        products_tbl = pa.table({"product_id": product_ids})
        orders_tbl = pa.table(
            {
                "order_id": [f"OR{i:04d}" for i in range(n_orders)],
                "customer_id": order_customer_ids,
                "product_id": order_product_ids,
            }
        )

        result = SimpleNamespace(
            outputs={
                "customers": customers_tbl,
                "products": products_tbl,
                "orders": orders_tbl,
            },
            quality_metrics={},
        )

        relationships = [
            RelationshipSpec(
                parent=RelationshipEndSpec(table="customers", columns=["customer_id"]),
                children=[RelationshipEndSpec(table="orders", columns=["customer_id"])],
                orphan_policy="warn",
                namespace="customer_identity",
            ),
            RelationshipSpec(
                parent=RelationshipEndSpec(table="products", columns=["product_id"]),
                children=[RelationshipEndSpec(table="orders", columns=["product_id"])],
                orphan_policy="warn",
                namespace="product_identity",
            ),
        ]

        fk_spec = [
            FKIntegritySpec(
                relationship_name="customer_identity", expected_orphans=0, policy="warn"
            ),
            FKIntegritySpec(
                relationship_name="product_identity", expected_orphans=0, policy="warn"
            ),
        ]

        return result, relationships, fk_spec

    def test_m2m_customer_fk_break_detected(self) -> None:
        """Breaking the CUSTOMER FK must trip the customer_identity check.

        RED: one order references customer "MC999" which is not in the customers
        output. check_fk_integrity expects 0 orphans for customer_identity.

        GREEN: test_m2m_both_fk_intact_passes (below) uses clean orders and passes.

        This proves the CUSTOMER side of the M2M FK invariant is active. A
        regression that skips the customer FK check would not catch this orphan.
        """
        result, relationships, fk_spec = self._build_m2m_outputs(broken_customer=True)

        with pytest.raises(AssertionError, match="orphan_count=1"):
            check_fk_integrity("m2m_customer_break", fk_spec, result, relationships)

    def test_m2m_product_fk_break_detected(self) -> None:
        """Breaking the PRODUCT FK must trip the product_identity check.

        RED: one order references product "MP999" which is not in the products
        output. check_fk_integrity expects 0 orphans for product_identity.

        This proves the PRODUCT side of the M2M FK invariant is active.
        The customer side passes (customer FKs are valid). Only the product
        check fires. This distinguishes the two-parent check: if only one
        FK check were run (e.g., customer-only), this test would silently pass.
        """
        result, relationships, fk_spec = self._build_m2m_outputs(broken_product=True)

        with pytest.raises(AssertionError, match="orphan_count=1"):
            check_fk_integrity("m2m_product_break", fk_spec, result, relationships)

    def test_m2m_both_fk_intact_passes(self) -> None:
        """All M2M FK references valid -> check_fk_integrity must NOT raise.

        Proves the invariant does not over-assert. With all orders referencing
        valid customers AND valid products, both FK checks pass.
        """
        result, relationships, fk_spec = self._build_m2m_outputs()

        # Must NOT raise: all child FK values exist in the parent key pools.
        check_fk_integrity("m2m_clean", fk_spec, result, relationships)


# ---------------------------------------------------------------------------
# Phase 3a: M2M correlation mutation controls (TestM2MCorrelationTeeth)
# ---------------------------------------------------------------------------


@pytest.mark.testflight
class TestM2MCorrelationTeeth:
    """Mutation controls for the M2M passthrough-column correlation invariant (Phase 3a).

    NOTE: this is correlation preservation for VALUE-STABLE (passthrough) columns,
    NOT correlation-through-masking. The engine crosstab-TVD metric cannot measure
    correlation through a value-changing mask (it relabels cell keys). See
    docs/what-we-cannot-prove.md.

    Job B plants two PASSTHROUGH columns on the orders junction table:
      qty_band and order_total_band, correlated by fixture construction.
    With both passthrough, source == output -> joint similarity = 1.0 -> PASSES.

    The mutation control replaces order_total_band in the output with independently
    shuffled values, destroying the correlation. The joint (qty_band, order_total_band)
    similarity drops well below corr_tol=0.90 -> RAISES.

    This proves: the correlation tooth catches a pipeline regression where the
    M2M masking pipeline accidentally transforms a passthrough column (e.g., by
    treating it as an FK column and remapping its values).
    """

    @staticmethod
    def _build_m2m_correlated_frames(
        n: int = 500,
        seed: int = 42,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Build source + faithful-output + broken-output for M2M correlation test.

        Source: orders table with correlated qty_band and order_total_band.
        Correlation design: electronics/home products are high-price -> high
        order_total_band regardless of qty. Food products are low-price -> low
        order_total_band. Within a category, qty and order_total scale together.

        Returns:
            (source_df, faithful_output_df, broken_output_df)
            faithful: both columns identical to source (passthrough)
            broken: order_total_band shuffled independently (correlation destroyed)
        """
        rng = np.random.default_rng(seed)

        # Unit price ranges matching the Job B fixture.
        price_ranges = {
            "electronics": (200.0, 1200.0),
            "food": (3.0, 30.0),
            "home": (50.0, 500.0),
            "clothing": (20.0, 150.0),
            "sports": (25.0, 200.0),
        }
        order_total_low = 50.0
        order_total_high = 300.0

        categories = list(price_ranges.keys())
        cat_choices = rng.choice(len(categories), size=n)

        rows = []
        for i in range(n):
            category = categories[cat_choices[i]]
            qty = int(rng.integers(1, 9))
            price_lo, price_hi = price_ranges[category]
            unit_price = float(rng.uniform(price_lo, price_hi))
            total = round(qty * unit_price, 2)

            if qty <= 2:
                qty_b = "low"
            elif qty <= 5:
                qty_b = "mid"
            else:
                qty_b = "high"

            if total < order_total_low:
                total_b = "low"
            elif total < order_total_high:
                total_b = "mid"
            else:
                total_b = "high"

            rows.append({"qty_band": qty_b, "order_total_band": total_b})

        source_df = pd.DataFrame(rows)
        faithful_df = source_df.copy()

        # Broken output: shuffle order_total_band independently (destroying correlation).
        broken_df = source_df.copy()
        shuffled = broken_df["order_total_band"].to_numpy().copy()
        rng.shuffle(shuffled)
        broken_df["order_total_band"] = shuffled

        return source_df, faithful_df, broken_df

    @staticmethod
    def _corr_spec() -> list[Any]:
        """Build the ColumnDistributionSpec for the M2M correlation pair."""
        from testflight._spec import ColumnDistributionSpec

        return [
            ColumnDistributionSpec(
                table="orders",
                column="qty_band",
                distribution_class="preserve",
                strategy="passthrough",
                joint_columns=[["qty_band", "order_total_band"]],
                corr_tol=0.90,
            ),
            ColumnDistributionSpec(
                table="orders",
                column="order_total_band",
                distribution_class="preserve",
                strategy="passthrough",
                joints_waived=True,
                joints_waived_reason="target of the joint declared on qty_band; waived here",
            ),
        ]

    def test_m2m_correlation_break_detected(self) -> None:
        """Shuffling order_total_band must drop joint similarity below 0.90.

        RED: order_total_band is shuffled independently in the output. The joint
        (qty_band, order_total_band) loses its category-driven structure.
        Label-aware TVD between source and shuffled output crosstabs is > 0.10
        -> similarity < 0.90 < corr_tol=0.90 -> RAISES.

        GREEN: test_m2m_good_passthrough_correlation_passes (below) uses a
        faithful passthrough output and passes.

        This proves the correlation tooth catches a pipeline regression where
        the M2M masking accidentally corrupts a passthrough column on the
        junction table (e.g., by treating qty_band or order_total_band as an
        FK column and remapping its values to a different value set).
        """
        source_df, _, broken_df = self._build_m2m_correlated_frames(n=800, seed=42)
        spec = self._corr_spec()

        with pytest.raises(AssertionError, match="corr_tol"):
            check_distribution_mask(
                "m2m_corr_break",
                "orders",
                spec,
                source_df,
                broken_df,
                strategy_map={"qty_band": "passthrough", "order_total_band": "passthrough"},
            )

    def test_m2m_good_passthrough_correlation_passes(self) -> None:
        """Faithful passthrough output must NOT raise for the M2M correlation pair.

        Both qty_band and order_total_band are passthrough -> output identical
        to source -> joint similarity = 1.0 >= corr_tol=0.90 -> PASSES.

        Proves the correlation tooth does not over-assert: a correctly masked
        junction table with passthrough columns passes without false alarms.
        """
        source_df, faithful_df, _ = self._build_m2m_correlated_frames(n=800, seed=42)
        spec = self._corr_spec()

        # Must NOT raise: source == output -> joint similarity = 1.0.
        check_distribution_mask(
            "m2m_corr_good",
            "orders",
            spec,
            source_df,
            faithful_df,
            strategy_map={"qty_band": "passthrough", "order_total_band": "passthrough"},
        )


# ---------------------------------------------------------------------------
# Phase 3b: Job C self-referential FK mutation controls (TestJobCSelfFKTeeth)
# ---------------------------------------------------------------------------


@pytest.mark.testflight
class TestJobCSelfFKTeeth:
    """Mutation controls for the self-referential FK invariant (Phase 3b, Job C).

    Job C topology: employees.manager_id -> employees.employee_id (same table).
    The fk_integrity check looks up manager_id values in the employee_id pool of
    the same table. A dangling manager_id (not in the masked employee_id set)
    is an orphan and must trip the invariant when expected_orphans < actual_orphans.

    RED: test_self_fk_closure_broken -- 1 dangling manager_id, expected 0 orphans.
    GREEN: test_self_fk_closure_good -- all manager_ids in the masked employee_id set.
    """

    @staticmethod
    def _build_self_fk_outputs(
        n_employees: int = 50,
        n_root: int = 5,
        broken: bool = False,
    ) -> tuple[Any, list[RelationshipSpec], list[FKIntegritySpec]]:
        """Build a self-referential employees table with optional dangling FK.

        Root employees (rows 0..n_root-1): manager_id = None.
        Non-root employees: manager_id references a valid masked employee_id,
        unless `broken=True`, in which case the last non-root row gets a
        dangling manager_id "EMP-GHOST" not present in the employee_id column.

        Returns:
            (result_ns, relationships, fk_spec) ready for check_fk_integrity.
        """
        emp_ids = [f"M-{i:04d}" for i in range(n_employees)]
        root_manager_ids: list[Any] = [None] * n_root
        valid_manager_ids = [emp_ids[i % n_root] for i in range(n_employees - n_root - 1)]
        if broken:
            # Last non-root employee has a dangling manager_id not in emp_ids.
            broken_manager_ids = [*valid_manager_ids, "EMP-GHOST"]
        else:
            broken_manager_ids = [*valid_manager_ids, emp_ids[0]]
        manager_ids = [*root_manager_ids, *broken_manager_ids]

        employees_tbl = pa.table(
            {
                "employee_id": emp_ids,
                "manager_id": manager_ids,
                "department": ["Engineering"] * n_employees,
                "salary": [75000] * n_employees,
            }
        )

        result = SimpleNamespace(
            outputs={"employees": employees_tbl},
            quality_metrics={},
        )

        relationships = [
            RelationshipSpec(
                parent=RelationshipEndSpec(table="employees", columns=["employee_id"]),
                children=[RelationshipEndSpec(table="employees", columns=["manager_id"])],
                orphan_policy="remap",
                namespace="employee_identity",
            )
        ]
        fk_spec = [
            FKIntegritySpec(
                relationship_name="employee_identity",
                expected_orphans=0,
                policy="remap",
            )
        ]
        return result, relationships, fk_spec

    def test_self_fk_closure_broken(self) -> None:
        """A dangling manager_id in the self-FK table must trip check_fk_integrity.

        RED: one employee references manager_id "EMP-GHOST" which is not in the
        masked employee_id column. check_fk_integrity expects 0 orphans.

        This proves the self-referential FK invariant is not vacuous: the parent
        key pool is the SAME table's masked employee_id column, so a dangling
        manager_id in the output is an orphan that the invariant catches.

        A regression where the FK check reads the wrong key pool (e.g., skips the
        self-reference case or uses an empty pool) would not catch this orphan.
        """
        result, relationships, fk_spec = self._build_self_fk_outputs(broken=True)

        with pytest.raises(AssertionError, match="orphan_count=1"):
            check_fk_integrity("c_selfref_break", fk_spec, result, relationships)

    def test_self_fk_closure_good(self) -> None:
        """All manager_ids in the masked employee_id pool must NOT raise.

        GREEN: all non-null manager_id values reference a valid masked employee_id
        in the same table. Null manager_ids (root nodes) are skipped.

        Proves check_fk_integrity does not over-assert for the self-FK case: a
        correctly masked table with a valid tree structure passes cleanly.
        """
        result, relationships, fk_spec = self._build_self_fk_outputs(broken=False)

        # Must NOT raise: all non-null manager_ids are valid employee_ids.
        check_fk_integrity("c_selfref_good", fk_spec, result, relationships)


# ---------------------------------------------------------------------------
# Phase 3b: Job C generate distribution controls (TestJobCGenerateControls)
# ---------------------------------------------------------------------------


@pytest.mark.testflight
class TestJobCGenerateControls:
    """Mutation controls for Job C's generate table distribution invariant.

    Job C introduces two new paths in check_distribution_generate:
      1. List weights for categorical columns (engine format: list parallel to categories).
      2. Formula columns with params.mean/std metadata (formula type + params).

    RED controls: skewed categorical (list weights) and shifted formula mean.
    GREEN control: correct output within tolerance passes.
    """

    def test_skewed_categorical_list_weights_raises(self) -> None:
        """Output all-login when list weights declare 40/30/20/10 must raise.

        Tests the list-weights path of check_distribution_generate. Before Phase 3b,
        only dict weights were handled; list weights were silently skipped (no check).

        TVD = 0.5*(|1.0-0.40| + |0.0-0.30| + |0.0-0.20| + |0.0-0.10|) = 0.60 >> tol.
        """
        config_table: dict[str, Any] = {
            "generate_columns": [
                {
                    "name": "event_type",
                    "type": "categorical",
                    "categories": ["login", "purchase", "view", "error"],
                    "weights": [0.40, 0.30, 0.20, 0.10],
                }
            ]
        }
        output_df = pd.DataFrame({"event_type": ["login"] * 500})
        spec = [
            ColumnDistributionSpec(
                table="synthetic_events",
                column="event_type",
                distribution_class="synthetic",
                tolerance=0.05,
                joints_waived=True,
                joints_waived_reason="generate table; no source to correlate",
            )
        ]
        with pytest.raises(AssertionError, match="generate categorical TVD"):
            check_distribution_generate(
                "c_gen_cat", "synthetic_events", spec, output_df, config_table
            )

    def test_shifted_formula_mean_raises(self) -> None:
        """Output mean far from declared params.mean on a formula column must raise.

        Tests the formula-with-params path of check_distribution_generate. Before
        Phase 3b, only type=statistical columns checked mean/std; type=formula with
        params was silently skipped even when params declared a known distribution.

        Output mean ~ 200 vs declared mean = 50: |200-50| = 150 > tol*50 = 5.0.
        """
        rng_np = np.random.default_rng(77)
        config_table: dict[str, Any] = {
            "generate_columns": [
                {
                    "name": "amount",
                    "type": "formula",
                    "formula": "gauss(50.0, 15.0)",
                    "params": {"mean": 50.0, "std": 15.0},
                }
            ]
        }
        # Broken output: mean ~ 200, far outside the 5.0 band (tol=0.10 * 50 = 5).
        output_df = pd.DataFrame({"amount": rng_np.normal(200.0, 15.0, size=1000).tolist()})
        spec = [
            ColumnDistributionSpec(
                table="synthetic_events",
                column="amount",
                distribution_class="synthetic",
                tolerance=0.10,
                joints_waived=True,
                joints_waived_reason="formula generate column; no source",
            )
        ]
        with pytest.raises(AssertionError, match="generate formula mean"):
            check_distribution_generate(
                "c_gen_formula", "synthetic_events", spec, output_df, config_table
            )

    def test_good_generate_output_passes(self) -> None:
        """Correct generate output (list weights + formula params within band) must pass.

        Proves check_distribution_generate does not over-assert for Job C's two new paths:
          - event_type with list weights: seeded sample within 5% TVD of declared weights.
          - amount with formula params: sample mean/std within 10% band of declared params.
        """
        rng_np = np.random.default_rng(88)
        n = 2000
        # Categorical: sample with the declared probabilities.
        cats = rng_np.choice(
            ["login", "purchase", "view", "error"],
            size=n,
            p=[0.40, 0.30, 0.20, 0.10],
        ).tolist()
        # Formula: gauss(50, 15) -> mean ~ 50, std ~ 15.
        amounts = rng_np.normal(50.0, 15.0, size=n).tolist()

        output_df = pd.DataFrame({"event_type": cats, "amount": amounts})
        config_table: dict[str, Any] = {
            "generate_columns": [
                {
                    "name": "event_type",
                    "type": "categorical",
                    "categories": ["login", "purchase", "view", "error"],
                    "weights": [0.40, 0.30, 0.20, 0.10],
                },
                {
                    "name": "amount",
                    "type": "formula",
                    "formula": "gauss(50.0, 15.0)",
                    "params": {"mean": 50.0, "std": 15.0},
                },
            ]
        }
        spec = [
            ColumnDistributionSpec(
                table="synthetic_events",
                column="event_type",
                distribution_class="synthetic",
                tolerance=0.05,
                joints_waived=True,
                joints_waived_reason="generate table test; no source",
            ),
            ColumnDistributionSpec(
                table="synthetic_events",
                column="amount",
                distribution_class="synthetic",
                tolerance=0.10,
                joints_waived=True,
                joints_waived_reason="formula generate column test; no source",
            ),
        ]
        # Must NOT raise: both columns within declared tolerance.
        check_distribution_generate("c_gen_good", "synthetic_events", spec, output_df, config_table)


# ---------------------------------------------------------------------------
# Phase 3b: Orphan remap-masks invariant tooth (TestOrphanRemapMasksTeeth)
# ---------------------------------------------------------------------------


@pytest.mark.testflight
class TestOrphanRemapMasksTeeth:
    """Mutation controls for the remap-masks-orphan invariant tooth.

    HIGH-2 (Phase 3b): the suite previously had no assertion that the remapped
    orphan output value DIFFERS from the source key. An out-of-charset orphan
    key (e.g. all-uppercase "EMP-ORPHAN") passes through FPE unchanged; the
    fk_integrity count still shows expected_orphans=1 so the job appeared to pass.

    Fix: check_remap_masks_orphan enforces output != source key. These controls
    prove it bites on passthrough and passes on genuine remap.

    RED: test_passthrough_orphan_raises -- orphan output equals source key.
    GREEN: test_genuinely_remapped_orphan_passes -- output differs from source.
    Integration: test_check_fk_integrity_with_source_frames -- end-to-end wiring
        through check_fk_integrity(source_frames=...) detects passthrough.
    """

    def test_passthrough_orphan_raises(self) -> None:
        """Output value equal to source key must raise.

        RED: an out-of-charset orphan ("EMP-ORPHAN") that FPE cannot permute
        passes through unchanged; output == source key. The remap-masks-orphan
        check must raise with a clear passthrough-gap message.
        """
        with pytest.raises(AssertionError, match="remap-masks"):
            check_remap_masks_orphan(
                "c_remap_control",
                orphan_source_key="EMP-ORPHAN",
                orphan_output_val="EMP-ORPHAN",  # unchanged passthrough
                child_table="employees",
                child_col="manager_id",
            )

    def test_genuinely_remapped_orphan_passes(self) -> None:
        """Output value differing from source key must NOT raise.

        GREEN: the in-charset orphan "emp99999" is permuted by FPE and produces
        a different value. The check passes cleanly.
        """
        # Must NOT raise: output differs from source key.
        check_remap_masks_orphan(
            "c_remap_control",
            orphan_source_key="emp99999",
            orphan_output_val="zqm38214",  # FPE permuted
            child_table="employees",
            child_col="manager_id",
        )

    def test_check_fk_integrity_with_source_frames_detects_passthrough(self) -> None:
        """check_fk_integrity with source_frames raises when remap yields passthrough.

        End-to-end wiring test: source child FK "EMP-ORPHAN" is an orphan (not in
        source parent pool), and the output child FK is also "EMP-ORPHAN" (unchanged
        passthrough). check_fk_integrity with source_frames supplied and policy=remap
        must raise via the remap-masks-orphan internal check.

        This is the regression proof: the original Job C fixture used "EMP-ORPHAN"
        (out-of-charset) as the orphan key. With the old code, fk_integrity counted
        1 orphan (matching expected_orphans=1) and returned pass -- silently accepting
        the verbatim source key in the output. This test proves the fixed path raises.
        """
        # Build source frames: parent has one key, child has one orphan row.
        parent_ids = [f"EMP-{i:05d}" for i in range(1, 11)]
        src_child_fk = [*parent_ids[:9], "EMP-ORPHAN"]  # last row is orphan
        src_child = pa.table({"employee_id": parent_ids, "manager_id": src_child_fk})

        # Broken output: orphan manager_id is EMP-ORPHAN (unchanged passthrough).
        out_child_fk = [*[f"MK-{i:05d}" for i in range(1, 10)], "EMP-ORPHAN"]
        out_child = pa.table(
            {"employee_id": [f"MK-{i:05d}" for i in range(1, 11)], "manager_id": out_child_fk}
        )

        result = SimpleNamespace(
            outputs={"employees": out_child},
            quality_metrics={},
        )
        # Note: self-FK so parent and child are the same table in relationships.
        relationships = [
            RelationshipSpec(
                parent=RelationshipEndSpec(table="employees", columns=["employee_id"]),
                children=[RelationshipEndSpec(table="employees", columns=["manager_id"])],
                orphan_policy="remap",
                namespace="employee_identity",
            )
        ]
        fk_spec = [
            FKIntegritySpec(
                relationship_name="employee_identity",
                expected_orphans=1,
                policy="remap",
            )
        ]
        source_frames = {"employees": src_child}

        with pytest.raises(AssertionError, match="remap-masks"):
            check_fk_integrity(
                "c_passthrough_control",
                fk_spec,
                result,
                relationships,
                source_frames=source_frames,
            )

    def test_check_fk_integrity_with_source_frames_good_remap_passes(self) -> None:
        """check_fk_integrity with source_frames passes when remap output differs.

        GREEN: source orphan "emp99999" is remapped to "zqm38214" (different). The
        fk_integrity count is 1 (matches expected_orphans=1) and the remap-masks
        check passes because the output value differs from the source key.
        """
        parent_ids = [f"emp{i:05d}" for i in range(1, 11)]
        src_child_fk = [*parent_ids[:9], "emp99999"]
        src_child = pa.table({"employee_id": parent_ids, "manager_id": src_child_fk})

        # Good output: orphan remapped to a different value.
        masked_parent_ids = [f"msk{i:05d}" for i in range(1, 11)]
        out_child_fk = [*masked_parent_ids[:9], "zqm38214"]  # remapped != source
        out_child = pa.table(
            {
                "employee_id": masked_parent_ids,
                "manager_id": out_child_fk,
            }
        )

        result = SimpleNamespace(
            outputs={"employees": out_child},
            quality_metrics={},
        )
        relationships = [
            RelationshipSpec(
                parent=RelationshipEndSpec(table="employees", columns=["employee_id"]),
                children=[RelationshipEndSpec(table="employees", columns=["manager_id"])],
                orphan_policy="remap",
                namespace="employee_identity",
            )
        ]
        fk_spec = [
            FKIntegritySpec(
                relationship_name="employee_identity",
                expected_orphans=1,
                policy="remap",
            )
        ]
        source_frames = {"employees": src_child}

        # Must NOT raise: orphan count matches and output differs from source key.
        check_fk_integrity(
            "c_good_remap_control",
            fk_spec,
            result,
            relationships,
            source_frames=source_frames,
        )


# ---------------------------------------------------------------------------
# Phase 3c: masked-correlation mutation controls (Cramers V relabel-invariant)
# ---------------------------------------------------------------------------


class TestMaskedCorrelationTeeth:
    """Mutation controls for the relabel-invariant masked-correlation invariant.

    Phase 3c closes the carry-forward that the engine crosstab-TVD metric
    cannot measure correlation through a value-changing mask (it scores ~0.0
    even on a faithfully-preserved FPE pair).

    Three controls:

    A. Faithful FPE pair (structure preserved) -> NEW metric PASSES and the
       OLD engine TVD metric is shown to score ~0.0 (proves genuine capability).
    B. Association destroyed by masking (one masked col independently shuffled)
       -> NEW metric RAISES (proves the tooth bites on real decorrelation).
    C. Degenerate input (column with one unique value -> V undefined) -> NO
       assertion, NO divide-by-zero (proves the guard is robust).
    """

    # ------------------------------------------------------------------
    # A. Faithful FPE pair: new metric PASSES, old metric scores ~0.0
    # ------------------------------------------------------------------

    def test_faithful_fpe_pair_passes(self) -> None:
        """A faithfully FPE-bijected correlated pair must PASS the new metric.

        OLD metric failure (the carry-forward):
          Source has cat_code in {EL, CL, FD, HM, SP} perfectly correlated with
          risk_flag in {HI, MD, LO}. Source Cramers V = 1.0.
          After FPE bijection: EL->X1, CL->X2, FD->X3, HM->X4, SP->X5 and
          HI->Y1, MD->Y2, LO->Y3. The engine crosstab-TVD metric compares
          value-LABELED cells. Source (EL, HI) -> count N; output (EL, HI) ->
          count 0 (FPE relabeled). TVD = 1.0 -> similarity = 0.0. Worse than
          a genuinely decorrelated pair (~0.34).

        NEW metric correctness (Phase 3c):
          Cramers V uses contingency COUNTS, not labels. Source (EL, HI) -> N
          rows; output (X1, Y1) -> same N rows (bijection preserves counts).
          V_out = 1.0 = V_src. diff = 0.0 < tol=0.10. PASSES.

        This test directly proves the new capability: the old metric could not
        distinguish a faithfully-masked pair from a decorrelated one; the new
        metric can.
        """
        rng = np.random.default_rng(42)
        n = 500
        # Source: deterministic many-to-one mapping (5 cat_codes -> 3 risk_flags).
        _cat_to_risk = {"EL": "HI", "CL": "MD", "FD": "LO", "HM": "HI", "SP": "MD"}
        _cats = ["EL", "CL", "FD", "HM", "SP"]
        _cat_weights = [0.20, 0.25, 0.20, 0.20, 0.15]

        cat_codes = rng.choice(_cats, size=n, p=_cat_weights).tolist()
        risk_flags = [_cat_to_risk[c] for c in cat_codes]
        source_df = pd.DataFrame({"cat_code": cat_codes, "risk_flag": risk_flags})

        # Simulate FPE bijection: each unique value maps to a new unique value.
        _fpe_cat = {"EL": "X1", "CL": "X2", "FD": "X3", "HM": "X4", "SP": "X5"}
        _fpe_risk = {"HI": "Y1", "MD": "Y2", "LO": "Y3"}
        output_df = pd.DataFrame(
            {
                "cat_code": [_fpe_cat[c] for c in cat_codes],
                "risk_flag": [_fpe_risk[r] for r in risk_flags],
            }
        )

        # --- Prove old metric scores ~0.0 ---
        from decoy_engine.quality.report import compute_quality_report

        report = compute_quality_report(
            source_df,
            output_df,
            joint_columns=[("cat_code", "risk_flag")],
            now_iso=FIXED_TS,
        )
        joints = report.get("pairwise", {}).get("joints", [])
        old_sim = None
        for j in joints:
            if isinstance(j, dict) and set(j.get("columns", [])) == {"cat_code", "risk_flag"}:
                old_sim = j.get("similarity")
                break

        # The old TVD metric must score at or near 0.0 (disjoint label sets).
        # We assert it is below 0.15 -- well below the corr_tol=0.90 the
        # distribution spec would require, proving this pair is genuinely
        # invisible to the old metric.
        assert old_sim is not None, "Old metric: joint similarity not found in report"
        assert float(old_sim) < 0.15, (
            f"OLD engine crosstab-TVD metric scored {old_sim:.4f} on a faithfully "
            f"FPE-masked pair. Expected < 0.15 (near 0.0 due to disjoint labels). "
            f"If this assertion fails, the engine metric may have changed and the "
            f"Phase 3c capability claim needs reassessment."
        )

        # --- Prove new metric PASSES ---
        result = check_correlation_through_masking(
            "p3c_faithful_control",
            "orders",
            "cat_code",
            "risk_flag",
            source_df,
            output_df,
            tol=0.10,
            min_assoc=0.50,
            strategy_a="fpe",
            strategy_b="fpe",
        )

        assert result["diff"] is not None, "New metric: Cramers V undefined (degenerate)"
        assert float(result["diff"]) < 0.01, (
            f"New metric: FPE-masked pair drift = {result['diff']:.4f}. "
            f"A bijection preserves contingency counts exactly; drift must be ~0."
        )
        assert float(result["v_src"]) >= 0.50, (
            f"New metric: v_src={result['v_src']:.4f} < min_assoc=0.50. "
            f"The source pair has insufficient association for a non-vacuous check."
        )

    # ------------------------------------------------------------------
    # B. Association destroyed -> new metric RAISES
    # ------------------------------------------------------------------

    def test_destroyed_association_raises(self) -> None:
        """Shuffling one masked column must trip the Cramers V check.

        RED: source has strong cat_code/risk_flag correlation (V=1.0 on the
        FPE-bijected output). A bug shuffles the output risk_flag column
        independently, destroying the pairing. V_out drops to near 0.
        abs(V_out - V_src) > tol=0.10 -> RAISES.

        This proves the tooth catches real association destruction and is NOT
        vacuous: a correct FPE run (test A) passes; an incorrectly-shuffled
        output fails.
        """
        rng = np.random.default_rng(99)
        n = 500
        _cat_to_risk = {"EL": "HI", "CL": "MD", "FD": "LO", "HM": "HI", "SP": "MD"}
        _cats = ["EL", "CL", "FD", "HM", "SP"]
        cat_codes = rng.choice(_cats, size=n).tolist()
        risk_flags = [_cat_to_risk[c] for c in cat_codes]
        source_df = pd.DataFrame({"cat_code": cat_codes, "risk_flag": risk_flags})

        # FPE bijection on cat_code (structure preserved in isolation).
        _fpe_cat = {"EL": "X1", "CL": "X2", "FD": "X3", "HM": "X4", "SP": "X5"}
        out_cat = [_fpe_cat[c] for c in cat_codes]

        # BUG: risk_flag is independently shuffled (association destroyed).
        _fpe_risk = {"HI": "Y1", "MD": "Y2", "LO": "Y3"}
        out_risk_correct = [_fpe_risk[r] for r in risk_flags]
        # Independent permutation -> destroys the joint structure.
        out_risk_broken = rng.permutation(out_risk_correct).tolist()

        output_df = pd.DataFrame({"cat_code": out_cat, "risk_flag": out_risk_broken})

        with pytest.raises(AssertionError, match="masked_correlation"):
            check_correlation_through_masking(
                "p3c_destroyed_control",
                "orders",
                "cat_code",
                "risk_flag",
                source_df,
                output_df,
                tol=0.10,
                min_assoc=0.0,
                strategy_a="fpe",
                strategy_b="fpe",
            )

    # ------------------------------------------------------------------
    # C. Degenerate input: single-value column -> no assertion
    # ------------------------------------------------------------------

    def test_degenerate_column_handled(self) -> None:
        """A column with one unique value yields V=undefined; no assertion fires.

        When one column has only one unique non-null value, the contingency
        table is degenerate (only one row or one column), min(r-1, c-1) = 0,
        and Cramers V is undefined (0/0). The function must return
        {v_src: None, v_out: None, diff: None} and NOT raise an AssertionError
        or ZeroDivisionError.

        Callers with degenerate data should validate their fixtures independently;
        the degenerate guard exists to prevent the check from crashing on edge
        cases, not to allow genuinely degenerate test data.
        """
        n = 100
        # col_a has only one unique value -> contingency table is 1xM (degenerate).
        source_df = pd.DataFrame(
            {
                "cat_code": ["SAME"] * n,  # all identical
                "risk_flag": ["HI", "MD", "LO"] * (n // 3) + ["HI"] * (n % 3),
            }
        )
        output_df = source_df.copy()
        output_df["cat_code"] = ["XFPE"] * n  # FPE maps SAME -> XFPE (still 1 unique)

        result = check_correlation_through_masking(
            "p3c_degenerate_control",
            "orders",
            "cat_code",
            "risk_flag",
            source_df,
            output_df,
            tol=0.10,
            min_assoc=0.0,
        )

        assert result["diff"] is None, (
            f"Degenerate guard: expected diff=None for single-value column, "
            f"got diff={result['diff']}."
        )
        assert result["v_src"] is None or result["v_out"] is None, (
            "Degenerate guard: at least one V must be None for single-value column."
        )


# ---------------------------------------------------------------------------
# Phase 3c: value-changing-mask passthrough tooth mutation controls
# ---------------------------------------------------------------------------


@pytest.mark.testflight
class TestValueChangingMaskPassthroughTooth:
    """Mutation controls for the value-changing-mask passthrough tooth.

    The tooth detects the BLOCKER-1 class of bug: an FPE column whose charset
    does not cover the data's characters passes every value through unchanged,
    making the mask a silent no-op. The suite previously had no check for this
    because check_correlation_through_masking only compares Cramers V (which is
    identical when output == input, so it scores v_src = v_out correctly -- but
    only because no masking occurred).

    Controls:
    A. No-op mask (BUG): a value-changing mask that left output == input ->
       check_value_changing_not_passthrough RAISES. Constructed directly: fix #42
       closed the original FPE alphanum-on-uppercase passthrough route at the
       engine level (the covering hash now transforms all-out-of-charset values).
    B. Real mask (FIX): ALPHANUM charset on uppercase data -> values permuted
       -> check_value_changing_not_passthrough PASSES.
    C. Non-value-changing strategy -> check is skipped (no assertion).

    The RED/GREEN symmetry proves the tooth catches exactly the bug it targets
    and does not fire on a correctly-masked column.

    BLOCKER-1 root cause proof:
    - charset:alphanum is '0123456789abcdefghijklmnopqrstuvwxyz' (lowercase only).
    - Source values 'EL', 'CL', 'FD', 'HM', 'SP' are uppercase letters.
    - FPE finds no in-charset characters in any value, so every value passes
      through unchanged: output value-set == source value-set.
    - charset:ALPHANUM is '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ...' (includes
      uppercase). FPE permutes each value to a different 2-char string.
    """

    # Reference values matching Job B fixture (uppercase 2-char codes).
    _SRC_CATS = ["EL", "CL", "FD", "HM", "SP"]
    _SRC_RISKS = ["HI", "MD", "LO"]

    @staticmethod
    def _apply_fpe(values: list[str], charset_name: str, tweak: bytes) -> list[str]:
        """Apply real FPE engine with the given charset name and tweak."""
        from decoy_engine.transforms.fpe import _CHARSETS, fpe_encrypt_value

        charset = _CHARSETS[charset_name]
        key = b"tooth-test-key-32bytes-padding--"
        return [fpe_encrypt_value(v, key, charset, tweak) for v in values]

    def test_alphanum_charset_on_uppercase_raises(self) -> None:
        """A value-changing mask that no-op'd (output == input) must raise the tooth.

        RED control. Originally this no-op was produced by FPE charset:alphanum on
        uppercase data (a verbatim passthrough); fix #42's covering hash now
        transforms all-out-of-charset values, so the no-op is constructed directly
        below. The tooth (check_value_changing_not_passthrough) must still RAISE
        when a value-changing strategy left every value unchanged -- the exact bug
        that let BLOCKER-1 ship green (26/26 checks passed while fpe columns were
        verbatim passthroughs).
        """
        cats = self._SRC_CATS * 20  # 100 rows, 5 unique values
        # Fix #42 closed the FPE alphanum-on-uppercase passthrough route at the
        # engine level (the covering hash now transforms all-out-of-charset
        # values). Construct the no-op directly: a value-changing mask that left
        # every value unchanged is exactly the bug this tooth must still detect.
        out_cats = list(cats)

        # Confirm the constructed scenario is a complete passthrough.
        assert all(o == s for o, s in zip(out_cats, cats, strict=True)), (
            "Constructed passthrough must leave every value unchanged."
        )

        source_df = pd.DataFrame({"cat_code": cats})
        output_df = pd.DataFrame({"cat_code": out_cats})

        with pytest.raises(AssertionError, match="value-changing-mask passthrough"):
            check_value_changing_not_passthrough(
                "tooth_test", "orders", "cat_code", "fpe", source_df, output_df
            )

    def test_ALPHANUM_charset_on_uppercase_passes(self) -> None:
        """FPE with ALPHANUM charset on uppercase data must pass the passthrough tooth.

        GREEN (BLOCKER-1 fix): charset:ALPHANUM includes uppercase letters. FPE
        permutes each unique value to a different 2-char string over the 62-char
        ALPHANUM set. The output value-set differs from the source value-set.
        check_value_changing_not_passthrough must NOT raise.

        This proves the fix: ALPHANUM charset -> real permutation -> tooth passes.
        """
        cats = self._SRC_CATS * 20  # 100 rows, 5 unique values
        out_cats = self._apply_fpe(cats, "ALPHANUM", b"cat_code")  # real permutation

        # Prove the fix: at least one output value differs from its source value.
        assert any(o != s for o, s in zip(out_cats, cats, strict=True)), (
            "Expected ALPHANUM FPE to permute at least one uppercase value, "
            "but all outputs are identical to their source values. "
            "The FPE module or test key may have changed."
        )
        # Additionally: source and output value-sets must be disjoint.
        src_set = set(self._SRC_CATS)
        out_set = set(out_cats)
        assert src_set.isdisjoint(out_set), (
            f"ALPHANUM FPE output value-set {sorted(out_set)} is not disjoint "
            f"from source value-set {sorted(src_set)}. "
            f"Expected full relabeling under this key."
        )

        source_df = pd.DataFrame({"cat_code": cats})
        output_df = pd.DataFrame({"cat_code": out_cats})

        # Must NOT raise: output set differs from source set.
        check_value_changing_not_passthrough(
            "tooth_test", "orders", "cat_code", "fpe", source_df, output_df
        )

    def test_risk_flag_alphanum_charset_raises(self) -> None:
        """A no-op'd value-changing mask on risk_flag (HI/MD/LO) must raise the tooth.

        Mirrors test_alphanum_charset_on_uppercase_raises for the second column of
        the BLOCKER-1 pair (constructed no-op; fix #42 closed the FPE route).
        """
        risks = self._SRC_RISKS * 33 + self._SRC_RISKS[:1]  # ~100 rows
        # Fix #42: the FPE alphanum-on-uppercase passthrough route is closed; the
        # tooth is verified against a directly-constructed no-op (see cat_code test).
        out_risks = list(risks)

        assert all(o == s for o, s in zip(out_risks, risks, strict=True)), (
            "Constructed passthrough must leave every value unchanged."
        )

        source_df = pd.DataFrame({"risk_flag": risks})
        output_df = pd.DataFrame({"risk_flag": out_risks})

        with pytest.raises(AssertionError, match="value-changing-mask passthrough"):
            check_value_changing_not_passthrough(
                "tooth_test", "orders", "risk_flag", "fpe", source_df, output_df
            )

    def test_non_value_changing_strategy_skipped(self) -> None:
        """A passthrough strategy column must not be checked by this tooth.

        The tooth only fires for _VALUE_CHANGING_STRATEGIES (fpe, hash, code_set).
        A passthrough column with identical source and output must NOT raise even
        though the value-sets are equal, because passthrough is not expected to
        change values.

        This proves the tooth does not over-assert on strategies that are not
        supposed to change values.
        """
        vals = ["low", "mid", "high", "low", "mid"]
        source_df = pd.DataFrame({"qty_band": vals})
        output_df = pd.DataFrame({"qty_band": vals})  # identical (passthrough)

        # Must NOT raise: strategy=passthrough is not in _VALUE_CHANGING_STRATEGIES.
        check_value_changing_not_passthrough(
            "tooth_test", "orders", "qty_band", "passthrough", source_df, output_df
        )

    def test_good_fpe_with_matching_charset_passes(self) -> None:
        """FPE with digits charset on digit-only data must pass the tooth.

        GREEN control: customer_id is FPE-masked with charset:digits (correct match).
        FPE permutes each digit string to a different digit string. The output
        value-set differs from the source value-set.

        Proves the tooth does not fire on any correctly-configured FPE column.
        """
        src_ids = [f"CU{i:06d}" for i in range(1, 101)]  # 100 unique IDs
        # FPE with digits charset: only the digit part is permuted, "CU" prefix preserved.
        out_ids = self._apply_fpe(src_ids, "digits", b"customer_id")

        # With preserve_separators=True (default), "CU" prefix is unchanged.
        # At least the digit portion is permuted so at least some outputs differ.
        src_digit_sets = {s[2:] for s in src_ids}  # digit suffixes
        out_digit_sets = {o[2:] for o in out_ids}
        # The digit suffix sets should differ (FPE genuinely permuted the digits).
        assert src_digit_sets != out_digit_sets, (
            "Expected FPE with digits charset to permute the numeric suffix. "
            "If all outputs match source, the FPE module may have a bug."
        )

        source_df = pd.DataFrame({"customer_id": src_ids})
        output_df = pd.DataFrame({"customer_id": out_ids})

        # Must NOT raise: some values changed.
        check_value_changing_not_passthrough(
            "tooth_test", "customers", "customer_id", "fpe", source_df, output_df
        )
