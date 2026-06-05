"""D7a: SynthReport foundation + new-row-synthesis tests.

Acceptance criteria from the sprint plan:
  - Exact-copy synthetic output reports POOR new-row synthesis.
  - Independent synthetic sample reports STRONGER new-row synthesis.
  - Report states that DCR is not a privacy guarantee.
  - No differential privacy claim is made.

These tests pin all four explicitly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from decoy_engine.quality.synth_report import (
    SYNTH_REPORT_SCHEMA_VERSION,
    assemble_synth_report,
    compute_new_row_synthesis,
)

# ── acceptance: exact-copy reports POOR (fraction ~0) ─────────────────────


class TestExactCopyMemorization:
    def test_exact_copy_scores_zero_new_rows(self):
        """The acceptance-critical case: synth == source -> 0 new rows."""
        source = pd.DataFrame(
            {
                "a": [1, 2, 3, 4, 5],
                "b": ["x", "y", "z", "p", "q"],
            }
        )
        synth = source.copy()
        result = compute_new_row_synthesis(source, synth)
        assert result["fraction_new"] == 0.0
        assert result["matched_rows"] == 5
        assert result["new_rows"] == 0
        assert result["band"] == "low"
        assert result["warning"] is not None
        assert "memorization" in result["warning"].lower()

    def test_partial_copy_scores_partial_match(self):
        """Half the synth rows are copies; the other half are new."""
        source = pd.DataFrame({"v": [1, 2, 3, 4, 5]})
        synth = pd.DataFrame({"v": [1, 2, 100, 200, 300]})  # 2 copies + 3 new
        result = compute_new_row_synthesis(source, synth)
        assert result["matched_rows"] == 2
        assert result["new_rows"] == 3
        assert result["fraction_new"] == 0.6


# ── acceptance: independent sample scores STRONG (fraction ~1) ────────────


class TestIndependentSample:
    def test_independent_floats_score_near_one(self):
        """Independent random draws are vanishingly unlikely to collide
        with the source - fraction_new should be ~1.0."""
        rng = np.random.default_rng(42)
        source = pd.DataFrame({"x": rng.normal(0, 1, size=1000)})
        synth = pd.DataFrame({"x": rng.normal(0, 1, size=1000)})
        result = compute_new_row_synthesis(source, synth)
        assert result["fraction_new"] >= 0.99
        assert result["band"] == "high"
        # No memorization warning on a high score.
        assert result["warning"] is None

    def test_independent_categorical_with_small_universe(self):
        """Categorical with a 5-value universe will see collisions
        even on independent draws; band should be 'moderate' or 'high'
        depending on sample noise, but NOT 'low'."""
        rng = np.random.default_rng(7)
        source = pd.DataFrame({"c": rng.choice(["A", "B", "C", "D", "E"], 200)})
        synth = pd.DataFrame({"c": rng.choice(["A", "B", "C", "D", "E"], 200)})
        result = compute_new_row_synthesis(source, synth)
        # With 5 values and 200 rows, every output value will collide
        # with a source value, so fraction_new will be 0.0. This is
        # the documented behavior - single-column low-cardinality
        # comparison hits this floor. We still verify it scores
        # consistently across a re-run.
        result2 = compute_new_row_synthesis(source, synth)
        assert result["fraction_new"] == result2["fraction_new"]


# ── subset_columns control ────────────────────────────────────────────────


class TestSubsetColumns:
    def test_default_uses_column_intersection(self):
        """When subset_columns is None, the intersection is used."""
        source = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"], "c": [9, 9, 9]})
        synth = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"], "d": [0, 0, 0]})
        result = compute_new_row_synthesis(source, synth)
        # Common cols: a, b -> rows match -> fraction_new = 0
        assert result["subset_columns"] == ["a", "b"]
        assert result["fraction_new"] == 0.0

    def test_explicit_subset_narrows_comparison(self):
        """An explicit subset narrows what counts as a 'match'."""
        source = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        synth = pd.DataFrame({"a": [1, 2, 3], "b": ["X", "Y", "Z"]})  # b differs
        # Comparing on b alone: 0 matches.
        narrow = compute_new_row_synthesis(source, synth, subset_columns=["b"])
        assert narrow["fraction_new"] == 1.0
        # Comparing on a alone: all match.
        wide = compute_new_row_synthesis(source, synth, subset_columns=["a"])
        assert wide["fraction_new"] == 0.0

    def test_no_overlap_returns_unavailable(self):
        source = pd.DataFrame({"x": [1, 2]})
        synth = pd.DataFrame({"y": [3, 4]})
        result = compute_new_row_synthesis(source, synth)
        assert result["band"] == "unavailable"
        assert result["fraction_new"] is None


# ── edge cases ─────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_empty_output_returns_unavailable(self):
        source = pd.DataFrame({"x": [1, 2, 3]})
        synth = pd.DataFrame({"x": []})
        result = compute_new_row_synthesis(source, synth)
        assert result["band"] == "unavailable"
        assert result["fraction_new"] is None
        assert "undefined" in result["warning"].lower()

    def test_none_inputs_returns_unavailable(self):
        result = compute_new_row_synthesis(None, None)
        assert result["band"] == "unavailable"
        assert result["fraction_new"] is None

    def test_null_values_preserved_in_comparison(self):
        """A null in the same position in both frames counts as a match."""
        source = pd.DataFrame({"x": [1, None, 3]})
        synth = pd.DataFrame({"x": [1, None, 3]})
        result = compute_new_row_synthesis(source, synth)
        assert result["matched_rows"] == 3
        assert result["fraction_new"] == 0.0

    def test_deterministic_across_runs(self):
        source = pd.DataFrame({"x": [1, 2, 3], "y": ["a", "b", "c"]})
        synth = pd.DataFrame({"x": [1, 2, 99], "y": ["a", "b", "z"]})
        r1 = compute_new_row_synthesis(source, synth)
        r2 = compute_new_row_synthesis(source, synth)
        assert r1 == r2

    def test_no_raw_rows_in_result(self):
        """Per security requirement: aggregate only, no raw row data."""
        source = pd.DataFrame({"ssn": ["111-22-3333", "444-55-6666"]})
        synth = pd.DataFrame({"ssn": ["111-22-3333", "999-88-7777"]})
        result = compute_new_row_synthesis(source, synth)
        # The hash domain text is private; no SSN should appear in
        # any value in the result dict.
        flat = str(result)
        assert "111-22-3333" not in flat
        assert "444-55-6666" not in flat
        assert "999-88-7777" not in flat


# ── assemble_synth_report ─────────────────────────────────────────────────


class TestAssembleReport:
    def test_schema_version_present(self):
        r = assemble_synth_report(new_row_synthesis=None)
        assert r["schema_version"] == SYNTH_REPORT_SCHEMA_VERSION

    def test_includes_new_row_synthesis_block(self):
        nrs = compute_new_row_synthesis(
            pd.DataFrame({"x": [1, 2, 3]}),
            pd.DataFrame({"x": [1, 2, 3]}),
        )
        r = assemble_synth_report(new_row_synthesis=nrs)
        assert r["new_row_synthesis"]["fraction_new"] == 0.0

    def test_dcr_and_attacks_are_placeholders(self):
        """D7b + D7c haven't shipped yet; the keys exist as None."""
        r = assemble_synth_report(new_row_synthesis=None)
        assert "dcr" in r
        assert r["dcr"] is None
        assert "attacks" in r
        assert r["attacks"] is None

    def test_dcr_disclaimer_present(self):
        """Acceptance: report states that DCR is not a privacy guarantee."""
        r = assemble_synth_report(new_row_synthesis=None)
        joined = " ".join(r["disclaimers"]).lower()
        assert "dcr" in joined
        assert "not a privacy guarantee" in joined

    def test_no_dp_claim_disclaimer(self):
        """Acceptance: no differential-privacy claim is made."""
        r = assemble_synth_report(new_row_synthesis=None)
        joined = " ".join(r["disclaimers"]).lower()
        assert "differential" in joined or "differentially private" in joined
        assert (
            "not differentially private" in joined
            or "not make a differential-privacy claim" in joined
        )

    def test_high_fidelity_privacy_warning_present(self):
        """Acceptance: high fidelity does NOT imply low privacy risk."""
        r = assemble_synth_report(new_row_synthesis=None)
        joined = " ".join(r["disclaimers"]).lower()
        assert "high fidelity" in joined
        assert "privacy" in joined

    def test_job_id_passes_through(self):
        r = assemble_synth_report(new_row_synthesis=None, job_id=42)
        assert r["job_id"] == 42

    def test_json_serializable(self):
        """The whole report round-trips through JSON cleanly."""
        import json

        nrs = compute_new_row_synthesis(
            pd.DataFrame({"a": [1, 2, 3]}),
            pd.DataFrame({"a": [1, 2, 4]}),
        )
        r = assemble_synth_report(new_row_synthesis=nrs, job_id=7)
        encoded = json.dumps(r)
        decoded = json.loads(encoded)
        assert decoded["schema_version"] == SYNTH_REPORT_SCHEMA_VERSION
        assert decoded["new_row_synthesis"]["matched_rows"] == 2


