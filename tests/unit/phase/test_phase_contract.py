"""Phase envelope, reason registry and the three foundation correctness fixes."""

from __future__ import annotations

import dataclasses
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
