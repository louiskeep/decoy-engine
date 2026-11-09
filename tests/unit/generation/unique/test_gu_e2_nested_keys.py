"""GEN-UNIQUE E2: list and dict parent keys are tracked by equality, never by hash (Codex V1)."""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.config import value_key
from decoy_engine.generation import _unique
from decoy_engine.generation._synthesize_derived import parent_keys
from decoy_engine.generation.pool import GenerationError
from decoy_engine.generators.derivation import GenDeriveContext, with_table
from tests.unit.generation.unique._gu_e2_support import run, table

IMPOSSIBLE = str(reasons.runtime.UNIQUENESS_IMPOSSIBLE)

LISTS = [[1, 2], [3], [1, 2], None, [4, 5, 6], [3]]
DICTS = [{"a": 1}, {"b": 2}, {"a": 1}, None, {"c": 3}, {"b": 2}]


def _pools(values):
    return {"p": pa.table({"k": values})}


def _col(table_name="child", name="c", **extra):
    return {
        "name": name,
        "type": "reference",
        "reference_table": "p",
        "reference_column": "k",
        "_table": table_name,
        **extra,
    }


@pytest.mark.parametrize("values", [LISTS, DICTS])
class TestNestedParentKeys:
    def test_parent_keys_dedup_by_equality_in_first_seen_order(self, values):
        got = parent_keys(_pools(values), "p", "k")
        assert got == [v for i, v in enumerate(values) if v is not None and v not in values[:i]]
        assert len(got) == 3

    def test_unique_reference_gives_distinct_parent_keys(self, values):
        keys = parent_keys(_pools(values), "p", "k")
        out = _unique.unique_reference(_col(unique=True), keys, 3, 7)
        assert all(out.count(v) == 1 for v in out)
        assert sorted(map(repr, out)) == sorted(map(repr, keys))

    def test_unique_reference_refuses_past_the_distinct_count(self, values):
        keys = parent_keys(_pools(values), "p", "k")
        with pytest.raises(GenerationError) as exc:
            _unique.unique_reference(_col(unique=True), keys, 4, 7)
        assert exc.value.code == IMPOSSIBLE

    def test_unique_pair_gives_distinct_pairs(self, values):
        pools = _pools(values)
        a, b = _col(name="a"), _col(name="b")
        got_a, got_b = _unique.unique_pair(a, b, 9, 7, None, pools, table_name="j")
        pairs = [(value_key(x), value_key(y)) for x, y in zip(got_a, got_b, strict=True)]
        assert len(set(pairs)) == 9

    def test_unique_pair_refuses_past_the_product(self, values):
        a, b = _col(name="a"), _col(name="b")
        with pytest.raises(GenerationError) as exc:
            _unique.unique_pair(a, b, 10, 7, None, _pools(values), table_name="j")
        assert exc.value.code == IMPOSSIBLE


@pytest.mark.parametrize("categories", [[[1, 2], [3], [4, 5]], [{"a": 1}, {"a": 2}, {"a": 3}]])
def test_end_to_end_unique_reference_over_a_nested_parent(categories):
    parent = table("p", 40, [{"name": "k", "type": "categorical", "categories": categories}])
    child = table(
        "child",
        3,
        [
            {
                "name": "c",
                "type": "reference",
                "reference_table": "p",
                "reference_column": "k",
                "unique": True,
            }
        ],
    )
    got = run([parent, child]).get("child")
    assert got is not None
    values = got.column("c").to_pylist()
    assert len(values) == 3 and all(values.count(v) == 1 for v in values)


class _Inst:
    def seed_instance(self, seed):
        pass


def _faker_ctx():
    col = with_table({"name": "c", "type": "faker", "faker_type": "x", "unique": True}, "t")
    return GenDeriveContext.for_column(derive_key=None, column_config=col, fallback_seed=7)


class TestFakerSeenSetUsesTheSameKeyRule:
    def test_list_and_dict_outputs_are_tracked_by_equality(self):
        calls = iter([[1], [1], [2], {"a": 1}, {"a": 1}, {"a": 2}])
        out = _unique.unique_faker(
            _Inst(), lambda **_: next(calls), {}, _faker_ctx(), 4, table="t", column="c"
        )
        assert out == [[1], [2], {"a": 1}, {"a": 2}]

    def test_an_unkeyable_output_refuses_with_its_own_remedy(self):
        with pytest.raises(GenerationError) as exc:
            _unique.unique_faker(
                _Inst(), lambda **_: {1, 2}, {}, _faker_ctx(), 2, table="t", column="c"
            )
        assert exc.value.code == IMPOSSIBLE
        assert "cannot be compared" in exc.value.message
        assert "domain ran out" not in exc.value.message
        assert exc.value.__cause__ is None and exc.value.__context__ is None


def test_value_key_separates_types_that_only_look_alike():
    assert value_key([1]) != value_key((1,))
    assert value_key({"a": [1]}) == value_key({"a": [1]})
    assert value_key({"a": 1, "b": 2}) == value_key({"b": 2, "a": 1})
