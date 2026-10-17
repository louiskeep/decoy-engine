Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, architecture, performance, testing, verification

# Phase F slice F2: mixed generate+mask with the masking on Rust

Per program plan §3 (F2) + the Cam 2026-10-17 re-sequence (F2 now; F1/F3 held for a
re-baselined generation oracle). Generation stays RESIDENT (unchanged); the MASK
half of a mixed job runs on the bounded Rust routes. Rev-3 grounds every seam in
real symbols (seam map 2026-10-17) and folds the round-2 plan-gate NO-GO (3 P1
remaining + 1 new P2; the 2 earlier P2s were resolved in rev-2).

## 1. Problem + goal

Today a generate table disqualifies the bounded routes in TWO independent places:
- `execution/_pipeline_routing.py` `_sequential_eligible` returns
  `(False, "generate_plus_mask")` when `has_generate_table` (lines 216-217), and
  `decide_execution_route` requires `not has_generate_table` for the chunked route.
- `execution/_planner.py` `_chunked_rejection` appends `generate-kind table(s)...`
  (lines 300-304) and `chunked_relationships_unsupported` for any FK edge (lines
  322-326).
So mixed jobs run full-frame/pandas (`_pipeline_generate_mask.py` + `_stitch.py`).
A native mixed path exists but is SHADOW-ONLY/inert
(`execution/physical/_shadow_mixed.py` `dispatch_mixed`), reusing the native mask
loop + the shared FK engine. Goal: a PRODUCTION mixed route - generation resident,
mask tables on the bounded Rust routes, one transactional publish, byte-identical
to the full-frame mixed oracle.

## 2. Admission: extract the semantic core, keep shadow inertness separate (gate #3)

Extract the SEMANTIC admission out of `_shadow_mixed.require_independent_mixed_shadowable`
into a production-neutral module OUTSIDE `execution/physical/` (so both the shadow
coordinator and the new production route import it, and the physical-seam
import-disconnection sentry stays intact - §6). The semantic core keeps, verbatim
in behavior:
- mixed-shape guard (plan has tables AND synthesis), `require_generation_shape`;
- admitted mask-driver set = scalar / full_frame / chunked only;
  **`DriverId.OUT_OF_CORE` is an UNCONDITIONAL decline** (today
  `_require_admitted_mask_drivers` raises `MIXED_DRIVER_UNSUPPORTED`);
- edge topology via `_select_admitted_coupled_edge`: returns None = INDEPENDENT
  mixed (zero crossing edges) OR exactly ONE crossing edge = COUPLED mixed;
  `len(crossing) > 1` -> `MIXED_FK_TOPOLOGY_UNSUPPORTED`; composite key ->
  `MIXED_FK_CROSS_GENERATE_UNSUPPORTED`; child key type not int/string/large_string
  -> `MIXED_FK_CROSS_GENERATE_UNSUPPORTED` (`_is_admitted_fk_key_type`);
- complete-graph restriction: no OTHER edge may touch a mask table (lines 448-455);
- parent-key-type check `_require_admitted_parent_key_type` (int/string), which is
  post-generation (see §4);
- all-column generation stability `_require_roundtrip_stable_generate_outputs`
  (see §4).

The SHADOW-ONLY inert guards in `_require_no_disqualifying_job_settings`
(sink_requested, source_loader_requested, vault_writer, fidelity, quarantine,
validators, mask_secret_ref) stay in the shadow wrapper. The production route does
NOT inherit the sink/source-loader bans (it publishes and may lazily load); it
keeps the real job-setting restrictions that the bounded mask routes already
enforce (validators / quarantine / fidelity / post-validation / vault / non-pandas
substrate mirror the `_sequential_eligible` ladder). State this split explicitly in
the core's docstring so the extraction cannot silently widen or narrow the subset.

## 3. Two-stage admission + the routing decision table (gate #3)

Admission is TWO-STAGE:
- STATIC (pre-generation, shape): the semantic-core checks that need only config +
  graph (shape guard, driver set incl. OOC-decline, edge topology, complete-graph,
  child key type).
- RUNTIME (post-generation, materialized output, before ANY sink write):
  `_require_roundtrip_stable_generate_outputs` across ALL generate tables, plus
  `_require_admitted_parent_key_type` for a coupled parent.

Routing decision table (production mixed route), by `execution_mode`
(`Literal["auto","sequential","full_frame","out_of_core"]`, `_pipeline.py:128`):

