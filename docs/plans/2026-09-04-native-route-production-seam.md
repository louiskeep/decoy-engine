Status: plan

# Q3 slice 1: a streaming native lane for passthrough, redact, truncate

The native columnar-streaming route is built and gated but nothing in production selects it,
so real mask jobs run the pandas oracle and pay its row-linear memory. This slice makes the
native route a real production path for the three pure-kernel strategies that need neither the
Rust companion nor the pool-quality machinery: `passthrough`, `redact`, `truncate`. It builds
the streaming lifecycle those strategies need (a lane that branches before the source is
materialized, resolves the exact oracle output schema up front, owns a streaming transactional
sink, and refuses any job feature or column shape it cannot reproduce exactly), proven end to
end through the production entry. `faker`, keyed `hash`, and the wider payload ports layer onto
this lane in later slices.

The narrowing is deliberate. Wiring the full five-strategy route live couples three hard
problems: the streaming lifecycle, `faker`'s pool-quality check (which as built needs the whole
dataset at once), and keyed `hash`'s Rust companion (unpublished, no installable path). The three
chosen strategies use the in-process Python kernel (`decoy_engine.kernel._scalar`), carry no
pool-quality obligation, and emit no row errors, so this slice isolates the foundational lifecycle
work and, because no companion is involved, its whole test surface runs in normal CI.

Enablement is a validated runtime option defaulting OFF. Flipping the default and adding `hash`
and `faker` are later slices with their own gates.

## 1. Current reality and why a seam is not enough

Route selection is a ladder: layer-1 sends FK jobs to the bounded out-of-core route; layer-2
(`decide_chunk_route`) picks chunked vs full_frame for single-table non-FK jobs. The native route
has the layer-2 admission shape, so it is naturally a third layer-2 outcome. But the plan-gate
established that placing native inside the existing chunked executor cannot deliver the memory
win: the pipeline materializes every `LazySource` into a full in-memory table
(`resolve_resident_sources`, `_pipeline.py`) before the layer-2 split, and finalization (fidelity
reports, validators, quarantine, result assembly) plus the chunked executor itself all operate on
complete materialized outputs. So the native path must be a dedicated lane that branches right
after layer-1 routing and BEFORE source materialization, streams the source in batches, and
refuses any job that needs a whole-dataset pass. A test asserts `resolve_resident_sources` is
never called on the native path.

## 2. Design

### 2.1 The dedicated streaming lane

A native lane branches after layer-1 (FK) routing, before `resolve_resident_sources`. It is
selected only when `native_route_enabled` is on, the source is a `LazySource` (see 2.5), the
table admits, and the job carries none of the rejected whole-dataset features (2.3). It consumes
the source as batches (`LazySource.iter_batches()`), never materializing the whole source.

### 2.2 Bounded preflight and the physical-parity admission matrix

The pandas oracle's output type depends on whole-column data, so per-batch admission is not
sufficient for exact parity. Three concrete cases (verified in the code):

- `passthrough` on `large_utf8` round-trips through pandas to `string`; the native kernel keeps
  `large_utf8`. The parity harness allows only the null-typed normalization, not string-width
  drift (`_fixtures.py`), and per the plan-gate a broad new `PhysicalDiff` allowance is not an
  acceptable fix.
- `passthrough` on a nullable integer column becomes `double` under pandas when a null is
  present; the native kernel keeps the integer width.
- `truncate` (and `hash`, `categorical`) on an integer column containing any null is rejected
  outright by the oracle after examining the complete column (`reject_null_bearing_int` in
  `_guards.py`). A null-free first batch would admit, then a later null batch would diverge.

So the lane runs a bounded-memory preflight pass over the source before any output: one streaming
pass that records, per column, `is_empty`, `has_null`, and the physical Arrow type (O(columns)
state, no materialization). From that it resolves a per-strategy, per-Arrow-type admission matrix
and the exact oracle-equivalent output schema, fixed before the first sink write:

- reroute to the oracle before output (never a native drift): `passthrough` on `large_utf8`;
  `passthrough` on an integer column with `has_null`; any integer column with `has_null` under
  `truncate` (the oracle rejects it, so the reroute lets the oracle raise the identical
  `ExecutionError`); and any shape the matrix does not explicitly admit.
- admit natively: `passthrough`/`redact`/`truncate` on the concrete types proven identical
  (utf8 for the string strategies, bool and tz-timestamp and null-free integers for passthrough),
  with empty and all-null cases resolved to the oracle-equivalent physical type (the existing
  null-typed normalization is the one allowed diff).

The matrix is exhaustive over `{strategy} x {Arrow type} x {empty, all-null, has-null, no-null}`;
any cell not proven identical reroutes. As defense in depth, the streaming executor also carries a
per-batch integer guard for `truncate` that aborts and discards staged output if a null integer
appears despite the preflight (belt and suspenders against a source whose batches disagree with
its own preflight), tested with a late-null batch.

