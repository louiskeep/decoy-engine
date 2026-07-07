"""C3c primitive 2: parent key relation built from a streamed source.

`build_parent_key_relation` must accept a `LazySource` (or any iterable of
RecordBatches) in place of a resident `pa.Table` and stream it through the
same staging/dedup path. Relation row order is nondeterministic by design
(DuckDB owns the dedup), so parity is content equality: the join_key ->
masked_key mapping must be identical to the in-memory build. The residency
bound must hold on the streamed path too: masking runs per batch, and an
oversized input batch is re-sliced rather than masked whole.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution.out_of_core import LazySource
from decoy_engine.execution.out_of_core import _relation as relation_mod
from decoy_engine.execution.out_of_core._relation import build_parent_key_relation
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed

_SEED = b"\x00" * 8


def _hash_col(namespace: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _plan() -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "customers",
                    TableSeed(per_column=(("customer_id", _hash_col("cust")),), per_group=()),
                ),
            ),
        )
    )


def _edge():
    from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

    return RelationshipEdge(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="cust",
        orphan_policy=OrphanPolicy.FAIL,
    )


def _pairs(path) -> dict:
    out = pq.read_table(path)
    return dict(
        zip(
            out.column("__decoy_fk_join_key").to_pylist(),
            out.column("__decoy_masked_key").to_pylist(),
            strict=True,
        )
    )


# Nulls interleaved and the duplicate key "c1" split across streamed batches
# under batch_rows=2, so cross-batch dedup and null skipping are both exercised.
_PARENT = pa.table(
    {
        "customer_id": ["c1", "c2", None, "c3", "c1", None, "c4", "c2"],
        "payload": ["a", "b", "n", "c", "d", "n2", "e", "f"],
    }
)


def test_lazy_source_relation_content_matches_in_memory(tmp_path) -> None:
    path = tmp_path / "parent.parquet"
    pq.write_table(_PARENT, path)

    in_memory = build_parent_key_relation(
        plan=_plan(), parent=_PARENT, edge=_edge(), temp_dir=tmp_path / "mem", batch_rows=2
    )
    streamed = build_parent_key_relation(
        plan=_plan(),
        parent=LazySource(path=path),
        edge=_edge(),
        temp_dir=tmp_path / "lazy",
        batch_rows=2,
    )

    expected = _pairs(in_memory.path)
    assert len(expected) == 4
    assert _pairs(streamed.path) == expected


def test_record_batch_iterable_relation_content_matches_in_memory(tmp_path) -> None:
    in_memory = build_parent_key_relation(
        plan=_plan(), parent=_PARENT, edge=_edge(), temp_dir=tmp_path / "mem", batch_rows=2
    )
    streamed = build_parent_key_relation(
        plan=_plan(),
        parent=iter(_PARENT.to_batches(max_chunksize=3)),
        edge=_edge(),
        temp_dir=tmp_path / "iter",
        batch_rows=2,
    )

    assert _pairs(streamed.path) == _pairs(in_memory.path)


def test_lazy_source_missing_parent_column_raises_before_streaming(tmp_path) -> None:
    path = tmp_path / "parent.parquet"
    pq.write_table(pa.table({"wrong_column": ["c1"]}), path)

    with pytest.raises(ExecutionError) as exc:
        build_parent_key_relation(
            plan=_plan(),
            parent=LazySource(path=path),
            edge=_edge(),
            temp_dir=tmp_path / "lazy",
        )

    assert exc.value.code == "out_of_core_parent_column_missing"


def test_streamed_relation_masks_per_batch(tmp_path, monkeypatch) -> None:
    # The Tier-1 residency invariant on the streamed path: every hash_array call
    # is bounded by batch_rows even when the input arrives as one big batch, so
    # an oversized upstream batch must be re-sliced, never masked whole.
    lengths: list[int] = []
    real_hash_array = relation_mod.hash_array

    def spy(values, **kwargs):
        lengths.append(len(values))
        return real_hash_array(values, **kwargs)

    monkeypatch.setattr(relation_mod, "hash_array", spy)
    one_big_batch = _PARENT.combine_chunks().to_batches()
    assert len(one_big_batch) == 1

    relation = build_parent_key_relation(
        plan=_plan(),
        parent=iter(one_big_batch),
        edge=_edge(),
        temp_dir=tmp_path,
        batch_rows=2,
    )

    assert lengths
    assert max(lengths) <= 2
    assert len(_pairs(relation.path)) == 4
