"""C3d: batch-streaming out-of-core FK runner.

`run_fk_out_of_core` must run a relational FK job as a batch-streaming
pipeline: sources may be `LazySource`s (never whole-table resident on that
path), each table streams through per-batch masking and per-batch FK joins
into `TransactionalSink.write_batches` under one fixed schema, and the
in-memory (no-sink) result stays byte-identical to the whole-table pandas
oracle, including the value-derived FK column types the oracle battery pins.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ExecutionError, PandasExecutionAdapter, ParquetTransactionalSink
from decoy_engine.execution.out_of_core import LazySource, run_fk_out_of_core
from decoy_engine.execution.out_of_core import _join as join_mod
from decoy_engine.execution.out_of_core import _stream_driver as stream_driver_mod
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from tests.perf_fixtures.fk_relational import (
    lazy_sources,
    make_graph,
    make_plan,
    write_large_fk_chain,
)

_SEED = b"\x00" * 8
_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())


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


def _edge(policy: OrphanPolicy) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="cust",
        orphan_policy=policy,
    )


def test_lazy_source_sink_run_matches_resident_run_and_oracle(tmp_path) -> None:
    """A LazySource-fed FK chain through a ParquetTransactionalSink completes,
    publishes tables byte-equal to the resident-table (no-sink) run, matches
    the pandas oracle, and preserves FK integrity end to end."""
    rows = 1_000
    paths = write_large_fk_chain(tmp_path / "src", rows, width=2, orphan_frac=0.02, batch_rows=400)
    plan = make_plan()
    graph = make_graph(OrphanPolicy.WARN)
    resident = {name: pq.read_table(path) for name, path in paths.items()}

    in_memory = run_fk_out_of_core(
        plan,
        resident,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work-mem",
    )
    target = tmp_path / "published"
    streamed = run_fk_out_of_core(
        plan,
        lazy_sources(paths),
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-lazy",
    )
    pandas = PandasExecutionAdapter().run(
        plan,
        resident,
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )

    for name in ("parent", "child", "grandchild"):
        published = pq.read_table(target / f"{name}.parquet")
        assert published.equals(in_memory.outputs[name]), name
        assert in_memory.outputs[name].to_pydict() == pandas.outputs[name].to_pydict()

    # WARN totals ride through the stream identically on both paths. DE-03: the
    # fk_relational fixture's wide payload columns are undeclared, so the pre-GA
    # warn default also emits `undeclared_output_columns` warnings on both paths;
    # filter to the orphan warnings for the orphan-total assertions.
    orphans_per_edge = rows // 50  # orphan_frac=0.02 plants every 50th row
    orphan_warnings = [w for w in streamed.warnings if w.code == "orphan_fk"]
    assert [w.detail["orphan_rows"] for w in orphan_warnings] == [orphans_per_edge] * 2
    assert [w.code for w in orphan_warnings] == ["orphan_fk", "orphan_fk"]
    # Full warning parity (undeclared + orphan) still holds identically on both paths.
    assert streamed.warnings == in_memory.warnings

    # FK integrity on the published output: every non-orphan child key maps to
    # a masked parent key; planted orphans are preserved source keys.
    masked_parent_ids = set(pq.read_table(target / "parent.parquet").column("id").to_pylist())
    child_fk = pq.read_table(target / "child.parquet").column("parent_id").to_pylist()
    preserved = [value for value in child_fk if value not in masked_parent_ids]
    assert len(preserved) == orphans_per_edge
    assert all(value.startswith("orphan_p") for value in preserved)


def test_streaming_runner_reads_lazy_sources_in_bounded_batches(tmp_path, monkeypatch) -> None:
    """Capability proof: on the LazySource path the runner never performs a
    whole-table read; masking sees only batches bounded by the route's batch
    size, across multiple batches per table."""
    rows = 350
    batch_rows = 100
    monkeypatch.setattr(join_mod, "_JOIN_BATCH_ROWS", batch_rows, raising=False)
    paths = write_large_fk_chain(tmp_path / "src", rows, width=1, batch_rows=150)

    def forbidden_to_table(self: LazySource) -> pa.Table:
        raise AssertionError("streaming runner must never read a whole table")

    monkeypatch.setattr(LazySource, "to_table", forbidden_to_table)

    masked_sizes: list[int] = []
    real_mask_batch = stream_driver_mod.mask_batch

    def spy_mask_batch(*args: Any, **kwargs: Any) -> pa.RecordBatch:
        masked_sizes.append(args[2].num_rows)
        return real_mask_batch(*args, **kwargs)

    monkeypatch.setattr(stream_driver_mod, "mask_batch", spy_mask_batch)

    target = tmp_path / "published"
    run_fk_out_of_core(
        make_plan(),
        lazy_sources(paths),
        registry=_REG,
        relationship_graph=make_graph(OrphanPolicy.FAIL),
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work",
    )

    assert masked_sizes
    assert max(masked_sizes) <= batch_rows
    # Three tables at 350 rows and 100-row batches: at least 4 batches each.
    assert len(masked_sizes) >= 12
    assert pq.read_table(target / "child.parquet").num_rows == rows


def test_all_null_redact_batch_publishes_fixed_type(tmp_path, monkeypatch) -> None:
    """A redact-masked column whose first streamed batch is all-null (mask
    inference yields a null-typed column there) must still publish under the
    fixed non-null type, with values identical to the no-sink run."""
    monkeypatch.setattr(join_mod, "_JOIN_BATCH_ROWS", 2, raising=False)
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "customers",
                    TableSeed(
                        per_column=(("customer_id", _col("hash", namespace="cust")),), per_group=()
                    ),
                ),
                (
                    "orders",
                    TableSeed(
                        per_column=(
                            ("customer_id", _col("hash", namespace="cust")),
                            ("note", _col("redact", provider_config=(("redact_with", "X"),))),
                        ),
                        per_group=(),
                    ),
                ),
            ),
        )
    )
    graph = RelationshipGraph(edges=(_edge(OrphanPolicy.FAIL),), ordering=())
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2"]}),
        "orders": pa.table(
            {
                "customer_id": ["c1", "c1", "c2", "c2"],
                "note": pa.array([None, None, "secret", None], type=pa.string()),
            }
        ),
    }
    target = tmp_path / "published"

    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-sink",
    )
    in_memory = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work-mem",
    )

    published = pq.read_table(target / "orders.parquet")
    assert published.column("note").type == pa.string()
    assert published.column("note").to_pylist() == [None, None, "X", None]
    assert published.to_pydict() == in_memory.outputs["orders"].to_pydict()


def test_bool_orphan_publishes_int_not_bool(tmp_path, monkeypatch) -> None:
    """Codex round-5 Finding B regression test.

    A bool PARENT key that covers only one of True/False (here: only False)
    makes the other value (True) a genuine orphan under PRESERVE. An orphan's
    preserved value goes through `fk_key_value`, which unconditionally
    normalizes bool -> int (0/1); with every child row an orphan (no parent
    row is ever False's counterpart here), the pandas oracle's whole column is
    int64. Before the fix, `_resolve_output_types` declared the FIXED sink
    schema type as plain `bool` (treating the orphan candidate as staying
    bool, like every other type family), so the streaming ParquetWriter cast
    the orphan's int64 value back to bool and published True/True where the
    oracle's value is 1/1 -- a real wrong-output divergence on the ONE path
    this bug can only show up on (`emit_to_sink`'s fixed-schema write; the
    no-sink resident path accidentally self-heals via
    `StreamFkJoiner.observed_types`, so a sink is required to catch this).

    A small `_JOIN_BATCH_ROWS` splits the (here, uniformly orphan) rows
    across multiple batches, proving the fixed int64 type is applied
    consistently batch to batch, not just within one.
    """
    monkeypatch.setattr(join_mod, "_JOIN_BATCH_ROWS", 2, raising=False)
    ns = "cust_bool"
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
        namespace=ns,
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    graph = RelationshipGraph(edges=(edge,), ordering=())
    sources = {
        "customers": pa.table({"id": pa.array([False, False], type=pa.bool_())}),
        # No row references False: every child row is an orphan, split
        # across 2 batches (_JOIN_BATCH_ROWS=2) that are each homogeneous.
        "orders": pa.table({"id": pa.array([True, True, True, True], type=pa.bool_())}),
    }
    target = tmp_path / "published"

    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-sink",
    )
    in_memory = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, temp_dir=tmp_path / "work-mem"
    )
    pandas = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )

    published = pq.read_table(target / "orders.parquet")
    assert published.column("id").type == pa.int64()
    assert published.column("id").to_pylist() == [1, 1, 1, 1]
    assert pandas.outputs["orders"].column("id").type == pa.int64()
    assert pandas.outputs["orders"].column("id").to_pylist() == [1, 1, 1, 1]
    assert published.to_pydict() == in_memory.outputs["orders"].to_pydict()
    assert published.to_pydict() == pandas.outputs["orders"].to_pydict()


def test_large_whole_float_orphan_sink_matches_oracle(tmp_path, monkeypatch) -> None:
    """SC1 round-6 P2 (int/float sink-parity) regression.

    An int PARENT (passthrough) with a float CHILD whose orphan key is a whole
    number beyond +/-2**53. `fk_key_value` folds the whole float to int, so an
    orphan batch's FK chunk is int64 while the fixed sink schema types the
    column float64 (a fractional float was possible). Before the fix, the
    per-batch cast to the fixed float64 type used pyarrow's SAFE cast, which
    rejects every integer beyond +/-2**53 -- even one exactly representable as a
    double -- so `emit_to_sink`'s fixed-schema write crashed (ArrowInvalid) on a
    config the pandas oracle accepts. The guarded cast now takes the lossless
    unsafe cast, so the published value equals the oracle's exact integer
    (9007199254740994 == 9007199254740994.0). A small batch size splits the
    matched and orphan rows so the int64 orphan chunk is cast in isolation.
    """
    monkeypatch.setattr(join_mod, "_JOIN_BATCH_ROWS", 2, raising=False)
    ns = "cust_bigfloat"
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
        namespace=ns,
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    graph = RelationshipGraph(edges=(edge,), ordering=())
    sources = {
        "customers": pa.table({"id": pa.array([1, 2], type=pa.int64())}),
        # Rows 0-1 match parents 1/2; rows 2-3 are whole-float orphans, one of
        # them beyond the safe-cast range (2**53 + 2, exactly representable).
        "orders": pa.table(
            {"id": pa.array([1.0, 2.0, 9007199254740994.0, 8.0], type=pa.float64())}
        ),
    }
    target = tmp_path / "published"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-sink",
    )
    in_memory = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, temp_dir=tmp_path / "work-mem"
    )
    pandas = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )

    published = pq.read_table(target / "orders.parquet")
    # The sink commits to float64 (a fractional float was possible); values must
    # equal the oracle's exact-integer output row for row (folded across int/float).
    assert published.column("id").to_pylist() == [1.0, 2.0, 9007199254740994.0, 8.0]
    assert pandas.outputs["orders"].column("id").to_pylist() == [1, 2, 9007199254740994, 8]
    assert published.to_pydict() == pandas.outputs["orders"].to_pydict()
    # The no-sink path narrows the all-integral column back to int64, exactly
    # like the oracle, and stays value-equal.
    assert in_memory.outputs["orders"].to_pydict() == pandas.outputs["orders"].to_pydict()


def test_bool_orphan_and_match_cross_batch_casts_to_int(tmp_path) -> None:
    """A bool FK edge whose rows split across batches such that ONE batch is
    all matched (bool, untouched) and another is all orphan (fk_key_value's
    int normalization) must resolve its per-batch chunks to ONE int64 column,
    not raise and not silently keep a mixed bool/int64 type list. This is the
    per-batch mechanism `_unified_chunk_type`'s bool/int64 case fixes
    (Codex round-5 Finding B sibling): unlike the whole-column pandas oracle
    (which sees matched and orphan rows in the SAME unbatched Python list and
    would raise ArrowInvalid on the genuine bool/int mix -- out of this
    parity suite's declared scope, see the module-level docstring), the
    streaming route never builds that single mixed list: each per-batch
    `component` list is internally homogeneous (all-matched or all-orphan),
    so only the FIXED TYPE resolved ahead of time needs to reconcile bool with
    int64 across batches.
    """
    ns = "cust_bool_mixed"
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
        namespace=ns,
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    graph = RelationshipGraph(edges=(edge,), ordering=())
    sources = {
        "customers": pa.table({"id": pa.array([False, False], type=pa.bool_())}),
        # Rows 0-1: matched (False). Rows 2-3: orphan (True). Batches of 2
        # (below) keep each batch internally homogeneous.
        "orders": pa.table({"id": pa.array([False, False, True, True], type=pa.bool_())}),
    }
    target = tmp_path / "published"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-sink",
        batch_rows=2,
    )
    published = pq.read_table(target / "orders.parquet")
    assert published.column("id").type == pa.int64()
    assert published.column("id").to_pylist() == [0, 0, 1, 1]


def _shared_key_chain_plan(orders_namespace: str) -> Any:
    """A->B->C chain where orders.customer_id is BOTH an incoming FK child
    column and the outgoing parent key of the payments edge."""
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "customers",
                    TableSeed(
                        per_column=(("customer_id", _col("hash", namespace="cust")),),
                        per_group=(),
                    ),
                ),
                (
                    "orders",
                    TableSeed(
                        per_column=(("customer_id", _col("hash", namespace=orders_namespace)),),
                        per_group=(),
                    ),
                ),
                (
                    "payments",
                    TableSeed(
                        per_column=(("customer_id", _col("hash", namespace="cust")),),
                        per_group=(),
                    ),
                ),
            ),
        )
    )


def _shared_key_chain_graph() -> RelationshipGraph:
    return RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="customers",
                parent_columns=("customer_id",),
                child_table="orders",
                child_columns=("customer_id",),
                namespace="cust",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
            RelationshipEdge(
                parent_table="orders",
                parent_columns=("customer_id",),
                child_table="payments",
                child_columns=("customer_id",),
                namespace="cust",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )


def test_shared_key_chain_with_orphan_matches_oracle_and_preserves_ri(tmp_path) -> None:
    """Chain on one shared key column with an orphan "ghost" in the middle
    table: the payments edge's relation must map through orders' POST-REWRITE
    values (what the incoming join published), so payments carries exactly the
    value orders holds (RI) and both paths match the pandas oracle."""
    plan = _shared_key_chain_plan("cust")
    graph = _shared_key_chain_graph()
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2"]}),
        "orders": pa.table({"customer_id": ["c1", "ghost"]}),
        "payments": pa.table({"customer_id": ["c1", "ghost"]}),
    }

    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    in_memory = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, temp_dir=tmp_path / "work-mem"
    )
    target = tmp_path / "published"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-sink",
    )

    # Value parity with the oracle (the oracle's pandas round trip types
    # strings as large_string; the suite pins oracle parity on values).
    for name in ("customers", "orders", "payments"):
        assert in_memory.outputs[name].to_pydict() == oracle.outputs[name].to_pydict(), name
        published = pq.read_table(target / f"{name}.parquet")
        assert published.to_pydict() == oracle.outputs[name].to_pydict(), name

    orders_values = set(in_memory.outputs["orders"].column("customer_id").to_pylist())
    payments_values = in_memory.outputs["payments"].column("customer_id").to_pylist()
    assert all(value in orders_values for value in payments_values)


def test_shared_key_chain_namespace_mismatch_matches_oracle(tmp_path) -> None:
    """Zero-orphan variant where orders' own plan seed uses a DIFFERENT
    namespace than the incoming edge: a relation re-masked from the raw stream
    under orders' own seed would diverge from what the join actually wrote, so
    the relation must be built from orders' post-rewrite output."""
    plan = _shared_key_chain_plan("other_ns")
    graph = _shared_key_chain_graph()
    sources = {
        "customers": pa.table({"customer_id": ["c1", "c2"]}),
        "orders": pa.table({"customer_id": ["c1", "c2"]}),
        "payments": pa.table({"customer_id": ["c1", "c2"]}),
    }

    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    in_memory = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, temp_dir=tmp_path / "work-mem"
    )
    target = tmp_path / "published"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-sink",
    )

    for name in ("customers", "orders", "payments"):
        assert in_memory.outputs[name].to_pydict() == oracle.outputs[name].to_pydict(), name
        published = pq.read_table(target / f"{name}.parquet")
        assert published.to_pydict() == oracle.outputs[name].to_pydict(), name
    assert in_memory.outputs["payments"].equals(in_memory.outputs["orders"])


