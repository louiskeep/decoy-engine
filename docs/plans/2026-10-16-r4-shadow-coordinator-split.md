Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, refactoring, testing, verification, autonomous-operation

# R4 / E6: ShadowCoordinator coordinator/batch-runner split

Refactor slice, scheduled "before Phase E" per the refactor track in
`decoy-platform/docs/ROADMAP.md` and the codebase-health report
`decoy-platform/docs/records/2026-10-06-codebase-health-and-refactor-report.md`
(§2.1, row E6). Behavior-preserving. No masking-output change on any route.

## 1. Why (the pain this removes)

`execution/physical/_shadow_coordinator.py` is 599 LOC against the 600 hard cap
(`tests/physical/test_shadow_full_frame.py::test_shadow_coordinator_module_stays_under_the_loc_cap`)
and the most-edited engine file since September (37 commits touching it). Its
`run` method is ~190 lines and mixes two concerns: plan-shape coordination
(route dispatch to the synthesis / mixed / out-of-core / full-frame adapters)
and the per-node/per-batch masking execution (the loop, pool/kernel resolution,
column assembly). Phase E's 100M milestone will add scale-hardening edits to
exactly the per-batch path, and that path has no headroom in this file. This
slice makes the change easy before making it: extract the batch runner so the
coordinator stays a thin dispatcher and the per-batch work gets its own module
with room to grow.

This is the same carve already applied three times to this class: the
synthesis, mixed, and full-frame branch bodies live in `_shadow_generation.py`
(480 LOC), `_shadow_mixed.py` (527), and `_shadow_full_frame.py` (388), each
reached through a thin `_dispatch_*` wrapper on the coordinator. R4 applies it
to the one branch that is still inline: the default per-node loop.

## 2. Scope

In: a behavior-preserving extraction of the per-node/per-batch masking loop and
its pool/kernel resolution out of `_shadow_coordinator.py` into a new
`execution/physical/_shadow_batch_runner.py`. Out: any change to masking output,
route selection, evidence content, timing records, or admission. No new feature,
no operator change, no FK-logic change. The D3a follow-up (a validators
rejection in `_dispatch_out_of_core`) is explicitly NOT folded in: R4 does not
change that dispatch's reach (it stays dormant, gated off by `cheap_admission`
in the live routing), so the D3a guard remains its own slice. If a later slice
gives the coordinator's OOC dispatch production reach, that guard is required
first; R4 does not.

## 3. The carve

### Moves to `_shadow_batch_runner.py`
- The per-node/per-batch loop: the body of `run` from the local-state setup
  (`outputs`/`route_evidence`/`index_kernel`/`pools_by_identity`/`pool_cache`/
  `fk`/`warnings`/`row_errors`) through the `for table -> for node -> for batch`
  nest, column assembly, source-schema-order reconstruction, and the final
  per-node-path `ShadowRunResult` construction (`_shadow_coordinator.py:283-433`).
- `_resolve_pool` (`:524-599`): the build-once-per-`PoolIdentity` faker-pool
  resolution.
- The module helpers used only by that loop: `_batches` (`:170`) and
  `_pool_values_are_string_valued` (`:149`).

The extracted entry point is a module-level function, mirroring the sibling
pattern (`_shadow_mixed.dispatch_mixed(self, ...)`,
`_shadow_full_frame.dispatch_full_frame_if_applicable(self, ...)`):

    def run_shadow_batches(
        coordinator: ShadowCoordinator,
        plan: PhysicalPlan,
        snapshot: ShadowSnapshot,
        *,
        admitted_edges: tuple[RelationshipEdge, ...] = (),
        when_masks: Mapping[str, pa.Array] | None = None,
    ) -> ShadowRunResult

It reads `coordinator.ctx`, `coordinator.registry`, `coordinator.pool_cache`.
`_resolve_pool` becomes a module-private helper taking `coordinator` (or the
three fields it needs) rather than a method; its asserts and the
`FAKER_POOL_NON_STRING_OUTPUT` raise are preserved verbatim.

### Stays in `_shadow_coordinator.py`
- `ShadowCoordinator` dataclass + fields.
- `run`'s dispatch head (`:244-281`): the synthesis / mixed / `{OUT_OF_CORE}` /
  `{FULL_FRAME}` / mixed-driver-raise routing. Its fall-through becomes a single
  call to `run_shadow_batches(self, plan, snapshot, admitted_edges=..., when_masks=...)`.
- `_dispatch_synthesis`, `_dispatch_mixed`, `_dispatch_out_of_core`,
  `_require_ooc_deps` (unchanged).
- `_adapt_ooc_result`, `_privacy_safe_driver_set` (coordination helpers).

### Import-cycle resolution (the one structural decision)
`run_shadow_batches` needs `ShadowRunResult`; the coordinator needs
`run_shadow_batches`. To keep the hot default path free of a lazy per-call
import and avoid a cycle, move `ShadowRunResult` to a new tiny
`execution/physical/_shadow_result.py` that both import, and have the
coordinator import `run_shadow_batches` at module top. The batch runner imports
`ShadowCoordinator` only under `TYPE_CHECKING` (it receives the instance; no
runtime dependency). The coordinator re-exports `ShadowRunResult`
(`from ._shadow_result import ShadowRunResult`, kept in `__all__`) so every
existing `from ..._shadow_coordinator import ShadowRunResult` importer keeps
working. If the build finds the shared-types module disproportionate, the
fallback is a lazy `from ... import run_shadow_batches` inside `run` (the sibling
precedent); byte-parity is the invariant, not the import mechanism.

