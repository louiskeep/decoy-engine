"""Sentinel detection unit tests."""

import pandas as pd

from decoy_engine.storm.sentinels import detect_sentinels


class TestDateSentinels:
    def test_year_0001_sentinel_fires(self):
        # Strings, with the column name hinting "date" — exercises the
        # stdlib date-coerce path that handles values pandas can't.
        s = pd.Series(["1985-03-15", "0001-01-01", "1990-07-22"])
        flags = detect_sentinels(s, "start_date")
        kinds = {(f.kind, f.value) for f in flags}
        assert ("date_sentinel", "0001-01-01") in kinds

    def test_year_9999_sentinel_fires(self):
        s = pd.Series(["1985-03-15", "9999-12-31"])
        flags = detect_sentinels(s, "end_date")
        kinds = {(f.kind, f.value) for f in flags}
        assert ("date_sentinel", "9999-12-31") in kinds

    def test_excel_origin_date_fires(self):
        s = pd.Series(["1985-03-15", "1899-12-31"])
        flags = detect_sentinels(s, "created_date")
        assert any(f.value == "1899-12-31" for f in flags)

    def test_native_datetime_dtype_sentinels_fire(self):
        # Same logic via pd.to_datetime path — values inside pandas range.
        s = pd.to_datetime(pd.Series(["1985-03-15", "1900-01-01"]))
        flags = detect_sentinels(s, "dob")
        kinds = {f.kind for f in flags}
        assert "date_sentinel" in kinds

    def test_no_sentinel_for_clean_dates(self):
        s = pd.Series(["1985-03-15", "1990-07-22", "2001-11-08"])
        flags = detect_sentinels(s, "start_date")
        # No date_sentinel; out-of-range may still fire but it shouldn't here.
        assert not any(f.kind == "date_sentinel" for f in flags)

    def test_dob_in_future_flagged_as_out_of_range(self):
        # Birth date in the future is impossible.
        s = pd.Series(["1985-03-15", "2099-01-01"])
        flags = detect_sentinels(s, "dob")
        assert any(f.kind == "date_out_of_range" for f in flags)

    def test_date_hint_required_for_string_path(self):
        # Same values, but column name doesn't hint date → no date scan.
        s = pd.Series(["1985-03-15", "0001-01-01", "1990-07-22"])
        flags = detect_sentinels(s, "label")
        assert not any(f.kind == "date_sentinel" for f in flags)


class TestNumericSentinels:
    def test_negative_one_fires(self):
        s = pd.Series([10, 20, -1, 30, 40])
        flags = detect_sentinels(s, "parent_id")
        assert any(f.kind == "numeric_sentinel" and f.value == "-1" for f in flags)

    def test_999999999_fires(self):
        s = pd.Series([100, 200, 999999999])
        flags = detect_sentinels(s, "fake_ssn")
        assert any(f.value == "999999999" for f in flags)

    def test_clean_numeric_no_flags(self):
        s = pd.Series([10, 20, 30, 40, 50])
        flags = detect_sentinels(s, "amount")
        assert flags == []


class TestStringSentinels:
    def test_n_a_fires(self):
        s = pd.Series(["hello", "N/A", "world"])
        flags = detect_sentinels(s, "notes")
        assert any(f.value == "n/a" for f in flags)

    def test_tbd_fires(self):
        s = pd.Series(["a", "TBD", "b"])
        flags = detect_sentinels(s, "label")
        assert any(f.value == "tbd" for f in flags)

    def test_case_insensitive(self):
        s = pd.Series(["null", "NULL", "Null"])
        flags = detect_sentinels(s, "x")
        # All three normalize to "null"; one flag with count=3.
        null_flags = [f for f in flags if f.value == "null"]
        assert len(null_flags) == 1 and null_flags[0].count == 3

    def test_clean_strings_no_flags(self):
        s = pd.Series(["alpha", "beta", "gamma"])
        flags = detect_sentinels(s, "x")
        assert flags == []


class TestEmptyAndAllNull:
    def test_all_null_returns_empty(self):
        s = pd.Series([None, None, None], dtype="object")
        assert detect_sentinels(s, "anything") == []

    def test_empty_series_returns_empty(self):
        s = pd.Series([], dtype="object")
        assert detect_sentinels(s, "anything") == []
