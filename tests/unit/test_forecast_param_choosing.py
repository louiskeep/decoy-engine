"""Plan B-2 tests for the FORECAST per-detector choosers.

The choosers live in ``decoy_engine/forecast/transform_metadata.py``.
Each detector_id maps to a function that reads a FieldStats and returns
``(mask, params, why)``. These tests assert that the *same* detector_id
produces *different* params when the FieldStats inputs change — that's
the whole point of the B-2 rewrite.

We build FieldStats directly so we can isolate chooser behavior from
profiler computation. The end-to-end path (profiler → recommender → UI
shape) is covered by the integration tests.
"""

from __future__ import annotations

from decoy_engine.forecast.transform_metadata import best_transform_for
from decoy_engine.storm.types import DetectorMatch, FieldStats


def _f(
    name: str,
    *,
    detector: str,
    unique_rate: float = 0.5,
    is_likely_unique: bool = False,
    value_set_size_class: str | None = None,
    alphabet: str | None = None,
    max_length: int | None = None,
    format_pattern: str | None = None,
    date_min: str | None = None,
    date_max: str | None = None,
) -> FieldStats:
    return FieldStats(
        name=name,
        inferred_type="string",
        dtype_raw="object",
        row_count=100,
        null_count=0,
        null_rate=0.0,
        distinct_count=50,
        unique_rate=unique_rate,
        is_likely_unique=is_likely_unique,
        value_set_size_class=value_set_size_class,
        alphabet=alphabet,
        max_length=max_length,
        format_pattern=format_pattern,
        date_min=date_min,
        date_max=date_max,
        detector_matches=[DetectorMatch(detector_id=detector, match_rate=1.0)],
    )


# ── ssn: identifier-shaped → hash; derived → faker ────────────────────

class TestSsnChooser:
    def test_unique_ssn_hashes(self):
        f = _f("ssn", detector="ssn", unique_rate=1.0, is_likely_unique=True,
               alphabet="digits", max_length=9, value_set_size_class="unique")
        mask, params, _ = best_transform_for("ssn", f)
        assert mask == "hash"
        # B-2: truncate sized to the column's max_length when alphabet
        # is digits, so masked output keeps the same visual length.
        assert params["truncate"] == 9
        assert params["algorithm"] == "sha256"

    def test_low_card_ssn_fakes(self):
        # Same detector_id, but a low-card SSN column (e.g. a small
        # demographic dimension table with repeated values) → faker.
        f = _f("ssn", detector="ssn", unique_rate=0.05, is_likely_unique=False,
               value_set_size_class="low")
        mask, params, _ = best_transform_for("ssn", f)
        assert mask == "faker"
        assert params == {"faker_type": "ssn"}


# ── email: same branching ─────────────────────────────────────────────

class TestEmailChooser:
    def test_unique_email_hashes(self):
        f = _f("email", detector="email", unique_rate=1.0, is_likely_unique=True,
               alphabet="mixed", value_set_size_class="unique")
        mask, _params, _ = best_transform_for("email", f)
        assert mask == "hash"

    def test_low_card_email_fakes(self):
        f = _f("email_template", detector="email", unique_rate=0.05,
               is_likely_unique=False, value_set_size_class="low")
        mask, params, _ = best_transform_for("email", f)
        assert mask == "faker"
        assert params == {"faker_type": "email"}


# ── phone: locale from format_pattern ─────────────────────────────────

class TestPhoneChooser:
    def test_us_format_phone_gets_en_us_locale(self):
        f = _f("phone", detector="us_phone",
               format_pattern=r"\(\d{3}\)\s\d{3}-\d{4}")
        mask, params, _ = best_transform_for("us_phone", f)
        assert mask == "faker"
        assert params["faker_type"] == "phone_number"
        assert params["locale"] == "en_US"

    def test_neutral_format_phone_no_locale(self):
        f = _f("phone", detector="us_phone", format_pattern=r"\d{10}")
        mask, params, _ = best_transform_for("us_phone", f)
        assert mask == "faker"
        assert "locale" not in params

    def test_no_field_no_locale(self):
        # best_transform_for tolerates missing FieldStats (UI preview path).
        mask, params, _ = best_transform_for("us_phone", None)
        assert mask == "faker"
        assert "locale" not in params


# ── date: jitter scales to the date span ──────────────────────────────

class TestDateChooser:
    def test_wide_span_uses_default_30_day_jitter(self):
        # 5-year span → wider than 365 days → fall back to default 30d.
        f = _f("event_date", detector="iso_date",
               date_min="2020-01-01", date_max="2025-01-01")
        mask, params, _ = best_transform_for("iso_date", f)
        assert mask == "date_shift"
        assert params["jitter_days"] == 30

    def test_tight_span_scales_jitter_down(self):
        # 90-day span → ±22d (span // 4 with a 7d floor).
        f = _f("enrollment_date", detector="iso_date",
               date_min="2025-01-01", date_max="2025-04-01")
        _mask, params, _ = best_transform_for("iso_date", f)
        assert params["jitter_days"] == 22

    def test_no_date_range_falls_back_to_default(self):
        f = _f("event_date", detector="iso_date")
        _mask, params, _ = best_transform_for("iso_date", f)
        assert params["jitter_days"] == 30


# ── unknown detector returns None ─────────────────────────────────────

def test_unknown_detector_returns_none():
    assert best_transform_for("bogus_detector_id", None) is None


# ── chooser is deterministic for the same input ───────────────────────

def test_same_input_same_output():
    f = _f("ssn", detector="ssn", unique_rate=1.0, is_likely_unique=True,
           alphabet="digits", max_length=9, value_set_size_class="unique")
    out1 = best_transform_for("ssn", f)
    out2 = best_transform_for("ssn", f)
    assert out1 == out2
