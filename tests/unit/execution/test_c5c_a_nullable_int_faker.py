"""Deterministic Faker over integer columns that hold nulls.

pandas widens an Arrow integer column with a null to float64, and canonicalization
refuses a float. The fix hands the Faker handler the exact Arrow values, so each
source value maps to the same fake value it gets in a null-free column.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import run_mask_pipeline_chunked
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import PandasExecutionAdapter, run_pipeline
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._strategies._faker import FakerStrategyHandler
from decoy_engine.generation.pool import _sampler
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._errors import GenerationError
from tests.unit.execution import _c5c_a_support as sup

pytestmark = pytest.mark.filterwarnings("ignore")

_BIG = 2**53 + 1

_CASES: list[tuple[str, pa.DataType, list[int]]] = [
    ("int8", pa.int8(), [1, -5, 127, -128]),
    ("int16", pa.int16(), [300, -300, 32767]),
    ("int32", pa.int32(), [70000, -70000, 2**31 - 1]),
    ("int64", pa.int64(), [1, -1, _BIG, _BIG + 2, 2**62, -(2**62)]),
    ("uint8", pa.uint8(), [0, 7, 255]),
    ("uint64", pa.uint64(), [5, 2**63 + 5, 2**64 - 1, 2**63]),
]


def _with_nulls(values: list[int], dtype: pa.DataType) -> list[int | None]:
    out: list[int | None] = [None]
    for v in values:
        out += [v, None, v]
    return out


def _plan(**kw: Any) -> Any:
    return sup.plan_of({"n": sup.faker_seed(**kw)})


# 1. Works on the whole-frame route -----------------------------------------------


@pytest.mark.parametrize(("name", "dtype", "values"), _CASES, ids=[c[0] for c in _CASES])
def test_whole_frame_maps_each_value_like_a_null_free_column(
    name: str, dtype: pa.DataType, values: list[int]
) -> None:
    nullable = _with_nulls(values, dtype)
    got = sup.column(sup.run(_plan(), pa.table({"n": pa.array(nullable, type=dtype)})))
    clean = sup.column(sup.run(_plan(), pa.table({"n": pa.array(values, type=dtype)})))
    expected = dict(zip(values, clean, strict=True))
    assert [None if v is None else expected[v] for v in nullable] == got
    assert all(g is None for g, v in zip(got, nullable, strict=True) if v is None)


def test_values_past_2_53_key_exactly() -> None:
    # 2**53 + 1 and 2**53 + 2 collapse to one float64; they must stay distinct keys.
    values: list[int | None] = [_BIG, None, _BIG + 1, _BIG + 2, _BIG + 3, _BIG + 4]
    table = pa.table({"n": pa.array(values, type=pa.int64())})
    got = sup.column(sup.run(_plan(pool_size=4096), table))
    clean = sup.column(
        sup.run(
            _plan(pool_size=4096),
            pa.table({"n": pa.array([v for v in values if v is not None], type=pa.int64())}),
        )
    )
    assert [g for g in got if g is not None] == clean
    assert len(set(clean)) == 5


class _SpyKernel:
    """Wraps the compiled index kernel and records the Arrow types it was given."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.types: list[pa.DataType] = []

    def derive_index_batch(self, values: Any, **kw: Any) -> Any:
        self.types.append(values.type)
        return self._inner.derive_index_batch(values, **kw)


def _compiled_or_skip() -> Any:
    kernel = _sampler._compiled_index_kernel()
    if kernel is None:
        pytest.skip("compiled index kernel not available")
    return kernel


