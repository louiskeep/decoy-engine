"""Strategy-aware quality policy (V2 Phase 3 D4).

Distinguishes *intentional* loss (a strategy is meant to destroy
shape) from *accidental* drift (a strategy preserves shape, but the
output drifted anyway). The QualityReport already records the
similarity numbers; this layer adds the judgment.

Inputs:
  - A persisted QualityReport dict (D1d shape).
  - A `policy_config` dict (see schema below).
  - Optional `strategy_map: {column_name: strategy_name}` so per-column
    expectations can fall back to per-strategy defaults.

Output: a `quality-policy/v1` dict:
    {
      "schema_version": "quality-policy/v1",
      "verdict": "pass" | "warn" | "fail",
      "mode": "report" | "warn" | "fail",
      "violations": [
        {"check": str, "severity": "warn"|"fail",
         "expected": ..., "actual": ..., "detail": str},
        ...
      ],
    }

Mode semantics:
  - "report": violations are recorded but verdict is always "pass".
    Operators see the numbers; no gating.
  - "warn": violations promote verdict to "warn" (UI surfaces an
    alert; job still succeeds).
  - "fail": violations promote verdict to "fail" (caller can choose
    to fail the job; this module never raises).

The module is pure: takes dicts, returns a dict, never raises on
bad input (malformed config is treated as no-policy -> pass).

Strategy expectation defaults are listed in
`_DEFAULT_STRATEGY_EXPECTATIONS`. These are starting points based on
the per-strategy semantics in
`docs/backlog/v2/plans/decoy-platform/2026-05-09-distribution-synthesis.md`:

  - identity: 1.00  (no change at all)
  - hash:     0.95  (preserves cardinality + null rate)
  - shuffle:  0.85  (preserves marginals exactly; pairwise shifts)
  - bucketize: 0.70 (intentional generalization)
  - date_shift: 0.80 (preserves intervals)
  - faker:    0.10  (preserves type but not value semantics)
  - redact:   0.05  (intentional destruction)
  - generate: 0.30  (synthetic; D6 will raise this)

Overrides via `policy_config["strategy_expectations"]` win over
defaults; explicit per-column thresholds via
`policy_config["columns"]` win over both.

Out of scope:
  - The platform-side decision to actually fail the job. This module
    emits the verdict; the caller (runner / API consumer) gates.
  - Persisting the policy verdict to a DB table. The verdict travels
    with the report dict if the caller chooses to attach it.
"""

from __future__ import annotations

from typing import Any

QUALITY_POLICY_SCHEMA_VERSION = "quality-policy/v1"

# Per-strategy default expected minimum similarity in [0, 1] under
# the CURRENT D1c comparator (TVD on value identity for categorical
# top-K, quantile RMSE for numeric). Corrected in D5a after the D5
# design survey caught that the original D4 defaults assumed an
# aspirational "shape-preserving" comparator that doesn't exist yet.
# See docs/audit/v2-d5-design-survey.md for the full table + rationale.
#
# Under value-identity TVD:
#   - hash produces totally disjoint value sets (the hashes don't
#     match source values), so similarity collapses to ~0.0.
#   - bucketize collapses many source values into one bucket
#     value, also driving TVD up.
#   - date_shift moves values into new year bins.
#   - faker / redact / generate replace value sets entirely.
# Only identity, shuffle, and (with limits) per-row passthroughs
# preserve value identity.
#
# These defaults will RISE again in D5b once a shape-only similarity
# metric lands; until then, this is what the current TVD comparator
# actually reports, and operators who turn on policy mode=warn|fail
# need defaults that match measurement reality.
_DEFAULT_STRATEGY_EXPECTATIONS: dict[str, float] = {
    "identity": 1.00,
    "hash": 0.05,        # D5a: was 0.95; value-identity TVD treats hashes as disjoint
    "shuffle": 0.85,     # unchanged: shuffle keeps same value set + frequencies
    "bucketize": 0.30,   # D5a: was 0.70; collapses values, TVD reflects the loss
    "date_shift": 0.50,  # D5a: was 0.80; shifted dates land in different year bins
    "faker": 0.05,       # D5a: was 0.10; faker produces disjoint values
    "redact": 0.05,      # unchanged
    "generate": 0.30,    # unchanged: synthetic data, modest baseline
}

_VALID_MODES = {"report", "warn", "fail"}


