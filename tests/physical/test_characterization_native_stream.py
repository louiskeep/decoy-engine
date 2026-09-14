"""D3 characterization: `native_stream` driver adapter vs `try_native_route`
called directly -- the admitted case, a resident sink, and a declined
`(None, report)` reroute (plan C1: decline is a first-class outcome, not an
error the adapter reacts to).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._native_route_exec import try_native_route
from decoy_engine.execution._pipeline import classify_table_kinds
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.execution.physical.drivers import NativeStreamAdapter
from decoy_engine.profile._readers import LazySource
from tests.physical._helpers import build_job


def _table_specs(strategy: str = "redact") -> list[dict[str, Any]]:
    if strategy == "redact":
        column: dict[str, Any] = {"name": "note", "strategy": "redact"}
    else:
        column = {"name": "note", "strategy": strategy, "namespace": "ns"}
    return [{"name": "t", "columns": [column]}]


def _source() -> pa.Table:
    return pa.table({"note": pa.array(["s1", None, "s3"], type=pa.string())})


def _call_kwargs(job: Any, source_path: Path, *, sink: Any = None) -> dict[str, Any]:
    table_kinds = classify_table_kinds(job.config)
    return dict(
        config=job.config,
        plan=job.plan,
        table_kinds=table_kinds,
        caller_sources={"t": LazySource(path=source_path)},
        source_loader=None,
        sink=sink,
        fidelity_report=False,
        execution_mode="auto",
        graph=job.graph,
        resolved_substrate="pandas",
    )


def test_native_stream_adapter_admitted_byte_identical(tmp_path: Path) -> None:
    job = build_job(tmp_path, {"t": _source()}, _table_specs())
    source_path = Path(job.config["sources"]["t"]["path"])

    direct_result, direct_report = try_native_route(**_call_kwargs(job, source_path))
    via_result, via_report = NativeStreamAdapter().run(**_call_kwargs(job, source_path))

    assert direct_report.admitted is True, direct_report.reason
    assert via_report.admitted == direct_report.admitted
    assert direct_result is not None and via_result is not None
    assert direct_result.outputs["t"].to_pydict() == via_result.outputs["t"].to_pydict()
    assert direct_result.warnings == via_result.warnings


def test_native_stream_adapter_sink_write_identical(tmp_path: Path) -> None:
    job = build_job(tmp_path, {"t": _source()}, _table_specs())
    source_path = Path(job.config["sources"]["t"]["path"])
    direct_sink_dir = tmp_path / "direct_sink"
    via_sink_dir = tmp_path / "via_sink"

    direct_result, direct_report = try_native_route(
        **_call_kwargs(job, source_path, sink=ParquetTransactionalSink(direct_sink_dir))
    )
    via_result, via_report = NativeStreamAdapter().run(
        **_call_kwargs(job, source_path, sink=ParquetTransactionalSink(via_sink_dir))
    )

    assert direct_report.admitted is True and via_report.admitted is True
    assert direct_result is not None and via_result is not None
    assert direct_result.outputs == {} and via_result.outputs == {}
    direct_written = pq.read_table(direct_sink_dir / "t.parquet")
    via_written = pq.read_table(via_sink_dir / "t.parquet")
    assert direct_written.to_pydict() == via_written.to_pydict()


def test_native_stream_adapter_declines_unsupported_strategy_identically(tmp_path: Path) -> None:
    """`hash` is not in `ALLOWED_STRATEGIES`; both calls must decline with the
    same `(None, report)` shape and the same coded reason."""
    job = build_job(tmp_path, {"t": _source()}, _table_specs(strategy="hash"))
    source_path = Path(job.config["sources"]["t"]["path"])

    direct_result, direct_report = try_native_route(**_call_kwargs(job, source_path))
    via_result, via_report = NativeStreamAdapter().run(**_call_kwargs(job, source_path))

    assert direct_result is None and via_result is None
    assert direct_report.admitted is False and via_report.admitted is False
    assert direct_report.reason == via_report.reason
    assert direct_report.reason is not None and "unsupported_strategy" in direct_report.reason


def test_native_stream_adapter_scope_bookkeeping(tmp_path: Path) -> None:
    job = build_job(tmp_path, {"t": _source()}, _table_specs())
    source_path = Path(job.config["sources"]["t"]["path"])
    adapter = NativeStreamAdapter()
    assert adapter.last_invocation is None
    adapter.run(**_call_kwargs(job, source_path))
    assert adapter.last_invocation is not None
    assert adapter.last_invocation.tables == ("t",)
