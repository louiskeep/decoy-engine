"""GEN-UNIQUE E1: the config-time uniqueness checks, called directly.

`unique` is not a registered option in this slice, so these functions are exercised on plain
mappings. AT-3, AT-4, AT-5, AT-8 and AT-13 get their validate cells here; the end-to-end cells
land with the wiring slice.
"""

from __future__ import annotations

import pytest

from decoy_engine import reasons
from decoy_engine.config import _generate_unique as gu

CONFLICT = str(reasons.config.GENERATE_UNIQUE_CONFLICT)
TOO_SMALL = str(reasons.config.GENERATE_UNIQUE_DOMAIN_TOO_SMALL)
UNSUPPORTED = str(reasons.config.GENERATE_UNIQUE_UNSUPPORTED)
OPTION_TYPE = str(reasons.config.GENERATE_OPTION_TYPE)

MARKER = "MARKER-SECRET-VALUE"


def codes(problems):
    return [code for _, code in problems]


class TestUniqueFlag:
    @pytest.mark.parametrize("value", [True, False])
    def test_real_bool_accepted(self, value):
        assert gu.unique_flag(value, {}) == []

    @pytest.mark.parametrize("value", ["true", "false", 1, 0, 1.0, [], {}, None])
    def test_everything_else_refused(self, value):
        # M6: accepting a string bool turns this red.
        assert gu.unique_flag(value, {}) == [((), OPTION_TYPE)]


class TestUniqueType:
    @pytest.mark.parametrize("type_", sorted(gu.UNIQUE_TYPES))
    def test_supported_types_accept(self, type_):
        assert gu.unique_type_problems(type_, {"unique": True}) == []

    @pytest.mark.parametrize(
        "type_",
        [
            "formula",
            "statistical",
            "derived",
            "derived_aggregate",
            "grouped_series",
            "windowed_date",
            "group_key",
        ],
    )
    def test_other_seven_types_refused_when_true(self, type_):
        assert gu.unique_type_problems(type_, {"unique": True}) == [(("unique",), UNSUPPORTED)]

    @pytest.mark.parametrize("extras", [{"unique": False}, {}, {"unique": None}])
    def test_false_absent_and_null_change_nothing(self, extras):
        assert gu.unique_type_problems("formula", extras) == []

    def test_supported_set_is_exactly_the_four(self):
        assert {"sequence", "categorical", "faker", "reference"} == gu.UNIQUE_TYPES


class TestColumnConflicts:
    def test_no_conflict_without_unique(self):
        extras = {"weights": [1, 2], "distribution": "weighted"}
        assert gu.column_conflict_problems("categorical", extras, pooled=True) == []

    def test_pooled_true_conflicts_on_faker(self):
        got = gu.column_conflict_problems("faker", {"unique": True}, pooled=True)
        assert got == [(("pooled",), CONFLICT)]

    @pytest.mark.parametrize("pooled", [False, None])
    def test_pooled_false_or_unset_is_fine(self, pooled):
        assert gu.column_conflict_problems("faker", {"unique": True}, pooled=pooled) == []

    def test_categorical_weights_conflict(self):
        got = gu.column_conflict_problems("categorical", {"unique": True, "weights": [1, 1]})
        assert got == [(("weights",), CONFLICT)]

    @pytest.mark.parametrize("dist", ["weighted", "fitted"])
    def test_reference_distribution_conflicts(self, dist):
        got = gu.column_conflict_problems("reference", {"unique": True, "distribution": dist})
        assert got == [(("distribution",), CONFLICT)]

    @pytest.mark.parametrize("dist", ["random", "sequential"])
    def test_reference_random_and_sequential_allowed(self, dist):
        assert (
            gu.column_conflict_problems("reference", {"unique": True, "distribution": dist}) == []
        )

    def test_reference_per_parent_bounds_conflict_when_positive(self):
        extras = {"unique": True, "min_per_parent": 1, "max_per_parent": 3}
        got = gu.column_conflict_problems("reference", extras)
        assert got == [(("min_per_parent",), CONFLICT), (("max_per_parent",), CONFLICT)]

    def test_zero_and_null_bounds_are_the_off_state(self):
        extras = {"unique": True, "min_per_parent": 0, "max_per_parent": None}
        assert gu.column_conflict_problems("reference", extras) == []


