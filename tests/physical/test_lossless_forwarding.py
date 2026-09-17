"""D3 sentinel: every driver adapter forwards its delegate's result UNCHANGED.

Each case stubs the adapter's real delegate to return a MARKED sentinel object
(a value no real delegate could produce -- a fresh unique instance) and
asserts the adapter's `run(...)` returns that EXACT object (`is`, not `==`),
proving the adapter neither re-aggregates, unwraps, copies, nor mutates any
field. It also asserts the delegate was called with the exact arguments the
adapter's caller passed (by identity for at least one mutable argument),
proving no massaging happens on the way in either.

This is the D3 "lossless forwarding" sentinel the plan calls for, done once
per adapter surface (8 total: full_frame, sequential, the 3 chunked surfaces,
native_stream, out_of_core, synthesis).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.physical.drivers import (
    FullFrameAdapter,
    MaskPipelineChunkedAdapter,
    NativeOrOracleChunkedAdapter,
    NativeStreamAdapter,
    OutOfCoreAdapter,
    ResidentChunkedAggregatorAdapter,
    SequentialAdapter,
    SynthesisStageAdapter,
)


class _Sentinel:
    """A unique marker no real delegate would ever construct."""


def test_full_frame_adapter_forwards_result_unchanged() -> None:
    sentinel = _Sentinel()
    calls: list[tuple[Any, ...]] = []

    class _StubAdapter:
        adapter_name = "stub"
        adapter_version = "0"

        def run(self, plan: Any, sources: Any, **kwargs: Any) -> Any:
            calls.append((plan, sources, kwargs))
            return sentinel

        def supports_strategy(self, strategy_name: str) -> bool:
            return True

        def shutdown(self) -> None:
            pass

    stub = _StubAdapter()
    adapter = FullFrameAdapter(stub)
    plan_marker = object()
    sources_marker = {"t": pa.table({"c": [1]})}

    result = adapter.run(
        plan_marker,  # type: ignore[arg-type]
        sources_marker,
        registry=object(),  # type: ignore[arg-type]
        relationship_graph=object(),  # type: ignore[arg-type]
        namespace_registry=object(),  # type: ignore[arg-type]
    )

    assert result is sentinel
    assert len(calls) == 1
    called_plan, called_sources, _ = calls[0]
    assert called_plan is plan_marker
    assert called_sources is sources_marker


def test_sequential_adapter_forwards_result_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = _Sentinel()
    calls: list[dict[str, Any]] = []

    def _stub_run_sequential(adapter: Any, plan: Any, source_loader: Any, **kwargs: Any) -> Any:
        calls.append({"adapter": adapter, "plan": plan, "source_loader": source_loader, **kwargs})
        return sentinel

    monkeypatch.setattr(
        "decoy_engine.execution.physical.drivers._sequential.run_sequential",
        _stub_run_sequential,
    )
    adapter = SequentialAdapter()
    plan_marker = object()
    loader_marker = object()

    result = adapter.run(
        object(),  # type: ignore[arg-type]
        plan_marker,  # type: ignore[arg-type]
        loader_marker,  # type: ignore[arg-type]
        registry=object(),  # type: ignore[arg-type]
        relationship_graph=object(),  # type: ignore[arg-type]
        namespace_registry=object(),  # type: ignore[arg-type]
    )

    assert result is sentinel
    assert calls[0]["plan"] is plan_marker
    assert calls[0]["source_loader"] is loader_marker


def test_mask_pipeline_chunked_adapter_forwards_result_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = _Sentinel()
    calls: list[dict[str, Any]] = []

    def _stub(config: Any, chunks: Any, **kwargs: Any) -> Any:
        calls.append({"config": config, "chunks": chunks, **kwargs})
        return sentinel

    # The adapter imports `run_mask_pipeline_chunked` lazily from `_chunked`
    # inside its `run` method, so the module-level attribute is what must be
    # patched (see `physical/drivers/_chunked.py`'s comment on that choice).
    monkeypatch.setattr("decoy_engine.execution._chunked.run_mask_pipeline_chunked", _stub)
    adapter = MaskPipelineChunkedAdapter()
    chunks_marker = iter([pa.table({"c": [1]})])

    result = adapter.run({"cfg": 1}, chunks_marker, table="t", engine_version="v")

    assert result is sentinel
    assert calls[0]["chunks"] is chunks_marker


def test_native_or_oracle_chunked_adapter_forwards_result_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = _Sentinel()
    calls: list[dict[str, Any]] = []

    def _stub(config: Any, chunks: Any, **kwargs: Any) -> Any:
        calls.append({"config": config, "chunks": chunks, **kwargs})
        return sentinel

    monkeypatch.setattr(
        "decoy_engine.execution.physical.drivers._chunked.run_native_or_oracle_chunked",
        _stub,
    )
    adapter = NativeOrOracleChunkedAdapter()
    chunks_marker = iter([pa.table({"c": [1]})])

    result = adapter.run({"cfg": 1}, chunks_marker, table="t", engine_version="v")

    assert result is sentinel
    assert calls[0]["chunks"] is chunks_marker


def test_resident_chunked_aggregator_forwards_result_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = _Sentinel()
    calls: list[dict[str, Any]] = []

    def _stub(config: Any, source: Any, **kwargs: Any) -> Any:
        calls.append({"config": config, "source": source, **kwargs})
        return sentinel

    monkeypatch.setattr("decoy_engine.execution.physical.drivers._chunked.run_mask_chunked", _stub)
    adapter = ResidentChunkedAggregatorAdapter()
    source_marker = pa.table({"c": [1]})

    result = adapter.run(
        {"cfg": 1},
        source_marker,
        table="t",
        engine_version="v",
        registry=object(),  # type: ignore[arg-type]
        adapter=object(),
        vault_writer=None,
        chunk_size_rows=10,
    )

    assert result is sentinel
    assert calls[0]["source"] is source_marker


def test_native_stream_adapter_forwards_result_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel_result = _Sentinel()
    sentinel_report = _Sentinel()
    calls: list[dict[str, Any]] = []

    def _stub(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return (sentinel_result, sentinel_report)

    monkeypatch.setattr(
        "decoy_engine.execution.physical.drivers._native_stream.try_native_route", _stub
    )
    adapter = NativeStreamAdapter()
    plan_marker = object()

    result, report = adapter.run(
        config={"cfg": 1},
        plan=plan_marker,
        table_kinds={"t": "mask"},
        caller_sources={},
        source_loader=None,
        sink=None,
        fidelity_report=False,
        execution_mode="auto",
        graph=object(),  # type: ignore[arg-type]
    )

    assert result is sentinel_result
    assert report is sentinel_report
    assert calls[0]["plan"] is plan_marker
    # The pinned module default for `batch_rows` must not be overridden by a
    # `None` the caller never asked to change (see the adapter's conditional
    # forwarding).
    assert "batch_rows" not in calls[0]


def test_native_stream_adapter_forwards_declined_none_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The native lane's decline shape `(None, report)` is a first-class,
    non-error outcome (plan C1); the adapter must forward it verbatim, not
    treat it as a missing/incomplete result."""
    sentinel_report = _Sentinel()

    def _stub(**kwargs: Any) -> Any:
        return (None, sentinel_report)

    monkeypatch.setattr(
        "decoy_engine.execution.physical.drivers._native_stream.try_native_route", _stub
    )
    adapter = NativeStreamAdapter()

    result, report = adapter.run(
        config={},
        plan=object(),
        table_kinds={},
        caller_sources={},
        source_loader=None,
        sink=None,
        fidelity_report=False,
        execution_mode="auto",
        graph=object(),  # type: ignore[arg-type]
    )

    assert result is None
    assert report is sentinel_report


