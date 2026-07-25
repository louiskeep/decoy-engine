"""Unit tests for decoy_engine.quality.diagnostic (V2 Phase 3 D1b).

Coverage:
  - Identity case: same snapshot twice -> all checks pass.
  - column_survival: missing column fails, added column observed but
    does not fail.
  - row_count: parity required (mask path) vs. opted out (generate
    path).
  - kind_drift: differing kinds per column flagged.
  - null_drift: above-threshold delta flagged, at/below ignored,
    threshold kwarg honored.
  - Mutation contract: input snapshots are not modified.
  - Determinism + JSON serializability.
  - Zero-row column handled without ZeroDivisionError.
"""

from __future__ import annotations

import copy
import json

import pytest

from decoy_engine.quality.diagnostic import (
    QUALITY_DIAGNOSTIC_SCHEMA_VERSION,
    compute_diagnostic,
)

# ── fixtures ─────────────────────────────────────────────────────────────────


def _col(
    *,
    kind: str,
    null_count: int = 0,
    non_null_count: int = 100,
    distinct_count: int = 5,
    dtype: str = "object",
    stats: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "dtype": dtype,
        "kind": kind,
        "null_count": null_count,
        "non_null_count": non_null_count,
        "distinct_count": distinct_count,
        "stats": stats or {},
    }


def _snap(
    columns: dict[str, dict[str, object]],
    row_count: int = 100,
) -> dict[str, object]:
    return {
        "schema_version": "distribution-snapshot/v1",
        "row_count": row_count,
        "columns": columns,
        "joints": [],
    }


@pytest.fixture
def base_source() -> dict[str, object]:
    return _snap(
        {
            "name": _col(kind="freetext", null_count=0, non_null_count=100),
            "age": _col(kind="numeric", null_count=5, non_null_count=95, dtype="int64"),
            "state": _col(kind="categorical", null_count=0, non_null_count=100),
        }
    )


# ── envelope + identity ──────────────────────────────────────────────────────


def test_identity_diagnostic_passes(base_source: dict[str, object]) -> None:
    diag = compute_diagnostic(base_source, base_source)
    assert diag["schema_version"] == QUALITY_DIAGNOSTIC_SCHEMA_VERSION
    assert diag["passed"] is True
    assert {c["check"] for c in diag["checks"]} == {
        "column_survival",
        "row_count",
        "kind_drift",
        "null_drift",
    }
    for check in diag["checks"]:
        assert check["passed"] is True


def test_diagnostic_is_json_serializable(base_source: dict[str, object]) -> None:
    diag = compute_diagnostic(base_source, base_source)
    encoded = json.dumps(diag, sort_keys=True)
    assert isinstance(encoded, str)


def test_diagnostic_is_deterministic(base_source: dict[str, object]) -> None:
    d1 = compute_diagnostic(base_source, base_source)
    d2 = compute_diagnostic(base_source, base_source)
    assert json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True)


def test_diagnostic_does_not_mutate_inputs(base_source: dict[str, object]) -> None:
    src_before = copy.deepcopy(base_source)
    out_before = copy.deepcopy(base_source)
    compute_diagnostic(base_source, base_source)
    assert base_source == src_before == out_before


# ── column_survival ─────────────────────────────────────────────────────────


def test_missing_column_fails_survival(base_source: dict[str, object]) -> None:
    output = _snap({k: v for k, v in base_source["columns"].items() if k != "age"})
    diag = compute_diagnostic(base_source, output)
    survival = next(c for c in diag["checks"] if c["check"] == "column_survival")
    assert survival["passed"] is False
    assert survival["missing_columns"] == ["age"]
    assert survival["added_columns"] == []
    assert diag["passed"] is False


def test_added_column_observed_but_does_not_fail(
    base_source: dict[str, object],
) -> None:
    output = _snap(
        {
            **base_source["columns"],
            "derived_id": _col(kind="categorical", non_null_count=100),
        }
    )
    diag = compute_diagnostic(base_source, output)
    survival = next(c for c in diag["checks"] if c["check"] == "column_survival")
    assert survival["passed"] is True
    assert survival["missing_columns"] == []
    assert survival["added_columns"] == ["derived_id"]


# ── row_count ────────────────────────────────────────────────────────────────


def test_row_count_parity_required_by_default(
    base_source: dict[str, object],
) -> None:
    output = _snap(base_source["columns"], row_count=80)
    diag = compute_diagnostic(base_source, output)
    row = next(c for c in diag["checks"] if c["check"] == "row_count")
    assert row["passed"] is False
    assert row["source_rows"] == 100
    assert row["output_rows"] == 80
    assert diag["passed"] is False