@pytest.mark.parametrize(("name", "dtype", "values"), _CASES, ids=[c[0] for c in _CASES])
def test_compiled_and_reference_index_paths_agree(
    name: str, dtype: pa.DataType, values: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    compiled = _compiled_or_skip()
    table = pa.table({"n": pa.array(_with_nulls(values, dtype), type=dtype)})
    spy = _SpyKernel(compiled)
    monkeypatch.setattr(_sampler, "_COMPILED_INDEX_KERNEL", spy)
    with_compiled = sup.column(sup.run(_plan(), table))
    # Evidence that the compiled kernel really ran for widths it admits.
    if name != "uint64":
        assert spy.types, "the compiled kernel was never called"
    monkeypatch.setattr(_sampler, "_COMPILED_INDEX_KERNEL", None)
    reference = sup.column(sup.run(_plan(), table))
    assert with_compiled == reference


def test_sampler_sees_exact_values_at_both_kernel_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _compiled_or_skip()
    ref = _sampler._reference_index_kernel()
    compiled_seen: list[list[Any]] = []
    ref_seen: list[list[Any]] = []

    class _Capture:
        def __init__(self, inner: Any, sink: list[list[Any]]) -> None:
            self._inner, self._sink = inner, sink

        def derive_index_batch(self, values: Any, **kw: Any) -> Any:
            self._sink.append(values.to_pylist() if isinstance(values, pa.Array) else list(values))
            return self._inner.derive_index_batch(values, **kw)

    values = [_BIG, None, _BIG + 1, 2**62]
    exact = [_BIG, _BIG + 1, 2**62]
    table = pa.table({"n": pa.array(values, type=pa.int64())})
    monkeypatch.setattr(_sampler, "_COMPILED_INDEX_KERNEL", _Capture(compiled, compiled_seen))
    sup.run(_plan(), table)
    assert compiled_seen == [exact]

    monkeypatch.setattr(_sampler, "_COMPILED_INDEX_KERNEL", None)
    monkeypatch.setattr(_sampler, "_REFERENCE_INDEX_KERNEL", _Capture(ref, ref_seen))
    sup.run(_plan(), table)
    wide = [2**64 - 1, None, 2**63 + 5]
    sup.run(_plan(), pa.table({"n": pa.array(wide, type=pa.uint64())}))
    assert ref_seen == [exact, [2**64 - 1, 2**63 + 5]]


# 1. Works on the chunked oracle route ---------------------------------------------


def _config(path: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
    return PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {"t": {"type": "file", "format": "parquet", "path": path}},
            "tables": [{"name": "t", "columns": columns}],
            "targets": {"t": {"type": "file", "format": "parquet", "path": "/dev/null"}},
        }
    ).model_dump()


def _faker_cfg(name: str = "n") -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": f"{name}_ns",
        "cardinality_mode": "reuse",
        "provider_config": {"pool_size": 64},
    }


def _chunks(table: pa.Table, size: int) -> list[pa.Table]:
    return [table.slice(i, size) for i in range(0, table.num_rows, size)]


def _chunked(tmp_path: Path, table: pa.Table, size: int) -> list[Any]:
    pq.write_table(table, tmp_path / "in.parquet")
    cfg = _config(str(tmp_path / "in.parquet"), [_faker_cfg()])
    out = run_mask_pipeline_chunked(cfg, _chunks(table, size), table="t", engine_version="c5c-a")
    return [v for chunk in out for v in chunk.column("n").to_pylist()]


def _whole(tmp_path: Path, table: pa.Table) -> pa.Table:
    pq.write_table(table, tmp_path / "in.parquet")
    cfg = _config(str(tmp_path / "in.parquet"), [_faker_cfg()])
    return run_pipeline(cfg, sources={"t": table}, engine_version="c5c-a").outputs["t"]


def test_chunked_oracle_matches_whole_frame_when_only_some_chunks_hold_nulls(
    tmp_path: Path,
) -> None:
    # Chunks of 3: [1,2,3] [None,5,6] [7,8,9] [None,None,None] [_BIG, 11, None]
    values = [1, 2, 3, None, 5, 6, 7, 8, 9, None, None, None, _BIG, 11, None]
    table = pa.table({"n": pa.array(values, type=pa.int64())})
    chunked = _chunked(tmp_path, table, 3)
    whole = _whole(tmp_path, table)
    assert chunked == whole.column("n").to_pylist()
    assert chunked[3] is None


def test_chunked_native_entry_falls_back_to_the_oracle_for_int_faker(tmp_path: Path) -> None:
    from decoy_engine.execution.native._chunked_entry import run_mask_chunked

    values = [1, 2, None, 4, 5, None]
    table = pa.table({"n": pa.array(values, type=pa.int64())})
    pq.write_table(table, tmp_path / "in.parquet")
    cfg = _config(str(tmp_path / "in.parquet"), [_faker_cfg()])
    out = pa.concat_tables(
        list(run_mask_chunked(cfg, _chunks(table, 2), table="t", engine_version="c5c-a"))
    )
    assert out.column("n").to_pylist() == _whole(tmp_path, table).column("n").to_pylist()


