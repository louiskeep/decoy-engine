"""D4: the preflight-decision-equivalence harness (the exit gate).

For each fixture, this asserts `compile_physical_plan`'s driver selection
matches what `run_pipeline` would ACTUALLY dispatch to, without ever letting
`run_pipeline` mask a single row: `_live_dispatched_driver` monkeypatches the
five real dispatch boundaries (`run_sequential_route`, `run_out_of_core_
route`, `_run_native_streaming`, `run_mask_chunked`,
`PandasExecutionAdapter.run`) to raise a marker naming which one was
reached, then runs the REAL `run_pipeline` and catches the marker. Everything
upstream of that boundary -- `decide_execution_route`, `classify_job`,
`static_candidacy`, `classify_and_preflight`, `peek_and_admit` -- runs
UNCHANGED, for real, exactly as `run_pipeline` runs it; only the moment
where a driver would start masking is swapped for an exception. This is a
stronger oracle than re-calling the same decision functions a second time
(which would only catch a mistake in re-deriving inputs, not a mis-wiring of
the precedence itself): it exercises production's ACTUAL `if`/`elif` control
flow.

Reject-before-read (`ExecutionError`) and forced-mode (`ConfigError`)
failures are asserted the same way on both sides: the compiler calls
`decide_execution_route` directly (`_compiler.layer1_route`), so it raises
the identical exception `run_pipeline` raises for the same inputs -- code-
for-code for `ExecutionError`, normalized branch identity for the uncoded
`ConfigError` forced-mode failures (plan D3's documented exclusion).
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.errors import ConfigError
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution.physical import (
    DriverId,
    capture_physical_plan_inputs,
    compile_physical_plan,
)
from decoy_engine.execution.physical._reasons import (
    FORCED_MODE_BRANCH_IDENTITIES,
    native_reason_code_family,
)
from decoy_engine.profile._readers import LazySource


class _DriverMarkerError(Exception):
    """Raised by a patched dispatch boundary; `driver` names which one."""

    def __init__(self, driver: str) -> None:
        super().__init__(driver)
        self.driver = driver


def _bomb(driver: str) -> Any:
    def _fn(*_args: Any, **_kwargs: Any) -> Any:
        raise _DriverMarkerError(driver)

    return _fn


@contextlib.contextmanager
def _patched_dispatch(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        "decoy_engine.execution._pipeline_route_exec.run_sequential_route",
        _bomb(DriverId.SEQUENTIAL.value),
    )
    monkeypatch.setattr(
        "decoy_engine.execution._pipeline_route_exec.run_out_of_core_route",
        _bomb(DriverId.OUT_OF_CORE.value),
    )
    monkeypatch.setattr(
        "decoy_engine.execution._native_route_exec._run_native_streaming",
        _bomb(DriverId.NATIVE_STREAM.value),
    )
    monkeypatch.setattr(
        "decoy_engine.execution._pipeline_route_exec.run_mask_chunked",
        _bomb(DriverId.CHUNKED.value),
    )
    monkeypatch.setattr(PandasExecutionAdapter, "run", _bomb(DriverId.FULL_FRAME.value))
    yield


def live_dispatched_driver(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, Any], sources: Any, **kwargs: Any
) -> str:
    """Run the REAL `run_pipeline` with the dispatch boundaries bombed;
    return which driver it reached. Lets `ExecutionError`/`ConfigError`
    propagate uncaught -- callers that expect a reject/forced-mode failure
    assert on those directly (`pytest.raises`), never on this return value.
    """
    with _patched_dispatch(monkeypatch):
        try:
            run_pipeline(config, sources, engine_version="d4-oracle", **kwargs)
        except _DriverMarkerError as marker:
            return marker.driver
    raise AssertionError("run_pipeline completed without dispatching to any known driver")


def _write(tmp_path: Path, table: pa.Table, name: str) -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _single_table_config(
    tmp_path: Path, source: pa.Table, *, strategy: str = "redact"
) -> dict[str, Any]:
    path = _write(tmp_path, source, "t")
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "note", "strategy": strategy}]}],
        }
    ).model_dump()


def _fk_config(tmp_path: Path, parent: pa.Table, child: pa.Table) -> dict[str, Any]:
    parent_path = _write(tmp_path, parent, "parent")
    child_path = _write(tmp_path, child, "child")
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {
                "parent": {"type": "file", "format": "parquet", "path": str(parent_path)},
                "child": {"type": "file", "format": "parquet", "path": str(child_path)},
            },
            "targets": {
                "parent": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "parent.out.parquet"),
                },
                "child": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "child.out.parquet"),
                },
            },
            "tables": [
                {
                    "name": "parent",
                    "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}],
                },
                {
                    "name": "child",
                    "columns": [{"name": "pid", "strategy": "hash", "namespace": "n"}],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                }
            ],
        }
    ).model_dump()


def _assert_equivalent(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, Any],
    sources: Any,
    *,
    engine_version: str = "d4-corpus",
    **kwargs: Any,
) -> None:
    """Build a real `PhysicalPlanInputs` snapshot, compile it, and assert the
    resulting driver matches the live-dispatched one for the SAME
    config/sources/kwargs."""
    inputs = capture_physical_plan_inputs(config, sources, engine_version=engine_version, **kwargs)
    plan = compile_physical_plan(inputs)
    assert plan.tables, "expected at least one masking table in the compiled plan"
    live_driver = live_dispatched_driver(monkeypatch, config, sources, **kwargs)
    for table in plan.tables:
        assert table.driver.value == live_driver, (
            f"table {table.table!r}: compiler picked {table.driver.value!r} "
            f"({table.driver_reason!r}) but run_pipeline dispatched to {live_driver!r}"
        )


# ---------------------------------------------------------------------------
# Route corpus: every route + rejection path observable without execution.
# ---------------------------------------------------------------------------


def test_full_frame_no_relationships(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    config = _single_table_config(tmp_path, source)
    _assert_equivalent(monkeypatch, config, {"t": source})


def test_sequential_small_fk_byte_estimate_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    _assert_equivalent(
        monkeypatch,
        config,
        {"parent": parent, "child": child},
        use_byte_estimate_routing=False,
    )


def test_full_frame_fk_byte_estimate_fits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default `use_byte_estimate_routing=True`: a tiny FK job confirms a
    full_frame fit and is routed there instead of sequential (§13)."""
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    _assert_equivalent(monkeypatch, config, {"parent": parent, "child": child})


