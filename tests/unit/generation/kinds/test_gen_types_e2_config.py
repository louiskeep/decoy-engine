"""GEN-TYPES E2: the `output_type` option in the registry, the catalog, the schema and plan compile.

Mutant named in the plan (AT-17): drop `output_type` from `_EXCLUDED_FROM_FINGERPRINT`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from decoy_engine import reasons
from decoy_engine.config import PipelineConfig
from decoy_engine.config import _generate_option_catalog as catalog
from decoy_engine.config import _generate_options as opts
from decoy_engine.errors import PipelineValidationError
from decoy_engine.generators.derivation import GenDeriveContext, with_table
from decoy_engine.plan import compile_plan
from tests.unit._dps_helpers import empty_profile
from tests.unit.generation.kinds._support import cfg, dist_col, table

TYPE = str(reasons.config.GENERATE_OPTION_TYPE)
VALUE = str(reasons.config.GENERATE_OPTION_VALUE_UNKNOWN)
RANGE = str(reasons.config.GENERATE_OPTION_RANGE)
UNKNOWN = str(reasons.config.GENERATE_OPTION_UNKNOWN)
UNSUPPORTED = "generate_output_type_unsupported"

ALL = ("int", "decimal(2)", "date", "bool")
NUMBERS = ("int", "decimal(2)")

# type -> (minimal valid extras, output types it admits). `lookup` joins in E3.
ACCEPTS: dict[str, tuple[dict[str, Any], tuple[str, ...]]] = {
    "constant": ({"value": 1}, ALL),
    "categorical": ({"categories": ["a", "b"]}, ALL),
    "derived": ({"expression": "1"}, ALL),
    "formula": ({"formula": "1"}, ALL),
    "faker": ({"faker_type": "word"}, ALL),
    "distribution": ({"family": "uniform", "low": 0, "high": 1}, NUMBERS),
    "sequence": ({"start": 1}, NUMBERS),
    "grouped_series": ({"group_by": "g", "order_by": "o"}, NUMBERS),
    "derived_aggregate": ({"op": "sum", "column": "c"}, NUMBERS),
    "windowed_date": ({"anchor": "d", "max_days": 3}, ("date",)),
}
# Types that cannot carry the key at all: it is an unknown option there.
NOT_ACCEPTED: dict[str, dict[str, Any]] = {
    "reference": {"reference_table": "p", "reference_column": "id"},
    "statistical": {"snapshot_file": "s.json"},
    "group_key": {"group_by": "g"},
}

CELLS = [(t, o) for t, (_, kinds) in ACCEPTS.items() for o in ALL if o in kinds]
REFUSED_CELLS = [(t, o) for t, (_, kinds) in ACCEPTS.items() for o in ALL if o not in kinds]


@pytest.mark.parametrize(("kind", "output"), CELLS)
def test_an_allowed_type_and_output_type_pair_is_valid(kind: str, output: str) -> None:
    extras = {**ACCEPTS[kind][0], "output_type": output}
    assert opts.option_problems(kind, extras) == []


@pytest.mark.parametrize(("kind", "output"), REFUSED_CELLS)
def test_a_known_output_type_the_type_cannot_carry_is_unsupported(kind: str, output: str) -> None:
    extras = {**ACCEPTS[kind][0], "output_type": output}
    assert opts.option_problems(kind, extras) == [(("output_type",), UNSUPPORTED)]


@pytest.mark.parametrize("kind", sorted(NOT_ACCEPTED))
def test_reference_statistical_and_group_key_do_not_know_the_key(kind: str) -> None:
    extras = {**NOT_ACCEPTED[kind], "output_type": "int"}
    assert opts.option_problems(kind, extras) == [(("output_type",), UNKNOWN)]


@pytest.mark.parametrize("extra", [{"prefix": "A"}, {"suffix": "Z"}, {"pad_length": 5}])
@pytest.mark.parametrize("output", NUMBERS)
def test_sequence_with_text_decoration_cannot_carry_a_number_type(
    extra: dict[str, Any], output: str
) -> None:
    got = opts.option_problems("sequence", {"start": 1, **extra, "output_type": output})
    assert got == [(("output_type",), UNSUPPORTED)]


@pytest.mark.parametrize(
    "bad",
    [
        "float",
        "decimal",
        "decimal()",
        "decimal(x)",
        "Int",
        "int ",
        "",
        "decimal(-1)",
        "decimal(2.5)",
        "decimal(02x)",
    ],
)
def test_an_unparseable_value_is_a_value_error(bad: str) -> None:
    got = opts.option_problems("constant", {"value": 1, "output_type": bad})
    assert got == [(("output_type",), VALUE)]


@pytest.mark.parametrize("bad", [5, True, None, ["int"], {"a": 1}])
def test_a_non_text_value_is_a_type_error(bad: Any) -> None:
    got = opts.option_problems("constant", {"value": 1, "output_type": bad})
    assert got == [(("output_type",), TYPE)]


def test_decimal_scale_bounds() -> None:
    ok = [
        opts.option_problems("constant", {"value": 1, "output_type": f"decimal({n})"})
        for n in (0, 18)
    ]
    assert ok == [[], []]
    got = opts.option_problems("constant", {"value": 1, "output_type": "decimal(19)"})
    assert got == [(("output_type",), RANGE)]


def test_the_refusal_names_no_submitted_value() -> None:
    with pytest.raises(ValidationError) as err:
        PipelineConfig.model_validate(
            cfg(
                [
                    table(
                        "t",
                        3,
                        [
                            {
                                "name": "x",
                                "type": "constant",
                                "value": 1,
                                "output_type": "SENTINEL_9",
                            }
                        ],
                    )
                ]
            )
        )
    assert "SENTINEL_9" not in str(err.value)
    assert {e["type"] for e in err.value.errors(include_url=False)} == {VALUE}


def test_pipeline_validation_accepts_and_refuses_with_the_new_code() -> None:
    good = cfg(
        [
            table(
                "t", 3, [{"name": "x", "type": "constant", "value": 1, "output_type": "decimal(2)"}]
            )
        ]
    )
    PipelineConfig.model_validate(good)
    bad = cfg(
        [
            table(
                "t",
                3,
                [
                    {
                        "name": "x",
                        "type": "windowed_date",
                        "anchor": "d",
                        "max_days": 3,
                        "output_type": "int",
                    }
                ],
            )
        ]
    )
    with pytest.raises(ValidationError) as err:
        PipelineConfig.model_validate(bad)
    assert {e["type"] for e in err.value.errors(include_url=False)} == {UNSUPPORTED}


def test_the_new_codes_are_registered_with_value_free_text() -> None:
    cfg_spec = reasons.find("config", UNSUPPORTED)
    run_spec = reasons.find("runtime", "output_type_cast_failed")
    assert cfg_spec is not None and run_spec is not None
    for spec in (cfg_spec, run_spec):
        assert "{" not in spec.summary and "%" not in spec.summary
    assert reasons.config.GENERATE_OUTPUT_TYPE_UNSUPPORTED == UNSUPPORTED
    assert reasons.runtime.OUTPUT_TYPE_CAST_FAILED == "output_type_cast_failed"


# The catalog and the JSON schema read the one registry ---------------------------------------


@pytest.mark.parametrize("kind", sorted(ACCEPTS))
def test_the_catalog_lists_output_type_as_optional_per_type_not_common(kind: str) -> None:
    entry = catalog.generate_option_catalog()[kind]
    assert "output_type" in entry.optional
    assert "output_type" not in entry.common
    assert "output_type" not in opts.COMMON_OPTIONS


@pytest.mark.parametrize("kind", sorted(NOT_ACCEPTED))
def test_the_catalog_omits_output_type_where_it_is_unknown(kind: str) -> None:
    assert "output_type" not in catalog.generate_option_catalog()[kind].optional


def test_the_json_schema_closes_the_key_set_with_output_type() -> None:
    schema: dict[str, Any] = {}
    catalog.closed_key_schema(schema)
    branches = {b["if"]["properties"]["type"]["const"]: b["then"] for b in schema["allOf"]}
    for kind in ACCEPTS:
        assert "output_type" in branches[kind]["properties"]
    for kind in NOT_ACCEPTED:
        assert "output_type" not in branches[kind]["properties"]


# Fingerprint (AT-17, seed half) --------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(ACCEPTS))
def test_output_type_does_not_move_the_column_seed(kind: str) -> None:
    col = {"name": "x", "type": kind, **ACCEPTS[kind][0]}
    bare = GenDeriveContext.for_column(
        derive_key=None, column_config=with_table(col, "t"), fallback_seed=7
    )
    typed = GenDeriveContext.for_column(
        derive_key=None,
        column_config=with_table({**col, "output_type": ACCEPTS[kind][1][0]}, "t"),
        fallback_seed=7,
    )
    assert bare.base_int("np") == typed.base_int("np")
    assert bare.base_int("dist") == typed.base_int("dist")


# Fixed-width refusal at plan compile ---------------------------------------------------------


def _fixed_width_config(output: str | None) -> dict[str, Any]:
    col: dict[str, Any] = {"name": "x", "type": "distribution", **dist_col("uniform")}
    if output is not None:
        col["output_type"] = output
    config = cfg([table("t", 5, [col])])
    config["targets"] = {
        "t": {
            "type": "file",
            "format": "fixed_width",
            "path": "o.txt",
            "layout": {"columns": [{"name": "x", "start": 0, "width": 12, "type": "str"}]},
        }
    }
    return config


@pytest.mark.parametrize("output", ["decimal(2)", "date", "bool"])
def test_a_fixed_width_target_refuses_types_its_writer_cannot_print(output: str) -> None:
    config = _fixed_width_config(output)
    config["tables"][0]["generate_columns"][0] = {
        "name": "x",
        "type": "constant",
        "value": 1,
        "output_type": output,
    }
    with pytest.raises(PipelineValidationError) as info:
        compile_plan(config, empty_profile(), decoy_engine_version="test")
    assert info.value.code == UNSUPPORTED
    assert info.value.path == "targets.t.layout.columns.x"
    assert info.value.__cause__ is None


@pytest.mark.parametrize("output", [None, "int"])
def test_a_fixed_width_target_accepts_int_and_untyped_columns(output: str | None) -> None:
    compile_plan(_fixed_width_config(output), empty_profile(), decoy_engine_version="test")


def test_a_csv_target_accepts_every_output_type() -> None:
    for output in ALL:
        config = cfg(
            [table("t", 3, [{"name": "x", "type": "constant", "value": 1, "output_type": output}])]
        )
        compile_plan(config, empty_profile(), decoy_engine_version="test")


def test_config_only_checks_refuse_the_same_fixed_width_column() -> None:
    from decoy_engine.plan._compile import run_config_only_checks

    config = _fixed_width_config("bool")
    config["tables"][0]["generate_columns"][0] = {
        "name": "x",
        "type": "constant",
        "value": True,
        "output_type": "bool",
    }
    with pytest.raises(PipelineValidationError) as info:
        run_config_only_checks(config)
    assert info.value.code == UNSUPPORTED


def test_the_plan_embeds_the_output_type_in_the_pinned_config() -> None:
    config = cfg(
        [
            table(
                "t", 3, [{"name": "x", "type": "constant", "value": 1, "output_type": "decimal(2)"}]
            )
        ]
    )
    plan = compile_plan(config, empty_profile(), decoy_engine_version="test")
    col = json.loads(plan.generation.config_json)["tables"][0]["generate_columns"][0]
    assert col["output_type"] == "decimal(2)"