def test_numeric_output_drift_between_chunks_is_still_rejected_on_concat(
    tmp_path: Path,
) -> None:
    # Known limit: a numeric-output provider yields int64 for a chunk with no null and double
    # for one with a null, and the chunk concatenation refuses to promote. Sampling cannot fix it.
    from decoy_engine.execution._chunked import concat_masked_chunks
    from decoy_engine.execution._errors import ExecutionError

    table = pa.table({"n": pa.array([1, 2, 3, None], type=pa.int64())})
    pq.write_table(table, tmp_path / "in.parquet")
    column = {**_faker_cfg(), "provider": "address_zip"}
    cfg = _config(str(tmp_path / "in.parquet"), [column])
    out = list(
        run_mask_pipeline_chunked(
            cfg,
            _chunks(table, 2),
            table="t",
            engine_version="c5c-a",
            registry=sup.int_registry(),
        )
    )
    assert [o.schema.field("n").type for o in out] == [pa.int64(), pa.float64()]
    with pytest.raises(ExecutionError) as exc:
        concat_masked_chunks(out, table="t")
    assert exc.value.code == "chunked_schema_mismatch"


# 1. Works on the sequential route -------------------------------------------------


def _fk_job(tmp_path: Path, n_values: list[int | None]) -> tuple[dict[str, Any], dict[str, Any]]:
    ids = [f"p{i}" for i in range(len(n_values))]
    parent = pa.table(
        {"id": pa.array(ids), "n": pa.array(n_values, type=pa.int64())},
    )
    child = pa.table({"cid": pa.array([f"c{i}" for i in range(len(ids))]), "pid": pa.array(ids)})
    paths = {}
    for name, tbl in (("parent", parent), ("child", child)):
        paths[name] = str(tmp_path / f"{name}.parquet")
        pq.write_table(tbl, paths[name])
    id_col = {
        "name": "id",
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": "p_ns",
        "provider_config": {"pool_size": 64},
    }
    cfg = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {k: {"type": "file", "path": v, "format": "parquet"} for k, v in paths.items()},
        "targets": {
            k: {"type": "file", "path": str(tmp_path / f"{k}.out.parquet"), "format": "parquet"}
            for k in paths
        },
        "tables": [
            {"name": "parent", "columns": [id_col, _faker_cfg("n")]},
            {"name": "child", "columns": [{**id_col, "name": "pid"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "preserve",
                "namespace": "p_ns",
            }
        ],
    }
    return cfg, {"parent": parent, "child": child}


def _run_sequential(cfg: dict[str, Any], sources: dict[str, pa.Table], sink: Any = None) -> Any:
    from tests.unit.execution.test_sequential_run_kills import compile_job

    plan, graph, ns, registry = compile_job(cfg)
    return PandasExecutionAdapter().run_sequential(
        plan,
        lambda t: sources[t],
        registry=registry,
        relationship_graph=graph,
        namespace_registry=ns,
        sink=sink,
    )


def test_sequential_route_masks_an_unrelated_nullable_int_faker_column(tmp_path: Path) -> None:
    values: list[int | None] = [1, None, 3, _BIG, None, 3]
    cfg, sources = _fk_job(tmp_path, values)
    seq = _run_sequential(cfg, sources).outputs["parent"].column("n").to_pylist()
    ref_cfg, ref_sources = _fk_job(tmp_path, [v for v in values if v is not None])
    clean = _run_sequential(ref_cfg, ref_sources).outputs["parent"].column("n").to_pylist()
    expected = dict(zip([v for v in values if v is not None], clean, strict=True))
    assert seq == [None if v is None else expected[v] for v in values]


def test_sequential_route_releases_the_references_with_the_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, sources = _fk_job(tmp_path, [1, None, 3, 4])
    held: dict[str, Any] = {}
    seen: dict[str, int] = {}
    orig = PandasExecutionAdapter._dispatch_mask_node

    def spy(self: Any, node: Any, *args: Any, **kw: Any) -> Any:
        ctx = args[-1]
        held["ctx"] = ctx
        seen[node.table] = len(ctx.exact_int_sources)
        return orig(self, node, *args, **kw)

    monkeypatch.setattr(PandasExecutionAdapter, "_dispatch_mask_node", spy)
    written: dict[str, pa.Table] = {}
    _run_sequential(cfg, sources, sink=lambda name, tbl: written.__setitem__(name, tbl))
    assert seen["parent"] == 1
    # The parent's frame is evicted before the child runs; its buffers go with it.
    assert seen["child"] == 0
    assert held["ctx"].exact_int_sources == {}


def test_two_tables_with_the_same_column_name_keep_separate_sources() -> None:
    from types import SimpleNamespace

    from decoy_engine.plan._types import SeedEnvelope, TableSeed

    seed = sup.faker_seed()
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=sup.SEED,
            per_table=tuple(
                (name, TableSeed(per_column=(("n", seed),), per_group=())) for name in ("a", "b")
            ),
        )
    )
    a_vals: list[int | None] = [1, None, 2]
    b_vals: list[int | None] = [None, 2, 1]
    out = PandasExecutionAdapter().run(
        plan,
        {
            "a": pa.table({"n": pa.array(a_vals, type=pa.int64())}),
            "b": pa.table({"n": pa.array(b_vals, type=pa.int64())}),
        },
        registry=sup.REG,
        relationship_graph=sup.GRAPH,
        namespace_registry=sup.NS,
    )
    a = out.outputs["a"].column("n").to_pylist()
    b = out.outputs["b"].column("n").to_pylist()
    assert a[1] is None and b[0] is None
    assert a[0] == b[2] and a[2] == b[1]


