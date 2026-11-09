"""Config error text is value-free: structural names shown, data values never.

Owner decision (Q1): names (config keys, source, table and column names, file names, patterns)
are structure and appear in ``location``; data values, literals taken from data and secrets do not
appear anywhere a caller can read: ``text``, ``code``, ``str()``, ``repr()``, ``args`` or the
exception chain. Every case plants ``VALUE`` where a value is read, and checks the full surface.
"""

from __future__ import annotations

import copy
import inspect
import traceback
import typing
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from pydantic_core import PydanticCustomError

import decoy_engine
from decoy_engine import (
    ConfigError,
    ConfigRuleError,
    PipelineValidationError,
    parse_pipeline_config,
    validate_config,
)
from decoy_engine import reasons as _reasons
from decoy_engine.config import PipelineConfigError, override_sources
from decoy_engine.safe_output import validation_problems, yaml_error_text

VALUE = "ZZ_DATA_VALUE_91837"
KEY = "zz_marker_key"


def _config() -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": {
            "customers_prod": {
                "type": "file",
                "format": "csv",
                "path": "in.csv",
                "has_header": True,
            }
        },
        "tables": [{"name": "customers_prod", "columns": [{"name": "a", "strategy": "redact"}]}],
        "targets": {"out": {"type": "file", "format": "csv", "path": "out.csv"}},
    }


def _surface(exc: BaseException) -> str:
    parts = [str(exc), repr(exc), repr(exc.args), "".join(traceback.format_exception(exc))]
    for p in getattr(exc, "problems", ()):
        parts += [p.code, p.text]
    return "\n".join(parts)


def _chain_is_empty(exc: BaseException) -> bool:
    return exc.__cause__ is None and exc.__context__ is None


def _problems(bad: dict[str, Any]) -> PipelineConfigError:
    with pytest.raises(PipelineConfigError) as info:
        parse_pipeline_config(bad)
    return info.value


def test_location_keeps_names_and_drops_the_bad_value() -> None:  # AT-A1
    bad = _config()
    bad["sources"]["customers_prod"]["path"] = {VALUE: [VALUE]}
    err = _problems(bad)
    assert any(p.location == "sources.customers_prod.file.path" for p in err.problems)
    assert VALUE not in _surface(err)


def test_extra_keys_are_named_but_their_values_are_not() -> None:  # AT-A2
    bad = _config()
    bad[KEY] = VALUE
    bad["tables"][0][KEY] = VALUE
    err = _problems(bad)
    found = {(p.location, p.code) for p in err.problems}
    assert (KEY, "extra_forbidden") in found
    assert (f"tables[0].{KEY}", "extra_forbidden") in found
    assert VALUE not in _surface(err)


def test_discriminator_tag_is_not_echoed() -> None:  # AT-A3
    bad = _config()
    bad["sources"]["customers_prod"]["type"] = VALUE
    err = _problems(bad)
    tag = [p for p in err.problems if p.code == "union_tag_invalid"]
    assert tag, "fixture must trigger a union tag error"
    assert "'file'" in tag[0].text and "found using 'type'" in tag[0].text
    assert VALUE not in _surface(err)


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = 0
    fixed: str = ""
    rule: str = ""
    sub: str = ""
    plain: str = ""
    implicit: str = ""
    registered: str = ""
    unregistered: str = ""

    @field_validator("rule")
    @classmethod
    def _rule(cls, v: str) -> str:
        raise ConfigRuleError("column names must be unique")

    @field_validator("sub")
    @classmethod
    def _sub(cls, v: str) -> str:
        class SneakyError(ConfigRuleError):
            pass

        raise SneakyError(f"value {v}")

    @field_validator("plain")
    @classmethod
    def _plain(cls, v: str) -> str:
        raise ValueError(f"bad value {v}")

    @field_validator("implicit")
    @classmethod
    def _implicit(cls, v: str) -> str:
        int(v)
        return v

    @field_validator("registered")
    @classmethod
    def _registered(cls, v: str) -> str:
        raise PydanticCustomError(_REGISTERED_CODE, f"custom text {v}")

    @field_validator("unregistered")
    @classmethod
    def _unregistered(cls, v: str) -> str:
        raise PydanticCustomError("not_a_registered_code", f"custom text {v}")


_REGISTERED_CODE = str(_reasons.plan.DECLARED_CAST_CONTRACT_INVALID)


def _row_problems(**fields: str) -> dict[str, Any]:
    with pytest.raises(ValidationError) as info:
        _Row.model_validate(fields)
    out = {p.location: p for p in validation_problems(info.value)}
    assert VALUE not in "".join(f"{p.code}{p.text}{p.location}" for p in out.values())
    return out