## 4. Invariants that must hold byte-identical (the hard part)

Each is pinned by an existing test; the extraction must not perturb any:
- Index kernel loaded at most once per `run`, lazily, before the first pool
  build (`test_shadow_faker_lifecycle.py`).
- Faker pool built once per `PoolIdentity` (never per batch, never per node_id),
  against the resolved `registry` (never `get_default_registry()`), via the
  job-scoped `pool_cache` (never the module-global default). The empty-`job_seed`
  and missing-`registry` fail-loud asserts survive verbatim.
- `FAKER_POOL_NON_STRING_OUTPUT` is raised from the pool path and reaches the
  sole production caller `_unified_slice.py:259`, where it drives the
  non-string-pool reroute. Moving `_resolve_pool` must keep that raise
  propagating unchanged.
- The mixed branch recurses into `run()` for its mask half
  (`_shadow_mixed.dispatch_mixed`): after the split, that recursion must reach
  `run_shadow_batches` identically.
- The OOC branch keeps its lazy `drivers/` (DuckDB) import locality; R4 touches
  neither the OOC dispatch nor its import.
- Evidence and timing fidelity: `route_evidence` per-node `OperatorCallEvidence`,
  the `timed_strategy(node.strategy, columns)` scoping (one record per node,
  enclosing kernel load + pool resolve + the batch loop + assembly), and the
  coded `ShadowDifference` set (`DUPLICATE_NODE_DECLARATION`,
  `OPERATOR_NOT_EXECUTED`, `PLANNED_VS_ACTUAL_ROUTE_DIFF`, `SCHEMA_DIFF`,
  `RESOURCE_LIMIT_BREACH`, `NATIVE_COMPANION_UNAVAILABLE`). Phase E's 100M run
  asserts on exactly this per-column Rust evidence, so it must be byte-identical.
- Source-schema-order column reconstruction and the `SCHEMA_DIFF` guard.

## 5. Verification + acceptance tests

Behavior-preserving refactor, so the acceptance bar is "nothing changed":
1. Pre-extraction byte baseline (the R3/P6 pattern): before moving anything,
   record the `ShadowRunResult` outputs (output tables' digests, route_evidence
   keys + planned/actual operators, warnings, row_errors) for a representative
   set of plans (scalar single-table, chunked, faker-pool, mixed, the degenerate
   zero-row and all-null shapes). After the extraction, assert byte-identical.
   `tests/physical/test_shadow_corpus.py` already re-verifies the degenerate
   shapes against the live `run_pipeline(substrate="pandas")` oracle; keep it
   green unchanged.
2. The full `tests/physical/` suite green with NO assertion change. The only
   permitted test edit is the import line in `test_shadow_coordinator.py` for the
   moved `_batches` / `_pool_values_are_string_valued` helpers (update to the new
   module; no behavior change).
3. Module size: coordinator drops well under 600; the new `_shadow_batch_runner.py`
   is under 600 (if the loop + `_resolve_pool` exceed it, split `_resolve_pool`
   into its own module too). Update the census (`tests/sentry/test_module_size.py`)
   for both the coordinator's new (lower) LOC and the new module(s); relocate or
   retarget the cap test at `test_shadow_full_frame.py:663` to the coordinator's
   new size.
4. Physical-seam sentry (`tests/sentry/test_physical_seam_disconnection.py`): the
   new module lives under `execution/physical/` so it is inside the seam by
   construction. The byte-identical-to-origin-main production check
   (`test_production_execution_modules_are_byte_identical_to_origin_main`) will
   flag both the changed coordinator and the new module; add both to that
   sentry's permitted/changed list with the R4 rationale, per the standing
   physical-seam allowlist rule. Confirm no module OUTSIDE `physical/` is touched
   (so no `permitted_non_physical` / `DELIBERATELY_CONNECTED_MODULES` entry is
   needed); if one is, add it with rationale.
5. ci-mirror clean (ruff check + format, mypy on `src/decoy_engine`, no-extras
   env). Companion-present parity suite green (this is a native-touching area).

## 6. Risk

- Sole production reach: the per-batch loop is the only live unified-slice
  masking path (`_unified_slice.py:256`), run on every admitted single-table
  job. A behavior change here is a live-output correctness change. Mitigated by
  the byte baseline + the oracle-anchored corpus test + the byte-identical seam
  sentry.
- Re-entrancy: the mixed branch recurses into `run`; the extraction must keep
  that reaching the batch runner.
- Lifecycle invariants (§4) are easy to break in a mechanical move; each has a
  named test guard, listed so the build checks them explicitly.
- Churn/merge: Phase D work is still settling on engine main (D-audit merged
  2026-10-16, #269). Branch from known-green main; expect a possible rebase.

## 7. Gate chain

Codex plan-gate GO (this doc) -> build on `feat/r4-coordinator-split` ->
ci-mirror PASS -> dennis APPROVE -> Codex cross-model final GO -> CI green ->
merge. DOCUMENT: roadmap refactor-track row E6 checked, RECENTLY-SHIPPED entry,
engine graph refresh if used. Then the single 100M run (Phase E's first data
point), per the owner's direction.

cam
