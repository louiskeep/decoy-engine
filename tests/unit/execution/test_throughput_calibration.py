"""Job-time estimator Slice 1: throughput calibration validation.

The core honesty check: each reference-host anchor the calibration is built from
must round-trip WITHIN its own band (the model reproduces the measurement it was
calibrated on), the band is monotonic in work, and the functions are advisory --
they never raise, even on an unknown route.
"""

from __future__ import annotations

import pytest

from decoy_engine.execution._throughput_calibration import (
    RuntimeBand,
    estimate_generation_runtime_band,
    estimate_mask_runtime_band,
)

_MIB = 1024 * 1024


def _within(band: RuntimeBand, seconds: float) -> bool:
    return band.low_seconds <= seconds <= band.high_seconds


class TestAnchorsRoundTrip:
    def test_native_mask_anchor_within_band(self):
        # b6a native_2000000: 160,000,000 source bytes, resident wall_s p50 3.539 s.
        band = estimate_mask_runtime_band(160_000_000, "native")
        assert _within(band, 3.539), f"measured 3.539s not in band {band}"
        assert band.confidence == "medium"  # directly measured route

    def test_generation_anchor_within_band(self):
        # f4a: ~14k rows/s floor -> 10M rows ~= 714 s.
        band = estimate_generation_runtime_band(10_000_000)
        assert _within(band, 10_000_000 / 14_000)
        assert band.confidence == "low"  # per-row cost swings by provider


class TestBandShape:
    def test_band_is_ordered_and_positive(self):
        for route in ("native", "streaming", "out_of_core", "full_frame", "sequential"):
            band = estimate_mask_runtime_band(500 * _MIB, route)
            assert 0 < band.low_seconds <= band.high_seconds

    def test_runtime_is_monotonic_in_bytes(self):
        small = estimate_mask_runtime_band(100 * _MIB, "native")
        large = estimate_mask_runtime_band(1000 * _MIB, "native")
        assert large.low_seconds > small.low_seconds
        assert large.high_seconds > small.high_seconds

    def test_tiny_job_low_end_is_floored(self):
        # A near-empty job must not claim a sub-0.5s low end (fixed overhead).
        band = estimate_mask_runtime_band(1, "native")
        assert band.low_seconds == pytest.approx(0.5)
        assert band.high_seconds >= band.low_seconds


class TestAdvisoryContract:
    def test_uncalibrated_route_is_low_confidence_not_a_raise(self):
        band = estimate_mask_runtime_band(500 * _MIB, "some_future_route")
        assert band.confidence == "low"
        # priced at the slowest known rate (conservative, never optimistic)
        slowest = estimate_mask_runtime_band(500 * _MIB, "full_frame")
        assert band.low_seconds == pytest.approx(slowest.low_seconds)

    def test_conservative_mask_routes_are_low_confidence(self):
        for route in ("out_of_core", "full_frame", "sequential"):
            assert estimate_mask_runtime_band(500 * _MIB, route).confidence == "low"

    def test_zero_work_never_raises(self):
        assert estimate_mask_runtime_band(0, "native").low_seconds == pytest.approx(0.5)
        assert estimate_generation_runtime_band(0).low_seconds == pytest.approx(0.5)
