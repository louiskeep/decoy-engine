"""Key columns that differ only in integer width or string encoding are compared by value.

The oracle for every cell is the same data held in one common type on both sides, which takes
the declared-type path of `key_facts` that never meets two different types."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.out_of_core import _duckdb
from decoy_engine.profiling import KeyBatches, KeyFacts, key_facts

INTS = [
    pa.int8(),
    pa.int16(),
    pa.int32(),
    pa.int64(),
    pa.uint8(),
    pa.uint16(),
    pa.uint32(),
    pa.uint64(),
]
DECIMAL = pa.decimal128(38, 0)


def _keys(*arrays: pa.Array) -> KeyBatches:
    """One `k0` column, one batch per array."""
    batches = [pa.RecordBatch.from_arrays([a], names=["k0"]) for a in arrays]
    schema = pa.schema([("k0", arrays[0].type)])
    return KeyBatches(schema, lambda: iter(batches))


def _typed(values: Sequence[int | None], type_: pa.DataType) -> KeyBatches:
    return _keys(pa.array(list(values), type_))


def _wide(values: Sequence[int | None]) -> KeyBatches:
    return _keys(pa.array([None if v is None else Decimal(v) for v in values], DECIMAL))


def _shape(facts: KeyFacts) -> tuple[Any, ...]:
    return (
        facts.status,
        facts.parent_rows,
        facts.parent_null_key_rows,
        facts.parent_duplicate_excess,
        tuple(
            (c.rows, c.null_key_rows, c.orphan_rows, c.orphan_distinct_keys) for c in facts.children
        ),
    )


def _run(tmp_path: Path, parent: KeyBatches, *children: KeyBatches) -> KeyFacts:
    return key_facts(parent, list(children), scratch=tmp_path / "s", memory_limit="64MB")


def _range(type_: pa.DataType) -> tuple[int, int]:
    info = type_.bit_width
    if pa.types.is_unsigned_integer(type_):
        return 0, 2**info - 1
    return -(2 ** (info - 1)), 2 ** (info - 1) - 1


@pytest.mark.parametrize(
    ("ptype", "ctype"), list(itertools.product(INTS, INTS)), ids=lambda t: str(t)
)
def test_integer_pairs_count_like_one_common_type(
    tmp_path: Path, ptype: pa.DataType, ctype: pa.DataType
) -> None:
    (plo, phi), (clo, chi) = _range(ptype), _range(ctype)
    shared = [max(plo, clo), min(phi, chi)]
    parent = [plo, phi, phi, 0, *shared]
    child = [clo, chi, 0, None, *shared]
    got = _run(tmp_path, _typed(parent, ptype), _typed(child, ctype))
    want = _run(tmp_path, _wide(parent), _wide(child))
    assert got.status == "checked"
    assert _shape(got) == _shape(want)


def test_typed_empty_load_uses_schema_types_only(tmp_path: Path) -> None:
    parent = _keys(pa.array([], pa.int32()))
    child = _keys(pa.array([], pa.uint64()))
    facts = _run(tmp_path, parent, child)
    assert _shape(facts) == ("checked", 0, 0, 0, ((0, 0, 0, 0),))


def test_uint64_never_wraps_onto_int64(tmp_path: Path) -> None:
    parent_values = [2**63 + 5, 1]
    got = _run(tmp_path, _typed(parent_values, pa.uint64()), _typed([-(2**63) + 5, 1], pa.int64()))
    want = _run(tmp_path, _wide(parent_values), _wide([-(2**63) + 5, 1]))
    assert _shape(got) == _shape(want)
    assert got.children[0].orphan_rows == 1


def test_dictionary_string_parent_with_differing_dictionaries(tmp_path: Path) -> None:
    first = pa.array(["a", "b", "a"]).dictionary_encode()
    second = pa.array(["z", "b", None]).dictionary_encode()
    assert first.dictionary.to_pylist() != second.dictionary.to_pylist()
    parent = _keys(first, second)
    child = _keys(pa.array(["a", "b", "q", "z", None], pa.large_string()))
    got = _run(tmp_path, parent, child)
    plain = _keys(pa.array(["a", "b", "a", "z", "b", None], pa.string()))
    want = _run(tmp_path, plain, _keys(pa.array(["a", "b", "q", "z", None], pa.string())))
    assert got.status == "checked"
    assert _shape(got) == _shape(want)
    assert got.children[0].orphan_rows == 1


def test_union_shaped_parent_widens_past_the_first_schema(tmp_path: Path) -> None:
    # `_keys.py` declares the first parent's schema (int32) and feeds the batches of every parent
    # through it, including a wider one that also rides as a child.
    wide_values = [1, 2**40, 7]
    narrow = pa.array([1, 2, 3], pa.int32())
    wide = pa.array(wide_values, pa.int64())
    batches = [
        pa.RecordBatch.from_arrays([narrow], names=["k0"]),
        pa.RecordBatch.from_arrays([wide], names=["k0"]),
    ]
    union = KeyBatches(pa.schema([("k0", pa.int32())]), lambda: iter(batches))
    child = _typed([2**40, 3, 99], pa.int64())
    ride_as_child = _typed(wide_values, pa.int64())
    got = _run(tmp_path, union, child, ride_as_child)
    want = _run(tmp_path, _wide([1, 2, 3, *wide_values]), _wide([2**40, 3, 99]), _wide(wide_values))
    assert got.status == "checked"
    assert _shape(got) == _shape(want)
    assert got.children[0].orphan_rows == 1


@pytest.mark.parametrize(
    ("parent", "child"),
    [
        (pa.array([1], pa.int64()), pa.array(["1"], pa.string())),
        (pa.array([1.0], pa.float64()), pa.array([1], pa.int64())),
        (pa.array([1], pa.int64()).dictionary_encode(), pa.array([1], pa.int64())),
    ],
    ids=["int-string", "float-int", "int-dictionary-int"],
)
def test_other_families_stay_mismatches(tmp_path: Path, parent: pa.Array, child: pa.Array) -> None:
    facts = _run(tmp_path, _keys(parent), _keys(child))
    assert facts.status == "key_type_mismatch"
    assert facts.children == ()


def test_keys_comparable_reads_schemas_and_opens_no_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.profiling import keys_comparable

    def refuse(**_: Any) -> None:
        raise AssertionError("a connection was opened")

    monkeypatch.setattr(_duckdb, "connect_duckdb", refuse)

    def unread() -> Any:
        raise AssertionError("a batch was read")

    def schema_only(type_: pa.DataType) -> KeyBatches:
        return KeyBatches(pa.schema([("k0", type_)]), unread)

    dict_string = pa.dictionary(pa.int32(), pa.large_string())
    assert keys_comparable(schema_only(pa.int32()), [schema_only(pa.uint64())])
    assert keys_comparable(schema_only(dict_string), [schema_only(pa.string())])
    assert keys_comparable(schema_only(pa.int8()), [])
    assert not keys_comparable(schema_only(pa.int64()), [schema_only(pa.string())])
    assert not keys_comparable(
        schema_only(pa.int32()), [schema_only(pa.int32()), schema_only(pa.float64())]
    )
    assert not keys_comparable(
        schema_only(pa.dictionary(pa.int32(), pa.int64())), [schema_only(pa.int64())]
    )