def test_overlapping_child_edges_join_from_raw_and_publish_no_source_value(tmp_path) -> None:
    """Two edges into one table whose child column tuples OVERLAP on one
    column, matched row, PRESERVE. Every joiner must take its join keys from
    the immutable raw batch (pre-C3d whole-table semantics: each edge joins
    the RAW child, later edges overwrite the shared column), so the matched
    row resolves through the composite parent and no raw source value is
    published."""
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "customers",
                    TableSeed(per_column=(("id", _col("hash", namespace="cust")),), per_group=()),
                ),
                (
                    "warehouses",
                    TableSeed(
                        per_column=(
                            ("cid", _col("hash", namespace="wc")),
                            ("wid", _col("hash", namespace="ww")),
                        ),
                        per_group=(),
                    ),
                ),
                ("orders", TableSeed(per_column=(), per_group=())),
            ),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="customers",
                parent_columns=("id",),
                child_table="orders",
                child_columns=("cust_id",),
                namespace="cust",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
            RelationshipEdge(
                parent_table="warehouses",
                parent_columns=("cid", "wid"),
                child_table="orders",
                child_columns=("cust_id", "wh_id"),
                namespace="wh",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    sources = {
        "customers": pa.table({"id": ["c1"]}),
        "warehouses": pa.table({"cid": ["c1"], "wid": ["w1"]}),
        "orders": pa.table({"cust_id": ["c1"], "wh_id": ["w1"]}),
    }

    in_memory = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, temp_dir=tmp_path / "work-mem"
    )
    target = tmp_path / "published"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-sink",
    )

    masked_warehouses = in_memory.outputs["warehouses"]
    for orders in (in_memory.outputs["orders"], pq.read_table(target / "orders.parquet")):
        cust = orders.column("cust_id").to_pylist()
        wh = orders.column("wh_id").to_pylist()
        # The later warehouses edge wins the shared column, keyed from raw.
        assert cust == masked_warehouses.column("cid").to_pylist()
        assert wh == masked_warehouses.column("wid").to_pylist()
        assert "c1" not in cust and "w1" not in wh


