"""GEN-UNIQUE E2: the unique draws (AT-1, AT-3, AT-4, AT-5, AT-6) on the whole-table route."""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from decoy_engine import reasons
from decoy_engine.generation.pool import GenerationError
from decoy_engine.internal.faker_setup import register_faker_provider, unregister_faker_provider
from tests.unit.generation.unique._gu_e2_support import (
    col_values,
    duplicates,
    error_types,
    pair_duplicates,
    ref,
    run,
    seq_parent,
    table,
    validate,
)

TOO_SMALL = str(reasons.config.GENERATE_UNIQUE_DOMAIN_TOO_SMALL)
CONFLICT = str(reasons.config.GENERATE_UNIQUE_CONFLICT)
IMPOSSIBLE = str(reasons.runtime.UNIQUENESS_IMPOSSIBLE)


def _faker(name: str, faker_type: str, **extra):
    return {"name": name, "type": "faker", "faker_type": faker_type, **extra}


def _users(rows: int, *, unique: bool, plain_pooled_off: bool = False):
    extra = {"unique": True} if unique else ({"pooled": False} if plain_pooled_off else {})
    return [
        table(
            "users",
            rows,
            [_faker("email", "email", **extra), _faker("user_name", "user_name", **extra)],
        )
    ]


class TestAT1FakerDistinct:
    def test_email_and_user_name_have_no_duplicates_at_200k(self):
        out = run(_users(200_000, unique=True))
        assert duplicates(col_values(out, "users", "email")) == 0
        assert duplicates(col_values(out, "users", "user_name")) == 0

    def test_non_vacuity_the_same_columns_collide_without_unique(self):
        out = run(_users(30_000, unique=False))
        assert duplicates(col_values(out, "users", "email")) > 0
        assert duplicates(col_values(out, "users", "user_name")) > 0

    def test_pool_eligible_name_is_distinct_at_60k(self):
        out = run([table("t", 60_000, [_faker("name", "name", unique=True)])])
        assert duplicates(col_values(out, "t", "name")) == 0

    def test_non_vacuity_pooled_name_collides_at_60k(self):
        out = run([table("t", 60_000, [_faker("name", "name")])])
        assert duplicates(col_values(out, "t", "name")) > 0

    @pytest.mark.parametrize(
        ("column", "faker_type"), [("email", "email"), ("user_name", "user_name")]
    )
    def test_u5_first_draw_rows_keep_the_pooled_false_value(self, column, faker_type):
        # M10 (leaving `unique` in the fingerprint) changes the seed root: every row differs.
        rows = 30_000
        uniq = run([table("t", rows, [_faker(column, faker_type, unique=True)])])
        plain = run([table("t", rows, [_faker(column, faker_type, pooled=False)])])
        out_v = col_values(uniq, "t", column)
        plain_v = col_values(plain, "t", column)
        seen: set = set()
        kept = changed = 0
        for i in range(rows):
            if plain_v[i] is None or plain_v[i] not in seen:
                assert out_v[i] == plain_v[i], i
                kept += 1
            else:
                assert out_v[i] != plain_v[i], i
                changed += 1
            if out_v[i] is not None:
                seen.add(out_v[i])
        assert kept > 0
        assert changed > 0, "the comparison must exercise at least one retry"

    def test_two_runs_in_one_process_are_byte_equal(self):
        # M3 (module-scope seen set) makes the second run diverge.
        tables = _users(5_000, unique=True)
        a, b = run(tables), run(tables)
        assert a["users"].equals(b["users"])


class TestAT2FakerTooSmall:
    TABLES = [table("users", 50_000, [_faker("first_name", "first_name", unique=True)])]

    def test_validate_passes(self):
        validate(self.TABLES)

    def test_run_refuses_with_a_coded_value_free_error(self):
        # M2 (return a padded prefix) turns this red.
        with pytest.raises(GenerationError) as exc:
            run(self.TABLES)
        assert exc.value.code == IMPOSSIBLE
        message = exc.value.message
        assert "users.first_name" in message
        assert "ran out at row" in message
        assert "distinct" in message


