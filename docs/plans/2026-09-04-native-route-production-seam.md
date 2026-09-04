Status: plan

# Q3 slice 1: a streaming native lane for passthrough, redact, truncate

The native columnar-streaming route is built and gated but nothing in production selects it,
so real mask jobs run the pandas oracle and pay its row-linear memory. This slice makes the
native route a real production path for the three pure-kernel strategies that need neither the
Rust companion nor the pool-quality machinery: `passthrough`, `redact`, `truncate`. It builds
the streaming lifecycle those strategies need (a lane that branches before the source is
materialized, owns a streaming transactional sink, and refuses any job feature that would force
a whole-dataset pass), proven end to end through the production entry. `faker`, keyed `hash`,
and the wider payload ports layer onto this lane in later slices.

The narrowing is deliberate. The two plan-gate rounds showed that wiring the full five-strategy
route live couples three hard problems: the streaming lifecycle, `faker`'s pool-quality check
(which as built needs the whole dataset at once), and keyed `hash`'s Rust companion (unpublished,
no installable path). The three chosen strategies use the in-process Python kernel
(`decoy_engine.kernel._scalar`), carry no pool-quality obligation, and emit no row errors, so
this slice isolates the foundational lifecycle work from those two problems and, because no
companion is involved, its whole test surface runs in the normal CI environment.

Enablement is a validated runtime option defaulting OFF. Flipping the default and adding `hash`
and `faker` are later slices with their own gates.

## 1. Current reality and why a seam is not enough

Route selection is a ladder: layer-1 sends FK jobs to the bounded out-of-core route; layer-2
(`decide_chunk_route`) picks chunked vs full_frame for single-table non-FK jobs. The native
route has the layer-2 admission shape (single table, no FK, whole-table atomic decision), so it
is naturally a third layer-2 outcome.

The plan-gate established that placing native inside the existing chunked executor cannot deliver
the memory win. Two facts force a dedicated lane instead:

- The production pipeline materializes every `LazySource` into a full in-memory table
  (`resolve_resident_sources`) before the chunked/full_frame split. A native branch downstream of
  that point has already paid the full-frame memory cost, so its flat-memory claim would be false.
- Finalization (fidelity reports, validators, quarantine, result assembly) operates on complete
  materialized outputs, and the chunked executor itself concatenates every chunk. A native branch
  that returns early would silently skip those steps; one that returns late loses the memory win.

So the native path must be a dedicated lane that branches immediately after layer-1 routing and
BEFORE source materialization, streams the source in batches, and explicitly refuses any job that
needs a whole-dataset pass.

## 2. Design

### 2.1 The dedicated streaming lane

A new native lane branches right after layer-1 (FK) routing, before `resolve_resident_sources`.
It consumes the source as batches (`LazySource.iter_batches()`), never materializing the whole
source table. It is selected only when the runtime option is on AND the table admits AND the job
carries none of the rejected whole-dataset features below.

A test asserts `resolve_resident_sources` is never called on the native path (a spy or a call
counter), so a future change that reintroduces materialization fails loudly.

### 2.2 Reject-before-output contract (closed world)

Before any output is produced, the lane rejects, by routing the whole job to the existing oracle
path, every feature it cannot honor while streaming. Rejection is a clean reroute, not an error,
and happens before the first batch is masked:

- FK participation (already handled at layer-1, reasserted here).
- `vault: true` on any column. This is new and load-bearing: the generic native eligibility has
  no vault check today, only the faker gate rejects vault, so `passthrough` / `redact` /
  `truncate` with `vault: true` would otherwise admit while the lane has no streaming
  vault-collection path. The lane rejects any vault column outright.
- Any validator, fidelity-report request, or quarantine / row-error configuration (these need
  complete outputs).
- A non-streaming sink (a legacy callable sink that materializes the whole stream). The lane
  requires a streaming transactional sink or the in-memory `sink=None` contract in 2.4.
- Unsupported projection, generation columns, or multi-table jobs.
- Any strategy not on the audited three-strategy allowlist for this slice.