def apply_quality_policy(
    report: dict[str, Any],
    policy_config: dict[str, Any] | None = None,
    *,
    strategy_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Evaluate a QualityReport against a policy config; return verdict.

    Args:
        report: A QualityReport dict (D1d, schema `quality-report/v1`).
        policy_config: Operator-supplied policy. None or empty maps to
            "report mode, no thresholds" -> always "pass".
        strategy_map: Optional per-column strategy assignments. Used
            to fall back to `strategy_expectations` defaults when a
            column lacks an explicit threshold.

    Returns:
        A `quality-policy/v1` dict. See module docstring for shape.
    """
    config = policy_config or {}
    mode = str(config.get("mode", "report")).lower()
    if mode not in _VALID_MODES:
        mode = "report"

    thresholds = config.get("thresholds") or {}
    strategy_expectations = {
        **_DEFAULT_STRATEGY_EXPECTATIONS,
        **(config.get("strategy_expectations") or {}),
    }
    column_overrides = _normalize_column_overrides(
        thresholds.get("columns") or config.get("columns") or [],
    )

    violations: list[dict[str, Any]] = []

    _check_diagnostic(report, thresholds, violations)
    _check_overall(report, thresholds, violations)
    _check_marginal(report, thresholds, violations)
    _check_pairwise(report, thresholds, violations)
    _check_per_column(
        report,
        column_overrides=column_overrides,
        strategy_map=strategy_map or {},
        strategy_expectations=strategy_expectations,
        violations=violations,
    )

    verdict = _verdict_for(mode, violations)
    return {
        "schema_version": QUALITY_POLICY_SCHEMA_VERSION,
        "verdict": verdict,
        "mode": mode,
        "violations": violations,
    }


# ── per-check evaluators ───────────────────────────────────────────────────


def _check_diagnostic(
    report: dict[str, Any],
    thresholds: dict[str, Any],
    violations: list[dict[str, Any]],
) -> None:
    cfg = thresholds.get("diagnostic") or {}
    required = bool(cfg.get("required", False))
    if not required:
        return
    passed = bool(report.get("diagnostic", {}).get("passed", False))
    if not passed:
        violations.append(
            {
                "check": "diagnostic",
                "severity": "fail",
                "expected": "passed",
                "actual": "failed",
                "detail": "Diagnostic must pass when thresholds.diagnostic.required=true",
            },
        )


def _check_overall(
    report: dict[str, Any],
    thresholds: dict[str, Any],
    violations: list[dict[str, Any]],
) -> None:
    cfg = thresholds.get("overall") or {}
    minimum = cfg.get("min")
    if minimum is None:
        return
    actual = report.get("overall_score")
    if actual is None or float(actual) < float(minimum):
        violations.append(
            {
                "check": "overall",
                "severity": "fail",
                "expected": float(minimum),
                "actual": actual,
                "detail": f"overall_score {actual} below minimum {minimum}",
            },
        )


def _check_marginal(
    report: dict[str, Any],
    thresholds: dict[str, Any],
    violations: list[dict[str, Any]],
) -> None:
    cfg = thresholds.get("marginal") or {}
    minimum = cfg.get("min")
    if minimum is None:
        return
    actual = report.get("marginal", {}).get("score")
    if actual is None or float(actual) < float(minimum):
        violations.append(
            {
                "check": "marginal",
                "severity": "fail",
                "expected": float(minimum),
                "actual": actual,
                "detail": f"marginal.score {actual} below minimum {minimum}",
            },
        )


def _check_pairwise(
    report: dict[str, Any],
    thresholds: dict[str, Any],
    violations: list[dict[str, Any]],
) -> None:
    cfg = thresholds.get("pairwise") or {}
    minimum = cfg.get("min")
    if minimum is None:
        return
    actual = report.get("pairwise", {}).get("score")
    if actual is None or float(actual) < float(minimum):
        violations.append(
            {
                "check": "pairwise",
                "severity": "fail",
                "expected": float(minimum),
                "actual": actual,
                "detail": f"pairwise.score {actual} below minimum {minimum}",
            },
        )


def _check_per_column(
    report: dict[str, Any],
    *,
    column_overrides: dict[str, float],
    strategy_map: dict[str, str],
    strategy_expectations: dict[str, float],
    violations: list[dict[str, Any]],
) -> None:
    columns = report.get("marginal", {}).get("columns", [])
    for col in columns:
        if not isinstance(col, dict):
            continue
        name = col.get("column")
        if not isinstance(name, str):
            continue
        if not col.get("comparable"):
            # Skipped from the score; D1b diagnostic already flagged
            # kind drift / empty / etc.
            continue
        sim = col.get("similarity")
        if sim is None:
            continue
        # Threshold resolution priority: explicit column override ->
        # per-strategy expectation -> no threshold (skip).
        minimum: float | None = column_overrides.get(name)
        if minimum is None:
            strategy = strategy_map.get(name)
            if strategy is not None:
                minimum = strategy_expectations.get(strategy)
        if minimum is None:
            continue
        if float(sim) < float(minimum):
            violations.append(
                {
                    "check": "column",
                    "column": name,
                    "strategy": strategy_map.get(name),
                    "severity": "fail",
                    "expected": float(minimum),
                    "actual": float(sim),
                    "detail": f"column {name!r} similarity {sim} below minimum {minimum}",
                },
            )


# ── helpers ────────────────────────────────────────────────────────────────


def _normalize_column_overrides(raw: Any) -> dict[str, float]:
    """Accept either {col: min} dict or [{column, min}, ...] list shape."""
    overrides: dict[str, float] = {}
    if isinstance(raw, dict):
        for col, minimum in raw.items():
            if isinstance(col, str) and isinstance(minimum, (int, float)):
                overrides[col] = float(minimum)
        return overrides
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            col = entry.get("column")
            minimum = entry.get("min")
            if isinstance(col, str) and isinstance(minimum, (int, float)):
                overrides[col] = float(minimum)
    return overrides


def _verdict_for(mode: str, violations: list[dict[str, Any]]) -> str:
    if not violations:
        return "pass"
    if mode == "report":
        return "pass"
    if mode == "warn":
        return "warn"
    return "fail"
