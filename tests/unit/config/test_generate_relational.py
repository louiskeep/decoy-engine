"""GEN-REL config-time checks for per-parent bounds (AT-2, AT-2b, AT-3, AT-4, AT-6f), for
self-reference trees (AT-7, AT-9) and for the parent lookup of an anchored date (AT-11).

Every refusal is a registered `config` code raised through `PipelineConfig`, in a fixed order:
`min > max`, then fitted with a bound, then nulls with a bound, then the row-count ceiling. A
self-reference reports its key first, then an option it cannot honor, then `root_count`. A
`windowed_date` with `anchor_via` needs a foreign key that reaches exactly one parent row.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from decoy_engine import config as config_api
from decoy_engine import reasons
from decoy_engine.generation.pool import unique_capacity_ok
from tests.unit.generation.unique._gu_e2_support import (
    error_types,
    ref,
    seq_parent,
    table,
    validate,
)

BOUNDS_CONFLICT = "generate_per_parent_bounds_conflict"
NULL_CONFLICT = "generate_per_parent_null_conflict"
INFEASIBLE = "generate_per_parent_infeasible"
FITTED_CONFLICT = "generate_fitted_reference_bounds_conflict"
KEY_INVALID = "generate_self_reference_key_invalid"
OPTION_UNSUPPORTED = "generate_self_reference_option_unsupported"
OPTION_RANGE = "generate_option_range"
OPTION_UNKNOWN = "generate_option_unknown"
OPTION_TYPE = "generate_option_type"
ID = {"name": "id", "type": "sequence", "start": 1}


def _tables(
    parent_rows: int, child_rows: int, parent: dict[str, Any] | None = None, **options: Any
) -> list[dict[str, Any]]:
    return [
        parent or seq_parent("parents", parent_rows),
        table("kids", child_rows, [ref("pid", "parents", **options)]),
    ]


def _refused(tables: list[dict[str, Any]]) -> tuple[list[str], list[tuple[Any, ...]]]:
    with pytest.raises(ValidationError) as exc:
        validate(tables)
    return error_types(exc), [e["loc"] for e in exc.value.errors()]


# ---- AT-2: min above max -----------------------------------------------------------------------


def test_at2_min_above_max_is_refused() -> None:
    codes, locs = _refused(_tables(10, 20, min_per_parent=4, max_per_parent=2))
    assert codes == [BOUNDS_CONFLICT]
    assert locs == [("tables", 1, "generate_columns", 0, "min_per_parent")]
    assert reasons.config.GENERATE_PER_PARENT_BOUNDS_CONFLICT == BOUNDS_CONFLICT


@pytest.mark.parametrize(
    "bounds",
    [
        {"min_per_parent": 2, "max_per_parent": 2},
        {"min_per_parent": 4, "max_per_parent": 0},  # zero max is unset, not a ceiling of none
        {"min_per_parent": 0, "max_per_parent": 2},
        {"min_per_parent": 1},
        {"max_per_parent": 3},
    ],
)
def test_at2_consistent_bounds_validate(bounds: dict[str, int]) -> None:
    assert validate(_tables(10, 20, **bounds))


# ---- AT-2b: nulls would undo the repair --------------------------------------------------------


@pytest.mark.parametrize("bound", ["min_per_parent", "max_per_parent"])
def test_at2b_null_probability_with_a_positive_bound_is_refused(bound: str) -> None:
    codes, locs = _refused(_tables(10, 20, null_probability=0.1, **{bound: 3}))
    assert codes == [NULL_CONFLICT]
    assert locs == [("tables", 1, "generate_columns", 0, "null_probability")]


@pytest.mark.parametrize(
    "options",
    [
        {"null_probability": 0.1},
        {"null_probability": 0.1, "min_per_parent": 0, "max_per_parent": 0},
        {"null_probability": 0, "max_per_parent": 3},
        {"null_probability": 0.0, "min_per_parent": 1},
    ],
)
def test_at2b_an_explicit_zero_is_off(options: dict[str, Any]) -> None:
    assert validate(_tables(10, 20, **options))


# ---- AT-3 and AT-4: the validate side checks max against the parent row count ------------------


def test_at3_max_below_the_row_count_ceiling_is_refused() -> None:
    codes, locs = _refused(_tables(10, 20, max_per_parent=1))
    assert codes == [INFEASIBLE]
    assert locs == [("tables", 1, "generate_columns", 0, "max_per_parent")]


def test_at3_equality_is_feasible_and_min_waits_for_the_run() -> None:
    assert validate(_tables(10, 20, max_per_parent=2))
    assert validate(_tables(10, 20, min_per_parent=2, max_per_parent=2))
    # min * row_count > N cannot be judged here: a nullable or repeated key shrinks the real P.
    assert validate(_tables(10, 20, min_per_parent=3))


def test_at4_validate_is_conservative_for_a_parent_with_repeated_keys() -> None:
    parent = table(
        "parents", 4, [{"name": "id", "type": "categorical", "categories": ["a", "b", "c"]}]
    )
    assert validate(_tables(4, 20, parent, max_per_parent=6))
    assert validate(_tables(4, 20, parent, max_per_parent=5))
    codes, _ = _refused(_tables(4, 20, parent, max_per_parent=4))
    assert codes == [INFEASIBLE]


def test_an_empty_parent_or_child_skips_the_ceiling() -> None:
    assert validate(_tables(0, 20, max_per_parent=1))
    assert validate(_tables(10, 0, max_per_parent=1))


def test_each_bounded_column_is_reported() -> None:
    tables = [
        seq_parent("parents", 10),
        table(
            "kids",
            20,
            [
                ref("a", "parents", max_per_parent=1),
                ref("b", "parents", min_per_parent=4, max_per_parent=2),
            ],
        ),
    ]
    codes, locs = _refused(tables)
    assert codes == [INFEASIBLE, BOUNDS_CONFLICT]
    assert [loc[3] for loc in locs] == [0, 1]


# ---- AT-6f: fitted with a bound, and the refusal order -----------------------------------------


@pytest.mark.parametrize("bound", ["min_per_parent", "max_per_parent"])
def test_at6f_a_positive_bound_with_fitted_is_refused(bound: str) -> None:
    codes, locs = _refused(
        _tables(10, 20, distribution="fitted", snapshot_file="snap.json", **{bound: 2})
    )
    assert codes == [FITTED_CONFLICT]
    assert locs == [("tables", 1, "generate_columns", 0, "distribution")]


def test_at6f_fitted_without_a_positive_bound_validates() -> None:
    fitted = {"distribution": "fitted", "snapshot_file": "snap.json"}
    assert validate(_tables(10, 20, **fitted))
    assert validate(_tables(10, 20, min_per_parent=0, max_per_parent=0, **fitted))
    assert config_api.per_parent_problems(_tables(10, 20, **fitted)) == []


def test_the_refusal_order_is_conflict_then_fitted_then_nulls_then_ceiling() -> None:
    fitted = {"distribution": "fitted", "snapshot_file": "snap.json"}
    everything = {"min_per_parent": 4, "max_per_parent": 1, "null_probability": 0.2, **fitted}
    assert _refused(_tables(10, 20, **everything))[0] == [BOUNDS_CONFLICT]
    everything["min_per_parent"] = 1
    assert _refused(_tables(10, 20, **everything))[0] == [FITTED_CONFLICT]
    del everything["distribution"], everything["snapshot_file"]
    assert _refused(_tables(10, 20, **everything))[0] == [NULL_CONFLICT]
    del everything["null_probability"]
    assert _refused(_tables(10, 20, **everything))[0] == [INFEASIBLE]


# ---- the capacity layer: one predicate for config and generation --------------------------------


def test_config_and_the_pool_facade_share_the_foundation_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine import _capacity

    infeasible = _tables(10, 20, max_per_parent=1)
    unique_child = [
        seq_parent("parents", 2),
        table("kids", 5, [ref("pid", "parents", unique=True)]),
    ]
    assert config_api.per_parent_problems(infeasible)
    assert not unique_capacity_ok(1, 2)
    with pytest.raises(ValidationError):
        validate(unique_child)

    monkeypatch.setattr(_capacity, "unique_capacity_ok", lambda *args, **kwargs: True)
    assert config_api.per_parent_problems(infeasible) == []
    assert validate(infeasible) and validate(unique_child)
    assert unique_capacity_ok(1, 2) is True


def test_the_ceiling_goes_through_the_predicate_with_max_times_row_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine import _capacity

    seen: list[tuple[int, int]] = []

    def spy(pool_size: int, rows: int, null_reusable: bool = False) -> bool:
        seen.append((pool_size, rows))
        return True

    monkeypatch.setattr(_capacity, "unique_capacity_ok", spy)
    assert config_api.per_parent_problems(_tables(10, 20, max_per_parent=3)) == []
    assert seen == [(30, 20)]


def test_problems_are_locations_and_codes_only() -> None:
    marker = "MARKER-SECRET-VALUE"
    tables = _tables(10, 20, min_per_parent=7919, max_per_parent=7907, weights=[marker])
    problems = config_api.per_parent_problems(tables)
    assert problems == [(("tables", 1, "generate_columns", 0, "min_per_parent"), BOUNDS_CONFLICT)]
    assert marker not in repr(problems) and "79" not in repr(problems)


# ---- AT-7: a self-edge is a tree, any other cycle is still a cycle -----------------------------


def _tree(
    rows: int = 10, key: dict[str, Any] | None = None, **options: Any
) -> list[dict[str, Any]]:
    return [table("employees", rows, [key or ID, ref("manager_id", "employees", **options)])]


def _tree_loc(option: str) -> list[tuple[Any, ...]]:
    return [("tables", 0, "generate_columns", 1, option)]


def test_at7_a_self_reference_validates() -> None:
    assert validate(_tree())
    assert reasons.config.GENERATE_SELF_REFERENCE_KEY_INVALID == KEY_INVALID
    assert reasons.config.GENERATE_SELF_REFERENCE_OPTION_UNSUPPORTED == OPTION_UNSUPPORTED


def test_at7_a_two_table_cycle_is_still_refused() -> None:
    cycle = [
        table("a", 5, [ID, ref("x", "b")]),
        table("b", 5, [ID, ref("y", "a")]),
    ]
    with pytest.raises(ValidationError, match="reference cycle in generate config"):
        validate(cycle)


def test_at7_a_self_edge_does_not_hide_a_cycle_through_the_same_table() -> None:
    cycle = [
        table("a", 5, [ID, ref("boss", "a"), ref("x", "b")]),
        table("b", 5, [ID, ref("boss", "b"), ref("y", "c")]),
        table("c", 5, [ID, ref("z", "a")]),
    ]
    with pytest.raises(ValidationError, match="reference cycle in generate config"):
        validate(cycle)
    assert validate([*cycle[:2], table("c", 5, [ID, ref("boss", "c")])])


# ---- AT-9: the key and option matrix of a self-reference ---------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        {"name": "id", "type": "categorical", "categories": ["a", "b", "c"]},
        {"name": "id", "type": "sequence", "start": 1, "null_probability": 0.2},
        {"name": "id", "type": "faker", "faker_type": "uuid4"},
    ],
    ids=["categorical", "nullable-sequence", "faker"],
)
def test_at9_a_key_that_is_not_a_non_null_sequence_is_refused(key: dict[str, Any]) -> None:
    codes, locs = _refused(_tree(key=key))
    assert codes == [KEY_INVALID]
    assert locs == _tree_loc("reference_column")


def test_at9_a_key_declared_after_the_reference_is_refused() -> None:
    late = [table("employees", 10, [ref("manager_id", "employees"), ID])]
    codes, locs = _refused(late)
    assert codes == [KEY_INVALID]
    assert locs == [("tables", 0, "generate_columns", 0, "reference_column")]
    own = [table("employees", 10, [ID, ref("manager_id", "employees", column="manager_id")])]
    assert _refused(own)[0] == [KEY_INVALID]


def test_at9_a_sequence_key_with_an_explicit_zero_null_probability_validates() -> None:
    assert validate(_tree(key={**ID, "null_probability": 0}))


@pytest.mark.parametrize(
    ("options", "option"),
    [
        ({"distribution": "weighted", "weights": [1, 2]}, "distribution"),
        ({"distribution": "sequential"}, "distribution"),
        ({"distribution": "fitted", "snapshot_file": "snap.json"}, "distribution"),
        ({"min_per_parent": 1}, "min_per_parent"),
        ({"min_per_parent": 1, "max_per_parent": 3}, "min_per_parent"),
        ({"null_probability": 0.1}, "null_probability"),
        ({"unique": True}, "unique"),
    ],
    ids=["weighted", "sequential", "fitted", "min", "min-and-max", "nulls", "unique"],
)
def test_at9_an_option_a_tree_cannot_honor_is_refused(options: dict[str, Any], option: str) -> None:
    codes, locs = _refused(_tree(**options))
    assert codes == [OPTION_UNSUPPORTED]
    assert locs == _tree_loc(option)


def test_at9_a_self_reference_in_a_unique_pair_is_refused() -> None:
    tables = [
        seq_parent("depts", 4),
        table(
            "employees",
            10,
            [ID, ref("dept", "depts"), ref("manager_id", "employees")],
            unique_together=[["dept", "manager_id"]],
        ),
    ]
    codes, locs = _refused(tables)
    assert codes == [OPTION_UNSUPPORTED]
    assert locs == [("tables", 1, "unique_together")]


@pytest.mark.parametrize(
    "options",
    [
        {"max_per_parent": 2},
        {"max_per_parent": 1},
        {"distribution": "random"},
        {"distribution": "random", "max_per_parent": 2, "root_count": 3},
        {"min_per_parent": 0, "max_per_parent": 0, "null_probability": 0},
        {"unique": False},
    ],
)
def test_at9_random_with_an_optional_max_is_the_allowed_shape(options: dict[str, Any]) -> None:
    assert validate(_tree(**options))


@pytest.mark.parametrize("root_count", [1, 2, 9, 10])
def test_at9_root_count_inside_the_row_count_validates(root_count: int) -> None:
    assert validate(_tree(10, root_count=root_count))


@pytest.mark.parametrize("root_count", [0, -1, 11, 7919])
def test_at9_root_count_outside_one_to_row_count_is_refused(root_count: int) -> None:
    codes, locs = _refused(_tree(10, root_count=root_count))
    assert codes == [OPTION_RANGE]
    assert locs == _tree_loc("root_count")


@pytest.mark.parametrize("options", [{}, {"root_count": 5}, {"root_count": 0}, {"root_count": -3}])
def test_at9_an_empty_table_ignores_root_count(options: dict[str, Any]) -> None:
    assert validate(_tree(0, **options))


@pytest.mark.parametrize("root_count", ["2", 1.5, True, None, [1]])
def test_at9_root_count_must_be_an_integer(root_count: Any) -> None:
    codes, locs = _refused(_tree(10, root_count=root_count))
    assert codes == [OPTION_TYPE]
    assert locs == _tree_loc("root_count")


@pytest.mark.parametrize("root_count", [1, 0, 3])
def test_at9_root_count_on_a_reference_to_another_table_is_unknown(root_count: int) -> None:
    codes, locs = _refused(_tables(10, 20, root_count=root_count))
    assert codes == [OPTION_UNKNOWN]
    assert locs == [("tables", 1, "generate_columns", 0, "root_count")]


def test_at9_the_refusal_order_is_key_then_option_then_root_count() -> None:
    everything = {"distribution": "sequential", "min_per_parent": 2, "root_count": 99}
    bad_key = {"name": "id", "type": "categorical", "categories": ["a", "b"]}
    assert _refused(_tree(key=bad_key, **everything))[0] == [KEY_INVALID]
    codes, locs = _refused(_tree(**everything))
    assert (codes, locs) == ([OPTION_UNSUPPORTED], _tree_loc("distribution"))
    del everything["distribution"]
    codes, locs = _refused(_tree(**everything))
    assert (codes, locs) == ([OPTION_UNSUPPORTED], _tree_loc("min_per_parent"))
    del everything["min_per_parent"]
    assert _refused(_tree(**everything))[0] == [OPTION_RANGE]


def test_at9_a_self_reference_reports_one_problem_not_a_per_parent_one_too() -> None:
    both = {"min_per_parent": 4, "max_per_parent": 2, "null_probability": 0.3}
    assert _refused(_tree(**both))[0] == [OPTION_UNSUPPORTED]
    assert config_api.per_parent_problems(_tree(**both)) == []


def test_at9_each_self_reference_column_is_reported() -> None:
    tables = [
        table(
            "employees",
            10,
            [
                ID,
                ref("manager_id", "employees", distribution="sequential"),
                ref("mentor_id", "employees", root_count=0),
                ref("buddy_id", "employees"),
            ],
        )
    ]
    codes, locs = _refused(tables)
    assert codes == [OPTION_UNSUPPORTED, OPTION_RANGE]
    assert [loc[3] for loc in locs] == [1, 2]


def test_at15_self_reference_problems_are_locations_and_codes_only() -> None:
    marker = "MARKER-SECRET-VALUE"
    tables = _tree(10, root_count=7919, weights=[marker])
    problems = config_api.self_reference_problems(tables)
    assert problems == [(("tables", 0, "generate_columns", 1, "root_count"), OPTION_RANGE)]
    assert marker not in repr(problems) and "79" not in repr(problems)
    with pytest.raises(ValidationError) as exc:
        validate(_tree(10, root_count=7919))
    (error,) = exc.value.errors()
    assert error["msg"] == reasons.get("config", OPTION_RANGE).summary
    assert error["input"] is None and "7919" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None


def test_the_one_self_reference_predicate_reads_the_table_name_and_the_column() -> None:
    column = ref("manager_id", "employees")
    assert config_api.is_self_reference("employees", column)
    assert not config_api.is_self_reference("depts", column)
    assert not config_api.is_self_reference("", {**column, "reference_table": ""})
    assert not config_api.is_self_reference("employees", {**column, "type": "categorical"})
    assert not config_api.is_self_reference("employees", {**column, "reference_table": None})


# ---- AT-11: the parent lookup behind `anchor_via` ----------------------------------------------

LOOKUP_KEY = "generate_parent_lookup_key_not_unique"
LOOKUP_SELF = "generate_parent_lookup_self_reference"
BORN = {"name": "born", "type": "categorical", "categories": ["2001-02-03"]}
VIA_LOC = [("tables", 1, "generate_columns", 1, "anchor_via")]


def _visit(via: Any = "pid", **extra: Any) -> dict[str, Any]:
    return {
        "name": "visit",
        "type": "windowed_date",
        "anchor": "born",
        "anchor_via": via,
        "max_days": 30,
        **extra,
    }


def _anchored(
    key: dict[str, Any] | None = None, fk: dict[str, Any] | None = None, **extra: Any
) -> list[dict[str, Any]]:
    return [
        table("parents", 4, [key or ID, BORN]),
        table("kids", 12, [fk or ref("pid", "parents"), _visit(**extra)]),
    ]


def test_at11_an_anchored_date_over_a_sequence_key_validates() -> None:
    assert validate(_anchored())
    assert validate(_anchored(key={**ID, "null_probability": 0}))
    assert validate(_anchored(fk=ref("pid", "parents", null_probability=0.3)))
    assert validate(_anchored(null_probability=0.2, min_days=-5, distribution="late"))


@pytest.mark.parametrize(
    "key",
    [
        {"name": "id", "type": "categorical", "categories": ["a", "b", "c"]},
        {"name": "id", "type": "sequence", "start": 1, "null_probability": 0.2},
        {"name": "id", "type": "faker", "faker_type": "uuid4"},
        {"name": "id", "type": "faker", "faker_type": "uuid4", "unique": True},
    ],
    ids=["categorical", "nullable-sequence", "faker", "unique-faker"],
)
def test_at11_a_parent_key_that_is_not_a_non_null_sequence_is_refused(key: dict[str, Any]) -> None:
    codes, locs = _refused(_anchored(key=key))
    assert codes == [LOOKUP_KEY]
    assert locs == VIA_LOC


def test_at11_a_lookup_through_a_self_reference_is_refused() -> None:
    tables = [
        table("parents", 4, [ID, BORN]),
        table("kids", 12, [ID, ref("boss", "kids"), {**_visit("boss"), "anchor": "id"}]),
    ]
    codes, locs = _refused(tables)
    assert codes == [LOOKUP_SELF]
    assert locs == [("tables", 1, "generate_columns", 2, "anchor_via")]


def test_at11_a_lookup_through_a_column_that_is_not_a_reference_is_refused() -> None:
    tables = _anchored(fk={"name": "pid", "type": "sequence", "start": 1})
    codes, locs = _refused(tables)
    assert codes == [LOOKUP_KEY]
    assert locs == VIA_LOC


def test_at11_a_missing_or_later_lookup_column_is_left_to_the_plan_check() -> None:
    """Which column exists and in what order is the generate-input check's rule alone."""
    assert validate(_anchored(via="ghost"))
    late = _anchored()
    late[1]["generate_columns"].reverse()
    assert validate(late)


