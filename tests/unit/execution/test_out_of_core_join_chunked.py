"""C2: chunk-bounded child FK join.

`mask_child_fk` must not hold Python or Arrow structures sized by total child
cardinality during join processing: the child-key side enters DuckDB as a lazy
bounded RecordBatchReader (never a materialized whole-child staging table) and
the ordered join result comes back as a streamed record-batch reader
(to_arrow_reader, the non-deprecated fetch_record_batch), never one whole Arrow
table. These tests pin the residency bound itself (spied, batch-counted,
deterministic) and the correctness that a batched rewrite can break:
orphan policies and null keys across batch boundaries, with the total orphan
count aggregated over all batches.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, PandasExecutionAdapter
from decoy_engine.execution.out_of_core import _join as join_mod
from decoy_engine.execution.out_of_core import (
    build_parent_key_relation,
    run_fk_out_of_core,
)
from decoy_engine.kernel import hash_array
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_SEED = b"\x00" * 8
_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())


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
                (
                    "orders",
                    TableSeed(per_column=(("customer_id", _hash_col("cust")),), per_group=()),
                ),
            ),
        )
    )


def _edge(policy: OrphanPolicy = OrphanPolicy.FAIL) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="cust",
        orphan_policy=policy,
    )


def test_mask_child_fk_streams_child_keys_and_join_result(tmp_path, monkeypatch) -> None:
    # The Tier-1 invariant on the child side: DuckDB is handed a lazy
    # RecordBatchReader of bounded child-key batches (zero pulled at register
    # time, ceil(rows / batch_rows) pulled in total) and the join result is
    # consumed via a streamed reader, never one whole to_arrow_table() read.
    n, batch_rows = 10, 4
    plan = _plan()
    edge = _edge()
    parent = pa.table({"customer_id": [f"c{i}" for i in range(n)]})
    child = pa.table({"customer_id": [f"c{i}" for i in range(n)], "amount": list(range(n))})
    relation = build_parent_key_relation(
        plan=plan, parent=parent, edge=edge, temp_dir=tmp_path / "rel"
    )
    monkeypatch.setattr(join_mod, "_JOIN_BATCH_ROWS", batch_rows, raising=False)

    recorded: dict[str, Any] = {
        "reader_registered": False,
        "table_registers": [],
        "pulled": 0,
        "at_register": None,
        "to_arrow_table": 0,
        "result_reader": 0,
    }

    # Count pulls at the source generator (C1's pattern): wrapping the
    # registered reader itself is not safe under DuckDB's parallel arrow scan.
    real_batches = join_mod._child_key_batches

    def counting_batches(*args, **kwargs):
        for batch in real_batches(*args, **kwargs):
            recorded["pulled"] += 1
            yield batch

    monkeypatch.setattr(join_mod, "_child_key_batches", counting_batches)

    class _SpyCursor:
        def __init__(self, inner) -> None:
            self._inner = inner

        def to_arrow_table(self):
            recorded["to_arrow_table"] += 1
            return self._inner.to_arrow_table()

        def fetch_record_batch(self, *args, **kwargs):
            recorded["result_reader"] += 1
            return self._inner.fetch_record_batch(*args, **kwargs)

        def to_arrow_reader(self, *args, **kwargs):
            recorded["result_reader"] += 1
            return self._inner.to_arrow_reader(*args, **kwargs)

        def fetchone(self):
            return self._inner.fetchone()

    class _SpyConn:
        def __init__(self, inner) -> None:
            self._inner = inner

        def register(self, name, obj):
            if isinstance(obj, pa.RecordBatchReader):
                recorded["reader_registered"] = True
                recorded["at_register"] = recorded["pulled"]
            else:
                recorded["table_registers"].append(name)
            return self._inner.register(name, obj)

        def execute(self, *args, **kwargs):
            return _SpyCursor(self._inner.execute(*args, **kwargs))

        def close(self) -> None:
            self._inner.close()

    real_connect = join_mod.connect_duckdb
    monkeypatch.setattr(
        join_mod, "connect_duckdb", lambda **kwargs: _SpyConn(real_connect(**kwargs))
    )

    out, warnings = join_mod.mask_child_fk(
        child=child,
        edge=edge,
        parent_relation=relation,
        temp_dir=tmp_path / "join",
    )

    assert recorded["reader_registered"] is True
    assert recorded["table_registers"] == []
    assert recorded["at_register"] == 0
    assert recorded["pulled"] == math.ceil(n / batch_rows)
    assert recorded["to_arrow_table"] == 0
    assert recorded["result_reader"] == 1
    expected = hash_array(
        child.column("customer_id").combine_chunks(), seed=_SEED, namespace="cust"
    ).to_pylist()
    assert out.column("customer_id").to_pylist() == expected
    assert out.column("amount").to_pylist() == list(range(n))
    assert out.column_names == ["customer_id", "amount"]
    assert warnings == ()


def _cross_batch_sources() -> dict[str, pa.Table]:
    # Ten child rows under batch_rows=3: orphans in batches 0, 1, and 2, null
    # keys straddling two batches, and matches in every batch, so any per-batch
    # policy or aggregation slip shows up.
    child_ids = ["c2", "missing1", None, "c1", "missing2", "c2", None, "missing1", "c1", "c2"]
    return {
        "customers": pa.table({"customer_id": ["c1", "c2"]}),
        "orders": pa.table({"customer_id": child_ids, "amount": list(range(len(child_ids)))}),
    }


@pytest.mark.parametrize(
    "policy",
    [OrphanPolicy.PRESERVE, OrphanPolicy.WARN, OrphanPolicy.REMAP],
)
def test_cross_batch_orphan_policies_match_pandas(tmp_path, monkeypatch, policy) -> None:
    monkeypatch.setattr(join_mod, "_JOIN_BATCH_ROWS", 3, raising=False)
    plan = _plan()
    graph = RelationshipGraph(edges=(_edge(policy),), ordering=())
    sources = _cross_batch_sources()

    out = run_fk_out_of_core(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        temp_dir=tmp_path / "work",
    )
    pandas = PandasExecutionAdapter().run(
        plan,
        sources,
        registry=_REG,
        relationship_graph=graph,
        namespace_registry=_NS,
    )

    assert out.outputs["orders"].to_pydict() == pandas.outputs["orders"].to_pydict()
    assert out.outputs["customers"].to_pydict() == pandas.outputs["customers"].to_pydict()
    # DE-03: `orders.amount` is an undeclared payload column, so the pre-GA warn
    # default emits an `undeclared_output_columns` warning on every route. It is
    # orthogonal to orphan-policy parity (which is what this test pins), so
    # filter it out before asserting on the orphan warning set.
    orphan_warnings = [w for w in out.warnings if w.code != "undeclared_output_columns"]
    if policy is OrphanPolicy.WARN:
        assert len(orphan_warnings) == 1
        assert orphan_warnings[0].code == "orphan_fk"
        assert orphan_warnings[0].detail["orphan_rows"] == 3
    else:
        assert orphan_warnings == []


_H = "a3f9c2e8b1d47765"

# Oracle battery for `_concat_fk_chunks`: each case is a tuple of per-batch
# value lists (>= 2 batches) mixing cross-batch-homogeneous splits (each batch
# infers a different type on its own) with mixed-within-batch splits (one batch
# already carries the promoted type). The oracle is one whole-column
# pa.array(all_values, from_pandas=True), the pre-C2 single-shot build, and the
# batched helper must reproduce it byte for byte (type AND values) or raise
# where it raises. Value families mirror what `_append_output_batch` can emit:
# masked hash strings, preserved source keys after `fk_key_value` normalization
# (int, non-whole float, Decimal, bool, bytes, datetime), and nulls.
_CONCAT_PARITY_CASES = {
    "all_strings": ([_H, _H + "1", _H + "2"], [_H + "3", _H + "4"]),
    "strings_nulls_mixed": ([_H, None, _H], [None, _H]),
    "all_null_batch_first": ([None, None, None], [_H, _H]),
    "all_null_batch_middle": ([_H, _H], [None, None, None], [_H]),
    "all_null": ([None, None], [None]),
    "all_int": ([1, 2, 3], [4, 5]),
    "int_then_float_batches": ([1, 2, 3], [5.5, 6.5]),
    "float_then_int_batches": ([5.5, 6.5], [1, 2, 3]),
    "int_float_within_batch": ([1, 5.5, 2], [3, 4]),
    "int_null_float": ([1, None, 2], [None, None, None], [5.5]),
    "whole_and_fractional_floats": ([5.0, 6.5], [7.0]),
    "decimal_precision_across_batches": (
        [Decimal("1.5"), Decimal("2.5")],
        [Decimal("12345.678")],
    ),
    "decimal_scale_across_batches": ([Decimal("1.5")], [Decimal("2.25")]),
    "decimal_intdigits_and_scale": ([Decimal("9999.5")], [Decimal("0.125")]),
    "decimal_with_all_null_batch": ([Decimal("1.5"), Decimal("2.5")], [None, None]),
    "str_then_bytes_batches": ([_H, _H, _H], [b"x", b"y"]),
    "bytes_then_str_batches": ([b"x", b"y"], [_H, _H, _H]),
    "str_bytes_within_batch": ([_H, b"x"], [_H, _H]),
    "bool_only": ([True, False], [True]),
    "bool_null": ([True, None], [None, None], [False]),
    "bytes_null": ([b"x", b"y"], [None, None], [b"z"]),
    "timestamps": (
        [datetime(2020, 1, 1, 0, 0, 0, 123456)],
        [datetime(1999, 12, 31, 23, 59, 59, 999999)],
    ),
    "timestamp_with_all_null_batch": ([datetime(2020, 1, 1)], [None, None]),
}

# Mixes where the whole-column oracle itself raises: the batched build must
# raise too (never silently invent a promotion the single-shot build refused).
_CONCAT_BOTH_RAISE_CASES = {
    "str_then_int_batches": ([_H, _H, _H], [5, 6]),
    "int_then_str_batches": ([5, 6], [_H, _H, _H]),
    "str_int_within_batch": ([_H, 5], [_H, _H]),
    "bigint_float_within_batch": ([2**53 + 1, 0.5], [1.5]),
    "uint64_beyond_int64_then_int": ([2**63 + 1], [1, 2]),
    "uint64_beyond_int64_then_float": ([2**63 + 1], [0.5]),
    "bool_then_int_batches": ([True, False], [1, 2]),
    "bytes_then_int_batches": ([b"x"], [1]),
    "bool_then_str_batches": ([True], [_H]),
}


@pytest.mark.parametrize("name", sorted(_CONCAT_PARITY_CASES))
def test_concat_fk_chunks_matches_whole_column_inference(name) -> None:
    batches = _CONCAT_PARITY_CASES[name]
    values = [value for batch in batches for value in batch]
    oracle = pa.array(values, from_pandas=True)
    result = join_mod._concat_fk_chunks([pa.array(batch, from_pandas=True) for batch in batches])
    assert result.type == oracle.type
    assert result.to_pylist() == oracle.to_pylist()
    assert result.equals(oracle)


@pytest.mark.parametrize("name", sorted(_CONCAT_BOTH_RAISE_CASES))
def test_concat_fk_chunks_raises_where_whole_column_raises(name) -> None:
    batches = _CONCAT_BOTH_RAISE_CASES[name]
    values = [value for batch in batches for value in batch]
    # Category note: Arrow rejects these as ArrowInvalid/ArrowTypeError, except
    # the beyond-int64 whole-column path, which surfaces OverflowError from the
    # Python int conversion. Both sides must land in this rejection family;
    # what matters for parity is that neither side produces a promoted array.
    raise_family = (pa.ArrowException, OverflowError)
    with pytest.raises(raise_family):
        pa.array(values, from_pandas=True)
    with pytest.raises(raise_family):
        join_mod._concat_fk_chunks([pa.array(batch, from_pandas=True) for batch in batches])


@pytest.mark.parametrize(
    "batches",
    [
        ([Decimal("1.5")], [5]),
        ([5], [Decimal("1.5")]),
        ([Decimal("1.5")], [0.5]),
    ],
    ids=["decimal_then_int", "int_then_decimal", "decimal_then_float"],
)
def test_concat_fk_chunks_rejects_decimal_nondecimal_mix_fail_closed(batches) -> None:
    # Arrow's permissive field merge widens decimal+int64 to a fixed-precision
    # decimal128 where whole-column inference picks a digit-fitted type, and
    # coerces decimal+float64 to double where whole-column inference raises.
    # Neither can be byte-identical, so the helper must reject fail closed with
    # a clear code instead of drifting or crashing deep inside Arrow.
    chunks = [pa.array(batch, from_pandas=True) for batch in batches]
    with pytest.raises(ExecutionError) as exc:
        join_mod._concat_fk_chunks(chunks)
    assert exc.value.code == "out_of_core_fk_key_dtype_unsupported"


@pytest.mark.parametrize(
    "batches",
    [
        ([2**53 + 1, 2**53 + 3], [0.5]),
        ([0.5], [2**53 + 1, 2**53 + 3]),
    ],
    ids=["bigint_then_float", "float_then_bigint"],
)
def test_concat_fk_chunks_rejects_int_beyond_float_range_fail_closed(batches) -> None:
    # A whole-integer chunk that lands in a float64 column (the unified type is
    # float only because a fractional value was possible) whose key exceeds
    # +/-2**53 cannot be represented in float without rounding away from the
    # pandas oracle's exact int64. The whole-column oracle itself rejects this
    # (ArrowInvalid); the batched build must also reject, and does so with a
    # clean coded error instead of a bare Arrow crash so `run_fk_out_of_core`
    # fails closed with an attributable code rather than leaking ArrowInvalid.
    values = [value for batch in batches for value in batch]
    with pytest.raises(pa.ArrowException):
        pa.array(values, from_pandas=True)
    chunks = [pa.array(batch, from_pandas=True) for batch in batches]
    with pytest.raises(ExecutionError) as exc:
        join_mod._concat_fk_chunks(chunks)
    assert exc.value.code == "out_of_core_fk_key_dtype_unsupported"


def test_non_fk_child_columns_keep_source_arrow_types(tmp_path, monkeypatch) -> None:
    # C2 splices only the FK columns into the resident child frame, so non-FK
    # columns keep their source Arrow types; the pre-C2 DuckDB round-trip
    # degraded dictionary<string> and large_string to string. Values must stay
    # byte-identical while the richer types survive.
    monkeypatch.setattr(join_mod, "_JOIN_BATCH_ROWS", 3, raising=False)
    plan = _plan()
    edge = _edge(OrphanPolicy.PRESERVE)
    ids = ["c0", "orphan1", None, "c1", "orphan2", "c0", None, "c1"]
    dict_col = pa.array(["a", "b", "a", "c", "b", "a", "c", "b"]).dictionary_encode()
    large_col = pa.array([f"v{i}" for i in range(len(ids))], type=pa.large_string())
    parent = pa.table({"customer_id": ["c0", "c1"]})
    child = pa.table(
        {
            "customer_id": pa.array(ids),
            "category": dict_col,
            "note": large_col,
        }
    )
    relation = build_parent_key_relation(
        plan=plan, parent=parent, edge=edge, temp_dir=tmp_path / "rel"
    )

    out, warnings = join_mod.mask_child_fk(
        child=child,
        edge=edge,
        parent_relation=relation,
        temp_dir=tmp_path / "join",
    )

    assert warnings == ()
    assert pa.types.is_dictionary(out.column("category").type)
    assert out.column("note").type == pa.large_string()
    assert out.column("category").to_pylist() == dict_col.to_pylist()
    assert out.column("note").to_pylist() == large_col.to_pylist()
    masked = hash_array(pa.array(["c0", "c1"]), seed=_SEED, namespace="cust").to_pylist()
    parent_map = {"c0": masked[0], "c1": masked[1]}
    expected = [parent_map.get(value, value) for value in ids]
    assert out.column("customer_id").to_pylist() == expected


def test_cross_batch_orphans_fail_with_total_count(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(join_mod, "_JOIN_BATCH_ROWS", 3, raising=False)
    plan = _plan()
    graph = RelationshipGraph(edges=(_edge(OrphanPolicy.FAIL),), ordering=())
    sources = _cross_batch_sources()

    with pytest.raises(ExecutionError) as exc:
        run_fk_out_of_core(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            temp_dir=tmp_path / "work",
        )

    assert exc.value.code == "orphan_fk_violation"
    assert "3 orphan row(s) in" in str(exc.value)  # the oracle's message, one producer


def _redact_to_null_plan() -> Any:
    """A parent key strategy that legitimately masks every non-null value to
    null (redact with redact_with=None), so a MATCHED child row's masked
    output is null too. This is the P1 masked-null-sentinel repro shape: the
    join must not use the masked value's nullness to decide orphan vs matched.
    """
    redact_null = ColumnSeed(
        namespace=None,
        strategy="redact",
        provider="redact",
        backend_type="decoy_native",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(("redact_with", None),),
        coherent_with=(),
    )
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                ("customers", TableSeed(per_column=(("customer_id", redact_null),), per_group=())),
                ("orders", TableSeed(per_column=(("customer_id", redact_null),), per_group=())),
            ),
        )
    )


def test_masked_null_parent_key_is_not_treated_as_orphan(tmp_path) -> None:
    """P1 regression: a parent key that masks to null (e.g. redact producing
    null) must still resolve a matched child row to that masked null value,
    not be misclassified as an orphan. Before the fix, `_append_output_batch`
    used `masked_components[0][row] is not None` as the match indicator, so
    every matched-but-null-masked row fell through to the orphan branch:
    under WARN/PRESERVE it published the RAW child FK key (a leak), and under
    FAIL it inflated the orphan count for rows that actually matched.
    """
    plan = _redact_to_null_plan()
    edge = _edge(OrphanPolicy.PRESERVE)
    parent = pa.table({"customer_id": ["c0", "c1"]})
    # c0/c1 match (and mask to null); "orphan1" has no parent row at all.
    child = pa.table({"customer_id": ["c0", "orphan1", "c1"], "amount": [1, 2, 3]})
    relation = build_parent_key_relation(
        plan=plan, parent=parent, edge=edge, temp_dir=tmp_path / "rel"
    )

    out, warnings = join_mod.mask_child_fk(
        child=child, edge=edge, parent_relation=relation, temp_dir=tmp_path / "join"
    )

    # Matched rows resolve to the masked (null) parent value, NOT the raw
    # child key; the true orphan keeps its source key under PRESERVE.
    assert out.column("customer_id").to_pylist() == [None, "orphan1", None]
    assert warnings == ()


def test_masked_null_parent_key_fail_policy_does_not_false_positive(tmp_path) -> None:
    """Same repro under FAIL: with every child key actually matching a parent
    row (whose masked value happens to be null), FAIL must not raise. Before
    the fix, the FAIL anti-join count used the masked column's nullness, so
    every matched-but-null-masked row was counted as an orphan and raised.
    """
    plan = _redact_to_null_plan()
    edge = _edge(OrphanPolicy.FAIL)
    parent = pa.table({"customer_id": ["c0", "c1"]})
    child = pa.table({"customer_id": ["c0", "c1", "c0"], "amount": [1, 2, 3]})
    relation = build_parent_key_relation(
        plan=plan, parent=parent, edge=edge, temp_dir=tmp_path / "rel"
    )

    out, warnings = join_mod.mask_child_fk(
        child=child, edge=edge, parent_relation=relation, temp_dir=tmp_path / "join"
    )

    assert out.column("customer_id").to_pylist() == [None, None, None]
    assert warnings == ()


def test_masked_null_parent_key_warn_counts_only_true_orphans(tmp_path) -> None:
    """WARN's orphan count must reflect only the genuine orphan, not the
    matched-but-null-masked rows."""
    plan = _redact_to_null_plan()
    edge = _edge(OrphanPolicy.WARN)
    parent = pa.table({"customer_id": ["c0", "c1"]})
    child = pa.table({"customer_id": ["c0", "orphan1", "c1"], "amount": [1, 2, 3]})
    relation = build_parent_key_relation(
        plan=plan, parent=parent, edge=edge, temp_dir=tmp_path / "rel"
    )

    out, warnings = join_mod.mask_child_fk(
        child=child, edge=edge, parent_relation=relation, temp_dir=tmp_path / "join"
    )

    assert out.column("customer_id").to_pylist() == [None, "orphan1", None]
    assert len(warnings) == 1
    assert warnings[0].detail["orphan_rows"] == 1


# ---------------------------------------------------------------------------
# Slice 0 item 2: the typed FK output seam (plan T4, T4z, T6, T7). Labels in the test names and
# ids: FB = FAIL-BEFORE (red on main), CH = CHARACTERIZATION (green before and after),
# NS = NEW-SEAM (a unit cell of `fk_output_array`, which does not exist on main; neither red
# nor characterization evidence). `fk_output_array` is imported inside the NEW-SEAM tests so
# the rest of this module still collects on main.
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402

import pandas as pd  # noqa: E402

from decoy_engine.execution._fk_keys import FK_KEY_DTYPE_UNSUPPORTED_CODE  # noqa: E402
from tests.parity import _d2b_shapes as _d2b  # noqa: E402
from tests.parity import _dcomp_ooc_shapes as _dco  # noqa: E402
from tests.parity import _ooc_temporal_fk as _tf  # noqa: E402
from tests.unit.execution.test_out_of_core_batch_join import (  # noqa: E402
    _col,
    _joiner,
    _relation_for,
    _two_table_plan,
)


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    todo: list[BaseException | None] = [exc]
    while todo:
        current = todo.pop()
        if current is None or any(current is s for s in seen):
            continue
        seen.append(current)
        todo += [current.__cause__, current.__context__]
    return seen


def _assert_value_free(exc: BaseException, marker: str) -> None:
    for link in _chain(exc):
        assert marker not in str(link), type(link).__name__
        assert marker not in repr(link.args), type(link).__name__
        for note in getattr(link, "__notes__", ()):
            assert marker not in note


def _seam(values: list[Any], target: pa.DataType) -> pa.Array:
    from decoy_engine.execution.out_of_core._join import fk_output_array

    return fk_output_array(values, target)


def _cast(values: list[Any], target: pa.DataType) -> pa.Array:
    return join_mod.cast_fk_chunk(_seam(values, target), target)


class TestFkOutputArrayNewSeam:
    """T4 (NS): exact or raising, one code, no cause or context."""

    def test_integer_beyond_2_53_exactly_representable_is_admitted_into_float64_NS(self) -> None:
        assert _cast([2**54], pa.float64()).to_pylist() == [float(2**54)]

    @pytest.mark.parametrize(
        ("values", "target"),
        [
            pytest.param([2**53 + 1], pa.float64(), id="int_beyond_exact_float"),
            pytest.param([1.5, 2**53 + 1], pa.float64(), id="mixed_batch_beyond_exact_float"),
            pytest.param([2.5], pa.int64(), id="fraction_into_int64"),
            pytest.param([Decimal("1.234")], pa.decimal128(4, 2), id="decimal_rescale"),
            pytest.param([_dt.datetime(9999, 1, 1)], pa.timestamp("ns"), id="far_date_into_ns"),
            pytest.param(
                [pd.Timestamp("2020-01-01 00:00:00.000000001")], pa.timestamp("us"), id="ns_into_us"
            ),
            pytest.param(
                [_dt.datetime(2020, 1, 1, 0, 0, 0, 5)], pa.timestamp("ms"), id="us_into_ms"
            ),
            pytest.param([_dt.date(2020, 1, 1)], pa.timestamp("us"), id="date_into_timestamp"),
            pytest.param(["CANARY-7f3a"], pa.timestamp("ns"), id="string_into_timestamp"),
        ],
    )
    def test_unrepresentable_values_raise_the_one_code_NS(self, values, target) -> None:
        with pytest.raises(ExecutionError) as exc:
            _cast(values, target)
        assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE
        assert exc.value.__cause__ is None and exc.value.__context__ is None
        _assert_value_free(exc.value, "CANARY-7f3a")
        _assert_value_free(exc.value, "9999")
        _assert_value_free(exc.value, str(2**53 + 1))

    @pytest.mark.parametrize(
        ("values", "target"),
        [
            pytest.param(
                [pd.Timestamp("2020-01-01 00:00:00.000000001"), None],
                pa.timestamp("ns"),
                id="timestamp_with_ns_part",
            ),
            pytest.param(
                [_dt.datetime(2020, 1, 1, 0, 0, 0, 5)], pa.timestamp("ns"), id="datetime_into_ns"
            ),
            pytest.param(
                [_dt.datetime(9999, 1, 1), _dt.datetime(1, 1, 1)],
                pa.timestamp("us"),
                id="far_dates_us",
            ),
            pytest.param([None, None], pa.timestamp("ns"), id="all_null_keeps_the_fixed_type"),
        ],
    )
    def test_temporal_values_are_built_exactly_at_the_fixed_type_NS(self, values, target) -> None:
        out = _seam(values, target)
        assert out.type == target
        assert out.to_pylist() == values

    def test_non_temporal_targets_keep_inference_NS(self) -> None:
        # The joiner records the pre-cast chunk type to replay whole-column narrowing, and the
        # oracle types an all-null string or date32 child `null`: those stay inferred.
        assert _seam([1, 2], pa.float64()).type == pa.int64()
        assert _seam([None, None], pa.string()).type == pa.null()
        assert _seam([None], pa.date32()).type == pa.null()


def _float_parent_int_child(tmp_path, child_keys: list[int]):
    plan = _two_table_plan(_col("passthrough"), "customers", "orders", "customer_id")
    edge = RelationshipEdge(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="cust",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    parent = pa.table({"customer_id": pa.array([2.0, 1.5], pa.float64())})
    child = pa.table({"customer_id": pa.array(child_keys, pa.int64())})
    relation = _relation_for(plan, edge, parent, tmp_path / "rel")
    return _joiner(plan, edge, relation, tmp_path / "join", child), child


_MARKER_INT = 2**53 + 1  # 9007199254740993: pyarrow's own error text echoes it


def test_a_refused_fk_batch_carries_no_key_value_in_its_error_chain_FB(tmp_path) -> None:
    """T6 (FB): a matched float key and an orphan integer beyond exact float precision share a
    batch. Main raises the coded error `from exc`, so pyarrow's text with the value sits in
    `__cause__`; the seam raises after its `except` block, with no cause or context."""
    joiner, child = _float_parent_int_child(tmp_path, [2, _MARKER_INT])
    with joiner, pytest.raises(ExecutionError) as exc:
        joiner.join_batch(child.to_batches()[0])
    assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE
    _assert_value_free(exc.value, str(_MARKER_INT))


def test_a_refused_fk_batch_leaves_the_joiner_usable_and_closable_CH(tmp_path) -> None:
    """T7 (CH): the refusal exists on main; it emits nothing for the batch, the staging
    registration is released (a following valid batch joins), and close() releases DuckDB."""
    joiner, child = _float_parent_int_child(tmp_path, [2, _MARKER_INT])
    with pytest.raises(ExecutionError) as exc:
        joiner.join_batch(child.to_batches()[0])
    assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE
    out, orphans = joiner.join_batch(
        pa.table({"customer_id": pa.array([2], pa.int64())}).to_batches()[0]
    )
    assert (out.column(0).to_pylist(), orphans) == ([2.0], 0)
    joiner.close()
    assert joiner._conn is None


def test_an_out_of_range_value_emits_no_output_chunk_NS() -> None:
    """T7 (NS): the range refusal raises before anything is appended to `output_chunks`."""
    edge = RelationshipEdge(
        parent_table="p",
        parent_columns=("k",),
        child_table="c",
        child_columns=("k",),
        namespace="n",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    batch = pa.record_batch(
        {
            "__decoy_row_nr": pa.array([0], pa.int64()),
            "__decoy_fk_join_key": pa.array(["t"]),
            "__decoy_src_0": pa.array([_dt.datetime(9999, 1, 1)], pa.timestamp("us")),
            "__decoy_parent_match": pa.array(["t"]),
            "__decoy_parent_masked_0": pa.array([_dt.datetime(9999, 1, 1)], pa.timestamp("us")),
        }
    )
    chunks: list[list[pa.Array]] = [[]]
    with pytest.raises(ExecutionError) as exc:
        join_mod._append_output_batch(
            batch,
            edge=edge,
            remap_values=None,
            output_chunks=chunks,
            output_types=(pa.timestamp("ns"),),
        )
    assert exc.value.code == FK_KEY_DTYPE_UNSUPPORTED_CODE
    assert chunks == [[]]


_ZERO_ROW = [("us", "ns"), ("ns", "us"), ("ns", "ns"), ("ms", "s")]
_ALL_POLICIES = [OrphanPolicy.PRESERVE, OrphanPolicy.WARN, OrphanPolicy.REMAP, OrphanPolicy.FAIL]
_FINER = {"s": 0, "ms": 1, "us": 2, "ns": 3}


@pytest.mark.parametrize("policy", _ALL_POLICIES, ids=[f"{p.value}-FB" for p in _ALL_POLICIES])
@pytest.mark.parametrize(("p", "c"), _ZERO_ROW, ids=[f"{p}->{c}" for p, c in _ZERO_ROW])
def test_empty_output_table_is_typed(p: str, c: str, policy: OrphanPolicy) -> None:
    """T4z (FB): a zero-row child publishes the fixed type (main: `null`); under FAIL the
    masked-parent type, otherwise the finer unit."""
    shape = _tf.scalar_shape(policy, pa.timestamp(p), pa.timestamp(c), _tf.instants(3), [])
    out = _dco.run_ooc(shape).outputs["C"].column("n")
    parent = _tf.RELATION_UNIT[p]
    unit = parent if policy is OrphanPolicy.FAIL else max(parent, c, key=_FINER.__getitem__)
    assert out.type == pa.timestamp(unit)
    assert len(out) == 0
    assert _d2b.run_full(shape).outputs["C"].num_rows == 0


def test_a_string_zero_row_child_stays_null_typed_CH() -> None:
    """T4z (CH): non-temporal zero-row FK columns keep today's null type, matching the oracle."""
    shape = _tf.scalar_shape(OrphanPolicy.WARN, pa.string(), pa.string(), ["a"], [])
    assert _dco.run_ooc(shape).outputs["C"].column("n").type == pa.null()
    assert _d2b.run_full(shape).outputs["C"].column("n").type == pa.null()
