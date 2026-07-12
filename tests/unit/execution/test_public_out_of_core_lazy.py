"""DE-09 (adversarial review): the DIRECT PUBLIC out-of-core route must not
fully materialize its input, and its residency telemetry must be honest.

TB-1 fixed input residency on the ISOLATED/governed out-of-core path
(`_isolated_worker._load_sources` hands `run_pipeline` `LazySource` handles).
The equivalent gap remained on the direct public route:
`_pipeline_route_exec.run_out_of_core_route` resolved every MISSING source
(the lazy `sources={}` + `source_loader` shape, or a forced
`execution_mode="out_of_core"`) by eagerly calling `source_loader(table)` and
retaining the resulting resident `pa.Table`s -- so a job that reached this
route specifically to STREAM its input paid for a fully resident copy first.
Its telemetry then reported `loaded_fully_in_memory` from the caller's
ORIGINAL shape, so it read `False` (bounded) while actually holding everything
resident.

DE-09 resolves a missing source to a `LazySource` from its on-disk Parquet
path (streamed by `run_fk_out_of_core` via bounded `iter_batches`, never
`.to_table()`) whenever a path is available, keeps a documented resident
`source_loader` fallback for a source with no lazy Parquet handle (CSV /
fixed_width / cloud), and computes `loaded_fully_in_memory` from what the
route ACTUALLY holds after resolution.

These tests exercise `run_pipeline` directly (no subprocess), mirroring the
mechanism-assertion style of `test_isolated_worker_streaming.py`: a spy over
`run_fk_out_of_core` captures the exact `sources` mapping the runner receives,
so "resolved as a LazySource, not a materialized pa.Table" is asserted at the
runner boundary rather than inferred from memory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import decoy_engine.execution.out_of_core as _ooc_mod
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.profile._readers import LazySource

_N = 30


def _hash_col(name: str, namespace: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _parent_child() -> tuple[pa.Table, pa.Table]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
            "note": pa.array([f"secret{i}" for i in range(_N)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(_N)], type=pa.string()),
            "parent_id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
        }
    )
    return parent, child


def _config(tmp_path: Path, src_fmt: str) -> dict[str, Any]:
    """A pure-mask parent->child FK job the out-of-core compat gate ADMITS
    (hash FK keys + a redact payload)."""
    parent, child = _parent_child()
    if src_fmt == "parquet":
        parent_src = str(tmp_path / "parent.parquet")
        child_src = str(tmp_path / "child.parquet")
        pq.write_table(parent, parent_src)
        pq.write_table(child, child_src)
    else:
        parent_src = str(tmp_path / "parent.csv")
        child_src = str(tmp_path / "child.csv")
        parent.to_pandas().to_csv(parent_src, index=False)
        child.to_pandas().to_csv(child_src, index=False)
    return {
        "version": 1,
        "global_settings": {"job_name": "de09", "seed": 7},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": src_fmt},
            "child": {"type": "file", "path": child_src, "format": src_fmt},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {
                "name": "parent",
                "columns": [_hash_col("id", "ns"), {"name": "note", "strategy": "redact"}],
            },
            {"name": "child", "columns": [_hash_col("cid", "cns"), _hash_col("parent_id", "ns")]},
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


def _file_loader(config: dict[str, Any]) -> Any:
    """A resident loader that reads the on-disk source for `table_name`."""

    def loader(table_name: str) -> pa.Table:
        spec = config["sources"][table_name]
        if spec["format"] == "parquet":
            return pq.read_table(spec["path"])
        return pa.Table.from_pandas(pd.read_csv(spec["path"]), preserve_index=False)

    return loader


def _capture_runner_sources(monkeypatch: Any) -> dict[str, Any]:
    """Spy over `run_fk_out_of_core` that records the exact `sources` mapping
    the runner is handed, then delegates to the real runner. `run_out_of_core_route`
    re-imports the name from the module on each call, so patching the module
    attribute is picked up."""
    captured: dict[str, Any] = {}
    real = _ooc_mod.run_fk_out_of_core

    def spy(plan: Any, sources: Any, **kwargs: Any) -> Any:
        captured["sources"] = dict(sources)
        return real(plan, sources, **kwargs)

    monkeypatch.setattr(_ooc_mod, "run_fk_out_of_core", spy)
    return captured


# ---------------------------------------------------------------------------
# Mechanism: a missing Parquet-backed source reaches the runner as a LazySource
# (streamed), NOT a resident pa.Table -- and source_loader is bypassed.
# ---------------------------------------------------------------------------


def test_missing_parquet_source_resolved_as_lazysource_not_materialized(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """FAILS pre-DE-09: `run_out_of_core_route` eagerly called
    `source_loader(table)` for every missing table, so the runner received
    resident `pa.Table`s and the loader spy was called."""
    config = _config(tmp_path, "parquet")
    captured = _capture_runner_sources(monkeypatch)
    loader = mock.Mock(side_effect=AssertionError("source_loader must not be called for Parquet"))

    result = run_pipeline(
        config,
        sources={},  # lazy shape: nothing resident
        engine_version="0.1.0",
        execution_mode="out_of_core",
        source_loader=loader,
    )

    # The runner-boundary mechanism proof: every source is a lazy on-disk
    # handle the runner streams via iter_batches, never a materialized table.
    assert set(captured["sources"]) == {"parent", "child"}
    for table, src in captured["sources"].items():
        assert isinstance(src, LazySource), f"{table} reached runner as {type(src).__name__}"
        assert not isinstance(src, pa.Table)
    # The resident source_loader was bypassed entirely.
    loader.assert_not_called()
    assert set(result.outputs) == {"parent", "child"}


def test_auto_routed_lazy_parquet_job_streams_without_loader(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The natural (non-forced) lazy admission shape: `sources={}` + loader,
    auto-routed to out_of_core by size. The Parquet sources still lazify."""
    config = _config(tmp_path, "parquet")
    captured = _capture_runner_sources(monkeypatch)
    loader = mock.Mock(side_effect=AssertionError("source_loader must not be called for Parquet"))

    run_pipeline(
        config,
        sources={},
        engine_version="0.1.0",
        source_loader=loader,
        out_of_core_threshold_rows=10,  # 30-row fixture is "large"
        full_frame_reject_rows=10,
    )

    assert all(isinstance(src, LazySource) for src in captured["sources"].values())
    loader.assert_not_called()


