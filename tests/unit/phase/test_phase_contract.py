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


# --- r6: encoding-faithful gate, foreign-code-free freeze, isolated defaults, contained errors ---

_MARKER = "R6-MARKER-do-not-leak"


class _RaisingRepr:
    def __repr__(self) -> str:
        raise RuntimeError(_MARKER)

    def __lt__(self, other):
        raise RuntimeError(_MARKER)

    def __hash__(self) -> int:
        return 1


class _RaisingIter(list):
    def __iter__(self):
        raise RuntimeError(_MARKER)


def _deep(n: int, wrap=lambda x: [x]):
    node: object = 1
    for _ in range(n):
        node = wrap(node)
    return node


def _cyclic():
    a: list = []
    a.append(a)
    return a


def _hostile():
    return [
        10**5000,
        -(10**5000),
        2**63,
        -(2**63) - 1,
        _RaisingRepr(),
        {_RaisingRepr()},
        frozenset({_RaisingRepr(), 1}),
        {(1, 2)},
        {1, "a", None, 2.5},
        None,
        Path("x"),
        ValueError(_MARKER),
        RuntimeError(_MARKER),
        _deep(5000),
        _deep(5000, lambda x: (x,)),
        _deep(5000, lambda x: {"k": x}),
        _cyclic(),
        {1: 2},
        {b"a"},
        b"bytes",
        float("nan"),
        float("inf"),
        object(),
        (i for i in range(3)),
        _RaisingIter([1]),
        SimpleNamespace(a=1),
        "x" * 10,
        7,
        True,
        1.5,
        (),
        [],
        {},
    ]


def test_gate_runs_the_real_canonical_encoding():
    with pytest.raises(phase.PhaseContractError):
        _result(payload=10**5000)
    with pytest.raises(phase.PhaseContractError):
        phase.freeze(10**5000)
    with pytest.raises(phase.PhaseContractError):
        phase.PhaseCoverage(rows_examined=10**5000)
    ok = _result(payload=2**63 - 1)
    assert phase.satisfies_required_gate(ok)


def test_forged_oversized_int_fails_gate_and_wire_with_contract_error():
    res = _result(payload=1)
    object.__setattr__(res, "payload", 10**5000)
    assert phase.satisfies_required_gate(res) is False
    with pytest.raises(phase.PhaseContractError):
        phase.to_wire(res)
    with pytest.raises(phase.PhaseContractError):
        phase.canonical_bytes(res)
    cov_res = _result()
    object.__setattr__(cov_res.coverage, "rows_examined", 10**5000)
    assert phase.satisfies_required_gate(cov_res) is False
    with pytest.raises(phase.PhaseContractError):
        phase.canonical_bytes(cov_res)


def test_deep_nesting_is_a_contract_error_not_recursion_error():
    for build in (lambda x: [x], lambda x: (x,), lambda x: {"k": x}):
        with pytest.raises(phase.PhaseContractError):
            phase.freeze(_deep(5000, build))
    with pytest.raises(phase.PhaseContractError):
        phase.freeze(_cyclic())
    res = _result(payload=1)
    object.__setattr__(res, "payload", _deep(5000, lambda x: (x,)))
    assert phase.satisfies_required_gate(res) is False
    with pytest.raises(phase.PhaseContractError):
        phase.to_wire(res)


@pytest.mark.parametrize("make", [_RaisingRepr, lambda: (_RaisingRepr(), 1)])
def test_freeze_set_never_runs_foreign_repr(make):
    elem = make()
    for container in (set, frozenset):
        with pytest.raises(phase.PhaseContractError) as ei:
            phase.freeze(container({elem}))
        assert _MARKER not in str(ei.value)


def test_freeze_set_of_mixed_primitives_is_deterministic():
    assert phase.freeze({3, "a", None, 2.5, True}) == phase.freeze({True, 2.5, None, "a", 3})
    assert phase.freeze({"b", "a"}) == ("a", "b")


def test_default_coverage_is_not_shared():
    first, second = _result(), _result()
    assert first.coverage is not second.coverage
    object.__setattr__(first.coverage, "rows_examined", True)
    assert phase.satisfies_required_gate(first) is False
    assert phase.satisfies_required_gate(second) is True
    assert phase.satisfies_required_gate(_result()) is True
    assert phase.PhaseCoverage() is not phase.PhaseCoverage()


