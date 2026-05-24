"""Unit tests for decoy_engine.quality.policy (V2 Phase 3 D4).

Coverage:
  - Empty policy / no thresholds -> always pass.
  - Diagnostic required check.
  - Overall / marginal / pairwise threshold checks.
  - Per-column explicit threshold checks.
  - Strategy-map fallback to per-strategy expected minimums.
  - Mode semantics: report -> pass even with violations; warn ->
    warn; fail -> fail.
  - Tolerant of malformed input (no raise).
"""

from __future__ import annotations

import pytest

from decoy_engine.quality.policy import (
    QUALITY_POLICY_SCHEMA_VERSION,
    apply_quality_policy,
)


def _report(
    *,
    overall: float | None = 0.9,
    marginal: float | None = 0.92,
    pairwise: float | None = 0.85,
    diagnostic_passed: bool = True,
    columns: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": "quality-report/v1",
        "diagnostic": {"passed": diagnostic_passed, "checks": []},
        "marginal": {
            "score": marginal,
            "columns": columns
            or [{"column": "age", "similarity": 0.92, "method": "tvd", "comparable": True}],
        },
        "pairwise": {"score": pairwise, "joints": []},
        "overall_score": overall,
        "grade": "B",
    }


# ── envelope ───────────────────────────────────────────────────────────────


def test_no_policy_returns_pass() -> None:
    verdict = apply_quality_policy(_report())
    assert verdict["schema_version"] == QUALITY_POLICY_SCHEMA_VERSION
    assert verdict["verdict"] == "pass"
    assert verdict["mode"] == "report"
    assert verdict["violations"] == []


def test_empty_thresholds_returns_pass() -> None:
    verdict = apply_quality_policy(_report(), {"mode": "fail", "thresholds": {}})
    assert verdict["verdict"] == "pass"


# ── diagnostic ─────────────────────────────────────────────────────────────


def test_diagnostic_required_fails_when_diagnostic_failed() -> None:
    rep = _report(diagnostic_passed=False)
    policy = {"mode": "fail", "thresholds": {"diagnostic": {"required": True}}}
    verdict = apply_quality_policy(rep, policy)
    assert verdict["verdict"] == "fail"
    codes = [v["check"] for v in verdict["violations"]]
    assert "diagnostic" in codes


def test_diagnostic_required_passes_when_diagnostic_passed() -> None:
    policy = {"mode": "fail", "thresholds": {"diagnostic": {"required": True}}}
    verdict = apply_quality_policy(_report(diagnostic_passed=True), policy)
    assert verdict["verdict"] == "pass"
    assert verdict["violations"] == []


# ── overall / marginal / pairwise ──────────────────────────────────────────


def test_overall_threshold_violation() -> None:
    policy = {"mode": "fail", "thresholds": {"overall": {"min": 0.95}}}
    verdict = apply_quality_policy(_report(overall=0.9), policy)
    assert verdict["verdict"] == "fail"
    v = next(x for x in verdict["violations"] if x["check"] == "overall")
    assert v["expected"] == pytest.approx(0.95)
    assert v["actual"] == pytest.approx(0.9)


def test_marginal_threshold_violation() -> None:
    policy = {"mode": "fail", "thresholds": {"marginal": {"min": 0.95}}}
    verdict = apply_quality_policy(_report(marginal=0.92), policy)
    assert verdict["verdict"] == "fail"
    assert any(v["check"] == "marginal" for v in verdict["violations"])


def test_pairwise_threshold_violation() -> None:
    policy = {"mode": "fail", "thresholds": {"pairwise": {"min": 0.95}}}
    verdict = apply_quality_policy(_report(pairwise=0.85), policy)
    assert verdict["verdict"] == "fail"
    assert any(v["check"] == "pairwise" for v in verdict["violations"])


def test_overall_threshold_none_score_treated_as_below() -> None:
    # No comparable columns / joints -> overall=None. Must still
    # violate a min threshold so an operator does not get a quiet
    # "pass" on a report that has nothing comparable to score.
    policy = {"mode": "fail", "thresholds": {"overall": {"min": 0.50}}}
    verdict = apply_quality_policy(_report(overall=None), policy)
    assert verdict["verdict"] == "fail"


# ── per-column ─────────────────────────────────────────────────────────────


def test_per_column_explicit_override_violation() -> None:
    cols = [
        {"column": "age", "similarity": 0.6, "method": "tvd", "comparable": True},
        {"column": "state", "similarity": 0.99, "method": "tvd", "comparable": True},
    ]
    policy = {
        "mode": "fail",
        "thresholds": {"columns": [{"column": "age", "min": 0.9}]},
    }
    verdict = apply_quality_policy(_report(columns=cols), policy)
    assert verdict["verdict"] == "fail"
    col_violations = [v for v in verdict["violations"] if v["check"] == "column"]
    assert len(col_violations) == 1
    assert col_violations[0]["column"] == "age"


