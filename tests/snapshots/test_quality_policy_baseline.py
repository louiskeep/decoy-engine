"""Snapshot harness for quality policy verdicts (V2 Phase 3 D4).

Pins SHA-256 digests for the policy dict across canonical fixtures.
Any change to violation ordering, severity defaults, mode semantics,
or default strategy expectations fails CI with one fixture name.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.quality.policy import apply_quality_policy

GOLDEN = Path(__file__).parent / "golden" / "quality_policy"


def _report(
    *,
    overall: float = 0.9,
    marginal: float = 0.92,
    pairwise: float = 0.85,
    diagnostic_passed: bool = True,
    columns: list[dict] | None = None,
) -> dict[str, Any]:
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


def _empty_policy_pass() -> dict[str, Any]:
    return apply_quality_policy(_report())


def _overall_fail() -> dict[str, Any]:
    return apply_quality_policy(
        _report(overall=0.5),
        {"mode": "fail", "thresholds": {"overall": {"min": 0.95}}},
    )


def _per_column_strategy_violation() -> dict[str, Any]:
    cols = [
        {"column": "ssn", "similarity": 0.80, "method": "tvd", "comparable": True},
    ]
    return apply_quality_policy(
        _report(columns=cols),
        {"mode": "fail"},
        strategy_map={"ssn": "hash"},
    )


def _all_checks_violate_warn_mode() -> dict[str, Any]:
    cols = [
        {"column": "age", "similarity": 0.50, "method": "tvd", "comparable": True},
    ]
    return apply_quality_policy(
        _report(diagnostic_passed=False, columns=cols),
        {
            "mode": "warn",
            "thresholds": {
                "diagnostic": {"required": True},
                "overall": {"min": 0.95},
                "marginal": {"min": 0.95},
                "pairwise": {"min": 0.95},
                "columns": [{"column": "age", "min": 0.95}],
            },
        },
    )


FIXTURES: dict[str, Callable[[], dict[str, Any]]] = {
    "empty_policy_pass": _empty_policy_pass,
    "overall_fail": _overall_fail,
    "per_column_strategy_violation": _per_column_strategy_violation,
    "all_checks_violate_warn_mode": _all_checks_violate_warn_mode,
}


def _digest(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _golden_path(name: str) -> Path:
    return GOLDEN / f"{name}.sha256"


@pytest.mark.parametrize("name", sorted(FIXTURES.keys()))
def test_quality_policy_baseline(name: str) -> None:
    verdict = FIXTURES[name]()
    digest = _digest(verdict)

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        GOLDEN.mkdir(parents=True, exist_ok=True)
        _golden_path(name).write_text(digest + "\n", encoding="utf-8")
        return

    path = _golden_path(name)
    if not path.exists():
        pytest.fail(
            f"Missing golden for fixture {name!r}. "
            f"Run with UPDATE_SNAPSHOTS=1 to create it, then inspect "
            f"{path} before committing."
        )
    expected = path.read_text(encoding="utf-8").strip()
    if expected != digest:
        pytest.fail(
            f"Quality policy drift on fixture {name!r}\n"
            f"  expected digest: {expected}\n"
            f"  actual digest:   {digest}\n"
            f"  actual payload:  {json.dumps(verdict, indent=2, sort_keys=True)[:2000]}"
        )
