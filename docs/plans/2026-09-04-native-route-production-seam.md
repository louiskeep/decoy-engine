Status: plan

# Q3 slice 1: a single-pass streaming native lane for string-column passthrough, redact, truncate

The native columnar-streaming route is built and gated but nothing in production selects it, so
real mask jobs run the pandas oracle and pay its row-linear memory. This slice makes the native
route a real production path for the three pure-kernel strategies (`passthrough`, `redact`,
`truncate`) on `utf8` string columns only. It builds the streaming lifecycle those strategies need
(a lane that branches before the source is materialized, owns a streaming transactional sink, and
refuses any job feature or column shape it cannot reproduce exactly), proven end to end through the
production entry, as a single pass over the source.

Two deliberate narrowings, both to keep the foundation small and provably correct:

- Only the three pure-kernel strategies, which use the in-process Python kernel
  (`decoy_engine.kernel._scalar`), so no Rust companion and no pool quality are involved and the
  whole test surface runs in normal CI. `faker` and keyed `hash` are later slices.
- Only `utf8` string columns. The pandas oracle changes physical Arrow types based on whole-column
  null state for integer (`int -> double` when a null is present) and boolean columns, which would
  force a whole-source preflight and a two-pass source-identity problem. String columns do not
  drift on nulls, so string-only admission is decidable from the batch schema alone and the lane
  stays a single pass. Integer, boolean, and timestamp columns move to a follow-on slice that
  builds the preflight and source-snapshot machinery on purpose. Strings are the dominant masking
  target (names, emails, identifiers, free text), so this covers the common case while keeping the
  lifecycle work isolated.

Enablement is a validated runtime option defaulting OFF. Flipping the default and adding `hash`,
`faker`, and the wider type surface are later slices.

## 1. Current reality and why a seam is not enough

Route selection is a ladder: layer-1 sends FK jobs to the bounded out-of-core route; layer-2
(`decide_chunk_route`) picks chunked vs full_frame for single-table non-FK jobs. The native route
has the layer-2 admission shape, so it is naturally a third layer-2 outcome. But the plan-gate
established that placing native inside the existing chunked executor cannot deliver the memory
win: the pipeline materializes every `LazySource` into a full in-memory table
(`resolve_resident_sources`, `_pipeline.py`) before the layer-2 split, and finalization (fidelity
reports, validators, quarantine, result assembly) plus the chunked executor itself all operate on
complete materialized outputs. So the native path must be a dedicated lane that branches right
after layer-1 routing and BEFORE source materialization, streams the source in batches once, and
refuses any job that needs a whole-dataset pass. A test asserts `resolve_resident_sources` is
never called on the native path.

## 2. Design

### 2.1 The dedicated single-pass streaming lane

A native lane branches after layer-1 (FK) routing, before `resolve_resident_sources`. It is
selected only when `native_route_enabled` is on, `execution_mode == "auto"`, the source is a
`LazySource` (see 2.5), the config admits (2.3), and the FIRST batch of the execution iterator
admits (2.2). It consumes the source as batches (`LazySource.iter_batches()`) exactly once, masking
and writing each batch as it goes, never materializing the whole source and never reading it twice.
Admission is taken from that first execution batch, not a separate schema read, so there is no
second open of the source and no no-first-batch race.

### 2.2 First-batch admission (why one pass is exact)

Admission is decided from the first execution batch, with no whole-source pass, because the
admitted strategies on `utf8` columns produce an output type that does not depend on the column's
null state:

- `passthrough` on `utf8` preserves `utf8`; the oracle round-trip also yields `utf8` (string).
  This is not true of `large_utf8` (the oracle yields `string`, a width drift the parity harness
  does not allow by default and which the plan-gate forbids papering over with a broad
  `PhysicalDiff`), so `large_utf8` is rejected. Admission pins on `field.type == pa.utf8()`
  exactly, with no dictionary unwrapping: a `dictionary<*, utf8>` column is rejected, because the
  oracle and native disagree on the index width (`dictionary<string, int8>` vs
  `dictionary<string, int32>`).
- `redact` outputs `string` only when its `redact_with` fill is a string; a non-string
  `redact_with` is rejected (the existing `redact_config_rejection` is an explicit production-lane
  prerequisite). `truncate` always outputs `string`. Their `utf8` inputs stay `string` on both
  routes.
