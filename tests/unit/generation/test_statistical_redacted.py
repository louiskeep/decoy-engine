"""`load_spec` refuses to sample categories from a snapshot that withheld the source values.

The fixtures come from the real fit writer (`distribution_snapshot_document`), with the
sensitivity predicate pinned to a named set so each cell controls which columns are withheld.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from decoy_engine import reasons
from decoy_engine.generation.statistical import StatisticalSpecError, load_spec, sample_column
from decoy_engine.plan._checks import check_statistical_columns
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.profiling import _snapshot_values as sv
from decoy_engine.profiling import distribution_snapshot_document
from decoy_engine.profiling._value_redaction import (
    POLICY_RAW,
    POLICY_REDACTED,
    REDACTED,
    REDACTED_COLUMNS_KEY,
    VALUE_POLICY_KEY,
)
from decoy_engine.quality.snapshot import compute_distribution_snapshot
from tests.unit._dps_helpers import compile_and_generate

CODE = "statistical_snapshot_values_redacted"
SENSITIVE = ("full_name", "balance", "admitted")
_NAMES = ["Marguerite Okonkwo", "Dmitri Vasquez", "Ingrid Halvorsen", "Tobias Nakamura"]
_ROWS = 48

Build = Callable[..., dict[str, Any]]


def _frame(names: list[str] | None = None) -> pd.DataFrame:
    pool = names or _NAMES
    return pd.DataFrame(
        {
            "full_name": [pool[i % len(pool)] for i in range(_ROWS)],
            "status": ["open"] * 20 + ["closed"] * 16 + ["hold"] * 12,
            "diagnosis": [["flu", "cold", "none"][i % 3] for i in range(_ROWS)],
            "balance": [31337.25 + 211.5 * i for i in range(_ROWS)],
            "admitted": pd.date_range("1991-01-01", periods=_ROWS, freq="9D"),
        }
    )


@pytest.fixture
def build(monkeypatch: pytest.MonkeyPatch) -> Build:
    """Fit the test frame with `SENSITIVE` as the flagged set."""
    monkeypatch.setattr(
        sv,
        "value_sensitive_names",
        lambda fields: [f["name"] for f in fields if f["name"] in SENSITIVE],
    )

    def make(frame: pd.DataFrame | None = None, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("joint_columns", [("full_name", "status"), ("diagnosis", "status")])
        return distribution_snapshot_document(_frame() if frame is None else frame, **kwargs)

    return make


def _cfg(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": "statistical", **extra}


def _refused(snap: dict[str, Any], cfg: dict[str, Any]) -> StatisticalSpecError:
    with pytest.raises(StatisticalSpecError) as exc:
        load_spec(cfg, snapshot=snap)
    assert exc.value.code == CODE
    return exc.value


def _values(spec: Any) -> set[str]:
    return {v["value"] for v in spec.stats["top_values"]}


def test_the_code_is_a_registered_plan_reason() -> None:
    spec = reasons.find("plan", CODE)
    assert spec is not None
    assert spec.owner == "StatisticalSpecError"
    assert reasons.plan.STATISTICAL_SNAPSHOT_VALUES_REDACTED == CODE
    assert spec.since == "1.11.0"


# AT-12 refusals


@pytest.mark.parametrize("extra", [{}, {"allow_real_categories": True}])
def test_a_redacted_categorical_column_is_refused_with_or_without_consent(
    build: Build, extra: dict[str, Any]
) -> None:
    snap = build()
    assert snap[VALUE_POLICY_KEY] == POLICY_REDACTED
    err = _refused(snap, _cfg("full_name", **extra))
    assert "'full_name'" in err.message
    assert "--include-values" in err.message
    assert "allow_real_categories: true" in err.message


def test_the_source_column_is_named_when_the_output_column_is_renamed(build: Build) -> None:
    err = _refused(build(), _cfg("patient", source_column="full_name"))
    assert "'patient'" in err.message and "'full_name'" in err.message


def test_a_missing_policy_beside_redacted_columns_refuses(build: Build) -> None:
    snap = build()
    del snap[VALUE_POLICY_KEY]
    _refused(snap, _cfg("full_name", allow_real_categories=True))
    # A malformed marker refuses any categorical source, listed or not.
    _refused(snap, _cfg("status", allow_real_categories=True))


@pytest.mark.parametrize("policy", ["flagged-redacted/v2", "RAW", "", None, 7, ["raw"], True])
def test_an_unknown_or_ill_typed_policy_refuses(build: Build, policy: Any) -> None:
    snap = build()
    snap[VALUE_POLICY_KEY] = policy
    _refused(snap, _cfg("status", allow_real_categories=True))


@pytest.mark.parametrize(
    "columns", ["full_name", [1, 2], ["full_name", None], {"full_name": 1}, None, 3]
)
def test_ill_typed_redacted_columns_refuse(build: Build, columns: Any) -> None:
    snap = build(include_values=True)
    assert snap[VALUE_POLICY_KEY] == POLICY_RAW
    snap[REDACTED_COLUMNS_KEY] = columns
    _refused(snap, _cfg("status", allow_real_categories=True))


def test_a_policy_with_no_column_list_refuses(build: Build) -> None:
    snap = build()
    del snap[REDACTED_COLUMNS_KEY]
    _refused(snap, _cfg("status", allow_real_categories=True))


def test_a_malformed_marker_refuses_a_conditional_column_too(build: Build) -> None:
    snap = build()
    snap[VALUE_POLICY_KEY] = "nonsense"
    _refused(snap, _cfg("diagnosis", condition_on="status", allow_real_categories=True))


def test_condition_on_a_redacted_side_is_refused(build: Build) -> None:
    snap = build()
    assert "full_name" in snap[REDACTED_COLUMNS_KEY] and "status" not in snap[REDACTED_COLUMNS_KEY]
    err = _refused(snap, _cfg("status", condition_on="full_name", allow_real_categories=True))
    assert "'full_name'" in err.message


class _Name:
    def __str__(self) -> str:
        return "full_name"


def test_a_non_string_condition_on_that_names_a_redacted_column_is_refused(build: Build) -> None:
    snap = build()
    err = _refused(snap, _cfg("status", condition_on=_Name(), allow_real_categories=True))
    assert "'full_name'" in err.message


def test_a_redacted_hc5_column_gets_this_code_not_the_hc5_one(build: Build) -> None:
    snap = build(high_cardinality_columns=["full_name"])
    assert snap["columns"]["full_name"]["stats"]["high_cardinality"] is True
    _refused(snap, _cfg("full_name", high_cardinality=True, allow_real_categories=True))
    _refused(snap, _cfg("full_name", high_cardinality=True))


# AT-12 loads


def test_redacted_numeric_and_datetime_columns_load_and_sample(build: Build) -> None:
    snap = build()
    for name in ("balance", "admitted"):
        assert name in snap[REDACTED_COLUMNS_KEY]
        spec = load_spec(_cfg(name), snapshot=snap)
        assert len(sample_column(spec, 20, col_seed=5)) == 20


@pytest.mark.parametrize("policy", ["nonsense", None])
def test_a_malformed_marker_does_not_stop_numeric_or_datetime_columns(
    build: Build, policy: Any
) -> None:
    snap = build()
    snap[VALUE_POLICY_KEY] = policy
    load_spec(_cfg("balance"), snapshot=snap)
    load_spec(_cfg("admitted"), snapshot=snap)


def test_a_raw_policy_snapshot_loads_a_listed_column_with_consent(build: Build) -> None:
    snap = build(include_values=True)
    assert snap[VALUE_POLICY_KEY] == POLICY_RAW and "full_name" in snap[REDACTED_COLUMNS_KEY]
    assert _values(load_spec(_cfg("full_name", allow_real_categories=True), snapshot=snap)) == set(
        _NAMES
    )
    load_spec(_cfg("status", condition_on="full_name", allow_real_categories=True), snapshot=snap)


def test_raw_only_removes_this_refusal_and_never_grants_consent(build: Build) -> None:
    snap = build(include_values=True)
    with pytest.raises(StatisticalSpecError) as exc:
        load_spec(_cfg("full_name"), snapshot=snap)
    assert exc.value.code == "statistical_real_categories_not_allowed"


def test_a_non_sensitive_categorical_column_of_a_redacted_document_loads(build: Build) -> None:
    spec = load_spec(_cfg("status", allow_real_categories=True), snapshot=build())
    assert _values(spec) == {"open", "closed", "hold"}


def test_a_conditional_pair_with_no_redacted_side_loads(build: Build) -> None:
    snap = build()
    spec = load_spec(
        _cfg("diagnosis", condition_on="status", allow_real_categories=True), snapshot=snap
    )
    assert spec.joint is not None and spec.joint["cells"]


def test_a_markerless_legacy_snapshot_with_a_real_redacted_category_loads() -> None:
    frame = _frame([REDACTED, "Dmitri Vasquez", "Ingrid Halvorsen"])
    snap = compute_distribution_snapshot(frame, joint_columns=[("full_name", "status")])
    assert VALUE_POLICY_KEY not in snap and REDACTED_COLUMNS_KEY not in snap
    spec = load_spec(_cfg("full_name", allow_real_categories=True), snapshot=snap)
    assert REDACTED in _values(spec)
    load_spec(_cfg("status", condition_on="full_name", allow_real_categories=True), snapshot=snap)
    with pytest.raises(StatisticalSpecError) as exc:
        load_spec(_cfg("full_name"), snapshot=snap)
    assert exc.value.code == "statistical_real_categories_not_allowed"


def test_a_raw_policy_snapshot_with_a_real_redacted_category_loads(build: Build) -> None:
    snap = build(_frame([REDACTED, "Dmitri Vasquez", "Ingrid Halvorsen"]), include_values=True)
    assert snap[VALUE_POLICY_KEY] == POLICY_RAW
    spec = load_spec(_cfg("full_name", allow_real_categories=True), snapshot=snap)
    assert REDACTED in _values(spec)


# AT-12: the same refusal through the plan compiler (validate)


def _plan_cfg(snapshot_file: str, *columns: dict[str, Any]) -> dict[str, Any]:
    return {
        "global_settings": {"seed": 42},
        "tables": [
            {
                "name": "t",
                "row_count": 10,
                "generate_columns": [
                    {**c, "type": "statistical", "snapshot_file": snapshot_file} for c in columns
                ],
            }
        ],
    }


def _write(tmp_path: Path, snap: dict[str, Any]) -> str:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


def test_validate_reports_the_same_refusal_code(build: Build, tmp_path: Path) -> None:
    path = _write(tmp_path, build())
    cfg = _plan_cfg(path, {"name": "full_name", "allow_real_categories": True})
    with pytest.raises(PlanCompileError) as exc:
        check_statistical_columns(cfg)
    assert exc.value.code == CODE
    assert exc.value.path == "tables.t.generate_columns.full_name"
    ok = _plan_cfg(path, {"name": "balance"}, {"name": "admitted"})
    assert len(check_statistical_columns(ok)) == 2


# AT-15


def test_the_fidelity_gate_reports_a_redacted_numeric_column_as_not_comparable(
    build: Build, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.generation import _fidelity_gate

    path = _write(tmp_path, build())
    seen: list[dict[str, Any]] = []
    real = _fidelity_gate.compute_fidelity

    def spy(source: Any, generated: Any) -> dict[str, Any]:
        report: dict[str, Any] = real(source, generated)
        seen.append(report)
        return report

    monkeypatch.setattr(_fidelity_gate, "compute_fidelity", spy)
    out = compile_and_generate(_plan_cfg(path, {"name": "balance"}))
    assert out["t"].num_rows == 10
    (report,) = seen
    (entry,) = [c for c in report["marginal"]["columns"] if c["column"] == "balance"]
    assert entry["comparable"] is False
    assert entry["method"] == "no_quantiles"
    assert entry["similarity"] is None
