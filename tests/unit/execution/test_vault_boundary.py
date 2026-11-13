"""B0: a file-vault writer is accepted only where the engine holds every output row (B-6 D1).

With a `sink` the engine commits output inside the run, and a chunked entry yields chunks the
caller publishes as they arrive; in both the caller can only write the vault afterwards, so a crash
between the two leaves published output whose vault never existed. `run_pipeline(sink=...,
vault_writer=...)`, the public chunked entries with any `vault_writer`, and `run_fk_out_of_core`
with both are refused with `vault_requires_resident_output` before any source is read. The
engine's own auto-chunk lane still collects vault entries and returns resident output.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.execution import ExecutionError
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution._b6a_support import RecordingSink

CODE = "vault_requires_resident_output"


def _vault_cfg(tmp_path: Path) -> tuple[dict[str, Any], pa.Table]:
    src = pa.table({"h": pa.array([f"u{i}@x.example" for i in range(support.ROWS)])})
    cfg = support.make_cfg(
        [{**support.hash_col("h"), "vault": True}],
        path=support.write_source(src, tmp_path / "s.parquet"),
    )
    return cfg, src


def _writer() -> Any:
    from decoy_engine.vault import VaultWriter

    return VaultWriter(bytes(range(32)))


class _LoudSource:
    """A source that fails the test when the engine touches it."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the source was touched ({name}) before the refusal")


class _UnreadChunks:
    """A chunk iterable that fails the test when it is advanced."""

    def __iter__(self) -> Iterator[pa.Table]:
        raise AssertionError("a chunk was read before the refusal")


def _assert_refused(exc: pytest.ExceptionInfo[ExecutionError]) -> None:
    assert exc.value.code == CODE
    assert str(exc.value) == f"[{CODE}] {reasons.get('runtime', CODE).summary}"
    assert exc.value.__cause__ is None and exc.value.__context__ is None


def test_run_pipeline_refuses_a_sink_with_a_vault_writer_before_reading_a_source(
    tmp_path: Path,
) -> None:
    from decoy_engine.execution import run_pipeline

    cfg, _src = _vault_cfg(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    sink = RecordingSink()
    writer = _writer()
    with pytest.raises(ExecutionError) as exc:
        run_pipeline(
            cfg,
            sources={support.TABLE: _LoudSource()},  # type: ignore[dict-item]
            sink=sink,
            vault_writer=writer,
            **support.run_kwargs(),
        )
    _assert_refused(exc)
    assert sink.calls == []
    assert writer._entries == set()
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.parametrize("entry", ["run_mask_chunked", "run_mask_pipeline_chunked"])
def test_chunked_entries_refuse_a_vault_writer_before_the_first_chunk(
    entry: str, tmp_path: Path
) -> None:
    import decoy_engine
    from tests._declared_schema import declared_schema

    cfg, _src = _vault_cfg(tmp_path)
    writer = _writer()
    # The pipeline entry takes the declared schema as a required argument, bound before the call.
    extra = {"source_schema": declared_schema(cfg, support.TABLE)}
    with pytest.raises(ExecutionError) as exc:
        getattr(decoy_engine, entry)(
            cfg,
            _UnreadChunks(),
            table=support.TABLE,
            engine_version=support.ENGINE_VERSION,
            vault_writer=writer,
            **(extra if entry == "run_mask_pipeline_chunked" else {}),
        )
    _assert_refused(exc)
    assert writer._entries == set()


def test_run_fk_out_of_core_refuses_a_sink_with_a_vault_writer(tmp_path: Path) -> None:
    from decoy_engine.execution.out_of_core import run_fk_out_of_core
    from decoy_engine.relationships import RelationshipGraph

    sink = RecordingSink()
    writer = _writer()
    with pytest.raises(ExecutionError) as exc:
        run_fk_out_of_core(
            _LoudSource(),  # type: ignore[arg-type]
            {support.TABLE: _LoudSource()},  # type: ignore[dict-item]
            registry=_LoudSource(),  # type: ignore[arg-type]
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            sink=sink,
            vault_writer=writer,
            config={},
        )
    _assert_refused(exc)
    assert sink.calls == [] and writer._entries == set()


@pytest.mark.parametrize("dispatcher", [True, False], ids=["dispatcher_lane", "legacy_lane"])
def test_the_auto_chunk_lane_still_collects_vault_entries_resident(
    dispatcher: bool, tmp_path: Path
) -> None:
    from decoy_engine.execution import run_pipeline
    from decoy_engine.vault import vault_writer_for_config

    cfg, src = _vault_cfg(tmp_path)
    writer = vault_writer_for_config(cfg)
    result = run_pipeline(
        cfg,
        sources={support.TABLE: src},
        vault_writer=writer,
        **support.run_kwargs(
            auto_chunk=True, auto_chunk_threshold_rows=1, chunked_dispatcher_enabled=dispatcher
        ),
    )
    assert result.outputs[support.TABLE].num_rows == support.ROWS
    assert len(writer._entries) == support.ROWS
    assert result.quality_metrics["auto_chunk"]["mode"] == "chunked"