@pytest.mark.parametrize(
    "bad",
    [None, Path("x"), ValueError(_MARKER), 5, 1.5, object(), True, _RaisingRepr(), b"ab", "ab"],
)
def test_malformed_identity_is_contract_error_with_fixed_text(bad):
    with pytest.raises(phase.PhaseContractError) as ei:
        _result(identity=bad)
    assert _MARKER not in str(ei.value)


@pytest.mark.parametrize("bad", [None, Path("x"), ValueError(_MARKER), 5, object()])
def test_frozenmap_non_iterable_is_contract_error(bad):
    with pytest.raises(phase.PhaseContractError) as ei:
        phase.FrozenMap(bad)
    assert _MARKER not in str(ei.value)


def _constructors():
    cov_kw = ("requested_scope", "observed_scope", "rows_examined", "exact", "sampling_method")
    out = [("freeze", lambda h: phase.freeze(h)), ("FrozenMap", lambda h: phase.FrozenMap(h))]
    out += [(f"coverage.{k}", lambda h, k=k: phase.PhaseCoverage(**{k: h})) for k in cov_kw]
    res_kw = ("check_id", "method", "method_version", "payload", "identity", "reasons", "coverage")
    out += [(f"result.{k}", lambda h, k=k: _result(**{k: h})) for k in res_kw]
    out += [
        ("result.phase", lambda h: _result(phase=h)),
        ("result.status", lambda h: _result(status=h)),
    ]
    out += [("event.event", lambda h: phase.PhaseEvent("post_scan", h, "passed"))]
    out += [("event.reason", lambda h: phase.PhaseEvent("post_scan", "e", "passed", h))]
    return out


@pytest.mark.parametrize("name,build", _constructors(), ids=[n for n, _ in _constructors()])
def test_hostile_inputs_only_ever_raise_contract_error(name, build):
    for hostile in _hostile():
        try:
            built = build(hostile)
        except phase.PhaseContractError as exc:
            assert _MARKER not in str(exc), (name, type(hostile))
            continue
        if isinstance(built, (phase.PhaseResult, phase.PhaseEvent)):
            for call in (phase.to_wire, phase.canonical_bytes):
                try:
                    call(built)
                except phase.PhaseContractError:
                    pass
            if phase.satisfies_required_gate(built):
                assert isinstance(phase.canonical_bytes(built), bytes)
        elif isinstance(built, phase.PhaseCoverage):
            res = _result(coverage=built)
            if phase.satisfies_required_gate(res):
                assert isinstance(phase.canonical_bytes(res), bytes)


@pytest.mark.parametrize("hostile", _hostile(), ids=lambda h: type(h).__name__)
def test_forged_fields_never_escape_as_other_exception(hostile):
    for field in ("payload", "identity", "reasons", "check_id", "coverage"):
        res = _result()
        object.__setattr__(res, field, hostile)
        for call in (phase.to_wire, phase.canonical_bytes):
            try:
                call(res)
            except phase.PhaseContractError as exc:
                assert _MARKER not in str(exc)
        assert phase.satisfies_required_gate(res) in (True, False)


# ---- r7: every field of every public envelope, contained end to end -----------------------------

_R7 = "R7-MARKER-do-not-leak"


def _r7_scalars() -> list:
    # Ordinary values only; hostile-dunder types live in test_v2_hostile_deferred.py.
    return [
        _R7,
        10**5000,
        -(10**5000),
        2**63,
        None,
        Path(_R7),
        ValueError(_R7),
        RuntimeError(_R7),
        ReasonRef(_R7, _R7),
        ReasonRef("post_scan", _R7),
        phase.PhaseCoverage(),
        _deep(5000),
        _deep(5000, lambda x: (x,)),
        _deep(5000, lambda x: {"k": x}),
        _cyclic(),
        float("nan"),
        b"bytes",
        object(),
    ]


def _r7_variants(h) -> list:
    # The hostile value itself and the container positions it can hide in.
    out = [h, (h,), [h], {"k": h}, (("k", h),), ((_R7, "v"),)]
    try:
        out.append({h})
    except Exception:
        pass
    return out


def _envelope_types() -> list[type]:
    found = [
        obj
        for name, obj in vars(phase).items()
        if not name.startswith("_")
        and isinstance(obj, type)
        and dataclasses.is_dataclass(obj)
        and obj.__module__ == phase.__name__
    ]
    assert {phase.PhaseCoverage, phase.PhaseResult, phase.PhaseEvent} <= set(found)
    return found


