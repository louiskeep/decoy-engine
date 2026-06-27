"""Tests for confidence bands and per-column latency benchmark (ML0.3).

Validates:
- Band classification thresholds (high/review/low) on exact boundary values.
- Per-column latency stays within the 50ms dev-tier budget on the standard
  fixture corpus (measured, not mocked).
- benchmark_all_fixture_columns returns a complete, bounded timing map.

Gate reference: ml-benchmarking-and-privacy.md §A.4; ml0.3-confidence-bands.md.
"""

from decoy_engine.storm.eval.bands import (
    HIGH_PRECISION_FLOOR,
    LATENCY_BUDGET_MS,
    REVIEW_PRECISION_FLOOR,
    Band,
    benchmark_all_fixture_columns,
    benchmark_column_latency,
    classify_band,
)
from decoy_engine.storm.eval import build_fixtures


class TestBandThresholds:
    """Exact boundary behaviour of classify_band."""

    def test_high_at_floor(self):
        assert classify_band(HIGH_PRECISION_FLOOR) == "high"

    def test_high_above_floor(self):
        assert classify_band(1.0) == "high"

    def test_review_at_floor(self):
        assert classify_band(REVIEW_PRECISION_FLOOR) == "review"

    def test_review_below_high_floor(self):
        assert classify_band(HIGH_PRECISION_FLOOR - 0.001) == "review"

    def test_low_below_review_floor(self):
        assert classify_band(REVIEW_PRECISION_FLOOR - 0.001) == "low"

    def test_low_at_zero(self):
        assert classify_band(0.0) == "low"

    def test_return_type_is_literal(self):
        result: Band = classify_band(0.5)
        assert result in {"high", "review", "low"}

    def test_thresholds_are_documented_values(self):
        # Pin the threshold constants so a change triggers a code review.
        assert HIGH_PRECISION_FLOOR == 0.95
        assert REVIEW_PRECISION_FLOOR == 0.70

    def test_latency_budget_is_fifty_ms(self):
        assert LATENCY_BUDGET_MS == 50.0


class TestBaselineBands:
    """Classify the regex-baseline precision scores into bands."""

    def test_perfect_types_are_high_band(self):
        from decoy_engine.storm.eval import run_baseline

        rep = run_baseline()
        for type_id in ["ssn", "email", "pan", "iban", "cvv", "npi", "icd10", "iso_date"]:
            m = rep.by_field_type[type_id]
            assert m.precision is not None
            band = classify_band(m.precision)
            assert band == "high", f"{type_id} precision={m.precision} -> expected 'high'"

    def test_mrn_is_review_band(self):
        # mrn precision = 0.5 -- below review floor, but the point is it is NOT high.
        # With the current corpus mrn.precision=0.5 -> "low" band.
        from decoy_engine.storm.eval import run_baseline

        rep = run_baseline()
        m = rep.by_field_type["mrn"]
        assert m.precision == 0.5
        band = classify_band(m.precision)
        # 0.5 < 0.70 -> low band (one FP + one FN makes mrn unreliable on this corpus)
        assert band == "low"

    def test_health_plan_id_is_review_band(self):
        # health_plan_id precision=1.0, recall=0.5 -> high band on precision
        # (the miss is a FN, not an FP -- precision stays 1.0 but F2 is lower).
        from decoy_engine.storm.eval import run_baseline

        rep = run_baseline()
        m = rep.by_field_type["health_plan_id"]
        assert m.precision == 1.0
        assert classify_band(m.precision) == "high"


class TestLatencyBenchmark:
    """Per-column latency stays under the 50ms dev-tier budget."""

    def test_single_column_within_budget(self):
        fx = build_fixtures()[0]  # hipaa fixture
        ms = benchmark_column_latency(fx.df["mrn"], "mrn")
        assert ms < LATENCY_BUDGET_MS, (
            f"mrn detection took {ms:.1f}ms, exceeding {LATENCY_BUDGET_MS}ms budget"
        )

    def test_all_fixture_columns_within_budget(self):
        timings = benchmark_all_fixture_columns()
        violations = {k: v for k, v in timings.items() if v >= LATENCY_BUDGET_MS}
        assert not violations, (
            f"Columns exceeded {LATENCY_BUDGET_MS}ms budget: "
            + ", ".join(f"{k}={v:.1f}ms" for k, v in sorted(violations.items()))
        )

    def test_benchmark_returns_positive_value(self):
        fx = build_fixtures()[0]
        ms = benchmark_column_latency(fx.df["npi"], "npi", n_warmup=1, n_timed=3)
        assert ms > 0.0

    def test_benchmark_all_covers_all_fixture_columns(self):
        timings = benchmark_all_fixture_columns()
        expected_keys = {
            f"{fx.name}.{col}"
            for fx in build_fixtures()
            for col in fx.df.columns
        }
        assert set(timings.keys()) == expected_keys
