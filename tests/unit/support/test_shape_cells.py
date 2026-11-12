"""F32 E1 negative cells for the closed shape checks, built with real allocator labels."""

from __future__ import annotations

import copy
import dataclasses
import pickle

import pytest

from decoy_engine.reasons import route
from decoy_engine.support import (
    BundleSchema,
    ColumnShape,
    CountKey,
    DtypeClass,
    EdgeShape,
    ErrorFact,
    EventFact,
    LabelAllocator,
    Limits,
    NativeStatus,
    NodeTiming,
    OrphanPolicy,
    Outcome,
    Phase,
    PhaseTiming,
    PlatformFamily,
    RatioBucket,
    RetryClass,
    RouteDeclineCode,
    RouteFact,
    RouteKind,
    Severity,
    StrategyKind,
    SupportBundle,
    TableShape,
    Timings,
    Topology,
    Version,
    Versions,
)

BAD = (TypeError, ValueError)
A = LabelAllocator()
T1, T2 = A.table("a"), A.table("b")
C1, C2, D1 = A.column("a", "x"), A.column("a", "y"), A.column("b", "z")
N1 = A.node("n")
REASON = route.FK_RELATIONSHIP.spec.ref


def _col(label: object) -> ColumnShape:
    return ColumnShape(
        label=label,  # type: ignore[arg-type]
        dtype=DtypeClass.INT,
        strategy=StrategyKind.HASH,
        nullable=RatioBucket.ZERO,
        distinct=RatioBucket.UNKNOWN,
        length_log2=None,
        pk=False,
        fk=False,
        pii=None,
    )


def _edge(**kw: object) -> EdgeShape:
    base: dict[str, object] = {
        "parent": T1,
        "parent_cols": (C1,),
        "child": T2,
        "child_cols": (D1,),
        "arity": 1,
        "orphan_policy": None,
    }
    return EdgeShape(**{**base, **kw})  # type: ignore[arg-type]


def _bundle() -> SupportBundle:
    versions = Versions(
        engine=Version("0.18.0"),
        sdk=Version("1.0"),
        registry=Version("1.3.0"),
        product=Version("0.18.0"),
        python=Version("3.10.20"),
        seed_protocol=1,
        platform=PlatformFamily.LINUX,
        native=NativeStatus.ABSENT,
    )
    return SupportBundle(
        versions=versions,
        limits=Limits(threads=2, peak_memory_mib=None),
        routes=(
            RouteFact(
                node=N1,
                table=T1,
                route=RouteKind.UNIFIED_SLICE,
                native_admitted=True,
                declines=(RouteDeclineCode(sorted(c.value for c in RouteDeclineCode)[0]),),
            ),
        ),
        timings=Timings(
            phases=(PhaseTiming(Phase.READ, 5), PhaseTiming(Phase.EXECUTE, 9)),
            nodes=(NodeTiming(N1, 3),),
        ),
        errors=(ErrorFact(N1, T1, REASON, Outcome.EXECUTION, RetryClass.NEVER),),
        topology=Topology(
            tables=(TableShape(T1, 10, (_col(C1), _col(C2))), TableShape(T2, None, (_col(D1),))),
            edges=(_edge(orphan_policy=next(iter(OrphanPolicy))),),
        ),
        events=(
            EventFact(Severity.WARNING, REASON, None, ((CountKey.ERRORS, 3), (CountKey.ROWS, 4))),
        ),
    )


def test_full_bundle_builds_and_survives_pickle_copy_asdict() -> None:
    bundle = _bundle()
    assert bundle.schema is BundleSchema.V1
    assert pickle.loads(pickle.dumps(bundle)) == bundle  # noqa: S301
    assert copy.deepcopy(bundle) == bundle
    assert dataclasses.asdict(bundle)["topology"]["tables"][0]["label"] == "t1"


def test_table_columns_owned_and_unique() -> None:
    for columns in ((_col(D1),), (_col(C1), _col(C1))):
        with pytest.raises(ValueError):
            TableShape(T1, None, columns)


def test_edge_arity_and_ownership() -> None:
    for kw in (
        {"arity": 2},
        {"arity": 0},
        {"arity": 9},
        {"parent_cols": (D1,)},
        {"child_cols": (C1,)},
        {"parent_cols": (C1, C2)},
    ):
        with pytest.raises(BAD):
            _edge(**kw)


def test_route_declines_sorted_and_unique() -> None:
    codes = sorted(RouteDeclineCode, key=lambda c: c.value)
    for declines in ((codes[1], codes[0]), (codes[0], codes[0])):
        with pytest.raises(ValueError):
            RouteFact(N1, T1, RouteKind.UNKNOWN, None, declines)


def test_event_counts_sorted_and_unique() -> None:
    for counts in (
        ((CountKey.ROWS, 1), (CountKey.ERRORS, 1)),
        ((CountKey.ROWS, 1), (CountKey.ROWS, 2)),
    ):
        with pytest.raises(ValueError):
            EventFact(Severity.WARNING, REASON, None, counts)


def test_int_ranges_are_tight() -> None:
    for ms in (-1, 2**53, 2**63, -(2**63)):
        with pytest.raises(ValueError):
            PhaseTiming(Phase.READ, ms)
        with pytest.raises(ValueError):
            NodeTiming(N1, ms)
    assert PhaseTiming(Phase.READ, 2**53 - 1).ms == 2**53 - 1
    nine = tuple(A.column("a", f"wide{i}") for i in range(9))
    for arity, cols in ((0, ()), (9, nine)):  # lengths agree, so only the range can refuse
        with pytest.raises(ValueError):
            _edge(arity=arity, parent_cols=cols, child_cols=tuple(A.column("b", c) for c in cols))


def test_none_refused_in_non_optional_fields() -> None:
    good = _bundle()
    for name in ("versions", "limits"):
        with pytest.raises(TypeError):
            dataclasses.replace(good, **{name: None})
    with pytest.raises(TypeError):
        _col(None)
    with pytest.raises(TypeError):
        RouteFact(N1, T1, None, None, ())  # type: ignore[arg-type]


def test_timings_phase_repeats_refused() -> None:
    with pytest.raises(ValueError):
        Timings(phases=(PhaseTiming(Phase.READ, 1), PhaseTiming(Phase.READ, 2)), nodes=())