def test_zero_row_table_no_sink_matches_whole_table_output(tmp_path) -> None:
    """A zero-row child on the no-sink path must reproduce the whole-table
    runner's output byte for byte: FK columns and value-inferred masked
    columns come out NULL-typed (no data-derived type exists), not under the
    analytic fixed schema. (The pandas oracle itself types empty frames
    through pandas as float64, a pre-existing divergence the whole-table
    runner never matched either; parity target here is the whole-table
    out-of-core runner.)"""
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "customers",
                    TableSeed(
                        per_column=(("customer_id", _col("hash", namespace="cust")),), per_group=()
                    ),
                ),
                (
                    "orders",
                    TableSeed(
                        per_column=(
                            ("customer_id", _col("hash", namespace="cust")),
                            ("note", _col("redact", provider_config=(("redact_with", "X"),))),
                        ),
                        per_group=(),
                    ),
                ),
            ),
        )
    )
    graph = RelationshipGraph(edges=(_edge(OrphanPolicy.PRESERVE),), ordering=())
    sources = {
        "customers": pa.table({"customer_id": ["c1"]}),
        "orders": pa.table(
            {
                "customer_id": pa.array([], type=pa.string()),
                "note": pa.array([], type=pa.string()),
            }
        ),
    }

    in_memory = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, temp_dir=tmp_path / "work-mem"
    )

    expected = pa.table(
        {
            "customer_id": pa.array([], from_pandas=True),
            "note": pa.array([], from_pandas=True),
        }
    )
    assert in_memory.outputs["orders"].equals(expected)
    assert in_memory.outputs["orders"].column("customer_id").type == pa.null()
    assert in_memory.outputs["orders"].column("note").type == pa.null()