# 7. Float sources unchanged -------------------------------------------------------


@pytest.mark.parametrize("values", [[1.5, None, 2.5], [1.0, None, 2.0], [1.0, 2.0, 3.0]])
def test_true_float_sources_still_raise(values: list[float | None]) -> None:
    table = pa.table({"n": pa.array(values, type=pa.float64())})
    with pytest.raises(GenerationError) as exc:
        sup.run(_plan(), table)
    assert exc.value.code == "float_canonicalization_unsupported"


# 8b. Edge columns that main already handles ---------------------------------------


def test_all_null_and_empty_int_columns_still_succeed() -> None:
    for arr in (pa.array([None, None, None], type=pa.int64()), pa.array([], type=pa.int64())):
        out = sup.column(sup.run(_plan(), pa.table({"n": arr})))
        assert out == [None] * len(arr)


def test_null_free_int_column_matches_main_dtype() -> None:
    out = sup.run(_plan(), pa.table({"n": pa.array([1, 2, 3], type=pa.int64())}))
    assert out.outputs["t"].schema.field("n").type == pa.string()


def test_all_rows_gate_selecting_only_null_rows_keeps_nulls() -> None:
    table = pa.table(
        {"n": pa.array([None, 4, None], type=pa.int64()), "f": pa.array([1, 0, 1])},
    )
    plan = sup.plan_of({"n": sup.faker_seed(when="f == 1"), "f": sup.seed_of("passthrough")})
    out = sup.run(plan, table)
    assert sup.column(out)[0] is None and sup.column(out)[2] is None


# 6. Wiring guard ------------------------------------------------------------------


def _ctx(sources: dict[tuple[str, str], pa.ChunkedArray], **kw: Any) -> StrategyContext:
    return StrategyContext(
        registry=sup.REG,
        pool_cache=PoolCache(),
        relationship_graph=sup.GRAPH,
        namespace_registry=sup.NS,
        job_seed=sup.SEED,
        current_table="t",
        exact_int_sources=sources,
        **kw,
    )


def test_null_mask_mismatch_raises_a_coded_error() -> None:
    df = pd.DataFrame({"n": [1.0, None, 3.0]})
    wrong = pa.chunked_array([pa.array([1, 2, None], type=pa.int64())])
    with pytest.raises(GenerationError) as exc:
        FakerStrategyHandler().run(df, "n", sup.faker_seed(), _ctx({("t", "n"): wrong}))
    assert exc.value.code == "exact_int_null_mask_mismatch"


