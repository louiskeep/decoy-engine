"""R3 acceptance test 6: the out-of-core external-reorder lane is live through the public pipeline.

Plan: docs/plans/2026-10-10-r3-run-context-route-executors.md section 5.3 and section 6, test 6;
evidence for `docs/records/2026-10-10-ooc-reorder-liveness.md`.

A real `run_pipeline` call with an eligible FK shape, a sink, a deterministic memory budget, a
fixed disk reading, acceptable fan-in and a deduplicated, null-filtered parent-key count at or
over the (overridable) threshold must execute `_stream_driver.stream_table` for the child table
and must NOT build the batch joiner for it. The same job must keep the batch route in each
negative case: default threshold, no sink, over-wide payload, unresolvable memory budget and
unresolvable disk reading. Spies wrap the real functions, so every run completes and its sink
output is compared with the batch route's.
"""

from __future__ import annotations

import dataclasses
import shutil
from collections import namedtuple
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink, run_pipeline
from decoy_engine.execution import _pipeline_route_exec as route_exec
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core import _runner
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._reorder_budget import resolve_reorder_budgets
from decoy_engine.execution.out_of_core._route_policy import REORDER_PARENT_KEY_THRESHOLD
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner

pytestmark = pytest.mark.filterwarnings("ignore")

BUDGET = 1024 * 1024 * 1024
_Usage = namedtuple("_Usage", "total used free")
FREE_DISK = 200 * 1024 * 1024 * 1024


def _write(tmp: Path, table: pa.Table, name: str) -> str:
    path = tmp / f"{name}.parquet"
    pq.write_table(table, path)
    return str(path)


