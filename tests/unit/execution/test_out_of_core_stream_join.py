"""OOC-B fix#1: the single-streaming-join FK joiner (`_stream_join.py`).

`StreamFkJoiner` replaces `ChildFkBatchJoiner`'s per-child-batch join against a
materialized parent TEMP TABLE with ONE streamed `LEFT JOIN child_keys x
parent_keys(read_parquet VIEW) ORDER BY __decoy_row_nr` per edge (the
`_join.py::mask_child_fk` shape), so no O(distinct-parent-key) structure stays
resident. The oracle is the whole-child resident path `mask_child_fk`:
the streamed joiner's output, after the same resident narrowing
`_emit.assemble_resident` applies, must be byte-identical to it (types and
values), and `mask_child_fk` is itself parity-pinned to the pandas oracle.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution._fk_keys import fk_join_key_tuple
from decoy_engine.execution.out_of_core import _runner as runner_mod
from decoy_engine.execution.out_of_core import _stream_join as stream_join_mod
from decoy_engine.execution.out_of_core._emit import _fk_resident_column
from decoy_engine.execution.out_of_core._join import mask_child_fk
from decoy_engine.execution.out_of_core._mask import mask_table
from decoy_engine.execution.out_of_core._relation import (
    ParentKeyRelation,
    build_parent_key_relation_from_tables,
)
from decoy_engine.execution.out_of_core._stream_join import JoinRowCursor, StreamFkJoiner
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


def _stream_joiner(plan, edge, relation, temp_dir, child):
    return StreamFkJoiner(
        edge=edge,
        parent_relation=relation,
        child_key_types=tuple(child.column(col).type for col in edge.child_columns),
        temp_dir=temp_dir,
        remap_seeds=_remap_seeds(plan, edge),
        job_seed=plan.seed_envelope.job_seed,
    )


def _whole_child_oracle(plan, edge, relation, child, temp_dir) -> pa.Table:
    """The whole-child resident path (`mask_child_fk`), the gold oracle.

    It carries VALUE-derived column types (the documented narrowing), so the
    streamed joiner's FIXED-schema output is compared to it only AFTER the same
    resident narrowing `_emit.assemble_resident` applies.
    """
    remap_values = (
        runner_mod._remap_values(plan, edge, child)
        if edge.orphan_policy is OrphanPolicy.REMAP
        else None
    )
    out, _warnings = mask_child_fk(
        child=child,
        edge=edge,
        parent_relation=relation,
        temp_dir=temp_dir,
        remap_values=remap_values,
    )
    return out


def _stream_output(
    plan, edge, relation, child, temp_dir, *, batch_rows: int = _BATCH
) -> tuple[pa.Table, pa.Table, int, tuple]:
    """Run the streamed joiner over the whole child.

    Returns (fixed_table, narrowed_table, orphan_total, output_types). The
    FIXED table keeps the schema-fixed FK types (what a Parquet sink writes);
    the NARROWED table applies `_fk_resident_column` (exactly what the resident
    runner does) to recover the value-derived type for oracle comparison.
    `iter_join_rows` yields raw join rows in global row_nr order, which is
    source order; each reader batch is resolved directly (whole-child test
    helper, no payload-store alignment needed here), so concatenating in
    iteration order restores the child's rows.
    """
    with _stream_joiner(plan, edge, relation, temp_dir, child) as joiner:
        joiner.stage_keys(child.to_batches(max_chunksize=batch_rows))
        fk_chunks: list[list[pa.Array]] = [[] for _ in edge.child_columns]
        for join_rows in joiner.iter_join_rows(batch_rows):
            fk_arrays = joiner.resolve_batch(join_rows)
            for idx, array in enumerate(fk_arrays):
                fk_chunks[idx].append(array)
        orphans = joiner.orphan_total
        output_types = joiner.output_types
        observed = joiner.observed_types
    fixed = child
    narrowed = child
    for idx, col in enumerate(edge.child_columns):
        chunks = fk_chunks[idx]
        fixed_col = pa.concat_arrays(chunks) if chunks else pa.array([], type=output_types[idx])
        narrowed_col = _fk_resident_column(chunks, observed[idx]).combine_chunks()
        fixed = _set_col(fixed, col, fixed_col)
        narrowed = _set_col(narrowed, col, narrowed_col)
    return fixed, narrowed, orphans, output_types


def _set_col(table: pa.Table, name: str, array: pa.Array) -> pa.Table:
    idx = table.schema.get_field_index(name)
    return table.set_column(idx, name, array)


def _assert_stream_matches_oracle(
    plan, edge, parent, child, tmp_path
) -> tuple[pa.Table, int, tuple]:
    """The streamed joiner's narrowed output is byte-identical to `mask_child_fk`."""
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    oracle = _whole_child_oracle(plan, edge, relation, child, tmp_path / "oracle")
    fixed, narrowed, orphans, output_types = _stream_output(
        plan, edge, relation, child, tmp_path / "stream"
    )
    assert narrowed.schema == oracle.schema
    assert narrowed.equals(oracle)
    return fixed, orphans, output_types


