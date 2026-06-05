"""PR6: custom-detector specs running alongside the built-ins.

Custom detectors let the platform register organization-specific PII
patterns (UK NHS numbers, ABA routing numbers, etc.) at scan time
without engine changes. The spec list is passed to run_storm via the
`custom_detectors` kwarg; each spec runs against every column.
"""

import pandas as pd

from decoy_engine.storm import CustomDetectorSpec, run_storm


def _profile(field_name: str, df: pd.DataFrame, custom=None):
    profile = run_storm(df, "test.csv", custom_detectors=custom)
    return next(f for f in profile.fields if f.name == field_name)


# ── basic firing ─────────────────────────────────────────────────────────────


class TestCustomDetectorFiring:
    def test_no_custom_specs_works_as_before(self):
        df = pd.DataFrame({"ssn": ["123-45-6789"] * 10})
        f = _profile("ssn", df, custom=None)
        # Built-in SSN detector still fires.
        assert any(m.detector_id == "ssn" for m in f.detector_matches)

    def test_custom_pattern_matches_high_rate_no_name_hint(self):
        # UK NHS-style numbers - built-in doesn't recognize them.
        spec = CustomDetectorSpec(
            id="custom__uk_nhs",
            pattern=r"\d{3} \d{3} \d{4}",
            name_hints=[],
            threshold=0.7,
        )
        df = pd.DataFrame(
            {
                "national_id": ["401 023 2137", "612 555 8888", "001 222 3333"] * 10,
            }
        )
        f = _profile("national_id", df, custom=[spec])
        ids = {m.detector_id for m in f.detector_matches}
        assert "custom__uk_nhs" in ids

    def test_name_hint_lowers_threshold(self):
        # Pattern matches only 50% of values - below the default 0.7 threshold.
        # Without a name hint it shouldn't fire.
        spec_no_hint = CustomDetectorSpec(
            id="custom__weak",
            pattern=r"^A\d+$",
            name_hints=[],
            threshold=0.7,
        )
        df = pd.DataFrame(
            {
                "asset_code": ["A101", "A202", "B999", "C111"] * 5,
            }
        )
        f = _profile("asset_code", df, custom=[spec_no_hint])
        assert all(m.detector_id != "custom__weak" for m in f.detector_matches)

        # Same data, same pattern, but with a name hint that matches the column.
        spec_with_hint = CustomDetectorSpec(
            id="custom__weak",
            pattern=r"^A\d+$",
            name_hints=["asset", "code"],
            threshold=0.7,
        )
        f2 = _profile("asset_code", df, custom=[spec_with_hint])
        # Name-hint floor is 0.4; 50% > 0.4, so it should fire now.
        assert any(m.detector_id == "custom__weak" for m in f2.detector_matches)

    def test_custom_fires_alongside_built_ins(self):
        # SSN-pattern values in a column called ssn - built-in fires;
        # custom detector also fires on a different field.
        spec = CustomDetectorSpec(
            id="custom__zip5",
            pattern=r"\d{5}",
            name_hints=["zip"],
            threshold=0.7,
        )
        df = pd.DataFrame(
            {
                "ssn": ["123-45-6789"] * 12,
                "zip_code": ["90210", "10001", "60601"] * 4,
            }
        )
        ssn_field = _profile("ssn", df, custom=[spec])
        zip_field = _profile("zip_code", df, custom=[spec])
        assert any(m.detector_id == "ssn" for m in ssn_field.detector_matches)
        # Both built-in us_zip and the custom one fire on zip_code.
        zip_ids = {m.detector_id for m in zip_field.detector_matches}
        assert "us_zip" in zip_ids
        assert "custom__zip5" in zip_ids


# ── detection trail ──────────────────────────────────────────────────────────


class TestCustomDetectionTrail:
    def test_winner_regex_signal_uses_custom_id(self):
        spec = CustomDetectorSpec(
            id="custom__uk_nhs",
            pattern=r"\d{3} \d{3} \d{4}",
            name_hints=["nhs"],
            threshold=0.7,
        )
        df = pd.DataFrame(
            {
                "nhs_id": ["401 023 2137", "612 555 8888", "001 222 3333"] * 10,
            }
        )
        f = _profile("nhs_id", df, custom=[spec])
        # Custom detector wins (100% match rate) -> trail's regex row uses
        # the custom id.
        assert f.detection_trail
        assert f.detection_trail[0].signal == "regex · custom__uk_nhs_pattern"
        assert f.detection_trail[0].winner is True

    def test_name_hint_row_emitted_for_custom_winner(self):
        spec = CustomDetectorSpec(
            id="custom__uk_nhs",
            pattern=r"\d{3} \d{3} \d{4}",
            name_hints=["nhs"],
            threshold=0.7,
        )
        df = pd.DataFrame(
            {
                "nhs_id": ["401 023 2137", "612 555 8888", "001 222 3333"] * 10,
            }
        )
        f = _profile("nhs_id", df, custom=[spec])
        signals = [s.signal for s in f.detection_trail]
        assert any('name-hint · col="nhs_id"' in s for s in signals)


# ── safety ───────────────────────────────────────────────────────────────────


class TestCustomDetectorSafety:
    def test_bad_regex_does_not_crash_scan(self):
        # An invalid regex ([ unclosed) should be skipped, not raise.
        spec = CustomDetectorSpec(
            id="custom__bad",
            pattern=r"[unclosed",
            name_hints=[],
            threshold=0.7,
        )
        df = pd.DataFrame({"col": ["x", "y", "z"] * 10})
        # Profile should still be produced; bad detector silently dropped.
        profile = run_storm(df, "t.csv", custom_detectors=[spec])
        assert len(profile.fields) == 1
        f = profile.fields[0]
        assert all(m.detector_id != "custom__bad" for m in f.detector_matches)

    def test_empty_name_hint_list_does_not_match_anything(self):
        # No name hints -> name-hint floor never kicks in.
        spec = CustomDetectorSpec(
            id="custom__strict",
            pattern=r"^X\d+$",
            name_hints=[],
            threshold=0.7,
        )
        df = pd.DataFrame(
            {
                "ref": ["X1", "X2", "Y3", "Z4", "W5"] * 5,  # 40% match
            }
        )
        f = _profile("ref", df, custom=[spec])
        assert all(m.detector_id != "custom__strict" for m in f.detector_matches)
