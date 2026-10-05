"""Unified-slice per-column route evidence, end to end and at the coordinator seam.

Plan: `docs/plans/2026-10-05-unified-route-evidence.md` rev 2.1, section 5.

Every admitted node's published evidence carries `planned_backend`,
`executed_backend`, `calls` and `elapsed_ms` next to the old three keys. These
tests pin the exact dict per operator on the production lane (with the oracle
poisoned so a silent decline cannot pass), the idle rules, the monotonic
accumulation of the compiled-kernel flag across batches, and `calls`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import _pandas_adapter, run_pipeline
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.execution.native._chunked_evidence import (
    ARROW_PYTHON,
    RUST_COMPANION,
    RUST_POOL_SELECT,
)
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._plan import ExecutionBinding, KeyBinding
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence, run_operator
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.instrumentation.timing import TimingCollector, use_collector
from decoy_engine.keyprovider import SecretKeyProvider
from tests.physical._shadow_helpers import build_config, write_read_only_fixture

ENGINE_VERSION = "unified-route-evidence-test"
_MASK_KEY = bytes(range(32))

NEEDS_COMPANION = pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled decoy-engine-native companion unavailable",
)

EVIDENCE_KEYS = {
    "operator",
    "executed",
    "compiled_kernel_executed",
    "planned_backend",
    "executed_backend",
    "calls",
    "elapsed_ms",
}


def _kp() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


# One column spec per admitted operator: (column dict, planned backend, compiled when populated).
_HASH = {"name": "h", "strategy": "hash", "namespace": "ns_h"}
_FAKER = {
    "name": "f",
    "strategy": "faker",
    "provider": "person_first_name",
    "deterministic": True,
    "namespace": "ns_f",
    "pool_size": 30,
}
_CATEGORICAL = {
    "name": "k",
    "strategy": "categorical",
    "namespace": "ns_k",
    "deterministic": True,
    "provider_config": {"categories": ["red", "green", "blue"]},
}
_BUCKET = {
    "name": "b",
    "strategy": "bucket_perturb",
    "namespace": "ns_b",
    "provider_config": {"bucket": "month", "date_format": "%Y-%m-%d"},
}
_DATE_SHIFT = {
    "name": "d",
    "strategy": "date_shift",
    "namespace": "ns_d",
    "provider_config": {"date_format": "%Y-%m-%d"},
}
_GROUP_BY = {"name": "gb", "strategy": "passthrough"}
_GROUP_KEY = {
    "name": "gk",
    "strategy": "group_key",
    "provider_config": {"group_by": "gb", "length": 16},
}
_REDACT = {"name": "r", "strategy": "redact"}
_TRUNCATE = {"name": "t", "strategy": "truncate", "provider_config": {"length": 3}}
_PASSTHROUGH = {"name": "p", "strategy": "passthrough"}

# operator id -> (columns the table needs, node column, planned, compiled when populated)
OPERATORS: dict[str, tuple[list[dict[str, Any]], str, str, bool]] = {
    "native_keyed_hash": ([_HASH], "h", RUST_COMPANION, True),
    "native_faker_select": ([_FAKER], "f", RUST_POOL_SELECT, True),
    "native_categorical": ([_CATEGORICAL], "k", RUST_COMPANION, True),
    "native_bucket_perturb": ([_BUCKET], "b", RUST_COMPANION, True),
    "native_date_shift": ([_DATE_SHIFT], "d", RUST_COMPANION, True),
    "native_group_key": ([_GROUP_BY, _GROUP_KEY], "gk", RUST_COMPANION, True),
    "native_redact": ([_REDACT], "r", ARROW_PYTHON, False),
    "native_truncate": ([_TRUNCATE], "t", ARROW_PYTHON, False),
    "native_passthrough": ([_PASSTHROUGH], "p", ARROW_PYTHON, False),
}

_VALUES: dict[str, list[Any]] = {
    "h": ["a@x.com", "b@x.com", "c@x.com", "d@x.com", "e@x.com"],
    "f": [f"src_{i % 3}" for i in range(5)],
    "k": ["red", "green", "blue", "red", "green"],
    "b": ["2024-02-29", "2023-02-28", "2024-01-01", "2024-12-31", "2024-03-31"],
    "d": ["2024-05-15", "2023-08-20", "2021-01-10", "2024-11-30", "2022-02-02"],
    "gb": ["g1", "g2", "g1", "g2", "g3"],
    "gk": ["seed"] * 5,
    "r": ["one", "two", "three", "four", "five"],
    "t": ["12345", "6789", "1111", "2222", "3333"],
    "p": ["x1", "x2", "x3", "x4", "x5"],
}


def _table_for(columns: list[dict[str, Any]], n_rows: int | None = None) -> pa.Table:
    data = {}
    for col in columns:
        values = _VALUES[col["name"]]
        if n_rows is not None:
            values = [values[i % len(values)] for i in range(n_rows)]
        data[col["name"]] = pa.array(values, type=pa.string())
    return pa.table(data)


@contextmanager
def _oracle_poisoned(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def _poisoned(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the legacy pandas adapter ran on an admitted lane run")

    with monkeypatch.context() as m:
        m.setattr(_pandas_adapter.PandasExecutionAdapter, "run", _poisoned)
        yield


def _run(
    tmp_path: Path,
    source: pa.Table,
    columns: list[dict[str, Any]],
    *,
    lane: bool,
) -> ExecutionResult:
    path = write_read_only_fixture(tmp_path / ("on" if lane else "off"), source, "fixture")
    config = build_config(tmp_path, "t", path, columns)
    return run_pipeline(
        config,
        {"t": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_kp(),
        unified_slice_enabled=lane,
    )


def lane_nodes(
    tmp_path: Path,
    source: pa.Table,
    columns: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, dict[str, Any]]:
    """Run the production lane with the oracle poisoned, assert byte-identical
    output against a separate lane-off run, return the evidence nodes by column."""
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    off = _run(tmp_path, source, columns, lane=False)
    with _oracle_poisoned(monkeypatch):
        on = _run(tmp_path, source, columns, lane=True)
    assert off.outputs["t"].schema.equals(on.outputs["t"].schema, check_metadata=True)
    assert off.outputs["t"].to_pylist() == on.outputs["t"].to_pylist()
    leaf = on.quality_metrics[QUALITY_METRICS_KEY]
    by_column: dict[str, dict[str, Any]] = {}
    for node_id, ev in leaf["nodes"].items():
        by_column[node_id.split(":")[-1] if ":" in node_id else node_id] = ev
    return by_column


def _evidence_for(nodes: dict[str, dict[str, Any]], operator: str) -> dict[str, Any]:
    found = [ev for ev in nodes.values() if ev["operator"] == operator]
    assert len(found) == 1, f"expected one {operator} node, got {sorted(nodes)}"
    return found[0]


def assert_exact_evidence(
    ev: dict[str, Any],
    *,
    operator: str,
    compiled: bool,
    planned: str,
    executed: str,
    calls: int,
) -> None:
    assert set(ev) == EVIDENCE_KEYS
    elapsed = ev["elapsed_ms"]
    assert isinstance(elapsed, float) and elapsed >= 0.0
    assert {k: v for k, v in ev.items() if k != "elapsed_ms"} == {
        "operator": operator,
        "executed": True,
        "compiled_kernel_executed": compiled,
        "planned_backend": planned,
        "executed_backend": executed,
        "calls": calls,
    }


# ---------------------------------------------------------------------------
# 1. Exact evidence per operator, and a mixed table.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("operator", list(OPERATORS))
def test_exact_evidence_per_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operator: str
) -> None:
    columns, _col, planned, compiled = OPERATORS[operator]
    nodes = lane_nodes(tmp_path, _table_for(columns), columns, monkeypatch)
    ev = _evidence_for(nodes, operator)
    assert_exact_evidence(
        ev,
        operator=operator,
        compiled=compiled,
        planned=planned,
        executed=planned,
        calls=1,
    )


@NEEDS_COMPANION
def test_exact_evidence_for_a_mixed_table_holding_every_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns: list[dict[str, Any]] = []
    for operator, (cols, _c, _p, _k) in OPERATORS.items():
        if operator == "native_passthrough":
            continue
        columns.extend(c for c in cols if c not in columns)
    columns.append(_PASSTHROUGH)
    nodes = lane_nodes(tmp_path, _table_for(columns), columns, monkeypatch)
    # `gb` is itself an admitted passthrough node, so passthrough appears twice.
    assert len(nodes) == len(columns)
    for operator, (_cols, _col, planned, compiled) in OPERATORS.items():
        matching = [ev for ev in nodes.values() if ev["operator"] == operator]
        assert matching, operator
        for ev in matching:
            assert_exact_evidence(
                ev,
                operator=operator,
                compiled=compiled,
                planned=planned,
                executed=planned,
                calls=1,
            )


# ---------------------------------------------------------------------------
# 2. Idle rules on the production lane.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize(
    "operator", ["native_bucket_perturb", "native_date_shift", "native_group_key"]
)
def test_empty_table_is_idle_for_value_dependent_kernels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operator: str
) -> None:
    columns, _col, _planned, _compiled = OPERATORS[operator]
    nodes = lane_nodes(tmp_path, _table_for(columns, 0), columns, monkeypatch)
    assert_exact_evidence(
        _evidence_for(nodes, operator),
        operator=operator,
        compiled=False,
        planned=RUST_COMPANION,
        executed=ARROW_PYTHON,
        calls=1,
    )


@NEEDS_COMPANION
@pytest.mark.parametrize("operator", ["native_bucket_perturb", "native_date_shift"])
def test_all_null_is_idle_for_bucket_perturb_and_date_shift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operator: str
) -> None:
    columns, col, _planned, _compiled = OPERATORS[operator]
    source = pa.table({col: pa.array([None, None, None], type=pa.string())})
    nodes = lane_nodes(tmp_path, source, columns, monkeypatch)
    assert_exact_evidence(
        _evidence_for(nodes, operator),
        operator=operator,
        compiled=False,
        planned=RUST_COMPANION,
        executed=ARROW_PYTHON,
        calls=1,
    )


@NEEDS_COMPANION
def test_all_null_group_key_sibling_still_runs_the_compiled_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns = OPERATORS["native_group_key"][0]
    source = pa.table(
        {
            "gb": pa.array([None, None, None], type=pa.string()),
            "gk": pa.array(["seed"] * 3, type=pa.string()),
        }
    )
    nodes = lane_nodes(tmp_path, source, columns, monkeypatch)
    assert_exact_evidence(
        _evidence_for(nodes, "native_group_key"),
        operator="native_group_key",
        compiled=True,
        planned=RUST_COMPANION,
        executed=RUST_COMPANION,
        calls=1,
    )


@NEEDS_COMPANION
@pytest.mark.parametrize(
    "operator", ["native_keyed_hash", "native_categorical", "native_faker_select"]
)
def test_empty_table_hash_categorical_faker_keep_their_planned_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operator: str
) -> None:
    columns, _col, planned, _compiled = OPERATORS[operator]
    nodes = lane_nodes(tmp_path, _table_for(columns, 0), columns, monkeypatch)
    assert_exact_evidence(
        _evidence_for(nodes, operator),
        operator=operator,
        compiled=True,
        planned=planned,
        executed=planned,
        calls=1,
    )


# ---------------------------------------------------------------------------
# Coordinator seam: evidence assembled before finalize, with real timings.
# ---------------------------------------------------------------------------


def seam_run(
    config: dict[str, Any], source: pa.Table, *, batch_size_rows: int
) -> tuple[Any, Any, list[Any]]:
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version=ENGINE_VERSION)
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=_kp(), batch_size_rows=batch_size_rows
    )
    snapshot = capture_shadow_snapshot({"t": source})
    collector = TimingCollector()
    with use_collector(collector):
        shadow = ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)
    return plan.tables[0], shadow, list(collector.records)


def seam_evidence(
    tmp_path: Path, columns: list[dict[str, Any]], source: pa.Table, *, batch_size_rows: int
) -> dict[str, dict[str, Any]]:
    from decoy_engine.execution._unified_slice_evidence import assemble_node_evidence

    path = write_read_only_fixture(tmp_path, source, "seam")
    config = build_config(tmp_path, "t", path, columns)
    table, shadow, timings = seam_run(config, source, batch_size_rows=batch_size_rows)
    return assemble_node_evidence(table.nodes, shadow.route_evidence, timings)


def _single(nodes: dict[str, dict[str, Any]], operator: str) -> dict[str, Any]:
    return _evidence_for(nodes, operator)


@NEEDS_COMPANION
def test_all_unparseable_date_shift_is_idle_at_the_seam(tmp_path: Path) -> None:
    source = pa.table({"d": pa.array(["bad-1", "bad-2", "bad-3"], type=pa.string())})
    nodes = seam_evidence(tmp_path, [_DATE_SHIFT], source, batch_size_rows=2)
    assert_exact_evidence(
        _single(nodes, "native_date_shift"),
        operator="native_date_shift",
        compiled=False,
        planned=RUST_COMPANION,
        executed=ARROW_PYTHON,
        calls=2,
    )


@NEEDS_COMPANION
def test_all_unparseable_date_shift_keeps_the_production_row_error_failure(
    tmp_path: Path,
) -> None:
    """Existing behavior (plan fact 6): finalize raises, the lane reroutes, and both
    arms fail with the same records. The oracle is NOT poisoned here."""
    source = pa.table({"d": pa.array(["bad-1", "bad-2", "bad-3"], type=pa.string())})
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    with pytest.raises(RowErrorsFailedError) as off_exc:
        _run(tmp_path, source, [_DATE_SHIFT], lane=False)
    with pytest.raises(RowErrorsFailedError) as on_exc:
        _run(tmp_path, source, [_DATE_SHIFT], lane=True)
    assert tuple(on_exc.value.records) == tuple(off_exc.value.records)


# ---------------------------------------------------------------------------
# 2b. Batch accumulation (3c).
# ---------------------------------------------------------------------------

_VALID_DATES = ["2024-05-15", "2023-08-20"]
_BATCH_SEQUENCES: dict[str, list[Any]] = {
    "valued_then_idle": [*_VALID_DATES, None, None],
    "idle_then_valued": [None, None, *_VALID_DATES],
    "all_idle": [None, None, None, None],
}
_EXPECT_COMPILED = {"valued_then_idle": True, "idle_then_valued": True, "all_idle": False}


@NEEDS_COMPANION
@pytest.mark.parametrize("operator", ["native_bucket_perturb", "native_date_shift"])
@pytest.mark.parametrize("shape", list(_BATCH_SEQUENCES))
def test_compiled_flag_accumulates_across_batches(
    tmp_path: Path, operator: str, shape: str
) -> None:
    columns, col, _planned, _compiled = OPERATORS[operator]
    source = pa.table({col: pa.array(_BATCH_SEQUENCES[shape], type=pa.string())})
    nodes = seam_evidence(tmp_path, columns, source, batch_size_rows=2)
    compiled = _EXPECT_COMPILED[shape]
    assert_exact_evidence(
        _single(nodes, operator),
        operator=operator,
        compiled=compiled,
        planned=RUST_COMPANION,
        executed=RUST_COMPANION if compiled else ARROW_PYTHON,
        calls=2,
    )


@NEEDS_COMPANION
@pytest.mark.parametrize("operator", ["native_bucket_perturb", "native_date_shift"])
def test_valid_dates_then_one_null_row_stay_compiled_on_the_production_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operator: str
) -> None:
    columns, col, _planned, _compiled = OPERATORS[operator]
    values: list[Any] = ["2024-05-15"] * 50_000 + [None]
    source = pa.table({col: pa.array(values, type=pa.string())})
    nodes = lane_nodes(tmp_path, source, columns, monkeypatch)
    assert_exact_evidence(
        _single(nodes, operator),
        operator=operator,
        compiled=True,
        planned=RUST_COMPANION,
        executed=RUST_COMPANION,
        calls=2,
    )


def _group_key_binding() -> ExecutionBinding:
    return ExecutionBinding(
        operator_id="native_group_key",
        operator_reason="test",
        resolved_config=(),
        input_schema=pa.schema([pa.field("gb", pa.string())]),
        output_schema=pa.schema([pa.field("gk", pa.string())]),
        determinism_family="source_keyed_hmac",
        determinism_version=1,
        key_binding=KeyBinding(key_source="mask_key", namespace="group_key/gk"),
        diagnostic_obligations=(),
        required_prepasses=(),
        batch_estimate=None,
        group_key_group_by="gb",
        group_key_length=16,
        group_key_prefix="",
    )


def _run_group_key(evidence: OperatorCallEvidence, sibling_values: list[Any]) -> None:
    sibling = pa.table({"gb": pa.array(sibling_values, type=pa.string())})
    ctx = SimpleNamespace(mask_key=_MASK_KEY, native_threads=None)
    run_operator(
        sibling.column("gb"),
        binding=_group_key_binding(),
        ctx=ctx,  # type: ignore[arg-type]
        evidence=evidence,
        group_key_sibling=sibling,
    )


@NEEDS_COMPANION
@pytest.mark.parametrize(
    "first, second",
    [(["a", "b"], []), ([], ["a", "b"])],
    ids=["populated_then_empty", "empty_then_populated"],
)
def test_group_key_flag_is_monotonic_across_run_operator_calls(
    first: list[Any], second: list[Any]
) -> None:
    evidence = OperatorCallEvidence(planned_operator="native_group_key")
    _run_group_key(evidence, first)
    _run_group_key(evidence, second)
    assert evidence.compiled_kernel_executed is True
    assert evidence.batches_run == 2


# ---------------------------------------------------------------------------
# 3. `calls`.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("operator", list(OPERATORS))
def test_calls_counts_batch_invocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operator: str
) -> None:
    columns, _col, _planned, _compiled = OPERATORS[operator]
    nodes = lane_nodes(tmp_path, _table_for(columns, 50_001), columns, monkeypatch)
    assert _evidence_for(nodes, operator)["calls"] == 2


@NEEDS_COMPANION
@pytest.mark.parametrize("operator", list(OPERATORS))
def test_one_row_table_has_one_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operator: str
) -> None:
    columns, _col, _planned, _compiled = OPERATORS[operator]
    nodes = lane_nodes(tmp_path, _table_for(columns, 1), columns, monkeypatch)
    assert _evidence_for(nodes, operator)["calls"] == 1
