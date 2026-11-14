"""AT-15 and AT-17: one cell per route proves the gate ran, stamped `keys`, and read no whole table.

`GATE_TESTS` in `tests/sentry/post_scan/_route_coverage.py` names `test_gate_cell` for every route
in `ROUTES` plus `stream` and `isolated`, so a route added without a cell fails the sentry. The
spies count only inside the gate's key and column-suite reads: the route itself may read what it
likes. All values are made up.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import run_pipeline_isolated
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.execution.pipeline import _post_scan
from decoy_engine.internal.memory_limit import require_megabytes
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _subset_run_support as subset_sup
from tests.unit.execution.post_scan.test_wiring import _config, _mixed_run, _run
from tests.unit.execution.run_stream import _support as rs
from tests.unit.execution.test_auto_chunk_routing import _single_column_job
from tests.unit.execution.test_lazy_path_route_admission import _parent_child_tables

Cell = Callable[[Path], tuple[Any, str]]
V = "ps12-cells"


@pytest.fixture(autouse=True)
def _subset_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(subset_sup.VAR, "ab" * 32)


def _full_frame(tmp: Path) -> tuple[Any, str]:
    result, routes = _run(_config(tmp), "full_frame")
    return result, routes[0]


def _staged(route: str) -> Cell:
    def run(tmp: Path) -> tuple[Any, str]:
        sink = ParquetTransactionalSink(tmp / "published")
        result, routes = _run(_config(tmp, integrity=True), route, sink=sink)
        return result, routes[0]

    return run


def _chunked(tmp: Path) -> tuple[Any, str]:
    cfg, lazy = rs.single_table_job(tmp, rs.two_column_table(30))
    cfg["post_scan"] = {"integrity": True}
    sink, _ = rs.engine_sink(tmp)
    result = run_pipeline_with(cfg, {support.TABLE: lazy}, sink)
    return result, result.quality_metrics["auto_chunk"]["mode"]


def run_pipeline_with(cfg: dict[str, Any], sources: dict[str, Any], sink: Any) -> Any:
    from decoy_engine.execution import run_pipeline

    return run_pipeline(
        cfg,
        sources=sources,
        sink=sink,
        engine_version=V,
        auto_chunk_threshold_rows=1,
        chunk_size_rows=7,
    )


def _mixed(tmp: Path) -> tuple[Any, str]:
    from tests.parity._f2_mixed_support import COMPANION_PRESENT

    if not COMPANION_PRESENT:
        pytest.skip("decoy-engine-native companion not installed")
    result, routes = _mixed_run(tmp, sink=ParquetTransactionalSink(tmp / "published"))
    return result, "mixed" if "mixed" in routes else routes[0]


def _unified(tmp: Path) -> tuple[Any, str]:
    if not support.COMPANION_PRESENT:
        pytest.skip("decoy-engine-native companion not installed")
    cfg, sources = _single_column_job(tmp, "hash")
    cfg["post_scan"] = {"integrity": True}
    result = run_pipeline_with(cfg, sources, None)
    taken = "unified_slice_activation" in result.quality_metrics
    return result, "unified_slice" if taken else "full_frame"


def _stream(tmp: Path) -> tuple[Any, str]:
    cfg, lazy, _ = rs.fk_job(tmp)
    sink, _ = rs.engine_sink(tmp)
    result = rs.run_stream(cfg, lazy, sink, chunk_size_rows=7)
    return result, "stream"


def _isolated(tmp: Path) -> tuple[Any, str]:
    parent, child = _parent_child_tables()
    out = run_pipeline_isolated(
        copy.deepcopy(_config(tmp)),
        {"parent": parent, "child": child},
        isolate=False,
        output_dir=tmp / "committed",
        engine_version=V,
        execution_mode="full_frame",
    )
    assert out.outcome == "completed", (out.error, out.reason)
    return out, "isolated"  # the child ran `run_pipeline`; its report rides the envelope


CELLS: dict[str, tuple[Cell, bool]] = {
    # route name -> (cell, whether the cell declares keys, so the report must list them)
    "full_frame": (_full_frame, True),
    "sequential": (_staged("sequential"), True),
    "chunked": (_chunked, False),
    "out_of_core": (_staged("out_of_core"), True),
    "mixed": (_mixed, True),
    "unified_slice": (_unified, False),
    "stream": (_stream, True),
    "isolated": (_isolated, True),
}


def _watch_whole_reads(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    inside: list[str] = []
    hits: list[str] = []
    limits: list[str] = []

    def trap(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def spy(*args: Any, **kwargs: Any) -> Any:
            if inside:
                hits.append(name)
            return real(*args, **kwargs)

        return spy

    def scoped(name: str) -> None:
        real = getattr(_post_scan, name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            inside.append(name)
            limits.append(kwargs.get("memory_limit", "0MB"))
            try:
                return real(*args, **kwargs)
            finally:
                inside.pop()

        monkeypatch.setattr(_post_scan, name, wrapper)

    monkeypatch.setattr(pq, "read_table", trap("read_table", pq.read_table))
    monkeypatch.setattr(pq.ParquetFile, "read", trap("ParquetFile.read", pq.ParquetFile.read))
    monkeypatch.setattr(
        _post_scan, "materialize_source", trap("materialize_source", _post_scan.materialize_source)
    )
    scoped("check_keys")
    return hits, limits


@pytest.mark.parametrize("route", CELLS)
def test_gate_cell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    cell, declares_keys = CELLS[route]
    hits, limits = _watch_whole_reads(monkeypatch)
    result, took = cell(tmp_path)
    if route not in ("chunked",):
        assert took == route, took
    metrics = result.quality_metrics
    assert "post_scan_gate_deferred" not in metrics
    report = metrics["post_scan_integrity"]
    assert "keys" in report and report["verdict"]["blocking"] is False
    if declares_keys:
        assert report["keys"], route
    assert hits == []
    assert all(require_megabytes(m) <= 512 for m in limits)