def _valid(cls: type):
    if cls is phase.PhaseCoverage:
        return phase.PhaseCoverage()
    if cls is phase.PhaseResult:
        return _result()
    if cls is phase.PhaseEvent:
        return phase.PhaseEvent("post_scan", "done", "passed")
    raise AssertionError(f"no valid builder for new envelope type {cls.__name__}")


def _ctor_kwargs(cls: type) -> dict:
    v = _valid(cls)
    return {f.name: getattr(v, f.name) for f in dataclasses.fields(cls)}


def _forge(cls: type, name: str, value):
    # A forged coverage is only reachable through a result that already holds it.
    holder = _result() if cls is phase.PhaseCoverage else _valid(cls)
    target = holder.coverage if cls is phase.PhaseCoverage else holder
    object.__setattr__(target, name, value)
    return holder


def _exercise(call, label, escapes):
    try:
        return call()
    except phase.PhaseContractError as exc:
        if _R7 in str(exc) or _R7 in repr(exc.args):
            escapes.append((label, "leaked input text"))
    except Exception as exc:  # every foreign escape is the finding
        escapes.append((label, type(exc).__name__))
    return None


def _check_exports(obj, label, escapes):
    if isinstance(obj, phase.PhaseCoverage):
        obj = _exercise(lambda: _result(coverage=obj), f"{label}:result", escapes)
        if obj is None:
            return
    env = obj
    for fn in (phase.to_wire, phase.canonical_bytes, phase.satisfies_required_gate):
        _exercise(lambda fn=fn: fn(env), f"{label}:{fn.__name__}", escapes)


def _r7_escapes(scalars: list | None = None) -> tuple[list, set]:
    escapes: list = []
    exercised: set = set()
    hostiles = [v for h in (_r7_scalars() if scalars is None else scalars) for v in _r7_variants(h)]
    for h in hostiles:
        _exercise(lambda h=h: phase.freeze(h), "freeze", escapes)
        _exercise(lambda h=h: phase.FrozenMap(h), "FrozenMap", escapes)
        _exercise(lambda h=h: phase.FrozenMap([h]), "FrozenMap[pair]", escapes)
        for cls in _envelope_types():
            for f in dataclasses.fields(cls):
                label = f"{cls.__name__}.{f.name}"
                exercised.add(label)
                kw = {**_ctor_kwargs(cls), f.name: h}
                built = _exercise(lambda cls=cls, kw=kw: cls(**kw), f"{label}:ctor", escapes)
                if built is not None:
                    _check_exports(built, f"{label}:built", escapes)
                _check_exports(_forge(cls, f.name, h), f"{label}:forged", escapes)
    return escapes, exercised


def test_every_envelope_field_is_walked_and_only_raises_fixed_contract_error():
    escapes, exercised = _r7_escapes()
    expected = {f"{c.__name__}.{f.name}" for c in _envelope_types() for f in dataclasses.fields(c)}
    assert exercised == expected and len(expected) >= 22
    assert sorted(set(escapes)) == []


@pytest.mark.parametrize(
    ("owner", "attr"),
    # PhaseCoverage.__post_init__ is only reachable with hostile dunders: test_v2_hostile_deferred.
    [(phase.FrozenMap, "__new__")],
)
def test_catch_all_detects_containment_removed_from_a_constructor(monkeypatch, owner, attr):
    raw = owner.__dict__[attr]
    raw = raw.__func__ if isinstance(raw, staticmethod) else raw
    unwrapped = raw.__wrapped__
    monkeypatch.setattr(owner, attr, staticmethod(unwrapped) if attr == "__new__" else unwrapped)
    escapes, _ = _r7_escapes()
    assert escapes, f"removing containment from {owner.__name__}.{attr} went undetected"


def test_unknown_header_and_reason_errors_never_echo_the_input():
    for res in (
        _result(status=_R7),
        _result(phase=_R7),
        _result(schema_version=_R7),
        _result(reasons=(ReasonRef("post_scan", _R7),)),
        phase.PhaseEvent("post_scan", "e", "passed", ReasonRef(_R7, "x")),
    ):
        with pytest.raises(phase.PhaseContractError) as ei:
            phase.canonical_bytes(res)
        assert _R7 not in str(ei.value)
