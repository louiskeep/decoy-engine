"""End-to-end golden test: HIPAA-shaped DataFrame -> run_storm -> recommend.

This is the load-bearing integration test for the apply-Disguise loop. It
asserts the user-facing promise: feed in a healthcare-shaped CSV and the
top recommendation is the HIPAA Disguise with an apply_payload that
covers every PHI column.

Lives in `tests/integration/` so it runs alongside the existing
test_masker.py and test_generator.py end-to-end suites.
"""

import warnings

import pandas as pd
import pytest
import yaml

from decoy_engine.forecast import recommend
from decoy_engine.storm import run_storm


@pytest.fixture
def hipaa_dataframe() -> pd.DataFrame:
    """A synthetic dataset with all the PHI shapes HIPAA cares about.

    Sized to exercise Plan B-1's data-driven k-anonymity: dob / zip /
    gender repeat across rows so they qualify as quasi-id candidates
    (unique_rate < 0.95) and form low-k combos. ssn / email / phone
    remain unique to keep direct-identifier detection working.
    """
    return pd.DataFrame({
        "patient_id":  [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
        "first_name":  ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank",
                        "Grace", "Henry", "Iris", "Jack", "Kate", "Leo"],
        "last_name":   ["Smith", "Jones", "Davis", "Martin", "Wilson", "Brown",
                        "Taylor", "Anderson", "Thomas", "Jackson", "White", "Harris"],
        "ssn":         ["123-45-6789", "555-12-3456", "111-22-3333",
                        "444-55-6677", "222-99-1212", "333-44-5566",
                        "777-88-9999", "888-11-2222", "999-33-4444",
                        "121-21-2121", "343-43-4343", "565-65-6565"],
        # 5 distinct DOBs across 12 rows (unique_rate 0.42 < 0.95)
        # so dob qualifies as a quasi-id candidate. Includes the
        # 0001-01-01 / 9999-12-31 sentinel values so the
        # RiskFlags coverage assertion still has data to find.
        "dob":         ["1985-03-15", "1990-07-22", "1972-11-08",
                        "1985-03-15", "1990-07-22", "1972-11-08",
                        "1985-03-15", "0001-01-01", "1972-11-08",
                        "1985-03-15", "9999-12-31", "1972-11-08"],
        # Four distinct ZIPs, varied frequencies.
        "zip":         ["90210", "10001", "60601", "94102",
                        "90210", "10001", "60601", "94102",
                        "90210", "10001", "60601", "94102"],
        "gender":      ["F", "M", "F", "M", "F", "M",
                        "F", "M", "F", "M", "F", "M"],
        "email":       ["a@b.com", "c@d.org", "e@f.io", "g@h.co",
                        "i@j.net", "k@l.com", "m@n.io", "o@p.co",
                        "q@r.net", "s@t.com", "u@v.io", "w@x.co"],
        "phone":       ["555-234-5678", "555-345-6789", "555-456-7890",
                        "555-567-8901", "555-678-9012", "555-789-0123",
                        "555-890-1234", "555-901-2345", "555-012-3456",
                        "555-123-4567", "555-234-0987", "555-345-1098"],
        "notes":       ["clean", "fine", "N/A", "ok", "TBD", "",
                        "clean", "fine", "N/A", "ok", "TBD", ""],
    })


@pytest.fixture
def storm_then_forecast(hipaa_dataframe: pd.DataFrame):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # pandas dateutil noise; not test-relevant
        profile = run_storm(hipaa_dataframe, "patients_2026Q1.csv")
    report = recommend(profile)
    return profile, report


