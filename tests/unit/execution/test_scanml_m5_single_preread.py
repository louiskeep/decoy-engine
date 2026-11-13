"""SCAN-ML M5: the chunked pre-read sequence lives once, in each guard-free core.

`run_mask_chunked` and `run_mask_pipeline_chunked` run the post-scan route guard and then hand
everything to their core (`native.chunked._core.mask_chunked`,
`_chunked_oracle._mask_pipeline_chunked`), which `run_pipeline`'s resident lane also calls. A
pre-read step added to a core therefore runs on the public entry too, before any chunk is read.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from tests.unit.execution import _auto_chunk_support as support

_SRC = pa.table({"r": pa.array([f"s{i}" for i in range(6)])})


class _CountedChunks:
    def __init__(self) -> None:
        self.pulled = 0

    def __iter__(self) -> Iterator[pa.Table]:
        for chunk in (_SRC.slice(0, 3), _SRC.slice(3)):
            self.pulled += 1
            yield chunk


def _config() -> dict[str, Any]:
    raw = {
        "version": 2,
        "global_settings": {"seed": 42},
        "post_scan": {"integrity": True},
        "sources": {"t": {"type": "file", "format": "parquet", "path": "/dev/null"}},
        "tables": [{"name": "t", "columns": [support.redact_col("r")]}],
        "targets": {"t": {"type": "file", "format": "parquet", "path": "/dev/null"}},
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _core_and_entry(entry: str) -> tuple[Any, str, Any, dict[str, Any]]:
    from decoy_engine.execution import _chunked, _chunked_oracle
    from decoy_engine.execution.native import _chunked_entry
    from decoy_engine.execution.native.chunked import _core

    if entry == "run_mask_chunked":
        return _core, "mask_chunked", _chunked_entry.run_mask_chunked, {}
    return (
        _chunked_oracle,
        "_mask_pipeline_chunked",
        _chunked.run_mask_pipeline_chunked,
        {"source_schema": _SRC.schema},
    )


@pytest.mark.parametrize("entry", ["run_mask_chunked", "run_mask_pipeline_chunked"])
def test_a_pre_read_step_added_to_the_core_runs_on_the_public_entry(
    entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, name, public, extra = _core_and_entry(entry)
    real = getattr(module, name)
    chunks = _CountedChunks()
    probe: list[int] = []

    def core_with_probe(*args: Any, **kwargs: Any) -> Any:
        probe.append(chunks.pulled)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, core_with_probe)
    out = list(public(_config(), chunks, table="t", engine_version="v", **extra))
    assert probe == [0], f"{entry} did not run the core's pre-read sequence before a read"
    assert sum(t.num_rows for t in out) == _SRC.num_rows


@pytest.mark.parametrize("entry", ["run_mask_chunked", "run_mask_pipeline_chunked"])
def test_the_core_takes_exactly_the_public_entry_parameters(entry: str) -> None:
    import inspect

    module, name, public, _ = _core_and_entry(entry)
    core = inspect.signature(getattr(module, name)).parameters
    kinds = {p.kind for p in core.values()}
    assert inspect.Parameter.VAR_KEYWORD not in kinds, f"{name} takes **kwargs"
    entry_params = inspect.signature(inspect.unwrap(public)).parameters
    assert [(p.name, p.kind, p.default) for p in core.values()] == [
        (p.name, p.kind, p.default) for p in entry_params.values()
    ]
