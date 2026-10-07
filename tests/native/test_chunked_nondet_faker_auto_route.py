"""C5b-ii auto-router regression (plan tests 10 and 10b).

`auto_chunk` is a transparent optimization, so a non-deterministic Faker job must succeed and
equal the forced whole-frame run of the identical config for every source dtype, with the
namespace configured and unset. A string source runs the native leg; any other source takes the
chunked-oracle leg. The multi-table split reclassifies each table through the single-table
planner, so an above-threshold Faker table inside a split run now runs chunked and must equal
its whole-frame run, with the default namespace of ITS table and the row offset restarting at 0.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import run_pipeline
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    key_provider,
)
from tests.native._chunked_faker_support import (
    default_namespace,
    expected_values,
    make_config,
    nd_faker,
    passthrough,
)

_ROWS = 10
_STRINGS = ["a", "b", None, "a", "b", "c", "a", "b", "c", "a"]


def _source(kind: str) -> pa.Table:
    column = {
        "string": pa.array(_STRINGS, pa.string()),
        "int64": pa.array(range(_ROWS), pa.int64()),
        "float64": pa.array([None if i % 4 == 1 else float(i) for i in range(_ROWS)], pa.float64()),
        "null": pa.nulls(_ROWS),
    }[kind]
    return pa.table({"f": column, "p": pa.array(range(_ROWS), pa.int64())})


def _config(kind: str, tmp_path: Path, namespace: str | None) -> dict[str, Any]:
    cfg = make_config([nd_faker(namespace=namespace), passthrough("p")])
    path = str(tmp_path / f"{kind}.parquet")
    pq.write_table(_source(kind), path)
    cfg["sources"][TABLE] = {"type": "file", "format": "parquet", "path": path}
    cfg["targets"][TABLE] = {"type": "file", "format": "parquet", "path": path + ".out"}
    return cfg


def _run(cfg: dict[str, Any], kind: str, **kw: Any) -> Any:
    return run_pipeline(
        copy.deepcopy(cfg),
        {TABLE: _source(kind)},
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        **kw,
    )


def _f_evidence(result: Any) -> dict[str, Any]:
    route = result.quality_metrics["chunked_route"]
    return {c["column"]: c for c in route["columns"]}["f"]


@NEEDS_COMPANION
@pytest.mark.parametrize("namespace", [None, "ns_f"], ids=["none", "configured"])
@pytest.mark.parametrize("kind", ["string", "int64", "float64", "null"])
def test_the_auto_routed_job_succeeds_and_equals_the_full_frame_run(
    kind: str, namespace: str | None, tmp_path: Path
) -> None:
    cfg = _config(kind, tmp_path, namespace)
    auto = _run(cfg, kind, auto_chunk_threshold_rows=3, chunk_size_rows=4)
    full = _run(cfg, kind, auto_chunk=False)
    assert auto.quality_metrics["auto_chunk"]["mode"] == "chunked"
    assert full.quality_metrics["auto_chunk"]["mode"] == "full_frame"
    a, f = auto.outputs[TABLE], full.outputs[TABLE]
    assert a.schema.names == f.schema.names
    assert a.column("f").to_pylist() == f.column("f").to_pylist()
    assert a.column("p").to_pylist() == f.column("p").to_pylist()
    if kind != "null":  # an all-null source keeps its null type on the whole-frame route
        assert a.schema.field("f").type == f.schema.field("f").type == pa.string()
    else:
        assert a.schema.field("f").type == pa.string()


@NEEDS_COMPANION
@pytest.mark.parametrize("kind", ["string", "int64", "float64", "null"])
def test_string_and_numeric_run_the_native_leg_and_every_other_source_runs_the_oracle_leg(
    kind: str, tmp_path: Path
) -> None:
    auto = _run(_config(kind, tmp_path, None), kind, auto_chunk_threshold_rows=3, chunk_size_rows=4)
    route = auto.quality_metrics["chunked_route"]
    evidence = _f_evidence(auto)
    if kind != "null":
        assert route["native_admitted"] is True
        assert evidence["planned_backend"] == evidence["executed_backend"] == "rust_companion"
    else:
        assert route["native_admitted"] is False
        assert "faker_source_type_not_string:f:" in route["reroute_reason"]
        assert evidence["executed_backend"] == "pandas_oracle"


# ---------------------------------------------------------------------------
# 10b. Multi-table split, end to end.
# ---------------------------------------------------------------------------


def _split_job(tmp_path: Path, *, a_rows: int) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    from tests.unit.execution import _auto_chunk_support as support
    from tests.unit.execution import _multi_table_support as mt

    a_table = pa.table(
        {
            "f": pa.array([f"a{i}" for i in range(a_rows)]),
            "h": pa.array([f"h{i}" for i in range(a_rows)]),
        }
    )
    return mt.build_job(
        tmp_path,
        {
            "tbl_a": ([nd_faker("f"), support.hash_col("h", "ha")], a_table),
            "tbl_b": (mt.std_columns("b_ns"), mt.string_table(mt.BIG, "b")),
        },
    )


@NEEDS_COMPANION
def test_an_above_threshold_faker_table_in_a_split_run_goes_chunked_and_equals_whole_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.execution import _multi_table_support as mt

    cfg, sources = _split_job(tmp_path, a_rows=mt.BIG)
    calls = mt.spy_split(monkeypatch)
    got = run_pipeline(cfg, sources=sources, **mt.kw())
    off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert len(calls) == 1
    assert set(mt.dispatched_tables(got)) == {"tbl_a", "tbl_b"}
    route_a = mt.route_of(got, "tbl_a")
    assert route_a is not None and route_a["native_admitted"] is True
    faker_a = next(c for c in route_a["columns"] if c["column"] == "f")
    assert faker_a["executed_backend"] == "rust_companion"
    # Per-table equality with the forced whole-frame run.
    for name in ("tbl_a", "tbl_b"):
        assert got.outputs[name].equals(off.outputs[name]), name
    # A's default namespace uses table A, and the offset restarts at 0 for it.
    values = got.outputs["tbl_a"].column("f").to_pylist()
    assert values == expected_values(
        range(len(values)),
        config=cfg,
        namespace=None,
        table="tbl_a",
        job_seed=_job_seed(cfg),
    )
    assert default_namespace("tbl_a", "f") != default_namespace("tbl_b", "f")


def test_a_below_threshold_faker_table_keeps_the_full_frame_group_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.execution import _multi_table_support as mt

    cfg, sources = _split_job(tmp_path, a_rows=mt.SMALL)
    calls = mt.spy_split(monkeypatch)
    got = run_pipeline(cfg, sources=sources, **mt.kw())
    off = run_pipeline(cfg, sources=sources, **mt.kw(**mt.off_kw()))
    assert "tbl_a" not in mt.dispatched_tables(got)
    assert len(calls) <= 1
    # The full-frame table keeps its pandas metadata; a dispatched sibling differs from the
    # split-off run only in that metadata.
    assert got.outputs["tbl_a"].equals(off.outputs["tbl_a"], check_metadata=True)
    assert got.outputs["tbl_b"].equals(off.outputs["tbl_b"])


def _job_seed(cfg: dict[str, Any]) -> bytes:
    from decoy_engine.plan import compile_plan
    from decoy_engine.profile import profile_source

    plan = compile_plan(cfg, profile_source(cfg, seed=42), decoy_engine_version=ENGINE_VERSION)
    return plan.seed_envelope.job_seed
