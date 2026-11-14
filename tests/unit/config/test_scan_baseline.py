"""SCAN-MULTI S3-engine.1: `scan_baseline` and the reserved namespace placeholder.

T3-config-field: the field accepts only the `scan-set/v1` tag and a config without it is
unchanged. T3-placeholder (validator half): a namespace holding the placeholder prefix is
refused with `config.namespace_placeholder_unset` at the field path, any other value passes.
T3-placeholder-error-value-free: the error carries the path and the code, never the value.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from decoy_engine import reasons
from decoy_engine.config import PipelineConfig, RelationshipConfig
from decoy_engine.config._namespaces import NAMESPACE_PLACEHOLDER_PREFIX
from decoy_engine.profiling.scan_set import SCAN_SET_SCHEMA

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
BASELINE = json.loads((FIXTURES / "f06" / "legacy-compat-baseline.json").read_text())
RAW: dict[str, Any] = {
    **BASELINE["raw_config"],
    "sources": {"p": {**BASELINE["raw_config"]["sources"]["p"], "has_header": True}},
}
CANARY = "CANARY-9c3e41"


def _raw(**extra: Any) -> dict[str, Any]:
    raw = copy.deepcopy(RAW)
    raw.update(extra)
    return raw


def _with_column_namespace(value: object) -> dict[str, Any]:
    raw = copy.deepcopy(RAW)
    raw["tables"][0]["columns"][0]["namespace"] = value
    return raw


def _relationship(namespace: object) -> dict[str, Any]:
    return {
        "parent": {"table": "a", "columns": ["id"]},
        "children": [{"table": "b", "columns": ["a_id"]}],
        "orphan_policy": "fail",
        "namespace": namespace,
    }


def test_the_tag_matches_the_scan_set_schema() -> None:
    assert SCAN_SET_SCHEMA == "scan-set/v1"


def test_scan_baseline_round_trips() -> None:
    cfg = PipelineConfig.model_validate(
        _raw(scan_baseline={"path": ".decoy/library/x.json", "schema": SCAN_SET_SCHEMA})
    )
    assert cfg.model_dump()["scan_baseline"] == {
        "path": ".decoy/library/x.json",
        "schema": "scan-set/v1",
    }
    again = PipelineConfig.model_validate(cfg.model_dump())
    assert again.model_dump() == cfg.model_dump()


def test_scan_baseline_absent_leaves_the_dump_unchanged() -> None:
    cfg = PipelineConfig.model_validate(_raw())
    assert cfg.scan_baseline is None
    assert "scan_baseline" not in cfg.model_dump()
    assert "scan_baseline" not in cfg.model_dump(mode="json")


@pytest.mark.parametrize("tag", ["scan-set/v2", "", "scan-set/v1 ", None, 1])
def test_scan_baseline_refuses_another_schema_tag(tag: object) -> None:
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(_raw(scan_baseline={"path": "x.json", "schema": tag}))


@pytest.mark.parametrize(
    "bad",
    [
        {"path": "x.json"},
        {"schema": "scan-set/v1"},
        {"path": "x.json", "schema": "scan-set/v1", "record_id": "r"},
        {"path": 3, "schema": "scan-set/v1"},
        "x.json",
    ],
)
def test_scan_baseline_refuses_other_shapes(bad: object) -> None:
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(_raw(scan_baseline=bad))


def test_the_placeholder_identity_is_registered() -> None:
    spec = reasons.REASONS[("config", "namespace_placeholder_unset")]
    assert str(reasons.config.NAMESPACE_PLACEHOLDER_UNSET) == "namespace_placeholder_unset"
    assert spec.since == reasons.UNRELEASED


@pytest.mark.parametrize("n", [1, 2, 17])
def test_a_column_placeholder_is_refused_at_its_path(n: int) -> None:
    with pytest.raises(ValidationError) as caught:
        PipelineConfig.model_validate(_with_column_namespace(f"decoy-set-namespace-{n}"))
    errors = caught.value.errors()
    assert [e["type"] for e in errors] == ["namespace_placeholder_unset"]
    assert errors[0]["loc"] == ("tables", 0, "columns", 0, "namespace")


def test_the_bare_prefix_is_refused() -> None:
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(_with_column_namespace(NAMESPACE_PLACEHOLDER_PREFIX))


@pytest.mark.parametrize(
    "value", ["cust", "decoy-set-namespace", "Decoy-set-namespace-1", " x", None]
)
def test_other_namespaces_pass(value: object) -> None:
    PipelineConfig.model_validate(_with_column_namespace(value))


def test_a_relationship_placeholder_is_refused_at_its_path() -> None:
    with pytest.raises(ValidationError) as caught:
        RelationshipConfig.model_validate(_relationship("decoy-set-namespace-1"))
    errors = caught.value.errors()
    assert [e["type"] for e in errors] == ["namespace_placeholder_unset"]
    assert errors[0]["loc"] == ("namespace",)
    RelationshipConfig.model_validate(_relationship("cust"))


def test_the_pipeline_path_names_a_relationship_placeholder() -> None:
    with pytest.raises(ValidationError) as caught:
        PipelineConfig.model_validate(_raw(relationships=[_relationship("decoy-set-namespace-2")]))
    locs = [e["loc"] for e in caught.value.errors() if e["type"] == "namespace_placeholder_unset"]
    assert locs == [("relationships", 0, "namespace")]


def test_the_error_is_value_free() -> None:
    """M3-echo: the text names the code and the path, never the submitted value."""
    value = f"decoy-set-namespace-{CANARY}"
    with pytest.raises(ValidationError) as caught:
        PipelineConfig.model_validate(_with_column_namespace(value))
    # errors() keeps pydantic's own `input` key for every field; type, msg and loc are value-free.
    assert CANARY not in str(caught.value)
    assert all(CANARY not in f"{e['type']}{e['msg']}{e['loc']}" for e in caught.value.errors())
    assert "namespace_placeholder_unset" in str(caught.value)
    with pytest.raises(ValidationError) as rel:
        RelationshipConfig.model_validate(_relationship(value))
    assert CANARY not in str(rel.value)
    assert all(CANARY not in f"{e['type']}{e['msg']}{e['loc']}" for e in rel.value.errors())
