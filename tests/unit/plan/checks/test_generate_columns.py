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


def _compile(config: dict[str, Any], **kwargs: Any) -> Any:
    """Compile as `decoy run` does: the platform default locale is known (None is en_US)."""
    kwargs.setdefault("instance_default_locale", None)
    return compile_plan(config, empty_profile(), decoy_engine_version="test", **kwargs)


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
        cfg([faker(faker_type="gen_strict_probe")]),
        provider_snapshot={"gen_strict_probe": custom},
        default_locale=None,
    )
    with pytest.raises(PlanCompileError):
        check_generate_columns(
            cfg([faker(faker_type="gen_strict_probe")]), provider_snapshot={}, default_locale=None
        )


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


# ---- the platform default locale ---------------------------------------------------------------


def dept(**extra: Any) -> dict[str, Any]:
    """`department` exists in fr_FR only, so it needs a locale from the column or the platform."""
    return {"name": "d", "type": "faker", "faker_type": "department", **extra}


def _generate(config: dict[str, Any], **kwargs: Any) -> list[Any]:
    plan = compile_plan(config, empty_profile(), decoy_engine_version="test")  # default deferred
    return synthesize.generate_tables(plan, **kwargs)["t"].column("d").to_pylist()


DEPT_REFUSAL = ("generate_faker_type_unknown", p("d", "faker_type"))


def test_provider_of_the_platform_default_locale_passes_plan_and_generates() -> None:
    config = cfg([dept()])
    run_config_only_checks(config, instance_default_locale="fr_FR")
    _compile(config, instance_default_locale="fr_FR")
    _compile(config, instance_default_locale=_deferred())  # a plan generated later
    values = _generate(config, instance_default_locale="fr_FR")
    assert len(values) == 5 and all(isinstance(v, str) and v for v in values)


def _deferred() -> Any:
    from decoy_engine.plan.checks import DEFERRED

    return DEFERRED


