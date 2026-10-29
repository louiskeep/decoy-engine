"""F29 remediation regressions: the safe boundary cannot be forged, and route reasons are registered.

HIGH 2: a `ReasonRecord`, a progress event group or a captured-failure marker built outside the
engine's own paths must never put caller-chosen text, semantics or identifiers on the wire.
HIGH 3: the route reasons a real job selects resolve in the registry's `route` namespace.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.progress import (
    CheckpointEvent,
    PhaseEvent,
    RateSample,
    RetryEvent,
    RouteEvent,
    TerminalEvent,
    WarningEvent,
    event_to_dict,
    event_to_legacy_dict,
)
from decoy_engine.reason_adapter import capture_failure, reason_from_exception
from decoy_engine.reasons import Phase, ReasonContext, ReasonRecord

CONN = "postgres://user:pw@host/db"


def _good(code: Any = None) -> ReasonRecord:
    return ReasonRecord.for_reason(code or reasons.runtime.MISSING_MASK_SECRET, phase=Phase.RESOLVE)


# -- forged ReasonRecord ---------------------------------------------------------------------


def test_a_record_cannot_carry_semantics_the_registry_does_not_assign() -> None:
    good = _good()
    for field, value in (
        ("retry_class", reasons.RetryClass.RETRY_OPERATION),
        ("severity", reasons.Severity.INFO),
        ("outcome", reasons.Outcome.SUCCESS),
        ("kind", reasons.Kind.WARNING),
    ):
        with pytest.raises(ValueError):
            dataclasses.replace(good, **{field: value})


def test_a_record_cannot_name_an_unregistered_identity_or_a_foreign_version() -> None:
    good = _good()
    with pytest.raises(ValueError):
        dataclasses.replace(good, code=f"leak {CONN}")
    with pytest.raises(ValueError):
        dataclasses.replace(good, namespace="not_a_namespace")
    with pytest.raises(ValueError):
        dataclasses.replace(good, registry_version="9.9.9")
    with pytest.raises(ValueError):
        dataclasses.replace(good, schema_version=99)


def test_a_record_cannot_carry_an_unapproved_context() -> None:
    good = _good()
    with pytest.raises(ValueError):
        dataclasses.replace(good, context=ReasonContext(object_ref=CONN))
    with pytest.raises(ValueError):
        dataclasses.replace(good, context="not a context")  # type: ignore[arg-type]


def test_a_record_forged_past_its_constructor_does_not_serialize() -> None:
    forged = _good()
    object.__setattr__(forged, "retry_class", reasons.RetryClass.RETRY_OPERATION)
    with pytest.raises(ValueError):
        forged.to_payload()
    ctx = ReasonContext()
    object.__setattr__(ctx, "object_ref", CONN)
    bad_ctx = dataclasses.replace(_good(), context=ReasonContext())
    object.__setattr__(bad_ctx, "context", ctx)
    with pytest.raises(ValueError):
        bad_ctx.to_payload()


def test_a_forged_record_in_an_event_degrades_to_the_fixed_diagnostic() -> None:
    forged = _good()
    object.__setattr__(forged, "code", f"leak {CONN}")
    event = TerminalEvent("error", error_class="ValueError", reason=_good())
    object.__setattr__(event, "reason", forged)
    wire = json.dumps(event_to_dict(event))
    assert CONN not in wire
    assert json.loads(wire)["reason"]["code"] == str(reasons.decoy.REASON_RECORD_INVALID)


# -- event groups ----------------------------------------------------------------------------


@pytest.mark.parametrize("group", ["reason", "retry", "checkpoint", "publication"])
def test_every_event_group_is_runtime_checked(group: str) -> None:
    with pytest.raises(ValueError):
        TerminalEvent("error", **{group: f"raw {CONN}"})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PhaseEvent("mask", "start", **{group: {"code": "x"}})  # type: ignore[arg-type]


def test_a_raw_string_reason_cannot_serialize() -> None:
    with pytest.raises(ValueError):
        TerminalEvent("error", error_class="ValueError", reason=CONN)  # type: ignore[arg-type]


def test_a_group_subclass_cannot_override_its_serializer() -> None:
    class Evil(ReasonRecord):
        def to_payload(self) -> dict[str, Any]:
            return {"leak": CONN}

    evil = object.__new__(Evil)
    with pytest.raises(ValueError):
        TerminalEvent("error", reason=evil)


# -- identifier-bearing legacy fields ---------------------------------------------------------


def test_legacy_name_fields_never_carry_a_connection_string_or_path() -> None:
    events = [
        PhaseEvent(CONN, "start", table=CONN),
        RateSample(CONN, 1, 1.0, 1.0),
        RouteEvent(CONN, lane=CONN, table=CONN),
        WarningEvent("missing_mask_secret", table=CONN, column="/etc/passwd"),
        RetryEvent(
            table="a=b;c",
            retry=reasons_retry(),
        ),
        CheckpointEvent(table="x\x00y", checkpoint=reasons_checkpoint()),
    ]
    for event in events:
        for wire in (event_to_dict(event), event_to_legacy_dict(event)):
            text = json.dumps(wire)
            assert "postgres" not in text and "passwd" not in text and "a=b" not in text, text


def test_ordinary_table_and_column_names_pass_through_unchanged() -> None:
    event = WarningEvent("missing_mask_secret", table="public.my table", column="Åge")
    assert (event.table, event.column) == ("public.my table", "Åge")
    assert PhaseEvent("mask", "start", table="orders").table == "orders"
    route = event_to_legacy_dict(RouteEvent("full_frame", lane="native"))
    assert (route["route"], route["lane"]) == ("full_frame", "native")


def test_unknown_statuses_are_rejected() -> None:
    with pytest.raises(ValueError):
        PhaseEvent("mask", "exploded")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TerminalEvent("fine-actually")  # type: ignore[arg-type]


def reasons_retry() -> Any:
    from decoy_engine.progress import RetryInfo

    return RetryInfo(scope="operation", attempt=1, retries_completed=0, state="not_scheduled")


def reasons_checkpoint() -> Any:
    from decoy_engine.progress import CheckpointInfo

    return CheckpointInfo(state="unsupported")


# -- captured failures ------------------------------------------------------------------------


def test_a_valid_record_attached_to_an_arbitrary_exception_is_not_trusted() -> None:
    exc = ValueError("anything")
    exc._decoy_reason_record = _good(reasons.runtime.MISSING_MASK_SECRET)  # type: ignore[attr-defined]
    record = reason_from_exception(exc)
    assert (record.namespace, record.code) == ("decoy", "unexpected_error")


def test_a_lookalike_capture_wrapper_is_not_trusted() -> None:
    from decoy_engine import reason_adapter

    exc = ValueError("anything")
    fake = object.__new__(reason_adapter._Captured)  # no token: not minted by capture_failure
    exc.__dict__[reason_adapter._ATTR] = fake
    assert reason_from_exception(exc).code == "unexpected_error"
    with pytest.raises(TypeError):
        reason_adapter._Captured(object(), _good())


def test_capture_failure_still_remembers_and_a_copy_does_not_carry_the_capture() -> None:
    import copy

    exc = ValueError("anything")
    first = capture_failure(exc, phase=Phase.RESOLVE)
    assert reason_from_exception(exc) == first
    assert reason_from_exception(copy.copy(exc)).code == "unexpected_error"


# -- HIGH 3: route reasons --------------------------------------------------------------------


class _Profile:
    relationships = (object(),)


def _decide(**overrides: Any) -> tuple[str, str]:
    from decoy_engine.execution._pipeline_routing import decide_execution_route
    from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="p",
                parent_columns=("id",),
                child_table="c",
                child_columns=("pid",),
                namespace="ns",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    kwargs: dict[str, Any] = {
        "has_generate_table": False,
        "has_mask_table": True,
        "validators": [],
        "fidelity_report": False,
        "vault_writer": None,
        "execution_mode": "auto",
        "graph": graph,
        "resolved_substrate": "pandas",
        "out_of_core_compatible": True,
        "out_of_core_reject_code": None,
        "largest_table_rows": 5_000_000,
        "out_of_core_threshold_rows": 5_000_000,
        "full_frame_reject_rows": 7_500_000,
        "use_byte_estimate_routing": False,
        "use_probe_routing": False,
    }
    kwargs.update(overrides)
    return decide_execution_route(_Profile(), **kwargs)


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        ({"post_validation": True}, "post_validation_requested"),
        ({"run_storm": True}, "run_storm_requested"),
    ],
)
def test_requested_scan_routes_resolve_in_the_route_namespace(
    flag: dict[str, bool], expected: str
) -> None:
    route, reason = _decide(**flag)
    assert route == "full_frame"
    assert reason == expected
    assert reasons.find("route", reason) is not None
    assert getattr(reason, "namespace", "route") == "route"


def test_the_mixed_chunked_reason_is_registered() -> None:
    assert reasons.find("route", "mixed_generate_mask_chunked") is not None
    assert reasons.find("route", "mixed_runtime_declined") is not None


def test_a_job_reports_a_registered_route_reason(tmp_path: Any) -> None:
    from decoy_engine import run_pipeline
    from tests.integration._f29_jobs import ENGINE_VERSION, fk_job

    cfg, sources = fk_job(tmp_path)
    result = run_pipeline(cfg, sources, engine_version=ENGINE_VERSION, post_validation=True)
    reason = result.quality_metrics["execution"]["route_reason"]
    assert reasons.find("route", reason.split(":")[0]) is not None, reason
