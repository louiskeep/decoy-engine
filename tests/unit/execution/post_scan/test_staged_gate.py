"""AT-15, AT-16, AT-20, AT-27, AT-28 and AT-29: the gate reads a staged sink back before commit.

A route that streams its tables into a `ParquetTransactionalSink` is judged from the staged files
at commit: a blocking verdict commits nothing, an unreadable sink keeps the run resident, and the
inputs are re-read in batches under the bind-time identity check. All values are made up.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import reasons
from decoy_engine.config import post_scan_resident_reason
from decoy_engine.execution import _transactional_sink
from decoy_engine.execution._chunked_output_sink import OutputPublish
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.execution.pipeline import _post_scan
from decoy_engine.execution.pipeline._post_scan import PostScanGate
from decoy_engine.internal.memory_limit import require_megabytes
from decoy_engine.verification.postmask import PostScanBlockedError, from_staged
from tests.unit.execution import _subset_run_support as subset_sup
from tests.unit.execution._b6a_support import RecordingSink
from tests.unit.execution.post_scan.test_wiring import (
    DUPLICATES,
    _blocked,
    _config,
    _mixed_run,
    _run,
)

STAGED_ROUTES = [pytest.param("sequential", id="sequential"), pytest.param("out_of_core", id="ooc")]
ORPHANS = "post_scan.foreign_key_orphans_introduced"
READABLE = "decoy_engine.execution._transactional_sink.ParquetTransactionalSink"


@pytest.fixture(autouse=True)
def _subset_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(subset_sup.VAR, "ab" * 32)


def _published(out: Path) -> list[str]:
    return sorted(p.name for p in out.iterdir()) if out.exists() else []


def _no_stage_left(root: Path) -> bool:
    return not list(root.glob("_decoy_stage_*"))


# ----- AT-16: a blocking verdict commits nothing ------------------------------------------------


@pytest.mark.parametrize("route", STAGED_ROUTES)
def test_at16_a_blocking_verdict_on_a_staged_sink_commits_nothing(
    tmp_path: Path, route: str
) -> None:
    out = tmp_path / "published"
    exc = _blocked(
        lambda: _run(_config(tmp_path, broken=True), route, sink=ParquetTransactionalSink(out))
    )
    assert exc.reason == DUPLICATES
    assert not out.exists()
    assert _no_stage_left(tmp_path)
    # The unbroken job takes this route and publishes. A route that never ran would pass above
    # for the wrong reason.
    control = tmp_path / "control"
    result, routes = _run(_config(tmp_path), route, sink=ParquetTransactionalSink(control))
    assert routes == [route]
    assert _published(control) == ["child.parquet", "parent.parquet"]
    assert result.quality_metrics["post_scan_integrity"]["verdict"]["blocking"] is False


def test_at16_the_gate_speaks_before_the_sink_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real_gate = PostScanGate.before_commit
    real_commit = ParquetTransactionalSink.commit

    def gate(self: PostScanGate, sink: Any) -> None:
        order.append("gate")
        real_gate(self, sink)

    def commit(self: ParquetTransactionalSink) -> None:
        order.append("commit")
        real_commit(self)

    monkeypatch.setattr(PostScanGate, "before_commit", gate)
    monkeypatch.setattr(ParquetTransactionalSink, "commit", commit)
    _run(_config(tmp_path), "sequential", sink=ParquetTransactionalSink(tmp_path / "o"))
    assert order == ["gate", "commit"]


@contextmanager
def _bound(gate: PostScanGate) -> Iterator[PostScanGate]:
    token = _post_scan._ACTIVE.set(gate)
    try:
        yield gate
    finally:
        _post_scan._ACTIVE.reset(token)


def test_at16_a_streamed_publish_session_is_gated_at_its_commit(tmp_path: Path) -> None:
    """The full-frame streamed lane and the mixed route commit through `OutputPublish`."""
    out = tmp_path / "published"
    sink = ParquetTransactionalSink(out)
    rel = {
        "parent": {"table": "parent", "columns": ["id"]},
        "children": [{"table": "child", "columns": ["pid"]}],
        "orphan_policy": "preserve",
    }
    sources = {"parent": pa.table({"id": ["a", "b", "c"]}), "child": pa.table({"pid": ["a"]})}
    kinds = {"parent": "mask", "child": "mask"}
    gate = PostScanGate({"relationships": [rel]}, sources=sources, table_kinds=kinds)
    publish = OutputPublish(sink, True, False)
    publish.open()
    sink.write("parent", pa.table({"id": ["a", "a", "b"]}))
    sink.write("child", pa.table({"pid": ["a"]}))
    with _bound(gate), pytest.raises(PostScanBlockedError) as caught:
        publish.commit()
    assert caught.value.reason == DUPLICATES
    publish.__exit__(PostScanBlockedError, caught.value, None)
    assert not out.exists() and _no_stage_left(tmp_path)


# ----- AT-20: once, with the report on the result -----------------------------------------------


@pytest.mark.parametrize("route", STAGED_ROUTES)
def test_at20_a_staged_run_is_gated_once_and_carries_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    calls: list[int] = []
    real = _post_scan.check_keys

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(_post_scan, "check_keys", counted)
    result, routes = _run(
        _config(tmp_path), route, sink=ParquetTransactionalSink(tmp_path / "published")
    )
    assert routes == [route] and len(calls) == 1
    assert "post_scan_gate_deferred" not in result.quality_metrics
    report = result.quality_metrics["post_scan_integrity"]
    assert report["verdict"] == {"blocking": False, "codes": [], "warnings": []}
    assert {(k["table"], k["kind"]) for k in report["keys"]} == {
        ("parent", "primary"),
        ("child", "foreign"),
    }


def test_at20_an_orphan_the_run_introduced_blocks_a_staged_run(tmp_path: Path) -> None:
    config = _config(tmp_path)
    child = next(t for t in config["tables"] if t["name"] == "child")
    # A foreign key hashed outside the parent's namespace no longer matches any parent key.
    child["columns"] = [
        {**c, "namespace": "elsewhere"} if c["name"] == "parent_id" else c for c in child["columns"]
    ]
    out = tmp_path / "published"
    exc = _blocked(lambda: _run(config, "sequential", sink=ParquetTransactionalSink(out)))
    assert exc.reason == ORPHANS
    assert not out.exists()


def test_at20_a_mixed_job_with_a_staged_sink_is_gated(tmp_path: Path) -> None:
    from tests.parity._f2_mixed_support import COMPANION_PRESENT

    if not COMPANION_PRESENT:
        pytest.skip("decoy-engine-native companion not installed")
    out = tmp_path / "published"
    result, routes = _mixed_run(tmp_path, sink=ParquetTransactionalSink(out))
    assert "mixed" in routes
    assert "post_scan_gate_deferred" not in result.quality_metrics
    assert result.quality_metrics["post_scan_integrity"]["verdict"]["blocking"] is False
    assert _published(out)


# ----- AT-15: the gate never reads a whole table ------------------------------------------------


def _forbid_whole_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spies that count only while the gate runs: the route itself may read what it likes."""
    inside: list[str] = []
    hits: list[str] = []

    def trap(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def spy(*args: Any, **kwargs: Any) -> Any:
            if inside:
                hits.append(name)
            return real(*args, **kwargs)

        return spy

    monkeypatch.setattr(pq, "read_table", trap("read_table", pq.read_table))
    monkeypatch.setattr(pq.ParquetFile, "read", trap("ParquetFile.read", pq.ParquetFile.read))
    monkeypatch.setattr(
        _post_scan, "materialize_source", trap("materialize_source", _post_scan.materialize_source)
    )
    real = PostScanGate.before_commit

    def scoped(self: PostScanGate, sink: Any) -> None:
        inside.append("gate")
        try:
            real(self, sink)
        finally:
            inside.pop()

    monkeypatch.setattr(PostScanGate, "before_commit", scoped)
    return hits


@pytest.mark.parametrize("route", STAGED_ROUTES)
def test_at15_the_gate_reads_staged_output_by_row_group_never_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    hits = _forbid_whole_reads(monkeypatch)
    limits: list[str] = []
    real = _post_scan.check_keys

    def seen(*args: Any, **kwargs: Any) -> Any:
        limits.append(kwargs["memory_limit"])
        return real(*args, **kwargs)

    monkeypatch.setattr(_post_scan, "check_keys", seen)
    _run(_config(tmp_path), route, sink=ParquetTransactionalSink(tmp_path / "published"))
    assert hits == []
    assert limits and all(require_megabytes(m) <= 512 for m in limits)


def test_at15_a_staged_read_holds_a_few_batches_not_the_file(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    rows, batch = 40_000, 1_000
    table = pa.table({"id": pa.array(range(rows), pa.int64()), "v": pa.array(["x" * 8] * rows)})
    pq.write_table(table, path, row_group_size=batch)

    staged = from_staged("t", lambda: os.open(path, os.O_RDONLY))
    assert staged.num_rows == rows and staged.schema.names == ["id", "v"]
    base = pa.total_allocated_bytes()
    peak = 0
    seen = 0
    for chunk in staged.read(["id"], batch):
        seen += chunk.num_rows
        peak = max(peak, pa.total_allocated_bytes() - base)
    assert seen == rows
    # A handful of 8-byte-key batches, never the 40 row groups of the file.
    assert peak < 4 * batch * 8 * 4, peak


# ----- AT-27: inputs are re-read under the bind-time identity check -----------------------------


def _rewrite_before_the_gate(monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]) -> None:
    real = PostScanGate.before_commit

    def changed(self: PostScanGate, sink: Any) -> None:
        change()
        real(self, sink)

    monkeypatch.setattr(PostScanGate, "before_commit", changed)


@pytest.mark.parametrize("fmt", ["parquet", "csv"])
def test_at27_an_input_rewritten_before_the_gate_reads_it_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    cfg, data, lazy = subset_sup.job(tmp_path, fmt=fmt)
    cfg["subset"] = None
    path = Path(cfg["sources"]["customers"]["path"])

    def change() -> None:
        shorter = data["customers"].slice(0, 7)
        if fmt == "parquet":
            pq.write_table(shorter, path)
        else:
            import pyarrow.csv as pacsv

            pacsv.write_csv(shorter, path)

    _rewrite_before_the_gate(monkeypatch, change)
    out = tmp_path / "published"
    with pytest.raises(Exception) as caught:
        _run(cfg, "sequential", sources=lazy, sink=ParquetTransactionalSink(out))
    assert getattr(caught.value, "code", None) == reasons.runtime.LAZY_SOURCE_CHANGED
    assert not out.exists() and _no_stage_left(tmp_path)


def test_at27_an_unchanged_lazy_input_passes_the_staged_gate(tmp_path: Path) -> None:
    cfg, _, lazy = subset_sup.job(tmp_path)
    cfg["subset"] = None
    result, _ = _run(
        cfg, "sequential", sources=lazy, sink=ParquetTransactionalSink(tmp_path / "published")
    )
    assert result.quality_metrics["post_scan_integrity"]["verdict"]["blocking"] is False
    assert "post_scan_gate_deferred" not in result.quality_metrics


# ----- AT-28: a sink the gate cannot read keeps the run resident --------------------------------


def _callable_sink(seen: list[str]) -> Any:
    return _transactional_sink._CallableSinkAdapter(lambda table, data: seen.append(table))


def test_at28_a_callable_sink_with_keys_stays_resident_and_blocks_before_commit(
    tmp_path: Path,
) -> None:
    seen: list[str] = []
    exc = _blocked(lambda: _run(_config(tmp_path, broken=True), "auto", sink=_callable_sink(seen)))
    assert exc.reason == DUPLICATES
    assert seen == []
    control: list[str] = []
    result, routes = _run(_config(tmp_path), "auto", sink=_callable_sink(control))
    assert routes == ["full_frame"]
    assert result.quality_metrics["execution"]["route_reason"] == (
        reasons.route.POST_SCAN_KEYS_RESIDENT.spec.ref.code
    )
    assert sorted(control) == ["child", "parent"]
    assert "post_scan_gate_deferred" not in result.quality_metrics


def test_at28_a_foreign_sink_class_is_never_read_back(tmp_path: Path) -> None:
    _, routes = _run(_config(tmp_path), "auto", sink=RecordingSink())
    assert routes == ["full_frame"]


def test_at28_the_resident_reason_by_sink_kind() -> None:
    keys = {"relationships": [{"x": 1}]}
    assert post_scan_resident_reason(keys) is None
    assert post_scan_resident_reason(keys, sink_kind=READABLE) is None
    assert post_scan_resident_reason(keys, sink_kind="x.Foreign") is (
        reasons.route.POST_SCAN_KEYS_RESIDENT
    )
    assert post_scan_resident_reason({}, sink_kind="x.Foreign") is None
    both = {**keys, "post_scan": {"integrity": True}}
    assert post_scan_resident_reason(both, sink_kind="x.Foreign") is (
        reasons.route.POST_SCAN_INTEGRITY_REQUESTED
    )


def test_at28_a_commit_the_gate_cannot_read_refuses_instead_of_deferring() -> None:
    sink = RecordingSink()
    gate = PostScanGate({"relationships": [{"x": 1}]})
    with pytest.raises(PostScanBlockedError) as caught:
        gate.before_commit(sink)
    assert caught.value.reason == "post_scan.check_error"
    assert sink.calls == []


# ----- AT-29: a declared cast is applied to the baseline re-read --------------------------------

INT_ASCII = {"id": "integer/ascii/v1", "signed": True, "bit_width": 64}


def _declared(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "passthrough",
        "logical_type": "integer",
        "parse_format": INT_ASCII,
        "error_policy": "fail",
    }


def _cast_job(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    from tests.acceptance._f06_support import column

    parent = pa.table({"id": pa.array(["01", "1", "2", "3"], pa.string())})
    child = pa.table({"cid": pa.array(["a", "b", "c"]), "pid": pa.array(["2", "3", "01"])})
    descriptors = {}
    for name, table in (("parent", parent), ("child", child)):
        path = tmp_path / f"{name}.parquet"
        pq.write_table(table, path)
        descriptors[name] = {"type": "file", "format": "parquet", "path": str(path)}
    cfg = {
        "version": 2,
        "global_settings": {"seed": 7},
        "sources": descriptors,
        "tables": [
            {"name": "parent", "columns": [_declared("id")]},
            {"name": "child", "columns": [column("cid"), _declared("pid")]},
        ],
        "targets": {n: {**d, "path": d["path"] + ".out"} for n, d in descriptors.items()},
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "preserve",
            }
        ],
        "namespaces": {},
    }
    return cfg, {"parent": parent, "child": child}


