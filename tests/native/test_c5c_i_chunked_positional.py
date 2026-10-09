"""Positional Faker over integer, unsigned, boolean and float sources on the chunked native route.

Every admitted case runs the native leg with the chunked oracle poisoned, so a silent reroute
fails, and compares it with a forced-oracle leg of the same chunks. The pandas missingness of
each chunk comes from the oracle's own conversion of the raw chunk, so the three metadata forms
of a float column are covered explicitly.
"""

from __future__ import annotations

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
    identical,
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
    "date64": pa.array([86_400_000, None, 3 * 86_400_000, 4 * 86_400_000], pa.date64()),
    "time64": pa.array([1_000, None, 3_000, 4_000], pa.time64("us")),
    "decimal256": pa.array([1, None, 3, 4], pa.decimal256(40, 2)),
    "duration": pa.array([1, None, 3, 4], pa.duration("us")),
    "decimal128": pa.array([1, None, 3, 4], pa.decimal128(10, 2)),
    "binary": pa.array([b"a", None, b"c", b"d"], pa.binary()),
    "list": pa.array([[1], None, [3], [4]], pa.list_(pa.int64())),
    "struct": pa.array([{"a": 1}, None, {"a": 3}, {"a": 4}], pa.struct([("a", pa.int64())])),
    "float16": pa.array([1.0, None, 3.0, 4.0], pa.float16()),
    "dictionary": pa.array(["a", None, "b", "a"]).dictionary_encode(),
    "null": pa.nulls(4),
}


def _outcome(config: dict[str, Any], chunks: list[pa.Table]) -> tuple[Any, list[Any]]:
    """The run's result or the exception it raised, plus its route decision either way."""
    evidence: list[Any] = []
    try:
        return run_one(config, chunks, route_evidence_sink=evidence), evidence
    except Exception as exc:
        return exc, evidence


def assert_declines_with_the_oracle_outcome(
    cols: list[dict[str, Any]], table: pa.Table, reason_prefix: str
) -> None:
    gs = {"unconfigured_column_policy": "warn"}
    got, ev = _outcome(make_config(cols, global_settings=gs), [table])
    want, _ = _outcome(
        make_config([*cols, force_oracle(FORCE)], global_settings=gs), [with_force(table)]
    )
    if ev:
        assert ev[0].native_admitted is False
        assert reason_prefix in (ev[0].reroute_reason or "")
    else:
        # A nested source fails in the profiler before any route decision, on both legs.
        assert isinstance(got, Exception)
    if isinstance(want, Exception):
        assert type(got) is type(want) and str(got) == str(want)
    else:
        assert not isinstance(got, Exception), got
        assert identical(got.out[0], want.out[0].drop_columns([FORCE]))


@pytest.mark.parametrize("kind", sorted(_DECLINED))
def test_4_positional_faker_over_a_declined_family_runs_the_oracle_leg(kind: str) -> None:
    array = _DECLINED[kind]
    table = pa.table({"f": array, "p": pa.array(range(len(array)), pa.int64())})
    assert_declines_with_the_oracle_outcome(
        columns(), table, f"faker_source_type_not_string:f:{array.type}"
    )


@pytest.mark.parametrize("typ", [pa.int64(), pa.uint8(), pa.bool_(), pa.float64()], ids=type_id)
def test_4_deterministic_faker_over_an_ordinary_iterable_still_declines(typ: pa.DataType) -> None:
    # C5c-ii opened bool/int/uint to deterministic Faker, but only from a producer that guarantees
    # the stream schema. An ordinary list carries no guarantee, so it declines up-front; float is
    # still an out-of-family decline. The trusted-producer admit case lives in the C5c-ii suite.
    table = int_table(typ, typed_array(typ, nulls=False))
    cols = [nd_faker(deterministic=True, namespace="ns_det"), passthrough("p")]
    reason = (
        "faker_source_type_not_string:f:"
        if typ == pa.float64()
        else "faker_conversion_schema_not_guaranteed:f"
    )
    assert_declines_with_the_oracle_outcome(cols, table, reason)


def test_4_the_admitted_set_is_exactly_the_planned_families() -> None:
    from decoy_engine.execution.native._faker_null_mask import POSITIONAL_FAKER_SOURCE_TYPES

    assert frozenset([*SIGNED, *UNSIGNED, pa.bool_(), *FLOATS]) == POSITIONAL_FAKER_SOURCE_TYPES


@pytest.mark.parametrize("dtype", ["object", "string", "arrow"])
@pytest.mark.parametrize("arrow_type", [pa.string(), pa.large_string()])
def test_string_mask_skips_the_conversion_and_equals_it(
    dtype: str, arrow_type: pa.DataType
) -> None:
    import pandas as pd

    from decoy_engine.execution.native import _faker_null_mask

    values = ["a", None, "nan", "", "b"]
    series = {
        "object": pd.Series(values, dtype=object),
        "string": pd.Series(values, dtype="string"),
        "arrow": pd.Series(values, dtype=pd.ArrowDtype(pa.string())),
    }[dtype]
    raw = pa.Table.from_pandas(pd.DataFrame({"f": series}), preserve_index=False)
    raw = raw.cast(pa.schema([pa.field("f", arrow_type)], metadata=raw.schema.metadata))
    fast = _faker_null_mask.faker_missing_mask(raw, "f")
    converted = _faker_null_mask.to_pandas_fk_safe(raw, set())["f"].isna().to_list()
    assert fast.to_pylist() == converted == [False, True, False, False, False]
