"""SPRINT-2 (TEST-4): a single canonical sweep over the engine's enumerated
hard caps / typed errors, per the readiness doc's Axis B/C matrix
(`~/.claude/plans/decoy-enterprise-readiness-testing.md`, section 2).

Five of the six enumerated targets already have solid, boundary-precise
coverage elsewhere in the suite; this module cites them (comment only, no
duplicate test body) rather than re-implementing near-identical checks. The
sixth (PKDuplicatesError) is a genuine gap: the exception class exists and
is fully specified, but is unreachable from any current code path (see the
class-level test below and the TEST-3 write-up for the full trail).

1. HC-5 100k-distinct / 16 MiB code-set caps (`DistributionSnapshotError`):
   tests/unit/quality/test_snapshot.py::test_high_cardinality_distinct_limit_exceeded
   (+ ..._boundary_ok for the exact-limit-is-OK edge) and
   ::test_high_cardinality_label_bytes_limit_exceeded.

2. 30-distinct free-text cliff (reclassify categorical->freetext AND the
   retention warn-gate fires): tests/unit/quality/test_retention_gate.py
   ::TestCliffCase::test_freetext_column_scores_zero_and_warns.

3. 256 MB pool cache budget (`PoolCapacityError(pool_exceeds_cache_budget)`):
   tests/unit/generation/pool/test_cache.py::TestEviction::test_pool_larger_than_budget_raises.

4. 5M-row out-of-core route / 7.5M-row full-frame reject thresholds
   (`ExecutionError(fk_full_frame_oom_risk_rejected)`):
   tests/unit/execution/test_out_of_core_routing.py (decision + dispatch
   coverage) and tests/unit/execution/test_out_of_core_public_api.py
   ::test_thresholds_match_the_live_routing_defaults (pins the exact
   5_000_000 / 7_500_000 constants).

5. FpeUnencryptableError -> job-fatal StrategyError(fpe_unencryptable_value):
   tests/unit/execution/test_fpe_remap_out_of_charset_fail_closed.py
   ::test_all_out_of_charset_orphan_fails_closed.

6. PKDuplicatesError: NO equivalent test exists anywhere in the suite, and
   none can meaningfully be written for the end-to-end contract described
   in its docstring (strict-by-default, `DECOY_PK_LENIENT=1` opt-out) --
   that contract is not wired to any current code path. See the class
   below.
"""

from __future__ import annotations

import pytest

from decoy_engine.errors import PKDuplicatesError


class TestPKDuplicatesErrorGap:
    """PKDuplicatesError (errors.py, code='pk.duplicates') is fully specified
    -- constructor, message formatting, the DECOY_PK_LENIENT env var contract,
    a Tier-1 audit citation -- but a repo-wide search finds zero call sites
    outside its own class body:

        grep -rn "PKDuplicatesError(" src/decoy_engine/
        -> only the class definition itself in errors.py.

    Nor is the `primary_key` config key it was written against ever parsed
    by the plan schema (`grep -rn "primary_key" src/decoy_engine/plan/`
    finds nothing). The class predates the V1 -> V2 architecture migration
    (introduced 2026-05-20 in commit d73d7a6, against the removed
    `graph/ops/generate_op.py` V1 pipeline); V2's actual PK-uniqueness
    mechanism is a different, opt-in, NON-RAISING code path:
    `validation/post/_checks/_pk_uniqueness.run_pk_uniqueness` (exercised by
    tests/unit/validation/test_structural_scans.py::test_duplicate_pk_fails),
    reachable only via `PostValidationRunner`, which has no production call
    from `run_pipeline`. That gap is ALREADY FILED as DE-06 in
    docs/adversarial-architecture-review-2026-07-12.md ("validated nested
    `post_validation` cannot be consumed by the flat key expected by
    `PostValidationRunner`, which has no production call from
    `run_pipeline`") and remains open as of this sprint.

    No triggering test for the documented strict/lenient contract is added
    here: writing one would require inventing a code path that does not
    exist, or asserting the current fail-open behavior as if it were
    intended, which would misrepresent an open gap as a passing contract.
    This class-contract test is the only thing that IS currently real: it
    pins the constructor/message/code shape so a future edit does not
    silently break the one artifact of the contract that remains.
    """

    def test_code_and_message_shape(self) -> None:
        exc = PKDuplicatesError(
            column="patient_id",
            total_non_null=1000,
            unique_values=997,
            strategy="faker",
        )
        assert exc.code == "pk.duplicates"
        assert exc.column == "patient_id"
        assert exc.duplicate_count == 3
        assert "3 duplicate value(s)" in str(exc)
        assert "DECOY_PK_LENIENT=1" in str(exc)

    def test_message_without_strategy_omits_remediation_hint(self) -> None:
        # Strategy is optional; without it there's no "switch to sequence"
        # remediation hint to give (the hint is strategy-specific).
        exc = PKDuplicatesError(column="id", total_non_null=10, unique_values=10)
        assert exc.duplicate_count == 0
        assert "DECOY_PK_LENIENT" not in str(exc)

    def test_unreachable_from_any_current_code_path(self) -> None:
        # Documents (does not merely assert) the gap: importing the class
        # succeeds (it is still shipped, public API surface), but there is
        # no production call site. This is a regression tripwire for the
        # OPPOSITE direction -- if a future change wires PKDuplicatesError
        # into run_pipeline, this test's docstring (not its assertion) goes
        # stale first and should be the prompt to delete this test and add
        # a real end-to-end trigger in its place.
        import decoy_engine.execution._pipeline as _pipeline_mod

        assert "PKDuplicatesError" not in _pipeline_mod.__dict__
        with pytest.raises(TypeError):
            # Confirms the class still requires the documented fields (no
            # accidental default-everything constructor drift).
            PKDuplicatesError()  # type: ignore[call-arg]