def test_length_mismatch_raises_a_coded_error() -> None:
    df = pd.DataFrame({"n": [1.0, None, 3.0]})
    short = pa.chunked_array([pa.array([1, None], type=pa.int64())])
    with pytest.raises(GenerationError) as exc:
        FakerStrategyHandler().run(df, "n", sup.faker_seed(), _ctx({("t", "n"): short}))
    assert exc.value.code == "exact_int_null_mask_mismatch"


def test_handler_uses_gate_positions_for_a_subset_frame() -> None:
    exact = pa.chunked_array([pa.array([10, None, 30, 40], type=pa.int64())])
    sub = pd.DataFrame({"n": [30.0, 40.0]}, index=[2, 3])
    got, _ = FakerStrategyHandler().run(
        sub.copy(),
        "n",
        sup.faker_seed(),
        _ctx({("t", "n"): exact}, gate_positions=pa_positions([2, 3])),
    )
    full = pa.table({"n": pa.array([30, 40], type=pa.int64())})
    expected = sup.column(sup.run(_plan(), full))
    assert got["n"].tolist() == expected


def pa_positions(values: list[int]) -> Any:
    import numpy as np

    return np.asarray(values, dtype=np.intp)


# Which columns the adapter registers ----------------------------------------------


def _node(column: str, seed: Any, *, kind: str = "scalar", columns: tuple[str, ...] | None = None):
    from decoy_engine.execution._runner import WorkNode

    return WorkNode(
        table="t",
        columns=columns or (column,),
        kind=kind,
        strategy=seed.strategy,
        provider=seed.provider,
        plan_slice=seed,
    )


def _registered(arrow: pa.Array, *, frame: pd.DataFrame | None = None, seed: Any = None) -> bool:
    from decoy_engine.execution._exact_int_faker import exact_int_faker_sources

    table = pa.table({"n": arrow})
    frame = table.to_pandas() if frame is None else frame
    node = _node("n", seed or sup.faker_seed())
    return ("t", "n") in exact_int_faker_sources("t", table, frame, [node])


def test_only_a_widened_nullable_integer_column_is_registered() -> None:
    assert _registered(pa.array([1, None, 3], type=pa.int64()))
    assert _registered(pa.array([1, None], type=pa.uint8()))
    # Already exact in the frame, or nothing to key.
    assert not _registered(pa.array([1, 2, 3], type=pa.int64()))
    assert not _registered(pa.array([None, None], type=pa.int64()))
    assert not _registered(pa.array([], type=pa.int64()))
    nullable = pa.table({"n": pa.array([1, None], type=pa.int64())}).to_pandas()
    assert not _registered(
        pa.array([1, None], type=pa.int64()), frame=nullable.astype({"n": "Int64"})
    )
    # Not an integer source, or not a deterministic Faker node.
    assert not _registered(pa.array([1.5, None], type=pa.float64()))
    assert not _registered(pa.array(["a", None]))
    assert not _registered(
        pa.array([1, None], type=pa.int64()), seed=sup.faker_seed(deterministic=False)
    )
    assert not _registered(pa.array([1, None], type=pa.int64()), seed=sup.seed_of("redact"))


def test_a_nondeterministic_faker_ignores_registered_exact_values() -> None:
    df = pd.DataFrame({"n": [1.0, None, 3.0]})
    exact = pa.chunked_array([pa.array([1, None, 3], type=pa.int64())])
    seed = sup.faker_seed(deterministic=False)
    with_exact, _ = FakerStrategyHandler().run(df.copy(), "n", seed, _ctx({("t", "n"): exact}))
    without, _ = FakerStrategyHandler().run(df.copy(), "n", seed, _ctx({}))
    assert with_exact["n"].tolist() == without["n"].tolist()


def test_registration_follows_the_real_order_for_a_multi_column_writer() -> None:
    from decoy_engine.execution._exact_int_faker import exact_int_faker_sources

    table = pa.table({"a": pa.array(["x", "y"]), "n": pa.array([1, None], type=pa.int64())})
    frame = table.to_pandas()
    faker = _node("n", sup.faker_seed())
    writer = _node("a", sup.seed_of("composite"), kind="composite", columns=("a", "n"))
    assert exact_int_faker_sources("t", table, frame, [writer, faker]) == {}
    assert list(exact_int_faker_sources("t", table, frame, [faker, writer])) == [("t", "n")]


