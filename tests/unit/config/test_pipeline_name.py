"""`PipelineConfig.name` and the one pipeline-name rule (PROJECT-LAYOUT E1, AT-2)."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import decoy_engine
from decoy_engine import reasons
from decoy_engine.config import PipelineConfig

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
BASELINE = json.loads((FIXTURES / "f06" / "legacy-compat-baseline.json").read_text())
CANARY = "CANARY-5d1f92"

VALID = ["orders", "a", "My-Pipeline", "x_y.z", "A" * 64, "9lives", "COM0", "LPT0", "CONX", "COM10"]
INVALID = [
    "",
    " orders",
    "orders ",
    "orders\n",
    "a b",
    "-a",
    ".a",
    "_a",
    "a/b",
    "a\\b",
    "a" * 65,
    "orders.",
    "é",
    "CON",
    "con",
    "Con.txt",
    "PRN",
    "AUX.log",
    "NUL",
    "nul.tar.gz",
    "COM1",
    "com9",
    "LPT1",
    "lpt9.txt",
    "COM¹",
    "LPT².txt",
    "COM³",
]


def _raw(**extra: Any) -> dict[str, Any]:
    raw = copy.deepcopy(BASELINE["raw_config"])
    raw.update(extra)
    return raw


def _is_valid() -> Any:
    return decoy_engine.is_valid_pipeline_name


@pytest.mark.parametrize("name", VALID)
def test_valid_names_pass(name: str) -> None:
    assert _is_valid()(name) is True
    assert PipelineConfig.model_validate(_raw(name=name)).name == name


@pytest.mark.parametrize("name", INVALID)
def test_invalid_names_fail_in_function_and_model(name: str) -> None:
    assert _is_valid()(name) is False
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(_raw(name=name))


@pytest.mark.parametrize("value", [True, False, 0, 1, 1.5, ["orders"], {"a": 1}, b"orders"])
def test_non_string_name_is_not_coerced(value: object) -> None:
    assert _is_valid()(value) is False
    with pytest.raises(ValidationError) as info:
        PipelineConfig.model_validate(_raw(name=value))
    assert str(reasons.config.PIPELINE_NAME_INVALID) in {e["type"] for e in info.value.errors()}


def test_none_name_is_the_unset_form() -> None:
    cfg = PipelineConfig.model_validate(_raw(name=None))
    assert cfg.name is None
    assert "name" not in cfg.model_dump()


def test_error_does_not_echo_the_submitted_value() -> None:
    with pytest.raises(ValidationError) as info:
        PipelineConfig.model_validate(_raw(name=f"{CANARY} bad"))
    assert CANARY not in str(info.value)
    # errors() keeps pydantic's own `input` key for every field; type, msg and loc are value-free.
    assert all(CANARY not in f"{e['type']}{e['msg']}{e['loc']}" for e in info.value.errors())
    assert info.value.__context__ is None
    assert info.value.__cause__ is None


def test_reason_is_registered_in_the_config_namespace() -> None:
    assert str(reasons.config.PIPELINE_NAME_INVALID) == "pipeline_name_invalid"
    spec = reasons.find("config", "pipeline_name_invalid")
    assert spec is not None
    assert spec.since == "1.11.0"


def test_dump_without_name_is_byte_identical_to_the_baseline() -> None:
    cfg = PipelineConfig.model_validate(BASELINE["raw_config"])
    dump = json.dumps(cfg.model_dump(), sort_keys=True)
    assert hashlib.sha256(dump.encode()).hexdigest() == BASELINE["config_dump_sha256"]
    assert "name" not in cfg.model_dump()
    assert "name" not in json.loads(cfg.model_dump_json())
    assert "name" not in cfg.model_dump(mode="json")


def test_dump_with_name_carries_it_and_round_trips() -> None:
    cfg = PipelineConfig.model_validate(_raw(name="orders"))
    assert cfg.model_dump()["name"] == "orders"
    assert PipelineConfig.model_validate(cfg.model_dump()).name == "orders"


def test_json_schema_lists_name_as_an_optional_string() -> None:
    schema = PipelineConfig.model_json_schema()
    assert "name" in schema["properties"]
    assert "name" not in schema.get("required", [])
