"""The one serializer that turns a reason identity into its `{namespace, code}` wire mapping."""

from __future__ import annotations

import pytest

from decoy_engine import phase, reasons
from decoy_engine.reasons import ReasonRef, reason_ref_payload


def test_the_payload_is_the_namespace_and_code_mapping() -> None:
    ref = reasons.post_scan.CHECK_ERROR.spec.ref
    assert reason_ref_payload(ref) == {"namespace": "post_scan", "code": "check_error"}


def test_the_payload_is_a_fresh_plain_dict_each_call() -> None:
    ref = reasons.post_scan.CHECK_ERROR.spec.ref
    first = reason_ref_payload(ref)
    first["code"] = "changed"
    assert reason_ref_payload(ref)["code"] == "check_error"
    assert type(first) is dict


def test_the_payload_does_not_resolve_the_identity() -> None:
    assert reason_ref_payload(ReasonRef("x", "y")) == {"namespace": "x", "code": "y"}


def test_the_phase_envelope_wire_form_is_the_same_mapping() -> None:
    ref = reasons.post_scan.CHECK_ERROR.spec.ref
    assert phase._reason_wire(ref) == reason_ref_payload(ref)


def test_the_phase_envelope_still_refuses_an_unknown_identity() -> None:
    with pytest.raises(phase.PhaseContractError, match="unknown reason identity"):
        phase._reason_wire(ReasonRef("post_scan", "no_such_code"))
