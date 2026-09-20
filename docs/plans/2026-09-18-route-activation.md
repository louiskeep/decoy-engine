# Route activation: unified-slice lane as production default (Task 4.6 activation)

Status: superseded (2026-09-20) by `docs/plans/2026-09-20-unified-slice-activation.md`, which
carries the same release rigor (D9 recert, AST omitted-flag sweep, quality_metrics consumer
inventory) plus the tightened sink-inertness safety analysis. Kept for history.

Author: Opus. Roadmap: Stage A2. Cam-authorized (activate the admitted subset now).
D9-cert-gated: the production-default flip does NOT merge until the D9 cert is GREEN under the
relaxed 1.25x 1M budget on the reference host, AND Cam acks the mechanism (below).

This revision folds in the Codex plan-gate NO-GO (round 1): the sink-admission finding, the
AST omitted-flag sweep, the admitted-domain parity matrix, default-on route-matrix coverage, and
the quality_metrics manifest-contract decision.

## FRAME
The unified-slice coordinator (Task 4.5) is merged, byte-parity-proven, ~4x faster at 1M, and
default-OFF (`unified_slice_enabled=False`, `_pipeline.py:174`). Activation makes it the DEFAULT
for the shapes admission accepts; everything else and any exception reroute to legacy.

### The sink finding (Codex HIGH) -- activation is a two-part change, not a bare flip
`_isolated_worker.py:225` ALWAYS passes a `ParquetTransactionalSink` to `run_pipeline` (a TB-1
safety fix so streaming routes can commit). `cheap_admission` (`_unified_slice_admission.py:154`)
declines whenever `sink is not None`. So the platform's production worker path is NEVER admitted,
regardless of the flag. A bare default flip would activate the lane ONLY for in-process no-sink
callers (SDK/embedding), not real platform jobs. To deliver the production speedup, admission must
accept the sink-bearing full_frame job.

## Design -- two coordinated changes

### Change 1 (admission): accept a sink the full_frame route provably ignores
Admission already REQUIRES `route == "full_frame"` (`_unified_slice_admission.py:143`). The legacy
full_frame route never touches `sink` (documented at `_isolated_worker.py:159-161,221`;
`_finalize_outputs` returns in-memory outputs; the flag-off inertness suite pins it). The unified
lane likewise returns in-memory `result.outputs` and never writes the sink. So for an admitted
full_frame shape the sink is inert on BOTH paths, and declining on it is pure conservatism.

Relax the predicate: admit when a sink is present ONLY IF the resolved route is full_frame (which
admission already requires) AND the lane is proven to leave the sink untouched. Keep declining
`source_loader is not None` (a lazy loader DOES change source resolution -- not inert). The build
MUST:
- Verify (test + code read) that neither the legacy full_frame finalize NOR the unified lane calls
  any sink method for an admitted shape; add an assertion/spy test proving the sink is untouched on
  the activated path (a poisoned sink that raises on any method call must NOT fire).
- Preserve the admission safety proof Codex H4 (Part 2) named: the `source_frame` full-conversion /
  round-trip that rejects malformed/transplanted pandas metadata stays intact; accepting a sink
  does not weaken it. If any shape cannot be mechanically proven sink-inert, DECLINE it to legacy.

### Change 2 (default): flip via a named, reversible constant
Change the default of `unified_slice_enabled` from `False` to a named module constant (not a bare
`True`), so activation is one documented, greppable, reversible switch (mirrors `RELEASE_PHASE`/
`is_pre_ga()`):
```python
# Route activation (Task 4.6, D9-cert-gated). The unified-slice lane is the production default for
# the shapes cheap_admission accepts (single-table full_frame pandas masks on resident sources,
# incl. the platform worker's inert sink). Every other shape and any exception reroute to legacy.
# Flip to False to roll the default back without touching call sites.
UNIFIED_SLICE_DEFAULT = True
```
`unified_slice_enabled: bool = UNIFIED_SLICE_DEFAULT`. `native_route_enabled` stays False (older
Q3 lane; Task 4.7 deletes it).

### Public-contract decision (Codex HIGH)
Flipping the library default changes behavior for every `run_pipeline` caller of an admitted shape,
including direct SDK/library callers -- that IS the intent of "production default." Document it in
the CHANGELOG + compatibility notes; keep `unified_slice_enabled=False` as the explicit legacy
opt-out. Do not describe a worker-only enable as activation (the worker can't know the route before
calling run_pipeline, per its docstring), which is why Change 1 (admission) is required.