@pytest.mark.parametrize("via", [5, True, ["pid"], None])
def test_at11_anchor_via_must_be_a_string(via: Any) -> None:
    codes, locs = _refused(_anchored(via=via))
    assert codes == [OPTION_TYPE]
    assert locs == VIA_LOC


def test_at11_anchor_via_belongs_to_windowed_date_only() -> None:
    tables = _anchored()
    tables[1]["generate_columns"][1] = {
        "name": "visit",
        "type": "group_key",
        "group_by": "pid",
        "anchor_via": "pid",
    }
    codes, locs = _refused(tables)
    assert codes == [OPTION_UNKNOWN]
    assert locs == VIA_LOC


def test_at11_each_anchored_column_is_reported() -> None:
    tables = _anchored(key={"name": "id", "type": "categorical", "categories": ["a", "b"]})
    tables[1]["generate_columns"].append({**_visit(), "name": "recheck"})
    codes, locs = _refused(tables)
    assert codes == [LOOKUP_KEY, LOOKUP_KEY]
    assert [loc[3] for loc in locs] == [1, 2]


def test_at11_a_date_without_anchor_via_is_not_judged() -> None:
    tables = [
        table("parents", 4, [{"name": "id", "type": "categorical", "categories": ["a", "b"]}]),
        table(
            "kids",
            12,
            [
                ref("pid", "parents"),
                BORN,
                {"name": "visit", "type": "windowed_date", "anchor": "born", "max_days": 30},
            ],
        ),
    ]
    assert validate(tables)
    assert config_api.parent_lookup_problems(tables) == []