def fk_job(
    tmp: Path,
    *,
    parent_ids: list[str | None],
    child_refs: list[str],
    key_strategy: str = "hash",
) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    tables = {
        "parent": pa.table({"id": pa.array(parent_ids, type=pa.string())}),
        "child": pa.table(
            {
                "cid": pa.array([f"c{i}" for i in range(len(child_refs))]),
                "parent_id": pa.array(child_refs),
            }
        ),
    }
    hash_ = lambda c, ns: {"name": c, "strategy": "hash", "namespace": ns}
    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {
            n: {"type": "file", "path": _write(tmp, t, n), "format": "parquet"}
            for n, t in tables.items()
        },
        "targets": {
            n: {"type": "file", "format": "parquet", "path": str(tmp / f"{n}.out.parquet")}
            for n in tables
        },
        "tables": [
            {"name": "parent", "columns": [hash_("id", "ns")]},
            {"name": "child", "columns": [hash_("cid", "cns"), hash_("parent_id", "ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }
    return cfg, tables


def standard_job(tmp: Path, n: int = 20) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    ids = [f"p{i}" for i in range(n)]
    return fk_job(tmp, parent_ids=ids, child_refs=ids)


class Spy:
    """Records which route each table took and which joiner classes were built."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reorder_tables: list[str] = []
        self.batch_tables: list[str] = []
        self.batch_joiners = 0
        self.reorder_joiners = 0
        real_stream, real_batch = _runner.stream_table, _runner._stream_table

        def stream(**kw: Any) -> Any:
            self.reorder_tables.append(kw["table_name"])
            return real_stream(**kw)

        def batch(**kw: Any) -> Any:
            self.batch_tables.append(kw["table_name"])
            return real_batch(**kw)

        monkeypatch.setattr(_runner, "stream_table", stream)
        monkeypatch.setattr(_runner, "_stream_table", batch)
        init_b, init_r = ChildFkBatchJoiner.__init__, StreamFkJoiner.__init__

        def b_init(this: Any, *a: Any, **k: Any) -> None:
            self.batch_joiners += 1
            init_b(this, *a, **k)

        def r_init(this: Any, *a: Any, **k: Any) -> None:
            self.reorder_joiners += 1
            init_r(this, *a, **k)

        monkeypatch.setattr(ChildFkBatchJoiner, "__init__", b_init)
        monkeypatch.setattr(StreamFkJoiner, "__init__", r_init)


class _ShutilProxy:
    """The real `shutil`, except `disk_usage` returns a fixed reading (or fails), and only as
    seen from `_pipeline_route_exec`, so no other module's disk check is disturbed."""

    def __init__(self, free: int | None) -> None:
        self._free = free

    def disk_usage(self, path: Any) -> Any:
        if self._free is None:
            raise OSError("disk usage unavailable")
        return _Usage(self._free * 2, self._free, self._free)

    def __getattr__(self, name: str) -> Any:
        return getattr(shutil, name)


def fixed_disk(monkeypatch: pytest.MonkeyPatch, free: int | None = FREE_DISK) -> None:
    monkeypatch.setattr(route_exec, "shutil", _ShutilProxy(free))


def run(
    tmp: Path, cfg: dict[str, Any], tables: dict[str, pa.Table], *, sink: bool = True, **kw: Any
) -> tuple[Any, dict[str, dict[str, list[Any]]]]:
    out = tmp / "sink_out"
    kwargs: dict[str, Any] = {
        "execution_mode": "out_of_core",
        "out_of_core_budget_bytes": BUDGET,
        "engine_version": "r3-reorder",
    }
    kwargs.update(kw)
    result = run_pipeline(
        cfg, tables, sink=ParquetTransactionalSink(out) if sink else None, **kwargs
    )
    if not sink:
        return result, {t: tbl.to_pydict() for t, tbl in result.outputs.items()}
    return result, {p.stem: pq.read_table(p).to_pydict() for p in sorted(out.glob("*.parquet"))}


def _batch_reference(tmp: Path, cfg: Any, tables: Any, **kw: Any) -> dict[str, Any]:
    (tmp / "ref").mkdir()
    return run(tmp / "ref", cfg, tables, **kw)[1]


def test_ooc_reorder_lane_reachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard_job(tmp_path)
    spy = Spy(monkeypatch)
    # 20 distinct parent keys against a threshold of 10: over it.
    result, got = run(tmp_path, cfg, tables, out_of_core_reorder_threshold_rows=10)
    assert result.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    assert spy.reorder_tables == ["child"]  # the real stream driver ran for the child
    assert spy.batch_tables == ["parent"]  # the root table never reorders
    assert spy.reorder_joiners >= 1
    assert spy.batch_joiners == 0  # the child did NOT use the batch joiner
    # Route choice changes timing and memory, never output.
    monkeypatch.undo()
    fixed_disk(monkeypatch)
    ref = _batch_reference(tmp_path, cfg, tables)
    assert got == ref and set(got) == {"parent", "child"}


def test_default_threshold_keeps_the_batch_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard_job(tmp_path)
    spy = Spy(monkeypatch)
    run(tmp_path, cfg, tables)
    assert REORDER_PARENT_KEY_THRESHOLD == 2_000_000
    assert spy.reorder_tables == [] and spy.reorder_joiners == 0
    assert sorted(spy.batch_tables) == ["child", "parent"] and spy.batch_joiners >= 1


@pytest.mark.parametrize(("threshold", "reorders"), [(19, True), (20, True), (21, False)])
def test_threshold_boundary_is_at_or_over_the_distinct_key_count(
    threshold: int, reorders: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard_job(tmp_path, n=20)
    spy = Spy(monkeypatch)
    run(tmp_path, cfg, tables, out_of_core_reorder_threshold_rows=threshold)
    assert (spy.reorder_tables == ["child"]) is reorders


@pytest.mark.parametrize(("threshold", "reorders"), [(12, True), (13, False)])
def test_the_decision_key_is_deduplicated_null_filtered_parent_keys(
    threshold: int, reorders: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """20 parent rows but only 12 distinct non-null keys: 12 decides, not 20."""
    fixed_disk(monkeypatch)
    distinct = [f"p{i}" for i in range(12)]
    parent_ids: list[str | None] = [*distinct, *distinct[:4], None, None, None, None]
    cfg, tables = fk_job(tmp_path, parent_ids=parent_ids, child_refs=distinct)
    spy = Spy(monkeypatch)
    run(tmp_path, cfg, tables, out_of_core_reorder_threshold_rows=threshold)
    assert (spy.reorder_tables == ["child"]) is reorders


def test_no_sink_keeps_the_batch_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_disk(monkeypatch)
    cfg, tables = standard_job(tmp_path)
    spy = Spy(monkeypatch)
    run(tmp_path, cfg, tables, sink=False, out_of_core_reorder_threshold_rows=0)
    assert spy.reorder_tables == [] and spy.reorder_joiners == 0


def _widen_relations(monkeypatch: pytest.MonkeyPatch, row_bytes: int) -> None:
    """Report every parent-key relation as `row_bytes` wide at the route decision. The pipeline's
    own masked keys are narrow (the real wide-key shapes are covered at the policy level in
    `test_route_policy_wide_key_fallback.py`), so the width is injected where the decision reads it."""
    real = _runner.decide_route

    def decide(edges: Any, relations: Any, **kw: Any) -> Any:
        wide = {
            e: dataclasses.replace(r, max_sort_payload_row_bytes=row_bytes)
            for e, r in relations.items()
        }
        return real(edges, wide, **kw)

    monkeypatch.setattr(_runner, "decide_route", decide)


def test_an_over_wide_payload_keeps_the_batch_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_disk(monkeypatch)
    cap = resolve_reorder_budgets(BUDGET, FREE_DISK).run_bytes_cap // (2 * 16)
    cfg, tables = standard_job(tmp_path)
    _widen_relations(monkeypatch, cap)  # at the cap: falls back
    spy = Spy(monkeypatch)
    run(tmp_path, cfg, tables, out_of_core_reorder_threshold_rows=0)
    assert spy.reorder_tables == [] and spy.reorder_joiners == 0
    assert sorted(spy.batch_tables) == ["child", "parent"]


def test_one_byte_under_the_cap_still_reorders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anti-vacuity for the over-wide case: only the injected width differs."""
    fixed_disk(monkeypatch)
    cap = resolve_reorder_budgets(BUDGET, FREE_DISK).run_bytes_cap // (2 * 16)
    cfg, tables = standard_job(tmp_path)
    _widen_relations(monkeypatch, cap - 1)
    spy = Spy(monkeypatch)
    run(tmp_path, cfg, tables, out_of_core_reorder_threshold_rows=0)
    assert spy.reorder_tables == ["child"]


def test_an_unresolvable_memory_budget_keeps_the_batch_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_disk(monkeypatch)

    def no_ram(*a: Any, **k: Any) -> Any:
        raise ExecutionError(code="out_of_core_memory_detection_failed", message="no ram reading")

    monkeypatch.setattr("decoy_engine.execution.out_of_core.resolve_ooc_memory_limit", no_ram)
    cfg, tables = standard_job(tmp_path)
    spy = Spy(monkeypatch)
    run(tmp_path, cfg, tables, out_of_core_budget_bytes=None, out_of_core_reorder_threshold_rows=0)
    assert spy.reorder_tables == [] and spy.reorder_joiners == 0


def test_an_unresolvable_disk_reading_keeps_the_batch_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed_disk(monkeypatch, free=None)
    cfg, tables = standard_job(tmp_path)
    spy = Spy(monkeypatch)
    run(tmp_path, cfg, tables, out_of_core_reorder_threshold_rows=0)
    assert spy.reorder_tables == [] and spy.reorder_joiners == 0
