"""GEN-TYPES E1: AT-1 (every registry names the new types) and AT-2 (one table drives the keys).

AT-2 mutant: dropping one family from `CONDITIONAL_REQUIRED` must change both the missing-key and
the foreign-key answers, which proves one table drives both rules.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.config import PipelineConfig
from decoy_engine.config import _generate_option_catalog as catalog
from decoy_engine.config import _generate_options as opts
from decoy_engine.config._tables import GENERATE_TYPES
from decoy_engine.execution.native import _determinism_protocol as protocol
from decoy_engine.execution.native._capabilities import _GEN_ONLY
from decoy_engine.generation._admission import ADMISSION_MATRIX
from decoy_engine.generation._numeric_typing import numeric_plan
from decoy_engine.generation._pinned_compile import PINNABLE_TYPES, REJECTED_TYPES
from tests.unit.generation.kinds._support import (
    DEFERRED_FAMILIES,
    FAMILY_COLS,
    FOREIGN_KEYS,
    cfg,
    dist_col,
    table,
)

NEW_TYPES = ("constant", "distribution")
UNKNOWN = str(reasons.config.GENERATE_OPTION_UNKNOWN)
TYPE = str(reasons.config.GENERATE_OPTION_TYPE)
RANGE = str(reasons.config.GENERATE_OPTION_RANGE)
VALUE = str(reasons.config.GENERATE_OPTION_VALUE_UNKNOWN)

ALL_FAMILY_KEYS = {k for cols in FAMILY_COLS.values() for k in cols if k != "family"} | set(
    FOREIGN_KEYS
)


def _problems(family: str | None, **extra: Any) -> list[tuple[tuple[Any, ...], str]]:
    given = {} if family is None else {"family": family}
    return opts.option_problems("distribution", {**given, **extra})


def _valid_extras(family: str) -> dict[str, Any]:
    return {k: v for k, v in FAMILY_COLS[family].items() if k != "family"}


# AT-1 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", NEW_TYPES)
def test_every_registry_names_the_new_types(kind: str) -> None:
    assert kind in GENERATE_TYPES
    assert kind in opts.GENERATE_OPTIONS
    assert kind in catalog.generate_option_catalog()
    assert kind in ADMISSION_MATRIX
    assert kind in PINNABLE_TYPES or kind in REJECTED_TYPES
    # A type the input registry did not know would keep every numeric column of its table text.
    counter = {"name": "n", "type": "sequence", "start": 1}
    table = {
        "name": "t",
        "row_count": 2,
        "generate_columns": [counter, {"name": "c", "type": kind}],
    }
    assert ("t", "n") in numeric_plan({"tables": [table]})
    assert kind in protocol.GEN_KIND_TO_SITE
    assert kind in _GEN_ONLY


def test_draw_sites_follow_the_plan() -> None:
    assert protocol.GEN_KIND_TO_SITE["constant"] == protocol.DETERMINISTIC_NO_DRAW
    assert protocol.GEN_KIND_TO_SITE["distribution"] == "gen.distribution"


def test_both_new_types_are_streamed_and_pinnable() -> None:
    for kind in NEW_TYPES:
        assert ADMISSION_MATRIX[kind][0] is True
        assert kind in PINNABLE_TYPES


def test_capability_matrix_entry_is_the_real_type() -> None:
    root = Path(__file__).resolve().parents[4]
    script = (root / "scripts" / "gen_capability_matrix.py").read_text()
    assert "legacy entry, not a streamed-generation promise" not in script
    assert "`constant`" in (root / "docs" / "capability-matrix.md").read_text()


# AT-2 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("family", sorted(FAMILY_COLS))
def test_a_family_accepts_exactly_its_keys(family: str) -> None:
    assert _problems(family, **_valid_extras(family)) == []
    assert _problems(family, **_valid_extras(family), min=0, max=200) == []
    own = set(_valid_extras(family))
    for foreign in sorted(ALL_FAMILY_KEYS - own):
        got = _problems(family, **_valid_extras(family), **{foreign: 1})
        assert got == [((foreign,), UNKNOWN)], (family, foreign)


@pytest.mark.parametrize("family", sorted(FAMILY_COLS))
def test_a_missing_family_key_is_missing(family: str) -> None:
    for key in _valid_extras(family):
        extras = {k: v for k, v in _valid_extras(family).items() if k != key}
        assert _problems(family, **extras) == [((key,), "missing")], (family, key)


def test_family_is_required_and_a_bad_family_is_a_value_error() -> None:
    assert _problems(None) == [(("family",), "missing")]
    assert _problems("weibull") == [(("family",), VALUE)]
    # Deferred to E1b (numpy's libm calls are not bit-portable across arm64 and x86).
    for name in DEFERRED_FAMILIES:
        assert _problems(name) == [(("family",), VALUE)]
    assert _problems("uniform ", low=0, high=1) == [(("family",), VALUE)]
    assert _problems(3) == [(("family",), TYPE)]  # type: ignore[arg-type]


BAD_RANGE: list[tuple[str, dict[str, Any], str]] = [
    ("uniform", {"low": 3, "high": 3}, RANGE),
    ("uniform", {"low": -1e308, "high": 1e308}, RANGE),
    ("uniform", {"low": 5, "high": 3}, RANGE),
    ("uniform", {"low": 0, "high": 1, "min": "a"}, TYPE),
    # Past 2**53 float64 rounds an int, so the draw could leave the configured bounds.
    ("uniform", {"low": 2**53 + 1, "high": 2**53 + 2}, RANGE),
    ("uniform", {"low": 0, "high": 2**53 + 1}, RANGE),
    ("uniform", {"low": 0, "high": 1, "min": 2**53 + 1}, RANGE),
]


@pytest.mark.parametrize(("family", "extras", "code"), BAD_RANGE)
def test_range_and_type_rules(family: str, extras: dict[str, Any], code: str) -> None:
    got = _problems(family, **extras)
    assert got and {c for _, c in got} == {code}, got


def test_boundary_values_pass() -> None:
    assert _problems("uniform", low=-5, high=5, min=2, max=2) == []
    assert _problems("uniform", low=-1, high=1.0, min=0, max=0.5) == []
    assert _problems("uniform", low=0, high=2**53) == []


def test_the_reference_weighted_rule_keeps_its_code_and_location() -> None:
    got = opts.option_problems("reference", {"reference_table": "p", "reference_column": "id"})
    assert got == []
    got = opts.option_problems(
        "reference",
        {"reference_table": "p", "reference_column": "id", "distribution": "weighted"},
    )
    assert got == [(("weights",), "missing")]
    ok = opts.option_problems(
        "reference",
        {
            "reference_table": "p",
            "reference_column": "id",
            "distribution": "weighted",
            "weights": [1, 2],
        },
    )
    assert ok == []


def test_the_catalog_shows_the_family_requirements() -> None:
    entry = catalog.generate_option_catalog()["distribution"]
    assert entry.required == ("family",)
    assert entry.required_when["family"] == {"uniform": ("low", "high")}
    assert set(entry.optional) == {"low", "high", "min", "max", "output_type"}
    ref = catalog.generate_option_catalog()["reference"]
    assert ref.required_when == {"distribution": {"weighted": ("weights",)}}
    assert catalog.generate_option_catalog()["sequence"].required_when == {}


def test_the_mutant_one_table_drives_missing_and_foreign_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_case = ("uniform", {"low": 0})
    foreign_case = ("uniform", {"low": 0, "high": 1, "mean": 3})
    assert _problems(missing_case[0], **missing_case[1]) == [(("high",), "missing")]
    assert _problems(foreign_case[0], **foreign_case[1]) == [(("mean",), UNKNOWN)]
    mutated = {
        t: {sel: {v: k for v, k in by.items() if v != "uniform"} for sel, by in rules.items()}
        for t, rules in opts.CONDITIONAL_REQUIRED.items()
    }
    monkeypatch.setattr(opts, "CONDITIONAL_REQUIRED", mutated)
    assert _problems(missing_case[0], **missing_case[1]) != [(("high",), "missing")]
    assert _problems(foreign_case[0], **foreign_case[1]) != [(("mean",), UNKNOWN)]


# the same rules through the config model and the JSON schema ----------------------------


def _config(col: dict[str, Any]) -> dict[str, Any]:
    return cfg([table("t", 5, [col])])


@pytest.mark.parametrize("family", sorted(FAMILY_COLS))
def test_pipeline_validation_accepts_each_family(family: str) -> None:
    PipelineConfig.model_validate(_config(dist_col(family)))
    PipelineConfig.model_validate(_config(dist_col(family, min=0, max=500)))


def test_pipeline_validation_refuses_a_foreign_key_at_its_name() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as info:
        PipelineConfig.model_validate(_config(dist_col("uniform", mean=2)))
    (err,) = info.value.errors(include_url=False)
    assert err["type"] == UNKNOWN
    assert tuple(err["loc"]) == ("tables", 0, "generate_columns", 0, "mean")


def test_constant_values() -> None:
    from pydantic import ValidationError

    for good in ("US", 0, 3, 2.5, True, False, ""):
        PipelineConfig.model_validate(_config({"name": "c", "type": "constant", "value": good}))
    for bad in (None, [1], {"a": 1}, math.inf, math.nan):
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(_config({"name": "c", "type": "constant", "value": bad}))
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(_config({"name": "c", "type": "constant"}))


def _column_schema() -> dict[str, Any]:
    return PipelineConfig.model_json_schema()["$defs"]["GenerateColumnConfig"]


def _then_accepts(then: dict[str, Any], col: dict[str, Any]) -> bool:
    if not set(then.get("required", ())) <= set(col):
        return False
    if then.get("additionalProperties") is False and not set(col) <= set(then["properties"]):
        return False
    banned = then.get("not", {}).get("anyOf", [])
    if any(set(b["required"]) <= set(col) for b in banned):
        return False
    for sub in then.get("allOf", []):
        cond = sub["if"]
        hit = all(col.get(k) == v["const"] for k, v in cond["properties"].items()) and set(
            cond["required"]
        ) <= set(col)
        if hit and not _then_accepts(sub["then"], col):
            return False
    return True


def _schema_accepts(col: dict[str, Any]) -> bool:
    for branch in _column_schema()["allOf"]:
        if branch["if"]["properties"]["type"]["const"] == col["type"]:
            return _then_accepts(branch["then"], col)
    raise AssertionError("no branch for the type")


@pytest.mark.parametrize("family", sorted(FAMILY_COLS))
def test_schema_and_options_refuse_the_same_foreign_key(family: str) -> None:
    good = dist_col(family, name="c")
    assert _schema_accepts(good)
    for foreign in sorted(ALL_FAMILY_KEYS - set(_valid_extras(family))):
        col = {**good, foreign: 1}
        in_options = bool(
            _problems(
                family, **{k: v for k, v in col.items() if k not in ("name", "type", "family")}
            )
        )
        assert in_options is True
        assert _schema_accepts(col) is False, (family, foreign)
    for key in _valid_extras(family):
        assert _schema_accepts({k: v for k, v in good.items() if k != key}) is False


def test_schema_takes_min_and_max_on_every_family() -> None:
    for family in FAMILY_COLS:
        assert _schema_accepts(dist_col(family, name="c", min=0, max=1))


def test_the_family_tables_agree() -> None:
    """The config table, the draw builders and the fixtures name the same families."""
    from decoy_engine.generation.kinds import _parametric

    table = set(opts.CONDITIONAL_REQUIRED["distribution"]["family"])
    assert table == set(_parametric._FAMILIES) == set(FAMILY_COLS) == {"uniform"}


# Compile-time refusal ---------------------------------------------------------------------
# The validator, `compile_plan` and `run_config_only_checks` share `generate_family_problems`.


def _raw(family: str) -> dict[str, Any]:
    """A config the model validator cannot see (it never went through `PipelineConfig`)."""
    return cfg([table("t", 5, [{"name": "x", "type": "distribution", "family": family}])])


@pytest.mark.parametrize("family", DEFERRED_FAMILIES)
def test_compile_plan_refuses_each_deferred_family(family: str) -> None:
    from decoy_engine.errors import PipelineValidationError
    from decoy_engine.plan import compile_plan
    from tests.unit._dps_helpers import empty_profile

    with pytest.raises(PipelineValidationError) as info:
        compile_plan(_raw(family), empty_profile(), decoy_engine_version="test")
    err = info.value
    assert err.code == VALUE
    assert err.path == "tables.t.generate_columns.x.family"
    assert err.__cause__ is None
    assert err.__context__ is None
    assert family not in str(err)


@pytest.mark.parametrize("family", DEFERRED_FAMILIES)
def test_config_only_checks_and_the_validator_refuse_the_same_family(family: str) -> None:
    from pydantic import ValidationError

    from decoy_engine.errors import PipelineValidationError
    from decoy_engine.plan._compile import run_config_only_checks

    with pytest.raises(PipelineValidationError) as info:
        run_config_only_checks(_raw(family))
    assert info.value.code == VALUE
    assert info.value.__cause__ is None
    with pytest.raises(ValidationError) as verr:
        PipelineConfig.model_validate(_raw(family))
    assert {e["type"] for e in verr.value.errors(include_url=False)} == {VALUE}
    assert opts.generate_family_problems(_raw(family)) == [
        ("tables.t.generate_columns.x.family", VALUE)
    ]
