"""Out-of-core parity for the `null_bearing_int_unsupported` ingest guard.

Every other route rejects a masked integer column that holds a null under
truncate/hash/categorical. The out-of-core FK route must reject the same input
with the same code, on resident and lazy (Parquet) sources, and must not
publish anything when it rejects.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import PandasExecutionAdapter, ParquetTransactionalSink
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core import LazySource, _null_int_guard, run_fk_out_of_core
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_CODE = "null_bearing_int_unsupported"
_REJECTED = ("truncate", "categorical")


def _seed(strategy: str, namespace: str | None = None) -> ColumnSeed:
    config: tuple[tuple[str, Any], ...] = ()
    if strategy == "truncate":
        config = (("length", 3),)
    elif strategy == "categorical":
        config = (("categories", ("a", "b", "c")),)
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=config,
        coherent_with=(),
    )


def _job(
    payload_strategy: str, *, payload_in: str = "child", key_strategy: str = "hash"
) -> tuple[Any, RelationshipGraph]:
    key = _seed(key_strategy, "ns_k")
    pay = _seed(payload_strategy, "ns_pay" if payload_strategy in ("hash", "categorical") else None)
    child_cols = (("fk", key), ("n", pay)) if payload_in == "child" else (("fk", key),)
    parent_cols = (("pk", key),) if payload_in == "child" else (("pk", key), ("n", pay))
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x11" * 8,
            per_table=(
                ("parent", TableSeed(per_column=parent_cols, per_group=())),
                ("child", TableSeed(per_column=child_cols, per_group=())),
            ),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="ns_k",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    return plan, graph


def _keys(n: int) -> list[str]:
    return [f"k{i}" for i in range(n)]


def _sources(values: list[Any], arrow_type: pa.DataType) -> dict[str, pa.Table]:
    n = len(values)
    return {
        "parent": pa.table({"pk": pa.array(_keys(n), type=pa.string())}),
        "child": pa.table(
            {
                "fk": pa.array(_keys(n), type=pa.string()),
                "n": pa.array(values, type=arrow_type),
            }
        ),
    }


def _ooc_code(plan: Any, sources: Any, graph: Any, **kw: Any) -> str | None:
    try:
        run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph, **kw)
    except (
        Exception
    ) as exc:  # other-route failures (e.g. float canonicalization) are not this guard's
        return getattr(exc, "code", type(exc).__name__)
    return None


def test_t1_direct_runner_rejects_and_matches_full_frame_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    plan, graph = _job("truncate")
    sources = _sources([12, None, 34], pa.int64())
    with pytest.raises(ExecutionError) as full:
        PandasExecutionAdapter().run(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            namespace_registry=NamespaceRegistry(bindings=()),
        )
    assert full.value.code == _CODE

    target = tmp_path / "published"
    sink = ParquetTransactionalSink(target)
    with pytest.raises(ExecutionError) as ooc:
        run_fk_out_of_core(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            sink=sink,
            temp_dir=tmp_path / "t",
        )
    assert ooc.value.code == _CODE
    assert not target.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("_decoy_stage_")]


@pytest.mark.parametrize("strategy", _REJECTED)
@pytest.mark.parametrize(
    "arrow_type", [pa.int8(), pa.int64(), pa.uint8(), pa.uint64()], ids=lambda t: str(t)
)
def test_t2_integer_with_null_rejected_under_every_rejected_strategy(
    strategy: str, arrow_type: pa.DataType
) -> None:
    plan, graph = _job(strategy)
    assert _ooc_code(plan, _sources([1, None, 3], arrow_type), graph) == _CODE


@pytest.mark.parametrize("strategy", _REJECTED)
def test_t2_typed_all_null_integer_rejected(strategy: str) -> None:
    plan, graph = _job(strategy)
    assert _ooc_code(plan, _sources([None, None], pa.int64()), graph) == _CODE


@pytest.mark.parametrize("strategy", ["redact", "passthrough"])
def test_t2_non_rejected_strategies_not_rejected_by_guard(strategy: str) -> None:
    plan, graph = _job(strategy)
    assert _ooc_code(plan, _sources([1, None, 3], pa.int64()), graph) != _CODE


@pytest.mark.parametrize("strategy", _REJECTED)
def test_t2_null_free_integer_and_string_payload_not_rejected(strategy: str) -> None:
    plan, graph = _job(strategy)
    assert _ooc_code(plan, _sources([1, 2, 3], pa.int64()), graph) is None
    assert _ooc_code(plan, _sources(["a", None, "c"], pa.string()), graph) != _CODE


@pytest.mark.parametrize("strategy", _REJECTED)
def test_t2_float_payload_gets_no_new_rejection_from_this_guard(strategy: str) -> None:
    plan, graph = _job(strategy)
    assert _ooc_code(plan, _sources([1.5, None, 3.5], pa.float64()), graph) != _CODE


def test_t2_fk_child_integer_with_null_is_exempt() -> None:
    # The FK-child key column is resolved through the edge, so an INTEGER null there
    # is not this guard's to reject. The key seed is categorical (a rejected strategy), so
    # without the FK-child exemption the guard would reject this column. Call the guard
    # directly: a full run rejects an integer FK key at the compat gate BEFORE the
    # guard, which would hide whether the exemption is load-bearing.
    # payload n is non-rejected; the key seed is categorical
    plan, graph = _job("passthrough", key_strategy="categorical")
    sources = {
        "parent": pa.table({"pk": pa.array([1, 2, 3], type=pa.int64())}),  # parent key: no null
        "child": pa.table(
            {
                "fk": pa.array([1, None, 3], type=pa.int64()),  # integer FK child WITH a null
                "n": pa.array([1, 2, 3], type=pa.int64()),
            }
        ),
    }
    work = order_work(build_work_list(plan, _REG), graph)
    # Exempt -> no raise. Removing the FK-child exemption makes this raise null_bearing_int.
    _null_int_guard.reject_null_bearing_int_ooc(work, sources, graph)


def test_t2_parent_side_non_fk_payload_also_rejected() -> None:
    plan, graph = _job("truncate", payload_in="parent")
    sources = {
        "parent": pa.table(
            {
                "pk": pa.array(_keys(3), type=pa.string()),
                "n": pa.array([5, None, 7], type=pa.int64()),
            }
        ),
        "child": pa.table({"fk": pa.array(_keys(3), type=pa.string())}),
    }
    assert _ooc_code(plan, sources, graph) == _CODE


def test_t4_clean_job_still_runs() -> None:
    plan, graph = _job("truncate")
    res = run_fk_out_of_core(
        plan, _sources([12, 33, 34], pa.int64()), registry=_REG, relationship_graph=graph
    )
    assert res.outputs["child"].num_rows == 3


# T5: lazy Parquet sources.


def _lazy(
    tmp_path: Path, values: list[Any], *, stats: bool, row_group_size: int = 4
) -> dict[str, Any]:
    n = len(values)
    parent = tmp_path / "parent.parquet"
    child = tmp_path / "child.parquet"
    pq.write_table(pa.table({"pk": pa.array(_keys(n), type=pa.string())}), parent)
    pq.write_table(
        pa.table(
            {
                "fk": pa.array(_keys(n), type=pa.string()),
                "n": pa.array(values, type=pa.int64()),
            }
        ),
        child,
        row_group_size=row_group_size,
        write_statistics=stats,
    )
    return {"parent": LazySource(parent), "child": LazySource(child)}


def test_t5a_lazy_with_footer_stats_rejects(tmp_path: Path) -> None:
    plan, graph = _job("truncate")
    sources = _lazy(tmp_path, [1, 2, None, 4, 5, 6], stats=True)
    assert sources["child"].column_null_counts()["n"] == 1
    assert _ooc_code(plan, sources, graph) == _CODE


def test_t5b_stats_disabled_late_null_beyond_first_batch_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shrink the scan batch so the late null lands in a LATER batch than the first:
    # a first-batch-only scan would miss it. Also assert more than one batch is read,
    # so a prefix-only implementation cannot pass.
    monkeypatch.setattr(_null_int_guard, "_SCAN_BATCH_ROWS", 8)
    seen_batches = 0
    real_iter = pq.ParquetFile.iter_batches

    def counting_iter(self: Any, *a: Any, **k: Any) -> Any:
        nonlocal seen_batches
        for batch in real_iter(self, *a, **k):
            seen_batches += 1
            yield batch

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", counting_iter)

    plan, graph = _job("truncate")
    values: list[Any] = list(range(40))
    values[37] = None  # 5th batch of 8 rows: well beyond the first batch
    sources = _lazy(tmp_path, values, stats=False)
    assert sources["child"].column_null_counts()["n"] is None
    assert _ooc_code(plan, sources, graph) == _CODE
    assert seen_batches > 1  # scanned past the first batch to find the null


def test_t5c_stats_disabled_null_free_control_is_not_rejected(tmp_path: Path) -> None:
    plan, graph = _job("truncate")
    sources = _lazy(tmp_path, list(range(40)), stats=False)
    assert sources["child"].column_null_counts()["n"] is None
    assert _ooc_code(plan, sources, graph) is None


def test_t5_unknown_stats_scan_never_materializes_the_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, graph = _job("truncate")
    values: list[Any] = list(range(40))
    values[37] = None
    sources = _lazy(tmp_path, values, stats=False)
    work = order_work(build_work_list(plan, _REG), graph)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("guard materialized a table")

    monkeypatch.setattr(LazySource, "to_table", boom)
    monkeypatch.setattr(pq.ParquetFile, "read", boom)
    monkeypatch.setattr(pq.ParquetFile, "read_row_group", boom)
    with pytest.raises(ExecutionError) as exc:
        _null_int_guard.reject_null_bearing_int_ooc(work, sources, graph)
    assert exc.value.code == _CODE


def test_t2_hash_over_integer_with_null_is_not_rejected() -> None:
    # Hash is keyed on the exact integer, so int+null is supported; only truncate and
    # categorical stay rejected.
    plan, graph = _job("hash")
    assert _ooc_code(plan, _sources([1, None, 3], pa.int64()), graph) != _CODE
    assert _ooc_code(plan, _sources([None, None], pa.int64()), graph) != _CODE
