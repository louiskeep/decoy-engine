"""`DeliveryConfig` and the `delivery` field: the default leaves every legacy dump and hash alone."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from decoy_engine.config import DeliveryConfig, PipelineConfig
from decoy_engine.plan._compile import compile_plan
from decoy_engine.profile._source import profile_source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
BASELINE = json.loads((FIXTURES / "f06" / "legacy-compat-baseline.json").read_text())
# The frozen F06 config predates the required csv has_header; the old default keeps its read.
LEGACY_RAW: dict[str, Any] = {
    **BASELINE["raw_config"],
    "sources": {"p": {**BASELINE["raw_config"]["sources"]["p"], "has_header": True}},
}


def _hash(tmp_path: Path, **extra: Any) -> str:
    raw = copy.deepcopy(LEGACY_RAW)
    (tmp_path / "p.csv").write_text("id,age,note\n1,42,a\n2,7,b\n3,13,c\n")
    raw["sources"]["p"]["path"] = str(tmp_path / "p.csv")
    raw.update(extra)
    cfg = PipelineConfig.model_validate(raw).model_dump()
    plan = compile_plan(cfg, profile_source(cfg, seed=7), decoy_engine_version="t")
    return str(plan.pipeline_config_hash)


def test_at10_default_delivery_is_omitted_and_the_legacy_hash_is_unchanged(tmp_path: Path) -> None:
    cfg = PipelineConfig.model_validate(LEGACY_RAW)
    assert cfg.delivery == DeliveryConfig()
    legacy = cfg.model_dump()
    assert "delivery" not in legacy
    assert "delivery" not in json.loads(cfg.model_dump_json())
    del legacy["sources"]["p"]["has_header"]
    dump = json.dumps(legacy, sort_keys=True)
    assert hashlib.sha256(dump.encode()).hexdigest() == BASELINE["config_dump_sha256"]
    assert _hash(tmp_path) == BASELINE["pipeline_config_hash"]
    # Written out as the default is the same config, so the same hash.
    assert _hash(tmp_path, delivery={"mode": "strict"}) == _hash(tmp_path)


def test_at10_best_effort_is_dumped_and_moves_the_hash(tmp_path: Path) -> None:
    cfg = PipelineConfig.model_validate({**LEGACY_RAW, "delivery": {"mode": "best_effort"}})
    assert cfg.model_dump()["delivery"] == {"mode": "best_effort"}
    assert PipelineConfig.model_validate(cfg.model_dump()).delivery.mode == "best_effort"
    assert _hash(tmp_path, delivery={"mode": "best_effort"}) != _hash(tmp_path)


@pytest.mark.parametrize("bad", [{"mode": "loose"}, {"mode": "strict", "extra": 1}])
def test_delivery_config_rejects_unknown_values(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DeliveryConfig.model_validate(bad)
