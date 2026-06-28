"""Test-flight mutation-control suite (Phase 0 stubs).

Anti-vacuity: an assertion that cannot fail on a real regression manufactures
false confidence. Each invariant family requires a known-bad mutation control
that applies a specific regression to a job config / fixture and asserts the
corresponding invariant RAISES. This is the engine analogue of
scripts/prove_regression.py (which re-runs a regression test against the
pre-fix baseline and requires it to fail there).

Phase 0: stubs that document the intended mutation controls. Phase 1-2 fills
in real bodies as each invariant family is implemented.

Mutation controls per family (plan section 9):
  - Distribution constant-collapse: swap fpe to constant; assert constant-
    collapse + grade-floor invariant raises.
  - Fake coarsening: replace bucketize with identity; assert real-coarsening
    guard raises (cardinality did not drop).
  - Correlation destroyed: replace correlated column with independent shuffle;
    assert correlation-preservation invariant raises.
  - FK break: delete a relationship; assert FK-integrity invariant raises.
  - Quarantine miscount: inject one extra bad row not in the manifest; assert
    quarantine-count invariant raises.
  - Sentinel leak: set masked column to passthrough; assert sentinel scan raises.
  - Computed-column corruption: perturb a derived formula; assert computed-column
    invariant raises.
  - Coverage rot: register a fake strategy key in SCALAR_HANDLERS within the test;
    assert coverage guard raises.

All tests are marked `testflight` (same as test_testflight.py). Run via:
  pytest testflight -m testflight
  python scripts/test_flight.py --mutate <kind>
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.testflight


class TestDistributionTeeth:
    """Mutation controls for the distribution-fidelity invariant family."""

    def test_constant_collapse_detected(self) -> None:
        """Swapping fpe to a constant strategy must trip the constant-collapse guard.

        Phase 1: build a minimal job config where an fpe column is replaced by a
        constant value, run the pipeline, and assert check_distribution_mask raises
        AssertionError naming the affected column and the constant-collapse guard.
        """
        pytest.skip("Phase 1: constant-collapse mutation control pending.")

    def test_fake_coarsening_detected(self) -> None:
        """Replacing bucketize with identity must trip the real-coarsening guard.

        Phase 1: modify a job config so a bucketize column uses passthrough
        instead, run the pipeline, and assert check_distribution_mask raises
        AssertionError naming the affected column and the coarsening guard
        (output cardinality did not drop below source cardinality).
        """
        pytest.skip("Phase 1: fake-coarsening mutation control pending.")

    def test_correlation_destroyed_detected(self) -> None:
        """Replacing a correlated column with independent shuffle must trip the
        correlation-preservation invariant.

        Phase 1: modify a job config so a correlated column (e.g. claim_amount
        correlated with diagnosis chapter) uses an independent shuffle, run the
        pipeline, and assert check_distribution_mask raises AssertionError naming
        the affected joint pair and the correlation tolerance.
        """
        pytest.skip("Phase 1: correlation-destroyed mutation control pending.")


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

        Phase 2: modify a derived column's expression so its output is off by one
        (e.g. multiply by units + 1 instead of units), run the pipeline, and assert
        check_computed_columns raises AssertionError naming the affected column.
        """
        pytest.skip("Phase 2: formula-corruption mutation control pending.")


class TestCoverageRotTeeth:
    """Mutation controls for the strategy-coverage guard."""

    def test_coverage_rot_detected(self) -> None:
        """Registering a fake strategy key must trip the coverage guard.

        Phase 4: monkeypatch SCALAR_HANDLERS to add a fake key not in any manifest's
        strategy_coverage, then assert check_strategy_coverage raises AssertionError
        identifying the uncovered strategy. This proves the guard reads the LIVE
        registry rather than a static list.
        """
        pytest.skip("Phase 4: coverage-rot mutation control pending.")
