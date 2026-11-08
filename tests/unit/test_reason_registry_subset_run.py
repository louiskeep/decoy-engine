"""SUB-STOP: `config.subset_run_unsupported` is registered and pinned append-only."""

from __future__ import annotations

import json
from pathlib import Path

from decoy_engine import reasons
from decoy_engine.errors import PipelineValidationError
from decoy_engine.reason_adapter import reason_from_exception
from decoy_engine.reasons import Phase

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "f29" / "reason-manifest-v1.6-sub-stop.json"
)


def test_subset_run_reason_is_registered() -> None:
    spec = reasons.find("config", "subset_run_unsupported")
    assert spec is not None
    assert spec.owner == "PipelineValidationError"

    assert _FIXTURE.is_file(), "the SUB-STOP addition is pinned in its own fixture"
    pinned = json.loads(_FIXTURE.read_text())
    live = reasons.manifest()
    assert pinned["reason_schema_version"] == live["reason_schema_version"]
    live_entry = next(
        e
        for e in live["entries"]
        if (e["namespace"], e["code"]) == ("config", "subset_run_unsupported")
    )
    assert pinned["entries"] == [live_entry]

    err = PipelineValidationError("x", path="subset", code=reasons.config.SUBSET_RUN_UNSUPPORTED)
    record = reason_from_exception(err, phase=Phase.CONFIGURATION)
    assert record.namespace == "config"
    assert record.code == "subset_run_unsupported"
    assert record.outcome.value == "configuration"
    assert record.retry_class.value == "configuration_repair"
    assert record.phase.value == "configuration"
