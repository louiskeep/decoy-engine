"""C3c primitive 3: per-batch child FK join with a schema-fixed output type.

A streaming runner writes child output batches to ONE ParquetWriter, whose
schema is fixed by the first batch, so every batch's FK column must share one
Arrow type computed up front from schemas alone. The byte-parity oracle is
C2's whole-child `mask_child_fk`: for each fixture, the concatenation of the
per-batch outputs must equal the whole-child output exactly (types and
values), including a numeric case whose int/float split crosses batches. Where
schema-derived typing cannot be guaranteed byte-identical to whole-column
inference (decimals, string+numeric, promotable mixes like string+binary
whose merge only matches when the data mixes), the joiner must reject fail
closed up
front with the same `out_of_core_fk_key_dtype_unsupported` C2 uses, before any
output exists.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution.out_of_core import _batch_join as batch_join_mod
from decoy_engine.execution.out_of_core import _runner as runner_mod
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._join import mask_child_fk
from decoy_engine.execution.out_of_core._mask import mask_table
from decoy_engine.execution.out_of_core._relation import build_parent_key_relation_from_tables
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


def _joiner(plan: Any, edge: RelationshipEdge, relation, temp_dir, child: pa.Table):
    remap_seeds = None
    if edge.orphan_policy is OrphanPolicy.REMAP:
        remap_seeds = tuple(
            runner_mod._column_seed(plan, edge.parent_table, col) for col in edge.parent_columns
        )
    return ChildFkBatchJoiner(
        edge=edge,
        parent_relation=relation,
        child_key_types=tuple(child.column(col).type for col in edge.child_columns),
        temp_dir=temp_dir,
        remap_seeds=remap_seeds,
        job_seed=plan.seed_envelope.job_seed,
    )


def _relation_for(plan: Any, edge: RelationshipEdge, parent: pa.Table, temp_dir):
    # Mirror the runner: the relation maps source parent keys to the FINAL
    # masked parent keys, whatever the parent column's strategy is.
    masked_parent = mask_table(plan, edge.parent_table, parent, skip_columns=frozenset())
    return build_parent_key_relation_from_tables(
        source_parent=parent,
        masked_parent=masked_parent,
        edge=edge,
        temp_dir=temp_dir,
    )


def _whole_child_oracle(plan, edge, relation, child, temp_dir):
    remap_values = (
        runner_mod._remap_values(plan, edge, child)
        if edge.orphan_policy is OrphanPolicy.REMAP
        else None
    )
    return mask_child_fk(
        child=child,
        edge=edge,
        parent_relation=relation,
        temp_dir=temp_dir,
        remap_values=remap_values,
    )


def _batched_output(plan, edge, relation, child, temp_dir) -> tuple[pa.Table, int]:
    batches: list[pa.RecordBatch] = []
    orphans = 0
    with _joiner(plan, edge, relation, temp_dir, child) as joiner:
        for batch in child.to_batches(max_chunksize=_BATCH):
            out, batch_orphans = joiner.join_batch(batch)
            batches.append(out)
            orphans += batch_orphans
    return pa.Table.from_batches(batches), orphans


def _assert_parity(plan, edge, parent, child, tmp_path) -> tuple[pa.Table, tuple, int]:
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    whole, warnings = _whole_child_oracle(plan, edge, relation, child, tmp_path / "whole")
    rebuilt, orphans = _batched_output(plan, edge, relation, child, tmp_path / "batched")

    assert rebuilt.schema == whole.schema
    assert rebuilt.equals(whole)
    return whole, warnings, orphans


# Ten child rows under batch size 3: orphans in three different batches, null
# keys straddling batches, matches in every batch (same shape the C2 chunked
# tests use), so any per-batch policy or typing slip crosses a batch boundary.
_CROSS_BATCH_IDS = ["c2", "missing1", None, "c1", "missing2", "c2", None, "missing1", "c1", "c2"]
_CLEAN_IDS = ["c2", "c1", None, "c1", "c2", "c2", None, "c1", "c1", "c2"]


@pytest.mark.parametrize(
    "policy",
    [OrphanPolicy.FAIL, OrphanPolicy.PRESERVE, OrphanPolicy.WARN, OrphanPolicy.REMAP],
)
def test_single_fk_policies_match_whole_child(tmp_path, policy: OrphanPolicy) -> None:
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(policy)
    parent = pa.table({"customer_id": ["c1", "c2"]})
    ids = _CLEAN_IDS if policy is OrphanPolicy.FAIL else _CROSS_BATCH_IDS
    child = pa.table({"customer_id": ids, "amount": list(range(len(ids)))})

    whole, warnings, orphans = _assert_parity(plan, edge, parent, child, tmp_path)

    assert whole.column("customer_id").type == pa.string()
    if policy is OrphanPolicy.WARN:
        assert warnings[0].detail["orphan_rows"] == 3
        assert orphans == 3
    elif policy is OrphanPolicy.FAIL:
        assert orphans == 0


@pytest.mark.parametrize(
    "policy",
    [OrphanPolicy.FAIL, OrphanPolicy.PRESERVE, OrphanPolicy.WARN, OrphanPolicy.REMAP],
)
def test_composite_fk_policies_match_whole_child(tmp_path, policy: OrphanPolicy) -> None:
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
    # Row 2 is a partial-null composite key (whole key treated as null); row 3
    # is the orphan for non-FAIL policies. Six rows cross two batches.
    accounts = ["a2", "a1", "a1", "a1" if policy is OrphanPolicy.FAIL else "missing", "a2", "a1"]
    child = pa.table(
        {
            "country": ["CA", "US", None, "US", "CA", "US"],
            "account_id": accounts,
            "amount": list(range(6)),
        }
    )

    _whole, warnings, orphans = _assert_parity(plan, edge, parent, child, tmp_path)

    if policy is OrphanPolicy.WARN:
        assert warnings[0].detail["orphan_rows"] == 1
        assert orphans == 1


@pytest.mark.parametrize(
    ("strategy", "provider_config"),
    [
        ("redact", (("redact_with", "X"),)),
        ("truncate", (("length", 2),)),
        ("passthrough", ()),
    ],
)
@pytest.mark.parametrize("policy", [OrphanPolicy.FAIL, OrphanPolicy.REMAP])
def test_strategy_parent_policies_match_whole_child(
    tmp_path,
    strategy: str,
    provider_config: tuple[tuple[str, Any], ...],
    policy: OrphanPolicy,
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

    _assert_parity(plan, edge, parent, child, tmp_path)


def test_numeric_cross_batch_split_resolves_to_one_float64_up_front(tmp_path) -> None:
    # The crux case: batch 1 emits only whole-number orphans (int64 on its
    # own), batch 2 only floats. C2 reconciles that split after the fact with
    # Arrow's permissive merge; the streaming joiner must land on the same
    # float64 BEFORE the first batch, so a ParquetWriter schema built from
    # `output_types` never sees a mid-stream type change.
    plan = _two_table_plan(_col("passthrough"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": pa.array([1.0, 2.5], type=pa.float64())})
    child = pa.table(
        {"customer_id": pa.array([1.0, 3.0, 1.0, 4.0, 6.0, 7.0, 2.5, 8.5, 1.0], type=pa.float64())}
    )
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")

    joiner = _joiner(plan, edge, relation, tmp_path / "batched", child)
    with joiner:
        assert joiner.output_types == (pa.float64(),)
        out_batches = []
        orphans = 0
        for batch in child.to_batches(max_chunksize=_BATCH):
            out, batch_orphans = joiner.join_batch(batch)
            assert out.column("customer_id").type == pa.float64()
            out_batches.append(out)
            orphans += batch_orphans

    whole, _warnings = _whole_child_oracle(plan, edge, relation, child, tmp_path / "whole")
    rebuilt = pa.Table.from_batches(out_batches)
    assert whole.column("customer_id").type == pa.float64()
    assert rebuilt.schema == whole.schema
    assert rebuilt.equals(whole)
    assert orphans == 5


@pytest.mark.parametrize(
    "policy",
    [OrphanPolicy.PRESERVE, OrphanPolicy.WARN, OrphanPolicy.REMAP],
)
def test_all_integral_numeric_output_widens_where_whole_child_narrows(
    tmp_path, policy: OrphanPolicy
) -> None:
    # Documented divergence, pinned so it never drifts silently: when every
    # emitted value happens to be integral (all rows orphan, all keys whole
    # numbers), C2's value-level inference narrows to int64 while schema-level
    # typing cannot know that and keeps the float64 union. Values are
    # numerically equal; only the Arrow type (and int vs float scalars) widen.
    # A streaming runner must gate or accept this config explicitly. Every
    # policy that can emit orphan-derived values hits it: PRESERVE and WARN
    # keep the source keys, REMAP with a passthrough parent re-mints them.
    plan = _two_table_plan(_col("passthrough"), "customers", "orders", "customer_id")
    edge = _edge(policy)
    parent = pa.table({"customer_id": pa.array([99.0], type=pa.float64())})
    child = pa.table({"customer_id": pa.array([5.0, 7.0], type=pa.float64())})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")

    whole, _warnings = _whole_child_oracle(plan, edge, relation, child, tmp_path / "whole")
    rebuilt, orphans = _batched_output(plan, edge, relation, child, tmp_path / "batched")

    assert whole.column("customer_id").type == pa.int64()
    assert whole.column("customer_id").to_pylist() == [5, 7]
    assert rebuilt.column("customer_id").type == pa.float64()
    assert rebuilt.column("customer_id").to_pylist() == [5.0, 7.0]
    assert orphans == 2


def test_string_masked_with_numeric_child_key_fails_closed_up_front(tmp_path) -> None:
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": ["c1"]})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    child = pa.table({"customer_id": pa.array([1, 2], type=pa.int64())})

    with pytest.raises(ExecutionError) as exc:
        _joiner(plan, edge, relation, tmp_path / "batched", child)

    assert exc.value.code == "out_of_core_fk_key_dtype_unsupported"


def test_string_masked_with_binary_child_key_fails_closed_up_front(tmp_path) -> None:
    # A promotable mix outside {int64, float64}: Arrow would merge
    # string+binary to binary, but the whole-child path only lands there when
    # the data actually mixes; an all-string run would then emit binary
    # scalars, a silent value drift, so the mix is rejected at construction.
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": ["c1"]})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    child = pa.table({"customer_id": pa.array([b"c1", b"c2"], type=pa.binary())})

    with pytest.raises(ExecutionError) as exc:
        _joiner(plan, edge, relation, tmp_path / "batched", child)

    assert exc.value.code == "out_of_core_fk_key_dtype_unsupported"


def test_all_null_fk_child_column_keeps_fixed_type_where_oracle_goes_null(tmp_path) -> None:
    # Documented divergence: C2's value-level inference leaves an all-null
    # child key column null-typed; the fixed schema keeps the masked-key type
    # so a streaming writer gets a writable schema. Values are identical (all
    # null), so the streaming schema is the intended one.
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": ["c1"]})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    child = pa.table({"customer_id": pa.array([None, None], type=pa.null())})

    whole, _warnings = _whole_child_oracle(plan, edge, relation, child, tmp_path / "whole")
    rebuilt, orphans = _batched_output(plan, edge, relation, child, tmp_path / "batched")

    assert whole.column("customer_id").type == pa.null()
    assert rebuilt.column("customer_id").type == pa.string()
    assert rebuilt.column("customer_id").null_count == rebuilt.num_rows == 2
    assert orphans == 0


def test_decimal_child_key_fails_closed_up_front(tmp_path) -> None:
    # Decimal is rejected outright, even unmixed: whole-column inference
    # digit-fits decimal precision from the values, so no schema-derived fixed
    # type can be guaranteed byte-identical to C2's output.
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": ["c1"]})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    child = pa.table({"customer_id": pa.array([Decimal("1.5")], type=pa.decimal128(4, 2))})

    with pytest.raises(ExecutionError) as exc:
        _joiner(plan, edge, relation, tmp_path / "batched", child)

    assert exc.value.code == "out_of_core_fk_key_dtype_unsupported"


def test_decimal_masked_parent_fails_closed_up_front(tmp_path) -> None:
    plan = _two_table_plan(_col("passthrough"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.FAIL)
    parent = pa.table({"customer_id": pa.array([Decimal("1.5")], type=pa.decimal128(4, 2))})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    child = pa.table({"customer_id": pa.array([Decimal("1.5")], type=pa.decimal128(4, 2))})

    with pytest.raises(ExecutionError) as exc:
        _joiner(plan, edge, relation, tmp_path / "batched", child)

    assert exc.value.code == "out_of_core_fk_key_dtype_unsupported"


def test_fail_policy_raises_on_orphaned_batch_before_output(tmp_path) -> None:
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.FAIL)
    parent = pa.table({"customer_id": ["c1", "c2"]})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    child = pa.table({"customer_id": ["c1", "c2", "c1", "c2", "missing", "c1"]})
    batches = child.to_batches(max_chunksize=_BATCH)

    with _joiner(plan, edge, relation, tmp_path / "batched", child) as joiner:
        clean, orphans = joiner.join_batch(batches[0])
        assert clean.num_rows == 3
        assert orphans == 0
        with pytest.raises(ExecutionError) as exc:
            joiner.join_batch(batches[1])

    assert exc.value.code == "orphan_fk_violation"
    assert "1 orphan FK row(s)" in str(exc.value)


def test_remap_masks_per_batch_not_per_child(tmp_path, monkeypatch) -> None:
    # Residency bound for the streaming remap path: orphan values are minted
    # from the batch's own keys, so no kernel call is sized by total child
    # cardinality (C2's runner-precomputed `_remap_values` is whole-child).
    lengths: list[int] = []
    real_mask_column = batch_join_mod.mask_column

    def spy(values, *args, **kwargs):
        lengths.append(len(values))
        return real_mask_column(values, *args, **kwargs)

    monkeypatch.setattr(batch_join_mod, "mask_column", spy)

    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.REMAP)
    parent = pa.table({"customer_id": ["c1", "c2"]})
    child = pa.table({"customer_id": _CROSS_BATCH_IDS})

    _assert_parity(plan, edge, parent, child, tmp_path)

    assert lengths
    assert max(lengths) <= _BATCH


def test_empty_batch_yields_empty_output_with_fixed_type(tmp_path) -> None:
    plan = _two_table_plan(_col("hash", namespace="cust"), "customers", "orders", "customer_id")
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": ["c1"]})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    child = pa.table({"customer_id": ["c1"], "amount": [1]})
    empty = child.to_batches()[0].slice(0, 0)

    with _joiner(plan, edge, relation, tmp_path / "batched", child) as joiner:
        out, orphans = joiner.join_batch(empty)

    assert out.num_rows == 0
    assert orphans == 0
    assert out.column("customer_id").type == pa.string()
    assert out.column("amount").type == child.column("amount").type