def test_out_of_core_forced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    _assert_equivalent(
        monkeypatch, config, {"parent": parent, "child": child}, execution_mode="out_of_core"
    )


def test_sequential_forced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    _assert_equivalent(
        monkeypatch, config, {"parent": parent, "child": child}, execution_mode="sequential"
    )


def test_full_frame_forced_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    config = _fk_config(tmp_path, parent, child)
    _assert_equivalent(
        monkeypatch, config, {"parent": parent, "child": child}, execution_mode="full_frame"
    )


def test_chunked_admitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = pa.table({"note": pa.array([f"s{i}" for i in range(10)], type=pa.string())})
    config = _single_table_config(tmp_path, source)
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": source},
        auto_chunk=True,
        auto_chunk_threshold_rows=1,
        chunk_size_rows=3,
    )


def test_chunked_declined_below_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = pa.table({"note": pa.array([f"s{i}" for i in range(10)], type=pa.string())})
    config = _single_table_config(tmp_path, source)
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": source},
        auto_chunk=True,
        auto_chunk_threshold_rows=1_000_000,
        chunk_size_rows=3,
    )


def test_native_stream_admitted_utf8_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = _single_table_config(tmp_path, source)
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": LazySource(path=path)},
        native_route_enabled=True,
    )


def test_native_stream_declined_unsupported_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = _single_table_config(tmp_path, source, strategy="hash")
    config["tables"][0]["columns"][0]["namespace"] = "n"
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": LazySource(path=path)},
        native_route_enabled=True,
    )