def test_at15_parent_lookup_problems_are_locations_and_codes_only() -> None:
    marker = "MARKER-SECRET-VALUE"
    tables = _anchored(key={"name": "id", "type": "categorical", "categories": [marker, "7919"]})
    problems = config_api.parent_lookup_problems(tables)
    assert problems == [(VIA_LOC[0], LOOKUP_KEY)]
    assert marker not in repr(problems) and "79" not in repr(problems)
    with pytest.raises(ValidationError) as exc:
        validate(tables)
    (error,) = exc.value.errors()
    assert error["msg"] == reasons.get("config", LOOKUP_KEY).summary
    assert error["input"] is None and marker not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None


def test_the_one_parent_key_predicate_returns_the_callers_code() -> None:
    """Per-parent aggregates and `inherit` call the same rule with their own reason code."""
    parent = [ID, BORN, {"name": "tag", "type": "sequence", "start": 1, "null_probability": 0.5}]
    assert config_api.parent_key_problem(parent, "id", "callers_code") is None
    assert config_api.parent_key_problem(parent, "born", "callers_code") == "callers_code"
    assert config_api.parent_key_problem(parent, "tag", "another_code") == "another_code"
    assert config_api.parent_key_problem(parent, "ghost", "callers_code") == "callers_code"
    assert config_api.parent_key_problem([], "id", "callers_code") == "callers_code"
    assert config_api.parent_key_problem(parent, None, "callers_code") == "callers_code"


def test_the_self_reference_key_rule_is_the_same_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One rule for "a key that is distinct and never null": the tree's key goes through it too."""
    from decoy_engine.config import _generate_relational as relational

    asked: list[Any] = []
    real = relational.parent_key_problem

    def spy(columns: Any, key: Any, code: str) -> Any:
        asked.append(key)
        return real(columns, key, code)

    monkeypatch.setattr(relational, "parent_key_problem", spy)
    assert validate([table("employees", 10, [ID, ref("manager_id", "employees")])])
    assert validate(_anchored())
    assert asked == ["id", "id"]
