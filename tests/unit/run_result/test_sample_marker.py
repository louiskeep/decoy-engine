"""SAMPLE SE2 (AT-16): `RunResult.sample` is optional and an old record still reads."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from decoy_engine.run_result import RunResult, RunResultRead

CORPUS = Path(__file__).resolve().parents[2] / "integration" / "compat_corpus" / "run_result"
MARKER = {
    "rows": 5,
    "roots": ["items", "notes"],
    "head_tables": {"notes": 5},
    "scratch": ".decoy/scratch/sample-0123",
}


def _old() -> dict[str, Any]:
    return json.loads((CORPUS / "run-result-v1.json").read_text(encoding="utf-8"))


def _sampled() -> dict[str, Any]:
    doc = _old()
    doc["delivery"]["outcome"] = "not_applicable"
    for table in doc["tables"]:
        table["delivery"].update(state="none", format=None, output=None)
    doc["sample"] = copy.deepcopy(MARKER)
    return doc


@pytest.mark.parametrize("model", [RunResult, RunResultRead])
def test_at16_a_sample_marker_round_trips(model: Any) -> None:
    doc = _sampled()
    assert model.model_validate(doc).to_payload() == doc


@pytest.mark.parametrize("model", [RunResult, RunResultRead])
def test_at16_an_old_record_without_the_field_reads_and_stays_without_it(model: Any) -> None:
    doc = _old()
    assert "sample" not in doc
    read = model.model_validate(doc)
    assert read.sample is None
    assert "sample" not in read.to_payload()
    assert "sample" not in read.model_dump(mode="json", by_alias=True)


@pytest.mark.parametrize(
    "edit",
    [
        lambda m: m.update(rows=True),
        lambda m: m.update(rows="5"),
        lambda m: m.update(scratch="/abs/path"),
        lambda m: m.update(scratch="../up"),
        lambda m: m.update(roots="items"),
        lambda m: m["head_tables"].update(notes=-1),
        lambda m: m.update(extra=1),
    ],
)
def test_at16_the_write_model_refuses_a_malformed_marker(edit: Any) -> None:
    doc = _sampled()
    edit(doc["sample"])
    with pytest.raises(ValidationError):
        RunResult.model_validate(doc)


class _Wrapper(BaseModel):
    record: RunResult


@pytest.mark.parametrize("with_sample", [False, True])
def test_every_dump_path_gives_one_wire_form(with_sample: bool) -> None:
    doc = _sampled() if with_sample else _old()
    record = RunResult.model_validate(doc)
    payload = record.to_payload()
    assert payload == doc
    assert json.loads(record.model_dump_json(by_alias=True)) == doc
    nested = json.loads(_Wrapper(record=record).model_dump_json(by_alias=True))["record"]
    assert nested == doc
    assert _Wrapper(record=record).model_dump(mode="json", by_alias=True)["record"] == doc