def test_all_null_redact_parent_key_with_int_child_accepted(tmp_path) -> None:
    """An all-null redact parent key over an int64 child must be ACCEPTED
    (whole-table behavior): the relation's masked type is data-derived from
    the parent's post-mask output (null-typed), every child row is a
    preserved orphan, and the no-sink result matches the pandas oracle."""
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "customers",
                    TableSeed(per_column=(("customer_id", _col("redact")),), per_group=()),
                ),
                ("orders", TableSeed(per_column=(("customer_id", _col("redact")),), per_group=())),
            ),
        )
    )
    graph = RelationshipGraph(edges=(_edge(OrphanPolicy.PRESERVE),), ordering=())
    sources = {
        "customers": pa.table({"customer_id": pa.array([None, None], type=pa.int64())}),
        "orders": pa.table({"customer_id": pa.array([7, 8], type=pa.int64())}),
    }

    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    in_memory = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, temp_dir=tmp_path / "work-mem"
    )

    for name in ("customers", "orders"):
        assert in_memory.outputs[name].equals(oracle.outputs[name]), name
    assert in_memory.outputs["orders"].column("customer_id").type == pa.int64()
    assert in_memory.outputs["orders"].column("customer_id").to_pylist() == [7, 8]

    target = tmp_path / "published"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-sink",
    )
    published = pq.read_table(target / "orders.parquet")
    assert published.column("customer_id").type == pa.int64()
    assert published.column("customer_id").to_pylist() == [7, 8]


