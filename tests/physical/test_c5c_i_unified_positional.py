"""Positional Faker over integer, unsigned, boolean and float sources on the unified route.

The unchanged round-trip gate still decides which columns reach the binder: it admits the
columns whose pandas round trip is value-identical to the resident column and declines the rest
(a default-conversion integer with nulls widens to double, and a valid NaN does not survive).
Every admitted case poisons the oracle and compares with an explicit lane-off run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from tests.native._c5c_i_support import (
    ADMITTED,
    FLOATS,
    SIGNED,
    UNSIGNED,
    float_table,
    type_id,
    typed_array,
    with_nan,
)
from tests.physical.test_unified_slice_faker import NEEDS_COMPANION, Case, lane_run
from tests.physical.test_unified_slice_positional import (
    FAKER_OP,
    _inputs,
    _slice_of,
    lane_batch_rows,
    nd_faker,
    node_evidence,
    parity,
)


def one_col(array: pa.Array, *, meta: dict[bytes, bytes] | None = None) -> pa.Table:
    table = pa.table({"c": array})
    return table.replace_schema_metadata(meta) if meta is not None else table


def pandas_meta(series: pd.Series) -> dict[bytes, bytes]:
    frame = pd.DataFrame({"c": series})
    return dict(pa.Table.from_pandas(frame, preserve_index=False).schema.metadata)


# ---------------------------------------------------------------------------
# 3. Admitted cases: activation, values, schema and metadata equal to lane-off.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("typ", [*SIGNED, *UNSIGNED, pa.bool_()], ids=type_id)
def test_3_null_free_integer_unsigned_and_bool(tmp_path: Path, typ: pa.DataType) -> None:
    case = Case(tmp_path, one_col(typed_array(typ, nulls=False)), [nd_faker()])
    leaf = parity(case, batch=5)
    assert node_evidence(leaf, FAKER_OP)["compiled_kernel_executed"] is True


@NEEDS_COMPANION
def test_3_bool_with_nulls(tmp_path: Path) -> None:
    case = Case(tmp_path, one_col(typed_array(pa.bool_(), nulls=True)), [nd_faker()])
    parity(case, batch=5)


@NEEDS_COMPANION
@pytest.mark.parametrize(
    "pandas_dtype, typ", [("Int64", pa.int64()), ("UInt64", pa.uint64())], ids=["Int64", "UInt64"]
)
def test_3_pandas_nullable_integer_with_nulls(
    tmp_path: Path, pandas_dtype: str, typ: pa.DataType
) -> None:
    series = pd.Series(pd.array([1, None, 3, 4, None, 6, 7, 8, 9], dtype=pandas_dtype))
    table = one_col(pa.array(series, type=typ), meta=pandas_meta(series))
    case = Case(tmp_path, table, [nd_faker()])
    parity(case, batch=4)


@NEEDS_COMPANION
@pytest.mark.parametrize("typ", FLOATS, ids=type_id)
def test_3_nan_free_float_with_nulls(tmp_path: Path, typ: pa.DataType) -> None:
    case = Case(tmp_path, one_col(typed_array(typ, nulls=True)), [nd_faker()])
    parity(case, batch=5)


@NEEDS_COMPANION
@pytest.mark.parametrize("shape", ["all_null", "empty"])
@pytest.mark.parametrize("typ", ADMITTED, ids=type_id)
def test_3_typed_all_null_and_empty_columns(tmp_path: Path, typ: pa.DataType, shape: str) -> None:
    array = pa.nulls(9, typ) if shape == "all_null" else pa.array([], type=typ)
    case = Case(tmp_path, one_col(array), [nd_faker()])
    parity(case, batch=4)


@NEEDS_COMPANION
def test_3_a_positional_faker_beside_a_passthrough_over_several_batches(tmp_path: Path) -> None:
    table = pa.table(
        {
            "c": pa.array(list(range(33)), pa.int32()),
            "p": pa.array([f"keep_{i}" for i in range(33)], pa.string()),
        }
    )
    case = Case(tmp_path, table, [nd_faker(), {"name": "p", "strategy": "passthrough"}])
    parity(case, batch=10)
    with lane_batch_rows(10):
        split = lane_run(case).outputs["t"].column("c").to_pylist()
    assert split == lane_run(case).outputs["t"].column("c").to_pylist()


# ---------------------------------------------------------------------------
# 4. Declines unchanged.
# ---------------------------------------------------------------------------

_DECLINED: dict[str, pa.Array] = {
    "timestamp": pa.array([1, None, 3, 4], pa.timestamp("us")),
    "date32": pa.array([1, None, 3, 4], pa.date32()),
    "duration": pa.array([1, None, 3, 4], pa.duration("us")),
    "decimal128": pa.array([1, None, 3, 4], pa.decimal128(10, 2)),
    "binary": pa.array([b"a", None, b"c", b"d"], pa.binary()),
    "list": pa.array([[1], None, [3], [4]], pa.list_(pa.int64())),
    "struct": pa.array([{"a": 1}, None, {"a": 3}, {"a": 4}], pa.struct([("a", pa.int64())])),
    "float16": pa.array(np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float16)),
    "dictionary": pa.array(["a", None, "b", "a"]).dictionary_encode(),
    "null": pa.nulls(4),
}


def _assert_declines_with_the_oracle_output(case: Case) -> None:
    try:
        off = case.run(lane=False)
    except Exception as exc:  # a config the oracle rejects must be rejected identically
        with pytest.raises(type(exc)) as info:
            case.run(lane=True)
        assert str(info.value) == str(exc)
        return
    on = case.run(lane=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)
    assert tuple(on.warnings) == tuple(off.warnings)


@pytest.mark.parametrize("kind", sorted(_DECLINED))
def test_4_positional_faker_over_a_declined_family_declines(tmp_path: Path, kind: str) -> None:
    _assert_declines_with_the_oracle_output(Case(tmp_path, one_col(_DECLINED[kind]), [nd_faker()]))


@pytest.mark.parametrize("typ", [pa.int64(), pa.uint8(), pa.bool_(), pa.float64()], ids=type_id)
def test_4_deterministic_faker_over_a_non_string_source_declines(
    tmp_path: Path, typ: pa.DataType
) -> None:
    case = Case(tmp_path, one_col(typed_array(typ, nulls=False)), [nd_faker(deterministic=True)])
    _assert_declines_with_the_oracle_output(case)


def test_4_the_resident_domain_is_wider_only_for_a_bound_positional_node() -> None:
    from decoy_engine.execution._operator_registry import OPERATORS
    from decoy_engine.execution._unified_slice_resident_types import positional_resident_types
    from decoy_engine.execution.native._operator_params import FakerParams

    wider = OPERATORS["faker"].positional_resident_types
    assert wider is not None and pa.int32() in wider
    assert positional_resident_types("faker", FakerParams("ns", positional=True)) == wider
    assert positional_resident_types("faker", FakerParams("ns")) is None
    assert positional_resident_types("hash", FakerParams("ns", positional=True)) is None


def test_4_the_binder_takes_a_numeric_source_only_for_the_positional_variant(
    tmp_path: Path,
) -> None:
    from decoy_engine.execution.physical import _shadow_bindings

    table = one_col(typed_array(pa.int32(), nulls=False))
    for name, column, expected in (
        ("det", nd_faker(deterministic=True), False),
        ("pos", nd_faker(), True),
    ):
        sub = tmp_path / name
        sub.mkdir()
        inputs = _inputs(Case(sub, table, [column]))
        plan_slice = _slice_of(inputs)
        pool_only = _shadow_bindings._faker_pool_bindable(
            plan_slice=plan_slice, table="t", column="c", inputs=inputs
        )
        assert pool_only is False
        positional = _shadow_bindings.positional_faker_bindable(
            plan_slice=plan_slice, table="t", column="c", inputs=inputs
        )
        assert positional is expected


# ---------------------------------------------------------------------------
# 5. Round-trip declines.
# ---------------------------------------------------------------------------

_ROUND_TRIP_DECLINES: dict[str, Callable[[], pa.Table]] = {
    "default_int_with_nulls": lambda: one_col(pa.array([1, None, 3, 4, None, 6], pa.int64())),
    "uint_default_with_nulls": lambda: one_col(pa.array([1, None, 3, 4, None, 6], pa.uint8())),
    "float64_nan_numpy": lambda: float_table(pa.float64(), "numpy", with_nan(pa.float64())).select(
        ["f"]
    ),
    "float32_nan_numpy": lambda: float_table(pa.float32(), "numpy", with_nan(pa.float32())).select(
        ["f"]
    ),
    "float64_nan_arrow_ext": lambda: float_table(
        pa.float64(), "arrow_ext", with_nan(pa.float64())
    ).select(["f"]),
    "float32_nan_arrow_ext": lambda: float_table(
        pa.float32(), "arrow_ext", with_nan(pa.float32())
    ).select(["f"]),
}


@pytest.mark.parametrize("name", sorted(_ROUND_TRIP_DECLINES))
def test_5_round_trip_declines_keep_the_oracle_output(tmp_path: Path, name: str) -> None:
    table = _ROUND_TRIP_DECLINES[name]()
    if "f" in table.column_names:
        table = table.rename_columns(["c"])
    case = Case(tmp_path, table, [nd_faker()])
    off = case.run(lane=False)
    on = case.run(lane=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)
