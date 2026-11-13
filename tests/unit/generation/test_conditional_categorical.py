"""GEN-INHERIT E2: a `categorical` column whose mix depends on an earlier column (`by:`).

AT-2 (the plan-check part), AT-7 (categorical), AT-8 to AT-11 and AT-15. The registry cells of
AT-2 are in `tests/unit/config/test_generate_options.py`. Parent values are keyed by
`str(parent)`, the fitted `condition_on` rule; a row whose key has no entry draws from the
column's top-level mix, and the resident route reports those rows with the shared value-free
warning `warning.generation_condition_parent_unseen` (`provider: categorical`).
"""

from __future__ import annotations

import copy
import json
import logging
import random
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.config import categorical_arrow_type, generated_arrow_type
from decoy_engine.generation._admission import classify_plan
from decoy_engine.generation._numeric_typing import numeric_plan
from decoy_engine.generation._reproducibility import reproducibility_exclusions
from decoy_engine.generation.pinned import (
    PinnedModeError,
    PinnedPlan,
    compile_pinned_plan,
    execute_pinned,
)
from decoy_engine.generation.pool import GenerationError
from decoy_engine.generators.derivation import GenDeriveContext, with_table
from decoy_engine.plan import PlanCompileError
from tests.unit.generation.relational._support import REF, SEED, is_safe, plan_of, run_route
from tests.unit.generation.unique._gu_e2_support import cfg, table

CODE = "generation_condition_parent_unseen"
UNAVAILABLE = "categorical_condition_column_unavailable"
SENTINEL = "SENTPVALUE"
_DETAIL_KEYS = {
    "table",
    "column",
    "condition_on",
    "rows",
    "rows_null_parent",
    "rows_total",
    "parent_values",
    "parent_values_capped",
}
REGION = {"name": "region", "type": "categorical", "categories": ["north", "south", SENTINEL]}


def _tier(**extra: Any) -> dict[str, Any]:
    return {
        "name": "tier",
        "type": "categorical",
        "categories": ["basic", "plus", "premium"],
        "weights": [5, 3, 2],
        "condition_on": "region",
        "by": {
            "north": {"weights": [2, 3, 5]},
            "south": {"categories": ["gold", "silver"], "weights": [9, 1]},
        },
        **extra,
    }


def _config(rows: int = 300, *cols: dict[str, Any]) -> dict[str, Any]:
    return cfg([table("t", rows, list(cols) or [REGION, _tier()])], SEED)


def _direct(col: dict[str, Any], parents: list[Any] | None, *, pinned: bool = False) -> list[Any]:
    from decoy_engine.generation.synthesize import _generate_column

    generated = None if parents is None else {"region": parents}
    return _generate_column(
        col, len(parents or []) or 4, SEED, None, {}, None, generated, table_name="t", pinned=pinned
    )


def _follows_its_entry(out: dict[str, pa.Table]) -> None:
    """Every `south` row draws from its own categories; every other row from the top level."""
    rows = list(
        zip(out["t"].column("region").to_pylist(), out["t"].column("tier").to_pylist(), strict=True)
    )
    assert any(region == "south" for region, _ in rows), "no south row: the check is vacuous"
    for region, tier in rows:
        allowed = {"south": {"gold", "silver"}}.get(region, {"basic", "plus", "premium"})
        assert tier in allowed


# ---- AT-2 (plan check): forward reference and mixed category types ----------------------------


def _plan_error(config: dict[str, Any]) -> PlanCompileError:
    with pytest.raises(PlanCompileError) as exc:
        plan_of(config)
    assert is_safe(exc.value)
    return exc.value


def test_at2_a_later_or_missing_parent_is_a_forward_reference() -> None:
    later = _plan_error(_config(10, _tier(), REGION))
    assert later.code == reasons.plan.GENERATE_COLUMN_FORWARD_REFERENCE
    assert later.path == "tables.t.generate_columns.tier.condition_on"
    missing = _plan_error(_config(10, REGION, _tier(condition_on="ghost")))
    assert missing.code == reasons.plan.GENERATE_COLUMN_FORWARD_REFERENCE


