"""The physical-execution adapter seam (Task 4.2 of the execution-consolidation
program, docs/plans/2026-09-09-execution-consolidation-and-native-throughput.md).

REPRESENTATION-ONLY, ADDITIVE. This package models today's six table drivers
(docs/plans/2026-09-13-physical-plan-design.md section 4) and their node-
operator families (section 5) as a parallel seam that Tasks 4.3 (compiler) and
4.4 (shadow coordinator) build against. It changes NOTHING about masked output,
determinism, publication, or route selection:

  * every adapter here PURE-DELEGATES to the exact production entry point it
    names (`_pandas_adapter.PandasExecutionAdapter.run` / the selected
    `ExecutionAdapter`, `_sequential.run_sequential`,
    `_chunked.run_mask_pipeline_chunked`, `native._dispatch.
    run_native_or_oracle_chunked`, `_pipeline_route_exec.run_mask_chunked`,
    `_native_route_exec.try_native_route`, `out_of_core._runner.
    run_fk_out_of_core`, `generation._plan_entry.generate_tables`) and returns
    its result UNCHANGED -- no re-aggregation, no result mutation, no
    fallback/publication ownership taken over;
  * Task 4.5 (engine production-readiness) adds a single sanctioned production
    connection: `execution/_unified_slice.py` diverts bounded non-FK single-
    Parquet-table masks (strategies passthrough/redact/truncate/keyed-hash on
    resident pa.Table sources) inside `run_pipeline` to route through the
    physical plan + coordinator, returning an ExecutionResult identical to the
    pandas path. The lane is default-OFF per-run flag; caller activation is
    Task 4.6. Every other route/coordinator/executor module remains disconnected
    (enforced by `tests/sentry/test_physical_seam_disconnection.py`).

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
  `_reasons`        -- Task 4.3 D3: the frozen decision-code catalog.
  `_inputs`         -- Task 4.3 D1: `PhysicalPlanInputs`, the native-
                       admission captured fact, and the OOC routing facts.
  `_plan`           -- Task 4.3 D2 output records: `PhysicalPlan` /
                       `PhysicalTable` / `PhysicalNode` / `SynthesisStage`.
  `_compiler`       -- Task 4.3 D2: `compile_physical_plan`, pure; reached in
                       production only via the Task 4.5 default-OFF unified-
                       slice lane, never any other route.
  `_snapshot`        -- Task 4.3 D1: `capture_physical_plan_inputs`, the
                       real (non-synthesized) preflight-only snapshot builder.
  `_shadow_bindings` -- Task 4.4 C0: slice-only `ExecutionBinding`
                       construction for `PhysicalNode`, called from the
                       compiler.
  `_shadow_context`  -- Task 4.4 C0: `ShadowContext`, the runtime-only
                       carrier for the resolved mask key + resource budget.
  `_shadow_operators` -- Task 4.4 C2: direct dispatch to the four native
                       slice kernels, plus per-node route evidence.
  `_shadow_snapshot` -- Task 4.4 C5: `ShadowSnapshot` + its non-sensitive
                       snapshot-identity digest.
  `_shadow_coordinator` -- Task 4.4 C1/C6: `ShadowCoordinator`, the
                       no-publish shadow-mode coordinator.
  `_shadow_diff_codes` -- Task 4.4 C4: the frozen shadow difference/failure
                       code catalog + `ShadowDifference`.
  `_activation`      -- Task 4.5 D6: `UnifiedSliceActivation`, the frozen
                       PLANNED activation overlay the unified-slice
                       production lane builds before executing.
  `_live_inputs`     -- Task 4.5 D4: `build_live_physical_plan_inputs`, the
                       unified slice's own `PhysicalPlanInputs` constructor
                       (built from `run_pipeline`'s already-produced facts,
                       never by re-running `profile_source`/`compile_plan`).
"""

from __future__ import annotations

from decoy_engine.execution.physical._activation import (
    ACTIVATION_VERSION,
    AdmittedNode,
    UnifiedSliceActivation,
    build_unified_slice_activation,
)
from decoy_engine.execution.physical._capabilities import CAPABILITIES, DriverCapabilities
from decoy_engine.execution.physical._compiler import DriverSelection, compile_physical_plan
from decoy_engine.execution.physical._context import SeamContext
from decoy_engine.execution.physical._inputs import (
    NativeAdmissionFact,
    OutOfCoreRoutingFacts,
    PhysicalPlanInputs,
    capture_native_admission_fact,
)
from decoy_engine.execution.physical._live_inputs import build_live_physical_plan_inputs
from decoy_engine.execution.physical._plan import (
    ExecutionBinding,
    KeyBinding,
    PhysicalNode,
    PhysicalPlan,
    PhysicalTable,
    RejectedAlternative,
    SynthesisStage,
)
from decoy_engine.execution.physical._protocols import (
    BoundedPythonOperator,
    NativeScalarKeyedOperator,
    PandasCompositeOperator,
    PandasFkResolveOperator,
    PandasScalarOperator,
    TableDriver,
)
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator, ShadowRunResult
from decoy_engine.execution.physical._shadow_diff_codes import DIFFERENCE_CODES, ShadowDifference
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence, run_operator
from decoy_engine.execution.physical._shadow_snapshot import (
    ShadowSnapshot,
    capture_shadow_snapshot,
    snapshot_identity,
)
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.execution.physical._types import (
    DriverId,
    ExecutionScope,
    OperatorFamily,
    PublicationMode,
    Residency,
    Substrate,
)

__all__ = [
    "ACTIVATION_VERSION",
    "CAPABILITIES",
    "DIFFERENCE_CODES",
    "AdmittedNode",
    "BoundedPythonOperator",
    "DriverCapabilities",
    "DriverId",
    "DriverSelection",
    "ExecutionBinding",
    "ExecutionScope",
    "KeyBinding",
    "NativeAdmissionFact",
    "NativeScalarKeyedOperator",
    "OperatorCallEvidence",
    "OperatorFamily",
    "OutOfCoreRoutingFacts",
    "PandasCompositeOperator",
    "PandasFkResolveOperator",
    "PandasScalarOperator",
    "PhysicalNode",
    "PhysicalPlan",
    "PhysicalPlanInputs",
    "PhysicalTable",
    "PublicationMode",
    "RejectedAlternative",
    "Residency",
    "SeamContext",
    "ShadowContext",
    "ShadowCoordinator",
    "ShadowDifference",
    "ShadowRunResult",
    "ShadowSnapshot",
    "Substrate",
    "SynthesisStage",
    "TableDriver",
    "UnifiedSliceActivation",
    "build_live_physical_plan_inputs",
    "build_unified_slice_activation",
    "capture_native_admission_fact",
    "capture_physical_plan_inputs",
    "capture_shadow_snapshot",
    "compile_physical_plan",
    "run_operator",
    "snapshot_identity",
]
