"""Source-aware residual-PII escalation (audit C1/H6 regression suite).

Pre-fix, ``check_residual_pii`` never saw the source frames, so a column
whose mask silently failed (output byte-identical to source) on a
faker-family strategy reported severity='info' -- indistinguishable from
a correctly-synthesized column. These tests pin the escalation contract:
positional output==source identity on a strategy that should have
replaced values escalates the finding, while legitimate-by-design value
reuse (shuffle permutations, categorical resampling from the source
category set) never false-positives.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.storm.postmask import run_storm_post_mask
from decoy_engine.storm.postmask.residual_pii import check_residual_pii

_EMAILS = [f"user{i}@realmail.com" for i in range(100)]


def _cfg(strategy: str | None, column: str = "email") -> dict:
    columns = [] if strategy is None else [{"name": column, "strategy": strategy}]
    return {"version": 1, "tables": [{"name": "t", "columns": columns}]}


def _frames(src_vals: list, out_vals: list, column: str = "email"):
    return (
        {"t": pd.DataFrame({column: src_vals})},
        {"t": pd.DataFrame({column: out_vals})},
    )


def _one(findings):
    assert len(findings) == 1, f"expected exactly one finding, got {findings!r}"
    return findings[0]


class TestC1RegressionFakerLeak:
    def test_faker_output_identical_to_source_is_fail(self):
        """The audit's C1 repro: masking failed, output IS source -> fail."""
        source, output = _frames(_EMAILS, _EMAILS)
        finding = _one(check_residual_pii(output, _cfg("faker"), source_frames=source))
        assert finding.severity == "fail"
        assert finding.source_compared is True
        assert finding.source_identity_rate == 1.0

    def test_faker_leak_fails_through_runner(self):
        source, output = _frames(_EMAILS, _EMAILS)
        report = run_storm_post_mask(source, output, config=_cfg("faker"))
        assert report["fail_count"] >= 1
        sevs = [f["severity"] for f in report["residual_pii"]]
        assert "fail" in sevs

    def test_faker_legit_synthetic_output_stays_info(self):
        synthetic = [f"fake{i}@masked.example" for i in range(100)]
        source, output = _frames(_EMAILS, synthetic)
        finding = _one(check_residual_pii(output, _cfg("faker"), source_frames=source))
        assert finding.severity == "info"
        assert finding.source_compared is True
        assert finding.source_identity_rate == 0.0

    def test_faker_partial_leak_warns_below_fail_threshold(self):
        # 20% of rows kept their source value: above the 5% warn floor,
        # below the 50% fail bar.
        out = [s if i < 20 else f"fake{i}@masked.example" for i, s in enumerate(_EMAILS)]
        source, output = _frames(_EMAILS, out)
        finding = _one(check_residual_pii(output, _cfg("faker"), source_frames=source))
        assert finding.severity == "warning"

    def test_faker_majority_leak_is_fail(self):
        out = [s if i < 60 else f"fake{i}@masked.example" for i, s in enumerate(_EMAILS)]
        source, output = _frames(_EMAILS, out)
        finding = _one(check_residual_pii(output, _cfg("faker"), source_frames=source))
        assert finding.severity == "fail"


class TestValueReuseStrategiesNoFalsePositive:
    def test_categorical_resample_from_source_categories_stays_info(self):
        # from_profile-style reuse: same category SET, different positions.
        src = (["alice@x.com", "bob@x.com"] * 50)[:100]
        out = (["bob@x.com", "alice@x.com"] * 50)[:100]  # rotated, identity rate 0
        source, output = _frames(src, out)
        finding = _one(check_residual_pii(output, _cfg("categorical"), source_frames=source))
        assert finding.severity == "info"

    def test_categorical_full_identity_is_fail(self):
        src = [f"u{i}@x.com" for i in range(100)]
        source, output = _frames(src, list(src))
        finding = _one(check_residual_pii(output, _cfg("categorical"), source_frames=source))
        assert finding.severity == "fail"

    def test_shuffle_derangement_stays_info(self):
        src = [f"u{i}@x.com" for i in range(100)]
        out = src[1:] + src[:1]  # full derangement, 100% value overlap
        source, output = _frames(src, out)
        finding = _one(check_residual_pii(output, _cfg("shuffle"), source_frames=source))
        assert finding.severity == "info"

    def test_shuffle_with_zero_movement_is_fail(self):
        src = [f"u{i}@x.com" for i in range(100)]
        source, output = _frames(src, list(src))
        finding = _one(check_residual_pii(output, _cfg("shuffle"), source_frames=source))
        assert finding.severity == "fail"

    def test_constant_source_never_escalates(self):
        # Identity rate is meaningless when the source has one distinct
        # value; identical output is a coincidence, not a leak signal.
        src = ["same@x.com"] * 50
        source, output = _frames(src, list(src))
        finding = _one(check_residual_pii(output, _cfg("categorical"), source_frames=source))
        assert finding.severity == "info"