The preflight costs one extra streaming read of the source (bounded memory, not a
materialization); this is the honest price of exact parity on a streaming route and matches the
out-of-core route's own preflight discipline.

### 2.3 Reject-before-output contract (closed world)

Before any output, the lane reroutes to the oracle every feature it cannot honor while streaming.
Rerouting is clean, not an error, and happens before the first batch is masked:

- FK participation (already at layer-1, reasserted).
- `vault: true` on any column (new: generic native eligibility has no vault check today, so the
  three strategies would otherwise admit with no streaming vault path).
- any validator, fidelity-report request, or quarantine / row-error configuration.
- a sink the lane cannot stream to (2.4).
- unsupported projection, generation columns, or multi-table jobs.
- any non-`LazySource` source (2.5).
- any strategy off the three-strategy allowlist, or any column shape the 2.2 matrix does not admit.

A closed-world admission sentry (test) enumerates the admitted capabilities and fails the build if
any admitted strategy carries a row-error mode, a quarantine dependency, a pool-quality
obligation, or any `quality_obligation` / `warning_code` this slice does not explicitly implement.

### 2.4 Layer-2 selection, precedence, and the sink capability

The native decision runs in `decide_chunk_route`, which already holds the config, the compiled
`Plan`, the resolved registry, and source metadata (`_pipeline.py`). A plan-aware admission and
execution API takes the existing compiled `Plan` and resolved registry, so the decision does pure
config/plan inspection with no I/O and no recompile and no `get_default_registry()`. Only the
actual-first-batch schema/type validation stays at the executor boundary.

Routing precedence is defined and tested as a matrix. Every explicit `execution_mode`, meaning
`sequential`, `full_frame`, and `out_of_core`, wins first (unchanged). Then layer-1 FK routing.
Then the native lane (only when enabled and admitting). Then the existing chunked vs full_frame
decision. An explicit non-pandas substrate falls through to the existing decision.

"Streaming sink" is not guaranteed by the structural `TransactionalSink` interface: the callable
sink adapter materializes its batch iterable (`_transactional_sink.py`). So this slice admits
native only for the known bounded streaming Parquet sink, and reroutes any callable or otherwise
retaining sink to the oracle. This is a nominal capability, tested by rejecting the callable
adapter and a deliberately retaining custom sink.

### 2.5 Source shape and the sink=None contract

The memory claim holds only for `LazySource` inputs. `caller_sources` may also carry resident
`pa.Table` objects or data from `source_loader`; this slice routes only `LazySource` inputs to the
native lane and reroutes resident/`source_loader` inputs to the oracle (a later slice may admit
them with an explicit resident-input contract). Tests cover `LazySource`, resident tables, and
`source_loader`.

When the caller provides the streaming Parquet sink, the lane stages, drains the whole stream,
validates route evidence and the ledger, then atomically commits; any error discards the staged
artifact and raises a coded error, with no oracle fallback after the first native output. When
`sink=None` the whole output resides in memory because the caller asked for it, so peak RSS is
about one output dataset, not the flat-streaming figure; the win even there is that the source is
never fully materialized alongside an intermediate frame and the output at once. The flat-RSS
acceptance test uses the streaming sink, where the guarantee is real.

### 2.6 Structured native result and telemetry

The lane returns a structured result carrying every field the routed `ExecutionResult` needs,
measured not fabricated: per-column timings (not strategy totals split across columns), the
measured boundary-conversion time, and route evidence. Peak memory keeps its established meaning:
`StrategyTimingRecord`'s before/after delta stays a delta, and the route memory gate uses an
external fresh-process VmHWM. Warnings carry only oracle-equivalent `QualityWarning`s, in chunk
order (empty for these three strategies, asserted). Strategy-local `quality_metrics` are empty,
while the existing `quality_metrics["execution"]` envelope is present and unchanged; both facts
are asserted. `ExecutionResult` gains a route-evidence field, and all existing fields and
telemetry are preserved on the native path.

### 2.7 Restored invocation-scoped route ledger

The frozen Part-1 gate requires proof that a native job made zero oracle calls, zero oracle rows,
zero fallback calls, zero fallback rows, and zero rejected chunks. This slice restores an
invocation-scoped ledger whose counters are incremented at the actual native / oracle / fallback
call boundaries (not derived from a planned route table): attempted and completed call counters
and row counters, a rejected-chunk counter, and exact `(table, work-node identity, chunk index)`
records. Acceptance requires `attempted == completed` after a successful commit, every frozen
Part-1 count zero, job completion only after the sink commit, and a fail-fast spy on the oracle
chunked entry that fails the test on any stray invocation.

### 2.8 Enablement

`native_route_enabled` is a validated runtime option on `run_pipeline` (a kwarg alongside the
other routing controls), default False, threaded to `decide_chunk_route`. It is not a
`GlobalSettings` field. Flipping the default is a later slice.

## 3. Failure modes

1. Admission miss, a rejected whole-dataset feature, a non-streaming sink, a non-`LazySource`
   source, or a column shape the matrix does not admit. The job reroutes to the oracle with full
   production dependencies before any native output; the ledger records the coded reason. Not an
   error.