_CROSS = ["c2", "missing1", None, "c1", "missing2", "c2", None, "missing1", "c1", "c2"]
_CLEAN = ["c2", "c1", None, "c1", "c2", "c2", None, "c1", "c1", "c2"]


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.WARN, OrphanPolicy.REMAP])
def test_single_edge_matches_batch_joiner(tmp_path, policy: OrphanPolicy) -> None:
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(policy)
    parent = pa.table({"customer_id": ["c1", "c2"]})
    child = pa.table({"customer_id": _CROSS, "amount": list(range(len(_CROSS)))})
    _tbl, orphans, _types = _assert_stream_matches_oracle(plan, edge, parent, child, tmp_path)
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
    _tbl, orphans, _types = _assert_stream_matches_oracle(
        plan, edge, parent, child, tmp_path / "cmp"
    )
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
    _assert_stream_matches_oracle(plan, edge, parent, child, tmp_path / "cmp")


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
    _assert_stream_matches_oracle(plan, edge, parent, child, tmp_path)
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
    _tbl, orphans, _types = _assert_stream_matches_oracle(plan, edge, parent, child, tmp_path)
    assert _tbl.column("customer_id").type == pa.float64()
    assert orphans == 5


def test_all_null_fk_child_column_keeps_fixed_type(tmp_path) -> None:
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": ["c1"]})
    child = pa.table({"customer_id": pa.array([None, None], type=pa.null())})
    tbl, orphans, _types = _assert_stream_matches_oracle(plan, edge, parent, child, tmp_path)
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
        out = list(joiner.iter_join_rows(_BATCH))
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
    _assert_stream_matches_oracle(plan, edge, parent, child, tmp_path / "cmp")


# ---------------------------------------------------------------------------
# resolve_batch: the relocated per-slice resolution kernel (Task 2)
# ---------------------------------------------------------------------------