| mode          | mixed job behavior                                                                 |
|---------------|------------------------------------------------------------------------------------|
| `full_frame`  | existing full-frame mixed oracle (never raises - OOM escape hatch). No native path.|
| `out_of_core` | TYPED rejection: `ConfigError` (OOC-mixed unconditional decline), like the existing `reject`-on-ineligible path. Never silent full-frame. |
| `sequential`  | TYPED rejection via the existing `reject_explicit_sequential` (sequential is single-table bounded, not mixed). |
| `auto`        | STATIC-admit -> generate resident -> RUNTIME-admit; on admit run the native mixed route; on a RUNTIME decline fall back to full-frame REUSING the already-generated outputs (no regeneration), coded reason; on STATIC decline keep full-frame before generating. |

Lift the two rejection layers ONLY for the admitted mixed shape under `auto`:
`decide_execution_route` (new mixed branch, guarded by the static admission core)
and `_planner._chunked_rejection` (allow the coupled child's PAYLOAD-only chunked
mask - see §4 - which carries no live FK resolution inside the chunk loop). Name the
production dispatch call site (the new mixed branch in `decide_execution_route` ->
a `run_mixed_route` in `_pipeline_route_exec.py`, mirroring `run_sequential_route`)
and the kill switch (`dispatcher_enabled`-style flag gating the native mixed branch;
off => full-frame). Large coupled mixed jobs that cannot be bounded keep the
existing `lazy_transform_route` `fk_full_frame_oom_risk_rejected` typed rejection;
do not route them to an unbounded native path.

## 4. The generate->bounded-mask FK bridge, whole-table key + chunked payload (gate #1)

The bounded masking entry `run_auto_chunk` (`_pipeline_auto_chunk.py:341`) ->
`_run_dispatcher`/`_run_streamed` -> `_chunked_entry.run_mask_chunked` has NO FK
seam today; the child is masked single-table, FK-free. The resident generated
parent is fully in memory, so F2 does NOT resolve the FK key inside the chunk loop.
Instead:

INDEPENDENT mixed: no crossing edge. Mask each mask table on the bounded chunked
route unchanged; generation stays resident; publish both (§5).

COUPLED mixed (exactly one edge):
1. Resolve the child's FK KEY COLUMN WHOLE-TABLE in one pass against the resident
   generated parent, reusing `resolve_admitted_fk_node` -> `resolve_fk_keys` with
   the identity parent map (`resolve_generate_parent_fk_child`). This is cheap (key
   remap only) and emits EXACTLY ONE edge-level `QualityWarning` on WARN policy
   (`detail["orphan_rows"]` = whole-column count), matching the oracle. REMAP of an
   orphan against a generated parent RAISES `orphan_remap_parent_missing` on BOTH
   the oracle (its `make_remap_fn` parent lookup misses) and here (`_reject_remap`):
   so REMAP-orphan is a hard, byte-identical rejection, NOT a successful remap.
   Matched (non-orphan) rows resolve via the identity map; FAIL raises
   `orphan_fk_violation`; PRESERVE/WARN keep the source key.
2. Mask the child's PAYLOAD columns on the bounded chunked route. Thread the
   pre-resolved FK key array into `run_auto_chunk` as a new explicit
   pre-resolved-column carrier added to the `common` bundle (lines 378-386) and
   passed through `run_mask_chunked`: the admitted FK column BYPASSES ordinary
   per-column strategy execution (its value is the pre-resolved array, sliced per
   chunk by row offset) and is reassembled into each chunk's output; PAYLOAD columns
   retain native admission + the compiled/named-Arrow kernels. No live FK resolution
   runs inside the chunk loop, so the equality-dedup chunk warning fold is never
   asked to sum orphan counts (see §6).

KEY/PAYLOAD ALIGNMENT CONTRACT (plan-gate round-3 P2): the pre-resolved key array
and the chunked payload stream MUST represent the same prepared source in the same
row order. The carrier is derived from the SAME prepared source/iterator the
payload chunks come from (not an independent load or re-sort); the per-chunk slice
uses the payload chunk's own row offset+length, and the route asserts total key
cardinality == total payload rows and monotonically advancing, non-overlapping
offsets before reassembly. If a lazy source cannot guarantee a single stable row
ordering across the key pass and the payload pass, DECLINE to full-frame before any
publish (coded reason). This guards the one new assembly seam that kernel parity
does not cover.

Because the key is resolved whole-table (one WARN) and payloads mask chunked
(byte-identical kernels), the composition's bytes = inherited kernels + the
one-pass key resolution the oracle also does. Parity is the full-frame mixed
oracle, and acceptance asserts the ACTUAL backend ran (compiled/named-Arrow),
not just the route label (D-audit witness pattern).

## 5. Acceptance

