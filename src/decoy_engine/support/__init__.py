"""`decoy_engine.support`: pseudonymised support-bundle types (F32).

A support bundle describes a run's structure (versions, limits, routes, timings, coded errors,
topology) with no cell value, name, path or free text. Names become ordinal labels from a
per-build `LabelAllocator`. Decoy never uploads a bundle; the operator reviews and sends it.

The engine enums a bundle carries (`RouteDeclineCode`, `PIIClass`, `Severity`, `Outcome`,
`RetryClass`) are re-exported here so consumers never import a private module.
"""

from __future__ import annotations

from decoy_engine.execution.native._route_decline_codes import RouteDeclineCode
from decoy_engine.profile._types import PIIClass
from decoy_engine.reasons import Outcome, RetryClass, Severity
from decoy_engine.support._shape import (
    BundleSchema,
    ColumnShape,
    CountKey,
    DtypeClass,
    EdgeShape,
    ErrorFact,
    EventFact,
    Label,
    Limits,
    NativeStatus,
    NodeTiming,
    OrphanPolicy,
    Phase,
    PhaseTiming,
    PlatformFamily,
    RatioBucket,
    ReasonRef,
    RouteFact,
    RouteKind,
    StrategyKind,
    SupportBundle,
    TableShape,
    Timings,
    Topology,
    Version,
    Versions,
)

__all__ = [
    "BundleSchema",
    "ColumnShape",
    "CountKey",
    "DtypeClass",
    "EdgeShape",
    "ErrorFact",
    "EventFact",
    "Label",
    "Limits",
    "NativeStatus",
    "NodeTiming",
    "OrphanPolicy",
    "Outcome",
    "PIIClass",
    "Phase",
    "PhaseTiming",
    "PlatformFamily",
    "RatioBucket",
    "ReasonRef",
    "RetryClass",
    "RouteDeclineCode",
    "RouteFact",
    "RouteKind",
    "Severity",
    "StrategyKind",
    "SupportBundle",
    "TableShape",
    "Timings",
    "Topology",
    "Version",
    "Versions",
]
