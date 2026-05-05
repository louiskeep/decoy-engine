"""Integration tests for run_storm — exercises the full profiler against
realistic sample DataFrames and asserts the StormProfile that comes out.
"""

import pandas as pd

from decoy_engine.storm import run_storm


def _hipaa_like_dataframe() -> pd.DataFrame:
    return pd.DataFrame({
        "patient_id":  [1, 2, 3, 4, 5],
        "first_name":  ["Alice", "Bob", "Carol", "Dave", "Eve"],
        "last_name":   ["Smith", "Jones", "Davis", "Martin", "Wilson"],
        "ssn":         ["123-45-6789", "555-12-3456", "111-22-3333", "444-55-6677", "222-99-1212"],
        "dob":         ["1985-03-15", "1990-07-22", "0001-01-01", "1972-11-08", "9999-12-31"],
        "zip":         ["90210", "10001", "60601", "94102", "02134"],
        "gender":      ["F", "M", "F", "M", "F"],
        "email":       ["a@b.com", "c@d.org", "e@f.io", "g@h.co", "i@j.net"],
        "phone":       ["555-234-5678", "555-345-6789", "555-456-7890", "555-567-8901", "555-678-9012"],
        "notes":       ["clean", "fine", "N/A", "ok", "TBD"],
    })


class TestProfileShape:
    def test_returns_one_field_per_column(self):
        df = _hipaa_like_dataframe()
        profile = run_storm(df, "test.csv")
        assert len(profile.fields) == len(df.columns)
        assert {f.name for f in profile.fields} == set(df.columns)

    def test_source_label_preserved(self):
        df = _hipaa_like_dataframe()
        profile = run_storm(df, "patients_2026Q1.csv")
        assert profile.source_label == "patients_2026Q1.csv"

    def test_row_count_matches(self):
        df = _hipaa_like_dataframe()
        profile = run_storm(df, "x")
        assert profile.row_count == len(df)

    def test_json_serializable(self):
        # asdict + json.dumps must succeed — the API layer relies on this.
        import json
        df = _hipaa_like_dataframe()
        profile = run_storm(df, "x")
        json.dumps(profile.to_dict())


class TestPIIDetection:
    def test_email_column_flagged_pii(self):
        profile = run_storm(_hipaa_like_dataframe(), "x")
        email = next(f for f in profile.fields if f.name == "email")
        assert email.pii_score >= 0.9
        assert any(m.detector_id == "email" for m in email.detector_matches)

    def test_ssn_column_flagged_pii(self):
        profile = run_storm(_hipaa_like_dataframe(), "x")
        ssn = next(f for f in profile.fields if f.name == "ssn")
        assert ssn.pii_score >= 0.9
        assert any(m.detector_id == "ssn" for m in ssn.detector_matches)

    def test_first_name_flagged_pii(self):
        profile = run_storm(_hipaa_like_dataframe(), "x")
        fn = next(f for f in profile.fields if f.name == "first_name")
        assert fn.pii_score >= 0.9
        assert any(m.detector_id == "person_name" for m in fn.detector_matches)

    def test_gender_not_flagged_high_pii(self):
        # Gender alone is not high-PII (binary categorical) — should be low.
        profile = run_storm(_hipaa_like_dataframe(), "x")
        gender = next(f for f in profile.fields if f.name == "gender")
        assert gender.pii_score < 0.5


class TestQuasiIdentifierGroup:
    def test_dob_zip_gender_co_occurrence_flagged(self):
        profile = run_storm(_hipaa_like_dataframe(), "x")
        assert profile.quasi_identifier_groups
        group = profile.quasi_identifier_groups[0]
        assert set(group) == {"dob", "zip", "gender"}

    def test_no_qi_group_when_one_member_missing(self):
        df = _hipaa_like_dataframe().drop(columns=["gender"])
        profile = run_storm(df, "x")
        assert profile.quasi_identifier_groups == []


class TestSentinelsSurface:
    def test_dob_year_sentinels_surface(self):
        profile = run_storm(_hipaa_like_dataframe(), "x")
        dob = next(f for f in profile.fields if f.name == "dob")
        kinds = {f.kind for f in dob.sentinels}
        assert "date_sentinel" in kinds
        # Both 0001-01-01 and 9999-12-31 should be flagged.
        values = {f.value for f in dob.sentinels}
        assert "0001-01-01" in values and "9999-12-31" in values

    def test_string_sentinels_surface_in_notes_column(self):
        profile = run_storm(_hipaa_like_dataframe(), "x")
        notes = next(f for f in profile.fields if f.name == "notes")
        kinds = {f.kind for f in notes.sentinels}
        assert "string_sentinel" in kinds


class TestReidRiskScore:
    def test_reid_score_reflects_unique_columns(self):
        # Every row unique on most columns → reid_risk_score is high.
        profile = run_storm(_hipaa_like_dataframe(), "x")
        assert profile.reid_risk_score > 50
        assert "ssn" in profile.reid_risk_columns

    def test_low_cardinality_columns_not_flagged(self):
        profile = run_storm(_hipaa_like_dataframe(), "x")
        assert "gender" not in profile.reid_risk_columns


class TestDateFormatSignal:
    def test_iso_format_detected_on_dob(self):
        profile = run_storm(_hipaa_like_dataframe(), "x")
        dob = next(f for f in profile.fields if f.name == "dob")
        assert dob.date_format == "iso_date"