def test_per_column_strategy_fallback() -> None:
    # Column has no explicit min; strategy_map says hash -> default
    # expectation is 0.95; actual 0.80 violates.
    cols = [
        {"column": "ssn", "similarity": 0.80, "method": "tvd", "comparable": True},
    ]
    verdict = apply_quality_policy(
        _report(columns=cols),
        {"mode": "fail"},
        strategy_map={"ssn": "hash"},
    )
    assert verdict["verdict"] == "fail"
    col_violations = [v for v in verdict["violations"] if v["check"] == "column"]
    assert col_violations[0]["column"] == "ssn"
    assert col_violations[0]["strategy"] == "hash"


def test_per_column_redact_strategy_tolerates_low_score() -> None:
    # redact intentionally destroys; default expectation is 0.05;
    # actual 0.03 is below but still under threshold -> would violate.
    # actual 0.10 is above -> passes.
    cols = [
        {"column": "notes", "similarity": 0.10, "method": "length_mean_diff", "comparable": True},
    ]
    verdict = apply_quality_policy(
        _report(columns=cols),
        {"mode": "fail"},
        strategy_map={"notes": "redact"},
    )
    assert verdict["verdict"] == "pass"


def test_per_column_unknown_strategy_skipped() -> None:
    cols = [
        {"column": "x", "similarity": 0.50, "method": "tvd", "comparable": True},
    ]
    verdict = apply_quality_policy(
        _report(columns=cols),
        {"mode": "fail"},
        strategy_map={"x": "unknown_strategy"},
    )
    # No expectation for unknown_strategy -> no violation.
    assert verdict["verdict"] == "pass"


def test_per_column_incomparable_skipped() -> None:
    cols = [
        {"column": "x", "similarity": None, "method": "kind_mismatch", "comparable": False},
    ]
    verdict = apply_quality_policy(
        _report(columns=cols),
        {"mode": "fail"},
        strategy_map={"x": "hash"},
    )
    assert verdict["verdict"] == "pass"


def test_per_column_override_wins_over_strategy_default() -> None:
    # strategy says hash -> 0.95 (would violate at 0.80); explicit
    # override drops to 0.70 -> passes.
    cols = [
        {"column": "ssn", "similarity": 0.80, "method": "tvd", "comparable": True},
    ]
    verdict = apply_quality_policy(
        _report(columns=cols),
        {
            "mode": "fail",
            "thresholds": {"columns": [{"column": "ssn", "min": 0.70}]},
        },
        strategy_map={"ssn": "hash"},
    )
    assert verdict["verdict"] == "pass"


# ── mode semantics ─────────────────────────────────────────────────────────


def test_report_mode_records_violations_but_passes() -> None:
    policy = {"mode": "report", "thresholds": {"overall": {"min": 0.99}}}
    verdict = apply_quality_policy(_report(overall=0.5), policy)
    assert verdict["verdict"] == "pass"
    assert len(verdict["violations"]) == 1  # recorded


def test_warn_mode_promotes_violations_to_warn() -> None:
    policy = {"mode": "warn", "thresholds": {"overall": {"min": 0.99}}}
    verdict = apply_quality_policy(_report(overall=0.5), policy)
    assert verdict["verdict"] == "warn"


def test_fail_mode_promotes_violations_to_fail() -> None:
    policy = {"mode": "fail", "thresholds": {"overall": {"min": 0.99}}}
    verdict = apply_quality_policy(_report(overall=0.5), policy)
    assert verdict["verdict"] == "fail"


def test_unknown_mode_falls_back_to_report() -> None:
    policy = {"mode": "explode", "thresholds": {"overall": {"min": 0.99}}}
    verdict = apply_quality_policy(_report(overall=0.5), policy)
    assert verdict["mode"] == "report"
    assert verdict["verdict"] == "pass"


# ── overrides for strategy_expectations ────────────────────────────────────


def test_strategy_expectations_override_default() -> None:
    cols = [
        {"column": "ssn", "similarity": 0.92, "method": "tvd", "comparable": True},
    ]
    # Override raises hash expectation above the actual score.
    verdict = apply_quality_policy(
        _report(columns=cols),
        {
            "mode": "fail",
            "strategy_expectations": {"hash": 0.99},
        },
        strategy_map={"ssn": "hash"},
    )
    assert verdict["verdict"] == "fail"


# ── robustness ─────────────────────────────────────────────────────────────


def test_malformed_columns_config_does_not_raise() -> None:
    # 'columns' is supposed to be a list of {column, min} or a dict;
    # garbage should be silently ignored.
    policy = {"mode": "fail", "thresholds": {"columns": "not a list"}}
    verdict = apply_quality_policy(_report(), policy)
    assert verdict["verdict"] == "pass"


def test_dict_shape_column_overrides() -> None:
    # Accept {col_name: min} dict form as well as the canonical list.
    cols = [
        {"column": "age", "similarity": 0.5, "method": "tvd", "comparable": True},
    ]
    policy = {
        "mode": "fail",
        "thresholds": {"columns": {"age": 0.9}},
    }
    verdict = apply_quality_policy(_report(columns=cols), policy)
    assert verdict["verdict"] == "fail"