class TestHIPAARecommendation:
    def test_hipaa_is_top_recommendation(self, storm_then_forecast):
        _, report = storm_then_forecast
        assert report.disguise_recommendations, "expected at least one Disguise recommendation"
        top = report.disguise_recommendations[0]
        assert top.disguise_id == "hipaa"

    def test_hipaa_score_above_default(self, storm_then_forecast):
        _, report = storm_then_forecast
        score_by_id = {r.disguise_id: r.match_score for r in report.disguise_recommendations}
        assert "hipaa" in score_by_id and "default" in score_by_id
        assert score_by_id["hipaa"] > score_by_id["default"]

    def test_hipaa_covers_all_phi_columns(self, storm_then_forecast):
        _, report = storm_then_forecast
        top = next(r for r in report.disguise_recommendations if r.disguise_id == "hipaa")
        # All HIPAA-relevant columns should be in matched_fields.
        expected_subset = {"first_name", "last_name", "ssn", "dob", "zip", "email", "phone"}
        assert expected_subset.issubset(set(top.matched_fields)), (
            f"expected HIPAA matched_fields to cover {expected_subset}, got {top.matched_fields}"
        )

    def test_quasi_identifier_group_appears_in_reasoning(self, storm_then_forecast):
        _, report = storm_then_forecast
        top = next(r for r in report.disguise_recommendations if r.disguise_id == "hipaa")
        assert "quasi-identifier" in top.reasoning


class TestApplyPayload:
    def test_apply_payload_field_masks_have_correct_shape(self, storm_then_forecast):
        _, report = storm_then_forecast
        top = next(r for r in report.disguise_recommendations if r.disguise_id == "hipaa")
        masks = top.apply_payload["field_masks"]

        # Every entry must have column + type, the masking_rules contract.
        for m in masks:
            assert "column" in m, f"missing column key in {m}"
            assert "type" in m, f"missing type key in {m}"

    def test_ssn_column_gets_hash(self, storm_then_forecast):
        _, report = storm_then_forecast
        top = next(r for r in report.disguise_recommendations if r.disguise_id == "hipaa")
        ssn_mask = next(m for m in top.apply_payload["field_masks"] if m["column"] == "ssn")
        assert ssn_mask["type"] == "hash"

    def test_first_name_gets_faker_name(self, storm_then_forecast):
        _, report = storm_then_forecast
        top = next(r for r in report.disguise_recommendations if r.disguise_id == "hipaa")
        fn_mask = next(m for m in top.apply_payload["field_masks"] if m["column"] == "first_name")
        assert fn_mask["type"] == "faker"
        assert fn_mask["faker_type"] == "name"

    def test_dob_gets_date_shift(self, storm_then_forecast):
        _, report = storm_then_forecast
        top = next(r for r in report.disguise_recommendations if r.disguise_id == "hipaa")
        dob_mask = next(m for m in top.apply_payload["field_masks"] if m["column"] == "dob")
        assert dob_mask["type"] == "date_shift"


class TestRiskFlags:
    def test_dob_sentinels_surface_as_risk_flags(self, storm_then_forecast):
        _, report = storm_then_forecast
        dob_flags = [rf for rf in report.risk_flags if rf.field_name == "dob"]
        assert dob_flags, "expected risk flags on dob (0001-01-01 + 9999-12-31)"
        values = {rf.value for rf in dob_flags}
        assert "0001-01-01" in values and "9999-12-31" in values

    def test_string_sentinels_surface(self, storm_then_forecast):
        _, report = storm_then_forecast
        notes_flags = [rf for rf in report.risk_flags if rf.field_name == "notes"]
        assert notes_flags  # N/A and TBD should be flagged

    def test_every_risk_flag_has_fix_options(self, storm_then_forecast):
        _, report = storm_then_forecast
        for rf in report.risk_flags:
            assert rf.fix_options, f"risk flag for {rf.field_name}/{rf.kind} has no fix_options"


class TestProposedPipelineYAML:
    def test_yaml_parses_to_a_runnable_pipeline_shape(self, storm_then_forecast):
        _, report = storm_then_forecast
        cfg = yaml.safe_load(report.proposed_pipeline_yaml)
        assert {"version", "global_settings", "input", "output", "masking_rules"} <= cfg.keys()

    def test_yaml_masking_rules_match_apply_payload(self, storm_then_forecast):
        _, report = storm_then_forecast
        cfg = yaml.safe_load(report.proposed_pipeline_yaml)
        top = report.disguise_recommendations[0]

        # Same column set in both.
        cols_yaml = {r["column"] for r in cfg["masking_rules"]}
        cols_payload = {m["column"] for m in top.apply_payload["field_masks"]}
        assert cols_yaml == cols_payload
