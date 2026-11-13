"""GEN-STRICT E1: generate-column options are checked by one registry (AT-1 to AT-5)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from decoy_engine import reasons
from decoy_engine.config import PipelineConfig, parse_pipeline_config
from decoy_engine.config._errors import PipelineConfigError
from decoy_engine.config._generate_options import GENERATE_OPTIONS
from decoy_engine.config._tables import GENERATE_TYPES
from decoy_engine.generators.derivation import strategy_config_fingerprint

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "gen_strict"
ROOT = Path(__file__).resolve().parents[3]
CANARY = "CANARY-7731"

# The smallest valid column per type: exactly the catalog's required keys.
MINIMAL: dict[str, dict[str, Any]] = {
    "faker": {"faker_type": "name"},
    "sequence": {"start": 1},
    "categorical": {"categories": ["a", "b"]},
    "formula": {"formula": "1 + 1"},
    "reference": {"reference_table": "p", "reference_column": "id"},
    "statistical": {"snapshot_file": "s.json"},
    "derived": {"expression": "a + 1"},
    "derived_aggregate": {"op": "sum", "column": "x"},
    "grouped_series": {"group_by": "g", "order_by": "o"},
    "windowed_date": {"anchor": "a", "max_days": 5},
    "group_key": {"group_by": "g"},
    "constant": {"value": "US"},
    "distribution": {"family": "uniform", "low": 0, "high": 3},
    "lookup": {"codeset": "icd10"},
}


def _config(*columns: dict[str, Any]) -> dict[str, Any]:
    """Table `t` under test, plus a parent `p` so a reference column has something to point at."""
    parent = {"name": "id", "type": "sequence", "start": 1}
    return {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": {},
        "targets": {
            n: {"type": "file", "format": "csv", "path": f"/o/{n}.csv"} for n in ("t", "p")
        },
        "tables": [
            {"name": "t", "row_count": 5, "generate_columns": list(columns)},
            {"name": "p", "row_count": 5, "generate_columns": [parent]},
        ],
    }


def _col(type_: str, **extra: Any) -> dict[str, Any]:
    return {"name": "c", "type": type_, **MINIMAL[type_], **extra}


def _errors(*columns: dict[str, Any]) -> list[dict[str, Any]]:
    with pytest.raises(ValidationError) as info:
        PipelineConfig.model_validate(_config(*columns))
    return info.value.errors(include_url=False)


def _spot(err: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    return err["type"], tuple(err["loc"][4:])


def test_every_generate_type_has_a_minimal_column() -> None:
    assert set(MINIMAL) == set(GENERATE_TYPES)
    for type_ in MINIMAL:
        PipelineConfig.model_validate(_config(_col(type_)))


# AT-1 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("type_", sorted(MINIMAL))
def test_unknown_key_is_refused_at_its_name(type_: str) -> None:
    errs = _errors(_col(type_, weigths=1))
    assert len(errs) == 1
    assert errs[0]["type"] == UNKNOWN_KEY
    assert tuple(errs[0]["loc"]) == ("tables", 0, "generate_columns", 0, "weigths")


def test_two_unknown_keys_give_two_errors() -> None:
    errs = _errors(_col("sequence", weigths=1, stpe=2))
    assert sorted(e["loc"][-1] for e in errs) == ["stpe", "weigths"]
    assert {e["type"] for e in errs} == {UNKNOWN_KEY}


def test_parse_pipeline_config_reports_registry_summary_and_location() -> None:
    with pytest.raises(PipelineConfigError) as info:
        parse_pipeline_config(_config(_col("categorical", weigths=[1, 2])))
    (problem,) = info.value.problems
    assert problem.code == UNKNOWN_KEY
    assert problem.location == "tables[0].generate_columns[0].weigths"
    assert problem.text == reasons.lookup(UNKNOWN_KEY)[0].summary


def test_unknown_key_in_second_column_names_that_column() -> None:
    errs = _errors(_col("sequence"), {**_col("sequence"), "name": "d", "stpe": 1})
    assert [tuple(e["loc"]) for e in errs] == [("tables", 0, "generate_columns", 1, "stpe")]


# AT-2 ---------------------------------------------------------------------------------

TYPE = str(reasons.config.GENERATE_OPTION_TYPE)
RANGE = str(reasons.config.GENERATE_OPTION_RANGE)
WEIGHTS = str(reasons.config.GENERATE_WEIGHTS_INVALID)
VALUE = str(reasons.config.GENERATE_OPTION_VALUE_UNKNOWN)
FORMULA = str(reasons.config.GENERATE_FORMULA_INVALID)
UNKNOWN_KEY = str(reasons.config.GENERATE_OPTION_UNKNOWN)
DUPLICATE = str(reasons.config.GENERATE_COLUMN_DUPLICATE)

BAD: list[tuple[str, dict[str, Any], str, tuple[Any, ...]]] = [
    # common options
    ("sequence", {"null_probability": -0.1}, RANGE, ("null_probability",)),
    ("sequence", {"null_probability": 1.5}, RANGE, ("null_probability",)),
    ("sequence", {"null_probability": float("nan")}, RANGE, ("null_probability",)),
    ("sequence", {"null_probability": True}, TYPE, ("null_probability",)),
    ("sequence", {"null_probability": "0.5"}, TYPE, ("null_probability",)),
    ("sequence", {"determinism": "sometimes"}, VALUE, ("determinism",)),
    ("sequence", {"determinism": 3}, TYPE, ("determinism",)),
    # faker
    ("faker", {"locale": 5}, TYPE, ("locale",)),
    ("faker", {"locale": []}, TYPE, ("locale",)),
    ("faker", {"locale": ["en_US", 5]}, TYPE, ("locale", 1)),
    ("faker", {"faker_kwargs": [1]}, TYPE, ("faker_kwargs",)),
    ("faker", {"faker_kwargs": "x"}, TYPE, ("faker_kwargs",)),
    ("faker", {"faker_kwargs": {1: "x"}}, TYPE, ("faker_kwargs",)),
    ("faker", {"faker_kwargs": {(1, 2): "x"}}, TYPE, ("faker_kwargs",)),
    ("faker", {"faker_type": 5}, TYPE, ("faker_type",)),
    # sequence
    ("sequence", {"start": "1"}, TYPE, ("start",)),
    ("sequence", {"start": True}, TYPE, ("start",)),
    ("sequence", {"start": 1.5}, TYPE, ("start",)),
    ("sequence", {"step": True}, TYPE, ("step",)),
    ("sequence", {"step": "2"}, TYPE, ("step",)),
    ("sequence", {"step": 0}, RANGE, ("step",)),
    ("sequence", {"pad_length": -1}, RANGE, ("pad_length",)),
    ("sequence", {"pad_length": True}, TYPE, ("pad_length",)),
    ("sequence", {"prefix": 5}, TYPE, ("prefix",)),
    ("sequence", {"suffix": ["x"]}, TYPE, ("suffix",)),
    # categorical
    ("categorical", {"categories": "abc"}, TYPE, ("categories",)),
    ("categorical", {"weights": 5}, TYPE, ("weights",)),
    ("categorical", {"weights": [1]}, WEIGHTS, ("weights",)),
    ("categorical", {"weights": [1, 2, 3]}, WEIGHTS, ("weights",)),
    ("categorical", {"weights": [1, -1]}, WEIGHTS, ("weights", 1)),
    ("categorical", {"weights": [0, 0]}, WEIGHTS, ("weights",)),
    ("categorical", {"weights": [1, float("nan")]}, WEIGHTS, ("weights", 1)),
    ("categorical", {"weights": [1, float("inf")]}, WEIGHTS, ("weights", 1)),
    ("categorical", {"weights": [1, True]}, WEIGHTS, ("weights", 1)),
    ("categorical", {"weights": [1, "2"]}, WEIGHTS, ("weights", 1)),
    # formula
    ("formula", {"formula": "a +"}, FORMULA, ("formula",)),
    ("formula", {"formula": 5}, TYPE, ("formula",)),
    ("formula", {"references": "a"}, TYPE, ("references",)),
    ("formula", {"references": ["a", 3]}, TYPE, ("references", 1)),
    # reference
    ("reference", {"distribution": "bogus"}, VALUE, ("distribution",)),
    ("reference", {"distribution": 3}, TYPE, ("distribution",)),
    ("reference", {"weights": [1, -1]}, WEIGHTS, ("weights", 1)),
    ("reference", {"weights": [0, 0]}, WEIGHTS, ("weights",)),
    ("reference", {"min_per_parent": -1}, RANGE, ("min_per_parent",)),
    ("reference", {"max_per_parent": True}, TYPE, ("max_per_parent",)),
    ("reference", {"max_per_parent": "3"}, TYPE, ("max_per_parent",)),
    ("reference", {"reference_table": 7}, TYPE, ("reference_table",)),
    # statistical, derived, grouped_series, windowed_date, group_key: types only
    ("statistical", {"high_cardinality": "yes"}, TYPE, ("high_cardinality",)),
    ("statistical", {"allow_real_categories": 1}, TYPE, ("allow_real_categories",)),
    ("statistical", {"other_mode": 3}, TYPE, ("other_mode",)),
    ("statistical", {"condition_on": ["a"]}, TYPE, ("condition_on",)),
    ("derived", {"bounds": [0, 1]}, TYPE, ("bounds",)),
    ("derived", {"bounds": {1: 0}}, TYPE, ("bounds",)),
    ("derived", {"null_propagation": 1}, TYPE, ("null_propagation",)),
    ("derived_aggregate", {"op": 3}, TYPE, ("op",)),
    ("derived_aggregate", {"column": ["a"]}, TYPE, ("column",)),
    ("grouped_series", {"step": True}, TYPE, ("step",)),
    ("grouped_series", {"max_step": "10"}, TYPE, ("max_step",)),
    ("grouped_series", {"generator": 1}, TYPE, ("generator",)),
    ("windowed_date", {"max_days": "5"}, TYPE, ("max_days",)),
    ("windowed_date", {"min_days": 1.5}, TYPE, ("min_days",)),
    ("windowed_date", {"distribution": 2}, TYPE, ("distribution",)),
    ("group_key", {"length": True}, TYPE, ("length",)),
    ("group_key", {"prefix": 4}, TYPE, ("prefix",)),
]


@pytest.mark.parametrize(
    ("type_", "extra", "code", "loc"),
    BAD,
    ids=[
        f"{t}-{'.'.join(map(str, loc))}-{c.split('_')[-1]}-{i}"
        for i, (t, _, c, loc) in enumerate(BAD)
    ],
)
def test_bad_static_value_is_refused_with_code_and_location(
    type_: str, extra: dict[str, Any], code: str, loc: tuple[Any, ...]
) -> None:
    errs = _errors(_col(type_, **extra))
    assert [_spot(e) for e in errs] == [(code, loc)]


# None is "not given" only where the reader treats it so; everywhere else it is a type error.
NONE_OK = [
    ("categorical", "weights"),
    ("faker", "locale"),
    ("faker", "faker_kwargs"),
    ("sequence", "null_probability"),
    ("formula", "references"),
    ("reference", "min_per_parent"),
    ("reference", "max_per_parent"),
    ("derived", "bounds"),
    ("statistical", "high_cardinality"),
    ("statistical", "allow_real_categories"),
    ("statistical", "condition_on"),
    ("statistical", "source_column"),
    ("statistical", "other_mode"),
]
NONE_BAD = [
    ("sequence", "step"),
    ("sequence", "prefix"),
    ("sequence", "suffix"),
    ("sequence", "pad_length"),
    ("sequence", "determinism"),
    ("reference", "distribution"),
    ("derived", "null_propagation"),
    ("derived_aggregate", "op"),
    ("derived_aggregate", "column"),
    ("grouped_series", "start"),
    ("grouped_series", "step"),
    ("grouped_series", "max_step"),
    ("grouped_series", "generator"),
    ("windowed_date", "min_days"),
    ("windowed_date", "distribution"),
    ("group_key", "length"),
    ("group_key", "prefix"),
]


@pytest.mark.parametrize(("type_", "key"), NONE_OK)
def test_none_means_not_given_where_the_reader_agrees(type_: str, key: str) -> None:
    PipelineConfig.model_validate(_config(_col(type_, **{key: None})))


@pytest.mark.parametrize(("type_", "key"), NONE_BAD)
def test_none_elsewhere_is_a_type_error(type_: str, key: str) -> None:
    assert [_spot(e) for e in _errors(_col(type_, **{key: None}))] == [(TYPE, (key,))]


def test_huge_int_weight_is_a_refusal_not_an_overflow() -> None:
    huge = 10**400
    for extra in ({"weights": [1, huge]}, {"weights": [huge, huge]}):
        raw = _config(_col("categorical", **extra))
        assert {e["type"] for e in _errors(_col("categorical", **extra))} == {WEIGHTS}
        with pytest.raises(PipelineConfigError):
            parse_pipeline_config(raw)
    big_sum = {"weights": [1e308, 1e308]}
    assert [_spot(e) for e in _errors(_col("categorical", **big_sum))] == [(WEIGHTS, ("weights",))]
    PipelineConfig.model_validate(_config(_col("categorical", weights=[10**300, 1])))


def test_fitted_reference_parses_and_other_values_still_refuse() -> None:
    PipelineConfig.model_validate(
        _config(_col("reference", distribution="fitted", snapshot_file="s.json"))
    )
    errs = _errors(_col("reference", distribution="fitted-ish"))
    assert [_spot(e) for e in errs] == [(VALUE, ("distribution",))]


def test_snapshot_keys_refused_unless_distribution_is_fitted() -> None:
    for extra in ({"snapshot_file": "s.json"}, {"source_column": "c"}):
        errs = _errors(_col("reference", distribution="random", **extra))
        (key,) = extra
        assert [_spot(e) for e in errs] == [(UNKNOWN_KEY, (key,))]
    PipelineConfig.model_validate(
        _config(_col("reference", distribution="fitted", snapshot_file="s.json", source_column="c"))
    )


def test_weighted_distribution_requires_weights() -> None:
    errs = _errors(_col("reference", distribution="weighted"))
    assert [_spot(e) for e in errs] == [("missing", ("weights",))]


@pytest.mark.parametrize(
    ("type_", "key"),
    [
        ("faker", "faker_type"),
        ("sequence", "start"),
        ("categorical", "categories"),
        ("formula", "formula"),
        ("reference", "reference_table"),
        ("reference", "reference_column"),
        ("statistical", "snapshot_file"),
        ("derived", "expression"),
        ("grouped_series", "group_by"),
        ("grouped_series", "order_by"),
        ("windowed_date", "anchor"),
        ("windowed_date", "max_days"),
        ("group_key", "group_by"),
    ],
)
def test_missing_required_key_is_reported_at_the_key(type_: str, key: str) -> None:
    col = _col(type_)
    del col[key]
    assert [_spot(e) for e in _errors(col)] == [("missing", (key,))]


def test_start_none_counts_as_missing_and_zero_is_valid() -> None:
    assert [_spot(e) for e in _errors(_col("sequence", start=None))] == [("missing", ("start",))]
    PipelineConfig.model_validate(_config(_col("sequence", start=0)))


def test_one_validator_collects_every_problem_for_a_column() -> None:
    errs = _errors(_col("sequence", step=0, pad_length=-1, null_probability=2, nope=1))
    assert sorted(e["loc"][-1] for e in errs) == ["nope", "null_probability", "pad_length", "step"]


def test_pooled_stays_faker_only() -> None:
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(_config(_col("sequence", pooled=True)))
    PipelineConfig.model_validate(_config(_col("faker", pooled=True)))


@pytest.mark.parametrize(
    ("type_", "extra"),
    [
        ("categorical", {"categories": [CANARY, 1], "weights": [7731.5, CANARY]}),
        ("categorical", {"categories": [CANARY], "weights": [CANARY]}),
        ("categorical", {"categories": [CANARY, "b"], "weights": [-7731.5, 1]}),
        ("categorical", {"categories": [CANARY, "b"], "weights": [0, 0], "extra_" + CANARY: 1}),
        ("formula", {"formula": CANARY + " +"}),
        ("sequence", {"start": CANARY}),
        ("sequence", {"step": CANARY, "prefix": [CANARY]}),
        ("reference", {"distribution": CANARY}),
        ("faker", {"locale": [CANARY, 7731.5]}),
        ("faker", {"faker_kwargs": CANARY}),
        ("sequence", {"null_probability": CANARY}),
        ("sequence", {"determinism": CANARY}),
    ],
)
def test_refusals_never_echo_a_submitted_value(type_: str, extra: dict[str, Any]) -> None:
    raw = _config(_col(type_, **extra))
    with pytest.raises(ValidationError) as info:
        PipelineConfig.model_validate(raw)
    exc = info.value
    shown = [str(exc), repr(exc.errors()), exc.json()]
    with pytest.raises(PipelineConfigError) as parsed:
        parse_pipeline_config(raw)
    shown += [p.text + p.code for p in parsed.value.problems] + [str(parsed.value)]
    value_free = {"extra_" + CANARY} if "extra_" + CANARY in extra else set()
    for text in shown:
        for token in (CANARY, "7731.5"):
            if token == CANARY and value_free:
                continue  # a key name is config text, not a data value
            assert token not in text


def test_templates_are_fixed_text() -> None:
    errs = _errors(_col("categorical", weights=[CANARY, 1]))
    assert errs[0]["msg"] == reasons.lookup(WEIGHTS)[0].summary
    assert "ctx" not in errs[0] or not errs[0]["ctx"]


@pytest.mark.parametrize(
    ("type_", "extra"),
    [
        ("sequence", {"null_probability": 0}),
        ("sequence", {"null_probability": 1}),
        ("sequence", {"null_probability": 0.25, "determinism": "stable"}),
        ("sequence", {"determinism": "fresh"}),
        ("sequence", {"step": -1, "pad_length": 0, "prefix": "", "suffix": "x"}),
        ("categorical", {"weights": [0, 1]}),
        ("categorical", {"weights": [0.5, 2]}),
        ("categorical", {"categories": [1, 2.5], "weights": None}),
        ("faker", {"locale": ["en_US", "fr_FR"]}),
        ("faker", {"locale": "en_US", "faker_kwargs": {"a": 1}, "pooled": False}),
        ("faker", {"locale": None, "faker_kwargs": None}),
        ("reference", {"distribution": "weighted", "weights": [1, 2]}),
        ("reference", {"distribution": "sequential", "min_per_parent": 0, "max_per_parent": 4}),
        ("reference", {"distribution": "fitted", "snapshot_file": "v.snap.json"}),
        (
            "reference",
            {"distribution": "fitted", "snapshot_file": "v.json", "source_column": "pid"},
        ),
        ("formula", {"formula": "a * 2", "references": ["a"]}),
        ("formula", {"formula": "  1 + 1  "}),
        ("windowed_date", {"min_days": -3, "distribution": "uniform"}),
        ("statistical", {"high_cardinality": False, "allow_real_categories": True}),
        ("derived", {"bounds": {"min": 0}, "null_propagation": "explicit_null"}),
        ("group_key", {"length": 8, "prefix": "K"}),
    ],
)
def test_boundary_values_pass(type_: str, extra: dict[str, Any]) -> None:
    PipelineConfig.model_validate(_config(_col(type_, **extra)))


def test_validation_never_mutates_the_dump() -> None:
    raw = _config(_col("sequence", step=2))
    dumped = PipelineConfig.model_validate(raw).model_dump()["tables"][0]["generate_columns"][0]
    assert "pad_length" not in dumped and "prefix" not in dumped
    assert dumped["step"] == 2


# AT-3 ---------------------------------------------------------------------------------

IDENTITY = json.loads((FIXTURE / "generate-config-identity.json").read_text())


def _raw_for(key: str) -> dict[str, Any]:
    entry = IDENTITY[key]
    if "raw" in entry:
        return copy.deepcopy(entry["raw"])
    import yaml

    return yaml.safe_load((ROOT / key.removeprefix("file:")).read_text())


# The `testflight:` entries are the generate tables of the three test-flight manifests, recorded at
# main. Jobs C and D had a `params: {mean, std}` key on two columns that only the test-flight
# evaluator read; it moved to their distribution spec (the config refuses unread keys), so the
# recorded configs are the post-move ones and those two jobs' golden fingerprints were re-pinned.


@pytest.mark.parametrize("key", sorted(IDENTITY))
def test_shipped_generate_configs_keep_dump_and_fingerprints(key: str) -> None:
    cfg = PipelineConfig.model_validate(_raw_for(key))
    dump = cfg.model_dump(mode="json")
    assert dump == IDENTITY[key]["dump"]
    fingerprints = {
        f"{t['name']}.{c['name']}": strategy_config_fingerprint(
            {**c, "_table": t["name"]},
            content_digests={("*", "snapshot_file"): "sha256:0"},
        )
        for t in dump["tables"]
        for c in t.get("generate_columns", [])
    }
    assert fingerprints == IDENTITY[key]["fingerprints"]


def test_identity_fixture_covers_every_type() -> None:
    seen = {
        c["type"]
        for t in IDENTITY["synthetic/all-types"]["raw"]["tables"]
        for c in t["generate_columns"]
    }
    assert seen == set(GENERATE_TYPES)


# AT-4 ---------------------------------------------------------------------------------


def test_duplicate_generate_column_names_are_refused_at_the_second() -> None:
    errs = _errors(_col("sequence"), _col("sequence"))
    assert [(e["type"], tuple(e["loc"])) for e in errs] == [
        (DUPLICATE, ("tables", 0, "generate_columns", 1, "name"))
    ]


def test_every_later_duplicate_is_reported() -> None:
    errs = _errors(_col("sequence"), _col("sequence"), _col("sequence"))
    assert [e["loc"][-2] for e in errs] == [1, 2]


def test_same_name_in_two_tables_is_fine() -> None:
    raw = _config(_col("sequence"))
    other = copy.deepcopy(raw["tables"][0])
    other["name"] = "u"
    raw["tables"].append(other)
    raw["targets"]["u"] = {"type": "file", "format": "csv", "path": "/o/u.csv"}
    PipelineConfig.model_validate(raw)


# AT-5 ---------------------------------------------------------------------------------


def _column_schema() -> dict[str, Any]:
    schema = PipelineConfig.model_json_schema()
    return schema["$defs"]["GenerateColumnConfig"]


def _schema_accepts_column(col: dict[str, Any]) -> bool:
    """The closed-key-set part of the column schema: required keys and `additionalProperties`.

    `jsonschema` is not a dev dependency (adding one moves the lock), so this evaluates only the
    `allOf` of `if type == X then {properties, required, additionalProperties: false}` branches
    the registry generates, which is the part AT-5 is about.
    """
    schema = _column_schema()
    if not set(schema.get("required", ())) <= set(col):
        return False
    for branch in schema["allOf"]:
        if branch["if"]["properties"]["type"]["const"] != col["type"]:
            continue
        then = branch["then"]
        if not set(then["required"]) <= set(col):
            return False
        if then["additionalProperties"] is False and not set(col) <= set(then["properties"]):
            return False
    return True


def _schema_valid(instance: dict[str, Any]) -> bool:
    return all(
        _schema_accepts_column(c) for t in instance["tables"] for c in t.get("generate_columns", [])
    )


def test_table_schema_points_at_the_column_schema() -> None:
    schema = PipelineConfig.model_json_schema()
    items = schema["$defs"]["TableConfig"]["properties"]["generate_columns"]["items"]
    assert items == {"$ref": "#/$defs/GenerateColumnConfig"}


def test_schema_has_one_branch_per_generate_type() -> None:
    types = [b["if"]["properties"]["type"]["const"] for b in _column_schema()["allOf"]]
    assert sorted(types) == sorted(GENERATE_TYPES)


@pytest.mark.parametrize("key", sorted(IDENTITY))
def test_schema_accepts_shipped_generate_configs(key: str) -> None:
    assert _schema_valid(_raw_for(key))


@pytest.mark.parametrize("type_", sorted(MINIMAL))
def test_schema_rejects_an_unknown_key_for_each_type(type_: str) -> None:
    assert _schema_valid(_config(_col(type_)))
    assert not _schema_valid(_config(_col(type_, weigths=1)))


def test_schema_keeps_common_options_and_declared_fields() -> None:
    col = _col("sequence", null_probability=0.5, determinism="fresh")
    col["pooled"] = None
    assert _schema_valid(_config(col))


def _branch(type_: str) -> dict[str, Any]:
    (branch,) = [
        b for b in _column_schema()["allOf"] if b["if"]["properties"]["type"]["const"] == type_
    ]
    return branch["then"]


@pytest.mark.parametrize("type_", sorted(GENERATE_OPTIONS))
def test_schema_takes_a_pooled_flag_on_faker_only(type_: str) -> None:
    pooled = _branch(type_)["properties"]["pooled"]
    assert pooled == ({} if type_ == "faker" else {"type": "null"})


def test_schema_requires_the_keys_checked_at_plan_compile() -> None:
    for type_, spec in GENERATE_OPTIONS.items():
        assert _branch(type_)["required"] == [*spec.required, *spec.plan_required]
    assert {"op", "column"} <= set(_branch("derived_aggregate")["required"])


# ---- generate inputs: the one descriptor per option and its resolver ----------------------------

REF_KINDS = {
    "column",
    "column_list",
    "expression",
    "column_via_reference",
    "column_in_named_table",
}
REF_PHASES = {"inline", "post_fill"}


def _edges(table: dict[str, Any], column: dict[str, Any]) -> list[tuple[Any, ...]]:
    from decoy_engine.config import iter_generate_input_refs

    return [
        (e.option, e.source_table, e.source_column, e.must_be_earlier, e.phase)
        for e in iter_generate_input_refs(table, column)
    ]


def _kids(*columns: dict[str, Any]) -> dict[str, Any]:
    fk = {"name": "pid", "type": "reference", "reference_table": "p", "reference_column": "id"}
    return {"name": "t", "row_count": 5, "generate_columns": [fk, *columns]}


def test_every_input_descriptor_is_from_the_closed_vocabulary() -> None:
    from decoy_engine.config._generate_options import OptionSpec

    assert (OptionSpec().ref_kind, OptionSpec().ref_context_key, OptionSpec().ref_phase) == (
        None,
        None,
        "inline",
    )
    for type_, spec in GENERATE_OPTIONS.items():
        for key, option in spec.keys.items():
            where = f"{type_}.{key}"
            assert option.ref_kind is None or option.ref_kind in REF_KINDS, where
            assert option.ref_phase in REF_PHASES, where
            if option.ref_context_key is not None:
                assert option.ref_kind in {"column_via_reference", "column_in_named_table"}, where
                assert option.ref_context_key in spec.keys, where
            if option.ref_kind in {"column_via_reference", "column_in_named_table"}:
                assert option.ref_context_key is not None, where


def test_the_registered_inputs_are_exactly_these() -> None:
    registered = {
        f"{type_}.{key}": (option.ref_kind, option.ref_context_key, option.ref_phase)
        for type_, spec in GENERATE_OPTIONS.items()
        for key, option in spec.keys.items()
        if option.ref_kind is not None
    }
    assert registered == {
        "categorical.condition_on": ("column", None, "inline"),
        "statistical.condition_on": ("column", None, "inline"),
        "derived.expression": ("expression", None, "inline"),
        "derived_aggregate.column": ("column_in_named_table", "from_table", "post_fill"),
        "derived_aggregate.via": ("column_in_named_table", "from_table", "post_fill"),
        "grouped_series.group_by": ("column", None, "inline"),
        "grouped_series.order_by": ("column", None, "inline"),
        "windowed_date.anchor": ("column_via_reference", "anchor_via", "inline"),
        "windowed_date.anchor_via": ("column", None, "inline"),
        "group_key.group_by": ("column", None, "inline"),
        "formula.references": ("column_list", None, "post_fill"),
    }


def test_a_same_table_input_is_an_earlier_column_of_its_own_table() -> None:
    table = _kids()
    column = {"name": "g", "type": "grouped_series", "group_by": "a", "order_by": "b"}
    assert _edges(table, column) == [
        ("group_by", "t", "a", True, "inline"),
        ("order_by", "t", "b", True, "inline"),
    ]
    from decoy_engine.config import iter_generate_input_refs

    edge = iter_generate_input_refs(table, column)[0]
    assert (edge.consumer_table, edge.consumer_column) == ("t", "g")


def test_an_expression_input_is_each_column_it_reads() -> None:
    column = {"name": "d", "type": "derived", "expression": 'concat(a, "b") if c > 1 else a'}
    assert sorted(_edges(_kids(), column)) == [
        ("expression", "t", "a", True, "inline"),
        ("expression", "t", "c", True, "inline"),
    ]
    broken = {"name": "d", "type": "derived", "expression": "a +* ("}
    assert _edges(_kids(), broken) == []


def test_formula_references_are_read_after_the_table_is_filled() -> None:
    column = {"name": "f", "type": "formula", "formula": "a + b", "references": ["a", "b", 3]}
    assert _edges(_kids(), column) == [
        ("references", "t", "a", False, "post_fill"),
        ("references", "t", "b", False, "post_fill"),
    ]


def test_an_anchor_without_anchor_via_is_a_same_table_input() -> None:
    column = {"name": "v", "type": "windowed_date", "anchor": "born", "max_days": 3}
    assert _edges(_kids(), column) == [("anchor", "t", "born", True, "inline")]


def test_an_anchor_with_anchor_via_is_a_column_of_the_referenced_table() -> None:
    column = {
        "name": "v",
        "type": "windowed_date",
        "anchor": "born",
        "anchor_via": "pid",
        "max_days": 3,
    }
    assert _edges(_kids(column), column) == [
        ("anchor", "p", "born", False, "inline"),
        ("anchor_via", "t", "pid", True, "inline"),
    ]
    # A same-named sibling changes nothing: the anchor is still the parent's column.
    born = {"name": "born", "type": "sequence", "start": 1}
    assert _edges(_kids(born, column), column)[0] == ("anchor", "p", "born", False, "inline")


@pytest.mark.parametrize(
    "via_column",
    [
        None,
        {"name": "pid", "type": "sequence", "start": 1},
        {"name": "pid", "type": "reference", "reference_table": 7, "reference_column": "id"},
    ],
    ids=["absent", "not-a-reference", "no-table-name"],
)
def test_an_anchor_whose_lookup_column_leads_nowhere_has_no_source_table(via_column: Any) -> None:
    """A missing lookup column is the `anchor_via` edge's problem. One that exists but is not a
    reference to a table gives the anchor an empty source table, which no plan check accepts."""
    column = {
        "name": "v",
        "type": "windowed_date",
        "anchor": "born",
        "anchor_via": "pid",
        "max_days": 3,
    }
    table = {"name": "t", "generate_columns": [*([via_column] if via_column else []), column]}
    via_edge = ("anchor_via", "t", "pid", True, "inline")
    if via_column is None:
        assert _edges(table, column) == [via_edge]
    else:
        assert _edges(table, column) == [("anchor", "", "born", False, "inline"), via_edge]


def test_an_input_in_a_named_table_resolves_to_that_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """The descriptor a per-parent aggregate registers: `column` lives in the table `from` names."""
    from decoy_engine.config import _generate_options as options

    spec = options.TypeOptions(
        {
            "from": options.OptionSpec(),
            "column": options.OptionSpec(
                ref_kind="column_in_named_table", ref_context_key="from", ref_phase="post_fill"
            ),
        }
    )
    monkeypatch.setitem(options.GENERATE_OPTIONS, "probe", spec)  # type: ignore[index]
    column = {"name": "total", "type": "probe", "column": "amount", "from": "lines"}
    assert _edges(_kids(), column) == [("column", "lines", "amount", False, "post_fill")]
    # Without its companion key the option keeps the same-table, earlier, inline rule.
    assert _edges(_kids(), {**column, "from": None}) == [("column", "t", "amount", True, "inline")]
    assert _edges(_kids(), {**column, "from": 5}) == []


def test_a_per_parent_aggregate_reads_its_child_table_after_it_is_filled() -> None:
    total = {"name": "total", "type": "derived_aggregate", "op": "sum", "column": "amount"}
    per_parent = {**total, "from_table": "lines", "via": "oid"}
    assert _edges(_kids(), per_parent) == [
        ("column", "lines", "amount", False, "post_fill"),
        ("via", "lines", "oid", False, "post_fill"),
    ]
    # A same-named column of the aggregate's own table changes nothing.
    amount = {"name": "amount", "type": "sequence", "start": 1}
    assert _edges(_kids(amount), per_parent)[0] == ("column", "lines", "amount", False, "post_fill")
    # Without `from_table` the aggregate reads an earlier column of its own table, as before.
    assert _edges(_kids(), total) == [("column", "t", "amount", True, "inline")]


def _per_parent_config(**options: Any) -> dict[str, Any]:
    """`p` holds the aggregate over `t`, whose `pid` references `p.id`."""
    config = _config(
        {"name": "pid", "type": "reference", "reference_table": "p", "reference_column": "id"},
        {"name": "amount", "type": "categorical", "categories": [1, 2]},
    )
    total = {"name": "total", "type": "derived_aggregate", "op": "sum", "column": "amount"}
    config["tables"][1]["generate_columns"].append({**total, **options})
    return config


def test_from_table_and_via_are_derived_aggregate_options() -> None:
    PipelineConfig.model_validate(_per_parent_config(from_table="t", via="pid"))
    for key, value in (("from_table", 7), ("via", ["pid"])):
        options = {"from_table": "t", "via": "pid", key: value}
        with pytest.raises(ValidationError) as info:
            PipelineConfig.model_validate(_per_parent_config(**options))
        errs = info.value.errors(include_url=False)
        assert [(e["type"], tuple(e["loc"])) for e in errs] == [
            ("generate_option_type", ("tables", 1, "generate_columns", 1, key))
        ]
    for key in ("from_table", "via"):
        errs = _errors(_col("sequence", **{key: "t"}))
        assert [_spot(e) for e in errs] == [("generate_option_unknown", (key,))]


def test_a_column_with_no_registered_input_or_an_unknown_type_has_no_edges() -> None:
    assert _edges(_kids(), {"name": "s", "type": "sequence", "start": 1}) == []
    assert _edges(_kids(), {"name": "x", "type": "not_a_type", "anchor": "a"}) == []
    assert _edges(_kids(), {"name": "v", "type": "windowed_date", "anchor": 5}) == []
    lookup = {"name": "code", "type": "lookup", "codeset": "icd10", "value_column": "pid"}
    assert _edges(_kids(), lookup) == []


def test_the_resolver_is_exported_and_reads_only() -> None:
    from decoy_engine import config as config_api

    assert {"GenerateInputRef", "iter_generate_input_refs"} <= set(config_api.__all__)
    column = {"name": "v", "type": "windowed_date", "anchor": "born", "anchor_via": "pid"}
    table = _kids(column)
    before = copy.deepcopy(table)
    edges = config_api.iter_generate_input_refs(table, column)
    assert isinstance(edges, tuple) and table == before
    assert all(isinstance(e, config_api.GenerateInputRef) for e in edges)
    with pytest.raises(AttributeError):
        edges[0].source_table = "other"  # type: ignore[misc]


def test_anchor_via_is_a_windowed_date_option() -> None:
    column = _col("windowed_date", anchor_via="pid")
    fk = {"name": "pid", "type": "reference", "reference_table": "p", "reference_column": "id"}
    born = {"name": "a", "type": "categorical", "categories": ["2001-02-03"]}
    config = _config(fk, column)
    config["tables"][1]["generate_columns"].append(born)
    PipelineConfig.model_validate(config)
    errs = _errors(fk, _col("windowed_date", anchor_via=7))
    assert [(e["type"], tuple(e["loc"])) for e in errs] == [
        ("generate_option_type", ("tables", 0, "generate_columns", 1, "anchor_via"))
    ]


# GEN-INHERIT E2, AT-2: `condition_on` and `by` on a categorical column ----------------------

MISSING = "missing"
UNIQUE_CONFLICT = str(reasons.config.GENERATE_UNIQUE_CONFLICT)


def _conditional(**extra: Any) -> dict[str, Any]:
    by = {"north": {"weights": [2, 1]}, "south": {"categories": ["x", "y", "z"]}}
    return {**_col("categorical", condition_on="region", by=by), **extra}


def _region() -> dict[str, Any]:
    return {"name": "region", "type": "categorical", "categories": ["north", "south"]}


def _spots(col: dict[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    return [_spot(e) for e in _errors(_region(), col)]


def test_condition_on_and_by_are_categorical_options() -> None:
    keys = GENERATE_OPTIONS["categorical"].keys
    assert {"condition_on", "by"} <= set(keys)
    assert keys["condition_on"].ref_kind == "column" and keys["by"].ref_kind is None
    PipelineConfig.model_validate(_config(_region(), _conditional()))


@pytest.mark.parametrize(("given", "absent"), [("by", "condition_on"), ("condition_on", "by")])
def test_by_and_condition_on_are_given_together(given: str, absent: str) -> None:
    col = _conditional()
    del col[absent]
    assert _spots(col) == [(MISSING, (absent,))]
    assert [_spot(e)[1] for e in _errors(_region(), {**col, absent: None})] == [(absent,)]


def test_by_must_be_a_mapping() -> None:
    assert _spots(_conditional(by=["north"])) == [(TYPE, ("by",))]
    assert _spots(_conditional(by=None)) == [(TYPE, ("by",))]


@pytest.mark.parametrize("key", [7731, 1.5, True, False, None])
def test_a_key_that_is_not_text_is_refused_at_its_position_only(key: Any) -> None:
    by = {"north": {"weights": [1, 1]}, key: {"weights": [1, 1]}}
    errs = _errors(_region(), _conditional(by=by))
    assert [_spot(e) for e in errs] == [(TYPE, ("by", 1))]
    for text in (errs[0]["msg"], repr(errs)):
        assert "7731" not in text and "1.5" not in text


def test_a_bool_key_message_says_to_quote_it_as_its_text() -> None:
    errs = _errors(_region(), _conditional(by={True: {"weights": [1, 1]}}))
    assert '"True"' in errs[0]["msg"] and '"False"' in errs[0]["msg"]
    assert errs[0]["msg"].startswith(reasons.lookup(TYPE)[0].summary)


@pytest.mark.parametrize(
    ("entry", "where"),
    [
        ({"weights": [1, 1], "bogus": 1}, (UNKNOWN_KEY, ("by", 0, "bogus"))),
        ({"condition_on": "x", "weights": [1, 1]}, (UNKNOWN_KEY, ("by", 0, "condition_on"))),
        ({}, (TYPE, ("by", 0))),
        ("north", (TYPE, ("by", 0))),
        (None, (TYPE, ("by", 0))),
        ({"categories": "xy"}, (TYPE, ("by", 0, "categories"))),
        ({"categories": []}, (TYPE, ("by", 0, "categories"))),
        ({"categories": None}, (TYPE, ("by", 0, "categories"))),
        ({"weights": None}, (TYPE, ("by", 0, "weights"))),
        ({"weights": [1]}, (WEIGHTS, ("by", 0, "weights"))),
        ({"weights": [1, -1]}, (WEIGHTS, ("by", 0, "weights", 1))),
        ({"weights": [0, 0]}, (WEIGHTS, ("by", 0, "weights"))),
        ({"weights": [1, True]}, (WEIGHTS, ("by", 0, "weights", 1))),
        ({"categories": ["x", "y", "z"], "weights": [1, 1]}, (WEIGHTS, ("by", 0, "weights"))),
    ],
)
def test_a_bad_entry_is_refused_at_its_nested_location(entry: Any, where: Any) -> None:
    assert _spots(_conditional(by={"north": entry})) == [where]


def test_entry_weights_pair_with_the_entry_categories_else_the_top_level_ones() -> None:
    ok = {
        "north": {"weights": [1, 3]},
        "south": {"categories": ["x", "y", "z"], "weights": [1, 2, 3]},
    }
    PipelineConfig.model_validate(_config(_region(), _conditional(by=ok)))
    assert _spots(_conditional(by={"north": {"weights": [1, 2, 3]}})) == [
        (WEIGHTS, ("by", 0, "weights"))
    ]


def test_a_by_key_is_never_echoed() -> None:
    by = {CANARY: {"weights": [1, 1], "bogus": 1}, "south": {"weights": [CANARY]}}
    errs = _errors(_region(), _conditional(by=by))
    assert {_spot(e) for e in errs} == {
        (UNKNOWN_KEY, ("by", 0, "bogus")),
        (WEIGHTS, ("by", 1, "weights", 0)),
    }
    for text in (repr(errs), *(e["msg"] for e in errs)):
        assert CANARY not in text


def test_unique_with_condition_on_is_a_conflict() -> None:
    col = _conditional(unique=True)
    assert (UNIQUE_CONFLICT, ("condition_on",)) in _spots(col)