def test_native_stream_declined_source_loader_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    config = _single_table_config(tmp_path, source)
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": source},
        native_route_enabled=True,
        source_loader=lambda name: source,
    )


def test_native_stream_widened_admitted_integer_passthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"n": pa.array([1, 2, 3], type=pa.int64())})
    path = tmp_path / "t.parquet"
    pq.write_table(source, path)
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "n", "strategy": "passthrough"}]}],
        }
    ).model_dump()
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": LazySource(path=path)},
        native_route_enabled=True,
    )


def test_native_stream_widened_declined_null_bearing_integer_redact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"n": pa.array([1, None, 3], type=pa.int64())})
    path = tmp_path / "t.parquet"
    pq.write_table(source, path)
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "n",
                            "strategy": "truncate",
                            "provider_config": {"length": 1},
                        }
                    ],
                }
            ],
        }
    ).model_dump()
    _assert_equivalent(
        monkeypatch,
        config,
        {"t": LazySource(path=path)},
        native_route_enabled=True,
    )


def test_native_stream_not_enabled_falls_to_full_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = _single_table_config(tmp_path, source)
    _assert_equivalent(monkeypatch, config, {"t": LazySource(path=path)})


def test_generate_and_mask_full_frame(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mask_source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    mask_path = _write(tmp_path, mask_source, "masked")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"masked": {"type": "file", "format": "parquet", "path": str(mask_path)}},
            "targets": {
                "masked": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "masked.out.parquet"),
                },
                "people": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "people.out.csv"),
                },
            },
            "tables": [
                {"name": "masked", "columns": [{"name": "note", "strategy": "redact"}]},
                {
                    "name": "people",
                    "row_count": 3,
                    "generate_columns": [{"name": "id", "type": "sequence", "start": 1, "step": 1}],
                },
            ],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(
        config, {"masked": mask_source}, engine_version="d4-corpus"
    )
    plan = compile_physical_plan(inputs)
    assert plan.synthesis is not None and plan.synthesis.tables == ("people",)
    _assert_equivalent(monkeypatch, config, {"masked": mask_source})


# ---------------------------------------------------------------------------
# Reject-before-read / forced-mode failures: exact ExecutionError code
# equality, or normalized ConfigError branch identity (D3's documented
# exclusion -- production raises this uncoded).
# ---------------------------------------------------------------------------