class TestDistinctCategories:
    def test_duplicates_count_once_in_first_seen_order(self):
        assert gu.distinct_categories(["b", "a", "b", "a", "c"]) == (["b", "a", "c"], False)

    def test_none_is_reported_apart(self):
        assert gu.distinct_categories([None, "a", None]) == (["a"], True)

    def test_unhashable_categories_do_not_raise(self):
        got, has_null = gu.distinct_categories([[1], [1], {"k": 1}, {"k": 1}, "x"])
        assert got == [[1], {"k": 1}, "x"]
        assert has_null is False

    def test_equal_int_and_float_collide_like_python_equality(self):
        assert gu.distinct_categories([1, 1.0, True]) == ([1], False)


def cat_table(rows, cats, **col):
    return {
        "name": "t",
        "row_count": rows,
        "generate_columns": [
            {"name": "c", "type": "categorical", "categories": cats, "unique": True, **col}
        ],
    }


class TestCategoricalDomain:
    def test_five_categories_five_rows_pass(self):
        assert gu.table_unique_problems(cat_table(5, list("abcde"))) == []

    def test_six_rows_refused(self):
        got = gu.table_unique_problems(cat_table(6, list("abcde")))
        assert got == [(("generate_columns", 0), TOO_SMALL)]

    def test_duplicate_categories_count_once(self):
        got = gu.table_unique_problems(cat_table(4, ["a", "a", "b", "b", "c", "c"]))
        assert codes(got) == [TOO_SMALL]

    def test_reusable_none_makes_domain_unbounded(self):
        # AT-3: [None, "a"] at 4 rows is allowed; the draw fills the rest with None.
        assert gu.table_unique_problems(cat_table(4, [None, "a"])) == []

    def test_unhashable_categories_still_checked(self):
        got = gu.table_unique_problems(cat_table(3, [[1], [1]]))
        assert codes(got) == [TOO_SMALL]

    def test_null_probability_does_not_shrink_the_requirement(self):
        # Owner question 3: the check counts every row, so a lucky null draw never decides.
        got = gu.table_unique_problems(cat_table(6, list("abcde"), null_probability=0.5))
        assert codes(got) == [TOO_SMALL]

    def test_unique_false_is_not_checked(self):
        t = cat_table(99, ["a"], unique=False)
        assert gu.table_unique_problems(t) == []

    def test_capacity_goes_through_the_one_shared_rule(self, monkeypatch):
        # M12: a second comparison would ignore this patch and the cell would not flip.
        monkeypatch.setattr(
            "decoy_engine.generation.pool._capacity.unique_capacity_ok", lambda *a, **k: True
        )
        assert gu.table_unique_problems(cat_table(99, ["a"])) == []

    def test_message_inputs_never_include_category_values(self):
        got = gu.table_unique_problems(cat_table(5, [MARKER, "b"]))
        assert MARKER not in repr(got)


def ref(name, parent, **extra):
    return {
        "name": name,
        "type": "reference",
        "reference_table": parent,
        "reference_column": "id",
        **extra,
    }


def pair_table(rows, a="emp_id", b="proj_id", pairs=None, **col):
    return {
        "name": "assign",
        "row_count": rows,
        "unique_together": [[a, b]] if pairs is None else pairs,
        "generate_columns": [ref("emp_id", "emp", **col), ref("proj_id", "proj", **col)],
    }


def parents(emp, proj):
    return [
        {"name": "emp", "row_count": emp, "generate_columns": []},
        {"name": "proj", "row_count": proj, "generate_columns": []},
    ]


