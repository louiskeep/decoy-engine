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

from decoy_engine.profiling import KeyBatches, KeyFacts, _key_loading, _scan_integrity, key_facts

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


def test_typed_empty_load_uses_schema_types_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[tuple[str, str]]] = []

    def read_columns(conn: Any) -> None:
        seen.append(
            conn.cursor()
            .execute("SELECT table_name, data_type FROM duckdb_columns() WHERE column_name = 'k0'")
            .fetchall()
        )

    monkeypatch.setattr(_scan_integrity, "_before_fetch", read_columns)
    parent = _keys(pa.array([], pa.int32()))
    child = _keys(pa.array([], pa.uint64()))
    facts = _run(tmp_path, parent, child)
    assert _shape(facts) == ("checked", 0, 0, 0, ((0, 0, 0, 0),))
    assert seen
    for columns in seen:
        assert {data_type for _, data_type in columns} == {"HUGEINT"}
        assert {name.split("_")[0] for name, _ in columns} >= {"parent", "child"}


STRINGS = [
    pa.string(),
    pa.large_string(),
    pa.dictionary(pa.int32(), pa.string()),
    pa.dictionary(pa.int8(), pa.large_string()),
]


def _schema_only(type_: pa.DataType) -> KeyBatches:
    return KeyBatches(pa.schema([("k0", type_)]), lambda: iter(()))


def test_same_family_pairs_load_by_value_never_as_tokens() -> None:
    for group in (INTS, STRINGS):
        for p, c in itertools.product(group, group):
            kinds = _key_loading.duck_types([_schema_only(p).schema, _schema_only(c).schema])
            assert kinds != [_key_loading.JOIN_KEY_KIND], (p, c)
            if not p.equals(c):
                assert kinds != [None], (p, c)


@pytest.mark.parametrize(
    ("ptype", "ctype"),
    [
        (pa.timestamp("us", tz="UTC"), pa.timestamp("ns", tz="Europe/Paris")),
        (pa.duration("us"), pa.duration("ms")),
    ],
    ids=lambda t: str(t),
)
def test_zone_aware_timestamps_and_durations_compare_by_value_in_a_common_unit(
    ptype: pa.DataType, ctype: pa.DataType
) -> None:
    (kind,) = _key_loading.duck_types([_schema_only(ptype).schema, _schema_only(ctype).schema])
    assert isinstance(kind, pa.DataType)
    assert kind.unit == ("ns" if pa.types.is_timestamp(kind) else "us")


@pytest.mark.parametrize(
    ("ptype", "ctype"),
    [
        (pa.int64(), pa.string()),
        (pa.int64(), pa.float64()),
        (pa.bool_(), pa.int8()),
        (pa.date32(), pa.timestamp("us")),
        (pa.timestamp("us"), pa.timestamp("ns")),
        (pa.decimal128(10, 2), pa.int64()),
        (pa.int64(), pa.list_(pa.int64())),
    ],
    ids=lambda t: str(t),
)
def test_other_mixes_go_through_the_join_tokens(ptype: pa.DataType, ctype: pa.DataType) -> None:
    kinds = _key_loading.duck_types([_schema_only(ptype).schema, _schema_only(ctype).schema])
    assert kinds == [_key_loading.JOIN_KEY_KIND]


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
    ("parent", "child", "orphans"),
    [
        (pa.array(["1", "2"], pa.string()), pa.array([1, 9], pa.int64()), 2),
        (pa.array([1.0, 2.0], pa.float64()), pa.array([1, 9], pa.int64()), 1),
        (pa.array([1, 2], pa.int64()).dictionary_encode(), pa.array([1, 9], pa.int64()), 1),
        (pa.array([1, 2], pa.int64()), pa.array(["1", "9"], pa.string()), 2),
        (pa.array([1], pa.int64()), pa.array([[1]]), 1),
    ],
    ids=["string-int", "float-int", "int-dictionary-int", "int-string", "int-list"],
)
def test_mixed_pairs_follow_the_join_token_rule(
    tmp_path: Path, parent: pa.Array, child: pa.Array, orphans: int
) -> None:
    """The string "7" is not the integer 7 to the join, so a string parent and an integer child
    share no key."""
    facts = _run(tmp_path, _keys(parent), _keys(child))
    assert facts.status == "checked"
    assert facts.children[0].orphan_rows == orphans


def test_a_value_the_join_refuses_is_a_key_type_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(value: object) -> str:
        raise ValueError(f"secret {value!r}")

    monkeypatch.setattr(_key_loading, "fk_join_key", refuse)
    facts = _run(tmp_path, _keys(pa.array([1], pa.int64())), _keys(pa.array(["1"])))
    assert facts.status == "key_type_mismatch"
    assert facts.children == ()


def test_a_read_error_is_not_a_key_type_mismatch(tmp_path: Path) -> None:
    def broken() -> Any:
        raise pa.ArrowInvalid("unreadable batch")
        yield  # pragma: no cover

    keys = KeyBatches(pa.schema([("k0", pa.int64())]), broken)
    with pytest.raises(pa.ArrowInvalid):
        _run(tmp_path, keys, _keys(pa.array([1], pa.int64())))


def test_a_label_mixed_across_parents_still_compares_the_rest(tmp_path: Path) -> None:
    parent = _keys(pa.array([1, 2], pa.int64()))
    other = _keys(pa.array(["a"], pa.string()))
    child = _keys(pa.array([1, 3], pa.int64()))
    facts = _run(tmp_path, parent, other, child)
    assert facts.status == "checked"
    assert [c.orphan_rows for c in facts.children] == [1, 1]