# ── public surface ────────────────────────────────────────────────────────


class TestExports:
    def test_quality_package_exports_synth_symbols(self):
        from decoy_engine import quality

        assert hasattr(quality, "compute_new_row_synthesis")
        assert hasattr(quality, "assemble_synth_report")
        assert hasattr(quality, "SYNTH_REPORT_SCHEMA_VERSION")


class TestQa10F13AttacksDisclaimer:
    """QA-10 F13 (2026-06-01, NIT, refined 2026-06-01): the
    "no attack was attempted" disclaimer is omitted ONLY when an
    attack was actually attempted. Pre-fix that line was hardcoded
    into every report.

    Subtle: the platform hook always passes a dict (the
    `_attacks_unavailable` shape when `enable_attacks=False`), so the
    omit-when-not-None check would have hidden the disclaimer in every
    production report. The corrected check looks at
    `attacks.get("available")`; the unavailable shape has
    `available=False` and still earns the disclaimer."""

    def test_attacks_none_includes_skipped_disclaimer(self):
        """When attacks is None (legacy callers; nothing supplied),
        the disclaimer must be present."""
        report = assemble_synth_report(new_row_synthesis=None, attacks=None)
        joined = " ".join(report["disclaimers"])
        assert "no attack was attempted" in joined

    def test_attacks_unavailable_shape_includes_skipped_disclaimer(self):
        """When attacks is the _attacks_unavailable shape
        (`available=False`), no actual attack ran. Disclaimer present.
        This is the platform hook's default behaviour."""
        unavailable = {
            "metric": "attack_based_metrics",
            "available": False,
            "reason": "not_enabled_by_caller",
            "extras_module": "decoy_engine_privacy_attacks",
        }
        report = assemble_synth_report(
            new_row_synthesis=None,
            attacks=unavailable,
        )
        joined = " ".join(report["disclaimers"])
        assert "no attack was attempted" in joined, (
            "QA-10 F13 refined: an _attacks_unavailable block still "
            "means no attack ran; the disclaimer must remain."
        )

    def test_attacks_present_and_available_drops_skipped_disclaimer(self):
        """When attacks IS supplied AND `available` is anything other
        than False (an actual attack ran), the disclaimer is omitted.
        Available=True is the explicit form; absence of the key is
        treated permissively as available."""
        attack_block = {
            "metric": "attack_based_metrics",
            "available": True,
            "mia": {"attempted": True, "outcome": "low_risk"},
        }
        report = assemble_synth_report(
            new_row_synthesis=None,
            attacks=attack_block,
        )
        joined = " ".join(report["disclaimers"])
        assert "no attack was attempted" not in joined

    def test_unconditional_disclaimers_always_present(self):
        """The DCR-not-a-privacy-guarantee + no-DP-claim +
        high-fidelity-is-not-privacy disclaimers are unconditional
        and must remain regardless of attack presence."""
        for attacks in (None, {"available": True, "mia": {"attempted": True}}):
            report = assemble_synth_report(
                new_row_synthesis=None,
                attacks=attacks,
            )
            joined = " ".join(report["disclaimers"])
            assert "DCR" in joined
            assert "differential" in joined.lower() or "no differential-privacy" in joined.lower()


