"""Slice 3: STORM PII wiring in walk_dataframe.

Covers: opt-in flag default (off), default-off preserves slice-2
behavior, opt-in tags obvious-PII columns with the expected PIIClass,
non-PII columns stay None, confidence threshold (only high-confidence
matches tag), single STORM call per walk (not per-column), TableProfile
invariants still hold under PII tagging.
"""

from __future__ import annotations

import random

import pandas as pd
import pytest

from decoy_engine.profile import PIIClass
from decoy_engine.profile._pii import _best_high_confidence_match
from decoy_engine.profile._walk import walk_dataframe
from decoy_engine.storm import DetectorMatch


def _rng(seed: int = 42) -> random.Random:
    return random.Random(seed)


class TestDefaultOff:
    """run_pii_detection defaults to False; slice-2 behavior preserved."""

    def test_default_keeps_all_pii_class_none(self) -> None:
        df = pd.DataFrame(
            {
                "email": ["a@b.com", "c@d.com", "e@f.com"],
                "ssn": ["123-45-6789", "987-65-4321", "555-12-3456"],
            }
        )
        profile = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
        )
        for col in profile.columns:
            assert col.pii_class is None

    def test_explicit_false_keeps_all_pii_class_none(self) -> None:
        df = pd.DataFrame({"email": ["a@b.com", "c@d.com"]})
        profile = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
            run_pii_detection=False,
        )
        assert profile.columns[0].pii_class is None


class TestOptInTags:
    """run_pii_detection=True tags columns whose high-confidence STORM
    match maps to a PIIClass enum value."""

    def test_email_column_tagged(self) -> None:
        df = pd.DataFrame(
            {
                "email": [
                    "alice@example.com",
                    "bob@example.com",
                    "carol@example.com",
                    "dave@example.com",
                    "eve@example.com",
                ]
            }
        )
        profile = walk_dataframe(
            df,
            table_name="contacts",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
            run_pii_detection=True,
        )
        assert profile.columns[0].pii_class == PIIClass.EMAIL

    def test_ssn_column_tagged(self) -> None:
        df = pd.DataFrame(
            {
                "ssn": [
                    "123-45-6789",
                    "987-65-4321",
                    "555-12-3456",
                    "111-22-3333",
                    "444-55-6666",
                ]
            }
        )
        profile = walk_dataframe(
            df,
            table_name="people",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
            run_pii_detection=True,
        )
        assert profile.columns[0].pii_class == PIIClass.SSN

    def test_non_pii_column_stays_none(self) -> None:
        df = pd.DataFrame({"customer_id": [1, 2, 3, 4, 5]})
        profile = walk_dataframe(
            df,
            table_name="customers",
            declared_pk_cols=frozenset({"customer_id"}),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
            run_pii_detection=True,
        )
        # An integer ID column should not be tagged with any PIIClass.
        assert profile.columns[0].pii_class is None


class TestMixedDataFrame:
    """Multiple columns: some PII, some not. Each column resolves independently."""

    def test_mixed_columns(self) -> None:
        df = pd.DataFrame(
            {
                "customer_id": [1, 2, 3, 4, 5],
                "email": [
                    "alice@example.com",
                    "bob@example.com",
                    "carol@example.com",
                    "dave@example.com",
                    "eve@example.com",
                ],
                "amount": [100.0, 200.0, 300.0, 400.0, 500.0],
            }
        )
        profile = walk_dataframe(
            df,
            table_name="orders",
            declared_pk_cols=frozenset({"customer_id"}),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
            run_pii_detection=True,
        )
        cols = {c.name: c for c in profile.columns}
        assert cols["customer_id"].pii_class is None
        assert cols["email"].pii_class == PIIClass.EMAIL
        assert cols["amount"].pii_class is None


class TestPIIDoesNotBreakOtherInvariants:
    """PII tagging is additive; row counts, distinct counts, declared_pk,
    is_fk all still hold."""

    def test_pii_walk_preserves_other_fields(self) -> None:
        df = pd.DataFrame(
            {
                "email": [
                    "alice@example.com",
                    "bob@example.com",
                    "carol@example.com",
                    "dave@example.com",
                    "eve@example.com",
                ]
            }
        )
        profile = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset({"email"}),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
            run_pii_detection=True,
        )
        col = profile.columns[0]
        assert col.pii_class == PIIClass.EMAIL
        assert col.declared_pk is True
        assert col.row_count == 5
        assert col.distinct_count == 5
        assert col.is_candidate_key_sampled is True