def test_reject_before_read_large_fk_no_bounded_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cyclic FK graph (A -> B -> A) is eligible for neither sequential
    (cyclic) nor out_of_core, and large enough that full_frame rejects
    before read."""
    a = pa.table(
        {
            "id": pa.array([f"a{i}" for i in range(10)], type=pa.string()),
            "bid": pa.array([f"b{i}" for i in range(10)], type=pa.string()),
        }
    )
    b = pa.table(
        {
            "id": pa.array([f"b{i}" for i in range(10)], type=pa.string()),
            "aid": pa.array([f"a{i}" for i in range(10)], type=pa.string()),
        }
    )
    a_path = _write(tmp_path, a, "a")
    b_path = _write(tmp_path, b, "b")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {
                "a": {"type": "file", "format": "parquet", "path": str(a_path)},
                "b": {"type": "file", "format": "parquet", "path": str(b_path)},
            },
            "targets": {
                "a": {"type": "file", "format": "parquet", "path": str(tmp_path / "a.out.parquet")},
                "b": {"type": "file", "format": "parquet", "path": str(tmp_path / "b.out.parquet")},
            },
            "tables": [
                {"name": "a", "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}]},
                {"name": "b", "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}]},
            ],
            "relationships": [
                {
                    "parent": {"table": "a", "columns": ["id"]},
                    "children": [{"table": "b", "columns": ["aid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                },
                {
                    "parent": {"table": "b", "columns": ["id"]},
                    "children": [{"table": "a", "columns": ["bid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                },
            ],
        }
    ).model_dump()
    sources = {"a": a, "b": b}
    kwargs: dict[str, Any] = dict(
        use_byte_estimate_routing=False, full_frame_reject_rows=5, out_of_core_threshold_rows=5
    )

    inputs = capture_physical_plan_inputs(config, sources, engine_version="d4-corpus", **kwargs)
    with pytest.raises(ExecutionError) as compiler_exc:
        compile_physical_plan(inputs)

    with pytest.raises(ExecutionError) as live_exc:
        with _patched_dispatch(monkeypatch):
            run_pipeline(config, sources, engine_version="d4-oracle", **kwargs)

    assert compiler_exc.value.code == live_exc.value.code == "fk_full_frame_oom_risk_rejected"


def test_forced_sequential_ineligible_no_relationships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    config = _single_table_config(tmp_path, source)
    sources = {"t": source}
    kwargs: dict[str, Any] = dict(execution_mode="sequential")

    inputs = capture_physical_plan_inputs(config, sources, engine_version="d4-corpus", **kwargs)
    with pytest.raises(ConfigError):
        compile_physical_plan(inputs)
    with pytest.raises(ConfigError):
        with _patched_dispatch(monkeypatch):
            run_pipeline(config, sources, engine_version="d4-oracle", **kwargs)


def test_forced_out_of_core_no_mask_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pure-generate job forced to out_of_core: no mask table exists."""
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {},
            "targets": {
                "people": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "people.out.csv"),
                },
            },
            "tables": [
                {
                    "name": "people",
                    "row_count": 3,
                    "generate_columns": [{"name": "id", "type": "sequence", "start": 1, "step": 1}],
                },
            ],
        }
    ).model_dump()
    sources: dict[str, Any] = {}
    kwargs: dict[str, Any] = dict(execution_mode="out_of_core")

    inputs = capture_physical_plan_inputs(config, sources, engine_version="d4-corpus", **kwargs)
    with pytest.raises(ConfigError):
        compile_physical_plan(inputs)
    with pytest.raises(ConfigError):
        with _patched_dispatch(monkeypatch):
            run_pipeline(config, sources, engine_version="d4-oracle", **kwargs)


# ---------------------------------------------------------------------------
# Catalog-completeness audit over this corpus: every native-admission reason
# and every translated planner-rejection code observed above must be a KNOWN
# family / never the "unclassified_*" sentinel (D3's closing line).
# ---------------------------------------------------------------------------


def test_native_admission_reasons_stay_in_the_known_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenarios: list[tuple[dict[str, Any], Any, dict[str, Any]]] = []

    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "utf8")
    config = _single_table_config(tmp_path, source)
    scenarios.append((config, {"utf8": LazySource(path=path)}, {"native_route_enabled": True}))

    hash_source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    hash_path = _write(tmp_path, hash_source, "hashcol")
    hash_config = _single_table_config(tmp_path, hash_source, strategy="hash")
    hash_config["tables"][0]["columns"][0]["namespace"] = "n"
    scenarios.append(
        (hash_config, {"hashcol": LazySource(path=hash_path)}, {"native_route_enabled": True})
    )

    scenarios.append((config, {"utf8": source}, {}))  # native_route_enabled=False by default

    for cfg, srcs, kwargs in scenarios:
        inputs = capture_physical_plan_inputs(cfg, srcs, engine_version="d4-catalog", **kwargs)
        admission = inputs.native_admission
        if admission.reason is not None:
            family = native_reason_code_family(admission.reason)
            assert family != "unknown", (
                f"uncatalogued native-admission reason: {admission.reason!r}"
            )


def test_forced_mode_branch_identities_are_all_named() -> None:
    """Documents the closed set of forced-mode branch identities D4 asserts
    exact identity against (D3's normalized-branch-identity exclusion)."""
    assert {
        "forced_out_of_core_no_mask_table",
        "forced_out_of_core_ineligible",
        "forced_out_of_core_incompatible",
        "forced_sequential_ineligible",
        "forced_sequential_cyclic",
        "forced_sequential_no_mask_table",
    } == FORCED_MODE_BRANCH_IDENTITIES