2. `native_route_enabled=False`. Native is never selected; behavior is identical to today.
3. A post-first-output failure (mid-stream kernel error, the defense-in-depth truncate guard,
   schema drift, validator/ledger failure, commit failure). The staged artifact is discarded and a
   coded error is raised; no oracle retry. A hard failure, by design.
4. Parity divergence native vs oracle. A gate failure caught before ship; the byte-parity contract
   is frozen.

## 4. Acceptance tests

Every test asserts against the pinned pandas oracle. Routing tests drive the production entry
(`run_pipeline` with `native_route_enabled=True`), not the native function. No companion is
involved, so the whole surface runs in normal CI. Physical-schema parity uses the existing
harness rules on the committed Parquet artifact, with no new broad `PhysicalDiff` allowance.

1. **Byte and physical-schema parity through the production entry**, over the admission matrix:
   `passthrough`/`redact`/`truncate` across utf8, bool, tz-timestamp, null-free integers, plus the
   empty and all-null cases the matrix admits. Byte-identical values, row order, null placement,
   ordered warnings, and physical schema to both chunked-oracle and full-frame-oracle.
2. **Drift-prone shapes reroute, byte-identical**, parameterized: `passthrough` on `large_utf8`,
   `passthrough` on a null-bearing integer, `truncate` on a null-bearing integer (oracle raises
   the same `ExecutionError`), null-only-in-a-later-batch integer `truncate` (proves preflight
   reroute, or the defense-in-depth guard aborts and discards with no oracle retry).
3. **The native lane provably ran.** After a successful commit, every frozen Part-1 ledger count
   is zero, `attempted == completed`, per-node completed counts equal the chunk count, the job
   completed only after commit, and the oracle-entry spy recorded no call.
4. **Source never materialized.** A spy proves `resolve_resident_sources` is never called on the
   native path.
5. **Flat memory with the streaming Parquet sink.** Peak RSS through the production entry is flat
   in row count (4x <= 1.5x the 1x value) and under the frozen Phase-1 ceiling, measured in fresh
   processes over tiers with lazy input and incremental output.
6. **Every rejected feature reroutes to the oracle, byte-identical, with full deps**,
   parameterized over FK, `vault: true` on each strategy, a validator, a fidelity request,
   quarantine config, the callable sink, a retaining custom sink, resident-table and
   `source_loader` inputs, unsupported projection, a generation column, a multi-table job, and
   `native_route_enabled=False`, each with the correct coded ledger reason and a custom registry.
7. **Transactional failure behavior**, parameterized over late kernel failure, the truncate late
   -null guard, ledger-validation failure, and commit failure: no final artifact, staged data
   cleaned up, zero oracle retry, and `attempted`-vs-`completed` ledger state correct at each point.
8. **Routing precedence matrix**: `sequential`, `full_frame`, `out_of_core` overrides, FK, native,
   and the chunked vs full_frame decision resolve in the defined order.
9. **Closed-world admission sentry** and **compiled-plan/registry reuse** (one compile, honors a
   custom registry, no `get_default_registry()` on the native path).
10. **Mutation bar** on the changed units (lane selection, preflight/admission matrix,
    reject-before-output, structured result, ledger, transactional publish).

Non-regression: the full suite stays green; no Part-1 gate or the FK byte-parity contract is
weakened.

## 5. Scope

In: the dedicated streaming native lane; the bounded preflight and physical-parity admission
matrix; the reject-before-output closed-world contract including all-strategy vault rejection;
layer-2 native selection reusing the compiled plan and registry via a plan-aware API; the routing
precedence matrix including `sequential`; the streaming-Parquet-sink-only capability and the
`LazySource`-only input rule; the `sink=None` memory contract; the structured native result and
telemetry preservation; the restored invocation-scoped route ledger; the `native_route_enabled`
runtime option (default False); and the acceptance suite above, all in normal CI.

Out (later slices, each its own plan and gate): `faker` on the lane (streaming or admission
-aligned pool quality); keyed `hash` on the lane (Rust companion path plus publishing the
companion); flipping `native_route_enabled` default to True; resident and `source_loader` input
admission; the P4-C payload ports (`fpe`, `categorical`, `text_redact`, `text_mask`, `code_set`,
`bucket_perturb`, `group_key`); the FPE native kernel; the per-value row-error channel; and the
platform-side streaming-eligibility adoption.

## 6. Program sequence (this slice is step 1)

1. This slice: the streaming lane for `passthrough` / `redact` / `truncate`.
2. `faker` on the lane: streaming or admission-aligned pool-quality enforcement.
3. Keyed `hash` on the lane: the companion-present execution path plus a companion CI lane; in
   parallel, publish the Rust companion so `hash` can go native in a real install.
4. The default-on flip, once the companion is published and its CI lane is green.
5. The P4-C payload ports, each gated, some behind the row-error channel.
