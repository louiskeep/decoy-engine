"""The new config fields leave every legacy dump and hash where it was.

Pre-scan and the model post-scan fields are additive. A config that predates them must dump,
hash and compile byte-for-byte as before; a config that sets one changes its hash only when the
change alters what a run checks or blocks on (the plan hash keeps `post_scan.dismissed`, and
leaves `pre_scan` out because pre-scan only suggests).
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from decoy_engine.config import Dismissal, PipelineConfig, PostScanConfig, PreScanConfig
from decoy_engine.plan._compile import compile_plan
from decoy_engine.plan._serialize import plan_to_yaml
from decoy_engine.profile._source import profile_source

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
BASELINE = json.loads((FIXTURES / "f06" / "legacy-compat-baseline.json").read_text())
# The frozen F06 config predates the required csv has_header; the old default keeps its read.
LEGACY_RAW: dict[str, Any] = {
    **BASELINE["raw_config"],
    "sources": {"p": {**BASELINE["raw_config"]["sources"]["p"], "has_header": True}},
}
# A csv dump now always carries has_header, so only that field moves the dump sha.
DUMP_SHA256_WITH_HAS_HEADER = "5e4147baae457372e80f819346b646eb6f02c9f79fb53e5b8bfc2845e24ce905"

NER_DISMISSAL: dict[str, Any] = {
    "table": "p",
    "column": "note",
    "detector_id": "person_name",
    "source": "ner",
    "model_id": "en_core_web_sm",
    "model_version": "3.8.0",
    "strategy": "text_redact",
}
FAIL_POST_SCAN: dict[str, Any] = {
    "integrity": True,
    "detectors": ["regex", "ner"],
    "ner_findings": "fail",
}


def _raw(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    raw = copy.deepcopy(LEGACY_RAW)
    (tmp_path / "p.csv").write_text("id,age,note\n1,42,a\n2,7,b\n3,13,c\n")
    raw["sources"]["p"]["path"] = str(tmp_path / "p.csv")
    raw.update(extra)
    return raw


def _compile(raw: dict[str, Any]) -> Any:
    cfg = PipelineConfig.model_validate(raw).model_dump()
    return compile_plan(cfg, profile_source(cfg, seed=7), decoy_engine_version="legacy-fixture")


def _hash(tmp_path: Path, **extra: Any) -> str:
    return str(_compile(_raw(tmp_path, **extra)).pipeline_config_hash)


def test_f06_baseline_dump_hash_and_plan_are_unchanged(tmp_path: Path) -> None:
    cfg = PipelineConfig.model_validate(LEGACY_RAW).model_dump()
    dump = json.dumps(cfg, sort_keys=True)
    assert hashlib.sha256(dump.encode()).hexdigest() == DUMP_SHA256_WITH_HAS_HEADER
    del cfg["sources"]["p"]["has_header"]
    legacy_dump = json.dumps(cfg, sort_keys=True)
    assert hashlib.sha256(legacy_dump.encode()).hexdigest() == BASELINE["config_dump_sha256"]
    plan = _compile(_raw(tmp_path))
    assert plan.pipeline_config_hash == BASELINE["pipeline_config_hash"]
    assert plan_to_yaml(plan) == BASELINE["plan_yaml_at_baseline"].replace(
        "plan_version: 2", "plan_version: 3", 1
    )
    with_models = _compile(
        _raw(tmp_path, post_scan={**FAIL_POST_SCAN, "dismissed": [NER_DISMISSAL]})
    )
    assert with_models.plan_compile.checks_passed == plan.plan_compile.checks_passed, (
        "availability is a preflight, not a plan check"
    )


def _config_fixtures() -> list[tuple[str, dict[str, Any]]]:
    found = []
    for path in sorted(FIXTURES.rglob("*")):
        if path.suffix not in {".yaml", ".yml", ".json"} or not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text())
        except Exception:
            continue
        if path.name == "legacy-compat-baseline.json":
            data = {"raw_config": LEGACY_RAW}
        if isinstance(data, dict) and isinstance(data.get("raw_config"), dict):
            data = data["raw_config"]
        if (
            isinstance(data, dict)
            and data.get("version") == 2
            and "tables" in data
            and "targets" in data
        ):
            found.append((str(path.relative_to(FIXTURES)), data))
    return found


def test_every_config_fixture_validates_and_dumps_without_new_keys() -> None:
    fixtures = _config_fixtures()
    assert fixtures, "no config fixture found under tests/fixtures"
    for name, raw in fixtures:
        dump = PipelineConfig.model_validate(raw).model_dump()
        assert "pre_scan" not in dump, name
        assert set(dump.get("post_scan", {})) <= {"integrity"}, name
        again = PipelineConfig.model_validate(dump).model_dump()
        assert again == dump, name


def test_default_values_are_dropped_from_the_dump_even_when_written_out() -> None:
    raw = copy.deepcopy(LEGACY_RAW)
    plain = PipelineConfig.model_validate(raw).model_dump()
    explicit = PipelineConfig.model_validate(
        {
            **raw,
            "pre_scan": {"detectors": ["regex"]},
            "post_scan": {
                "integrity": False,
                "detectors": ["regex"],
                "ner_findings": "warn",
                "dismissed": [],
            },
        }
    ).model_dump()
    assert explicit == plain
    assert "pre_scan" not in explicit
    with_ner = PipelineConfig.model_validate(
        {
            **raw,
            "pre_scan": {"detectors": ["regex", "ner"], "ner_model": "en_core_web_sm"},
            "post_scan": {
                "integrity": True,
                "detectors": ["regex", "ner"],
                "ner_model": "en_core_web_sm",
            },
        }
    ).model_dump()
    assert "ner_model" not in with_ner["pre_scan"] and "ner_model" not in with_ner["post_scan"]
    assert PipelineConfig.model_validate(with_ner).model_dump() == with_ner
    assert PostScanConfig().model_dump() == {"integrity": False}
    assert json.loads(PostScanConfig().model_dump_json()) == {"integrity": False}


def test_new_fields_round_trip_in_canonical_order() -> None:
    raw = copy.deepcopy(LEGACY_RAW)
    raw["pre_scan"] = {"detectors": ["ner", "classifier", "regex"], "ner_model": "en_core_web_sm"}
    raw["post_scan"] = {
        **FAIL_POST_SCAN,
        "detectors": ["ner", "regex"],
        "dismissed": [NER_DISMISSAL],
    }
    dump = PipelineConfig.model_validate(raw).model_dump()
    assert tuple(dump["pre_scan"]["detectors"]) == ("regex", "classifier", "ner")
    assert tuple(dump["post_scan"]["detectors"]) == ("regex", "ner")
    assert dump["post_scan"]["ner_findings"] == "fail"
    assert dump["post_scan"]["dismissed"][0]["model_version"] == "3.8.0"
    assert PipelineConfig.model_validate(dump).model_dump() == dump
    assert isinstance(PreScanConfig().detectors, tuple)
    assert isinstance(Dismissal(**NER_DISMISSAL), Dismissal)


def test_a_dismissal_changes_the_plan_hash_under_fail_mode(tmp_path: Path) -> None:
    without = _hash(tmp_path, post_scan=dict(FAIL_POST_SCAN))
    with_dismissal = _hash(tmp_path, post_scan={**FAIL_POST_SCAN, "dismissed": [NER_DISMISSAL]})
    assert without != with_dismissal
    assert without != BASELINE["pipeline_config_hash"]  # fail mode itself changes the hash


def test_pre_scan_does_not_change_the_plan_hash(tmp_path: Path) -> None:
    base = _hash(tmp_path)
    assert base == BASELINE["pipeline_config_hash"]
    assert _hash(tmp_path, pre_scan={"detectors": ["regex", "classifier"]}) == base


@pytest.mark.parametrize("model", ["en_core_web_sm", "en_core_web_md"])
def test_ner_model_is_part_of_the_post_scan_hash(tmp_path: Path, model: str) -> None:
    default = _hash(tmp_path, post_scan={"integrity": True, "detectors": ["regex", "ner"]})
    named = _hash(
        tmp_path, post_scan={"integrity": True, "detectors": ["regex", "ner"], "ner_model": model}
    )
    assert (default == named) is (model == "en_core_web_sm")