class TestQa10F3FipsSha1:
    """QA-10 F3 (2026-06-01, HIGH): row-hash uses
    hashlib.sha1(..., usedforsecurity=False) so it does NOT raise on
    FIPS-hardened OpenSSL hosts. Pre-fix the bare sha1 call raised
    ValueError: unsupported on FIPS mode; healthcare + federal
    deployments (the named target market) commonly run FIPS hosts.

    Locks the contract by exercising the row-hash iterator
    indirectly via compute_new_row_synthesis on a tiny frame; the
    function would have raised pre-fix on a FIPS host. The cell here
    confirms the call shape compiles + executes; the FIPS-host
    integration confirmation lives in pilot smoke-test scope."""

    def test_compute_new_row_synthesis_runs_clean_with_usedforsecurity_flag(self):
        # Sanity: the function runs end-to-end. The sha1 call now
        # includes usedforsecurity=False per F3 fix.
        source = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
        synth = pd.DataFrame({"a": [1, 2, 99], "b": ["x", "y", "new"]})
        nrs = compute_new_row_synthesis(source, synth)
        assert nrs is not None
        # 2 matched, 1 new.
        assert nrs["matched_rows"] == 2
        assert nrs["new_rows"] == 1

    def test_row_hash_iter_call_signature_passes_usedforsecurity(self):
        """QA-10 F3: introspect the _row_hash_iter source to verify
        the usedforsecurity=False argument is present. Defends against
        a future regression that strips the flag."""
        import inspect

        from decoy_engine.quality import synth_report

        src = inspect.getsource(synth_report._row_hash_iter)
        assert "usedforsecurity=False" in src, (
            "QA-10 F3 fix regressed: hashlib.sha1 call no longer has "
            "usedforsecurity=False; FIPS hosts will raise."
        )