@pytest.mark.parametrize("default", [None, "en_US", "de_DE"])
def test_provider_missing_from_the_effective_locale_is_refused_even_if_another_has_it(
    default: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = cfg([dept()])
    # Validate, and a compile that knows the default, refuse at plan time.
    with pytest.raises(PlanCompileError) as info:
        run_config_only_checks(config, instance_default_locale=default)
    assert (info.value.code, info.value.path) == DEPT_REFUSAL
    with pytest.raises(PlanCompileError) as info:
        _compile(config, instance_default_locale=default)
    assert (info.value.code, info.value.path) == DEPT_REFUSAL
    # A plan compiled without it is refused when generation starts, before any provider runs.
    monkeypatch.setattr(
        "decoy_engine.generation._streamed.GenerationBudgetLedger",
        lambda *a, **k: pytest.fail("generation started"),
    )
    with pytest.raises(PlanCompileError) as info:
        _generate(config, instance_default_locale=default)
    assert (info.value.code, info.value.path) == DEPT_REFUSAL
    assert info.value.__context__ is None


def test_column_locale_overrides_the_platform_default() -> None:
    assert _generate(cfg([dept(locale="fr_FR")]), instance_default_locale="en_US")
    assert_refused(cfg([dept(locale="en_US")]), *DEPT_REFUSAL)
    assert_refused(cfg([dept(locale="de_DE")]), *DEPT_REFUSAL)


def test_name_no_locale_has_is_still_refused_at_plan_time() -> None:
    assert_refused(
        cfg([faker(faker_type="not_a_provider")]),
        "generate_faker_type_unknown",
        p("f", "faker_type"),
    )


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

    from decoy_engine.config import categorical_arrow_type

    atoms = st.one_of(st.text(max_size=3), st.integers(-5, 5), st.floats(-5, 5), st.booleans())

    @settings(max_examples=60, deadline=None)
    @given(st.lists(atoms, min_size=1, max_size=5))
    def prop(categories: list[Any]) -> None:
        if categorical_arrow_type(cat(categories))[0] is not None:
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
    # Without `from_table` the input of a generate aggregate is a same-table column, checked by
    # the generate-input check alone, so a missing one and a later one are the same refusal.
    "derived_aggregate.column": (
        lambda r: {"name": "x", "type": "derived_aggregate", "op": "sum", "column": r},
        "generate_column_forward_reference",
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
    # The anchor of a generate column is resolved and checked by the generate-input check alone,
    # so a missing one and a later one are the same refusal.
    "windowed_date.anchor": (
        lambda r: {"name": "x", "type": "windowed_date", "anchor": r, "max_days": 3},
        "generate_column_forward_reference",
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


# ---- inputs that live in another table ---------------------------------------------------------

FORWARD = "generate_column_forward_reference"


def _family(kids: list[dict[str, Any]], parents: list[dict[str, Any]] | None = None) -> dict:
    """Table `t` (the child under test) and its parent `p`."""
    config = cfg(kids)
    config["tables"].insert(
        0,
        {
            "name": "p",
            "row_count": 3,
            "generate_columns": parents
            or [seq("id"), {"name": "born", "type": "categorical", "categories": ["2001-02-03"]}],
        },
    )
    return config


def _fk(name: str = "pid") -> dict[str, Any]:
    return {"name": name, "type": "reference", "reference_table": "p", "reference_column": "id"}


def _anchored(anchor: str = "born", via: str = "pid", **extra: Any) -> dict[str, Any]:
    return {
        "name": "x",
        "type": "windowed_date",
        "anchor": anchor,
        "anchor_via": via,
        "max_days": 3,
        **extra,
    }


def test_cross_table_anchor_is_accepted_with_no_same_named_child_column() -> None:
    """`anchor` names a column of the table `anchor_via` points at, not a sibling."""
    assert_accepted(_family([_fk(), _anchored()]))


def test_cross_table_anchor_is_accepted_whatever_a_same_named_child_column_does() -> None:
    later = {"name": "born", "type": "sequence", "start": 1}
    assert_accepted(_family([_fk(), _anchored(), later]))
    assert_accepted(_family([later, _fk(), _anchored()]))


def test_cross_table_anchor_missing_from_the_parent_is_refused() -> None:
    assert_refused(_family([_fk(), _anchored("ghost")]), FORWARD, p("x", "anchor"))


def test_cross_table_anchor_is_not_satisfied_by_a_same_named_child_column() -> None:
    """The parent has no `nick`; an earlier child column of that name must not stand in."""
    nick = {"name": "nick", "type": "categorical", "categories": ["2001-02-03"]}
    assert_refused(_family([nick, _fk(), _anchored("nick")]), FORWARD, p("x", "anchor"))


def test_cross_table_anchor_may_be_declared_anywhere_in_the_parent() -> None:
    """Order inside the parent does not matter: the whole parent table is finished first."""
    parents = [{"name": "born", "type": "categorical", "categories": ["2001-02-03"]}, seq("id")]
    assert_accepted(_family([_fk(), _anchored()], parents))


def test_anchor_via_must_name_an_earlier_column_of_the_same_table() -> None:
    assert_refused(_family([_anchored(), _fk()]), FORWARD, p("x", "anchor_via"))
    assert_refused(_family([_fk(), _anchored(via="ghost")]), FORWARD, p("x", "anchor_via"))
    # `id` is the parent's key, not a column of this table.
    assert_refused(_family([_fk(), _anchored(via="id")]), FORWARD, p("x", "anchor_via"))


def test_anchor_via_that_is_not_a_reference_leads_to_no_table() -> None:
    """An earlier column that references nothing cannot carry the anchor to a parent."""
    plain = {"name": "pid", "type": "sequence", "start": 1}
    born = {"name": "born", "type": "categorical", "categories": ["2001-02-03"]}
    assert_refused(_family([plain, _anchored()]), FORWARD, p("x", "anchor"))
    assert_refused(_family([plain, born, _anchored()]), FORWARD, p("x", "anchor"))


def test_anchor_without_anchor_via_keeps_the_same_table_rule() -> None:
    own = {"name": "born", "type": "categorical", "categories": ["2001-02-03"]}
    same_table = {"name": "x", "type": "windowed_date", "anchor": "born", "max_days": 3}
    assert_accepted(_family([_fk(), own, same_table]))
    # The parent's `born` is not a sibling: without `anchor_via` nothing reaches it.
    assert_refused(_family([_fk(), same_table]), FORWARD, p("x", "anchor"))
    assert_refused(_family([_fk(), same_table, own]), FORWARD, p("x", "anchor"))


def test_cross_table_refusals_quote_no_name_from_the_config() -> None:
    config = _family([_fk(), _anchored("canary_7731_col")])
    for site in _sites():
        exc = refusal(config, site)
        assert (exc.code, exc.path) == (FORWARD, p("x", "anchor"))
        assert "canary_7731_col" not in str(exc) and exc.__context__ is None


def test_the_generate_input_check_walks_the_one_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every edge the check judges comes from `config.iter_generate_input_refs`."""
    from decoy_engine import config as config_api
    from decoy_engine.plan.checks import _generate_columns as checks

    seen: list[tuple[str, str]] = []
    real = config_api.iter_generate_input_refs

    def spy(table: Any, column: Any) -> Any:
        seen.append((table["name"], column["name"]))
        return real(table, column)

    monkeypatch.setattr(config_api, "iter_generate_input_refs", spy)
    checks.check_generate_columns(_family([_fk(), _anchored()]))
    assert seen == [("p", "id"), ("p", "born"), ("t", "pid"), ("t", "x")]
    assert not hasattr(checks, "_referenced")
    assert not hasattr(checks, "_check_forward_references")


# ---- per-parent aggregates: `column` and `via` are columns of the `from_table` child -----------


def _orders(
    total: dict[str, Any],
    parents: list[dict[str, Any]] | None = None,
    kids: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Table `t` (the parent under test, holding the aggregate) and its child `lines`."""
    fk = {"name": "oid", "type": "reference", "reference_table": "t", "reference_column": "id"}
    config = cfg(parents or [seq("id"), total])
    config["tables"].append(
        {
            "name": "lines",
            "row_count": 6,
            "generate_columns": kids or [seq("lid"), fk, seq("price", start=5)],
        }
    )
    return config


def _total(column: str = "price", via: str = "oid", **extra: Any) -> dict[str, Any]:
    return {
        "name": "x",
        "type": "derived_aggregate",
        "op": "sum",
        "column": column,
        "from_table": "lines",
        "via": via,
        **extra,
    }


def test_per_parent_column_is_accepted_with_no_same_named_parent_column() -> None:
    assert_accepted(_orders(_total()))
    # The aggregate check keeps its own op rule for this form.
    bad_op = _orders(_total(op="product"))
    assert_refused(bad_op, "derived_aggregate_op_invalid", p("x", "op"))


def test_per_parent_column_missing_from_the_child_is_refused() -> None:
    """The parent has a `price`; it must not stand in for the child's."""
    fk = {"name": "oid", "type": "reference", "reference_table": "t", "reference_column": "id"}
    config = _orders(_total(), [seq("id"), seq("price"), _total()], [seq("lid"), fk])
    assert_refused(config, FORWARD, p("x", "column"))


def test_per_parent_column_may_be_declared_anywhere_in_the_child() -> None:
    """The child table is finished before the post-pass reads it."""
    fk = {"name": "oid", "type": "reference", "reference_table": "t", "reference_column": "id"}
    assert_accepted(_orders(_total(), kids=[seq("price"), seq("lid"), fk]))
    assert_accepted(_orders(_total(), kids=[seq("lid"), seq("price"), fk]))


def test_per_parent_via_must_be_a_column_of_the_child() -> None:
    assert_refused(_orders(_total(via="ghost")), FORWARD, p("x", "via"))
    # `id` is the parent's column, not one of the child's.
    assert_refused(_orders(_total(via="id")), FORWARD, p("x", "via"))


def test_per_parent_from_table_that_names_no_table_is_refused() -> None:
    config = _orders(_total(), [seq("id"), seq("price"), _total(from_table="ghost")])
    assert_refused(config, FORWARD, p("x", "column"))


def test_per_parent_refusals_quote_no_name_from_the_config() -> None:
    config = _orders(_total("canary_7731_col"))
    for site in _sites():
        exc = refusal(config, site)
        assert (exc.code, exc.path) == (FORWARD, p("x", "column"))
        assert "canary_7731_col" not in str(exc) and exc.__context__ is None


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


# ---- a formula filled after the column loop ----------------------------------------------------
# A formula with `references` holds None until the post-pass after every other column is drawn,
# so a column of another type that reads it while it is drawn is refused like a forward read.


def _filled_late(name: str = "f") -> dict[str, Any]:
    return formula(name, "'north' if int(a) % 2 else 'south'", ["a"])


def _drawn_now(name: str = "f") -> dict[str, Any]:
    return formula(name, "'north'")


def _conditional(on: str = "f") -> dict[str, Any]:
    return {
        "name": "tier",
        "type": "categorical",
        "categories": ["basic", "plus"],
        "condition_on": on,
        "by": {"north": {"categories": ["gold"]}},
    }


def _joint_snapshot(tmp_path: Any) -> str:
    import json

    import pandas as pd

    from decoy_engine.quality.snapshot import compute_distribution_snapshot

    df = pd.DataFrame({"f": ["north", "south"] * 60, "tier": ["gold", "silver"] * 60})
    path = tmp_path / "joint.json"
    snap = compute_distribution_snapshot(df, joint_columns=[("f", "tier")])
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


def _fitted(snapshot: str) -> dict[str, Any]:
    return {
        "name": "tier",
        "type": "statistical",
        "snapshot_file": snapshot,
        "allow_real_categories": True,
        "condition_on": "f",
    }


def test_a_conditional_categorical_reading_a_formula_filled_late_is_refused() -> None:
    config = cfg([seq("a"), _filled_late(), _conditional()])
    assert_refused(config, FORWARD, p("tier", "condition_on"))


def test_a_fitted_condition_on_reading_a_formula_filled_late_is_refused(tmp_path: Any) -> None:
    config = cfg([seq("a"), _filled_late(), _fitted(_joint_snapshot(tmp_path))])
    assert_refused(config, FORWARD, p("tier", "condition_on"))


def test_a_derived_column_reading_a_formula_filled_late_is_refused() -> None:
    config = cfg([seq("a"), formula("f", "int(a) * 2", ["a"]), derived("f + 1")])
    assert_refused(config, FORWARD, p("x", "expression"))


def test_a_formula_drawn_in_the_loop_may_be_read_by_any_type(tmp_path: Any) -> None:
    conditional = cfg([seq("a"), _drawn_now(), _conditional()])
    assert_accepted(conditional)
    assert generated(conditional, "tier") == ["gold"] * 5
    assert_accepted(cfg([seq("a"), _drawn_now(), _fitted(_joint_snapshot(tmp_path))]))


def test_a_formula_reading_a_formula_filled_late_is_unchanged() -> None:
    chain = cfg([seq("a"), _filled_late(), formula("g", "f + '!'", ["f"])])
    assert_accepted(chain)
    assert generated(chain, "g") == ["north!", "south!"] * 2 + ["north!"]
    later = cfg([seq("a"), formula("g", "f + '!'", ["f"]), _filled_late()])
    assert_refused(later, FORWARD, p("g", "references.0"))
    reader = cfg([seq("a"), _filled_late(), formula("g", "f + '!'", ["f"]), _conditional("g")])
    assert_refused(reader, FORWARD, p("tier", "condition_on"))


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


@pytest.mark.parametrize(
    "expr",
    [
        "v + len([v for v in [1, 2, 3]])",  # read outside, bound inside a comprehension
        "len([w for x in [1] for w in [v]])",  # nested comprehension reading an outer name
    ],
)
def test_name_bound_in_an_inner_scope_is_still_unknown_outside_it(expr: str) -> None:
    cols = [seq("b"), formula("x", expr, ["b"])]
    assert_refused(cfg(cols), "generate_formula_name_unknown", p("x", "formula"))


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("len([v for v in [1, 2, 3]])", [3] * 5),
        ("len([w for v in [[1, 2]] for w in v])", [2] * 5),
        ("len({a, b})", [1] * 5),
        ("i + index", [0, 2, 4, 6, 8]),
    ],
)
def test_scoped_shapes_are_accepted_and_generate_values(expr: str, expected: list[int]) -> None:
    config = cfg([seq("a"), seq("b"), formula("x", expr, ["a", "b"])])
    assert_accepted(config)
    # An accepted formula must run: a node the evaluator rejects would come back as all nulls.
    assert generated(config, "x") == expected


@pytest.mark.parametrize(
    "expr",
    [
        "(lambda q: q + 1)(b)",
        "a + (lambda a: a + 1)(2)",
        "(x := b) + 1",
        "{v for v in [b]}",
        "str(*[1])",
        "f(*[1])(2)",
        "str(**{})",
    ],
)
def test_syntax_the_runtime_cannot_evaluate_is_refused(expr: str) -> None:
    config = cfg([seq("b"), formula("x", expr, ["b"])])
    for site in _sites():
        with pytest.raises(Exception) as info:
            site(config)
        assert getattr(info.value, "code", None) == "generate_formula_invalid", site.__name__
        assert expr not in str(info.value)
        assert info.value.__context__ is None
    from decoy_engine.plan.checks import check_generate_columns

    with pytest.raises(PlanCompileError) as plan_only:
        check_generate_columns(config, provider_snapshot={}, default_locale=None)
    assert (plan_only.value.code, plan_only.value.path) == (
        "generate_formula_invalid",
        p("x", "formula"),
    )
    assert expr not in str(plan_only.value)


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
