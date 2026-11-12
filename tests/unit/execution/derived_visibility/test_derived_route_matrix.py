"""T4: the derived Case A shape on every route (plan 2026-10-31-slice0-derived-visibility).

The two-table FK config of `_transform_testkit.fk_config` with a derived child column
`q2 = _1 * 2`, where `first name` (undeclared) sits at position 1 and `_1` is declared: on
main the positional alias `_1` named `first name`, so the published value was wrong.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_pipeline
from decoy_engine.reason_adapter import reason_from_exception
from tests._keyed_secret import THROWAWAY_SECRET
from tests.unit.execution._transform_testkit import ENGINE_VERSION, fk_config

_N = 6


def _tables() -> tuple[pa.Table, pa.Table]:
    parent = pa.table({"id": [f"p{i}" for i in range(_N)], "note": ["n"] * _N})
    child = pa.Table.from_arrays(
        [
            pa.array([f"c{i}" for i in range(_N)]),
            pa.array([999.0 + i for i in range(_N)]),  # `first name`, undeclared
            pa.array([float(i) for i in range(_N)]),  # `_1`, declared
            pa.array([f"p{i}" for i in range(_N)]),
            pa.array([0.0] * _N),
        ],
        names=["cid", "first name", "_1", "parent_id", "q2"],
    )
    return parent, child


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    parent, child = _tables()
    cfg = fk_config(tmp_path, parent, child)
    cfg["tables"][1]["columns"].extend(
        [
            {"name": "_1", "strategy": "passthrough"},
            {"name": "q2", "strategy": "derived", "provider_config": {"expression": "_1 * 2"}},
        ]
    )
    monkeypatch.setenv("DECOY_TEST_THROWAWAY_SECRET", THROWAWAY_SECRET.hex())
    cfg["global_settings"]["mask_secret_ref"] = "env:DECOY_TEST_THROWAWAY_SECRET"
    return cfg


_EXPECTED = [float(2 * i) for i in range(_N)]


@pytest.mark.parametrize("route", ["full_frame", "sequential"])
def test_case_a_publishes_the_declared_column(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FAIL-BEFORE (main publishes the undeclared column's values, 1998.0, 2000.0, ...).
    parent, child = _tables()
    result = run_pipeline(
        _config(tmp_path, monkeypatch),
        {"parent": parent, "child": child},
        engine_version=ENGINE_VERSION,
        execution_mode=route,
    )
    assert result.outputs["child"].column("q2").to_pylist() == _EXPECTED


def test_auto_routes_derived_to_sequential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CHARACTERIZATION: out-of-core declines derived, so auto falls to sequential.
    parent, child = _tables()
    result = run_pipeline(
        _config(tmp_path, monkeypatch),
        {"parent": parent, "child": child},
        engine_version=ENGINE_VERSION,
    )
    assert "sequential_hash" in result.quality_metrics
    assert "out_of_core_hash" not in result.quality_metrics


def test_forced_out_of_core_refuses_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CHARACTERIZATION.
    from decoy_engine.errors import ConfigError

    parent, child = _tables()
    with pytest.raises(ConfigError) as exc:
        run_pipeline(
            _config(tmp_path, monkeypatch),
            {"parent": parent, "child": child},
            engine_version=ENGINE_VERSION,
            execution_mode="out_of_core",
        )
    assert reason_from_exception(exc.value).code == "out_of_core_ineligible"
    assert "out_of_core_dynamic_output_type_unsupported" in str(exc.value)


@pytest.mark.parametrize("entry", ["run_mask_pipeline_chunked", "run_mask_chunked"])
def test_chunked_entries_refuse_derived_at_admission(entry: str) -> None:
    # CHARACTERIZATION: both refusals exist on main and must not change.
    import decoy_engine
    from decoy_engine.plan._errors import PlanCompileError
    from tests._declared_schema import chunked_entry_kwargs
    from tests.native._chunked_entry_support import key_provider, make_config

    cfg = make_config(
        [
            {"name": "_1", "strategy": "passthrough"},
            {"name": "q2", "strategy": "derived", "provider_config": {"expression": "_1 * 2"}},
        ]
    )
    chunk = pa.table({"first name": ["9"], "_1": ["1"], "q2": ["0"]})
    fn = getattr(decoy_engine, entry)
    with pytest.raises(PlanCompileError) as exc:
        list(
            fn(
                cfg,
                iter([chunk]),
                table="t",
                engine_version="t4",
                key_provider=key_provider(),
                **chunked_entry_kwargs(fn, cfg, "t"),
            )
        )
    assert exc.value.code == "strategy_not_chunk_safe"