A closed-world admission sentry (test) enumerates the admitted capabilities and fails the build
if any admitted strategy carries a row-error mode, a quarantine dependency, a pool-quality
obligation, or any `quality_obligation` / `warning_code` this slice does not explicitly implement.
Widening the allowlist later cannot silently pull in an unhandled obligation.

### 2.3 Layer-2 selection reusing the compiled plan and registry

The native decision runs in `decide_chunk_route`, which already holds the config, the compiled
`Plan`, the resolved registry, and source metadata. The plan-gate found that the current native
APIs recompile (`compile_native_plan`, `_mask_native`) and call `get_default_registry()`, which
would add a second compile and ignore a custom registry. This slice adds a plan-aware admission
and execution API that takes the existing compiled `Plan` and the resolved registry, so the
native decision does pure config/plan inspection with no I/O and no recompile. Only the
actual-first-batch schema and type validation stays at the streaming executor boundary, since it
needs the real first batch.

Routing precedence is defined explicitly and tested as a matrix: `execution_mode="full_frame"`
and `execution_mode="out_of_core"` overrides win first (unchanged), then layer-1 FK routing, then
the native lane (only when `native_route_enabled` is true and the job admits), then the existing
chunked vs full_frame decision. An explicit non-pandas substrate, or a sink the lane cannot
stream to, falls through to the existing decision.

### 2.4 Streaming transactional sink and the sink=None contract

When the caller provides a sink, the lane requires a streaming transactional sink and follows the
established stage / drain / validate / commit-or-abort discipline (the same ordering the
out-of-core runner uses): it stages output, drains the whole stream, validates route evidence and
the ledger (2.6), then atomically commits; any error discards the staged artifact. There is no
fallback to the oracle after the first native output; a post-output failure is a coded error.

When `sink=None` (the caller wants outputs in memory), the memory contract is stated honestly:
the whole output necessarily resides in memory because the caller asked for it, so peak RSS is
about one output dataset, not the flat-streaming figure. The win even here is that the source is
never fully materialized alongside an intermediate full-frame and the output at once. The
flat-RSS acceptance test therefore uses a streaming sink, where the guarantee is real.

### 2.5 Structured native execution result

The lane returns a structured result carrying every field the routed `ExecutionResult` needs,
measured not fabricated: per-column timings (not strategy totals split across columns), the
measured boundary-conversion time, peak memory by the established meaning (an external
fresh-process VmHWM for the route gate; the existing `StrategyTimingRecord` delta keeps its
current before/after meaning), the oracle-equivalent ordered warnings, quality metrics, and route
evidence. For the three strategies the warnings are empty (they emit no `QualityWarning` in the
oracle) and quality metrics are `{}`; both are asserted, not silently dropped. Pool-cache
diagnostics do not exist here (no faker), so the warning-channel confusion the earlier draft had
cannot arise.

`ExecutionResult` gains a route-evidence field so this reaches the caller, and all existing
`ExecutionResult` fields and telemetry are preserved on the native path.

### 2.6 Restored invocation-scoped route ledger

The frozen Part-1 route-evidence gate requires proof that a native job made zero oracle calls,
zero oracle rows, zero fallback calls, zero fallback rows, and zero rejected chunks. A per-node
"native" label cannot prove an accidental oracle call did not happen. This slice restores an
invocation-scoped ledger: attempted and completed native / oracle / fallback call counters and
row counters, a rejected-chunk counter, and exact `(table, work-node identity, chunk index)`
records. The acceptance tests assert every frozen Part-1 count is zero after a successful native
publication, and additionally install a fail-fast spy on the oracle chunked entry so any stray
invocation fails the test immediately.

### 2.7 Enablement

`native_route_enabled` is a validated runtime option on `run_pipeline` (a kwarg alongside the
other routing controls), default False, threaded to `decide_chunk_route`. It is not a
`GlobalSettings` field (routing controls are runtime kwargs, not profile-hashed config, and
`GlobalSettings` forbids unknown fields). Flipping the default is a later slice.

## 3. Failure modes

1. Admission miss or a rejected whole-dataset feature (FK, vault, validators, fidelity,
   quarantine, non-streaming sink, unsupported projection, generation, multi-table, unsupported
   strategy). The job reroutes to the oracle with full production dependencies before any native
   output. The ledger records the coded reason. Not an error.
