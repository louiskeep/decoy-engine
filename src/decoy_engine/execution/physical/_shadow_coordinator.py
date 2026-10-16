"""Task 4.4 C1/C2/C6 (extended by Task 4.6 slices 1 and 3): `ShadowCoordinator`
-- the unified batch coordinator, run in SHADOW mode, for the bounded slice
(design doc section 8.3's actions minus publish).

`ShadowCoordinator` has no sink, publisher, or target argument anywhere in
its constructor or `run` -- not a stubbed no-op, an argument that does not
exist. Publication is structurally impossible from this class, not merely
skipped (C1, C7).

Assembly reproduces the pandas oracle's own schema-inference quirks for the
two degenerate shapes this slice's acceptance corpus exercises: a zero-row
column and an all-null (non-empty) column. Both are pinned from a real
`run_pipeline(substrate="pandas")` probe run at build time, not guessed, and
`tests/physical/test_shadow_corpus.py` re-verifies every one of them against
the live oracle -- this module never redefines the oracle's answer, it
reproduces it.

Task 4.6 slice 1 adds the deterministic-faker lifecycle: the compiled index
kernel is loaded at most ONCE per `run()` call, lazily, the first time a
bound faker node is reached, and BEFORE that node's pool is built; every
faker node's pool is resolved ONCE per unique `PoolIdentity` via a
run-scoped map (never per batch, never keyed by node_id -- two nodes sharing
an identity share one build), backed by a FRESH `PoolCache` scoped to this
one `run()` call, never the module-global default cache (whose identity
space omits the registry/backend version, so a stale cross-run hit could
silently pass as a build).

Task 4.6 slice 3 adds an OUT_OF_CORE dispatch branch: when a compiled plan's
mask-table driver set is exactly `{DriverId.OUT_OF_CORE}`, `run()` skips the
per-node loop entirely and dispatches the whole relationship-JOB through the
existing Task 4.2 `OutOfCoreAdapter`, which delegates to `run_fk_out_of_core`
-- the FK machinery stays single-owner there, never reimplemented here. A
driver set mixing OUT_OF_CORE with any other masking driver, or pairing it
with a synthesis stage, is refused rather than dispatched partially.

Task 4.6 slice 5a adds a PURE-GENERATE dispatch branch: when a compiled
plan has no mask tables at all (`plan.tables` empty) and does carry a
synthesis stage, `run()` skips both the per-node loop and the OOC branch and
dispatches through the existing Task 4.2 `SynthesisStageAdapter`, which
delegates to `generate_tables` -- never reimplemented here. The admission
gate + adapter call themselves live in `_shadow_generation.py` (split out to
keep this module under the 600-LOC orchestration cap); `_dispatch_synthesis`
below is a thin wrap of it.

Task 4.6 slice 5b-i adds the INDEPENDENT-MIXED dispatch branch: when a
compiled plan has BOTH mask tables and a synthesis stage, `run()` dispatches
through `_shadow_mixed.dispatch_mixed`, which admits the job via its own
contract (no generate->mask FK edge, no OUT_OF_CORE mask driver, no
validators/quarantine/vault/fidelity), runs generation via the same adapter
path slice 5a uses, reuses THIS class's own per-node loop for the mask half
(by recursing into `run()` with `synthesis` stripped off the plan), and
stitches the two outputs via the shared `execution._stitch` helper. A shape
that dispatch does not admit (a generate->mask FK coupling, an OOC mask
driver, ...) declines coded rather than silently masking only part of the
job -- see `_shadow_mixed.py` for the full contract. Task 4.6 slice 6 (the LAST
masking slice) wraps `FullFrameAdapter` via `_shadow_full_frame.py`.

The default per-node/per-batch loop, including the kernel and pool lifecycle
described above, lives in `_shadow_batch_runner.run_shadow_batches`, which
`run` falls through to; `ShadowRunResult` lives in `_shadow_result.py` and is
re-exported here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.execution.physical._context import SeamContext
from decoy_engine.execution.physical._plan import PhysicalPlan, SynthesisStage
from decoy_engine.execution.physical._shadow_batch_runner import run_shadow_batches
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_diff_codes import (
    MIXED_DRIVER_UNSUPPORTED,
    OOC_DISPATCH_MISSING_DEPENDENCY,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_result import ShadowRunResult
from decoy_engine.execution.physical._shadow_snapshot import ShadowSnapshot
from decoy_engine.execution.physical._types import DriverId
from decoy_engine.generation.pool import PoolCache

if TYPE_CHECKING:
    from collections.abc import Mapping

    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph
    from decoy_engine.relationships._graph import RelationshipEdge

__all__ = ["ShadowCoordinator", "ShadowRunResult"]


def _privacy_safe_driver_set(drivers: set[DriverId]) -> str:
    """A `MIXED_DRIVER_UNSUPPORTED` detail: driver NAMES only, sorted for a
    stable message -- never a table or column identifier."""
    return ",".join(sorted(d.value for d in drivers))


def _adapt_ooc_result(
    execution_result: ExecutionResult, seam_context: SeamContext
) -> ShadowRunResult:
    """Pure `ExecutionResult` -> `ShadowRunResult` conversion for the
    OUT_OF_CORE dispatch branch (Task 4.6 slice 3). Reshapes nothing: the
    SAME `pa.Table` objects `OutOfCoreAdapter.run` produced pass straight
    through `outputs`, so a value-equal fold in a comparison harness could
    never hide a mutation this step introduced -- there is none to hide.
    `route_evidence` is empty (an OOC table's nodes carry `execution=None` by
    construction; there is no per-node evidence to report). `quality_metrics`
    (Task 4.6 slice 4) forwards by reference, same as `warnings`/`row_errors`
    -- `run_fk_out_of_core`'s code_set corpus-provenance evidence would
    otherwise be silently dropped here, the one real gap this slice closes.
    """
    return ShadowRunResult(
        outputs=dict(execution_result.outputs),
        route_evidence={},
        warnings=execution_result.warnings,
        row_errors=execution_result.row_errors,
        driver_invocation=seam_context,
        quality_metrics=execution_result.quality_metrics,
    )


@dataclass
class ShadowCoordinator:
    """Runs a C0-extended `PhysicalPlan` over a resident `ShadowSnapshot`.
    Carries no sink/publisher/target dependency at all -- neither the
    constructor nor `run` accepts one.

    `registry` (Task 4.6 slice 1) is the EXACT resolved `ProviderRegistry` a
    bound faker node's pool must build against -- the SAME registry the
    oracle used for the same job, never `get_default_registry()` (a
    caller-overridden registry builds different values under the same
    provider name, so falling back to the default would silently diverge
    from a custom-registry oracle run). Optional; a bound faker node asserts
    the registry is present rather than silently masking with the wrong one.

    `pool_cache`, when given, replaces the run-scoped fresh cache: the unified
    lane shares one job-scoped cache with the oracle so a pool is built once.
    """

    ctx: ShadowContext
    registry: ProviderRegistry | None = None
    pool_cache: PoolCache | None = None

    def run(
        self,
        plan: PhysicalPlan,
        snapshot: ShadowSnapshot,
        *,
        admitted_edges: tuple[RelationshipEdge, ...] = (),
        when_masks: Mapping[str, pa.Array] | None = None,
    ) -> ShadowRunResult:
        # Task 4.6 slice 5a: a PURE-GENERATE plan (no mask tables at all)
        # dispatches through the Task 4.2 SynthesisStageAdapter instead of
        # every branch below -- see _dispatch_synthesis. Checked first, and
        # unconditionally on plan.tables being empty, so every existing
        # scalar/chunked/OOC plan (which never has an empty table list AND a
        # synthesis stage at once outside the mixed case handled next) falls
        # straight through, byte-unchanged.
        if not plan.tables and plan.synthesis is not None:
            return self._dispatch_synthesis(plan.synthesis, snapshot)
        if plan.tables and plan.synthesis is not None:
            # Task 4.6 slice 5b-i/5b-ii: a MIXED plan (both present),
            # independent or FK-coupled. `_dispatch_mixed` admits via its own
            # contract, runs generation, reuses THIS loop for the mask half
            # (recursing into `run()` with `synthesis` stripped and an
            # admitted-edge allowlist), and stitches -- see `_shadow_mixed.py`.
            return self._dispatch_mixed(plan, snapshot)

        # Task 4.6 slice 3: an OUT_OF_CORE mask-table plan dispatches through
        # the Task 4.2 `OutOfCoreAdapter` (which owns the FK machinery via
        # `run_fk_out_of_core`) rather than running this loop -- the driver
        # set is computed ONCE, up front, so every existing scalar/chunked
        # plan (whose driver set never contains OUT_OF_CORE) falls straight
        # through to the loop below, byte-unchanged.
        mask_drivers = {table.driver for table in plan.tables}
        if mask_drivers == {DriverId.OUT_OF_CORE}:
            return self._dispatch_out_of_core(plan, snapshot)
        if mask_drivers == {DriverId.FULL_FRAME}:
            from decoy_engine.execution.physical import _shadow_full_frame

            result = _shadow_full_frame.dispatch_full_frame_if_applicable(self, plan, snapshot)
            if result is not None:
                return result
        if DriverId.OUT_OF_CORE in mask_drivers:
            # OUT_OF_CORE mixed with any other masking driver: never mask
            # part of a plan through the adapter and the rest scalar.
            raise ShadowDifference(
                code=MIXED_DRIVER_UNSUPPORTED, detail=_privacy_safe_driver_set(mask_drivers)
            )

        return run_shadow_batches(
            self, plan, snapshot, admitted_edges=admitted_edges, when_masks=when_masks
        )

    def _dispatch_synthesis(
        self, physical_synthesis: SynthesisStage, snapshot: ShadowSnapshot
    ) -> ShadowRunResult:
        """Thin wrap of `_shadow_generation.dispatch_synthesis` (moved out to
        keep this module under the 600-LOC orchestration cap; see that
        module's docstring for the admission gate + dispatch contract) into
        this class's own `ShadowRunResult` shape. The SAME table objects
        `SynthesisStageAdapter.run` produced pass straight through
        `outputs`; `route_evidence`/`warnings`/`row_errors`/`quality_metrics`
        stay at their empty defaults -- this slice's admitted shape
        (sequence/categorical, no validators/quarantine) produces none of
        any of them on either side.
        """
        from decoy_engine.execution.physical._shadow_generation import dispatch_synthesis

        outputs, seam_context = dispatch_synthesis(self.ctx, physical_synthesis, snapshot)
        return ShadowRunResult(
            outputs=dict(outputs), route_evidence={}, driver_invocation=seam_context
        )

    def _dispatch_mixed(self, plan: PhysicalPlan, snapshot: ShadowSnapshot) -> ShadowRunResult:
        """Thin wrap of `_shadow_mixed.dispatch_mixed` (moved out to keep
        this module under the 600-LOC orchestration cap; see that module's
        docstring for the admission gate + generate/mask/stitch sequence).
        Passes `self` (not just `self.ctx`) since the mask half needs to
        recurse into this class's own `run()` for its per-node loop.
        """
        from decoy_engine.execution.physical._shadow_mixed import dispatch_mixed

        return dispatch_mixed(self, plan, snapshot)

    def _dispatch_out_of_core(
        self, plan: PhysicalPlan, snapshot: ShadowSnapshot
    ) -> ShadowRunResult:
        """Dispatch a `{OUT_OF_CORE}` plan through the Task 4.2
        `OutOfCoreAdapter`, which delegates to `run_fk_out_of_core` -- the FK
        machinery stays single-owner there, never re-implemented here. The
        adapter is constructed locally (never on `self`), matching every
        other driver adapter's per-call lifecycle.
        """
        # Imported here, not at module scope: this is the ONE place inside
        # `execution.physical` that reaches into the `drivers/` subpackage,
        # kept lazy so a caller that never takes the OOC branch never pays
        # for (or risks) importing DuckDB-backed machinery.
        from decoy_engine.execution.physical.drivers._out_of_core import OutOfCoreAdapter

        seed_plan, graph, registry = self._require_ooc_deps()
        adapter = OutOfCoreAdapter()
        result = adapter.run(
            seed_plan,
            {t: snapshot.tables[t] for t in snapshot.tables},
            registry=registry,
            relationship_graph=graph,
            sink=None,  # resident compare only, never publication (C1/C7)
            batch_rows=self.ctx.batch_size_rows,  # the coordinator's own budget
            key_provider=self.ctx.key_provider,  # legitimately optional (unkeyed job)
        )
        seam_context = adapter.last_invocation
        if seam_context is None:  # pragma: no cover - last_invocation is always set first
            # `last_invocation` is set unconditionally, before delegation
            # (`_out_of_core.py`), so a successful `.run()` return always
            # leaves it non-None; this is a type-narrowing guard against a
            # future adapter change, not a reachable branch today.
            raise AssertionError("OutOfCoreAdapter.run returned without setting last_invocation")
        return _adapt_ooc_result(result, seam_context)

    def _require_ooc_deps(self) -> tuple[Plan, RelationshipGraph, ProviderRegistry]:
        """Type-narrow the runtime OOC carriers, raising a coded difference
        naming whichever is absent -- never a bare `None` reaching the
        adapter/delegate uncoded. `ctx.plan` / `ctx.relationship_graph` /
        `self.registry` are declared `| None` for back-compat with every
        scalar/chunked/faker caller, which never sets or reads them; an
        OUT_OF_CORE dispatch genuinely requires all three.
        """
        if self.ctx.plan is None:
            raise ShadowDifference(
                code=OOC_DISPATCH_MISSING_DEPENDENCY, detail="ShadowContext.plan is None"
            )
        if self.ctx.relationship_graph is None:
            raise ShadowDifference(
                code=OOC_DISPATCH_MISSING_DEPENDENCY,
                detail="ShadowContext.relationship_graph is None",
            )
        if self.registry is None:
            raise ShadowDifference(
                code=OOC_DISPATCH_MISSING_DEPENDENCY, detail="ShadowCoordinator.registry is None"
            )
        return self.ctx.plan, self.ctx.relationship_graph, self.registry