- An all-null `utf8` column resolves to the one physical difference the harness already allows
  (the null-typed normalization).

A zero-row source is the one shape first-batch admission cannot make exact: on an empty column the
oracle yields `null` for passthrough (allowed) but `double` for `redact` / `truncate`, while native
constructs `string` (a `double`-vs-`string` drift the harness does not allow). So a zero-batch
source reroutes to the oracle before any output, matching the existing native coordinator's
behavior. This is also why admission reads the first execution batch: an iterator that yields
nothing takes the oracle reroute.

So the admission matrix is exact: admit `passthrough` / `truncate`, and `redact` with a string
`redact_with`, on an exact `pa.utf8()` column of a non-empty source; reject every other Arrow type
(`large_utf8`, `dictionary<*, utf8>`, integer, unsigned integer, boolean, floating, timestamp,
`decimal128`, binary, nested, null, and the rest), a non-string `redact_with`, and a zero-row
source, routing those tables to the oracle. Later batches are checked for schema drift; a drifting
batch aborts (see 2.6). No integer null guard, no preflight, and no source fingerprint are needed,
because no admitted column's output type depends on data the first batch does not already reveal.

### 2.3 Reject-before-output contract (closed world)

Before any output, the lane reroutes to the oracle every feature it cannot honor while streaming.
Rerouting is clean, not an error, and happens before the first batch is masked:

- FK participation (already at layer-1, reasserted).
- `vault: true` on any column (generic native eligibility has no vault check today, so the three
  strategies would otherwise admit with no streaming vault path).
- any validator, fidelity-report request, or quarantine / row-error configuration.
- a sink the lane cannot stream to (2.4).
- unsupported projection, generation columns, or multi-table jobs.
- a non-`None` `source_loader`, or any non-`LazySource` source entry (2.5).
- a zero-row source (empty first batch), which the oracle types differently for `redact` /
  `truncate` (2.2).
- any strategy off the three-strategy allowlist, any column whose type is not exact `pa.utf8()`
  (including `large_utf8` and `dictionary<*, utf8>`), or a `redact` node with a non-string
  `redact_with`.

A closed-world admission sentry (test) enumerates the admitted capabilities and fails the build if
any admitted strategy carries a row-error mode, a quarantine dependency, a pool-quality
obligation, or any `quality_obligation` / `warning_code` this slice does not explicitly implement.

### 2.4 Layer-2 selection, precedence, and the sink capability

The native decision runs in `decide_chunk_route`, which already holds the config, the compiled
`Plan`, the resolved registry, and source metadata (`_pipeline.py`). A plan-aware admission and
execution API takes the existing compiled `Plan` and resolved registry, so the decision does pure
config/plan/schema inspection with no I/O, no recompile, and no `get_default_registry()`.

Routing precedence is defined and tested as a matrix. The native early return fires only when
`execution_mode == "auto"`, so an explicit `sequential`, `full_frame`, or `out_of_core` mode is
never swallowed; merely moving the native check below those returns is not sufficient, so the
dispatch is guarded on the mode explicitly. Layer-1 FK routing still precedes native. After native,
the existing chunked vs full_frame decision is unchanged. An explicit non-pandas substrate falls
through to the existing decision.

The sink predicate is literal, not structural, because the structural `TransactionalSink`
interface does not guarantee bounded consumption (the callable adapter materializes `list(batches)`
in `_transactional_sink.py`): `sink is None` selects the resident-output native mode (no flat-RSS
claim, the whole output resides in memory because the caller asked for it); `type(sink) is
ParquetTransactionalSink` exactly (not `isinstance`, so a retaining subclass is excluded) selects
the bounded streaming mode; every other sink reroutes to the oracle. Tested by rejecting the
callable adapter, a structural custom sink, and a retaining `ParquetTransactionalSink` subclass.

### 2.5 Source shape

