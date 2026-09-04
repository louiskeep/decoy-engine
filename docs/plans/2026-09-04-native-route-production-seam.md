Status: plan

# Q3 slice 1: wire the native route into production selection

The native columnar-streaming route (`execution/native/`) is built and gated for five
strategies (`passthrough`, `redact`, `truncate`, keyed `hash`, C1 `faker`) but nothing in
production calls it. `plan_native_route` and `run_native_or_oracle_chunked` are exercised
only by tests and benchmarks; the production routers (`_pipeline_routing.py`,
`_pipeline_route_exec.py`) never select it, so real mask jobs still run the pandas oracle
and pay its row-linear memory. This slice closes that gap: it selects the native route for
the tables it already admits, so the flat-memory and throughput wins reach real jobs. It
adds no new strategy ports (those are the later P4-C slices); it wires up what already
passes its parity and memory gates.

This is the native analogue of Task 7 (the reorder route seam): a route-selection behavior
change, gated on Cam's go-live call, which is given.

## 1. Where the native route fits

Route selection today (`_pipeline_routing` module docstring) is a ladder: out_of_core (FK,
bounded DuckDB) -> chunked (single-table, non-FK, streamed in `chunk_size_rows` slices via
`run_mask_pipeline_chunked`) -> full_frame -> sequential. The native route has the SAME
admission shape as the chunked route: single table, no declared FK relationship, whole-table
atomic decision. So the native route is a faster variant of the chunked route, not a new rung.

The seam is therefore inside the chunked branch. `run_mask_chunked`
(`_pipeline_route_exec.py:533`) is the chunked executor; today it always calls
`run_mask_pipeline_chunked` (the pandas oracle in `chunk_size_rows` slices). This slice makes
it consult `plan_native_route` first and, when the table is admitted, execute through
`run_native_or_oracle_chunked` instead. That native entry already carries the fallback:
on any admission miss it runs the same `run_mask_pipeline_chunked` oracle, so the seam cannot
produce a half-native table.

## 2. Design

### 2.1 Admission at the chunked branch

`run_mask_chunked` gains a preflight call to `plan_native_route(config, profile, table=...,
engine_version=...)`. The profile it needs is the same one the chunked route already resolves.
When `NativeRouteEvidence.native_admitted` is true, dispatch to the native executor; otherwise
call `run_mask_pipeline_chunked` unchanged. The decision is logged in route evidence either way.

The existing native admission gates are unchanged and load-bearing (see `_dispatch.py`):
declared FK participation, any non-scalar node, any node whose `fallback_policy != "native"`,
first-chunk column-set mismatch, non-string faker source, and (only when a `hash` node is
present) the compiled crypto extension failing to load. Any of these downgrades the whole
table to the oracle. This slice does not touch them; it consumes their verdict.

### 2.2 Interface adaptation (the real work)

`run_mask_chunked` returns `(outputs, timings, boundary_conversion_ms, warnings,
quality_metrics)` so the routed `ExecutionResult` keeps the same surface as the full-frame
one. `run_native_or_oracle_chunked` returns an `Iterator[pa.Table]` plus a
`NativeRouteEvidence` (via `route_evidence_sink`) and a `RouteDiagnostics` evidence object.
The seam reconstructs the tuple from those:

- **outputs**: concatenate the yielded chunks into the `dict[str, pa.Table]` the caller
  expects, the same concatenation the chunked oracle path already does.
- **timings**: build the per-(strategy, column) rollup from the native evidence's
  `kernel_elapsed_s` / `kernel_calls` and `pool_select_calls`. Where the native route reports
  no comparable timing, emit an empty rollup for that column rather than a fabricated number.
- **boundary_conversion_ms**: the native route's Arrow-native path has its own boundary cost;
  report the measured value, 0.0 when it does no pandas boundary conversion.
- **warnings**: the order-stable union of the `RouteDiagnostics` attributed/pool warnings,
  matching the chunked route's order-stable warning union.
- **quality_metrics**: `code_set_corpora` is the only key the chunked route stamps, and
  `code_set` is not a native strategy in this slice, so this is `{}` by construction. Assert
  it, do not silently drop it.

The adaptation is the seam's correctness surface: the returned tuple must be
indistinguishable from the chunked-oracle tuple for the same job, except for values that are
legitimately route-specific (timings, boundary ms). A test asserts field-by-field equality of
outputs/warnings/quality_metrics against the chunked-oracle run.

### 2.3 Go-live control

Cam's decision is to wire it live, so for an admitted non-FK table the native route becomes
the default, the same way the reorder driver became the default inside the out-of-core route
(Task 7). A config override disables it as a safety valve: `native_route_enabled` in
`global_settings`, default true (live), and when false the chunked branch always runs the
oracle. This mirrors the reorder route's `out_of_core_reorder_threshold_rows` override rather
than hiding the feature behind an off-by-default flag. The override exists to force the oracle
for a specific job under investigation, not as the normal path.

