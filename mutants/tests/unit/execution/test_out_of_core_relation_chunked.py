"""C1: chunk-bounded parent key relation build.

The relation builder must not materialize a Python list or Arrow staging table
sized by the whole parent table; it streams bounded batches into DuckDB, which
owns the last-write-wins dedup with on-disk spill. These tests pin the three
things a batched rewrite can break: correctness across batch boundaries (global
row numbering and cross-batch dedup order), the residency bound itself
(per-batch masking and lazy reader consumption on both public entry points),
and the empty/all-null staging schema.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._fk_keys import fk_join_key_tuple
from decoy_engine.execution.out_of_core import _relation as relation_mod
from decoy_engine.execution.out_of_core._relation import (
    _relation_staging_batches,
    build_parent_key_relation,
    build_parent_key_relation_from_tables,
)
from decoy_engine.kernel import hash_array
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge

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


def _edge() -> RelationshipEdge:
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


def _sliced_masked_batch_fn(masked: pa.Table, parent_columns: tuple[str, ...]):
    def masked_batch_fn(
        start: int, key_arrays: list[pa.Array], keep_mask: pa.BooleanArray
    ) -> list[pa.Array]:
        length = len(key_arrays[0])
        return [
            masked.column(col).slice(start, length).combine_chunks().filter(keep_mask)
            for col in parent_columns
        ]

    return masked_batch_fn


def test_small_batch_relation_matches_single_batch(tmp_path) -> None:
    # Duplicate key "c1" appears in the first and last batch under batch_rows=2;
    # nulls are interleaved. The batched build must dedup across batches and skip
    # nulls exactly as a single-shot build does.
    parent = pa.table(
        {
            "customer_id": ["c1", "c2", None, "c3", "c1", "c4", "c2"],
            "payload": ["a", "b", "n", "c", "d", "e", "f"],
        }
    )
    full = build_parent_key_relation(
        plan=_plan(), parent=parent, edge=_edge(), temp_dir=tmp_path / "full"
    )
    chunked = build_parent_key_relation(
        plan=_plan(), parent=parent, edge=_edge(), temp_dir=tmp_path / "chunked", batch_rows=2
    )

    expected_keys = ["c1", "c2", "c3", "c4"]
    masked = hash_array(pa.array(expected_keys), seed=_SEED, namespace="cust").to_pylist()
    expected = {fk_join_key_tuple((c,)): m for c, m in zip(expected_keys, masked, strict=True)}

    assert _pairs(full.path) == expected
    assert _pairs(chunked.path) == expected


def test_cross_batch_dedup_keeps_latest_masked_value(tmp_path) -> None:
    # Same source key "c1" in the first batch (row 0) and the last batch (row 6)
    # under batch_rows=2, with DIFFERENT pre-masked values. Hash masking cannot
    # catch a dedup-order regression (duplicates mask identically), so this pins
    # last-write-wins across batch boundaries: the LATER row's value must win.
    source = pa.table({"customer_id": ["c1", "c2", "c3", "c4", "c5", "c6", "c1"]})
    masked = pa.table({"customer_id": ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "ZZZ"]})

    relation = build_parent_key_relation_from_tables(
        source_parent=source,
        masked_parent=masked,
        edge=_edge(),
        temp_dir=tmp_path,
        batch_rows=2,
    )

    pairs = _pairs(relation.path)
    assert pairs[fk_join_key_tuple(("c1",))] == "ZZZ"
    assert pairs[fk_join_key_tuple(("c2",))] == "BBB"
    assert len(pairs) == 6


def test_plan_path_masks_per_batch(tmp_path, monkeypatch) -> None:
    # The Tier-1 invariant inside the plan-aware entry point: every hash_array
    # call must be bounded by batch_rows. A whole-column pre-mask would surface
    # as one call sized by total parent cardinality.
    lengths: list[int] = []
    real_hash_array = relation_mod.hash_array

    def spy(values, **kwargs):
        lengths.append(len(values))
        return real_hash_array(values, **kwargs)

    monkeypatch.setattr(relation_mod, "hash_array", spy)
    parent = pa.table({"customer_id": ["c1", "c2", None, "c3", "c1", "c4", "c2"]})

    relation = build_parent_key_relation(
        plan=_plan(), parent=parent, edge=_edge(), temp_dir=tmp_path, batch_rows=2
    )

    assert lengths
    assert max(lengths) <= 2

    expected_keys = ["c1", "c2", "c3", "c4"]
    masked = hash_array(pa.array(expected_keys), seed=_SEED, namespace="cust").to_pylist()
    expected = {fk_join_key_tuple((c,)): m for c, m in zip(expected_keys, masked, strict=True)}
    assert _pairs(relation.path) == expected


def test_from_tables_streams_reader_into_duckdb(tmp_path, monkeypatch) -> None:
    # Pins laziness through the public entry point: DuckDB must be handed the
    # lazy RecordBatchReader (never a materialized staging table) and must pull
    # batches during the COPY, not before. Deterministic by construction; a
    # 2M-row empirical residency probe during review measured ~16MB Arrow delta
    # vs ~47MB of inputs, so no slow large-row test is needed here.
    n, batch_rows = 20, 4
    source = pa.table({"customer_id": [f"id{i}" for i in range(n)]})
    masked = pa.table({"customer_id": [f"m{i}" for i in range(n)]})

    pulled: dict[str, Any] = {"count": 0, "at_register": None, "registered": None}
    real_batches = relation_mod._relation_staging_batches

    def counting_batches(**kwargs):
        def gen():
            for batch in real_batches(**kwargs):
                pulled["count"] += 1
                yield batch

        return gen()

    monkeypatch.setattr(relation_mod, "_relation_staging_batches", counting_batches)

    real_connect = relation_mod.connect_duckdb

    class _SpyConn:
        def __init__(self, inner) -> None:
            self._inner = inner

        def register(self, name, obj):
            pulled["registered"] = obj
            pulled["at_register"] = pulled["count"]
            return self._inner.register(name, obj)

        def execute(self, *args, **kwargs):
            return self._inner.execute(*args, **kwargs)

        def close(self) -> None:
            self._inner.close()

    monkeypatch.setattr(
        relation_mod, "connect_duckdb", lambda **kwargs: _SpyConn(real_connect(**kwargs))
    )

    relation = build_parent_key_relation_from_tables(
        source_parent=source,
        masked_parent=masked,
        edge=_edge(),
        temp_dir=tmp_path,
        batch_rows=batch_rows,
    )

    assert isinstance(pulled["registered"], pa.RecordBatchReader)
    assert pulled["at_register"] == 0
    assert pulled["count"] == math.ceil(n / batch_rows)
    assert len(_pairs(relation.path)) == n


def test_staging_batches_are_bounded_and_complete() -> None:
    n = 50
    ids = [f"id{i}" for i in range(n)]
    ids[10] = None  # one null: dropped, not a relation row
    source = pa.table({"customer_id": ids})
    masked = source.set_column(
        0,
        "customer_id",
        hash_array(source.column("customer_id").combine_chunks(), seed=_SEED, namespace="cust"),
    )

    batches = list(
        _relation_staging_batches(
            source_parent=source,
            parent_columns=("customer_id",),
            masked_columns=("__decoy_masked_key",),
            masked_types=(pa.string(),),
            masked_batch_fn=_sliced_masked_batch_fn(masked, ("customer_id",)),
            batch_rows=8,
        )
    )

    # More than one batch (streamed), none larger than batch_rows, and the total
    # equals the non-null row count. Global row numbers are strictly increasing.
    assert len(batches) > 1
    assert all(b.num_rows <= 8 for b in batches)
    assert sum(b.num_rows for b in batches) == n - 1
    row_nrs = [v for b in batches for v in b.column("__decoy_row_nr").to_pylist()]
    assert row_nrs == sorted(row_nrs)
    assert 10 not in row_nrs  # the null-key row is excluded by global index


def test_empty_parent_builds_string_masked_column(tmp_path) -> None:
    # Intermediate-schema note: the plan-aware build now declares the masked
    # staging type up front (always string for hash), so an empty parent yields
    # a string-typed masked column where the old build let DuckDB cast the
    # null-typed staging to int32. Benign for consumers (mask_child_fk joins by
    # key), but pinned here so the type does not drift silently.
    empty = pa.table({"customer_id": pa.array([], type=pa.string())})

    relation = build_parent_key_relation(
        plan=_plan(), parent=empty, edge=_edge(), temp_dir=tmp_path
    )

    out = pq.read_table(relation.path)
    assert out.num_rows == 0
    assert out.schema.field("__decoy_masked_key").type == pa.string()


def test_all_null_parent_builds_string_masked_column(tmp_path) -> None:
    parent = pa.table({"customer_id": pa.array([None, None, None], type=pa.string())})

    relation = build_parent_key_relation(
        plan=_plan(), parent=parent, edge=_edge(), temp_dir=tmp_path
    )

    out = pq.read_table(relation.path)
    assert out.num_rows == 0
    assert out.schema.field("__decoy_masked_key").type == pa.string()


def _composite_edge() -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="customers",
        parent_columns=("customer_id", "region_id"),
        child_table="orders",
        child_columns=("customer_id", "region_id"),
        namespace="cust",
        orphan_policy=OrphanPolicy.FAIL,
    )


def test_composite_dedup_winner_with_null_masked_member_is_row_consistent(tmp_path) -> None:
    # Regression for the spillable-dedup rewrite (fix/ooc-spillable-dedup): a
    # DUPLICATE composite parent key whose LAST-WRITE-WINS winner has a NULL in
    # ONE of its masked members. The parity generators only use unique parent
    # keys, so nothing else pins this. The dedup must emit BOTH masked columns
    # from the SAME winning row (row 2), NULL member included -- never fall back
    # to an older row's non-null value for the NULL column. A naive per-column
    # `arg_max(col, row_nr)` would SKIP the NULL and stitch row 0's "B0" onto
    # row 2's "A2", a silent composite-key parity bug; the staged max()+join-back
    # form (like the old struct_pack form) cannot, because masked columns ride
    # the join back as ordinary columns keyed on the globally-unique row_nr.
    source = pa.table(
        {
            "customer_id": ["a", "c", "a"],
            "region_id": ["b", "d", "b"],
        }
    )
    masked = pa.table(
        {
            # Row 2 is the later duplicate of composite key ("a", "b") and so
            # wins; its region_id masks to NULL.
            "customer_id": ["A0", "C", "A2"],
            "region_id": pa.array(["B0", "D", None], type=pa.string()),
        }
    )

    relation = build_parent_key_relation_from_tables(
        source_parent=source,
        masked_parent=masked,
        edge=_composite_edge(),
        temp_dir=tmp_path,
        batch_rows=2,  # split the two duplicates across batch boundaries
    )

    out = pq.read_table(relation.path)
    rows = {
        key: (mk0, mk1)
        for key, mk0, mk1 in zip(
            out.column("__decoy_fk_join_key").to_pylist(),
            out.column("__decoy_masked_key").to_pylist(),
            out.column("__decoy_masked_key_1").to_pylist(),
            strict=True,
        )
    }
    assert len(rows) == 2
    # The winning row 2's values for both columns, NULL member preserved.
    assert rows[fk_join_key_tuple(("a", "b"))] == ("A2", None)
    assert rows[fk_join_key_tuple(("c", "d"))] == ("C", "D")
