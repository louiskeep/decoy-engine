Status: plan

# Q3 slice 1: make the native route a runtime-selectable production path

The native columnar-streaming route (`execution/native/`) is built and gated for five
strategies (`passthrough`, `redact`, `truncate`, keyed `hash`, C1 `faker`) but nothing in
production calls it. `plan_native_route` and `run_native_or_oracle_chunked` are exercised
only by tests and benchmarks; the production routers never select it, so real mask jobs run
the pandas oracle and pay its row-linear memory. This slice builds the production seam that
makes the native route selectable and proves it through the production entry, so the later
default-on flip is a one-line change on a foundation that already passes parity, memory, and
route-evidence gates.

Enablement is a validated runtime option defaulting OFF. Default-on in production is a later,
separate flip, gated on two things this slice does not deliver: the Rust companion extension
being published with an installable path (without it `hash` downgrades to the oracle anyway),
and a companion-present CI lane that proves the production seam green. This is the native
analogue of Task 7 (the reorder route seam), but sequenced honestly: the plumbing and the
proof now, the default flip when its dependencies land.

This plan supersedes the earlier "wire it live, default-on, in `run_mask_chunked`" draft,
which the Codex plan-gate returned NO-GO for reconstructing a result surface the native route
cannot produce, placing the seam below the routing layer, and defaulting on against an
unpublished companion and a platform that pins streaming off.

## 1. Where the native route fits

Route selection is a ladder (`_pipeline_routing` module docstring): out_of_core (FK, bounded
DuckDB) then, for single-table non-FK jobs, a layer-2 decision between chunked and full_frame.
The native route has the same admission shape as the chunked route (single table, no declared
FK, whole-table atomic decision), so it is a third layer-2 outcome for single-table non-FK
jobs: native, else chunked, else full_frame.

The seam therefore belongs in **layer-2 routing** (`decide_chunk_route` /
`_pipeline_chunk_route.py`), not in the chunked executor. The plan-gate established why: the
executor `run_mask_chunked` receives neither the resolved profile nor the compiled `Plan`
that `run_pipeline` already built, so a native preflight placed there would recompile a second
profile via `to_pandas`. The layer-2 decision already has the compiled plan and config in hand
and is the correct, cheap place to select native.

## 2. Design

### 2.1 Admission in layer-2 routing

`decide_chunk_route` gains a native-selection step ahead of the chunked/full_frame decision.
It runs the pure config/plan admission (`native_route_eligibility` + the C1 config-aware layer)
against the already-compiled plan, with no I/O and no recompile. When the table admits AND the
runtime `native_route_enabled` option is true, the route is native; otherwise the existing
chunked/full_frame decision stands unchanged. The compiled-plan reuse is load-bearing: the
native decision must consume the same plan the rest of the pipeline uses, not a second one.

The existing native admission gates are unchanged and load-bearing (declared FK participation,
any non-scalar node, any node whose `fallback_policy != "native"`, uncovered columns,
non-string faker source, vault columns, and the crypto extension when a `hash` node is
present). This slice consumes their verdict; it does not touch them. Admission is **closed
world**: a strategy is native only if it is on the audited allowlist, and a sentry (section 4)
fails the build if any admitted capability has a row-error or quarantine dependency.

Only the actual-first-batch schema/type validation and the crypto-extension load stay at the
streaming executor boundary, because those need the real first chunk, not the plan.

### 2.2 A structured native execution result (the core new work)

The chunked route returns a 5-tuple (outputs, timings, boundary_conversion_ms, warnings,
quality_metrics) so the routed `ExecutionResult` matches the full-frame surface. The native
entry returns only an `Iterator[pa.Table]` plus an optional evidence sink, and the plan-gate
showed that surface cannot be reconstructed faithfully: `NativeRouteEvidence` aggregates timing
by strategy (column identity lost) and carries no peak-memory or boundary field, `RouteDiagnostics`
is a standalone object not wired into the dispatcher, native execution does real pandas
conversion during first-chunk profiling and faker selection (so `boundary_conversion_ms=0`
would be a lie), and the inner native-to-oracle fallback currently calls the oracle without the
production registry, adapter, vault writer, or chunk-result sink.

