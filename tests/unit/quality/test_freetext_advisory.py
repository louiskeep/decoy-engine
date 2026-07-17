"""Tests for the HC-7 clinical free-text advisory warn-gate.

Mirrors `tests/unit/quality/test_retention_gate.py`'s structure: pure
scorer coverage, then a thin logger-only-wrapper test. The advisory's
non-negotiable framing (see the build guide) is warn-only: never raises,
never mutates the plan/config, never changes output bytes. Test class
names below map 1:1 onto the guide's lettered test list (a)-(f).
"""

from __future__ import annotations

import logging

import pytest

from decoy_engine.quality._freetext_advisory import (
    DEFAULT_FREETEXT_ADVISORY_MIN_AVG_LENGTH,
    DEFAULT_FREETEXT_ADVISORY_MIN_DISTINCTNESS,
    FreetextColumnView,
    freetext_advisory_min_avg_length,
    freetext_advisory_min_distinctness,
    is_string_dtype_label,
    score_unmasked_freetext,
    warn_on_unmasked_freetext,
)

MIN_AVG_LENGTH = DEFAULT_FREETEXT_ADVISORY_MIN_AVG_LENGTH
MIN_DISTINCTNESS = DEFAULT_FREETEXT_ADVISORY_MIN_DISTINCTNESS


def _view(
    name: str,
    *,
    strategy: str = "passthrough",
    dtype_known_non_string: bool = False,
    avg_length: float | None = None,
    distinct_count: int | None = None,
    non_null_count: int | None = None,
) -> FreetextColumnView:
    return FreetextColumnView(
        name=name,
        strategy=strategy,
        dtype_known_non_string=dtype_known_non_string,
        avg_length=avg_length,
        distinct_count=distinct_count,
        non_null_count=non_null_count,
    )


def _score(views: list[FreetextColumnView]) -> list[str]:
    return score_unmasked_freetext(
        views, min_avg_length=MIN_AVG_LENGTH, min_distinctness=MIN_DISTINCTNESS
    )


class TestNameHintWarns:
    """(a) a clinical_notes passthrough column WARNS."""

    def test_clinical_notes_passthrough_warns(self) -> None:
        warnings = _score([_view("clinical_notes")])
        assert len(warnings) == 1
        assert "clinical_notes" in warnings[0]
        assert "text_mask" in warnings[0]

    def test_name_hint_fires_without_any_profile_stats(self) -> None:
        # Degrade-to-name-hint-only contract: no avg_length/distinct_count
        # available (missing profile, or a profile predating this field).
        warnings = _score(
            [_view("clinical_notes", avg_length=None, distinct_count=None, non_null_count=None)]
        )
        assert len(warnings) == 1


class TestLengthDistinctnessWarns:
    """(b) a long-avg-length oddly-named passthrough column warns via the
    length+distinctness branch."""

    def test_oddly_named_long_column_warns(self) -> None:
        warnings = _score(
            [_view("field_7", avg_length=180.0, distinct_count=900, non_null_count=1000)]
        )
        assert len(warnings) == 1
        assert "field_7" in warnings[0]

    def test_length_alone_without_distinctness_does_not_warn(self) -> None:
        warnings = _score(
            [_view("field_7", avg_length=180.0, distinct_count=5, non_null_count=1000)]
        )
        assert warnings == []

    def test_distinctness_alone_without_length_does_not_warn(self) -> None:
        warnings = _score(
            [_view("field_7", avg_length=10.0, distinct_count=990, non_null_count=1000)]
        )
        assert warnings == []

    def test_missing_stats_never_fires_fallback_branch(self) -> None:
        warnings = _score(
            [_view("field_7", avg_length=None, distinct_count=None, non_null_count=None)]
        )
        assert warnings == []

    def test_zero_non_null_count_does_not_divide_by_zero(self) -> None:
        warnings = _score([_view("field_7", avg_length=180.0, distinct_count=0, non_null_count=0)])
        assert warnings == []


class TestIcd10RegressionGuard:
    """(c) an ICD-10-style column (short avg length, high distinct) does
    NOT warn -- the HC-5 high-cardinality-code protection this advisory
    must not undermine."""

    def test_high_cardinality_short_code_column_does_not_warn(self) -> None:
        # Mirrors a real HC-5 icd10 high_cardinality column: avg ~6 chars,
        # >90% distinct.
        warnings = _score(
            [_view("diagnosis_code", avg_length=6.2, distinct_count=950, non_null_count=1000)]
        )
        assert warnings == []

    def test_short_code_name_does_not_hit_the_name_branch_either(self) -> None:
        # "diagnosis_code" ends in "code", not a free-text token -- the
        # name-hint branch must not compensate for the length guard.
        assert (
            _score(
                [_view("diagnosis_code", avg_length=6.2, distinct_count=950, non_null_count=1000)]
            )
            == []
        )