class TestH6UnconfiguredColumns:
    def test_unconfigured_leak_is_fail(self):
        """Output==source on an unconfigured PII column with a
        high-confidence detector hit must fail the report (pre-fix it
        capped at 'warning', making the CLI's exit 4 unreachable)."""
        source, output = _frames(_EMAILS, _EMAILS)
        finding = _one(check_residual_pii(output, _cfg(None), source_frames=source))
        assert finding.severity == "fail"

    def test_unconfigured_synthetic_column_stays_warning(self):
        synthetic = [f"fake{i}@masked.example" for i in range(100)]
        source, output = _frames(_EMAILS, synthetic)
        finding = _one(check_residual_pii(output, _cfg(None), source_frames=source))
        assert finding.severity == "warning"

    def test_unconfigured_column_missing_from_source_stays_warning(self):
        source = {"t": pd.DataFrame({"other": ["x"] * 100})}
        output = {"t": pd.DataFrame({"email": _EMAILS})}
        finding = _one(check_residual_pii(output, _cfg(None), source_frames=source))
        assert finding.severity == "warning"
        assert finding.source_compared is False


class TestNonComparablePathsDegrade:
    def test_no_source_frames_keeps_legacy_behavior(self):
        # Two-positional-arg call shape must keep working and must not
        # escalate (the documented degradation when source is absent).
        _, output = _frames(_EMAILS, _EMAILS)
        finding = _one(check_residual_pii(output, _cfg("faker")))
        assert finding.severity == "info"
        assert finding.source_compared is False
        assert finding.source_identity_rate is None

    def test_source_missing_table_degrades(self):
        output = {"t": pd.DataFrame({"email": _EMAILS})}
        finding = _one(
            check_residual_pii(output, _cfg("faker"), source_frames={"elsewhere": pd.DataFrame()})
        )
        assert finding.severity == "info"
        assert finding.source_compared is False

    def test_row_count_mismatch_degrades(self):
        source = {"t": pd.DataFrame({"email": _EMAILS[:50]})}
        output = {"t": pd.DataFrame({"email": _EMAILS})}
        finding = _one(check_residual_pii(output, _cfg("faker"), source_frames=source))
        assert finding.severity == "info"
        assert finding.source_compared is False

    def test_nondefault_index_alignment(self):
        # Frames with weird indexes still compare positionally.
        src_df = pd.DataFrame({"email": _EMAILS}, index=range(1000, 1100))
        out_df = pd.DataFrame({"email": _EMAILS}, index=range(100))
        finding = _one(
            check_residual_pii({"t": out_df}, _cfg("faker"), source_frames={"t": src_df})
        )
        assert finding.severity == "fail"
        assert finding.source_identity_rate == 1.0

    def test_passthrough_never_compared(self):
        source, output = _frames(_EMAILS, _EMAILS)
        finding = _one(check_residual_pii(output, _cfg("passthrough"), source_frames=source))
        assert finding.severity == "info"

    def test_destroys_pattern_strategies_unchanged(self):
        # hash already fails on any surviving hit; source comparison
        # must not lower or alter that.
        source, output = _frames(_EMAILS, _EMAILS)
        finding = _one(check_residual_pii(output, _cfg("hash"), source_frames=source))
        assert finding.severity == "fail"