Byte-identical to the full-frame mixed oracle across the admitted matrix, with the
bounded route taken AND the native backend asserted (not just the route label):
- independent mixed (generate + chunked mask, no edge);
- coupled one-edge with int AND string child keys, child spanning MULTIPLE chunks;
- orphan policies: PRESERVE, WARN (assert ONE edge-level warning, count = oracle),
  FAIL (assert `orphan_fk_violation` + publication abort), REMAP matched-only
  (identity resolve) AND REMAP-with-orphan (assert `orphan_remap_parent_missing`
  on both oracle and native);
- integer keys above 2**53 (no silent rounding);
- a lazy-source coupled fixture with UNEVEN batch sizes, null child keys, and a
  final PARTIAL chunk (exercises the key/payload alignment contract above);
- a source that cannot preserve stable ordering declines to full-frame before any
  publish.

Two-stage admission tests:
- STATIC declines keep full-frame before generating, coded reason: composite key,
  >1 crossing edge, OOC mask driver, complete-graph violation, non-admitted key
  type, disqualifying job settings.
- Forced-mode: `out_of_core` mixed -> `ConfigError`; `sequential` mixed ->
  existing typed rejection; `full_frame` -> full-frame oracle (never raises).
- RUNTIME declines fall back to full-frame REUSING the already-generated outputs
  (assert NO regeneration): run `_require_roundtrip_stable_generate_outputs` all
  columns of EVERY generate table - fixtures with null_count>0, NaN, and nested
  types in NON-KEY columns of BOTH an independent generate table AND a coupled
  parent (a valid key + a dirty non-key payload); plus `_require_admitted_parent_key_type`
  decline for a non-int/string parent key. Assert decline happens before any sink
  write.

Transactional boundary (reuse `OutputPublish` + `TransactionalSink`, which already
give atomic multi-write / single-commit): open ONE `OutputPublish` session BEFORE
the first generated write; `write()` each generate output and each masked output in
FK-topological order into that one session; exactly one `commit()`; no subordinate
commit. Inject failure AFTER a generate table AND at least one masked batch are
staged, plus a generate-write failure and a commit failure: assert `abort()` ran
(best-effort, non-raising), no published target, staging cleaned,
`internal_publish_not_committed` not swallowed, no retry/fallback; success case
publishes both table sets atomically.

No regression to pure-generate / pure-mask routing. ci-mirror clean (ruff + mypy on
`src/decoy_engine`); companion + no-companion suites green. Platform A3 contract /
routing tests green; a sibling platform activation slice (Cam go) mirrors D2c/D5 -
engine route first.

## 6. FK diagnostics + sentry (gate #4 new P2, and the two seam gates)

Diagnostics: the chunk warning fold is equality-dedup, NOT summing
(`_chunked.aggregate_chunk_warnings:573`; `_chunked_output_sink.fold_warnings:178`).
F2 AVOIDS the cross-chunk orphan-count aggregation problem by resolving the FK key
WHOLE-TABLE (§4): orphan WARN is emitted ONCE per edge from the single
`resolve_fk_keys` call, never per chunk, so the equality fold never has to sum.
Acceptance asserts WARN cardinality == the oracle's (one per edge, matching count).
A per-chunk-resolving variant (which WOULD need an edge-scoped summing accumulator)
is explicitly OUT of F2 scope and noted as a follow-up only if a future slice
resolves keys inside the chunk loop.

Sentry: the shared semantic-admission core + the FK-bridge helpers live OUTSIDE
`execution/physical/`; shadow imports them (dependency points shadow -> shared).
This keeps the two gates separate: the import-disconnection sentry
(`test_physical_seam_disconnection.py`, `permitted_non_physical` for any changed
`execution/` module outside `physical/`, each with a rationale) and the
byte-identical-diff seam sentry (shadow). Do not conflate them.

## 7. Risk

The one genuinely new behavior is threading a pre-resolved FK column through
`run_mask_chunked` (a new carrier in `common`); mitigated by the whole-table key
resolution reusing the shared `resolve_fk_keys` (byte-identical to oracle incl.
`orphan_remap_parent_missing`), the oracle-parity matrix, and the backend-execution
assertion. Admission-core extraction must not widen the bounded subset (guarded by
the decline tests + the documented semantic/inert split). One publish session over
resident + streamed tables reuses the existing atomic `OutputPublish` primitive
(guarded by the abort matrix). Scope deferrals (composite keys, OOC-mixed, 100M
mixed via F1, per-chunk-resolving FK variant) are explicit typed declines, not
silent gaps.

## 8. Gate chain

Codex plan-gate (this doc) -> build (Sonnet) -> ci-mirror + mixed/FK parity +
forced-route/backend + admission-decline + abort tests -> dennis -> Codex final ->
merge on green + Slack. DOCUMENT: roadmap + shipped-log, program plan F2 done,
engine graph refresh.