def _keys_by_table(result: Any) -> dict[str, dict[str, Any]]:
    return {k["table"]: k for k in result.quality_metrics["post_scan_integrity"]["keys"]}


def test_at29_a_cast_key_column_has_the_same_baseline_on_a_streamed_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.acceptance._f06_support import run

    attached: list[Any] = []
    real = PostScanGate.attach_cast_run

    def spy(self: PostScanGate, cast_run: Any) -> None:
        attached.append(cast_run)
        real(self, cast_run)

    monkeypatch.setattr(PostScanGate, "attach_cast_run", spy)
    cfg, data = _cast_job(tmp_path)
    resident = run(cfg, data, execution_mode="full_frame")
    assert attached == []  # the resident route casts its sources itself
    streamed = run(
        cfg,
        data,
        execution_mode="sequential",
        sink=ParquetTransactionalSink(tmp_path / "published"),
    )
    assert streamed.quality_metrics["execution"]["execution_mode"] == "sequential"
    assert len(attached) == 1
    assert "post_scan_gate_deferred" not in streamed.quality_metrics
    want, got = _keys_by_table(resident), _keys_by_table(streamed)
    for table in ("parent", "child"):
        assert got[table]["baseline"] == want[table]["baseline"] == "measured"
        assert got[table]["source"] == want[table]["source"]
    assert got["parent"]["source"]["duplicate_excess"] == 1  # "01" and "1" are one key
    assert streamed.quality_metrics["post_scan_integrity"]["verdict"]["blocking"] is False
