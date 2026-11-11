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
# The frozen F06 config predates the required csv has_header; the old default keeps its read.
LEGACY_RAW: dict[str, Any] = {
    **BASELINE["raw_config"],
    "sources": {"p": {**BASELINE["raw_config"]["sources"]["p"], "has_header": True}},
}
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
    raw = copy.deepcopy(LEGACY_RAW)
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
    with pytest.raises(ValidationError) as info:
        PipelineConfig.model_validate(_raw(name=name))
    assert "pipeline_name_invalid" in {e["type"] for e in info.value.errors()}


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
    assert spec.since == "1.12.0"


def test_dump_without_name_is_byte_identical_to_the_baseline() -> None:
    cfg = PipelineConfig.model_validate(LEGACY_RAW)
    legacy = cfg.model_dump()
    # A csv dump now always carries has_header, so only that field moves the dump sha.
    del legacy["sources"]["p"]["has_header"]
    dump = json.dumps(legacy, sort_keys=True)
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


def test_name_does_not_change_the_plan_hash(tmp_path: Path) -> None:
    """Renaming a pipeline is data binding, not masking semantics: the plan hash must not move."""
    from decoy_engine.plan._compile import compile_plan
    from decoy_engine.profile._source import profile_source

    (tmp_path / "p.csv").write_text("id,age,note\n1,42,a\n2,7,b\n3,13,c\n")

    def _hash(**extra: Any) -> str:
        raw = _raw(**extra)
        raw["sources"]["p"]["path"] = str(tmp_path / "p.csv")
        cfg = PipelineConfig.model_validate(raw).model_dump()
        plan = compile_plan(cfg, profile_source(cfg, seed=7), decoy_engine_version="t")
        return str(plan.pipeline_config_hash)

    assert _hash() == _hash(name="orders") == _hash(name="sales")
