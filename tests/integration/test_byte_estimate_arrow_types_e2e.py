"""End-to-end acceptance tests for Arrow-type byte-estimate pricing (plan
docs/plans/2026-10-01-byte-estimate-temporal-columns.md, revision 3.1): tests 1,
1b, 2, 4, and the run_pipeline halves of 11 and 12.

With default knobs, `run_pipeline` computes a full-frame byte estimate for every
job that has a mask table. Before the fix that estimate raised for any column
whose pandas label was not a numpy fixed-width type (dates, times, tz timestamps,
non-ns durations, decimals, binary, categories, ...), although the same job runs
with `use_byte_estimate_routing=False`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._mem_estimate import (
    ColumnSizeSpec,
    TableSizeSpec,
    estimate_peak_bytes,
    fits,
)
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.profile import profile_source
from decoy_engine.profile._readers import LazySource
from tests.native._rev9_type_catalogue import CATALOGUE
from tests.unit.execution._transform_testkit import fk_config, fk_tables

ENGINE_VERSION = "byte-estimate-arrow-types-e2e"
_GB = 1024 * 1024 * 1024
_KEY = bytes(range(32))


def _kp() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_KEY, key_version="v1")


def _validated(raw: dict[str, Any]) -> dict[str, Any]:
    return PipelineConfig.model_validate(raw).model_dump()


def _run(config: dict[str, Any], sources: dict[str, Any], **kwargs: Any) -> ExecutionResult:
    return run_pipeline(
        config, dict(sources), engine_version=ENGINE_VERSION, key_provider=_kp(), **kwargs
    )


def _assert_same_outputs(on: ExecutionResult, off: ExecutionResult) -> None:
    assert set(on.outputs) == set(off.outputs)
    for name in off.outputs:
        assert on.outputs[name].equals(off.outputs[name]), f"{name} differs"


def _single_table_config(
    tmp_path: Path, source: Path, columns: list[dict[str, Any]], extra_tables: list[Any] = ()
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": 5},
        "sources": {"t": {"type": "file", "format": "parquet", "path": str(source)}},
        "targets": {"t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out")}},
        "tables": [{"name": "t", "columns": columns}, *extra_tables],
    }
    for extra in extra_tables:
        raw["targets"][extra["name"]] = {
            "type": "file",
            "format": "parquet",
            "path": str(tmp_path / f"{extra['name']}.out"),
        }
    return _validated(raw)


def _hash(name: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": "ns"}


def _passthrough(name: str) -> dict[str, Any]:
    return {"name": name, "strategy": "passthrough"}


def _write(tmp_path: Path, table: pa.Table, name: str = "in") -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


# ---------------------------------------------------------------------------
# Test 1: temporal types, default knobs
# ---------------------------------------------------------------------------

_DATES = [dt.date(2020, 1, d) for d in (1, 2, 3)]
_TEMPORAL: dict[str, pa.Array] = {
    "date32": pa.array(_DATES, pa.date32()),
    "date64": pa.array(_DATES, pa.date64()),
    "time32_s": pa.array([dt.time(1, 1, i) for i in range(3)], pa.time32("s")),
    "time32_ms": pa.array([dt.time(1, 1, i) for i in range(3)], pa.time32("ms")),
    "time64_us": pa.array([dt.time(1, 1, i) for i in range(3)], pa.time64("us")),
    **{
        f"timestamp_{u}_tz": pa.array([1, 2, 3], pa.timestamp(u, "+05:30"))
        for u in ("s", "ms", "us", "ns")
    },
    "timestamp_us_utc": pa.array([1, 2, 3], pa.timestamp("us", "UTC")),
    **{f"duration_{u}": pa.array([1, 2, 3], pa.duration(u)) for u in ("s", "ms", "us")},
}


def _temporal_table(array: pa.Array, rows: int = 3) -> pa.Table:
    if rows != len(array):
        array = pa.concat_arrays([array] * (rows // len(array) + 1)).slice(0, rows)
    return pa.table({"s": pa.array([f"user{i}@example.com" for i in range(rows)]), "c": array})


@pytest.mark.parametrize("name", sorted(_TEMPORAL))
def test_temporal_column_default_knobs_matches_estimate_off(tmp_path: Path, name: str) -> None:
    table = _temporal_table(_TEMPORAL[name])
    config = _single_table_config(
        tmp_path, _write(tmp_path, table), [_hash("s"), _passthrough("c")]
    )
    off = _run(config, {"t": table}, use_byte_estimate_routing=False)
    on = _run(config, {"t": table})
    _assert_same_outputs(on, off)


def test_date32_at_100k_rows_the_original_report_shape(tmp_path: Path) -> None:
    table = _temporal_table(_TEMPORAL["date32"], rows=100_000)
    config = _single_table_config(
        tmp_path, _write(tmp_path, table), [_hash("s"), _passthrough("c")]
    )
    off = _run(config, {"t": table}, use_byte_estimate_routing=False)
    on = _run(config, {"t": table})
    _assert_same_outputs(on, off)


# ---------------------------------------------------------------------------
# Test 1b: every trigger shape from the plan's probes
# ---------------------------------------------------------------------------


def _date_table() -> pa.Table:
    return _temporal_table(_TEMPORAL["date32"])


def test_trigger_date_column_absent_from_the_config(tmp_path: Path) -> None:
    table = _date_table()
    config = _single_table_config(tmp_path, _write(tmp_path, table), [_hash("s")])
    _assert_same_outputs(
        _run(config, {"t": table}),
        _run(config, {"t": table}, use_byte_estimate_routing=False),
    )


def test_trigger_execution_mode_full_frame(tmp_path: Path) -> None:
    table = _date_table()
    config = _single_table_config(
        tmp_path, _write(tmp_path, table), [_hash("s"), _passthrough("c")]
    )
    _assert_same_outputs(
        _run(config, {"t": table}, execution_mode="full_frame"),
        _run(config, {"t": table}, execution_mode="full_frame", use_byte_estimate_routing=False),
    )


def test_trigger_generate_plus_mask_job(tmp_path: Path) -> None:
    table = _date_table()
    gen = {
        "name": "gen",
        "row_count": 5,
        "generate_columns": [{"name": "id", "type": "sequence", "start": 1}],
    }
    config = _single_table_config(
        tmp_path, _write(tmp_path, table), [_hash("s"), _passthrough("c")], [gen]
    )
    on = _run(config, {"t": table})
    off = _run(config, {"t": table}, use_byte_estimate_routing=False)
    _assert_same_outputs(on, off)


def test_trigger_fk_job_with_a_limit_transform_and_an_untransformed_date_child(
    tmp_path: Path,
) -> None:
    parent, child = fk_tables()
    child = child.append_column("d", pa.array([dt.date(2021, 1, 1)] * child.num_rows, pa.date32()))
    config = fk_config(tmp_path, parent, child, parent_transforms=[{"op": "limit", "n": 4}])
    sources = {"parent": parent, "child": child}
    _assert_same_outputs(
        _run(config, sources),
        _run(config, sources, use_byte_estimate_routing=False),
    )


# ---------------------------------------------------------------------------
# Test 2: the signal is actually read (FK pure-mask job, byte-estimate route)
# ---------------------------------------------------------------------------


def _faker_col(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": "ns",
    }


def _fk_pure_mask_with_temporal(
    tmp_path: Path, n: int = 20
) -> tuple[dict[str, Any], dict[str, Any]]:
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(n)]),
            "age": pa.array([str(i * 10) for i in range(n)]),
            "d": pa.array([dt.date(2020, 1, 1 + i % 20) for i in range(n)], pa.date32()),
            "tz": pa.array(list(range(n)), pa.timestamp("us", "+05:30")),
        }
    )
    child = pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(n)]),
            "parent_id": pa.array([f"p{i}" for i in range(n)]),
        }
    )
    paths = {
        "parent": _write(tmp_path, parent, "parent"),
        "child": _write(tmp_path, child, "child"),
    }
    config = _validated(
        {
            "version": 1,
            "global_settings": {"seed": 7},
            "sources": {
                k: {"type": "file", "path": str(v), "format": "parquet"} for k, v in paths.items()
            },
            "targets": {
                k: {"type": "file", "path": str(tmp_path / f"{k}.out.parquet"), "format": "parquet"}
                for k in paths
            },
            "tables": [
                {
                    "name": "parent",
                    "columns": [
                        _faker_col("id"),
                        {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
                        _passthrough("d"),
                        _passthrough("tz"),
                    ],
                },
                {"name": "child", "columns": [_faker_col("parent_id")]},
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
    )
    return config, {"parent": parent, "child": child}


def test_fk_pure_mask_with_temporal_columns_routes_by_the_byte_estimate(tmp_path: Path) -> None:
    config, sources = _fk_pure_mask_with_temporal(tmp_path)
    result = _run(config, sources, out_of_core_budget_bytes=1 * _GB)
    execution = result.quality_metrics["execution"]
    assert execution["route_reason"] == "byte_estimate_full_frame_fits"
    assert execution["execution_mode"] == "full_frame"


# ---------------------------------------------------------------------------
# Test 4: every catalogue type, end to end
# ---------------------------------------------------------------------------

# Types the engine itself refuses or cannot run with byte-estimate routing OFF
# (nothing for the estimator to be compared against). Separate issues, listed in
# the plan's Known issues; this set must not grow to hide an estimator crash.
_REFUSED_WITH_ESTIMATE_OFF = frozenset(
    {
        "time64_ns",
        "list",
        "large_list",
        "struct",
        "map",
        "dense_union",
        "sparse_union",
        "fixed_size_list",
        "fixed_shape_tensor",
        "dictionary_float16",
        "ree_string_view",
    }
)


def _catalogue_source(tmp_path: Path, name: str, array: pa.Array) -> Path:
    """Write the real column when Parquet can hold it, else a string placeholder so
    the profiler still has a file. The resident table carries the real type."""
    table = pa.table({"s": pa.array(list("abcde")), "c": array})
    try:
        return _write(tmp_path, table)
    except (pa.ArrowException, NotImplementedError):
        placeholder = pa.table({"s": table["s"], "c": pa.array(list("vwxyz"))})
        return _write(tmp_path, placeholder)


@pytest.mark.parametrize("has_null", [False, True])
@pytest.mark.parametrize("name", sorted(CATALOGUE))
def test_catalogue_type_default_knobs_matches_estimate_off(
    tmp_path: Path, name: str, has_null: bool
) -> None:
    array = CATALOGUE[name](has_null)
    table = pa.table({"s": pa.array(list("abcde")), "c": array})
    config = _single_table_config(
        tmp_path, _catalogue_source(tmp_path, name, array), [_hash("s"), _passthrough("c")]
    )
    knobs = {"auto_chunk_threshold_rows": 10}
    try:
        off = _run(config, {"t": table}, use_byte_estimate_routing=False, **knobs)
    except Exception:
        if name in _REFUSED_WITH_ESTIMATE_OFF:
            pytest.skip(f"{name}: refused by the engine with estimate routing off (known issue)")
        raise
    on = _run(config, {"t": table}, **knobs)
    _assert_same_outputs(on, off)


def _dictionary_of_string_view(chunks: int) -> pa.Array | pa.ChunkedArray:
    values = pa.array(["a", "cccc", "ee"], pa.string_view())
    indices = [
        pa.array([0, 1, 2, 1, 0], pa.int32()),
        pa.array([2, 2, 1, 0, 0], pa.int32()),
    ][:chunks]
    arrays = [pa.DictionaryArray.from_arrays(i, values) for i in indices]
    return arrays[0] if chunks == 1 else pa.chunked_array(arrays)


@pytest.mark.parametrize("chunks", [1, 2])
def test_dictionary_of_string_view_is_unpriceable_and_never_sampled(
    tmp_path: Path, chunks: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _mem_estimate_arrow
    from decoy_engine.execution import _mem_estimate_schema as schema_mod
    from decoy_engine.execution._pipeline_routing_signals import byte_estimate_full_frame_fits

    column = _dictionary_of_string_view(chunks)
    rows = len(column)
    table = pa.table({"s": pa.array([f"u{i}" for i in range(rows)]), "c": column})
    placeholder = pa.table({"s": table["s"], "c": pa.array(["v"] * rows)})
    path = _write(tmp_path, placeholder)
    config = _single_table_config(tmp_path, path, [_hash("s"), _passthrough("c")])

    cls = _mem_estimate_arrow.classify_column(column.type, has_nulls=False)
    assert type(cls).__name__ == "Unpriceable"

    sampled_types: list[pa.DataType] = []
    original = schema_mod.sample_average_string_bytes

    def recording(col: Any) -> float:
        sampled_types.append(col.type)
        return original(col)

    monkeypatch.setattr(schema_mod, "sample_average_string_bytes", recording)

    profile = profile_source(config, seed=5)
    assert (
        byte_estimate_full_frame_fits(
            profile, caller_sources={"t": table}, table_kinds={"t": "mask"}, budget_bytes=_GB
        )
        is None
    )
    assert not any(pa.types.is_dictionary(t) for t in sampled_types), sampled_types

    off = _run(config, {"t": table}, use_byte_estimate_routing=False)
    on = _run(config, {"t": table})
    _assert_same_outputs(on, off)


# ---------------------------------------------------------------------------
# Tests 11 and 12 through run_pipeline: late-null columns must not be priced at
# one byte. An FK pure-mask job whose estimate straddles an explicit budget.
# ---------------------------------------------------------------------------

_N = 50_000
_NULLS_FROM = 30_000


def _parent_table(with_nulls: bool) -> pa.Table:
    def late_null(i: int, value: Any) -> Any:
        return None if with_nulls and i >= _NULLS_FROM and i % 5 == 0 else value

    return pa.table(
        {
            "id": pa.array(range(_N), pa.int64()),
            "i8": pa.array([late_null(i, i % 100) for i in range(_N)], pa.int8()),
            "b": pa.array([late_null(i, i % 2 == 0) for i in range(_N)], pa.bool_()),
        }
    )


def _child_table() -> pa.Table:
    return pa.table(
        {"cid": pa.array(range(_N), pa.int64()), "pid": pa.array(range(_N), pa.int64())}
    )


def _fk_late_null_config(tmp_path: Path, parent: pa.Table, child: pa.Table) -> dict[str, Any]:
    paths = {
        "parent": tmp_path / "parent.parquet",
        "child": tmp_path / "child.parquet",
    }
    pq.write_table(parent, paths["parent"], row_group_size=10_000)
    pq.write_table(child, paths["child"], row_group_size=10_000)
    return _validated(
        {
            "version": 1,
            "global_settings": {"seed": 3},
            "sources": {
                k: {"type": "file", "format": "parquet", "path": str(v)} for k, v in paths.items()
            },
            "targets": {
                k: {"type": "file", "format": "parquet", "path": str(tmp_path / f"{k}.out")}
                for k in paths
            },
            "tables": [
                {
                    "name": "parent",
                    "columns": [_passthrough("id"), _passthrough("i8"), _passthrough("b")],
                },
                {"name": "child", "columns": [_passthrough("pid")]},
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "ns",
                }
            ],
        }
    )


def _late_null_budget() -> int:
    """A budget the all-narrow estimate fits and the nullable-wide estimate does not.
    float64 stands in for the 8-byte nullable price of the int8 and bool columns."""

    def specs(nullable: str, flag: str) -> list[TableSizeSpec]:
        parent = TableSizeSpec(
            "parent",
            _N,
            (
                ColumnSizeSpec("id", "int64"),
                ColumnSizeSpec("i8", nullable),
                ColumnSizeSpec("b", flag),
            ),
        )
        child = TableSizeSpec(
            "child", _N, (ColumnSizeSpec("cid", "int64"), ColumnSizeSpec("pid", "int64"))
        )
        return [parent, child]

    narrow = estimate_peak_bytes(specs("int8", "bool"), "full_frame").estimated_bytes
    wide = estimate_peak_bytes(specs("float64", "float64"), "full_frame").estimated_bytes
    assert narrow is not None and wide is not None and narrow < wide
    budget = int((narrow * 1.3 + wide * 1.3) / 2)
    assert fits(specs("int8", "bool"), "full_frame", budget) is True
    assert fits(specs("float64", "float64"), "full_frame", budget) is False
    return budget


@pytest.mark.parametrize("source_kind", ["lazy", "resident"])
def test_late_null_columns_are_not_admitted_to_full_frame_by_a_one_byte_price(
    tmp_path: Path, source_kind: str
) -> None:
    budget = _late_null_budget()
    parent, child = _parent_table(with_nulls=True), _child_table()
    config = _fk_late_null_config(tmp_path, parent, child)
    sources: dict[str, Any] = (
        {n: LazySource(tmp_path / f"{n}.parquet") for n in ("parent", "child")}
        if source_kind == "lazy"
        else {"parent": parent, "child": child}
    )
    result = _run(config, sources, out_of_core_budget_bytes=budget, use_probe_routing=False)
    assert result.quality_metrics["execution"]["execution_mode"] != "full_frame"


@pytest.mark.parametrize("source_kind", ["lazy", "resident"])
def test_the_same_job_without_nulls_still_fits_full_frame(tmp_path: Path, source_kind: str) -> None:
    """Widening is conditional: with no nulls anywhere (statistics count zero on the
    lazy table, null_count is zero on the resident one) the narrow price stands."""
    budget = _late_null_budget()
    parent, child = _parent_table(with_nulls=False), _child_table()
    config = _fk_late_null_config(tmp_path, parent, child)
    sources: dict[str, Any] = (
        {n: LazySource(tmp_path / f"{n}.parquet") for n in ("parent", "child")}
        if source_kind == "lazy"
        else {"parent": parent, "child": child}
    )
    result = _run(config, sources, out_of_core_budget_bytes=budget, use_probe_routing=False)
    execution = result.quality_metrics["execution"]
    assert execution["execution_mode"] == "full_frame"
    assert execution["route_reason"] == "byte_estimate_full_frame_fits"