def test_leading_all_null_key_window_keeps_aligned_cursor_in_sync(tmp_path) -> None:
    """C3d: a parent whose first FULL relation window (65_536 rows) is entirely
    null-keyed must not desync the staged-Parquet aligned cursor on the sink
    path. Both the sink and no-sink runs must complete and match the pandas
    oracle with FK integrity intact. (`iter_batches` coalesces row groups, so
    only a >= 65_536-row all-null run exercises the skipped-window path.)"""
    n_null = 66_000
    valued = [f"c{i}" for i in range(500)]
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "customers",
                    TableSeed(
                        per_column=(("customer_id", _col("hash", namespace="cust")),), per_group=()
                    ),
                ),
                (
                    "orders",
                    TableSeed(
                        per_column=(("customer_id", _col("hash", namespace="cust")),), per_group=()
                    ),
                ),
            ),
        )
    )
    graph = RelationshipGraph(edges=(_edge(OrphanPolicy.PRESERVE),), ordering=())
    customers = pa.table({"customer_id": pa.array([None] * n_null + valued, type=pa.string())})
    orders = pa.table({"customer_id": pa.array(["c0", "c499", None, "c250"], type=pa.string())})
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    pq.write_table(customers, src_dir / "customers.parquet")
    pq.write_table(orders, src_dir / "orders.parquet")
    resident = {"customers": customers, "orders": orders}

    oracle = PandasExecutionAdapter().run(
        plan, resident, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    in_memory = run_fk_out_of_core(
        plan, resident, registry=_REG, relationship_graph=graph, temp_dir=tmp_path / "work-mem"
    )
    target = tmp_path / "published"
    run_fk_out_of_core(
        plan,
        lazy_sources({name: src_dir / f"{name}.parquet" for name in ("customers", "orders")}),
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-sink",
    )

    for name in ("customers", "orders"):
        assert in_memory.outputs[name].to_pydict() == oracle.outputs[name].to_pydict(), name
        published = pq.read_table(target / f"{name}.parquet")
        assert published.to_pydict() == oracle.outputs[name].to_pydict(), name

    # FK integrity: every non-null child key resolves to a masked parent key.
    masked_ids = set(pq.read_table(target / "customers.parquet").column("customer_id").to_pylist())
    child_fk = pq.read_table(target / "orders.parquet").column("customer_id").to_pylist()
    assert child_fk[2] is None
    assert all(value in masked_ids for value in child_fk if value is not None)


def test_zero_row_parent_sink_publishes_oracle_values(tmp_path) -> None:
    """C3d: a zero-row parent with a child of orphans on the SINK path takes
    the `emit_to_sink` empty-stager branch (the relation builds against a
    synthesized whole-table-typed empty output); the published child must
    match the pandas oracle's preserved values and type."""
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "customers",
                    TableSeed(
                        per_column=(("customer_id", _col("hash", namespace="cust")),), per_group=()
                    ),
                ),
                (
                    "orders",
                    TableSeed(
                        per_column=(("customer_id", _col("hash", namespace="cust")),), per_group=()
                    ),
                ),
            ),
        )
    )
    graph = RelationshipGraph(edges=(_edge(OrphanPolicy.PRESERVE),), ordering=())
    sources = {
        "customers": pa.table({"customer_id": pa.array([], type=pa.string())}),
        "orders": pa.table({"customer_id": pa.array(["zz", None], type=pa.string())}),
    }

    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    target = tmp_path / "published"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-sink",
    )

    published = pq.read_table(target / "orders.parquet")
    assert published.column("customer_id").type == pa.string()
    assert published.column("customer_id").to_pylist() == ["zz", None]
    assert published.to_pydict() == oracle.outputs["orders"].to_pydict()