class TestAT3Categorical:
    def test_five_categories_five_rows_is_a_permutation(self):
        cats = ["a", "b", "c", "d", "e"]
        out = run(
            [
                table(
                    "t",
                    5,
                    [{"name": "c", "type": "categorical", "categories": cats, "unique": True}],
                )
            ]
        )
        assert sorted(col_values(out, "t", "c")) == cats

    def test_non_vacuity_plain_draw_repeats(self):
        cats = [f"c{i}" for i in range(40)]
        out = run([table("t", 40, [{"name": "c", "type": "categorical", "categories": cats}])])
        assert duplicates(col_values(out, "t", "c")) > 0

    def test_unique_draw_is_distinct_for_a_large_domain(self):
        cats = [f"c{i}" for i in range(40)]
        out = run(
            [
                table(
                    "t",
                    40,
                    [{"name": "c", "type": "categorical", "categories": cats, "unique": True}],
                )
            ]
        )
        assert duplicates(col_values(out, "t", "c")) == 0

    def test_six_rows_over_five_categories_refused_at_validate(self):
        cats = ["a", "b", "c", "d", "e"]
        with pytest.raises(ValidationError) as exc:
            validate(
                [
                    table(
                        "t",
                        6,
                        [{"name": "c", "type": "categorical", "categories": cats, "unique": True}],
                    )
                ]
            )
        assert TOO_SMALL in error_types(exc)

    def test_duplicate_category_entries_count_once(self):
        with pytest.raises(ValidationError) as exc:
            validate(
                [
                    table(
                        "t",
                        3,
                        [
                            {
                                "name": "c",
                                "type": "categorical",
                                "categories": ["a", "a", "b"],
                                "unique": True,
                            }
                        ],
                    )
                ]
            )
        assert TOO_SMALL in error_types(exc)

    def test_weights_with_unique_is_a_conflict(self):
        col = {
            "name": "c",
            "type": "categorical",
            "categories": ["a", "b"],
            "weights": [1, 2],
            "unique": True,
        }
        with pytest.raises(ValidationError) as exc:
            validate([table("t", 2, [col])])
        assert CONFLICT in error_types(exc)

    def test_a_none_category_fills_the_remaining_rows_with_none(self):
        col = {"name": "c", "type": "categorical", "categories": [None, "a"], "unique": True}
        out = col_values(run([table("t", 4, [col])]), "t", "c")
        assert out.count("a") == 1
        assert out.count(None) == 3


class TestAT4Reference:
    def _child(self, rows: int, **extra):
        return table("child", rows, [ref("pid", "parent", unique=True, **extra)])

    def test_child_uses_every_parent_key_once(self):
        out = run([seq_parent("parent", 100), self._child(100)])
        assert sorted(col_values(out, "child", "pid")) == sorted(col_values(out, "parent", "id"))

    def test_non_vacuity_plain_reference_repeats(self):
        out = run([seq_parent("parent", 100), table("child", 100, [ref("pid", "parent")])])
        assert duplicates(col_values(out, "child", "pid")) > 0

    def test_child_larger_than_parent_refused_at_validate(self):
        with pytest.raises(ValidationError) as exc:
            validate([seq_parent("parent", 100), self._child(101)])
        assert TOO_SMALL in error_types(exc)

    def test_parent_with_nulls_passes_validate_and_refuses_at_run(self):
        # M7 (skip the run-time check when validate passed) turns this red.
        tables = [seq_parent("parent", 100, null_probability=0.5), self._child(80)]
        validate(tables)
        with pytest.raises(GenerationError) as exc:
            run(tables)
        assert exc.value.code == IMPOSSIBLE
        assert "child.pid" in exc.value.message

    def test_all_null_parent_refuses_instead_of_emitting_none(self):
        # M14 (return before the capacity check on an empty parent) turns this red.
        tables = [seq_parent("parent", 10, null_probability=1.0), self._child(5)]
        validate(tables)
        with pytest.raises(GenerationError) as exc:
            run(tables)
        assert exc.value.code == IMPOSSIBLE

    def test_sequential_with_unique_equals_plain_sequential(self):
        plain = run(
            [
                seq_parent("parent", 100),
                table("child", 60, [ref("pid", "parent", distribution="sequential")]),
            ]
        )
        uniq = run([seq_parent("parent", 100), self._child(60, distribution="sequential")])
        assert col_values(plain, "child", "pid") == col_values(uniq, "child", "pid")

    def test_sequential_with_unique_refuses_instead_of_wrapping(self):
        tables = [
            seq_parent("parent", 10, null_probability=0.5),
            self._child(8, distribution="sequential"),
        ]
        validate(tables)
        with pytest.raises(GenerationError) as exc:
            run(tables)
        assert exc.value.code == IMPOSSIBLE

    def test_every_child_value_exists_in_the_parent(self):
        out = run([seq_parent("parent", 100), self._child(70)])
        assert set(col_values(out, "child", "pid")) <= set(col_values(out, "parent", "id"))


def _junction(rows: int, *, pair: bool, pa_rows: int = 30, pb_rows: int = 10):
    extra = {"unique_together": [["emp_id", "project_id"]]} if pair else {}
    return [
        seq_parent("employees", pa_rows),
        seq_parent("projects", pb_rows),
        table(
            "assignments",
            rows,
            [
                {**ref("emp_id", "employees")},
                {**ref("project_id", "projects")},
            ],
            **extra,
        ),
    ]