def _bool_preserve_joiner_with_join_rows(tmp_path):
    """A bool parent covering only False, joined against 2 matched + 2 orphan
    child rows (the Codex HIGH shape). Returns `(joiner, [matched_rows,
    orphan_rows])`, each a SEPARATE 2-row raw join-row batch -- the same
    source-chunk granularity the redesigned driver resolves at (never one
    coalesced 4-row batch mixing a real bool with an fk_key_value-normalized
    int, which even the whole-child oracle cannot resolve as one unit; see
    `mask_child_fk` raising `out_of_core_fk_key_dtype_unsupported` for that
    exact combined shape).
    """
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("customers", TableSeed(per_column=(("id", _col("passthrough")),), per_group=())),
                ("orders", TableSeed(per_column=(("id", _col("passthrough")),), per_group=())),
            ),
        )
    )
    edge = RelationshipEdge(
        parent_table="customers",
        parent_columns=("id",),
        child_table="orders",
        child_columns=("id",),
        namespace="cust_bool",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    parent = pa.table({"id": pa.array([False, False], type=pa.bool_())})
    child = pa.table({"id": pa.array([False, False, True, True], type=pa.bool_())})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    joiner = _stream_joiner(plan, edge, relation, tmp_path / "s", child)
    joiner.stage_keys(child.to_batches(max_chunksize=2))
    # batch_rows=2 caps the join reader at 2 rows per batch, matching the
    # driver's real usage: it resolves per payload-store batch (source-chunk
    # sized), never per whatever size DuckDB's own reader happens to coalesce.
    join_row_batches = list(joiner.iter_join_rows(2))
    assert [b.num_rows for b in join_row_batches] == [2, 2]
    return joiner, join_row_batches


def test_resolve_batch_mixed_bool_int_preserve(tmp_path) -> None:
    """Each payload-aligned slice resolves to the FIXED int64 type (the
    bool/int64 special case in `_fixed_schema_typing.py`), even though one
    slice is purely bool-valued and the other purely int-valued: concatenating
    the two slices' resolved arrays reproduces the oracle's [0, 0, 1, 1] --
    the HIGH repro, now succeeding because resolution runs per source-chunk
    slice, not per coalesced join-reader batch.
    """
    joiner, (matched_rows, orphan_rows) = _bool_preserve_joiner_with_join_rows(tmp_path)
    try:
        (matched_fk,) = joiner.resolve_batch(matched_rows)
        (orphan_fk,) = joiner.resolve_batch(orphan_rows)
    finally:
        joiner.close()
    assert matched_fk.type == pa.int64()
    assert orphan_fk.type == pa.int64()
    assert matched_fk.to_pylist() + orphan_fk.to_pylist() == [0, 0, 1, 1]


def test_resolve_batch_genuinely_irreconcilable_mix_fails_closed(tmp_path) -> None:
    """A single slice that mixes a matched float parent value with an orphan
    integer child key beyond exactly-representable float precision (> 2**53)
    cannot be resolved as one array by ANY chunking -- the whole-child oracle
    raises on this exact combination too (`_append_output_batch`'s own
    fail-closed guard). `resolve_batch` must raise the same coded error, not
    silently drift or crash uncoded.
    """
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("customers", TableSeed(per_column=(("id", _col("passthrough")),), per_group=())),
                ("orders", TableSeed(per_column=(("id", _col("passthrough")),), per_group=())),
            ),
        )
    )
    edge = RelationshipEdge(
        parent_table="customers",
        parent_columns=("id",),
        child_table="orders",
        child_columns=("id",),
        namespace="cust_bigfloat",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    parent = pa.table({"id": pa.array([1.0], type=pa.float64())})
    # Row 0 matches (float 1.0); row 1 is a whole-float orphan beyond 2**53,
    # in the SAME 2-row slice so resolve_batch sees the mix in one call.
    child = pa.table({"id": pa.array([1.0, 9007199254740994.0], type=pa.float64())})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    joiner = _stream_joiner(plan, edge, relation, tmp_path / "s", child)
    try:
        joiner.stage_keys(child.to_batches(max_chunksize=2))
        (join_rows,) = list(joiner.iter_join_rows(2))
        assert join_rows.num_rows == 2
        with pytest.raises(ExecutionError) as exc:
            joiner.resolve_batch(join_rows)
        assert exc.value.code == "out_of_core_fk_key_dtype_unsupported"
    finally:
        joiner.close()


# ---------------------------------------------------------------------------
# JoinRowCursor: the single-read identity-guarded forward zip primitive (Task 3)
# ---------------------------------------------------------------------------

_JOIN_COLS = ("__decoy_row_nr", "customer_id")


def _join_row_batch(start: int, values: list[object]) -> pa.RecordBatch:
    """One iter_join_rows-shaped batch: contiguous row_nr + one raw column."""
    n = len(values)
    return pa.record_batch(
        [
            pa.array(range(start, start + n), type=pa.int64()),
            pa.array(values, type=pa.string()),
        ],
        schema=pa.schema(
            [pa.field("__decoy_row_nr", pa.int64()), pa.field("customer_id", pa.string())]
        ),
    )


def _join_reader(*batches: pa.RecordBatch):
    return iter(batches)


def test_join_row_cursor_slices_across_reader_batches() -> None:
    # Reader yields 100-row batches; the cursor is asked for 40 then 90 then 70:
    # takes must slice across batch boundaries and conserve every row in order.
    vals = [f"v{i}" for i in range(200)]
    r = _join_reader(_join_row_batch(0, vals[:100]), _join_row_batch(100, vals[100:]))
    cursor = JoinRowCursor(r, _JOIN_COLS)
    a = cursor.take(40, 0)
    b = cursor.take(90, 40)
    c = cursor.take(70, 130)
    assert a.column("customer_id").to_pylist() == vals[:40]
    assert b.column("customer_id").to_pylist() == vals[40:130]
    assert c.column("customer_id").to_pylist() == vals[130:200]
    cursor.assert_exhausted()


def test_join_row_cursor_identity_mismatch_fails_closed() -> None:
    # The payload store claims the next batch starts at row_nr 5, but the join
    # reader is still positioned at 0 -- a genuine cross-artifact disagreement,
    # not merely the join reader's own internal contiguity.
    cursor = JoinRowCursor(_join_reader(_join_row_batch(0, ["a", "b", "c"])), _JOIN_COLS)
    with pytest.raises(ExecutionError) as exc:
        cursor.take(3, 5)
    assert exc.value.code == "out_of_core_fk_row_alignment"


def test_join_row_cursor_early_exhaustion_raises_fail_closed() -> None:
    cursor = JoinRowCursor(_join_reader(_join_row_batch(0, ["a", "b"])), _JOIN_COLS)
    with pytest.raises(ExecutionError) as exc:
        cursor.take(3, 0)  # only 2 rows available
    assert exc.value.code == "out_of_core_fk_row_alignment"


def test_join_row_cursor_row_nr_gap_trips_guard() -> None:
    # Second batch starts at 100 but only 2 rows were emitted: a reorder/gap in
    # the ordered join output must trip the fail-closed guard, not silently
    # misalign the zip.
    cursor = JoinRowCursor(
        _join_reader(_join_row_batch(0, ["a", "b"]), _join_row_batch(100, ["c", "d"])),
        _JOIN_COLS,
    )
    cursor.take(2, 0)
    with pytest.raises(ExecutionError) as exc:
        cursor.take(2, 2)
    assert exc.value.code == "out_of_core_fk_row_alignment"


def test_join_row_cursor_non_contiguous_within_batch_trips_guard() -> None:
    bad = pa.record_batch(
        [
            pa.array([0, 2, 1], type=pa.int64()),  # not contiguous ascending
            pa.array(["a", "b", "c"], type=pa.string()),
        ],
        schema=pa.schema(
            [pa.field("__decoy_row_nr", pa.int64()), pa.field("customer_id", pa.string())]
        ),
    )
    cursor = JoinRowCursor(_join_reader(bad), _JOIN_COLS)
    with pytest.raises(ExecutionError) as exc:
        cursor.take(3, 0)
    assert exc.value.code == "out_of_core_fk_row_alignment"


def test_join_row_cursor_assert_exhausted_ok_and_detects_leftovers() -> None:
    cursor = JoinRowCursor(_join_reader(_join_row_batch(0, ["a", "b", "c"])), _JOIN_COLS)
    cursor.take(3, 0)
    cursor.assert_exhausted()  # fully drained, no raise
    leftover = JoinRowCursor(_join_reader(_join_row_batch(0, ["a", "b", "c"])), _JOIN_COLS)
    leftover.take(2, 0)
    with pytest.raises(ExecutionError) as exc:
        leftover.assert_exhausted()
    assert exc.value.code == "out_of_core_fk_row_alignment"


def test_join_row_cursor_multi_column() -> None:
    batch = pa.record_batch(
        [
            pa.array([0, 1, 2], type=pa.int64()),
            pa.array(["a", "b", "c"], type=pa.string()),
            pa.array([10, 20, 30], type=pa.int64()),
        ],
        schema=pa.schema(
            [
                pa.field("__decoy_row_nr", pa.int64()),
                pa.field("country", pa.string()),
                pa.field("account_id", pa.int64()),
            ]
        ),
    )
    cursor = JoinRowCursor(_join_reader(batch), ("country", "account_id"))
    out = cursor.take(2, 0)
    assert out.column("country").to_pylist() == ["a", "b"]
    assert out.column("account_id").to_pylist() == [10, 20]


def test_join_row_cursor_take_zero_does_not_consume() -> None:
    cursor = JoinRowCursor(_join_reader(_join_row_batch(0, ["a", "b"])), _JOIN_COLS)
    out = cursor.take(0, 0)
    assert out.num_rows == 0
    assert out.schema == _join_row_batch(0, ["a", "b"]).schema
    # A following non-zero take still starts at row 0 (the peek did not consume).
    assert cursor.take(2, 0).column("customer_id").to_pylist() == ["a", "b"]


# ---------------------------------------------------------------------------
# Migrated pinned sentinels (were in the removed test_out_of_core_batch_join.py)
# ---------------------------------------------------------------------------


def _hand_built_relation(tmp_path) -> ParentKeyRelation:
    """A parent relation with ONE key masking to null and one to a normal
    string, built directly so the masked-key column is string-typed with a null
    entry (an all-null masked column hits an orthogonal Parquet round-trip
    quirk). Isolates the match-vs-orphan sentinel from that concern.
    """
    join_keys = [fk_join_key_tuple(("c0",)), fk_join_key_tuple(("c1",))]
    masked = pa.array([None, "MASKED_C1"], type=pa.string())
    parent_table = pa.table({"__decoy_fk_join_key": join_keys, "__decoy_masked_key": masked})
    rel_path = tmp_path / "relation.parquet"
    rel_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(parent_table, rel_path)
    return ParentKeyRelation(path=rel_path)


def _stream_fk_column(joiner, child, batch_rows: int = _BATCH) -> tuple[list, int]:
    joiner.stage_keys(child.to_batches(max_chunksize=batch_rows))
    values: list = []
    for join_rows in joiner.iter_join_rows(batch_rows):
        (fk_array,) = joiner.resolve_batch(join_rows)
        values.extend(fk_array.to_pylist())
    return values, joiner.orphan_total


def test_masked_null_parent_key_is_not_treated_as_orphan(tmp_path) -> None:
    """A parent key that legitimately masks to null must resolve a MATCHED child
    row to that masked null value, not fall through to the orphan branch (the
    LEFT JOIN match indicator uses the join key's nullness, not the masked
    value's). Shared `_append_output_batch` sentinel, carried to the new joiner.
    """
    edge = _edge(OrphanPolicy.PRESERVE)
    relation = _hand_built_relation(tmp_path / "rel")
    child = pa.table({"customer_id": ["c0", "orphan1", "c1"], "amount": [1, 2, 3]})
    with StreamFkJoiner(
        edge=edge,
        parent_relation=relation,
        child_key_types=(pa.string(),),
        temp_dir=tmp_path / "join",
    ) as joiner:
        values, orphans = _stream_fk_column(joiner, child)
    assert values == [None, "orphan1", "MASKED_C1"]
    assert orphans == 1


def test_masked_null_parent_key_fail_policy_does_not_false_positive(tmp_path) -> None:
    """Same repro under FAIL: every child key matches (one masked value is null),
    so the anti-join precount must not count the matched-but-null-masked row.
    """
    edge = _edge(OrphanPolicy.FAIL)
    relation = _hand_built_relation(tmp_path / "rel")
    child = pa.table({"customer_id": ["c0", "c1", "c0"], "amount": [1, 2, 3]})
    with StreamFkJoiner(
        edge=edge,
        parent_relation=relation,
        child_key_types=(pa.string(),),
        temp_dir=tmp_path / "join",
    ) as joiner:
        joiner.stage_keys(child.to_batches(max_chunksize=_BATCH))
        assert joiner.total_orphans() == 0
        values: list = []
        for join_rows in joiner.iter_join_rows(_BATCH):
            (fk_array,) = joiner.resolve_batch(join_rows)
            values.extend(fk_array.to_pylist())
    assert values == [None, "MASKED_C1", None]


def test_string_masked_with_binary_child_key_rejected_at_construction(tmp_path) -> None:
    # A promotable mix outside {int64, float64}: string+binary would merge to
    # binary, but only when the data mixes; an all-string run would drift, so
    # the mix is rejected fail-closed at construction.
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": ["c1"]})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    child = pa.table({"customer_id": pa.array([b"c1", b"c2"], type=pa.binary())})
    with pytest.raises(ExecutionError) as exc:
        _stream_joiner(plan, edge, relation, tmp_path / "s", child)
    assert exc.value.code == "out_of_core_fk_key_dtype_unsupported"
