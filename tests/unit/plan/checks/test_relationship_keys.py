"""A generated relationship parent needs a key the plan can show is unique (AT-23).

Both sites that run the generate-column checks (`run_config_only_checks` for `decoy validate`,
`compile_plan` for `decoy run`) refuse a non-unique key before any read, with the same code and
path, and the text carries no submitted value.
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine import run_config_only_checks
from decoy_engine.plan import PlanCompileError, compile_plan
from tests.unit._dps_helpers import empty_profile

CODE = "relationship_parent_not_unique"
CANARY = "CANARY-5519"


def _compile(config: dict[str, Any]) -> Any:
    return compile_plan(
        config, empty_profile(), decoy_engine_version="test", instance_default_locale=None
    )


SITES = [run_config_only_checks, _compile]


def key_col(kind: str = "faker", **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"name": "id", "type": kind}
    if kind == "faker":
        base["faker_type"] = "first_name"
    elif kind == "categorical":
        base["categories"] = ["a", "b", "c", CANARY]
    elif kind == "sequence":
        base["start"] = 1
    return {**base, **extra}


def other(name: str = "code") -> dict[str, Any]:
    return {"name": name, "type": "sequence", "start": 1}


def config(
    columns: list[dict[str, Any]],
    *,
    rows: int = 5,
    key: tuple[str, ...] = ("id",),
    together: list[list[str]] | None = None,
) -> dict[str, Any]:
    parent: dict[str, Any] = {"name": "p", "row_count": rows, "generate_columns": columns}
    if together is not None:
        parent["unique_together"] = together
    return {
        "global_settings": {"seed": 1},
        "tables": [
            parent,
            {"name": "c", "columns": [{"name": k, "strategy": "passthrough"} for k in key]},
        ],
        "relationships": [
            {
                "parent": {"table": "p", "columns": list(key)},
                "children": [{"table": "c", "columns": list(key)}],
                "orphan_policy": "preserve",
                "namespace": "ns_p",
            }
        ],
    }


def assert_refused(cfg: dict[str, Any]) -> None:
    for site in SITES:
        with pytest.raises(PlanCompileError) as info:
            site(cfg)
        exc = info.value
        assert exc.code == CODE, site.__name__
        assert exc.path == "tables.p.generate_columns.id", site.__name__
        assert "'p'" in str(exc) and "'id'" in str(exc) and "unique" in str(exc)
        assert CANARY not in str(exc) and exc.__context__ is None


def assert_accepted(cfg: dict[str, Any]) -> None:
    for site in SITES:
        site(cfg)


def test_non_unique_faker_key_is_refused_at_both_sites() -> None:
    assert_refused(config([key_col("faker")]))


def test_non_unique_categorical_key_is_refused_without_the_category_text() -> None:
    assert_refused(config([key_col("categorical")]))


@pytest.mark.parametrize("rows", [0, 1])
def test_row_count_of_at_most_one_is_accepted(rows: int) -> None:
    assert_accepted(config([key_col("faker")], rows=rows))


def test_unique_true_is_accepted() -> None:
    assert_accepted(config([key_col("faker", unique=True)]))


def test_unique_false_is_refused() -> None:
    assert_refused(config([key_col("faker", unique=False)]))


def test_sequence_is_accepted_with_default_and_negative_step() -> None:
    assert_accepted(config([key_col("sequence")]))
    assert_accepted(config([key_col("sequence", step=-3)]))


def test_composite_with_one_unique_member_is_accepted() -> None:
    cols = [key_col("faker"), other("code")]
    assert_accepted(config(cols, key=("id", "code")))


def test_composite_with_no_unique_member_is_refused() -> None:
    cols = [key_col("faker"), {"name": "code", "type": "faker", "faker_type": "word"}]
    for site in SITES:
        with pytest.raises(PlanCompileError) as info:
            site(config(cols, key=("id", "code")))
        assert info.value.code == CODE


def test_unique_together_equal_to_the_composite_key_is_accepted() -> None:
    cols = [
        {"name": "id", "type": "reference", "reference_table": "q", "reference_column": "k"},
        {"name": "code", "type": "reference", "reference_table": "q", "reference_column": "k"},
    ]
    for pair in (["id", "code"], ["code", "id"]):
        cfg = config(cols, key=("id", "code"), together=[pair])
        cfg["tables"].insert(0, {"name": "q", "row_count": 4, "generate_columns": [other("k")]})
        assert_accepted(cfg)


def _pair_config(together: list[list[str]], key: tuple[str, ...]) -> dict[str, Any]:
    cols = [
        {"name": n, "type": "reference", "reference_table": "q", "reference_column": "k"}
        for n in ("id", "code", "extra")
    ]
    cfg = config(cols, key=key, together=together)
    cfg["tables"].insert(0, {"name": "q", "row_count": 4, "generate_columns": [other("k")]})
    return cfg


def test_pair_superset_and_subset_of_the_key_are_refused() -> None:
    for site in SITES:
        with pytest.raises(PlanCompileError) as info:
            site(_pair_config([["id", "code"]], ("id", "code", "extra")))
        assert info.value.code == CODE
        with pytest.raises(PlanCompileError) as info:
            site(_pair_config([["id", "code", "extra"]], ("id", "code")))
        assert info.value.code == CODE


def test_masked_parent_and_missing_parent_are_not_this_rules_business() -> None:
    cfg = config([key_col("faker")])
    cfg["tables"][0] = {"name": "p", "columns": [{"name": "id", "strategy": "passthrough"}]}
    assert_accepted(cfg)


def test_no_relationships_means_no_rule() -> None:
    cfg = config([key_col("faker")])
    cfg["relationships"] = []
    assert_accepted(cfg)
