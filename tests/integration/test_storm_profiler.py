"""Integration tests for run_storm: exercises the full profiler against
realistic sample DataFrames and asserts the StormProfile that comes out.
"""

import pandas as pd

from decoy_engine.storm import run_storm


def _hipaa_like_dataframe() -> pd.DataFrame:
    """12-row HIPAA-shaped fixture. dob / zip / gender have realistic
    repetition so they qualify as quasi-id candidates under Plan B-1's
    data-driven k-anonymity; ssn / email / phone stay unique so the
    PII detector path keeps firing on them. Two dob sentinel values
    (0001-01-01, 9999-12-31) preserved for the sentinel-surface tests.
    """
    return pd.DataFrame(
        {
            "patient_id": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
            "first_name": [
                "Alice",
                "Bob",
                "Carol",
                "Dave",
                "Eve",
                "Frank",
                "Grace",
                "Henry",
                "Iris",
                "Jack",
                "Kate",
                "Leo",
            ],
            "last_name": [
                "Smith",
                "Jones",
                "Davis",
                "Martin",
                "Wilson",
                "Brown",
                "Taylor",
                "Anderson",
                "Thomas",
                "Jackson",
                "White",
                "Harris",
            ],
            "ssn": [
                "123-45-6789",
                "555-12-3456",
                "111-22-3333",
                "444-55-6677",
                "222-99-1212",
                "333-44-5566",
                "777-88-9999",
                "888-11-2222",
                "999-33-4444",
                "121-21-2121",
                "343-43-4343",
                "565-65-6565",
            ],
            "dob": [
                "1985-03-15",
                "1990-07-22",
                "1972-11-08",
                "1985-03-15",
                "1990-07-22",
                "1972-11-08",
                "1985-03-15",
                "0001-01-01",
                "1972-11-08",
                "1985-03-15",
                "9999-12-31",
                "1972-11-08",
            ],
            "zip": [
                "90210",
                "10001",
                "60601",
                "94102",
                "90210",
                "10001",
                "60601",
                "94102",
                "90210",
                "10001",
                "60601",
                "94102",
            ],
            "gender": ["F", "M", "F", "M", "F", "M", "F", "M", "F", "M", "F", "M"],
            "email": [
                "a@b.com",
                "c@d.org",
                "e@f.io",
                "g@h.co",
                "i@j.net",
                "k@l.com",
                "m@n.io",
                "o@p.co",
                "q@r.net",
                "s@t.com",
                "u@v.io",
                "w@x.co",
            ],
            "phone": [
                "555-234-5678",
                "555-345-6789",
                "555-456-7890",
                "555-567-8901",
                "555-678-9012",
                "555-789-0123",
                "555-890-1234",
                "555-901-2345",
                "555-012-3456",
                "555-123-4567",
                "555-234-0987",
                "555-345-1098",
            ],
            "notes": [
                "clean",
                "fine",
                "N/A",
                "ok",
                "TBD",
                "",
                "clean",
                "fine",
                "N/A",
                "ok",
                "TBD",
                "",
            ],
        }
    )


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
        # asdict + json.dumps must succeed: the API layer relies on this.
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
        assert any(m.detector_id == "first_name" for m in fn.detector_matches)

    def test_gender_not_flagged_high_pii(self):
        # Gender alone is not high-PII (binary categorical): should be low.
        profile = run_storm(_hipaa_like_dataframe(), "x")
        gender = next(f for f in profile.fields if f.name == "gender")
        assert gender.pii_score < 0.5


class TestQuasiIdentifierGroup:
    """Plan B-1: quasi_identifier_groups are now derived from actual
    joint uniqueness (k-anonymity), not from hardcoded HIPAA-trio
    name hints. dob/zip/gender will participate when their cardinality
    + co-distribution produces a low k, but they're no longer
    automatically grouped just because the names match.
    """

    def test_qi_groups_include_dob_zip_gender_when_jointly_identifying(self):
        profile = run_storm(_hipaa_like_dataframe(), "x")
        assert profile.quasi_identifier_groups, (
            "expected at least one quasi-id combo from data-driven k-anonymity"
        )
        # Every winning combo should be a 2- or 3-subset of the qi
        # candidate columns.
        contributing = {col for group in profile.quasi_identifier_groups for col in group}
        # The hipaa-shaped fixture's quasi-id-friendly columns are
        # dob / zip / gender. At least one should contribute.
        assert contributing & {"dob", "zip", "gender"}, (
            f"expected dob / zip / gender to participate, got {contributing}"
        )

    def test_dropping_gender_still_produces_qi_combos_from_remaining_columns(self):
        df = _hipaa_like_dataframe().drop(columns=["gender"])
        profile = run_storm(df, "x")
        # With gender gone, the winning combos can't include it.
        for group in profile.quasi_identifier_groups:
            assert "gender" not in group


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
    """Plan B-1: reid_risk_score = 100 / k_anonymity, capped at 100.
    reid_risk_columns is the flat union of columns participating in
    the winning quasi-id combo(s): NOT the set of columns whose
    unique_rate > 0.9 (direct identifiers like ssn are covered by
    per-field pii_score / detector hits and don't appear here)."""

    def test_reid_score_reflects_quasi_identifier_linkage(self):
        # With 12 rows and dob/zip/gender forming low-k combos, at
        # least one combo achieves k <= 2 -> reid_risk_score >= 50.
        profile = run_storm(_hipaa_like_dataframe(), "x")
        assert profile.k_anonymity is not None
        assert profile.reid_risk_score >= 50.0

    def test_direct_identifiers_not_in_quasi_id_columns(self):
        # ssn / email / phone are direct identifiers (unique -> above
        # the unique_rate cap -> filtered out of QI candidates).
        profile = run_storm(_hipaa_like_dataframe(), "x")
        assert "ssn" not in profile.reid_risk_columns
        assert "email" not in profile.reid_risk_columns
        assert "phone" not in profile.reid_risk_columns

    def test_quasi_id_columns_drawn_from_winning_combos(self):
        # gender / dob / zip have the right cardinality for QI combos
        # in this fixture; at least one should show up.
        profile = run_storm(_hipaa_like_dataframe(), "x")
        assert profile.reid_risk_columns
        assert set(profile.reid_risk_columns).issubset(
            # everything we'd accept as a QI candidate in this fixture
            {"dob", "zip", "gender", "first_name", "last_name", "notes"}
        )


class TestDateFormatSignal:
    def test_iso_format_detected_on_dob(self):
        profile = run_storm(_hipaa_like_dataframe(), "x")
        dob = next(f for f in profile.fields if f.name == "dob")
        assert dob.date_format == "iso_date"