class TestPairShape:
    def test_valid_pair_passes(self):
        assert gu.table_unique_problems(pair_table(10)) == []

    def test_reversed_order_is_the_same_pair(self):
        assert gu.table_unique_problems(pair_table(10, a="proj_id", b="emp_id")) == []

    @pytest.mark.parametrize(
        "pairs",
        [
            [["emp_id"]],
            [["emp_id", "proj_id", "x"]],
            [["emp_id", "emp_id"]],
            [["emp_id", "missing"]],
            [[1, 2]],
            ["emp_id"],
            [None],
        ],
    )
    def test_bad_shape_refused(self, pairs):
        got = gu.table_unique_problems(pair_table(10, pairs=pairs))
        assert codes(got) and set(codes(got)) == {CONFLICT}

    def test_non_reference_member_refused(self):
        t = pair_table(10)
        t["generate_columns"][1] = {"name": "proj_id", "type": "sequence", "start": 1}
        assert codes(gu.table_unique_problems(t)) == [CONFLICT]

    def test_member_in_two_entries_refused(self):
        t = pair_table(10, pairs=[["emp_id", "proj_id"], ["proj_id", "emp_id"]])
        assert codes(gu.table_unique_problems(t)) == [CONFLICT]

    @pytest.mark.parametrize(
        "extra",
        [
            {"unique": True},
            {"null_probability": 0.1},
            {"determinism": "fresh"},
            {"weights": [1]},
            {"min_per_parent": 1},
            {"max_per_parent": 2},
            {"distribution": "weighted"},
            {"distribution": "sequential"},
        ],
    )
    def test_blocked_member_options_refused(self, extra):
        got = gu.table_unique_problems(pair_table(10, **extra))
        assert set(codes(got)) == {CONFLICT}

    @pytest.mark.parametrize(
        "extra",
        [
            {"unique": False},
            {"null_probability": 0},
            {"null_probability": 0.0},
            {"min_per_parent": 0},
            {"max_per_parent": 0},
            {"unique": None},
        ],
    )
    def test_explicit_off_values_count_as_absent(self, extra):
        assert gu.table_unique_problems(pair_table(10, **extra)) == []

    def test_default_distribution_and_stable_determinism_allowed(self):
        t = pair_table(10, distribution="random", determinism="stable")
        assert gu.table_unique_problems(t) == []


class TestCrossTable:
    def test_reference_child_equal_to_parent_rows_passes(self):
        child = {"name": "c", "row_count": 100, "generate_columns": [ref("p", "emp", unique=True)]}
        assert gu.cross_table_unique_problems([*parents(100, 1), child]) == []

    def test_reference_child_above_parent_rows_refused(self):
        child = {"name": "c", "row_count": 101, "generate_columns": [ref("p", "emp", unique=True)]}
        got = gu.cross_table_unique_problems([*parents(100, 1), child])
        assert got == [(("tables", 2, "generate_columns", 0), TOO_SMALL)]

    def test_non_unique_reference_never_checked(self):
        child = {"name": "c", "row_count": 999, "generate_columns": [ref("p", "emp")]}
        assert gu.cross_table_unique_problems([*parents(1, 1), child]) == []

    def test_unknown_or_source_parent_is_left_to_the_run(self):
        child = {"name": "c", "row_count": 999, "generate_columns": [ref("p", "src", unique=True)]}
        assert gu.cross_table_unique_problems([child]) == []

    def test_pair_30_by_10_holds_300_rows(self):
        assert gu.cross_table_unique_problems([*parents(30, 10), pair_table(300)]) == []

    def test_pair_30_by_10_refuses_301_rows(self):
        got = gu.cross_table_unique_problems([*parents(30, 10), pair_table(301)])
        assert got == [(("tables", 2, "unique_together", 0), TOO_SMALL)]

    def test_pair_over_one_parent_twice_uses_the_square(self):
        t = {
            "name": "friends",
            "row_count": 100,
            "unique_together": [["a", "b"]],
            "generate_columns": [ref("a", "emp"), ref("b", "emp")],
        }
        assert gu.cross_table_unique_problems([*parents(10, 1), t]) == []
        t["row_count"] = 101
        assert codes(gu.cross_table_unique_problems([*parents(10, 1), t])) == [TOO_SMALL]

    def test_non_string_pair_names_do_not_crash(self):
        t = pair_table(999, pairs=[[["x"], ["y"]]])
        assert gu.cross_table_unique_problems([*parents(1, 1), t]) == []
        assert codes(gu.table_unique_problems(t)) == [CONFLICT]

    def test_non_string_reference_table_does_not_crash(self):
        col = ref("p", ["emp"], unique=True)
        child = {"name": "c", "row_count": 999, "generate_columns": [col]}
        assert gu.cross_table_unique_problems([*parents(1, 1), child]) == []
        t = pair_table(999)
        t["generate_columns"][0]["reference_table"] = ["emp"]
        assert gu.cross_table_unique_problems([*parents(1, 1), t]) == []

    def test_malformed_pair_is_skipped_here_and_refused_by_the_table_check(self):
        t = pair_table(999, pairs=[["emp_id"]])
        assert gu.cross_table_unique_problems([*parents(1, 1), t]) == []
        assert codes(gu.table_unique_problems(t)) == [CONFLICT]

    def test_product_beyond_sys_maxsize_is_not_a_validate_overflow(self):
        big = 2**40
        got = gu.cross_table_unique_problems([*parents(big, big), pair_table(10)])
        assert got == []
