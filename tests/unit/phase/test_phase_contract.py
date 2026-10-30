"""Phase envelope, reason registry and the three foundation correctness fixes."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from decoy_engine import phase, reasons
from decoy_engine.reasons import ReasonRef
from decoy_engine.verification.postmask.types import PostMaskReport


def _result(**kw):
    base = dict(
        phase="post_scan", check_id="integrity", status="passed", method="m", method_version="1"
    )
    base.update(kw)
    return phase.PhaseResult(**base)


def test_public_collection_types_are_immutable():
    for cls in (phase.PhaseResult, phase.PhaseCoverage, phase.PhaseEvent, PostMaskReport):
        for f in dataclasses.fields(cls):
            ann = str(f.type)
            assert (
                "list[" not in ann
                and "dict[" not in ann
                and "set[" not in ann.replace("frozenset[", "")
            ), (
                cls.__name__,
                f.name,
                ann,
            )


def test_freeze_is_deep_and_rejects_later_mutation():
    src = {"a": [1, {"b": [2, 3]}], "c": {"x"}}
    frozen = phase.freeze(src)
    src["a"][1]["b"].append(99)
    res = _result(payload=frozen)
    assert phase.to_wire(res)["payload"] == {"a": [1, {"b": [2, 3]}], "c": ["x"]}
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.status = "failed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        res.payload[0] = ("z", 1)  # type: ignore[index]


def test_canonical_bytes_stable_after_source_changes():
    src = {"k": [1, 2]}
    res = _result(payload=phase.freeze(src))
    before = phase.canonical_bytes(res)
    src["k"].append(3)
    assert phase.canonical_bytes(res) == before


def test_freeze_refuses_unsafe_objects():
    with pytest.raises(phase.PhaseContractError):
        phase.freeze({"x": object()})
    with pytest.raises(phase.PhaseContractError):
        phase.to_wire(_result(payload=(object(),)))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kw",
    [
        {"schema_version": "phase-result/v0"},
        {"status": "weird"},
        {"phase": "scan"},
        {"reasons": (ReasonRef("post_scan", "nope"),)},
    ],
)
def test_unknown_versions_status_or_reason_fail_closed(kw):
    res = _result(**kw)
    assert not phase.satisfies_required_gate(res)
    with pytest.raises(phase.PhaseContractError):
        phase.to_wire(res)


@pytest.mark.parametrize(
    "status", ["completed", "failed", "error", "unavailable", "skipped", "not_applicable"]
)
def test_only_passed_satisfies_required_gate(status):
    assert not phase.satisfies_required_gate(_result(status=status))
    assert phase.satisfies_required_gate(_result(status="passed"))


def test_error_result_carries_type_name_never_text():
    secret = "ssn=123-45-6789 /home/u/.ssh/id"
    res = phase.error_result(
        "post_scan",
        "integrity",
        "m",
        "1",
        reasons.get_reason("post_scan", "check_error").ref,
        OSError(secret),
    )
    wire = phase.canonical_bytes(res).decode()
    assert "OSError" in wire and secret not in wire and "123-45" not in wire
    assert res.status == "error"


def test_registry_identities_unique_and_resolvable():
    assert reasons.REGISTRY_SCHEMA == "decoy-reasons/v1"
    for (ns, code), spec in reasons.REASONS.items():
        assert reasons.get_reason(ns, code) is spec
        assert spec.summary and "{" not in spec.summary
    with pytest.raises(reasons.UnknownReasonError):
        reasons.get_reason("post_scan", "post_scan_integrity_requested")


def test_post_validation_runner_does_not_capture_exception_text_or_mutate_result(monkeypatch):
    from decoy_engine.execution import ExecutionResult
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph
    from decoy_engine.validation.post import PostValidationRunner, _runner

    def boom(ctx):
        raise ValueError("leaky cell value 4111111111111111")

    monkeypatch.setattr(_runner, "SCANS", (("boom", boom),))
    result = ExecutionResult(outputs={}, warnings=())
    summary = PostValidationRunner().run(
        plan=SimpleNamespace(),  # type: ignore[arg-type]
        execution_result=result,
        sources={},
        profile=SimpleNamespace(tables=()),  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        config={"post_validation": True},
    )
    assert summary is not None and summary.failed_checks == ("boom",)
    assert "4111" not in repr(summary)
    assert summary.quality_warnings[0].detail == {"error_type": "ValueError"}
    assert "quality_summary" not in result.quality_metrics


# ---- construction-time immutability and lossless freeze/thaw (round 2) --------------------------


def test_mutating_constructor_inputs_cannot_change_canonical_bytes():
    scope = ["t1", "t2"]
    limits = ["row_cap"]
    ident = [("source", "abc")]
    reasons_in = [ReasonRef("post_scan", "check_error")]
    payload = {"a": [1, 2]}
    res = phase.PhaseResult(
        phase="post_scan",
        check_id="integrity",
        status="error",
        method="m",
        method_version="1",
        coverage=phase.PhaseCoverage(requested_scope=scope, limits=limits),  # type: ignore[arg-type]
        reasons=reasons_in,  # type: ignore[arg-type]
        identity=ident,  # type: ignore[arg-type]
        payload=payload,  # type: ignore[arg-type]
    )
    before = phase.canonical_bytes(res)
    scope.append("t3")
    limits.clear()
    ident.append(("x", "y"))
    reasons_in.clear()
    payload["a"].append(3)
    payload["b"] = 1
    assert phase.canonical_bytes(res) == before
    for value in (res.reasons, res.identity, res.coverage.requested_scope, res.coverage.limits):
        assert isinstance(value, tuple)


def test_event_and_coverage_collections_are_tuples_after_construction():
    cov = phase.PhaseCoverage(observed_scope=["a"], omissions=["b"])  # type: ignore[arg-type]
    assert cov.observed_scope == ("a",) and cov.omissions == ("b",)


def test_freeze_thaw_round_trip_is_lossless_and_unambiguous():
    # a JSON list of pairs stays a list; an object stays an object
    assert phase.to_wire(_result(payload=phase.freeze([["a", 1], ["b", 2]])))["payload"] == [
        ["a", 1],
        ["b", 2],
    ]
    assert phase.to_wire(_result(payload=phase.freeze({"a": 1, "b": 2})))["payload"] == {
        "a": 1,
        "b": 2,
    }
    # empty object and empty list are distinct
    assert phase.to_wire(_result(payload=phase.freeze({})))["payload"] == {}
    assert phase.to_wire(_result(payload=phase.freeze([])))["payload"] == []
    # nested mix
    mixed = {"rows": [{"k": [1, {"z": None}]}], "pairs": [["x", 1]]}
    assert phase.to_wire(_result(payload=phase.freeze(mixed)))["payload"] == mixed


def test_freeze_refuses_keys_that_would_collide_or_be_rewritten():
    with pytest.raises(phase.PhaseContractError):
        phase.freeze({1: "a", "1": "b"})
    with pytest.raises(phase.PhaseContractError):
        phase.freeze({1: "a"})


def test_freeze_refuses_non_finite_numbers():
    for bad in (float("nan"), float("inf")):
        with pytest.raises(phase.PhaseContractError):
            phase.freeze({"x": bad})


# ---- identity and FrozenMap construction validation (round 4) ----------------------------------


@pytest.mark.parametrize(
    "ident",
    [
        (("k", "a"), ("k", "b")),
        [("k", "a"), ("k", "b")],
    ],
)
def test_identity_rejects_duplicate_keys_instead_of_dropping_a_field(ident):
    with pytest.raises(phase.PhaseContractError):
        _result(identity=ident)


@pytest.mark.parametrize(
    "ident",
    [
        ((1, "a"), ("1", "b")),
        ((1, 2),),
        (("k", 2),),
        ((1, "a"),),
        (("k", "a", "extra"),),
        ("ka",),
    ],
)
def test_identity_requires_exact_str_pairs_and_never_coerces(ident):
    with pytest.raises(phase.PhaseContractError):
        _result(identity=ident)


def test_identity_accepts_str_pairs_and_mappings_losslessly():
    res = _result(identity={"source": "s1", "target": "t1"})
    assert phase.to_wire(res)["identity"] == {"source": "s1", "target": "t1"}
    res = _result(identity=(("a", "1"), ("b", "2")))
    assert phase.to_wire(res)["identity"] == {"a": "1", "b": "2"}


@pytest.mark.parametrize(
    "pairs",
    [
        ((1, "a"), ("1", "b")),
        (("k", "a"), ("k", "b")),
        ((1, "a"),),
        (("k",),),
        ("ab",),
        (("k", object()),),
        (("k", ["mutable"]),),
        (("k", {"x": 1}),),
        (("k", float("nan")),),
    ],
)
def test_direct_frozenmap_construction_is_validated(pairs):
    with pytest.raises(phase.PhaseContractError):
        phase.FrozenMap(pairs)


def test_direct_frozenmap_is_sorted_and_round_trips_through_the_gate():
    fm = phase.FrozenMap((("b", 2), ("a", (1, phase.FrozenMap((("z", None),))))))
    assert [k for k, _ in fm] == ["a", "b"]
    res = _result(payload=fm)
    assert phase.satisfies_required_gate(res)
    assert phase.to_wire(res)["payload"] == {"a": [1, {"z": None}], "b": 2}
    assert phase.canonical_bytes(res)


# ---- every envelope field validated at construction; malformed -> PhaseContractError (round 5) --


class _Wrapped:
    """A non-envelope duck type that quacks like a PhaseEvent."""

    phase = "post_scan"
    event = "done"
    status = "passed"
    reason = None
    schema_version = "phase-event/v1"


_SECRET = Path("/secret/customer.csv")


@pytest.mark.parametrize(
    "kw",
    [
        {"check_id": _SECRET},
        {"method": _SECRET},
        {"method_version": 1},
        {"phase": _SECRET},
        {"status": _SECRET},
        {"schema_version": _SECRET},
        {"coverage": None},
        {"coverage": {"rows_examined": 1}},
        {"reasons": (object(),)},
        {"reasons": (ReasonRef(_SECRET, "check_error"),)},  # type: ignore[arg-type]
        {"reasons": ReasonRef("post_scan", "check_error")},
    ],
)
def test_result_rejects_non_contract_field_types_at_construction(kw):
    with pytest.raises(phase.PhaseContractError):
        _result(**kw)


@pytest.mark.parametrize(
    "kw",
    [
        {"rows_examined": _SECRET},
        {"rows_examined": True},
        {"rows_examined": 1.5},
        {"exact": "yes"},
        {"exact": 1},
        {"sampling_method": _SECRET},
        {"requested_scope": (_SECRET,)},
        {"limits": "abc"},
        {"omissions": None},
    ],
)
def test_coverage_rejects_non_contract_field_types_at_construction(kw):
    with pytest.raises(phase.PhaseContractError):
        phase.PhaseCoverage(**kw)


@pytest.mark.parametrize(
    "kw",
    [
        {"event": _SECRET},
        {"phase": _SECRET},
        {"status": 1},
        {"reason": object()},
        {"reason": "post_scan/check_error"},
        {"schema_version": None},
    ],
)
def test_event_rejects_non_contract_field_types_at_construction(kw):
    base = dict(phase="post_scan", event="done", status="passed")
    base.update(kw)
    with pytest.raises(phase.PhaseContractError):
        phase.PhaseEvent(**base)


def _bypass(obj, name, value):
    # Frozen dataclasses can still be forced; export must re-validate, never trust construction.
    object.__setattr__(obj, name, value)
    return obj


@pytest.mark.parametrize(
    ("make", "field", "value"),
    [
        (lambda: _result(), "check_id", _SECRET),
        (lambda: _result(), "coverage", None),
        (lambda: _result(), "reasons", (object(),)),
        (lambda: _result(), "identity", ((_SECRET, "x"),)),
        (lambda: _result(), "payload", [_SECRET]),
        (lambda: phase.PhaseCoverage(), "rows_examined", _SECRET),
        (lambda: phase.PhaseEvent("post_scan", "done", "passed"), "event", _SECRET),
        (lambda: phase.PhaseEvent("post_scan", "done", "passed"), "reason", object()),
    ],
)
def test_forced_malformed_envelope_fails_closed_with_contract_error(make, field, value):
    obj = _bypass(make(), field, value)
    if isinstance(obj, phase.PhaseCoverage):
        obj = _result(coverage=phase.PhaseCoverage())
        _bypass(obj.coverage, field, value)
    with pytest.raises(phase.PhaseContractError):
        phase.to_wire(obj)
    with pytest.raises(phase.PhaseContractError):
        phase.canonical_bytes(obj)
    if isinstance(obj, phase.PhaseResult):
        assert phase.satisfies_required_gate(obj) is False


@pytest.mark.parametrize("item", [_Wrapped(), object(), None, {"phase": "post_scan"}, "x"])
def test_non_envelope_inputs_are_rejected_explicitly(item):
    with pytest.raises(phase.PhaseContractError):
        phase.to_wire(item)  # type: ignore[arg-type]
    with pytest.raises(phase.PhaseContractError):
        phase.canonical_bytes(item)  # type: ignore[arg-type]
    assert phase.satisfies_required_gate(item) is False  # type: ignore[arg-type]


def test_gate_true_implies_canonical_bytes_succeed():
    ok = _result(
        coverage=phase.PhaseCoverage(rows_examined=10, sampling_method="head", exact=False),
        reasons=(reasons.get_reason("post_scan", "check_error").ref,),
        identity={"source": "s"},
        payload={"a": [1]},
    )
    assert phase.satisfies_required_gate(ok)
    assert json.loads(phase.canonical_bytes(ok))["coverage"]["rows_examined"] == 10
    ev = phase.PhaseEvent(
        "post_scan", "done", "passed", reasons.get_reason("post_scan", "check_error").ref
    )
    assert json.loads(phase.canonical_bytes(ev))["reason"]["code"] == "check_error"