def test_at2_mixed_category_types_across_entries_are_refused_once() -> None:
    mixed = _tier(categories=[1, 2, 3], by={"north": {"categories": [f"{SENTINEL}x"]}})
    exc = _plan_error(_config(10, REGION, mixed))
    assert exc.code == reasons.plan.GENERATE_CATEGORIES_MIXED
    assert SENTINEL not in str(exc)


def test_at2_number_entries_beside_number_categories_compile_and_draw(tmp_path: Path) -> None:
    ints = _tier(categories=[1, 2, 3], weights=None, by={"north": {"categories": [7, 8]}})
    out = run_route("resident", _config(60, REGION, ints), tmp_path)
    assert out["t"].schema.field("tier").type == pa.int64()
    north = {
        tier
        for region, tier in zip(
            out["t"].column("region").to_pylist(), out["t"].column("tier").to_pylist(), strict=True
        )
        if region == "north"
    }
    assert north and north <= {7, 8}


# ---- AT-7: route parity ------------------------------------------------------------------------


def test_at7_resident_and_pinned_draw_the_same_rows_with_fallbacks(tmp_path: Path) -> None:
    config = _config()
    resident = run_route("resident", config, tmp_path)
    pinned = run_route("pinned", config, tmp_path)
    assert resident["t"].to_pylist() == pinned["t"].to_pylist()
    regions = resident["t"].column("region").to_pylist()
    assert SENTINEL in regions, "no fallback row: the parity cell would be vacuous"
    _follows_its_entry(resident)


def test_at7_streamed_admission_declines_the_conditional_form() -> None:
    prepared = json.loads(plan_of(_config()).generation.config_json)
    verdict = classify_plan(prepared)
    assert verdict.reason() == "t.tier:categorical_condition_on"
    plain = json.loads(plan_of(_config(10, REGION)).generation.config_json)
    assert classify_plan(plain).eligible


# ---- AT-8: unseen parent values ----------------------------------------------------------------


def test_at8_unseen_parents_fall_back_and_warn_once_without_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    raised: list[Any] = []
    with caplog.at_level(logging.WARNING):
        out = run_route("resident", _config(), tmp_path, on_warning=raised.append)
    records = [r for r in raised if str(r.code) == CODE]
    (record,) = records
    regions = out["t"].column("region").to_pylist()
    tiers = out["t"].column("tier").to_pylist()
    unseen = regions.count(SENTINEL)
    assert unseen > 0
    assert record.detail == {
        "table": "t",
        "column": "tier",
        "condition_on": "region",
        "rows": unseen,
        "rows_null_parent": 0,
        "rows_total": 300,
        "parent_values": 1,
        "parent_values_capped": False,
    }
    assert set(record.detail) == _DETAIL_KEYS
    assert (record.provider, record.column) == ("categorical", "tier")
    for region, tier in zip(regions, tiers, strict=True):
        allowed = {"south": {"gold", "silver"}}.get(region, {"basic", "plus", "premium"})
        assert tier in allowed
    lines = [r.getMessage() for r in caplog.records if CODE in r.getMessage()]
    assert len(lines) == 1
    for text in (*lines, repr(record), json.dumps(record.detail)):
        assert SENTINEL not in text and "north" not in text and "south" not in text


def test_at8_every_parent_declared_gives_no_warning(tmp_path: Path) -> None:
    region = {**REGION, "categories": ["north", "south"]}
    raised: list[Any] = []
    out = run_route("resident", _config(100, region, _tier()), tmp_path, on_warning=raised.append)
    assert [r for r in raised if str(r.code) == CODE] == []
    _follows_its_entry(out)


def test_at8_pinned_draws_the_fallback_rows_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        out = execute_pinned(compile_pinned_plan(copy.deepcopy(_config()), REF), SEED)
    assert SENTINEL in out["t"].column("region").to_pylist()
    assert not [r for r in caplog.records if CODE in r.getMessage()]
    _follows_its_entry(out)


# ---- AT-9: the same number of draws as a plain column ------------------------------------------


