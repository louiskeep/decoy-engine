"""The ``decoy-run-result/v1`` run record: model (write and read) and the status derivation.

The run-record names are re-exported from ``decoy_engine``; the projection names
(``project_tables`` and its fact types) are public here only. The value-free leaf types (``Token``,
``SafeName`` and the rest) are exported here for the sibling ``records`` package, which shares
them; they are not part of the top-level API. The per-table projection lives in
``_project.py``, the progress collector in ``_collect.py`` and the builder in ``_build.py``.
"""

from decoy_engine.run_result._build import FailureInput, RouteView, RunFacts, build_run_result
from decoy_engine.run_result._collect import RunResultCollector
from decoy_engine.run_result._model import (
    RUN_RESULT_SCHEMA,
    BaselineLink,
    ConfigRef,
    EvidenceLink,
    InvocationId,
    Links,
    LogLink,
    NativeRoute,
    RelPath,
    RunResult,
    RunResultRead,
    SafeName,
    SafeRef,
    Sha256,
    Token,
    UtcTimestamp,
    Version,
)
from decoy_engine.run_result._outcome import derive_outcome
from decoy_engine.run_result._project import (
    CollectedFacts,
    DeliveryFacts,
    ProjectedTables,
    TableDelivery,
    project_tables,
)

__all__ = [
    "RUN_RESULT_SCHEMA",
    "BaselineLink",
    "CollectedFacts",
    "ConfigRef",
    "DeliveryFacts",
    "EvidenceLink",
    "FailureInput",
    "InvocationId",
    "Links",
    "LogLink",
    "NativeRoute",
    "ProjectedTables",
    "RelPath",
    "RouteView",
    "RunFacts",
    "RunResult",
    "RunResultCollector",
    "RunResultRead",
    "SafeName",
    "SafeRef",
    "Sha256",
    "TableDelivery",
    "Token",
    "UtcTimestamp",
    "Version",
    "build_run_result",
    "derive_outcome",
    "project_tables",
]