class TestAT5Pair:
    def test_all_300_pairs_appear_once(self):
        out = run(_junction(300, pair=True))
        a, b = (
            col_values(out, "assignments", "emp_id"),
            col_values(out, "assignments", "project_id"),
        )
        assert pair_duplicates(a, b) == 0
        assert len(set(zip(a, b, strict=True))) == 300

    def test_301_rows_refused_at_validate(self):
        with pytest.raises(ValidationError) as exc:
            validate(_junction(301, pair=True))
        assert TOO_SMALL in error_types(exc)

    def test_qa_shape_has_no_duplicate_pairs_and_the_plain_config_has_some(self):
        # M8 (plain draw in the unique branch) turns the first assertion red.
        uniq = run(_junction(300, pair=True, pa_rows=50, pb_rows=50))
        plain = run(_junction(300, pair=False, pa_rows=50, pb_rows=50))
        args = ("assignments", "emp_id"), ("assignments", "project_id")
        assert pair_duplicates(col_values(uniq, *args[0]), col_values(uniq, *args[1])) == 0
        assert pair_duplicates(col_values(plain, *args[0]), col_values(plain, *args[1])) >= 1

    def test_one_parent_used_twice_gives_distinct_ordered_pairs(self):
        tables = [
            seq_parent("users", 20),
            table(
                "friends",
                100,
                [ref("user_a", "users"), ref("user_b", "users")],
                unique_together=[["user_a", "user_b"]],
            ),
        ]
        out = run(tables)
        a, b = col_values(out, "friends", "user_a"), col_values(out, "friends", "user_b")
        assert pair_duplicates(a, b) == 0

    def test_declared_column_order_survives_and_the_other_column_is_unchanged(self):
        # M13 (assign data[b] at a's turn) reorders the columns.
        def cols():
            return [
                ref("a", "pa"),
                {"name": "x", "type": "categorical", "categories": ["p", "q", "r"]},
                ref("b", "pb"),
            ]

        parents = [seq_parent("pa", 20), seq_parent("pb", 20)]
        with_pair = run([*parents, table("t", 50, cols(), unique_together=[["b", "a"]])])
        without = run([*parents, table("t", 50, cols())])
        assert with_pair["t"].column_names == ["a", "x", "b"]
        assert col_values(with_pair, "t", "x") == col_values(without, "t", "x")

    @pytest.mark.parametrize(
        "bad",
        [
            [["a"]],
            [["a", "b", "c"]],
            [["a", "nope"]],
            [["a", "x"]],
            [["a", "a"]],
            [["a", "b"], ["b", "c"]],
        ],
    )
    def test_member_rules_refuse_with_conflict(self, bad):
        parents = [seq_parent("pa", 20), seq_parent("pb", 20), seq_parent("pc", 20)]
        cols = [
            ref("a", "pa"),
            ref("b", "pb"),
            ref("c", "pc"),
            {"name": "x", "type": "categorical", "categories": ["p"]},
        ]
        with pytest.raises(ValidationError) as exc:
            validate([*parents, table("t", 5, cols, unique_together=bad)])
        assert CONFLICT in error_types(exc)

    @pytest.mark.parametrize(
        "extra", [{"unique": True}, {"null_probability": 0.1}, {"determinism": "fresh"}]
    )
    def test_member_options_refuse_with_conflict(self, extra):
        cols = [ref("a", "pa", **extra), ref("b", "pb")]
        with pytest.raises(ValidationError) as exc:
            validate(
                [
                    seq_parent("pa", 20),
                    seq_parent("pb", 20),
                    table("t", 5, cols, unique_together=[["a", "b"]]),
                ]
            )
        assert CONFLICT in error_types(exc)

    def test_product_beyond_maxsize_refuses_not_overflows(self, monkeypatch):
        from decoy_engine.generation import _unique

        class Huge:
            def __len__(self):
                return 2**62

        monkeypatch.setattr(_unique, "parent_keys", lambda *a, **k: Huge())
        with pytest.raises(GenerationError) as exc:
            run(_junction(5, pair=True))
        assert exc.value.code == IMPOSSIBLE

    def test_population_stop_is_exactly_the_product(self, monkeypatch):
        # M5 (draw from range(P_a * P_b + 1)) turns this red.
        populations = []

        class Spy(random.Random):
            def sample(self, population, k, **kw):
                populations.append(population)
                return super().sample(population, k, **kw)

        monkeypatch.setattr(random, "Random", Spy)
        run(_junction(20, pair=True))
        ranges = [p for p in populations if isinstance(p, range)]
        assert [(r.start, r.stop) for r in ranges] == [(0, 300)]


class TestAT6Nulls:
    def test_categorical_with_null_probability(self):
        cats = [f"c{i}" for i in range(30)]
        col = {
            "name": "c",
            "type": "categorical",
            "categories": cats,
            "unique": True,
            "null_probability": 0.3,
        }
        out = col_values(run([table("t", 30, [col])]), "t", "c")
        assert duplicates(out) == 0
        assert out.count(None) >= 3

    def test_row_count_above_category_count_still_refused_with_null_probability(self):
        col = {
            "name": "c",
            "type": "categorical",
            "categories": ["a", "b", "c"],
            "unique": True,
            "null_probability": 0.5,
        }
        with pytest.raises(ValidationError) as exc:
            validate([table("t", 4, [col])])
        assert TOO_SMALL in error_types(exc)

    def test_a_provider_returning_none_emits_nulls_without_retry_or_refusal(self):
        register_faker_provider("gu_none", lambda fake: None)
        try:
            out = run([table("t", 25, [_faker("c", "gu_none", unique=True)])])
        finally:
            unregister_faker_provider("gu_none")
        assert col_values(out, "t", "c") == [None] * 25