def test_row_count_parity_opt_out_for_generate(
    base_source: dict[str, object],
) -> None:
    output = _snap(base_source["columns"], row_count=80)
    diag = compute_diagnostic(base_source, output, expect_row_parity=False)
    row = next(c for c in diag["checks"] if c["check"] == "row_count")
    assert row["passed"] is True
    assert row["expect_parity"] is False
    # Counts still reported for the operator to see.
    assert row["source_rows"] == 100
    assert row["output_rows"] == 80


# ── kind_drift ───────────────────────────────────────────────────────────────


def test_kind_drift_flagged_when_kind_changes(
    base_source: dict[str, object],
) -> None:
    # Simulate a strategy misconfiguration: numeric -> freetext.
    output_cols = copy.deepcopy(base_source["columns"])
    output_cols["age"]["kind"] = "freetext"
    diag = compute_diagnostic(base_source, _snap(output_cols))
    kind = next(c for c in diag["checks"] if c["check"] == "kind_drift")
    assert kind["passed"] is False
    assert kind["drifted"] == [
        {"column": "age", "source_kind": "numeric", "output_kind": "freetext"}
    ]
    assert diag["passed"] is False


def test_kind_drift_only_inspects_surviving_columns(
    base_source: dict[str, object],
) -> None:
    # Drop a column AND change another's kind. Survival fires, kind
    # only fires on the survivor so the missing column is not
    # double-counted.
    output_cols = {
        "name": base_source["columns"]["name"],
        "age": copy.deepcopy(base_source["columns"]["age"]),
    }
    output_cols["age"]["kind"] = "freetext"
    diag = compute_diagnostic(base_source, _snap(output_cols))
    kind = next(c for c in diag["checks"] if c["check"] == "kind_drift")
    assert [d["column"] for d in kind["drifted"]] == ["age"]


# ── null_drift ──────────────────────────────────────────────────────────────


def test_null_drift_flagged_past_threshold(
    base_source: dict[str, object],
) -> None:
    # Source age has 5/100 null -> 5%. Output goes to 50/100 -> 50%.
    # Delta 45pp > default 10pp threshold.
    output_cols = copy.deepcopy(base_source["columns"])
    output_cols["age"]["null_count"] = 50
    output_cols["age"]["non_null_count"] = 50
    diag = compute_diagnostic(base_source, _snap(output_cols))
    null = next(c for c in diag["checks"] if c["check"] == "null_drift")
    assert null["passed"] is False
    assert len(null["drifted"]) == 1
    drift = null["drifted"][0]
    assert drift["column"] == "age"
    assert drift["delta_pp"] == pytest.approx(45.0)


def test_null_drift_at_threshold_passes(
    base_source: dict[str, object],
) -> None:
    # Source age 5% null; bump output to 15% (exactly +10pp; uses
    # strict ">" comparison so equal-to-threshold passes).
    output_cols = copy.deepcopy(base_source["columns"])
    output_cols["age"]["null_count"] = 15
    output_cols["age"]["non_null_count"] = 85
    diag = compute_diagnostic(base_source, _snap(output_cols))
    null = next(c for c in diag["checks"] if c["check"] == "null_drift")
    assert null["passed"] is True


def test_null_drift_threshold_kwarg_honored(
    base_source: dict[str, object],
) -> None:
    # Same +10pp shift, tighter threshold (5pp) catches it.
    output_cols = copy.deepcopy(base_source["columns"])
    output_cols["age"]["null_count"] = 15
    output_cols["age"]["non_null_count"] = 85
    diag = compute_diagnostic(
        base_source,
        _snap(output_cols),
        null_drift_threshold_pp=5.0,
    )
    null = next(c for c in diag["checks"] if c["check"] == "null_drift")
    assert null["passed"] is False


def test_null_drift_skips_zero_row_column(
    base_source: dict[str, object],
) -> None:
    # A column present in both snapshots but with 0 rows on the
    # output side: skip rather than divide-by-zero on the null pct.
    output_cols = copy.deepcopy(base_source["columns"])
    output_cols["age"]["null_count"] = 0
    output_cols["age"]["non_null_count"] = 0
    diag = compute_diagnostic(base_source, _snap(output_cols, row_count=0))
    null = next(c for c in diag["checks"] if c["check"] == "null_drift")
    # `age` is silently skipped; nothing else drifted; check passes.
    assert all(d["column"] != "age" for d in null["drifted"])


# ── multi-check interaction ─────────────────────────────────────────────────


def test_top_level_passed_requires_all_checks_pass(
    base_source: dict[str, object],
) -> None:
    # Only row_count fails; top-level passed must be False.
    output = _snap(base_source["columns"], row_count=80)
    diag = compute_diagnostic(base_source, output)
    assert diag["passed"] is False
    assert sum(1 for c in diag["checks"] if c["passed"]) == 3