def test_validator_text_is_trusted_only_for_the_exact_rule_error() -> None:  # AT-A4
    got = _row_problems(
        rule=VALUE, sub=VALUE, plain=VALUE, implicit=VALUE, registered=VALUE, unregistered=VALUE
    )
    assert got["rule"].text == "column names must be unique"
    for name in ("sub", "plain", "implicit"):
        assert got[name].code == "value_error" and got[name].text == "Value is not valid."
    summary = _reasons.lookup(_REGISTERED_CODE)[0].summary
    assert got["registered"].code == _REGISTERED_CODE and got["registered"].text == summary
    assert got["unregistered"].code == "value_error"
    assert got["unregistered"].text == "Value is not valid."


@pytest.mark.parametrize("route", ["parse", "validate_config", "override_sources"])
def test_no_route_chains_the_pydantic_error(route: str, tmp_path: Path) -> None:  # AT-A5
    bad = _config()
    bad[KEY] = VALUE
    bad["sources"]["customers_prod"]["type"] = VALUE
    if route == "parse":
        with pytest.raises(PipelineConfigError) as info:
            parse_pipeline_config(bad)
    elif route == "validate_config":
        with pytest.raises(PipelineValidationError) as info:
            validate_config(bad)
    else:
        good = parse_pipeline_config(_config()).model_dump()
        with pytest.raises(PipelineConfigError) as info:
            override_sources(good, sources={"customers_prod": {"type": VALUE, "path": VALUE}})
    assert _chain_is_empty(info.value)
    assert VALUE not in _surface(info.value)


def _public_models() -> list[type[BaseModel]]:
    seen: dict[type[BaseModel], None] = {}

    def walk(tp: object) -> None:
        for arg in typing.get_args(tp):
            walk(arg)
        if inspect.isclass(tp) and issubclass(tp, BaseModel) and tp not in seen:
            seen[tp] = None
            for field in tp.model_fields.values():
                walk(field.annotation)

    for name in decoy_engine.__all__:
        obj = getattr(decoy_engine, name, None)
        if inspect.isclass(obj):
            walk(obj)
    return list(seen)


def _payloads(model: type[BaseModel]) -> list[dict[str, Any]]:
    names = [n for n in model.model_fields]
    shapes: list[Any] = [VALUE, [VALUE], {"k": VALUE}, {VALUE: 1}, 12345]
    out: list[dict[str, Any]] = []
    for shape in shapes:
        payload = {n: copy.deepcopy(shape) for n in names}
        payload[KEY] = VALUE
        out.append(payload)
    out.append({KEY: VALUE})
    return out


def test_public_model_matrix_is_value_free_and_covers_the_core_types() -> None:  # AT-A6
    observed: set[str] = set()
    models = _public_models()
    assert len(models) > 30
    for model in models:
        for payload in _payloads(model):
            try:
                model.model_validate(payload)
            except ValidationError as exc:
                for p in validation_problems(exc):
                    observed.add(p.code)
                    blob = f"{p.code}\n{p.text}"
                    assert VALUE not in blob, (model.__name__, p.code)
                    assert VALUE not in p.location or p.location.split(".")[-1] == VALUE
    # Mapping keys are names, so a planted value used as a key may appear in a location; the
    # value-as-value shapes above must not.
    required = {"missing", "extra_forbidden", "literal_error", "value_error", "string_type"}
    assert required <= observed, required - observed


def test_union_tag_is_covered_by_the_full_config() -> None:
    bad = _config()
    bad["targets"]["out"]["type"] = VALUE
    assert "union_tag_invalid" in {p.code for p in _problems(bad).problems}


@pytest.mark.parametrize(
    "body",
    [
        f"a: [\n  {VALUE}: : {VALUE}\n",
        # PyYAML puts an unknown tag into its message text, so this one fails if the text is shown.
        f"a: !{VALUE} x\n",
    ],
)
def test_yaml_error_reports_position_only(tmp_path: Path, body: str) -> None:  # AT-A7
    path = tmp_path / "bad.yaml"
    path.write_text(body)
    with pytest.raises(ConfigError) as info:
        validate_config(path)
    assert "line" in str(info.value) and "column" in str(info.value)
    assert VALUE not in _surface(info.value)
    assert _chain_is_empty(info.value)


def test_yaml_error_text_reads_only_the_mark() -> None:
    class MarkedError(Exception):
        problem_mark = type("M", (), {"line": 2, "column": 4})()

        def __str__(self) -> str:
            return VALUE

    assert yaml_error_text(MarkedError()) == (
        "could not parse YAML: invalid syntax at line 3, column 5"
    )
    assert VALUE not in yaml_error_text(ValueError(VALUE))