So this slice defines a structured result the native executor returns directly, carrying every
field the router needs, measured not fabricated:

- per-**column** timings (not strategy-level totals split across columns),
- peak memory and the measured boundary-conversion time,
- the canonically ordered warnings **the oracle would emit for this job** (see 2.3),
- quality metrics (`{}` for the five strategies, asserted, not dropped),
- route evidence with per-node executed proof (see 2.4),
- vault effects,
- and, on the fallback path, the complete oracle 5-tuple.

The exact production dependencies (registry, adapter, vault_writer, chunk-result sink) are
threaded through **both** the native path and its inner oracle fallback, so a fallback produces
the identical surface a direct chunked-oracle run would.

### 2.3 Warnings: match the oracle, do not invent

The chunked oracle unions `ExecutionResult.warnings` in chunk order (`_chunked.py`), and the
five native strategies (including C1 faker) emit no `QualityWarning` there. `RouteDiagnostics`
exposes pool-cache `AttributedWarning`s, which are a diagnostics side-channel, not the
user-facing masking-warning channel. Surfacing them as `ExecutionResult.warnings` would add
warnings the oracle never emits, a parity break. The structured result's warnings field
therefore carries only oracle-equivalent `QualityWarning`s (empty for this slice's strategies),
in chunk order; pool diagnostics stay on the route-evidence/diagnostics channel.

### 2.4 Route evidence with executed proof

Route evidence must prove the native route actually ran, per node, per chunk, not merely that
preflight intended it. `NativeRouteEvidence`'s single hash boolean and aggregate faker counter
are insufficient. This slice extends the executed-side counters so evidence records, for each
node, that it executed natively on every chunk (a per-node completed count reconciled against
chunk count), and `compiled_kernel_executed` for hash nodes. A native-admitted job whose
evidence shows any oracle-executed node, or a node count short of the chunk count, is a gate
failure. `ExecutionResult` gains a route-evidence field to carry this to the caller.

### 2.5 Transactional publication

The native iterator can raise (schema drift, a kernel error) after earlier chunks executed.
The executor stages output, drains the whole stream, validates route evidence, diagnostics,
and pool quality, then atomically publishes; any error discards the staged artifact. The
pre-first-output oracle fallback stays (an admission miss before any native chunk runs is a
clean oracle run), but there is **no fallback after the first native output**: a post-output
failure is a hard, coded error, never a silent oracle retry.

### 2.6 Enablement

`native_route_enabled` is a validated runtime option on `run_pipeline` (a kwarg, like the other
routing controls), default **False**, threaded to `decide_chunk_route`. It is not a
`GlobalSettings` field: routing controls are runtime kwargs, not profile-hashed semantic config,
and `GlobalSettings` forbids unknown fields. Default False holds until the companion is published
and the companion-present CI lane is green; flipping the default is a later slice, not this one.

## 3. Failure modes

1. **Admission miss at preflight** (FK edge, non-scalar node, unsupported strategy, vault
   column, uncovered columns, non-string faker). Layer-2 selects chunked/full_frame; the oracle
   runs with full production deps. Route evidence records the coded reason. Not an error.
2. **Crypto extension unavailable** with a `hash` node present. Whole table downgrades to the
   oracle before any output, fail-before-output. Not an error.
3. **`native_route_enabled=False`**. Native is never selected; behavior is identical to today.
4. **Schema drift across chunks** (`NativeChunkSchemaDriftError`) after native output began.
   A coded error, staged output discarded, no oracle fallback. Not admissible to reach through
   the production single-`pa.Table` entry (which is schema-fixed); tested direct-native.
5. **Parity divergence** native vs oracle. A gate failure caught before ship; the byte-parity
   contract is frozen.

## 4. Acceptance tests

Every test asserts against the pinned pandas oracle. Tests that prove routing drive the
**production** entry (`run_pipeline` with `native_route_enabled=True`), not the native function.

1. **Byte-parity through the production seam.** For a non-FK table covering the five strategies
   across the admitted type surface (utf8, large_utf8, bool, int8/16/32/64, uint*,
   timestamp-with-tz), the routed result is byte-identical (values, row order, null placement,
   warnings **in order**) to both the chunked-oracle and full-frame-oracle results. Warnings are
   compared order-sensitively, not as the multiset `assert_logical_parity` currently uses.
2. **Native route provably ran.** Route evidence shows every node executed natively on every
   chunk (per-node completed count == chunk count), `compiled_kernel_executed=True` when a hash
   node is present, and zero oracle-executed nodes.
3. **Flat memory through the production router.** Peak RSS driven through `run_pipeline` is flat
   in row count (4x <= 1.5x the 1x value) and under the frozen Phase-1 ceiling, measured in
   fresh processes over multiple tiers with lazy input and incremental output, so a reintroduced
   full-frame materialization is caught.
4. **FK tables never go native.** A table on either side of a declared relationship routes to
   out_of_core or oracle through the production router, never native.
5. **Every fallback lands on the oracle, byte-identical, with full deps.** Admission miss,
   crypto unavailable, vault-column rejection, uncovered columns, non-string faker, and
   `native_route_enabled=False` each produce the oracle result with a custom registry, vault
   writer, and the correct route evidence. Crypto-unavailable is simulated as the loader tests do.
6. **Ordered warnings and coded rejections direct.** All coded C1 rejections and the ordered
   warning union are asserted directly on the native executor, since some cannot be driven
   through the production schema-fixed entry.
7. **Schema drift raises, direct-native.** A second chunk drifting from the admitted schema
   raises `NativeChunkSchemaDriftError` and discards staged output; labeled direct-native.
8. **Closed-world admission sentry.** A test enumerates the admitted capabilities and fails if
   any has a row-error mode or quarantine dependency, so widening the allowlist later cannot
   silently pull in a strategy that needs the unbuilt row-error channel.
9. **Companion-present CI lane.** A CI lane that installs the compiled companion runs the
   production-entry parity, executed-proof, memory, and fallback tests. Main CI (no companion)
   keeps running the oracle-only and ABI tests; the lane's path filters include the routing seam.
10. **Mutation bar** on the changed units (layer-2 native selection, structured-result
    construction, transactional publish, evidence reconciliation).

Non-regression: the full suite stays green; no Part-1 gate or the FK byte-parity contract is
weakened.

## 5. Scope

In: the layer-2 native selection reusing the compiled plan, the structured native execution
result with production deps threaded through both paths, oracle-equivalent ordered warnings,
per-node/per-chunk executed evidence on `ExecutionResult`, transactional staged publication,
the `native_route_enabled` runtime option (default False), the closed-world admission sentry,
and the companion-present CI lane.

Out (later slices): flipping `native_route_enabled` default to True (gated on the two items
below); publishing the Rust companion with an installable extra (its own packaging slice, the
prerequisite for `hash` going native in production); porting `fpe`, `categorical`,
`text_redact`, `text_mask`, `code_set`, `bucket_perturb`, `group_key` (the P4-C ports); the
FPE native kernel; the per-value row-error channel; the platform-side
`classify_streaming_eligibility` adoption and its default-off streaming flag.

## 6. Sequencing note for Cam

The default-on flip and `hash`-in-production both depend on publishing the Rust companion
(currently no installable extra) and the companion-present CI lane being green. This slice
builds the seam and the proof so that flip is a one-line, low-risk change once those land. If
publishing the companion is prioritized first, this slice and that packaging slice are
independent and can proceed in parallel.