@pytest.mark.parametrize("seed", range(10))
def test_at9_entries_equal_to_the_top_mix_draw_one_choices_stream(seed: int) -> None:
    from decoy_engine.generation.synthesize import _generate_column

    cats, weights = ["a", "b", "c"], [5, 3, 2]
    col = {
        "name": "tier",
        "type": "categorical",
        "categories": cats,
        "weights": weights,
        "condition_on": "region",
        "by": {"north": {"weights": weights}, "south": {"categories": cats, "weights": weights}},
    }
    n = 500
    parents = [("north", "south", "east", None)[i % 4] for i in range(n)]
    got = _generate_column(col, n, seed, None, {}, None, {"region": parents}, table_name="t")
    own = GenDeriveContext.for_column(
        derive_key=None, column_config=with_table(col, "t"), fallback_seed=seed
    ).base_int("py")
    assert got == random.Random(own).choices(cats, weights, k=n)


def test_at9_an_entry_without_weights_draws_uniformly_like_a_plain_column() -> None:
    from decoy_engine.generation.synthesize import _generate_column

    col = {**_tier(), "by": {"north": {"categories": ["x", "y", "z"]}}}
    n = 200
    got = _generate_column(col, n, 3, None, {}, None, {"region": ["north"] * n}, table_name="t")
    own = GenDeriveContext.for_column(
        derive_key=None, column_config=with_table(col, "t"), fallback_seed=3
    ).base_int("py")
    assert got == random.Random(own).choices(["x", "y", "z"], k=n)


# ---- AT-10: parent keying ----------------------------------------------------------------------


def test_at10_int_bool_null_and_text_parents_match_their_str_keys() -> None:
    by = {
        "1": {"categories": ["one"]},
        "True": {"categories": ["yes"]},
        "None": {"categories": ["none"]},
        "north": {"categories": ["n"]},
    }
    col = {
        "name": "tier",
        "type": "categorical",
        "categories": ["fb"],
        "condition_on": "region",
        "by": by,
    }
    parents = [1, True, None, "north", 2, "1", "true"]
    assert _direct(col, parents) == ["one", "yes", "none", "n", "fb", "one", "fb"]


# ---- AT-11: typing of the parent and of the column ---------------------------------------------


def _reader_config(reader: dict[str, Any]) -> dict[str, Any]:
    return _config(20, {"name": "id", "type": "sequence", "start": 1}, reader)


def test_at11_a_sequence_parent_is_typed_as_under_a_fitted_condition_on(tmp_path: Path) -> None:
    by = {str(i): {"categories": [f"k{i}"]} for i in range(1, 21)}
    cat = {
        "name": "kid",
        "type": "categorical",
        "categories": ["fb"],
        "condition_on": "id",
        "by": by,
    }
    stat = {"name": "kid", "type": "statistical", "snapshot_file": "s.json", "condition_on": "id"}
    alone = numeric_plan(
        _reader_config({"name": "kid", "type": "categorical", "categories": ["fb"]})
    )
    assert alone.get(("t", "id")) == pa.int64(), "the parent must be a numeric candidate"
    assert numeric_plan(_reader_config(cat)).get(("t", "id")) is None
    assert numeric_plan(_reader_config(stat)).get(("t", "id")) is None
    out = run_route("resident", _reader_config(cat), tmp_path)
    ids = out["t"].column("id").to_pylist()
    assert out["t"].column("kid").to_pylist() == [f"k{int(i)}" for i in ids]


@pytest.mark.parametrize(
    ("categories", "want"),
    [
        ([1, 2, 3], pa.int64()),
        (["a", "b"], pa.string()),
        ([True, False], pa.bool_()),
        ([1.5, 2.5], pa.float64()),
        ([1, 2.5], None),
    ],
)
def test_at11_plain_categorical_types_are_unchanged(categories: list[Any], want: Any) -> None:
    col = {"name": "c", "type": "categorical", "categories": categories}
    assert categorical_arrow_type(col)[0] == want
    assert generated_arrow_type(col) == want


def test_at11_a_conditional_column_is_typed_by_every_category_it_can_draw() -> None:
    ints = {**_tier(categories=[1, 2, 3], weights=None), "by": {"north": {"categories": [7]}}}
    assert categorical_arrow_type(ints)[0] == pa.int64()
    text = {**ints, "by": {"north": {"categories": ["x"]}}}
    assert categorical_arrow_type(text)[0] is None
    assert generated_arrow_type(text) is None
    floats = {**ints, "categories": [1.5], "by": {"north": {"weights": [1]}}}
    assert categorical_arrow_type(floats)[0] == pa.float64()