The memory claim holds only for `LazySource` inputs. `caller_sources` is built from the caller's
`sources` mapping and may carry resident `pa.Table` objects; the native lane engages only for a
`LazySource` entry and leaves a resident-table entry on the existing path. A `source_loader` is a
separate mechanism the layer-2 decision does not resolve, so native admission explicitly requires
`source_loader is None` (a guard threaded to the decision), and a loader-driven job takes the path
it takes today. Tests present a non-`None` loader alongside an otherwise-native-admissible
`LazySource` (a resident source would be a vacuous test), a resident-table entry, and a plain
`LazySource`, asserting each lands on the correct behavior.

### 2.6 Streaming transactional publication and the sink=None contract

With the streaming Parquet sink the lane stages, drains the whole single pass, validates route
evidence and the ledger, then atomically commits; any error (a mid-stream kernel error, a
schema-drift abort, a ledger-validation failure, a commit failure) discards the staged artifact
and raises a coded error, with no oracle fallback after the first native output. With `sink=None`
the whole output resides in memory because the caller asked for it, so peak RSS is about one
output dataset, not the flat-streaming figure; the win even there is that the source is never
fully materialized alongside an intermediate frame and the output at once. The flat-RSS acceptance
test uses the streaming sink, where the guarantee is real.

### 2.7 Structured native result and telemetry

The lane returns a structured result carrying every field the routed `ExecutionResult` needs,
measured not fabricated: per-column timings (not strategy totals split across columns), the
measured boundary-conversion time, and route evidence. Peak memory keeps its established meaning
(`StrategyTimingRecord`'s before/after delta stays a delta; the route memory gate uses an external
fresh-process VmHWM). Warnings carry only oracle-equivalent `QualityWarning`s in chunk order
(empty for these three strategies, asserted). Strategy-local `quality_metrics` are empty while the
existing `quality_metrics["execution"]` envelope is present and unchanged; both are asserted.
`ExecutionResult` gains a route-evidence field, and all existing fields and telemetry are preserved
on the native path.

### 2.8 Restored invocation-scoped route ledger

The frozen Part-1 gate requires proof that a native job made zero oracle calls, zero oracle rows,
zero fallback calls, zero fallback rows, and zero rejected chunks. This slice restores an
invocation-scoped ledger whose counters increment at the actual native / oracle / fallback call
boundaries (not derived from a planned route table): attempted and completed call and row
counters, a rejected-chunk counter, and exact `(table, work-node identity, chunk index)` records.
Acceptance requires `attempted == completed` after a successful commit, every frozen Part-1 count
zero, job completion only after the sink commit, and a fail-fast spy on the oracle chunked entry
that fails the test on any stray invocation.

### 2.9 Enablement

`native_route_enabled` is a validated runtime option on `run_pipeline` (a kwarg alongside the
other routing controls), default False, threaded to `decide_chunk_route`. It is not a
`GlobalSettings` field. Flipping the default is a later slice.

## 3. Failure modes

1. Admission miss, a rejected whole-dataset feature, a non-streaming sink, a non-`LazySource`
   source, a non-`None` loader, a non-exact-`utf8` column, a non-string `redact_with`, or a
   zero-row source. The job reroutes to the oracle with full production dependencies before any
   native output; the ledger records the coded reason. Not an error.
2. `native_route_enabled=False` or a non-`auto` execution mode. Native is never selected; behavior
   is identical to today.
3. A post-first-output failure (mid-stream kernel error, schema drift, ledger-validation failure,
   commit failure). The staged artifact is discarded and a coded error is raised; no oracle retry.
   A hard failure, by design.
4. Parity divergence native vs oracle. A gate failure caught before ship; the byte-parity contract
   is frozen.

## 4. Acceptance tests

Every test asserts against the pinned pandas oracle. Routing tests drive the production entry
(`run_pipeline` with `native_route_enabled=True`), not the native function. No companion is
involved, so the whole surface runs in normal CI. Physical-schema parity uses the existing harness
rules on the committed Parquet artifact, with no new broad `PhysicalDiff` allowance.

1. **Byte and physical-schema parity through the production entry**: `passthrough` / `truncate`
   and `redact` (string `redact_with`) on non-empty `utf8` columns including non-null, null-bearing,
   and all-null cases. Byte-identical values, row order, null placement, ordered warnings, and
   physical schema to both chunked-oracle and full-frame-oracle.