def test_out_of_core_adapter_forwards_result_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = _Sentinel()
    calls: list[dict[str, Any]] = []

    def _stub(plan: Any, sources: Any, **kwargs: Any) -> Any:
        calls.append({"plan": plan, "sources": sources, **kwargs})
        return sentinel

    monkeypatch.setattr(
        "decoy_engine.execution.physical.drivers._out_of_core.run_fk_out_of_core", _stub
    )
    adapter = OutOfCoreAdapter()
    sources_marker = {"t": pa.table({"c": [1]})}

    result = adapter.run(
        object(),  # type: ignore[arg-type]
        sources_marker,
        registry=object(),  # type: ignore[arg-type]
        relationship_graph=object(),  # type: ignore[arg-type]
    )

    assert result is sentinel
    assert calls[0]["sources"] is sources_marker


def test_synthesis_stage_adapter_forwards_result_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel_outputs = {"gen_table": pa.table({"c": [1]})}
    calls: list[tuple[Any, Any, Any]] = []

    def _stub(
        plan: Any,
        derive_key: Any = None,
        instance_default_locale: Any = None,
        *,
        provider_snapshot: Any = None,
    ) -> Any:
        calls.append((plan, derive_key, instance_default_locale))
        return sentinel_outputs

    monkeypatch.setattr("decoy_engine.execution.physical.drivers._synthesis.generate_tables", _stub)
    adapter = SynthesisStageAdapter()
    plan_marker = object()

    result = adapter.run(plan_marker)

    assert result is sentinel_outputs
    assert calls[0][0] is plan_marker