### 2.4 Route evidence

The routed `ExecutionResult`'s route evidence must record that the native route ran, with the
per-node `native_kernel` / `native_pool` / `oracle` tags `NativeRouteEvidence` already carries,
so a caller (and the acceptance tests) can prove the intended route executed. A native-admitted
job whose evidence shows `oracle` is a gate failure, not a silent success.

## 3. Failure modes and fallback

1. **Native admission miss at preflight** (FK edge, non-scalar node, unsupported strategy,
   column mismatch). Route evidence records the coded reroute reason; the oracle runs. Not an
   error.
2. **Compiled crypto extension unavailable** and a `hash` node is present. Whole table
   downgrades to the oracle (`crypto_extension_unavailable`), fail-before-output, never a
   partial-native table. Not an error.
3. **Schema drift across chunks** (`NativeChunkSchemaDriftError`). A later chunk's schema
   differs from the admitted first chunk. This is a coded error, not a fallback: the table was
   admitted on a schema the source then violated, which is a data-integrity fault the caller
   must see.
4. **Parity divergence** native vs oracle. Cannot happen silently: it is a gate failure caught
   by the acceptance suite before this ships, and the byte-parity contract is frozen.
5. **`native_route_enabled = false`**. The chunked branch runs the oracle for every table,
   identical to today's behavior.

## 4. Acceptance tests

Every test asserts against the pinned pandas oracle, the frozen bar.

1. **Byte-parity through the live seam.** For a non-FK table covering the five native
   strategies across the admitted type surface (utf8, large_utf8, bool, int8/16/32/64,
   uint*, timestamp-with-tz), the routed result with `native_route_enabled=true` is
   byte-identical (values, row order, null placement, warnings) to both the chunked-oracle
   result and the full-frame oracle result. Uses the existing `assert_logical_parity` plus a
   field-by-field tuple comparison of the `run_mask_chunked` return.
2. **Native route provably ran.** The same job's route evidence shows `native_admitted=true`
   and every node tagged `native_kernel` / `native_pool`, `compiled_kernel_executed=true`
   when a hash node is present. An assertion that no node ran `oracle`.
3. **Flat memory preserved end-to-end.** Peak RSS through the routed seam is flat in row count
   (native peak at 4x <= 1.5x its 1x value) and under the frozen Phase-1 ceiling, measured with
   the existing external-VmHWM bench harness driving the PRODUCTION router (not the native
   entry directly). This proves the seam did not reintroduce a full-frame materialization.
4. **FK tables never go native.** A table on either side of a declared relationship routes to
   out_of_core or oracle, never native (`fk_relationship_not_native_route`), asserted through
   the production router.
5. **Every fallback path lands on the oracle, byte-identical.** Admission miss, crypto
   extension unavailable, and `native_route_enabled=false` each produce the oracle result and
   the correct route evidence. The crypto-unavailable case is simulated the way the existing
   loader tests do.
6. **Schema drift raises, not falls back.** A source whose second chunk drifts from the
   admitted first-chunk schema raises `NativeChunkSchemaDriftError` through the router.
7. **Mutation bar.** The changed seam logic (admission branch, tuple reconstruction, override
   handling) meets the Phase 2-3 mutation bar on the changed units.

Non-regression: the full existing suite stays green, and no Part-1 gate or the FK byte-parity
contract is weakened.

## 5. Scope

In: the chunked-branch admission call, the interface adaptation, the `native_route_enabled`
override, route evidence surfacing, and the acceptance suite above.

Out (later P4-C slices, not this one): porting `fpe`, `categorical`, `text_redact`,
`text_mask`, `code_set`, `bucket_perturb`, `group_key` to the native route; the FPE native
kernel; the per-value row-error channel that `date_shift` / `bucketize` / `bucket_perturb`
need; widening the faker provider allowlist beyond C1; the platform-side
`classify_streaming_eligibility` adoption (that is the platform leg, tracked separately).

## 6. Open questions for the plan-gate

- Confirm the seam belongs in `run_mask_chunked` rather than one level up in
  `decide_execution_route`. `run_mask_chunked` keeps the native decision adjacent to the chunk
  execution it replaces and reuses the resolved profile; a routing-layer placement would split
  the decision from the execution. Proposed: keep it in `run_mask_chunked`.
- Confirm `native_route_enabled` default true (live) is the intended go-live posture versus an
  off-by-default flag flipped in a follow-up. Cam's decision was to wire it live, so default
  true; the plan-gate should confirm no release-phase gate requires otherwise.
- Confirm the timings rollup contract: is an empty per-column timing acceptable where the
  native route has no comparable measurement, or must every masked column carry a timing? The
  chunked route's own rollup is the reference.