def test_float64_all_orphan_preserve_narrowing_parity_and_sink_type(tmp_path) -> None:
    """Divergence pinned (accepted, not gated): schema-level FK typing keeps
    float64 where whole-column inference narrows to int64 (reachable only when
    every float key is an orphan). The no-sink result reproduces the
    whole-table narrowing byte for byte; the sink path publishes the fixed
    float64 with numerically equal values."""
    seed = _col("passthrough")
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("customers", TableSeed(per_column=(("customer_id", seed),), per_group=())),
                ("orders", TableSeed(per_column=(("customer_id", seed),), per_group=())),
            ),
        )
    )
    graph = RelationshipGraph(edges=(_edge(OrphanPolicy.PRESERVE),), ordering=())
    sources = {
        "customers": pa.table({"customer_id": pa.array([99.0], type=pa.float64())}),
        "orders": pa.table({"customer_id": pa.array([5.0, 7.0], type=pa.float64())}),
    }

    in_memory = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work-mem",
    )
    pandas = PandasExecutionAdapter().run(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )
    target = tmp_path / "published"
    run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(target),
        temp_dir=tmp_path / "work-sink",
    )

    mem_col = in_memory.outputs["orders"].column("customer_id")
    assert mem_col.type == pandas.outputs["orders"].column("customer_id").type == pa.int64()
    assert mem_col.to_pylist() == [5, 7]
    published_col = pq.read_table(target / "orders.parquet").column("customer_id")
    assert published_col.type == pa.float64()
    assert published_col.to_pylist() == [5.0, 7.0]


