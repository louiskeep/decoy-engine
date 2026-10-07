"""Positional Faker over integer, unsigned, boolean and float sources on the chunked native route.

Every admitted case runs the native leg with the chunked oracle poisoned, so a silent reroute
fails, and compares it with a forced-oracle leg of the same chunks. The pandas missingness of
each chunk comes from the oracle's own conversion of the raw chunk, so the three metadata forms
of a float column are covered explicitly.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native import _chunked_entry
from tests.native._b8_support import (
    FORCE,
    Run,
    assert_same_as_oracle,
    run_one,
    with_force,
)
from tests.native._c5c_i_support import (
    ADMITTED,
    FLOATS,
    METADATA_FORMS,
    SIGNED,
    UNSIGNED,
    float_table,
    int_table,
    type_id,
    typed_array,
    with_nan,
)
from tests.native._chunked_entry_support import (
    NEEDS_COMPANION,
    column_values,
    force_oracle,
    make_config,
    passthrough,
)
from tests.native._chunked_faker_support import nd_faker, split
from tests.native.test_chunked_entry_values_schema import _full_frame


@contextmanager
def poisoned_chunked_oracle() -> Iterator[None]:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the chunked oracle leg ran on a table that must stay native")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_chunked_entry, "_oracle_route", boom)
        yield


def columns(namespace: str | None = None) -> list[dict[str, Any]]:
    return [nd_faker(namespace=namespace), passthrough("p")]


def native_and_oracle(
    cols: list[dict[str, Any]], chunks: list[pa.Table], **kw: Any
) -> tuple[Run, Run]:
    gs = {"unconfigured_column_policy": "warn"}
    with poisoned_chunked_oracle():
        native = run_one(make_config(cols, global_settings=gs), chunks, **kw)
    forced = run_one(
        make_config([*cols, force_oracle(FORCE)], global_settings=gs),
        [with_force(c) for c in chunks],
        **kw,
    )
    return native, forced


def ragged(table: pa.Table, size: int = 3) -> list[pa.Table]:
    """Chunks that put nulls across boundaries, plus an empty chunk and an all-null chunk."""
    head = table.slice(0, size)
    empty = table.slice(0, 0)
    null_typed = pa.table(
        {
            "f": pa.nulls(2, table.schema.field("f").type),
            "p": pa.array([100, 101], pa.int64()),
        }
    ).replace_schema_metadata(table.schema.metadata)
    rest = split(table.slice(size), size + 1)
    return [head, empty, null_typed, *rest]


# ---------------------------------------------------------------------------
# 1. Every admitted family.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("nulls", [False, True], ids=["no_nulls", "nulls"])
@pytest.mark.parametrize("typ", ADMITTED, ids=type_id)
def test_1_every_admitted_family_equals_the_chunked_oracle(typ: pa.DataType, nulls: bool) -> None:
    table = int_table(typ, typed_array(typ, nulls=nulls))
    for chunks in (split(table, 5), ragged(table)):
        native, forced = native_and_oracle(columns(), chunks)
        assert_same_as_oracle(native, forced)
        assert native.ev[0].node_routes[0].route == "native_pool"
        assert {o.schema.field("f").type for o in native.out} == {pa.string()}


@NEEDS_COMPANION
@pytest.mark.parametrize("typ", FLOATS, ids=type_id)
def test_1_floats_with_real_nan_inf_and_negative_zero(typ: pa.DataType) -> None:
    table = int_table(typ, with_nan(typ))
    native, forced = native_and_oracle(columns("ns_f"), split(table, 4))
    assert_same_as_oracle(native, forced)
    got = column_values(native.out, "f")
    # Under default conversion a NaN is missing, so it gets no draw; inf and -0.0 are values.
    assert got[2] is None and got[5] is None and got[7] is None
    assert all(v is not None for i, v in enumerate(got) if i not in (2, 5, 7))


@NEEDS_COMPANION
@pytest.mark.parametrize("typ", [pa.int8(), pa.uint64(), pa.bool_(), pa.float32()], ids=type_id)
def test_1_chunked_equals_the_whole_frame_run(typ: pa.DataType, tmp_path: Path) -> None:
    table = int_table(typ, typed_array(typ, nulls=True))
    config = make_config(columns())
    run = run_one(config, split(table, 5))
    assert run.ev[0].native_admitted is True
    full = _full_frame(config, table, tmp_path)
    assert column_values(run.out, "f") == full.column("f").to_pylist()


@NEEDS_COMPANION
def test_1_a_multi_chunk_run_draws_by_global_position() -> None:
    table = int_table(pa.int64(), pa.array([7] * 40, pa.int64()))
    run = run_one(make_config(columns()), split(table, 9))
    assert run.ev[0].native_admitted is True
    assert len(set(column_values(run.out, "f"))) > 1


# ---------------------------------------------------------------------------
# 2. Missingness follows the oracle's conversion.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("form", sorted(METADATA_FORMS))
@pytest.mark.parametrize("typ", FLOATS, ids=type_id)
def test_2_each_metadata_form_matches_the_oracle(typ: pa.DataType, form: str) -> None:
    table = float_table(typ, form)
    native, forced = native_and_oracle(columns(), split(table, 5))
    assert_same_as_oracle(native, forced)


@NEEDS_COMPANION
@pytest.mark.parametrize("typ", FLOATS, ids=type_id)
def test_2_an_arrow_extension_nan_is_a_value_and_gets_a_draw(typ: pa.DataType) -> None:
    table = float_table(typ, "arrow_ext")
    native, _forced = native_and_oracle(columns(), [table])
    got = column_values(native.out, "f")
    assert got[2] is not None and got[5] is not None, "a valid NaN must draw"
    assert got[7] is None, "a real null stays null"


@NEEDS_COMPANION
@pytest.mark.parametrize("typ", FLOATS, ids=type_id)
def test_2_a_numpy_nan_is_missing(typ: pa.DataType) -> None:
    table = float_table(typ, "numpy")
    native, _forced = native_and_oracle(columns(), [table])
    got = column_values(native.out, "f")
    assert got[2] is None and got[5] is None and got[7] is None


@NEEDS_COMPANION
@pytest.mark.parametrize("typ", FLOATS, ids=type_id)
def test_2_chunks_with_different_metadata_match_the_oracle(typ: pa.DataType) -> None:
    forms = ["numpy", "arrow_ext", "nullable", "arrow_ext", "numpy"]
    chunks = [float_table(typ, f) for f in forms]
    native, forced = native_and_oracle(columns(), chunks)
    assert_same_as_oracle(native, forced)


@NEEDS_COMPANION
@pytest.mark.parametrize("typ", FLOATS, ids=type_id)
def test_2_normalization_does_not_hide_an_arrow_extension_nan(typ: pa.DataType) -> None:
    """A later chunk with a null-typed passthrough column triggers the null-column cast, which
    rebuilds the table without schema metadata. The mask must come from the raw chunk."""
    first = float_table(typ, "arrow_ext", with_p=False).append_column(
        "n", pa.array(list(range(12)), pa.int64())
    )
    later = float_table(typ, "arrow_ext", with_p=False).append_column("n", pa.nulls(12))
    cols = [nd_faker(), passthrough("n")]
    native, forced = native_and_oracle(cols, [first, later])
    assert_same_as_oracle(native, forced)
    assert column_values(native.out[1:], "f")[2] is not None


# ---------------------------------------------------------------------------
# 4. Families that stay declined, with the oracle's output.
# ---------------------------------------------------------------------------

_DECLINED: dict[str, pa.Array] = {
    "timestamp": pa.array([1, None, 3, 4], pa.timestamp("us")),
    "date32": pa.array([1, None, 3, 4], pa.date32()),
    "duration": pa.array([1, None, 3, 4], pa.duration("us")),
    "decimal128": pa.array([1, None, 3, 4], pa.decimal128(10, 2)),
    "binary": pa.array([b"a", None, b"c", b"d"], pa.binary()),
    "list": pa.array([[1], None, [3], [4]], pa.list_(pa.int64())),
    "struct": pa.array([{"a": 1}, None, {"a": 3}, {"a": 4}], pa.struct([("a", pa.int64())])),
    "float16": pa.array([1.0, None, 3.0, 4.0], pa.float16()),
    "dictionary": pa.array(["a", None, "b", "a"]).dictionary_encode(),
    "null": pa.nulls(4),
}


@pytest.mark.parametrize("kind", sorted(_DECLINED))
def test_4_positional_faker_over_a_declined_family_runs_the_oracle_leg(kind: str) -> None:
    array = _DECLINED[kind]
    table = pa.table({"f": array, "p": pa.array(range(len(array)), pa.int64())})
    config = make_config(columns())
    run = run_one(config, [table])
    ev = run.ev[0]
    assert ev.native_admitted is False
    assert f"faker_source_type_not_string:f:{array.type}" in (ev.reroute_reason or "")
    assert run.out[0].schema.field("f").type == pa.string()


@pytest.mark.parametrize("typ", [pa.int64(), pa.uint8(), pa.bool_(), pa.float64()], ids=type_id)
def test_4_deterministic_faker_over_a_non_string_source_still_declines(typ: pa.DataType) -> None:
    table = int_table(typ, typed_array(typ, nulls=False))
    det = nd_faker(deterministic=True, namespace="ns_det")
    config = make_config([det, passthrough("p")])
    cfg = copy.deepcopy(config)
    try:
        run = run_one(cfg, [table])
    except Exception as exc:  # the oracle may reject the config; the reroute is the point
        assert "native_admitted" not in str(exc)
        return
    assert run.ev[0].native_admitted is False
    assert "faker_source_type_not_string:f:" in (run.ev[0].reroute_reason or "")


def test_4_the_admitted_set_is_exactly_the_planned_families() -> None:
    from decoy_engine.execution.native._faker_null_mask import POSITIONAL_FAKER_SOURCE_TYPES

    assert POSITIONAL_FAKER_SOURCE_TYPES == frozenset([*SIGNED, *UNSIGNED, pa.bool_(), *FLOATS])