class TestAlreadyMaskedNeverWarns:
    """(d) a clinical_notes column already routed to text_mask does NOT
    warn."""

    @pytest.mark.parametrize("strategy", ["text_mask", "text_redact", "redact", "hash", "faker"])
    def test_masked_clinical_notes_column_does_not_warn(self, strategy: str) -> None:
        warnings = _score([_view("clinical_notes", strategy=strategy)])
        assert warnings == []


class TestSentinelDisablesGate:
    """(e) sentinel threshold disables all warnings."""

    def test_zero_threshold_disables_name_hint_branch(self) -> None:
        warnings = score_unmasked_freetext(
            [_view("clinical_notes")], min_avg_length=0.0, min_distinctness=MIN_DISTINCTNESS
        )
        assert warnings == []

    def test_negative_threshold_disables_length_branch(self) -> None:
        warnings = score_unmasked_freetext(
            [_view("field_7", avg_length=500.0, distinct_count=999, non_null_count=1000)],
            min_avg_length=-1.0,
            min_distinctness=MIN_DISTINCTNESS,
        )
        assert warnings == []


class TestNonStringDtypeNeverWarns:
    """(f) non-string dtype never warns, even when the name would
    otherwise hit the name-hint branch."""

    def test_known_non_string_dtype_suppresses_name_hint(self) -> None:
        warnings = _score([_view("clinical_notes", dtype_known_non_string=True)])
        assert warnings == []

    def test_known_non_string_dtype_suppresses_length_branch(self) -> None:
        warnings = _score(
            [
                _view(
                    "field_7",
                    dtype_known_non_string=True,
                    avg_length=500.0,
                    distinct_count=999,
                    non_null_count=1000,
                )
            ]
        )
        assert warnings == []


class TestThresholdReaders:
    def test_min_avg_length_default(self) -> None:
        assert freetext_advisory_min_avg_length({}) == DEFAULT_FREETEXT_ADVISORY_MIN_AVG_LENGTH

    def test_min_avg_length_reads_configured_value(self) -> None:
        config = {"global_settings": {"freetext_advisory_min_avg_length": 75.0}}
        assert freetext_advisory_min_avg_length(config) == 75.0

    def test_min_distinctness_default(self) -> None:
        assert freetext_advisory_min_distinctness({}) == DEFAULT_FREETEXT_ADVISORY_MIN_DISTINCTNESS

    def test_min_distinctness_reads_configured_value(self) -> None:
        config = {"global_settings": {"freetext_advisory_min_distinctness": 0.9}}
        assert freetext_advisory_min_distinctness(config) == 0.9


class TestIsStringDtypeLabel:
    @pytest.mark.parametrize("label", ["object", "string", "string[pyarrow]"])
    def test_string_labels(self, label: str) -> None:
        assert is_string_dtype_label(label) is True

    @pytest.mark.parametrize("label", ["int64", "float64", "bool", "datetime64[ns]", "category"])
    def test_non_string_labels(self, label: str) -> None:
        assert is_string_dtype_label(label) is False


class TestWarnWrapper:
    """Gate wrapper: logs only, never raises, never mutates its input."""

    def test_logs_one_warning_per_match(self, caplog: pytest.LogCaptureFixture) -> None:
        views = [_view("clinical_notes"), _view("customer_id", avg_length=8.0)]
        with caplog.at_level(logging.WARNING, logger="decoy_engine.quality._freetext_advisory"):
            warn_on_unmasked_freetext(
                views, min_avg_length=MIN_AVG_LENGTH, min_distinctness=MIN_DISTINCTNESS
            )
        assert len(caplog.records) == 1
        assert "clinical_notes" in caplog.records[0].message

    def test_never_raises_on_pathological_input(self) -> None:
        views = [
            _view("clinical_notes", avg_length=float("nan"), distinct_count=1, non_null_count=1)
        ]
        warn_on_unmasked_freetext(
            views, min_avg_length=MIN_AVG_LENGTH, min_distinctness=MIN_DISTINCTNESS
        )  # no raise

    def test_input_views_unchanged_after_scoring(self) -> None:
        # FreetextColumnView is a frozen dataclass, so "mutation" would
        # raise at the language level -- this test documents that identity
        # contract explicitly rather than relying on frozen-ness alone.
        view = _view("clinical_notes", avg_length=200.0, distinct_count=900, non_null_count=1000)
        before = FreetextColumnView(**vars(view))
        score_unmasked_freetext(
            [view], min_avg_length=MIN_AVG_LENGTH, min_distinctness=MIN_DISTINCTNESS
        )
        assert view == before
