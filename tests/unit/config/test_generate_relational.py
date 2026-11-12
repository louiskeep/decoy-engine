"""GEN-REL config-time checks for per-parent bounds (AT-2, AT-2b, AT-3, AT-4, AT-6f).

Every refusal is a registered `config` code raised through `PipelineConfig`, in a fixed order:
`min > max`, then fitted with a bound, then nulls with a bound, then the row-count ceiling.
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
