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
    # Column has no explicit min; strategy_map says shuffle -> default
    # expectation is 0.85; actual 0.50 violates. (Hash default dropped
    # to 0.05 in D5a because the current TVD comparator scores hashed
    # columns as ~0 similarity; use shuffle here to test the fallback.)
    cols = [
        {"column": "name", "similarity": 0.50, "method": "tvd", "comparable": True},
    ]
    verdict = apply_quality_policy(
        _report(columns=cols),
        {"mode": "fail"},
        strategy_map={"name": "shuffle"},
    )
    assert verdict["verdict"] == "fail"
    col_violations = [v for v in verdict["violations"] if v["check"] == "column"]
    assert col_violations[0]["column"] == "name"
    assert col_violations[0]["strategy"] == "shuffle"


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
    # Strategy default would yield a different threshold than the
    # explicit column override; verify the explicit override wins.
    # Use shuffle (default 0.85) so explicit 0.70 is meaningfully
    # different. Actual sim 0.80 passes under explicit 0.70 but
    # would fail under shuffle's 0.85 default.
    cols = [
        {"column": "name", "similarity": 0.80, "method": "tvd", "comparable": True},
    ]
    verdict = apply_quality_policy(
        _report(columns=cols),
        {
            "mode": "fail",
            "thresholds": {"columns": [{"column": "name", "min": 0.70}]},
        },
        strategy_map={"name": "shuffle"},
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


# ── D5b wiring (e): shape thresholds + per-strategy shape expectations ─────


def _report_with_shape(
    *,
    overall: float | None = 0.05,  # value-identity (hash tanks)
    shape_overall: float | None = 0.95,  # shape-only (hash preserves)
    shape_marginal: float | None = 0.95,
    shape_pairwise: float | None = None,
    shape_columns: list[dict] | None = None,
    diagnostic_passed: bool = True,
) -> dict:
    base = _report(
        overall=overall, marginal=overall, pairwise=overall, diagnostic_passed=diagnostic_passed
    )
    base["shape_fidelity"] = {
        "schema_version": "quality-shape-fidelity/v1",
        "overall_shape_score": shape_overall,
        "marginal": {
            "shape_score": shape_marginal,
            "columns": shape_columns
            or [
                {
                    "column": "age",
                    "shape_similarity": 0.95,
                    "method": "freq_vector_sorted_tvd",
                    "comparable": True,
                },
            ],
        },
        "pairwise": {"shape_score": shape_pairwise, "joints": []},
    }
    return base


class TestShapeOverallThreshold:
    def test_shape_overall_threshold_violation(self) -> None:
        policy = {"mode": "fail", "thresholds": {"shape": {"overall": {"min": 0.99}}}}
        verdict = apply_quality_policy(_report_with_shape(shape_overall=0.85), policy)
        assert verdict["verdict"] == "fail"
        assert any(v["check"] == "shape_overall" for v in verdict["violations"])

    def test_shape_overall_threshold_passes_when_above(self) -> None:
        policy = {"mode": "fail", "thresholds": {"shape": {"overall": {"min": 0.85}}}}
        verdict = apply_quality_policy(_report_with_shape(shape_overall=0.95), policy)
        assert verdict["verdict"] == "pass"

    def test_shape_thresholds_ignored_when_no_shape_block(self) -> None:
        """Pre-D5b reports (no shape_fidelity) flow through unchanged."""
        report = _report()  # no shape_fidelity
        policy = {"mode": "fail", "thresholds": {"shape": {"overall": {"min": 0.99}}}}
        verdict = apply_quality_policy(report, policy)
        assert verdict["verdict"] == "pass"
        assert not any(v["check"].startswith("shape_") for v in verdict["violations"])


class TestShapeMarginalAndPairwise:
    def test_shape_marginal_violation(self) -> None:
        policy = {"mode": "fail", "thresholds": {"shape": {"marginal": {"min": 0.99}}}}
        verdict = apply_quality_policy(_report_with_shape(shape_marginal=0.5), policy)
        assert verdict["verdict"] == "fail"
        assert any(v["check"] == "shape_marginal" for v in verdict["violations"])

    def test_shape_pairwise_violation(self) -> None:
        policy = {"mode": "fail", "thresholds": {"shape": {"pairwise": {"min": 0.8}}}}
        verdict = apply_quality_policy(
            _report_with_shape(shape_pairwise=0.5),
            policy,
        )
        assert verdict["verdict"] == "fail"
        assert any(v["check"] == "shape_pairwise" for v in verdict["violations"])

    def test_shape_pairwise_skipped_when_none(self) -> None:
        """shape_pairwise=None (no joints) shouldn't fire the threshold check."""
        # Defensive: when pairwise.shape_score is None we currently
        # treat it as a violation (matches value-identity behavior).
        # Operators who set this min must accept that a no-joint job
        # registers a violation.
        policy = {"mode": "fail", "thresholds": {"shape": {"pairwise": {"min": 0.5}}}}
        verdict = apply_quality_policy(
            _report_with_shape(shape_pairwise=None),
            policy,
        )
        # Yes, this fires. Documented behavior; mirrors the existing
        # value-identity pairwise check on `pairwise.score = None`.
        assert verdict["verdict"] == "fail"


class TestShapePerColumn:
    def test_per_column_shape_threshold_violation(self) -> None:
        shape_cols = [
            {
                "column": "age",
                "shape_similarity": 0.3,
                "method": "freq_vector_sorted_tvd",
                "comparable": True,
            },
        ]
        policy = {
            "mode": "fail",
            "thresholds": {
                "shape": {
                    "columns": [
                        {"column": "age", "min": 0.9},
                    ]
                }
            },
        }
        verdict = apply_quality_policy(
            _report_with_shape(shape_columns=shape_cols),
            policy,
        )
        assert verdict["verdict"] == "fail"
        col_violations = [
            v
            for v in verdict["violations"]
            if v["check"] == "shape_column" and v["column"] == "age"
        ]
        assert col_violations
        assert col_violations[0]["expected"] == 0.9
        assert col_violations[0]["actual"] == 0.3

    def test_shape_strategy_fallback(self) -> None:
        """When no per-column override, fall back to per-strategy
        shape expectation. Hash defaults to 0.95 (D5b table). A
        hash column scoring 0.5 on shape triggers a violation."""
        shape_cols = [
            {
                "column": "ssn",
                "shape_similarity": 0.5,
                "method": "freq_vector_sorted_tvd",
                "comparable": True,
            },
        ]
        policy = {"mode": "fail"}
        verdict = apply_quality_policy(
            _report_with_shape(shape_columns=shape_cols),
            policy,
            strategy_map={"ssn": "hash"},
        )
        assert verdict["verdict"] == "fail"
        violations = [v for v in verdict["violations"] if v["check"] == "shape_column"]
        assert violations
        assert violations[0]["strategy"] == "hash"
        # Default hash shape expectation is 0.95.
        assert violations[0]["expected"] == 0.95

    def test_shape_strategy_expectations_override(self) -> None:
        """Operator can override the per-strategy default."""
        shape_cols = [
            {
                "column": "ssn",
                "shape_similarity": 0.5,
                "method": "freq_vector_sorted_tvd",
                "comparable": True,
            },
        ]
        policy = {
            "mode": "fail",
            "shape_strategy_expectations": {"hash": 0.4},
        }
        verdict = apply_quality_policy(
            _report_with_shape(shape_columns=shape_cols),
            policy,
            strategy_map={"ssn": "hash"},
        )
        # 0.5 >= 0.4 -> no violation now.
        assert verdict["verdict"] == "pass"

    def test_incomparable_shape_column_skipped(self) -> None:
        """Empty / kind-mismatched columns aren't dragged into the gate."""
        shape_cols = [
            {
                "column": "empty_col",
                "shape_similarity": None,
                "method": "skipped_empty",
                "comparable": False,
            },
        ]
        policy = {
            "mode": "fail",
            "thresholds": {
                "shape": {
                    "columns": [
                        {"column": "empty_col", "min": 0.9},
                    ]
                }
            },
        }
        verdict = apply_quality_policy(
            _report_with_shape(shape_columns=shape_cols),
            policy,
        )
        # Incomparable column skipped; no shape_column violation.
        assert not any(v["check"] == "shape_column" for v in verdict["violations"])


# ── extreme-case coverage (operator question 2026-05-24) ───────────────────


class TestExtremeCases:
    def test_layered_gates_fire_independently(self) -> None:
        """One column at 100%, one at 0%. Marginal average = 0.5.
        Verify BOTH aggregate AND per-column gates fire with their
        own violations so the operator can tell which rule caught
        what."""
        cols = [
            {"column": "good", "similarity": 1.0, "method": "tvd", "comparable": True},
            {"column": "bad", "similarity": 0.0, "method": "tvd", "comparable": True},
        ]
        policy = {
            "mode": "fail",
            "thresholds": {
                "marginal": {"min": 0.8},  # aggregate gate
                "columns": [{"column": "bad", "min": 0.5}],  # per-column gate
            },
        }
        verdict = apply_quality_policy(
            _report(marginal=0.5, columns=cols),
            policy,
        )
        assert verdict["verdict"] == "fail"
        checks = [v["check"] for v in verdict["violations"]]
        assert "marginal" in checks  # aggregate fired
        assert "column" in checks  # per-column fired
        # Operator sees both with their own details.
        bad_col_violations = [
            v for v in verdict["violations"] if v["check"] == "column" and v["column"] == "bad"
        ]
        assert bad_col_violations
        assert bad_col_violations[0]["actual"] == 0.0

    def test_all_incomparable_columns_does_not_fire_per_column(self) -> None:
        """Every column got skipped -> per-column gate produces 0
        violations. The aggregate scores would be None and the
        overall.min gate (separate check) would fire instead."""
        cols = [
            {"column": "a", "similarity": None, "method": "skipped", "comparable": False},
            {"column": "b", "similarity": None, "method": "skipped", "comparable": False},
        ]
        policy = {
            "mode": "fail",
            "thresholds": {
                "columns": [
                    {"column": "a", "min": 0.9},
                    {"column": "b", "min": 0.9},
                ]
            },
        }
        verdict = apply_quality_policy(
            _report(columns=cols),
            policy,
        )
        # Per-column check fires zero violations; the per-column gate
        # explicitly skips incomparable columns.
        col_violations = [v for v in verdict["violations"] if v["check"] == "column"]
        assert col_violations == []

    def test_value_disjoint_shape_preserved_no_violation_with_layered_policy(self) -> None:
        """The D5b motivating case at the policy layer: hash strategy
        with low value-identity (0.05) but high shape (0.95) should
        pass a policy that uses both metrics with strategy defaults."""
        cols = [
            {"column": "ssn", "similarity": 0.05, "method": "tvd", "comparable": True},
        ]
        shape_cols = [
            {
                "column": "ssn",
                "shape_similarity": 0.96,
                "method": "freq_vector_sorted_tvd",
                "comparable": True,
            },
        ]
        report = _report(
            overall=0.05,
            marginal=0.05,
            pairwise=None,
            columns=cols,
        )
        report["shape_fidelity"] = {
            "schema_version": "quality-shape-fidelity/v1",
            "overall_shape_score": 0.96,
            "marginal": {"shape_score": 0.96, "columns": shape_cols},
            "pairwise": {"shape_score": None, "joints": []},
        }
        # Use defaults: hash value-identity expects 0.05, shape expects 0.95.
        policy = {"mode": "fail"}
        verdict = apply_quality_policy(
            report,
            policy,
            strategy_map={"ssn": "hash"},
        )
        # Both checks satisfied by their respective defaults -> pass.
        assert verdict["verdict"] == "pass"
        assert verdict["violations"] == []
