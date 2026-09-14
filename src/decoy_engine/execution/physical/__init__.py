"""The physical-execution adapter seam (Task 4.2 of the execution-consolidation
program, docs/plans/2026-09-09-execution-consolidation-and-native-throughput.md).

REPRESENTATION-ONLY, ADDITIVE, DISCONNECTED. This package models today's six
table drivers (docs/plans/2026-09-13-physical-plan-design.md section 4) and
their node-operator families (section 5) as a parallel seam that Tasks 4.3
(compiler) and 4.4 (shadow coordinator) will build against. It changes NOTHING
about masked output, determinism, publication, or route selection:

  * every adapter here PURE-DELEGATES to the exact production entry point it
    names (`_pandas_adapter.PandasExecutionAdapter.run` / the selected
    `ExecutionAdapter`, `_sequential.run_sequential`,
    `_chunked.run_mask_pipeline_chunked`, `native._dispatch.
    run_native_or_oracle_chunked`, `_pipeline_route_exec.run_mask_chunked`,
    `_native_route_exec.try_native_route`, `out_of_core._runner.
    run_fk_out_of_core`, `generation._plan_entry.generate_tables`) and returns
    its result UNCHANGED -- no re-aggregation, no result mutation, no
    fallback/publication ownership taken over;
  * `run_pipeline` and every current route/coordinator/executor module never
    import this package (enforced by `tests/sentry/test_physical_seam_
    disconnection.py`); production activation is a later task (4.5+).

Package layout:
  `_types`         -- seam identifiers: `DriverId`, `ExecutionScope`,
                       `Residency`, `PublicationMode`, `Substrate`,
                       `OperatorFamily` (design doc sections 2-5).
  `_context`        -- `SeamContext`: the per-invocation identity carrier this
                       seam owns (deliberately NOT named `ExecutionContext`,
                       which is the distinct public caller-context class at
                       `decoy_engine.context.ExecutionContext`).
  `_capabilities`   -- `DriverCapabilities` + the frozen `CAPABILITIES` table,
                       one entry per driver (design doc section 4 table).
  `_protocols`      -- the `BatchOperator` family protocols (design doc
                       section 5 / plan C2) and the `TableDriver` protocol.
  `drivers/`        -- the six delegating adapters (plan D2).
"""

from __future__ import annotations

from decoy_engine.execution.physical._capabilities import CAPABILITIES, DriverCapabilities
from decoy_engine.execution.physical._context import SeamContext
from decoy_engine.execution.physical._protocols import (
    BoundedPythonOperator,
    NativeScalarKeyedOperator,
    PandasCompositeOperator,
    PandasFkResolveOperator,
    PandasScalarOperator,
    TableDriver,
)
from decoy_engine.execution.physical._types import (
    DriverId,
    ExecutionScope,
    OperatorFamily,
    PublicationMode,
    Residency,
    Substrate,
)

__all__ = [
    "CAPABILITIES",
    "BoundedPythonOperator",
    "DriverCapabilities",
    "DriverId",
    "ExecutionScope",
    "NativeScalarKeyedOperator",
    "OperatorFamily",
    "PandasCompositeOperator",
    "PandasFkResolveOperator",
    "PandasScalarOperator",
    "PublicationMode",
    "Residency",
    "SeamContext",
    "Substrate",
    "TableDriver",
]