## Guards that bound the blast radius (all merged)
1. Admission predicate (now incl. the sink-inert rule) -- only the proven subset is admitted.
2. Fail-closed boundary (#158) -- any unexpected exception in the lane reroutes to legacy.
3. Byte-parity suite -- the lane is byte-identical to legacy (values + Arrow types + `b"pandas"`
   metadata) for admitted shapes. Any admitted-shape golden move = a real defect, STOP.

## Findings folded in (Codex plan-gate round 1)

### F1 (BLOCKER) -- D9 cert green at the candidate commit
The production-default-flip commit must have a GREEN `d9_certified: true` from
`bench_compare.py --require-cert` on the reference host under the 1.25x 1M budget, preserved as
evidence (JSON + command + companion status). Re-run after the flip commit even though the bench
arms set flags explicitly (companion + host must match). Hard merge precondition.

### F2 (HIGH) -- comprehensive omitted-flag sweep (AST, not grep)
Flipping the default silently changes every `run_pipeline` / `run_pipeline_isolated(**kwargs)`
caller that omitted the flag AND can satisfy admission. Build an AST inventory of direct calls,
wrappers/partials/mocks, and isolated-run kwargs across `src/`, `tests/`, `scripts/`. Classify each
omitted-flag call by whether it can satisfy admission. Then:
- Legacy-oracle tests that mean to test the legacy path: pass `unified_slice_enabled=False`
  explicitly (preserves intent).
- Default-contract tests: update to prove an omitted flag ACTIVATES an admitted shape and explicit
  `False` stays inert. Specifically `test_unified_slice_inertness.py::test_default_omitted_flag_is_also_inert`
  inverts: the omitted-flag default now activates; rewrite it to assert activation on an admitted
  shape and keep a separate explicit-False inertness case.
Run the full relevant suites + goldens after the sweep.

### F3 (HIGH) -- admitted-domain parity matrix + companion-present hash
Enumerate the admitted matrix (strategy x allowed Arrow type x null state x empty/non-empty x
chunk/batch state x pandas metadata state) and add activated-route byte assertions for every cell,
including nullable int64/bool passthrough, mixed-null redact/truncate/hash, and StringDtype. Hash
cases MUST execute with the native companion PRESENT (some parity tests pass by declining to legacy
when the companion is absent -- that proves nothing about activation); require companion-present in
CI for these, or remove hash from default admission until that evidence exists. Add default-on
differential tests vs explicit legacy, incl. output schema metadata and the quality_metrics delta.

### F4 (MEDIUM) -- default-on route-matrix coverage
Table-driven default-on test proving each decline stays legacy with no `physical` import/execution:
chunked, non-full_frame, non-pandas substrate, lazy/source_loader, FK/relationships, validators/
quarantine/storm, vault/fidelity, `native_route_enabled=True` (native-first precedence), multi-
table. Prove an admitted resident single-table full_frame pandas mask WITH a sink activates. Cover
`execution_mode`, `auto_chunk` (chunked-first precedence), substrate, sink, source_loader combos.

### F5 (MEDIUM) -- quality_metrics manifest contract
The admitted lane adds `quality_metrics["unified_slice_activation"]` (+ `["execution"]`,
`["execution_plan"]`). `quality_metrics` is manifest-safe free-form (stamped into the job manifest),
NOT part of byte/determinism goldens. But newly-admitted omitted-flag callers now emit these keys.
Decide + document the contract: the activation telemetry is an intended, additive manifest delta
(reproducible route provenance), not a data change. Find every consumer asserting WHOLE-dict
`quality_metrics` equality on an admitted shape and update it; keyed assertions are unaffected. Test
both the default-on telemetry delta and explicit-False stability (no keys added).

## VERIFY (acceptance)
1. Full engine suite GREEN with the default flipped (`.venv/bin/python -m pytest`), esp.
   `tests/physical/test_unified_slice_*.py` + `tests/parity/`.
2. Parity suite GREEN (byte-parity under the new default, admitted matrix from F3).
3. Inertness rewritten + GREEN (explicit-False still fully inert; `physical` not imported).
4. Exception-boundary GREEN (reroute-to-legacy intact).
5. Route-matrix (F4) GREEN; sink-untouched spy test GREEN.
6. test_flight GREEN; data fingerprints UNCHANGED (parity guarantee); note any intended manifest
   quality_metrics delta explicitly.
7. Module-size sentry + ruff + mypy clean.
8. D9 cert GREEN on the reference host under the 1.25x 1M budget BEFORE the flip merge (F1).

## Out of scope
- `native_route_enabled` (stays False; Task 4.7 deletes it).
- Coordinator slices 3-6 shapes (FK/out-of-core etc. not admitted; activate later).
- Platform Phase 3.3/3.4 (Stage A4; separate) -- but note Change 1 means the platform worker is
  admitted with NO platform code change, so 3.3/3.4 becomes an expose/monitor step, not a re-plumb.

## Gates
FRAME (done) -> PLAN (this revision) -> Codex plan-gate (re-run) -> build (Opus; delicate:
admission-safety change + default flip + the F2-F5 test matrix; own branch) -> SELF-CHECK -> VERIFY
-> dennis -> Codex FINAL -> D9 cert GREEN -> Cam mechanism ack -> merge.