# ---- AT-15: a missing conditioning column is a value-free run-time refusal ---------------------


def _unavailable(exc: GenerationError) -> None:
    assert str(exc.code) == UNAVAILABLE
    assert exc.code.namespace == "runtime"
    assert is_safe(exc)
    assert SENTINEL not in str(exc)


def test_at15_resident_refuses_a_missing_conditioning_column() -> None:
    from decoy_engine.generation.synthesize import _generate_column

    col = {**_tier(), "by": {SENTINEL: {"weights": [1, 1, 1]}}}
    for generated in (None, {"other": [SENTINEL] * 4}):
        with pytest.raises(GenerationError) as exc:
            _generate_column(col, 4, SEED, None, {}, None, generated, table_name="t")
        _unavailable(exc.value)


def test_at15_pinned_refuses_a_missing_conditioning_column() -> None:
    plan = compile_pinned_plan(copy.deepcopy(_config(10)), REF)
    stored = plan.to_dict()
    entry = next(c for c in stored["tables"][0]["columns"] if c["name"] == "tier")
    column = json.loads(entry["config_json"])
    column["condition_on"] = "ghost"
    entry["config_json"] = json.dumps(column, sort_keys=True)
    with pytest.raises(GenerationError) as exc:
        execute_pinned(PinnedPlan.from_dict(stored), SEED)
    _unavailable(exc.value)


def test_at15_the_code_is_registered_unreleased_in_the_runtime_catalog() -> None:
    spec = reasons.get("runtime", UNAVAILABLE)
    assert spec.since == reasons.UNRELEASED
    assert spec.owner == "GenerationError" and spec.kind is reasons.Kind.FAILURE
    assert "statistical" not in spec.summary


# ---- neighbours: unique, reproducibility, pinned anchors ---------------------------------------


def test_a_column_conditioned_on_an_excluded_column_is_excluded() -> None:
    fresh = {**REGION, "determinism": "fresh"}
    config = json.loads(plan_of(_config(10, fresh, _tier())).generation.config_json)
    assert ("t", "tier") in reproducibility_exclusions(config)


def test_a_pinned_date_anchored_on_a_conditional_column_checks_every_entry() -> None:
    dates = {
        "name": "d",
        "type": "categorical",
        "categories": ["2020-01-01"],
        "condition_on": "region",
        "by": {"north": {"categories": ["next tuesday"]}},
    }
    window = {"name": "w", "type": "windowed_date", "anchor": "d", "max_days": 3}
    with pytest.raises(PinnedModeError) as exc:
        compile_pinned_plan(_config(10, REGION, dates, window), REF)
    assert exc.value.code == reasons.plan.PINNED_WINDOWED_ANCHOR_NOT_ABSOLUTE


WIDE = "w" * 500


def _wide(**entry: Any) -> dict[str, Any]:
    return {
        "name": "tier",
        "type": "categorical",
        "categories": ["a", "b"],
        "condition_on": "region",
        "by": {"north": entry or {"categories": [WIDE]}},
    }


def test_the_width_bound_covers_the_longest_entry_category() -> None:
    from decoy_engine.generation._bounds import categorical_payload_bound, compute_payload_bounds

    assert categorical_payload_bound(_wide()) == len(WIDE)
    assert compute_payload_bounds(_config(10, REGION, _wide()))[("t", "tier")] == len(WIDE)
    numbers = {**_wide(categories=[1, 2]), "categories": [3, 4]}
    assert categorical_payload_bound(numbers) == 8
    mixed = {**_wide(categories=[WIDE]), "categories": [3, 4]}
    assert categorical_payload_bound(mixed) is None


@pytest.mark.parametrize(
    ("categories", "want"),
    [(["a", "bcd"], 3), ([1, 2], 8), ([1.5, True], 8), (["a", 1], None), ([], None)],
)
def test_a_plain_column_keeps_its_width_bound(categories: list[Any], want: int | None) -> None:
    from decoy_engine.generation._bounds import categorical_payload_bound

    plain = {"name": "c", "type": "categorical", "categories": categories}
    assert categorical_payload_bound(plain) == want
    # A `by` without `condition_on` is not the conditional form; only the top-level list counts.
    assert categorical_payload_bound({**plain, "by": {"x": {"categories": [WIDE]}}}) == want