# ---------------------------------------------------------------------------
# Telemetry honesty: loaded_fully_in_memory reflects ACTUAL residency.
# ---------------------------------------------------------------------------


def test_telemetry_bounded_when_all_sources_lazified(tmp_path: Path) -> None:
    """FAILS pre-DE-09: telemetry read `loaded_fully_in_memory=False` from the
    caller's `sources={}`+loader shape while the route actually held every
    table resident (via eager `source_loader`). Post-DE-09 the sources are
    genuinely streamed, so the honest value is also False -- and now TRUE to
    the residency it reports."""
    config = _config(tmp_path, "parquet")
    result = run_pipeline(
        config,
        sources={},
        engine_version="0.1.0",
        execution_mode="out_of_core",
        source_loader=_file_loader(config),
    )
    exec_meta = result.quality_metrics["execution"]
    assert exec_meta["execution_mode"] == "out_of_core"
    assert exec_meta["loaded_fully_in_memory"] is False


def test_telemetry_resident_when_source_loader_fallback_holds_a_table(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A CSV source has no lazy Parquet handle, so the documented resident
    `source_loader` fallback fires -- and telemetry must ADMIT the resulting
    residency rather than claiming bounded. Pre-DE-09 this reported
    `loaded_fully_in_memory=False` while holding both tables resident."""
    config = _config(tmp_path, "csv")
    captured = _capture_runner_sources(monkeypatch)
    loader = mock.Mock(side_effect=_file_loader(config))

    result = run_pipeline(
        config,
        sources={},
        engine_version="0.1.0",
        execution_mode="out_of_core",
        source_loader=loader,
    )
    exec_meta = result.quality_metrics["execution"]
    assert exec_meta["execution_mode"] == "out_of_core"
    # Honest: CSV fell back to the resident loader, so a whole table IS held.
    assert exec_meta["loaded_fully_in_memory"] is True
    assert loader.call_count == 2
    assert all(isinstance(src, pa.Table) for src in captured["sources"].values())


def test_telemetry_resident_for_mixed_residency(tmp_path: Path, monkeypatch: Any) -> None:
    """A caller-supplied resident parent + a missing Parquet child: the child
    lazifies, but the resident parent means a whole input IS in memory, so
    telemetry honestly reports `loaded_fully_in_memory=True`."""
    config = _config(tmp_path, "parquet")
    parent_resident = pq.read_table(config["sources"]["parent"]["path"])
    captured = _capture_runner_sources(monkeypatch)

    result = run_pipeline(
        config,
        sources={"parent": parent_resident},  # parent resident, child missing
        engine_version="0.1.0",
        execution_mode="out_of_core",
        source_loader=_file_loader(config),
    )
    assert isinstance(captured["sources"]["parent"], pa.Table)  # caller's resident table kept
    assert isinstance(captured["sources"]["child"], LazySource)  # missing child lazified
    assert result.quality_metrics["execution"]["loaded_fully_in_memory"] is True


# ---------------------------------------------------------------------------
# Correctness: streaming a lazified input stays byte-parity with full_frame.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("execution_mode", ["out_of_core", None])
def test_lazified_out_of_core_matches_full_frame_oracle(
    tmp_path: Path, execution_mode: str | None
) -> None:
    config = _config(tmp_path, "parquet")
    resident = {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}
    oracle = run_pipeline(config, resident, engine_version="0.1.0", execution_mode="full_frame")

    kwargs: dict[str, Any] = {"source_loader": _file_loader(config)}
    if execution_mode is not None:
        kwargs["execution_mode"] = execution_mode
    else:
        kwargs["out_of_core_threshold_rows"] = 10
        kwargs["full_frame_reject_rows"] = 10
    routed = run_pipeline(config, sources={}, engine_version="0.1.0", **kwargs)

    assert routed.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    for table in ("parent", "child"):
        assert routed.outputs[table].schema == oracle.outputs[table].schema
        assert routed.outputs[table].to_pydict() == oracle.outputs[table].to_pydict()
