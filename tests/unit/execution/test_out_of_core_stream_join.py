"""OOC-B fix#1: the single-streaming-join FK joiner (`_stream_join.py`).

`StreamFkJoiner` replaces `ChildFkBatchJoiner`'s per-child-batch join against a
materialized parent TEMP TABLE with ONE streamed `LEFT JOIN child_keys x
parent_keys(read_parquet VIEW) ORDER BY __decoy_row_nr` per edge (the
`_join.py::mask_child_fk` shape), so no O(distinct-parent-key) structure stays
resident. The oracle for these unit tests is `ChildFkBatchJoiner` itself: for
the same relation/edge/child, the streamed joiner's rebuilt output table,
orphan total, and fixed/observed types must equal the per-batch joiner's,
which the parity suite already pins against the whole-child pandas oracle.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution.out_of_core import _runner as runner_mod
from decoy_engine.execution.out_of_core import _stream_join as stream_join_mod
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._mask import mask_table
from decoy_engine.execution.out_of_core._relation import build_parent_key_relation_from_tables
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.plan._types import ColumnSeed, GroupSeed, SeedEnvelope, TableSeed
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

_SEED = b"\x00" * 8
_BATCH = 3


def _col(
    strategy: str,
    *,
    namespace: str | None = None,
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=provider_config,
        coherent_with=(),
    )


def _two_table_plan(seed: ColumnSeed, parent: str, child: str, column: str) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (parent, TableSeed(per_column=((column, seed),), per_group=())),
                (child, TableSeed(per_column=((column, seed),), per_group=())),
            ),
        )
    )


def _edge(policy: OrphanPolicy) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="cust",
        orphan_policy=policy,
    )


def _remap_seeds(plan: Any, edge: RelationshipEdge):
    if edge.orphan_policy is not OrphanPolicy.REMAP:
        return None
    return tuple(
        runner_mod._column_seed(plan, edge.parent_table, col) for col in edge.parent_columns
    )


def _relation_for(plan: Any, edge: RelationshipEdge, parent: pa.Table, temp_dir):
    masked_parent = mask_table(plan, edge.parent_table, parent, skip_columns=frozenset())
    return build_parent_key_relation_from_tables(
        source_parent=parent, masked_parent=masked_parent, edge=edge, temp_dir=temp_dir
    )


def _batch_joiner(plan, edge, relation, temp_dir, child):
    return ChildFkBatchJoiner(
        edge=edge,
        parent_relation=relation,
        child_key_types=tuple(child.column(col).type for col in edge.child_columns),
        temp_dir=temp_dir,
        remap_seeds=_remap_seeds(plan, edge),
        job_seed=plan.seed_envelope.job_seed,
    )


def _stream_joiner(plan, edge, relation, temp_dir, child):
    return StreamFkJoiner(
        edge=edge,
        parent_relation=relation,
        child_key_types=tuple(child.column(col).type for col in edge.child_columns),
        temp_dir=temp_dir,
        remap_seeds=_remap_seeds(plan, edge),
        job_seed=plan.seed_envelope.job_seed,
    )


def _batched_output(plan, edge, relation, child, temp_dir) -> tuple[pa.Table, int]:
    batches: list[pa.RecordBatch] = []
    orphans = 0
    with _batch_joiner(plan, edge, relation, temp_dir, child) as joiner:
        for batch in child.to_batches(max_chunksize=_BATCH):
            out, batch_orphans = joiner.join_batch(batch)
            batches.append(out)
            orphans += batch_orphans
        observed = tuple(frozenset(s) for s in joiner.observed_types)
        output_types = joiner.output_types
    return pa.Table.from_batches(batches), orphans, observed, output_types


def _stream_output(
    plan, edge, relation, child, temp_dir, *, batch_rows: int = _BATCH
) -> tuple[pa.Table, int]:
    """Rebuild the child table with FK columns replaced from the streamed join.

    `iter_output` yields FK output batches ordered by global `__decoy_row_nr`,
    which for a source read in row order is positional, so concatenating the FK
    columns in iteration order restores source order and can overwrite the
    child's FK columns directly (the whole-child equivalent the runner drives
    per-batch via a cursor).
    """
    with _stream_joiner(plan, edge, relation, temp_dir, child) as joiner:
        joiner.stage_keys(child.to_batches(max_chunksize=batch_rows))
        fk_chunks: list[list[pa.Array]] = [[] for _ in edge.child_columns]
        for out_batch in joiner.iter_output(batch_rows):
            for idx, col in enumerate(edge.child_columns):
                fk_chunks[idx].append(out_batch.column(col))
        orphans = joiner.orphan_total
        observed = tuple(frozenset(s) for s in joiner.observed_types)
        output_types = joiner.output_types
    result = child
    for idx, col in enumerate(edge.child_columns):
        arrays = fk_chunks[idx]
        merged = pa.concat_arrays(arrays) if arrays else pa.array([], type=output_types[idx])
        child_idx = result.schema.get_field_index(col)
        fields = list(result.schema)
        fields[child_idx] = pa.field(col, output_types[idx])
        cols = list(result.columns)
        cols[child_idx] = merged
        result = pa.table(
            {f.name: c for f, c in zip(fields, cols, strict=True)},
            schema=pa.schema(fields, metadata=result.schema.metadata),
        )
    return result, orphans, observed, output_types


def _assert_stream_matches_batch(plan, edge, parent, child, tmp_path) -> tuple[pa.Table, int]:
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    batch_tbl, batch_orphans, batch_obs, batch_types = _batched_output(
        plan, edge, relation, child, tmp_path / "batch"
    )
    stream_tbl, stream_orphans, stream_obs, stream_types = _stream_output(
        plan, edge, relation, child, tmp_path / "stream"
    )
    assert stream_types == batch_types
    assert stream_tbl.schema == batch_tbl.schema
    assert stream_tbl.equals(batch_tbl)
    assert stream_orphans == batch_orphans
    assert stream_obs == batch_obs
    return stream_tbl, stream_orphans


_CROSS = ["c2", "missing1", None, "c1", "missing2", "c2", None, "missing1", "c1", "c2"]
_CLEAN = ["c2", "c1", None, "c1", "c2", "c2", None, "c1", "c1", "c2"]


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.WARN, OrphanPolicy.REMAP])
def test_single_edge_matches_batch_joiner(tmp_path, policy: OrphanPolicy) -> None:
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(policy)
    parent = pa.table({"customer_id": ["c1", "c2"]})
    child = pa.table({"customer_id": _CROSS, "amount": list(range(len(_CROSS)))})
    _tbl, orphans = _assert_stream_matches_batch(plan, edge, parent, child, tmp_path)
    assert orphans == 3


def test_fail_policy_total_orphans_counts_whole_child(tmp_path) -> None:
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.FAIL)
    parent = pa.table({"customer_id": ["c1", "c2"]})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    # Two orphans in two different batches; total_orphans must see the whole
    # child, not just the first offending batch (the phase-1 FAIL precount).
    child = pa.table({"customer_id": ["c1", "missing1", "c2", "c1", "missing2", "c2"]})
    with _stream_joiner(plan, edge, relation, tmp_path / "stream", child) as joiner:
        joiner.stage_keys(child.to_batches(max_chunksize=_BATCH))
        assert joiner.total_orphans() == 2


def test_fail_policy_clean_child_matches_batch_joiner(tmp_path) -> None:
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.FAIL)
    parent = pa.table({"customer_id": ["c1", "c2"]})
    child = pa.table({"customer_id": _CLEAN, "amount": list(range(len(_CLEAN)))})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    with _stream_joiner(plan, edge, relation, tmp_path / "s", child) as joiner:
        joiner.stage_keys(child.to_batches(max_chunksize=_BATCH))
        assert joiner.total_orphans() == 0
    _tbl, orphans = _assert_stream_matches_batch(plan, edge, parent, child, tmp_path / "cmp")
    assert orphans == 0


@pytest.mark.parametrize(
    "policy",
    [OrphanPolicy.FAIL, OrphanPolicy.PRESERVE, OrphanPolicy.WARN, OrphanPolicy.REMAP],
)
def test_composite_fk_matches_batch_joiner(tmp_path, policy: OrphanPolicy) -> None:
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "accounts",
                    TableSeed(
                        per_column=(
                            ("country", _col("hash", namespace="country")),
                            ("account_id", _col("hash", namespace="acct")),
                        ),
                        per_group=(),
                    ),
                ),
                (
                    "transactions",
                    TableSeed(
                        per_column=(),
                        per_group=(
                            (
                                "account_id__country",
                                GroupSeed(
                                    namespace="acct_rel",
                                    coherent_columns=("country", "account_id"),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
    )
    edge = RelationshipEdge(
        parent_table="accounts",
        parent_columns=("country", "account_id"),
        child_table="transactions",
        child_columns=("country", "account_id"),
        namespace="acct_rel",
        orphan_policy=policy,
    )
    parent = pa.table({"country": ["US", "CA"], "account_id": ["a1", "a2"]})
    accounts = ["a2", "a1", "a1", "a1" if policy is OrphanPolicy.FAIL else "missing", "a2", "a1"]
    child = pa.table(
        {
            "country": ["CA", "US", None, "US", "CA", "US"],
            "account_id": accounts,
            "amount": list(range(6)),
        }
    )
    if policy is OrphanPolicy.FAIL:
        relation = _relation_for(plan, edge, parent, tmp_path / "rel")
        with _stream_joiner(plan, edge, relation, tmp_path / "s", child) as joiner:
            joiner.stage_keys(child.to_batches(max_chunksize=_BATCH))
            assert joiner.total_orphans() == 0
    _assert_stream_matches_batch(plan, edge, parent, child, tmp_path / "cmp")


def test_remap_mints_per_output_batch_not_per_child(tmp_path, monkeypatch) -> None:
    # Residency bound: REMAP orphan values are minted from each JOIN-OUTPUT
    # batch's own keys, so no kernel call is ever sized by total child
    # cardinality (the sink-path invariant carried over from ChildFkBatchJoiner).
    lengths: list[int] = []
    real_mask_column = stream_join_mod.mask_column

    def spy(values, *args, **kwargs):
        lengths.append(len(values))
        return real_mask_column(values, *args, **kwargs)

    monkeypatch.setattr(stream_join_mod, "mask_column", spy)

    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.REMAP)
    parent = pa.table({"customer_id": ["c1", "c2"]})
    child = pa.table({"customer_id": _CROSS})
    _assert_stream_matches_batch(plan, edge, parent, child, tmp_path)
    assert lengths
    assert max(lengths) <= _BATCH


def test_numeric_cross_batch_split_matches_batch_joiner(tmp_path) -> None:
    # The int/float split across join-output batches must land on the same
    # fixed float64 the batch joiner fixes up front (schema-derived, not
    # value-derived), so a streaming sink writer never sees a mid-stream type.
    plan = _two_table_plan(_col("passthrough"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": pa.array([1.0, 2.5], type=pa.float64())})
    child = pa.table(
        {"customer_id": pa.array([1.0, 3.0, 1.0, 4.0, 6.0, 7.0, 2.5, 8.5, 1.0], type=pa.float64())}
    )
    _tbl, orphans = _assert_stream_matches_batch(plan, edge, parent, child, tmp_path)
    assert _tbl.column("customer_id").type == pa.float64()
    assert orphans == 5


def test_all_null_fk_child_column_keeps_fixed_type(tmp_path) -> None:
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": ["c1"]})
    child = pa.table({"customer_id": pa.array([None, None], type=pa.null())})
    tbl, orphans = _assert_stream_matches_batch(plan, edge, parent, child, tmp_path)
    assert tbl.column("customer_id").type == pa.string()
    assert orphans == 0


def test_empty_child_yields_no_output(tmp_path) -> None:
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": ["c1"]})
    child = pa.table(
        {"customer_id": pa.array([], type=pa.string()), "amount": pa.array([], type=pa.int64())}
    )
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    with _stream_joiner(plan, edge, relation, tmp_path / "s", child) as joiner:
        joiner.stage_keys(child.to_batches(max_chunksize=_BATCH))
        assert joiner.total_orphans() == 0
        out = list(joiner.iter_output(_BATCH))
    assert out == []
    assert joiner.orphan_total == 0
    assert joiner.output_types == (pa.string(),)


def test_dtype_rejection_at_construction(tmp_path) -> None:
    # Fail-closed schema typing is inherited unchanged: a string masked parent
    # with a numeric child key cannot fix one Arrow type up front.
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": ["c1"]})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    child = pa.table({"customer_id": pa.array([1, 2], type=pa.int64())})
    with pytest.raises(ExecutionError) as exc:
        _stream_joiner(plan, edge, relation, tmp_path / "s", child)
    assert exc.value.code == "out_of_core_fk_key_dtype_unsupported"


def test_decimal_child_key_rejected_at_construction(tmp_path) -> None:
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": ["c1"]})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    child = pa.table({"customer_id": pa.array([Decimal("1.5")], type=pa.decimal128(4, 2))})
    with pytest.raises(ExecutionError) as exc:
        _stream_joiner(plan, edge, relation, tmp_path / "s", child)
    assert exc.value.code == "out_of_core_fk_key_dtype_unsupported"


@pytest.mark.parametrize(
    ("strategy", "provider_config"),
    [("redact", (("redact_with", "X"),)), ("truncate", (("length", 2),)), ("passthrough", ())],
)
@pytest.mark.parametrize("policy", [OrphanPolicy.FAIL, OrphanPolicy.REMAP])
def test_strategy_parent_matches_batch_joiner(
    tmp_path, strategy: str, provider_config, policy: OrphanPolicy
) -> None:
    seed = _col(strategy, provider_config=provider_config)
    plan = _two_table_plan(seed, "parents", "children", "parent_id")
    edge = RelationshipEdge(
        parent_table="parents",
        parent_columns=("parent_id",),
        child_table="children",
        child_columns=("parent_id",),
        namespace="parent_rel",
        orphan_policy=policy,
    )
    parent = pa.table({"parent_id": ["AB123", "CD456"]})
    ids = (
        ["CD456", "AB123", None, "AB123", "CD456", "CD456"]
        if policy is OrphanPolicy.FAIL
        else ["CD456", "missing", None, "AB123", "missing2", "CD456"]
    )
    child = pa.table({"parent_id": ids})
    if policy is OrphanPolicy.FAIL:
        relation = _relation_for(plan, edge, parent, tmp_path / "rel")
        with _stream_joiner(plan, edge, relation, tmp_path / "s", child) as joiner:
            joiner.stage_keys(child.to_batches(max_chunksize=_BATCH))
            assert joiner.total_orphans() == 0
    _assert_stream_matches_batch(plan, edge, parent, child, tmp_path / "cmp")