2. `native_route_enabled=False`. Native is never selected; behavior is identical to today.
3. A post-first-output failure (a kernel error mid-stream). The staged artifact is discarded and
   a coded error is raised; no oracle retry. A hard failure, by design.
4. Parity divergence native vs oracle. A gate failure caught before ship; the byte-parity
   contract is frozen.

## 4. Acceptance tests

Every test asserts against the pinned pandas oracle. Routing tests drive the production entry
(`run_pipeline` with `native_route_enabled=True`), not the native function directly. No companion
is involved, so the whole surface runs in normal CI.

1. **Byte-parity through the production entry.** For a non-FK, non-vault table covering
   `passthrough`, `redact`, `truncate` across the admitted type surface (utf8, large_utf8, bool,
   int8/16/32/64, uint*, timestamp-with-tz), the routed result is byte-identical (values, row
   order, null placement, warnings in order) to both the chunked-oracle and full-frame-oracle
   results. Warnings are compared order-sensitively.
2. **The native lane provably ran.** After a successful native publication, every frozen Part-1
   ledger count is zero (oracle calls, oracle rows, fallback calls, fallback rows, rejected
   chunks), per-node completed counts equal the chunk count, and the oracle-entry spy recorded no
   call.
3. **Source never materialized.** A spy proves `resolve_resident_sources` is never called on the
   native path.
4. **Flat memory with a streaming sink.** Peak RSS driven through the production entry with a
   streaming sink is flat in row count (4x <= 1.5x the 1x value) and under the frozen Phase-1
   ceiling, measured in fresh processes over multiple tiers with lazy input and incremental
   output.
5. **Every rejected feature reroutes to the oracle, byte-identical, with full deps.** Parameterized
   over FK, `vault: true` on each of the three strategies, a validator, a fidelity-report request,
   quarantine config, a non-streaming callable sink, unsupported projection, a generation column,
   a multi-table job, and `native_route_enabled=False`. Each produces the oracle result with a
   custom registry and the correct coded ledger reason.
6. **Routing precedence matrix.** `execution_mode` overrides, FK, native, and the chunked vs
   full_frame decision resolve in the defined order.
7. **Closed-world admission sentry.** The admitted-capability enumeration fails the build if any
   admitted strategy carries a row-error, quarantine, pool-quality, or unimplemented
   obligation / warning code.
8. **Compiled-plan and registry reuse.** A native run compiles the plan exactly once and honors a
   custom registry (no `get_default_registry()` on the native path).
9. **Mutation bar** on the changed units (native lane selection, reject-before-output, structured
   result, ledger, transactional publish).

Non-regression: the full suite stays green; no Part-1 gate or the FK byte-parity contract is
weakened.

## 5. Scope

In: the dedicated streaming native lane branching before source materialization; the
reject-before-output closed-world contract including the new all-strategy vault rejection; layer-2
native selection reusing the compiled plan and registry via a plan-aware API; the routing
precedence matrix; the streaming transactional sink and the `sink=None` memory contract; the
structured native result; the restored invocation-scoped route ledger; the `native_route_enabled`
runtime option (default False); and the acceptance suite above, all in normal CI.

Out (later slices, each its own plan and gate): `faker` on the live lane (needs streaming or
admission-aligned pool quality); keyed `hash` on the live lane (needs the Rust companion and,
for production, publishing it with an installable path); flipping `native_route_enabled` default
to True; the P4-C payload ports (`fpe`, `categorical`, `text_redact`, `text_mask`, `code_set`,
`bucket_perturb`, `group_key`); the FPE native kernel; the per-value row-error channel; and the
platform-side streaming-eligibility adoption.

## 6. Program sequence (this slice is step 1)

1. This slice: the streaming lane for `passthrough` / `redact` / `truncate`.
2. `faker` on the lane: streaming or admission-aligned pool-quality enforcement.
3. Keyed `hash` on the lane: the companion-present execution path plus a companion CI lane; in
   parallel, publish the Rust companion so `hash` can go native in a real install.
4. The default-on flip, once the companion is published and its CI lane is green.
5. The P4-C payload ports, each gated, some behind the row-error channel.
