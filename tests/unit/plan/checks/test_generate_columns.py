"""GEN-STRICT E2: generate columns are checked per type at validate and at compile (AT-7, AT-8).

Every refusal is asserted at both sites (`run_config_only_checks`, which `decoy validate` calls,
and `compile_plan`, which `decoy run` calls) with its code and path. Canary cells assert that no
submitted value reaches the error text or its `__context__`.
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine import run_config_only_checks
from decoy_engine.generation import synthesize
from decoy_engine.generators.columns import ColumnGenerator
from decoy_engine.internal.faker_setup import register_faker_provider, unregister_faker_provider
from decoy_engine.plan import PlanCompileError, compile_plan
from tests.unit._dps_helpers import empty_profile

CANARY = "CANARY-7731"


def cfg(cols: list[dict[str, Any]], n: int = 5) -> dict[str, Any]:
    return {
        "global_settings": {"seed": 1},
        "tables": [{"name": "t", "row_count": n, "generate_columns": cols}],
    }


def _compile(config: dict[str, Any]) -> Any:
    return compile_plan(config, empty_profile(), decoy_engine_version="test")


def _sites() -> list[Any]:
    return [run_config_only_checks, _compile]


def refusal(config: dict[str, Any], site: Any) -> PlanCompileError:
    with pytest.raises(PlanCompileError) as info:
        site(config)
    return info.value


def assert_refused(config: dict[str, Any], code: str, path: str) -> None:
    """Both sites refuse with the same code and path, and the text holds no canary."""
    for site in _sites():
        exc = refusal(config, site)
        assert (exc.code, exc.path) == (code, path), site.__name__
        assert CANARY not in str(exc) and "7731" not in str(exc)
        assert exc.__context__ is None


def assert_accepted(config: dict[str, Any]) -> None:
    run_config_only_checks(config)
    _compile(config)


def p(column: str, key: str | None = None) -> str:
    base = f"tables.t.generate_columns.{column}"
    return base if key is None else f"{base}.{key}"


def seq(name: str = "a", **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": "sequence", "start": 1, **extra}


def formula(name: str, expr: str, references: list[str] | None = None) -> dict[str, Any]:
    col: dict[str, Any] = {"name": name, "type": "formula", "formula": expr}
    if references is not None:
        col["references"] = references
    return col


# ---- faker -----------------------------------------------------------------------------------


def faker(name: str = "f", **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": "faker", "faker_type": "first_name", **extra}


def test_unknown_faker_type_refused() -> None:
    assert_refused(
        cfg([faker(faker_type="not_a_provider")]),
        "generate_faker_type_unknown",
        p("f", "faker_type"),
    )


def test_custom_provider_accepted_live_and_from_snapshot() -> None:
    def custom(fake: Any) -> str:
        return "x"

    register_faker_provider("gen_strict_probe", custom)
    try:
        assert_accepted(cfg([faker(faker_type="gen_strict_probe")]))
    finally:
        unregister_faker_provider("gen_strict_probe")
    from decoy_engine.plan.checks import check_generate_columns

    check_generate_columns(
        cfg([faker(faker_type="gen_strict_probe")]), provider_snapshot={"gen_strict_probe": custom}
    )
    with pytest.raises(PlanCompileError):
        check_generate_columns(cfg([faker(faker_type="gen_strict_probe")]), provider_snapshot={})


@pytest.mark.parametrize("locale", ["xx_YY", ["en_US", "xx_YY"]])
def test_unknown_locale_refused_alone_and_in_a_list(locale: Any) -> None:
    assert_refused(cfg([faker(locale=locale)]), "generate_locale_unknown", p("f", "locale"))


@pytest.mark.parametrize("locale", ["en_US", "en-US", ["en_US", "fr_FR"]])
def test_known_locale_accepted(locale: Any) -> None:
    assert_accepted(cfg([faker(locale=locale)]))


def test_locale_is_checked_before_the_provider_list() -> None:
    col = faker(locale="xx_YY", faker_type="not_a_provider")
    assert_refused(cfg([col]), "generate_locale_unknown", p("f", "locale"))


def test_faker_kwargs_unknown_key_refused_and_real_one_accepted() -> None:
    assert_refused(
        cfg([faker(faker_kwargs={"nope": 1})]),
        "generate_faker_kwargs_unknown",
        p("f", "faker_kwargs"),
    )
    ok = {"name": "r", "type": "faker", "faker_type": "random_int", "faker_kwargs": {"min": 0}}
    assert_accepted(cfg([ok]))


# ---- categorical ------------------------------------------------------------------------------


def cat(categories: list[Any], name: str = "c") -> dict[str, Any]:
    return {"name": name, "type": "categorical", "categories": categories}


def test_mixed_categories_refused() -> None:
    assert_refused(cfg([cat(["a", 1])]), "generate_categories_mixed", p("c", "categories"))


@pytest.mark.parametrize("categories", [[1, 2.5], ["a", None], [True, False], ["a", "b"]])
def test_categories_the_run_can_type_accepted(categories: list[Any]) -> None:
    assert_accepted(cfg([cat(categories)]))


def test_every_list_admission_accepts_is_accepted() -> None:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    from decoy_engine.generation._admission import _categorical_type

    atoms = st.one_of(st.text(max_size=3), st.integers(-5, 5), st.floats(-5, 5), st.booleans())

    @settings(max_examples=60, deadline=None)
    @given(st.lists(atoms, min_size=1, max_size=5))
    def prop(categories: list[Any]) -> None:
        if _categorical_type(cat(categories))[0] is not None:
            run_config_only_checks(cfg([cat(categories)]))

    prop()


# ---- forward references and transform parse rules ----------------------------------------------


def derived(expr: str, name: str = "x", **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": "derived", "expression": expr, **extra}


def test_derived_reading_a_later_column_refused() -> None:
    assert_refused(
        cfg([derived("a + 1"), seq()]), "generate_column_forward_reference", p("x", "expression")
    )


def test_derived_with_one_later_ref_among_several_refused() -> None:
    cols = [seq("a"), derived("a + b"), seq("b")]
    assert_refused(cfg(cols), "generate_column_forward_reference", p("x", "expression"))


@pytest.mark.parametrize("expr", ["a + b", "1 + 2"])
def test_derived_with_earlier_or_no_refs_accepted(expr: str) -> None:
    assert_accepted(cfg([seq("a"), seq("b"), derived(expr)]))


def test_group_by_equal_to_order_by_refused() -> None:
    cols = [
        cat(["p", "q"], "g"),
        {"name": "x", "type": "grouped_series", "group_by": "g", "order_by": "g"},
    ]
    assert_refused(cfg(cols), "grouped_series_group_by_equals_order_by", p("x", "order_by"))


@pytest.mark.parametrize(
    ("col", "code", "key"),
    [
        (
            {"name": "x", "type": "derived_aggregate", "column": "a"},
            "derived_aggregate_op_missing",
            "op",
        ),
        (
            {"name": "x", "type": "windowed_date", "anchor": "d", "max_days": 3, "min_days": 9},
            "windowed_date_min_exceeds_max",
            "min_days",
        ),
        (
            {"name": "x", "type": "group_key", "group_by": "a", "length": 7},
            "group_key_length_odd",
            "length",
        ),
        (
            {"name": "x", "type": "grouped_series", "group_by": "a", "order_by": "b", "step": 99},
            "grouped_series_step_out_of_range",
            "step",
        ),
        (derived("a +"), "generate_option_invalid", "expression"),
    ],
)
def test_transform_parse_rules_are_translated_value_free(
    col: dict[str, Any], code: str, key: str
) -> None:
    anchor = {"name": "d", "type": "faker", "faker_type": "date_this_decade"}
    assert_refused(cfg([seq("a"), seq("b"), anchor, col]), code, p("x", key))


def test_translated_error_never_carries_the_bound_values() -> None:
    col = derived("a + 1", bounds={"min": 7731.5, "max": 1})
    assert_refused(cfg([seq("a"), col]), "derived_bounds_inverted", p("x", "bounds"))


@pytest.mark.parametrize(
    ("col", "code", "key"),
    [
        (
            {"name": "x", "type": "derived_aggregate", "op": CANARY, "column": "a"},
            "derived_aggregate_op_invalid",
            "op",
        ),
        (
            {
                "name": "x",
                "type": "windowed_date",
                "anchor": "d",
                "max_days": 3,
                "distribution": CANARY,
            },
            "windowed_date_distribution_invalid",
            "distribution",
        ),
        (
            {
                "name": "x",
                "type": "grouped_series",
                "group_by": "a",
                "order_by": "b",
                "generator": CANARY,
            },
            "grouped_series_generator_invalid",
            "generator",
        ),
    ],
)
def test_owned_rules_keep_owner_code_and_path_without_the_value(
    col: dict[str, Any], code: str, key: str
) -> None:
    anchor = {"name": "d", "type": "faker", "faker_type": "date_this_decade"}
    assert_refused(cfg([seq("a"), seq("b"), anchor, col]), code, p("x", key))


def test_mask_only_config_keeps_its_ref_refusal_value_free() -> None:
    config = {
        "global_settings": {"seed": 1},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "a",
                        "strategy": "derived",
                        "provider_config": {"expression": "canary_7731_missing + 1"},
                    }
                ],
            }
        ],
    }
    exc = refusal(config, run_config_only_checks)
    assert exc.code == "derived_missing_column_ref"
    assert "canary_7731_missing" not in str(exc) and exc.__context__ is None


# ---- every key that names a sibling column ---------------------------------------------------

REF_KEYS = {
    "derived_aggregate.column": (
        lambda r: {"name": "x", "type": "derived_aggregate", "op": "sum", "column": r},
        "derived_aggregate_missing_column_ref",
        "column",
    ),
    "grouped_series.group_by": (
        lambda r: {"name": "x", "type": "grouped_series", "group_by": r, "order_by": "o"},
        "grouped_series_missing_group_by_ref",
        "group_by",
    ),
    "grouped_series.order_by": (
        lambda r: {"name": "x", "type": "grouped_series", "group_by": "o", "order_by": r},
        "grouped_series_missing_order_by_ref",
        "order_by",
    ),
    "windowed_date.anchor": (
        lambda r: {"name": "x", "type": "windowed_date", "anchor": r, "max_days": 3},
        "windowed_date_missing_anchor_ref",
        "anchor",
    ),
    "group_key.group_by": (
        lambda r: {"name": "x", "type": "group_key", "group_by": r},
        "group_key_missing_group_by_ref",
        "group_by",
    ),
    "derived.expression": (
        lambda r: derived(f"{r} + 1"),
        "derived_missing_column_ref",
        "expression",
    ),
}


@pytest.mark.parametrize("name", sorted(REF_KEYS))
def test_reference_key_earlier_accepted_later_and_missing_refused(name: str) -> None:
    build, missing_code, key = REF_KEYS[name]
    assert_accepted(cfg([seq("o"), seq("p"), build("p")]))
    assert_refused(
        cfg([seq("o"), build("p"), seq("p")]), "generate_column_forward_reference", p("x", key)
    )
    assert_refused(cfg([seq("o"), build("ghost")]), missing_code, p("x", key))


def _conditioned_snapshot(tmp_path: Any) -> str:
    import json

    import pandas as pd

    from decoy_engine.quality.snapshot import compute_distribution_snapshot

    df = pd.DataFrame({"state": ["CA", "NY"] * 60, "tier": ["gold", "silver"] * 60})
    path = tmp_path / "snap.json"
    snap = compute_distribution_snapshot(df, joint_columns=[("state", "tier")])
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


def test_statistical_condition_on_must_name_an_earlier_column(tmp_path: Any) -> None:
    snap = _conditioned_snapshot(tmp_path)

    def stat(name: str, **extra: Any) -> dict[str, Any]:
        return {
            "name": name,
            "type": "statistical",
            "snapshot_file": snap,
            "allow_real_categories": True,
            **extra,
        }

    assert_accepted(cfg([stat("state"), stat("tier", condition_on="state")]))
    code = "generate_column_forward_reference"
    assert_refused(
        cfg([stat("tier", condition_on="state"), stat("state")]), code, p("tier", "condition_on")
    )
    # A name the snapshot has no joint for is refused first by the snapshot check, at the column.
    exc = refusal(cfg([stat("tier", condition_on="ghost")]), run_config_only_checks)
    assert (exc.code, exc.path) == ("statistical_joint_missing", p("tier"))


# ---- formulas ---------------------------------------------------------------------------------


def test_formula_unknown_name_refused() -> None:
    assert_refused(
        cfg([formula("x", "zzz + 1")]), "generate_formula_name_unknown", p("x", "formula")
    )


def test_formula_sibling_not_in_references_refused() -> None:
    cols = [seq("a"), formula("x", "a + 1")]
    assert_refused(cfg(cols), "generate_formula_reference_missing", p("x", "formula"))


def test_formula_reference_to_no_sibling_refused() -> None:
    cols = [formula("x", "ghost + 1", ["ghost"])]
    assert_refused(cfg(cols), "generate_formula_reference_unknown", p("x", "references.0"))


def test_formula_reading_itself_or_a_later_reference_bearing_formula_refused() -> None:
    own = [seq("a"), formula("x", "x + 1", ["x"])]
    assert_refused(cfg(own), "generate_column_forward_reference", p("x", "references.0"))
    later = [seq("a"), formula("x", "y + 1", ["y"]), formula("y", "a + 1", ["a"])]
    assert_refused(cfg(later), "generate_column_forward_reference", p("x", "references.0"))


def generated(config: dict[str, Any], column: str) -> list[Any]:
    return synthesize.generate_tables(_compile(config))["t"].column(column).to_pylist()


def test_unused_self_and_later_declarations_run_unchanged() -> None:
    own = cfg([formula("x", "1", ["x"])])
    assert_accepted(own)
    assert generated(own, "x") == [1] * 5
    later = cfg([formula("x", "1", ["y"]), formula("y", "2", ["a"]), seq("a")])
    assert_accepted(later)
    assert generated(later, "x") == [1] * 5


def test_earlier_reference_bearing_and_later_plain_dependencies_accepted() -> None:
    earlier = cfg([seq("a"), formula("x", "int(a) * 2", ["a"]), formula("y", "x + 1", ["x"])])
    assert_accepted(earlier)
    assert generated(earlier, "y") == [3, 5, 7, 9, 11]
    plain_later = cfg([formula("x", "int(a) * 2", ["a"]), seq("a")])
    assert_accepted(plain_later)
    assert generated(plain_later, "x") == [2, 4, 6, 8, 10]


def test_braced_template_form_refused() -> None:
    cols = [seq("a"), formula("x", "{a} * 2", ["a"])]
    assert_refused(cfg(cols), "generate_formula_template_syntax", p("x", "formula"))
    whole = [seq("a"), formula("x", "{a}", ["a"])]
    assert_refused(cfg(whole), "generate_formula_template_syntax", p("x", "formula"))


@pytest.mark.parametrize(
    "expr", ["len({a, b})", "a in {1, 2}", "[v for v in [1, 2, 3]]", "i + index", "round(a / 3)"]
)
def test_valid_formula_shapes_accepted(expr: str) -> None:
    assert_accepted(cfg([seq("a"), seq("b"), formula("x", expr, ["a", "b"])]))


def test_scope_name_constant_matches_the_formula_scope() -> None:
    from decoy_engine.generators._formula import FORMULA_SCOPE_NAMES

    assert frozenset(ColumnGenerator(seed=1)._formula_scope(0)) == FORMULA_SCOPE_NAMES


# ---- reference weights ------------------------------------------------------------------------


def two_tables(parent_col: dict[str, Any], weights: list[int]) -> dict[str, Any]:
    child = {
        "name": "k",
        "type": "reference",
        "reference_table": "p",
        "reference_column": "id",
        "distribution": "weighted",
        "weights": weights,
    }
    return {
        "global_settings": {"seed": 1},
        "tables": [
            {"name": "p", "row_count": 3, "generate_columns": [parent_col]},
            {"name": "t", "row_count": 4, "generate_columns": [child]},
        ],
    }


def test_weights_length_against_a_sequence_parent() -> None:
    parent = {"name": "id", "type": "sequence", "start": 1}
    for site in _sites():
        exc = refusal(two_tables(parent, [1, 1]), site)
        assert (exc.code, exc.path) == (
            "reference_weights_length_mismatch",
            "tables.t.generate_columns.k.weights",
        )
    assert_accepted(two_tables(parent, [1, 1, 1]))


def test_weights_length_not_checked_when_the_parent_can_drop_keys_or_is_not_a_sequence() -> None:
    nulls = {"name": "id", "type": "sequence", "start": 1, "null_probability": 0.5}
    assert_accepted(two_tables(nulls, [1, 1]))
    other = {"name": "id", "type": "faker", "faker_type": "random_int"}
    assert_accepted(two_tables(other, [1, 1]))


# ---- AT-8: validate refuses what run refuses ---------------------------------------------------

H4_REPROS = {
    "mixed_categories": ([cat(["a", 1])], "generate_categories_mixed"),
    "group_by_equals_order_by": (
        [
            cat(["p", "q"], "g"),
            {"name": "x", "type": "grouped_series", "group_by": "g", "order_by": "g"},
        ],
        "grouped_series_group_by_equals_order_by",
    ),
    "derived_forward_reference": ([derived("a + 1"), seq()], "generate_column_forward_reference"),
}


@pytest.mark.parametrize("name", sorted(H4_REPROS))
def test_h4_repro_is_refused_before_generation_starts(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cols, code = H4_REPROS[name]
    started: list[str] = []
    monkeypatch.setattr(
        "decoy_engine.generation._plan_entry.run_generation",
        lambda *a, **k: started.append("run") or pytest.fail("generation started"),
    )
    for site in _sites():
        assert refusal(cfg(cols), site).code == code
    assert started == []


# What the run raises on its own for each repro when the plan check is bypassed.
RUN_FAILURE = {
    "mixed_categories": (("ArrowInvalid", "ArrowTypeError"), None),
    "group_by_equals_order_by": (("PlanCompileError",), "grouped_series_group_by_equals_order_by"),
    "derived_forward_reference": (("ValueError",), None),
}


@pytest.mark.parametrize("name", sorted(H4_REPROS))
def test_h4_repro_is_real_when_the_check_is_bypassed(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cols, _ = H4_REPROS[name]
    monkeypatch.setattr("decoy_engine.plan._compile.check_generate_columns", lambda *a, **k: None)
    names, code = RUN_FAILURE[name]
    with pytest.raises(Exception) as info:
        synthesize.generate_tables(_compile(cfg(cols)))
    assert type(info.value).__name__ in names
    assert getattr(info.value, "code", None) == code