def _fk_job(
    tmp_path: Path, child_pids: list[int | None], child_n: list[int | None]
) -> dict[str, Any]:
    """Parent/child job: deterministic Faker on the FK child column and on a nullable-int column."""
    parent = pa.table({"id": pa.array([1, 2, 3], type=pa.int64())})
    child = pa.table(
        {
            "cid": pa.array(range(len(child_pids)), type=pa.int64()),
            "pid": pa.array(child_pids, type=pa.int64()),
            "n": pa.array(child_n, type=pa.int64()),
        }
    )

    def col(name: str, ns: str) -> dict[str, Any]:
        return {
            "name": name,
            "strategy": "faker",
            "provider": "person_email",
            "deterministic": True,
            "namespace": ns,
            "cardinality_mode": "reuse",
            "provider_config": {"pool_size": 64},
        }

    paths = {}
    for name, table in (("parent", parent), ("child", child)):
        paths[name] = str(tmp_path / f"{name}.parquet")
        pq.write_table(table, paths[name])
    cfg = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                k: {"type": "file", "path": v, "format": "parquet"} for k, v in paths.items()
            },
            "targets": {
                k: {"type": "file", "path": v + ".out", "format": "parquet"}
                for k, v in paths.items()
            },
            "tables": [
                {"name": "parent", "columns": [col("id", "p_ns")]},
                {"name": "child", "columns": [col("pid", "p_ns"), col("n", "n_ns")]},
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "p_ns",
                }
            ],
        }
    ).model_dump()
    result = run_pipeline(cfg, sources={"parent": parent, "child": child}, engine_version="x")
    return {name: table.to_pydict() for name, table in result.outputs.items()}


def test_fk_child_column_is_untouched_and_a_nullable_sibling_now_works(tmp_path: Path) -> None:
    # The FK child `pid` converts FK-safe (never float64), so it is not an exact-int source and
    # its resolution is unchanged; the nullable `n` beside it now masks like its null-free copy.
    with_null_n = _fk_job(tmp_path / "a", [1, None, 3], [5, None, 7])
    null_free_n = _fk_job(tmp_path / "b", [1, None, 3], [5, 6, 7])
    assert with_null_n["child"]["pid"] == null_free_n["child"]["pid"]
    assert with_null_n["child"]["n"][1] is None
    assert with_null_n["child"]["n"][0] == null_free_n["child"]["n"][0]
    assert with_null_n["child"]["n"][2] == null_free_n["child"]["n"][2]


# Nested child calls never read a real column's exact integers ---------------------------------


def _leaves_ctx(source: pa.ChunkedArray) -> StrategyContext:
    """A context registering a real column whose name collides with nested dispatch's leaf name."""
    return _ctx({("t", "_nested_leaves"): source}, nested_outer_column="payload")


def test_nested_leaves_ignore_a_same_named_source_with_a_different_null_mask() -> None:
    leaves = pd.DataFrame({"_nested_leaves": ["alice", "bob", None]})
    source = pa.chunked_array([pa.array([1, None, 3], type=pa.int64())])
    out, _ = FakerStrategyHandler().run(
        leaves.copy(), "_nested_leaves", sup.faker_seed(), _leaves_ctx(source)
    )
    assert out["_nested_leaves"].iloc[2] is None


def test_nested_leaves_key_on_their_own_values_when_the_null_masks_match() -> None:
    # Identical leaf values must map identically; keyed from [1, None, 3] they would not.
    leaves = pd.DataFrame({"_nested_leaves": ["alice", None, "alice"]})
    source = pa.chunked_array([pa.array([1, None, 3], type=pa.int64())])
    out, _ = FakerStrategyHandler().run(
        leaves.copy(), "_nested_leaves", sup.faker_seed(), _leaves_ctx(source)
    )
    plain, _ = FakerStrategyHandler().run(
        leaves.copy(), "_nested_leaves", sup.faker_seed(), _ctx({}, nested_outer_column="payload")
    )
    assert out["_nested_leaves"].iloc[0] == out["_nested_leaves"].iloc[2]
    assert out["_nested_leaves"].to_list() == plain["_nested_leaves"].to_list()