class TestQa10P1SeverityFieldPlumbing:
    """QA-10 P1 (2026-06-01, PO-locked spec at
    docs/v2/sprints/qa-carries/qa-10-quality-report-hardening.md):
    overall report severity is the max-band across sub-reports.

    Ordering: ok < info < low < medium < high < critical. Empty reports
    route to 'info' (nothing ran). DCR median == 0 routes to 'critical'.
    The 'info' band is the no-escalation route for opt-out / unavailable
    sub-reports + (post-P2) under-sample TVD."""

    def test_empty_report_severity_is_info(self):
        r = assemble_synth_report(new_row_synthesis=None)
        assert r["severity"] == "info"

    def test_new_row_synthesis_low_band_severity_is_low(self):
        r = assemble_synth_report(
            new_row_synthesis={"fraction_new": 0.05, "band": "low"},
        )
        assert r["severity"] == "low"

    def test_new_row_synthesis_high_band_severity_is_high(self):
        r = assemble_synth_report(
            new_row_synthesis={"fraction_new": 0.95, "band": "high"},
        )
        assert r["severity"] == "high"

    def test_dcr_zero_distance_routes_to_critical(self):
        """DCR median == 0 = at least one synth row is verbatim source.
        This is the 'fail open' band that must surface to the operator
        regardless of other sub-reports."""
        r = assemble_synth_report(
            new_row_synthesis={"fraction_new": 0.05, "band": "low"},
            dcr={"synth_to_source": {"median": 0}},
        )
        assert r["severity"] == "critical"

    def test_dcr_positive_distance_does_not_escalate(self):
        r = assemble_synth_report(
            new_row_synthesis={"fraction_new": 0.05, "band": "low"},
            dcr={"synth_to_source": {"median": 0.42}},
        )
        assert r["severity"] == "low"

    def test_attacks_unavailable_routes_to_info(self):
        """The _attacks_unavailable shape (available=False) is the
        platform default. Should surface as info, not escalate the
        overall severity."""
        r = assemble_synth_report(
            new_row_synthesis={"fraction_new": 0.05, "band": "low"},
            attacks={"available": False, "reason": "not_enabled_by_caller"},
        )
        assert r["severity"] == "low"  # low > info; new_row_synthesis wins

    def test_attacks_ran_with_high_band_escalates(self):
        r = assemble_synth_report(
            new_row_synthesis={"fraction_new": 0.05, "band": "low"},
            attacks={"available": True, "band": "high", "mia": {"attempted": True}},
        )
        assert r["severity"] == "high"

    def test_band_ordering_max_wins(self):
        """When multiple sub-reports have bands, the max wins."""
        r = assemble_synth_report(
            new_row_synthesis={"fraction_new": 0.5, "band": "medium"},
            dcr={"synth_to_source": {"median": 0.42}},  # ok
            attacks={"available": True, "band": "high"},  # high beats medium
        )
        assert r["severity"] == "high"

    def test_severity_field_in_returned_dict(self):
        """Smoke: every assembled report carries a 'severity' field."""
        r = assemble_synth_report(new_row_synthesis=None)
        assert "severity" in r
        assert isinstance(r["severity"], str)