def test_two_pass_source_divergence_trips_alignment_guard(tmp_path, monkeypatch) -> None:
    """Row_nr alignment backbone (OOC-B): the FK join runs over the child's
    phase-1 read while the mask stream runs over a SECOND read, so the two must
    agree row-for-row. A deterministic source zips cleanly; an injected short
    second read on the child must fail closed (out_of_core_fk_row_alignment)
    rather than silently emit misaligned FK columns.
    """
    ns = "cust_align"
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
        namespace=ns,
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    graph = RelationshipGraph(edges=(edge,), ordering=())
    sources = {
        "customers": pa.table({"id": pa.array([1, 2, 3], type=pa.int64())}),
        "orders": pa.table(
            {"id": pa.array([1, 2, 3, 1, 2], type=pa.int64()), "amt": list(range(5))}
        ),
    }
    child_src = sources["orders"]

    # Sanity: the deterministic (un-monkeypatched) run completes and preserves RI.
    ok = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, temp_dir=tmp_path / "ok"
    )
    assert ok.outputs["orders"].num_rows == 5

    real = stream_driver_mod._iter_source_batches
    reads: dict[int, int] = {}

    def diverging(src: Any, batch_rows: int) -> Any:
        batches = list(real(src, batch_rows))
        if src is child_src:
            reads[id(src)] = reads.get(id(src), 0) + 1
            if reads[id(src)] >= 2 and batches:
                # Drop the last row on the SECOND read (the phase-3 mask pass),
                # so the join output (built from the full phase-1 read) has one
                # more row than the mask stream consumes.
                batches[-1] = batches[-1].slice(0, batches[-1].num_rows - 1)
        yield from batches

    monkeypatch.setattr(stream_driver_mod, "_iter_source_batches", diverging)
    with pytest.raises(ExecutionError) as exc:
        run_fk_out_of_core(
            plan, sources, registry=_REG, relationship_graph=graph, temp_dir=tmp_path / "bad"
        )
    assert exc.value.code == "out_of_core_fk_row_alignment"
