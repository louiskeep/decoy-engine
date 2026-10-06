"""C8-i acceptance test 8: the auto-chunk planner and the multi-table split.

`auto_chunk` is a transparent optimization: a job that auto-chunks must equal the forced
whole-frame run. The planner therefore relaxes its blanket `when` rejection only for a
column the native route admits whose predicate reads string columns only, and keeps it for
everything else, including Codex's integer counterexample (a numeric reference can widen
int64 to float64 in some chunks and not others).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import run_pipeline
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    hash_col,
    key_provider,
    make_config,
    passthrough,
    redact,
    truncate,
)

_ROWS = 24
_S = [f"name-{i}" for i in range(_ROWS)]
_P = [("x" if i % 3 == 0 else "y" if i % 3 == 1 else None) for i in range(_ROWS)]


def _source() -> pa.Table:
    return pa.table(
        {
            "s": pa.array(_S),
            "p": pa.array(_P, pa.string()),
            "n": pa.array([i if i % 5 else None for i in range(_ROWS)], pa.int64()),
        }
    )


def _config(columns: list[dict[str, Any]], tmp_path: Path, source: pa.Table) -> dict[str, Any]:
    cfg = make_config(columns)
    path = str(tmp_path / "source.parquet")
    pq.write_table(source, path)
    cfg["sources"][TABLE] = {"type": "file", "format": "parquet", "path": path}
    cfg["targets"][TABLE] = {"type": "file", "format": "parquet", "path": path + ".out"}
    return cfg


def _run(cfg: dict[str, Any], source: pa.Table, **kw: Any) -> Any:
    return run_pipeline(
        copy.deepcopy(cfg),
        {TABLE: source},
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        **kw,
    )


def _auto(cfg: dict[str, Any], source: pa.Table) -> Any:
    return _run(cfg, source, auto_chunk_threshold_rows=5, chunk_size_rows=7)


def _full(cfg: dict[str, Any], source: pa.Table) -> Any:
    return _run(cfg, source, auto_chunk=False)


def _equal_outputs(a: Any, b: Any) -> None:
    got, want = a.outputs[TABLE], b.outputs[TABLE]
    assert got.schema.names == want.schema.names
    for name in got.schema.names:
        assert got.column(name).to_pylist() == want.column(name).to_pylist(), name


@pytest.mark.parametrize(
    "column",
    [redact("s"), truncate("s"), pytest.param(hash_col("s"), marks=NEEDS_COMPANION)],
    ids=["redact", "truncate", "hash"],
)
@pytest.mark.parametrize(
    "predicate",
    ["s == 'name-3'", "p == 'x'", "p != 'x' and s != 'name-1'", "p in ['y'] or s == 'name-0'"],
)
def test_an_admitted_when_table_auto_chunks_and_runs_native(
    column: dict[str, Any], predicate: str, tmp_path: Path
) -> None:
    cfg = _config([{**column, "when": predicate}, passthrough("p")], tmp_path, _source())
    auto, full = _auto(cfg, _source()), _full(cfg, _source())
    assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked", auto.quality_metrics[
        "auto_chunk"
    ]["reason"]
    assert full.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    route = auto.quality_metrics["chunked_route"]
    assert route["native_admitted"] is True, route["reroute_reason"]
    _equal_outputs(auto, full)
    assert auto.outputs[TABLE].schema.field("s").type == pa.string()


def test_the_integer_redact_counterexample_stays_full_frame_and_succeeds(tmp_path: Path) -> None:
    """Codex round 1: an integer column redacted to 0.5 where `x == 1`. Per-chunk and
    whole-frame evaluation can type that column differently, so it must not auto-chunk."""
    source = pa.table({"x": pa.array([1, 2, 1, 3, 1, 4, 1, 5], pa.int64())})
    cfg = _config([{**redact("x", redact_with=0.5), "when": "x == 1"}], tmp_path, source)
    auto = _run(cfg, source, auto_chunk_threshold_rows=3, chunk_size_rows=3)
    assert auto.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    assert "when_predicate_not_chunk_stable" in auto.quality_metrics["auto_chunk"]["reason"]
    _equal_outputs(auto, _full(cfg, source))


def test_a_numeric_reference_stays_full_frame(tmp_path: Path) -> None:
    """The native route admits a numeric reference on the explicit chunked entry, but the
    planner needs auto-chunk to equal the whole frame, and int64 can widen per chunk."""
    cfg = _config([{**redact("s"), "when": "n > 3"}, passthrough("n")], tmp_path, _source())
    auto = _auto(cfg, _source())
    assert auto.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    assert "when_predicate_not_chunk_stable" in auto.quality_metrics["auto_chunk"]["reason"]
    _equal_outputs(auto, _full(cfg, _source()))


@pytest.mark.parametrize("expr", ["p.notnull()", "p == s", "len(p) == 1"])
def test_a_raw_dict_predicate_outside_the_grammar_stays_full_frame(
    expr: str, tmp_path: Path
) -> None:
    cfg = _config([{**redact("s"), "when": expr}, passthrough("p")], tmp_path, _source())
    auto = _auto(cfg, _source())
    assert auto.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    assert "when_predicate_not_chunk_stable" in auto.quality_metrics["auto_chunk"]["reason"]
    _equal_outputs(auto, _full(cfg, _source()))


def test_a_predicate_reading_an_earlier_masked_column_stays_full_frame(tmp_path: Path) -> None:
    source = pa.table({"a": pa.array(_S), "z": pa.array(_S)})
    cfg = _config([{**redact("z"), "when": "a == 'REDACTED'"}, redact("a")], tmp_path, source)
    auto = _run(cfg, source, auto_chunk_threshold_rows=5, chunk_size_rows=7)
    assert auto.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    assert "when_predicate_not_chunk_stable" in auto.quality_metrics["auto_chunk"]["reason"]
    _equal_outputs(auto, _full(cfg, source))


def test_one_declined_when_column_keeps_the_whole_table_full_frame(tmp_path: Path) -> None:
    cfg = _config(
        [
            {**redact("s"), "when": "p == 'x'"},
            {**truncate("p"), "when": "p.notnull()"},
        ],
        tmp_path,
        _source(),
    )
    auto = _auto(cfg, _source())
    assert auto.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    _equal_outputs(auto, _full(cfg, _source()))


# ---------------------------------------------------------------------------
# The multi-table split classifies each table through the same planner.
# ---------------------------------------------------------------------------


def test_a_split_job_routes_an_admitted_when_table_chunked_and_the_other_full_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.execution import _multi_table_support as mt

    a_rows = mt.BIG
    a_table = pa.table(
        {
            "r": pa.array([f"a{i}" for i in range(a_rows)]),
            "g": pa.array(["x" if i % 2 else "y" for i in range(a_rows)]),
        }
    )
    b_table = pa.table(
        {
            "r": pa.array([f"b{i}" for i in range(a_rows)]),
            "g": pa.array(["x" if i % 2 else "y" for i in range(a_rows)]),
        }
    )
    cfg, sources = mt.build_job(
        tmp_path,
        {
            "tbl_a": ([{**redact("r"), "when": "g == 'x'"}, passthrough("g")], a_table),
            "tbl_b": ([redact("r"), passthrough("g")], b_table),
        },
    )
    # A raw-dict predicate outside the grammar is not validated: table B keeps full-frame.
    cfg["tables"][1]["columns"][0]["when"] = "g.notnull()"
    calls = mt.spy_split(monkeypatch)
    got = run_pipeline(cfg, sources=sources, **mt.kw())
    off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert len(calls) == 1
    assert mt.dispatched_tables(got) == ["tbl_a"]
    route_a = mt.route_of(got, "tbl_a")
    assert route_a is not None and route_a["native_admitted"] is True, route_a
    for name in ("tbl_a", "tbl_b"):
        assert (
            got.outputs[name].column("r").to_pylist() == off.outputs[name].column("r").to_pylist()
        )
    assert got.outputs["tbl_a"].schema.field("r").type == pa.string()
