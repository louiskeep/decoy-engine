"""F29 consumer contract: the pinned cases the CLI (C1) and platform (C2) read.

`tests/fixtures/f29/consumer-contract.json` holds one record per outcome category and retry class,
one event of every variant in both projections, and the degraded readings a consumer must handle
(an unknown identity, an unsupported version, a forged retry class, an old engine with no reason).
The consumers' own tests load this file, so a change here is a visible contract change.

Regenerate after an intentional change by running this file as a script.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from decoy_engine import progress, reasons
from decoy_engine.progress import (
    CheckpointEvent,
    CheckpointInfo,
    PhaseEvent,
    PublicationInfo,
    RateSample,
    RetryEvent,
    RetryInfo,
    RouteEvent,
    TerminalEvent,
    WarningEvent,
)
from decoy_engine.reasons import (
    CleanupState,
    Operation,
    Phase,
    PublicationState,
    ReasonContext,
    ReasonRecord,
)

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "f29" / "consumer-contract.json"
_TS = 1_700_000_000.0


def _records() -> dict[str, dict[str, Any]]:
    r = ReasonRecord.for_reason
    publication = ReasonContext.build(
        operation=Operation.COMMIT,
        storage_ref="output",
        publication_state=PublicationState.NOT_PUBLISHED,
        cleanup_state=CleanupState.CONFIRMED,
    )
    return {
        "missing_secret_is_configuration_repair": r(
            reasons.runtime.MISSING_MASK_SECRET, phase=Phase.RESOLVE
        ).to_payload(),
        "full_sink_is_environment_repair": r(
            reasons.decoy.STORAGE_CAPACITY_EXHAUSTED, phase=Phase.PUBLISH, context=publication
        ).to_payload(),
        "transient_connector_is_retry_operation": r(
            reasons.decoy.CONNECTOR_TRANSIENT_FAILURE, phase=Phase.PUBLISH
        ).to_payload(),
        "sidecar_after_commit_is_partial_publication": r(
            reasons.decoy.PUBLICATION_PARTIAL,
            phase=Phase.PUBLISH,
            context=ReasonContext.build(
                operation=Operation.SIDECAR,
                storage_ref="output",
                publication_state=PublicationState.PARTIAL,
                cleanup_state=CleanupState.NOT_NEEDED,
            ),
        ).to_payload(),
        "enforced_negative_verdict_is_verification": r(
            reasons.decoy.VERIFICATION_FAILED, phase=Phase.VERIFY
        ).to_payload(),
        "verifier_outage_is_verification": r(
            reasons.decoy.VERIFICATION_UNAVAILABLE, phase=Phase.VERIFY
        ).to_payload(),
        "report_only_findings_are_findings": r(
            reasons.decoy.FINDINGS_REPORTED, phase=Phase.VERIFY
        ).to_payload(),
        "pause_is_a_control_outcome": r(reasons.decoy.RUN_PAUSED).to_payload(),
        "unknown_exception_has_unknown_retry": r(
            reasons.decoy.UNEXPECTED_ERROR, phase=Phase.MASK
        ).to_payload(),
    }


def _events() -> dict[str, dict[str, Any]]:
    record = ReasonRecord.for_reason(reasons.runtime.MISSING_MASK_SECRET, phase=Phase.RESOLVE)
    events: dict[str, progress.ProgressEvent] = {
        "phase_start": PhaseEvent("mask", "start", "t", 3, None, None, None, _TS, sequence=1, phase="mask", object_ref="t"),
        "phase_error": PhaseEvent("resolve", "error", None, None, None, 0.5, "MissingMaskSecret", _TS, sequence=2, phase="resolve", reason=record),
        "rate_sample": RateSample("t", 10, 100.0, 0.1, 20, False, _TS, sequence=3, phase="mask", object_ref="t", bytes_done=800, byte_basis="arrow_logical"),
        "route": RouteEvent("sequential", None, None, None, _TS, sequence=4, phase="resolve"),
        "warning": WarningEvent("top_code_generalized", "t", "c", _TS, sequence=5, object_ref="t"),
        "terminal_error": TerminalEvent("error", "MissingMaskSecret", 0.6, 0, _TS, sequence=6, reason=record, publication=PublicationInfo(Operation.COMMIT, "output", PublicationState.NOT_PUBLISHED, CleanupState.CONFIRMED)),
        "retry_report": RetryEvent(None, _TS, sequence=7, retry=RetryInfo("route", 2, 1, "scheduled")),
        "checkpoint_report": CheckpointEvent(None, _TS, sequence=8, checkpoint=CheckpointInfo("committed", "ckpt-1", 1, 10, 80)),
    }  # fmt: skip
    return {
        name: {
            "v1": progress.event_to_dict(event),
            "v0": progress.event_to_legacy_dict(event),
        }
        for name, event in events.items()
    }


def _degraded() -> dict[str, dict[str, Any]]:
    base = ReasonRecord.for_reason(
        reasons.runtime.MISSING_MASK_SECRET, phase=Phase.RESOLVE
    ).to_payload()
    cases = {
        "an_identity_from_a_newer_registry": dict(base, code="a_code_from_a_newer_registry"),
        "an_unsupported_schema_major": dict(base, schema_version=2),
        "a_forged_retry_class": dict(base, retry_class="retry_operation", outcome="success"),
        "an_old_engine_with_no_reason": None,
        "a_payload_that_is_not_a_mapping": "text",
    }
    out: dict[str, dict[str, Any]] = {}
    for name, payload in cases.items():
        parsed = ReasonRecord.from_payload(payload) if payload is not None else None
        out[name] = {
            "input": payload,
            "read_as": parsed.to_payload() if parsed else None,
            "consumer_rule": "no reason key: treat as unknown (decoy.unexpected_error), never success"
            if payload is None
            else "read severity, outcome and retry_class from the record, never from the input",
        }
    return out


def build_contract() -> dict[str, Any]:
    return {
        "registry_version": reasons.REGISTRY_VERSION,
        "reason_schema_version": reasons.REASON_SCHEMA_VERSION,
        "event_schema_version": reasons.EVENT_SCHEMA_VERSION,
        "reason_records": _records(),
        "events": _events(),
        "degraded_readings": _degraded(),
    }


def test_the_consumer_contract_fixture_is_current() -> None:
    expected = json.loads(json.dumps(build_contract(), sort_keys=True))
    actual = json.loads(FIXTURE.read_text())
    assert actual == expected, "consumer contract changed; regenerate the fixture deliberately"


def test_every_outcome_category_and_retry_class_the_consumers_branch_on_is_covered() -> None:
    records = [ReasonRecord.from_payload(p) for p in build_contract()["reason_records"].values()]
    assert {r.outcome.value for r in records} >= {
        "configuration",
        "publication",
        "verification",
        "findings",
        "cancelled",
        "execution",
    }
    assert {r.retry_class.value for r in records} >= {
        "never",
        "configuration_repair",
        "environment_repair",
        "retry_operation",
        "unknown",
    }


if __name__ == "__main__":
    FIXTURE.write_text(json.dumps(build_contract(), indent=1, sort_keys=True) + "\n")
    print(f"wrote {FIXTURE}")