2. **Rejected shapes reroute, byte-identical**: `large_utf8`, `dictionary<*, utf8>`, `decimal128`,
   integer, unsigned integer, boolean, floating, timestamp, and binary columns; a non-string
   `redact_with`; and a zero-row source (whose `redact` / `truncate` output the oracle types as
   `double`) each route to the oracle with the coded reason.
3. **The native lane provably ran.** After a successful commit, every frozen Part-1 ledger count is
   zero, `attempted == completed`, per-node completed counts equal the chunk count, the job
   completed only after commit, and the oracle-entry spy recorded no call.
4. **Source never materialized and read once.** A spy proves `resolve_resident_sources` is never
   called on the native path and that the source is iterated a single time.
5. **Flat memory with the streaming Parquet sink.** Peak RSS through the production entry is flat in
   row count (4x <= 1.5x the 1x value) and under the frozen Phase-1 ceiling, measured in fresh
   processes over tiers with lazy input and incremental output.
6. **Every rejected feature reroutes to the oracle, byte-identical, with full deps**, parameterized
   over FK, `vault: true` on each strategy, a validator, a fidelity request, quarantine config,
   unsupported projection, a generation column, a multi-table job, and `native_route_enabled=False`,
   each with the correct coded ledger reason and a custom registry. The sink predicate is tested
   separately (callable adapter, structural custom sink, retaining subclass each reroute; `None`
   runs resident mode; exact `ParquetTransactionalSink` runs streaming mode). Source shape is tested
   separately (resident-table entry stays on the existing path; a non-`None` loader alongside an
   otherwise-native-admissible `LazySource` does not fire native).
7. **Transactional failure behavior**, parameterized over late kernel failure, a schema-drift batch,
   ledger-validation failure, and commit failure: no final artifact, staged data cleaned up, zero
   oracle retry, and `attempted`-vs-`completed` ledger state correct at each point.
8. **Routing precedence matrix**: `sequential`, `full_frame`, `out_of_core` overrides, FK, native
   (only under `auto`), and the chunked vs full_frame decision resolve in the defined order.
9. **Closed-world admission sentry** and **compiled-plan/registry reuse** (one compile, honors a
   custom registry, no `get_default_registry()` on the native path).
10. **Mutation bar** on the changed units (lane selection, schema-only admission, reject-before
    -output, structured result, ledger, transactional publish).

Non-regression: the full suite stays green; no Part-1 gate or the FK byte-parity contract is
weakened.

## 5. Scope

In: the dedicated single-pass streaming native lane for `utf8` `passthrough` / `redact` /
`truncate`; schema-only admission; the reject-before-output closed-world contract including
all-strategy vault rejection; layer-2 native selection reusing the compiled plan and registry via a
plan-aware API; the routing precedence matrix guarded on `execution_mode == "auto"`; the literal
streaming-Parquet-sink predicate and the `LazySource`-only, `source_loader is None` input rule; the
`sink=None` memory contract; the structured native result and telemetry preservation; the restored
invocation-scoped route ledger; the `native_route_enabled` runtime option (default False); and the
acceptance suite above, all in normal CI.

Out (later slices, each its own plan and gate): integer, unsigned, boolean, and timestamp column
types on the native lane (needs the whole-source preflight and a source-snapshot identity contract
to handle pandas null-driven type drift); `faker` on the lane; keyed `hash` on the lane plus
publishing the Rust companion; flipping `native_route_enabled` default to True; resident and
`source_loader` input admission; the P4-C payload ports (`fpe`, `categorical`, `text_redact`,
`text_mask`, `code_set`, `bucket_perturb`, `group_key`); the FPE native kernel; the per-value
row-error channel; and the platform-side streaming-eligibility adoption.

## 6. Program sequence (this slice is step 1)

1. This slice: the single-pass string lane for `passthrough` / `redact` / `truncate`.
2. The wider type surface (integer, boolean, timestamp): the whole-source preflight and
   source-snapshot identity contract.
3. `faker` on the lane: streaming or admission-aligned pool-quality enforcement.
4. Keyed `hash` on the lane: the companion-present execution path plus a companion CI lane; in
   parallel, publish the Rust companion so `hash` can go native in a real install.
5. The default-on flip, once the companion is published and its CI lane is green.
6. The P4-C payload ports, each gated, some behind the row-error channel.