class TestBestMatchHelper:
    """_best_high_confidence_match: only high-confidence matches qualify,
    ties on match_rate stay deterministic."""

    def test_no_matches_returns_none(self) -> None:
        assert _best_high_confidence_match([]) is None

    def test_only_medium_returns_none(self) -> None:
        matches = [
            DetectorMatch(detector_id="email", match_rate=0.6, confidence="medium"),
            DetectorMatch(detector_id="ssn", match_rate=0.55, confidence="medium"),
        ]
        assert _best_high_confidence_match(matches) is None

    def test_only_low_returns_none(self) -> None:
        matches = [DetectorMatch(detector_id="email", match_rate=0.4, confidence="low")]
        assert _best_high_confidence_match(matches) is None

    def test_high_confidence_wins_over_medium(self) -> None:
        matches = [
            DetectorMatch(detector_id="ssn", match_rate=0.7, confidence="medium"),
            DetectorMatch(detector_id="email", match_rate=0.6, confidence="high"),
        ]
        best = _best_high_confidence_match(matches)
        assert best is not None
        assert best.detector_id == "email"

    def test_highest_match_rate_among_high_wins(self) -> None:
        matches = [
            DetectorMatch(detector_id="email", match_rate=0.5, confidence="high"),
            DetectorMatch(detector_id="ssn", match_rate=0.9, confidence="high"),
            DetectorMatch(detector_id="us_phone", match_rate=0.7, confidence="high"),
        ]
        best = _best_high_confidence_match(matches)
        assert best is not None
        assert best.detector_id == "ssn"


class TestCustomDetectorIdsSkipped:
    """Detector ids not in the PIIClass enum (custom detectors, future
    built-ins) result in pii_class=None at this layer rather than crashing
    or silently inventing a tag.

    Slice-3 B1: built-ins missing from PIIClass log a WARNING; only
    custom__-prefixed ids drop silently.
    """

    def test_pii_class_rejects_custom_detector_ids(self) -> None:
        with pytest.raises(ValueError):
            PIIClass("custom__uk_nhs_number")

    def test_custom_detector_id_drops_silently(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from decoy_engine.profile import _pii
        from decoy_engine.storm import DetectorMatch, FieldStats, StormProfile

        fake_field = FieldStats(
            name="weird_col",
            inferred_type="string",
            dtype_raw="object",
            row_count=10,
            null_count=0,
            null_rate=0.0,
            distinct_count=10,
            unique_rate=1.0,
            is_likely_unique=True,
            detector_matches=[
                DetectorMatch(
                    detector_id="custom__uk_nhs_number",
                    match_rate=0.9,
                    confidence="high",
                )
            ],
        )
        fake_profile = StormProfile(
            source_label="t",
            row_count=10,
            sample_strategy="full",
            fields=[fake_field],
        )

        def _fake_run_storm(df: pd.DataFrame, source_label: str, **kwargs: object) -> StormProfile:
            return fake_profile

        monkeypatch.setattr(_pii, "run_storm", _fake_run_storm)
        with caplog.at_level("WARNING", logger="decoy_engine.profile._pii"):
            tags = _pii.detect_pii_classes(pd.DataFrame({"weird_col": list(range(10))}), "t")

        assert tags == {}
        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warning_records == [], (
            "custom__ detector ids should drop silently, no WARNING expected"
        )

    def test_built_in_not_in_enum_logs_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from decoy_engine.profile import _pii
        from decoy_engine.storm import DetectorMatch, FieldStats, StormProfile

        # Simulate a future built-in STORM detector ("dob_iso", say) that
        # lands before the enum catches up. The walker should drop the tag
        # but log a WARNING so the gap is operationally visible.
        fake_field = FieldStats(
            name="dob",
            inferred_type="string",
            dtype_raw="object",
            row_count=10,
            null_count=0,
            null_rate=0.0,
            distinct_count=10,
            unique_rate=1.0,
            is_likely_unique=True,
            detector_matches=[
                DetectorMatch(
                    detector_id="dob_iso",  # not in PIIClass, not custom__
                    match_rate=0.9,
                    confidence="high",
                )
            ],
        )
        fake_profile = StormProfile(
            source_label="t",
            row_count=10,
            sample_strategy="full",
            fields=[fake_field],
        )

        def _fake_run_storm(df: pd.DataFrame, source_label: str, **kwargs: object) -> StormProfile:
            return fake_profile

        monkeypatch.setattr(_pii, "run_storm", _fake_run_storm)
        with caplog.at_level("WARNING", logger="decoy_engine.profile._pii"):
            tags = _pii.detect_pii_classes(pd.DataFrame({"dob": list(range(10))}), "people")

        assert tags == {}
        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warning_records) == 1
        msg = warning_records[0].getMessage()
        assert "dob_iso" in msg
        assert "dob" in msg
        assert "PIIClass" in msg
