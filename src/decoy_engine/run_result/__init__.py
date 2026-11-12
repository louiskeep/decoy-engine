"""The ``decoy-run-result/v1`` run record: model (write and read) and the status derivation.

Public names are re-exported from ``decoy_engine``. The value-free leaf types (``Token``,
``SafeName`` and the rest) are exported here for the sibling ``records`` package, which shares
them; they are not part of the top-level API. The per-table projection lives in ``_project.py``; the builder and collector arrive in later
slices of the same package.
"""

from decoy_engine.run_result._model import (
    RUN_RESULT_SCHEMA,
    InvocationId,
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
    "CollectedFacts",
    "DeliveryFacts",
    "ProjectedTables",
    "TableDelivery",
    "project_tables",
    "RUN_RESULT_SCHEMA",
    "InvocationId",
    "RelPath",
    "RunResult",
    "RunResultRead",
    "SafeName",
    "SafeRef",
    "Sha256",
    "Token",
    "UtcTimestamp",
    "Version",
    "derive_outcome",
]
