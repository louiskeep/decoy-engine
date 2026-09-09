Status: draft for Cam approval
Created: 2026-09-09
Primary repo: `decoy-engine`
Related repo: `decoy-platform`
Risk: R2, because this program affects architecture, compatibility, concurrency, packaging, performance, and cryptographic code

# Execution Consolidation and Native Throughput

## 1. Executive decision

Do not rewrite the Decoy engine in Rust.

Keep Python as the product and control layer. Use DuckDB for relational work. Use Arrow as the batch boundary. Use Rust for expensive deterministic operators.

Consolidate the current execution routes behind one physical plan and one coordinator. Keep the pandas full-frame path as the compatibility oracle.

The program has two outcomes:

1. The first outcome is 100 million rows in 10 minutes on the approved reference host.
2. The second outcome is one coherent execution model that is easy to explain, operate, and extend.

The first outcome does not depend on the full consolidation. Complete the focused throughput work before the broad structural work.

## 2. Product intent

Decoy must feel like one product, not a set of connected execution experiments.

A multi-language implementation is acceptable. Overlapping ownership and hidden route changes are not acceptable.

The final product description is:

> Decoy compiles one execution plan and streams Arrow batches through it. DuckDB handles relational work. Rust handles expensive deterministic transforms. Python handles configuration, planning, providers, and publication.

The architecture must give users these properties:

- Performance is predictable for a declared workload and host.
- Memory use stays bounded as row count increases.
- Output does not change when a backend changes.
- A job report identifies every selected backend.
- A missing accelerator never causes silent partial execution.
- Installation behavior is explicit for each supported machine.
- Unsupported large jobs fail before output or use an approved oracle route.

## 3. Evidence and problem statement

### 3.1 Current throughput

The representative masking workload runs near 25,000 rows per second on one core. Memory is bounded, but wall time grows with row count.

At 400 million rows, masking takes approximately 4.7 hours. The DuckDB foreign-key join build takes approximately 6 minutes.

Masking is the measured bottleneck. Additional relational engines do not remove this bottleneck.

The target is a 6x to 8x throughput increase. The target converts 100 million rows from approximately 65 minutes to 10 minutes.

### 3.2 Current native result (authoritative reference-host baseline)

The authoritative anchor is the Task 0.2 baseline on the frozen reference host
(GCP n2-standard-8), NOT the 4-core devbox cert. See
`docs/plans/native-throughput-phase0-baseline.md`.

Measured 100M native on n2-standard-8: **1,571.77 s** wall, peak RSS 463.3 MB
(flat). Per-strategy split from the raw run: keyed-hash **1,280.11 s** (3 cols),
redact 124.5 s, truncate 155.5 s, passthrough 0.3 s. So the non-hash serial floor
is **~291.7 s** and the available hash budget to hit 600 s is ~308 s, requiring
**~4.15x** on the hash kernel (or ~2.6x on total wall).

TWO risks this creates, both gated in Phase 1:

1. **Serial floor.** redact + truncate (~280 s) and IO sit OUTSIDE Phase 1's
   keyed-derivation scope and stay serial. Driving hash to ~308 s leaves total
   ~600 s with near-zero margin. Hitting 600 s may therefore also require
   parallelizing or overlapping redact + truncate; that follow-on is pre-planned,
   not assumed away.
2. **Core topology.** n2-standard-8 is **4 physical cores + hyperthreading (8
   vCPUs)**, so 8-thread Rayon likely yields ~4-5x, not 8x. At perfect 8-way
   scaling the projection is ~452 s; at only 4x it is ~612 s, already a miss.
   Effective scaling is therefore the single biggest risk to 600 s.

Because 600 s is a frozen Cam-approved target, Task 1.1 is a **feasibility gate**
(see below): measured cached-HMAC cost plus a conservative scaling model must
project <= 600 s on the reference host BEFORE Tasks 1.2-1.6 build; if it does not,
STOP and bring the host/scope decision (bigger host, or parallelize redact+truncate)
to Cam rather than building the full kernel and discovering the miss at Task 1.6.

Corroboration: the 4-core devbox Phase 2 cert (1,563.39 s / hash 1,218.96 s;
`native-testing-T6-e2e.md` §3) is within 0.5% of the reference-host wall, so the
single-core speeds are close.

### 3.2.1 Throughput contract scope (mask-only)

The 600 s contract is **mask-only throughput**: the baseline and every Phase 1
perf gate count and drop returned Arrow batches (no publication write), so they
measure the mask compute path on equal terms. Publication cost (the transactional
Parquet sink) is a SEPARATE, Phase-4 concern and is explicitly NOT inside the 600 s
number. A future gate must not silently compare a count/drop baseline against a
Parquet-writing run.

### 3.3 Immediate kernel defect in the performance design

The Rust batch loop calls `derive()` for each non-null row. Each call computes the same namespace HKDF key again.

The namespace key depends only on the seed and namespace. It does not depend on the source value.

The Python implementation already has `DeriveContext` for this optimization. The Rust batch implementation does not use an equivalent context.

This repeated work is the first optimization target. It has lower structural risk than a wider native migration.

Relevant code:

- `decoy-engine-native/src/derive.rs::hkdf_key`
- `decoy-engine-native/src/derive.rs::derive`
- `decoy-engine-native/src/batch.rs::derive_array`
- `src/decoy_engine/determinism/_derive.py::DeriveContext`

### 3.4 Faker pool selection is not compiled

The native Faker route builds a bounded pool once. The route then calls the Python `PoolSampler` for each chunk.

The deterministic sampler calls `derive_index` once for each non-null source row. This is the same keyed derivation family as hash.

The existing Rust companion does not provide `derive_index_batch`. The native Faker route does not receive the compiled crypto speed gain.

Relevant code:

- `src/decoy_engine/execution/native/_dispatch.py`
- `src/decoy_engine/generation/pool/_sampler.py`
- `src/decoy_engine/determinism/_derive.py::derive_index`

### 3.5 Current architecture cost

The engine has several execution paths:

- pandas full-frame
- pandas chunked
- DuckDB out-of-core
- native columnar
- pandas and Polars execution adapters

Each path is useful in its present scope. Their overlapping decisions create long-term product risk.

The risk includes duplicated eligibility logic, route-specific diagnostics, different publication paths, and performance changes caused by hidden fallback.

The consolidation work removes this overlap in recoverable slices. It does not replace all routes at once.

## 4. Goals

### 4.1 Performance goals

1. Process the frozen 100-million-row W2 workload in 600 seconds or less.
2. Use the approved eight-core reference host and the approved batch size.
3. Keep peak RSS within the frozen memory limit.
4. Preserve flat RSS growth across the measured row tiers.
5. Record median, variance, tail, CPU use, thread count, and spill use.

Phase 0 must record the exact host before implementation starts. The plan does not treat a different host as equivalent evidence.

### 4.2 Correctness goals

1. Preserve exact logical and byte behavior required by the current contract.
2. Preserve row order, null positions, output types, warnings, row errors, and failure codes.
3. Preserve deterministic output across process, batch, and thread boundaries.
4. Preserve the current no-midstream-fallback rule.
5. Preserve the pandas full-frame path as the pinned oracle.

### 4.3 Architecture goals

1. Produce one internal physical plan for production execution.
2. Select each operator backend during preflight.
3. Use one batch coordinator for input, execution, diagnostics, staging, and publication.
4. Use DuckDB for scans, joins, aggregation, sorting, and spill.
5. Use Arrow `RecordBatch` as the operator boundary.
6. Use Rust for measured CPU-bound deterministic operators.
7. Use bounded Python operators only where exact behavior depends on Python.
8. Keep the oracle outside the common large-job path.

### 4.4 Product goals

1. Include the native companion in the official production image.
2. Enable the qualified native path explicitly in that image.
3. Report the selected backend for every operator.
4. Report all preflight rejection reasons without sensitive data.
5. Give operators one thread budget for DuckDB, Rust, and job concurrency.
6. Keep a documented portable fallback for unsupported machines.

## 5. Non-goals

This program does not include these changes:

- A complete Rust rewrite of `decoy-engine`.
- A rewrite of `decoy-platform`.
- A new cryptographic primitive.
- A silent change from the current FPE construction to NIST FF1.
- A distributed execution engine.
- A multi-node scheduler.
- Native ports for every rare strategy.
- Immediate deletion of the pandas oracle.
- Immediate removal of Polars before usage evidence and approval.
- A universal default-on change for unsupported operating systems.
- A new synchronous row API.

## 6. Binding design rules

### 6.1 One behavior contract

All backends implement the same observable strategy contract. A backend does not define new strategy behavior.

The pandas full-frame result remains the oracle until a later decision changes the compatibility contract.

### 6.2 One preflight decision

Preflight selects the physical plan before any output is staged. Execution does not select a replacement backend after work starts.

If a required native component is absent, preflight rejects or selects the approved oracle path. The job report records this decision.

### 6.3 No new cryptography

Use maintained cryptographic libraries. Custom Rust code can bind the Decoy envelope, canonicalization, batching, and format behavior.

Do not create a new cipher, KDF, MAC, random-number generator, or security protocol.

### 6.4 Explicit concurrency

One host budget controls these values:

- concurrent jobs
- DuckDB threads
- Rust kernel threads
- Python worker processes

No component can default to all host cores without a budget decision.

### 6.5 Bounded state

Each production operator declares its memory class, spill needs, input schema, output schema, and state requirements.

Unknown or arbitrary Python code cannot claim bounded execution. Large jobs with such code use the approved policy.

### 6.6 Observable routing

The job report records facts, not intent. A native route tag is insufficient unless a native-call counter proves execution.

The report must not contain raw input values, keys, derived material, or sensitive failure detail.

### 6.7 Separate optimization and refactoring

Do not combine a behavior-preserving structural change with a new algorithm in one slice.

Each slice must preserve the frozen acceptance criteria. A builder cannot weaken a gate to make a result pass.

## 7. Target architecture

```text
Public Python API and validated pipeline configuration
                         |
                         v
                 Logical Plan
                         |
                         v
              Physical Plan Compiler
                         |
        +----------------+----------------+
        |                |                |
        v                v                v
  DuckDB operator    Rust operator   Bounded Python operator
  join/sort/spill    keyed/scalar    provider/ML/hard-tail
        |                |                |
        +----------------+----------------+
                         |
                         v
               Arrow RecordBatch stream
                         |
                         v
       One diagnostic, staging, and publication coordinator
```

### 7.1 Python control layer

Python keeps these responsibilities:

- public API compatibility
- pipeline configuration validation
- logical plan compilation
- provider discovery and setup
- physical backend selection
- resource admission
- diagnostics and manifests
- staging and publication control
- bounded Python-only operators

Python does not touch individual values on a qualified common large-job path.

### 7.2 DuckDB relational layer

DuckDB keeps these responsibilities:

- file scans and projections
- foreign-key joins
- stable sort and order restoration
- global aggregation
- external-memory spill
- disk-backed state tables

Do not duplicate these operations in Rust without measured evidence and a separate decision.

### 7.3 Arrow batch boundary

Arrow provides typed input and output batches. Each operator receives a fixed schema from the physical plan.

The boundary must preserve nulls and row order. The boundary must not create Python objects for each row.

### 7.4 Rust operator layer

Rust owns measured CPU-bound deterministic loops. Initial operators include keyed derivation and deterministic pool index selection.

Each Rust operator is stateless across calls unless the physical plan declares state. Each operator returns output in input row order.

### 7.5 Bounded Python operator layer

Some operators depend on Python libraries or exact Python behavior. These operators can remain in Python when they process bounded batches.

Examples include selected Faker pool construction, ML inference, and approved hard-tail behavior. Arbitrary Python remains outside large-job admission.

### 7.6 Oracle layer

The pandas full-frame path remains available for compatibility evidence and approved small jobs.

The oracle is not the performance path. A large-job policy prevents accidental full-frame use beyond its resource limit.

## 8. Internal contracts

The exact names below are proposals. Phase 4 can change names during its reviewed design slice.

### 8.1 Physical plan

The physical plan contains these fields for each node:

- stable node identity
- selected backend
- input projection
- input and output Arrow schemas
- determinism family and version
- key source and namespace
- required prepasses
- bounded state and spill estimates
- diagnostic obligations
- fallback policy
- resource estimate

The physical plan is immutable after preflight.

### 8.2 Operator interface

Each batch operator accepts a typed batch and an execution context. It returns output arrays and structured diagnostics.

An operator does not publish files. An operator does not select another backend.

### 8.3 Coordinator interface

The coordinator owns these actions:

- open the approved source snapshot
- run required prepasses
- send batches to planned operators
- preserve row identity and order
- combine structured diagnostics
- enforce the resource budget
- stage output
- publish with the current atomicity contract
- record exact route evidence

## 9. Expected behavior

### 9.1 Backend selection

Preflight assigns one backend to each operator. The selection uses declared capabilities and resolved configuration requirements.

If one unsupported operator invalidates a whole-table invariant, preflight selects the oracle or rejects the table. The decision remains explicit.

### 9.2 Native companion states

The official production image contains the exact compatible native companion. Startup verifies its ABI before the worker accepts jobs.

The portable Python package can run without the companion. In this state, preflight records `native_unavailable` and uses the approved fallback.

The official production image treats a missing companion as an installation failure. It does not silently accept degraded throughput.

### 9.3 Runtime failures

A native panic becomes a coded engine error. The boundary does not expose a partial array.

Execution does not restart the table on the oracle after output staging starts. The current fail-before-output and publication rules remain binding.

### 9.4 Parallel output

Parallel execution produces the same ordered array as one-thread execution. Thread count does not affect any output byte.

If several rows fail, the operator reports the same first failure as the oracle. Parallel task completion order does not select the failure.

### 9.5 Diagnostics

Each job records these fields:

- physical plan version
- selected backend by node
- native ABI and package version
- thread budget and actual thread count
- batch count and row count
- native call count by operator
- preflight rejection reasons
- wall time by operator family
- peak RSS and spill bytes

Metrics contain counts, codes, durations, and stable identifiers. Metrics do not contain source values or keys.

## 10. Program sequence

Each phase has a hard exit gate. Do not start the next phase before the current gate passes.

### Phase 0: Freeze the decision and evidence

#### Task 0.1: Freeze the reference workloads

Purpose: prevent performance targets from changing after implementation.

Work:

1. Record the exact W2 configuration and data generator.
2. Record the exact deterministic Faker workload.
3. Record row tiers, batch sizes, seed, mask-key shape, and output sink.
4. Record the eight-core host CPU, memory, storage, operating system, and dependency lock.
5. Record warmup, repetition count, variance method, and tail method.

Expected behavior: another worker can reproduce the baseline from a clean checkout.

Exit gate: Cam approves the workloads, host, 600-second target, RSS limit, and evidence format.

#### Task 0.2: Capture a fresh baseline

Purpose: verify that the existing records match the current main branch and target host.

Work:

1. Run the oracle and native W2 tiers in fresh processes.
2. Run the deterministic Faker workload in fresh processes.
3. Record PIPELINE-LEVEL per-strategy timing (hash / redact / truncate /
   passthrough), wall, peak RSS, and route evidence. The finer INTRA-KERNEL
   decomposition (HKDF vs HMAC vs canonicalization vs Arrow conversion vs output
   construction) is measured at Rust level in Task 1.1, not here: Task 0.2
   establishes the pipeline starting line, Task 1.1 decomposes the kernel on the
   same reference host.
4. Record exact host identity: CPU model AND physical-core/thread topology (not
   just vCPU count), RAM, kernel, base-image family+version, and the `uv.lock`
   hash, so the baseline is reproducible.

Expected behavior: the result identifies the current dominant cost per workload
and pins the exact host.

Exit gate: the baseline includes raw result files, the host identity, and a
reviewed summary. No implementation starts from an extrapolated-only bottleneck.
DONE: see `docs/plans/native-throughput-phase0-baseline.md` (run p0base3).

#### Task 0.3: Inventory route and contract ownership

Purpose: prevent the consolidation from duplicating an existing responsibility.

Work:

1. Map planning, admission, batch execution, diagnostics, staging, and publication for each route.
2. Map every caller in `decoy-platform` and the CLI.
3. List every public API, configuration field, artifact, determinism rule, and error contract in scope.
4. Mark each current route as production, oracle, fallback, experimental, or held.

Expected behavior: the inventory gives one owner for every existing responsibility.

Exit gate: an independent architecture review finds no unknown production caller or publication path.

#### Task 0.4: Write the acceptance matrix

Purpose: define correctness before the implementation changes.

Work:

1. Define byte-parity cases for values, types, nulls, row order, warnings, and errors.
2. Define batch-size and thread-count matrices.
3. Define companion-present and companion-absent cases.
4. Define packaging and clean-install cases.
5. Define controlled failures for panic, ABI mismatch, schema drift, disk exhaustion, and cancellation.
6. Define mutation targets for changed security and routing units.

Expected behavior: each planned failure has an observable expected result.

Exit gate: the plan review returns GO. All BLOCKER and HIGH findings are closed.

### Phase 1: Optimize the existing keyed kernel

This phase changes the existing narrow Rust companion. It does not change route scope.

**Production on-ramp for the optimized hash kernel (Phase 0 gate, dennis HIGH-1).**
The optimized keyed-hash kernel lives on native route 4b (the
`run_native_or_oracle_chunked` path whose kernel set `NATIVE_KERNEL_STRATEGIES`
includes `hash`), which today has NO production caller. The default-off route 4a
(`maybe_run_native_route`) deliberately EXCLUDES hash (its `ALLOWED_STRATEGIES` is
passthrough/redact/truncate only, pending keyed-secret handling + an ABI probe on
that lane). So Phases 1-2 optimize a kernel whose production on-ramp does not yet
exist. This is resolved as follows and MUST hold for the throughput work to reach
customers: **Phase 4's unified physical-plan coordinator (Tasks 4.5-4.6) becomes
the production owner of the keyed-hash operator, promoting route 4b's kernel set
(with the keyed-secret handling and ABI probe that route 4a defers) onto the
production path.** Phase 3 enables the passthrough/redact/truncate lane (4a) as the
first controlled production step; hash reaches production at Phase 4, not Phase 3.
Until then the 100M throughput result is a benchmarked capability, and the plan
says so rather than implying hash ships fast in Phase 3.

**Phase 1 parallel-execution acceptance criteria (Phase 0 gate, Codex).** These are
binding gates on Tasks 1.4-1.6, recorded in the acceptance matrix (§5-6 of
`native-throughput-phase0-acceptance-matrix.md`):
- GIL-release PROOF: a Python sentinel thread must observably make progress during
  the native compute interval (concurrent-call tests alone do not prove `Python::detach`).
- Rayon pool ownership: Task 1.3 defines one long-lived pool with explicit lifetime
  and an aggregate thread bound across concurrent jobs; NO pool constructed per 50k batch.
- Multi-error under parallelism: an executable case placing failures in DIFFERENT
  Rayon ranges/batches, asserting the first error is selected by minimum global row
  index, never by task-completion order.
- Worker-panic: the panic case injects the panic INSIDE a Rayon worker, and it
  surfaces as a coded engine error with no partial array.
- Threaded scratch: the per-batch transient scratch bound (<= 2x input Arrow bytes,
  excluding the returned output buffer) holds at thread counts 1/2/4/8 at the frozen 50k batch.
- Precedence x threads: empty/all-null behavior and seed-length-vs-namespace error
  precedence (pinned in Task 1.2) are re-crossed with thread counts.
- Mutation bar phrased as "zero non-equivalent value/type/error/arbitration
  survivors"; equivalent/unreachable mutants documented individually.
- Non-regression: native wall <= oracle wall at every tier (Task 1.6), and a
  small-batch {1,5,11} x thread {1,2,4,8} perf non-regression check (Task 1.5) so
  thread-pool spin-up does not regress tiny batches.
- Sequencing: after Task 1.2, a 1-thread 4M W2 checkpoint before adding concurrency;
  before the costly 100M run (1.6), an observed 1/2/4/8 scaling sweep at a smaller tier.

#### Task 1.1: Kernel cost decomposition AND reference-host feasibility gate

Purpose: separate derivation from canonicalization and output allocation, AND
decide, on the reference host and BEFORE any concurrency work, whether the 600 s
target is credibly reachable. This is the go/no-go feasibility gate the baseline
alone could not settle (the 4-physical-core topology makes 600 s margin-thin).

Work:

1. Add a Rust benchmark for namespace-key derivation (HKDF).
2. Add a benchmark for one per-row HMAC with a cached key (the post-1.2 cost).
3. Add a benchmark for current `derive_array` behavior (the per-row-HKDF cost).
4. Add benchmarks for the non-derivation per-batch costs the pipeline baseline
   could not isolate: source **canonicalization**, **Arrow-array conversion** (input
   decode + output encode across the FFI boundary), and **output-array construction**.
   Together with 1-3 this completes the plan's full kernel-cost component list
   (HKDF, HMAC, canonicalization, Arrow conversion, pool selection, output
   construction) on the reference host.
5. Add per-type measurements for UTF-8, integer, Boolean, and timestamp arrays.
6. Record physical-core topology and measure a small-tier 1/2/4/8-thread scaling
   sample of the cached-HMAC path (a cheap proxy for Rayon efficiency; NOT the full
   100M run), to estimate effective scaling on this host's 4 cores + HT.

Expected behavior: the measurements quantify (a) the cache gain (per-row HKDF ->
one HKDF per batch) and (b) the effective multi-core scaling, so a conservative
model can project the 100M wall.

Exit gate (FEASIBILITY): measured cached-HMAC per-row cost, times 300M rows over
3 cols, divided by the measured effective scaling, PLUS the frozen ~292 s non-hash
serial floor, must project **<= 600 s** with margin. If the projection exceeds
600 s, STOP: do not build Tasks 1.2-1.6; bring the decision to Cam (a
higher-physical-core host, or bringing redact+truncate into scope so they
parallelize too). Record the benchmark with variance.

#### Task 1.2: Compute the namespace key once

Purpose: remove repeated HKDF work from each row.

Work:

1. Add a Rust derivation context that owns the namespace HMAC key and framing prefix.
2. Detect whether the batch has a non-null row before context construction.
3. Preserve the current empty-batch and all-null validation behavior.
4. Preserve seed-length and namespace-error precedence.
5. Keep the scalar reference function unchanged.

Expected behavior: one batch computes HKDF once, then computes one final HMAC for each non-null row.

Exit gate:

- All known-answer vectors pass.
- Python differential parity passes.
- Empty, all-null, mixed-null, wrong-key, and empty-namespace cases pass.
- The one-thread benchmark has no regression for any admitted type.
- Mutation grading leaves no unreviewed value, type, or error survivor.

#### Task 1.3: Define one native thread budget

Purpose: prevent unbounded core use and later DuckDB contention.

Work:

1. Add a caller-supplied native thread limit.
2. Define precedence between the platform worker budget and the engine argument.
3. Reject zero, negative, and excessive values with a coded error.
4. Record the resolved value in the job report.
5. Keep one thread as a supported deterministic mode.

Expected behavior: a job never creates more Rust worker threads than its approved budget.

Exit gate: unit tests cover precedence, invalid values, concurrent jobs, and report output.

#### Task 1.4: Release the GIL around pure Rust work

Purpose: let Rust use the approved cores without blocking unrelated Python threads.

Work:

1. Import and validate the Arrow array while the GIL is held.
2. Move owned Rust data into a detached closure.
3. Run only PyO3-free code while the GIL is released.
4. Reacquire the GIL before the Arrow result returns to Python.
5. Keep the panic boundary around the complete native operation.

Expected behavior: the native compute interval does not hold the GIL. Python objects do not cross the detached boundary.

Exit gate: ABI tests, panic tests, concurrent-call tests, and ThreadSanitizer pass.

#### Task 1.5: Add bounded Rayon parallelism

Purpose: use several cores for independent rows.

Work:

1. Partition an indexed Arrow array into deterministic row ranges.
2. Process ranges on the approved Rayon pool.
3. Assemble results in input range order.
4. If several ranges report errors, select the lowest failing row.
5. Preserve null behavior and output type.
6. Avoid one heap allocation per row beyond the returned Arrow buffers.

Expected behavior: values and errors match one-thread execution for every thread count.

Exit gate:

- Thread counts 1, 2, 4, and 8 produce identical output.
- Several batch sizes and null patterns produce identical output.
- AddressSanitizer and ThreadSanitizer pass.
- The bounded fuzz targets report no crash.
- Peak temporary allocation stays within the frozen limit.

#### Task 1.6: Run the 100-million-row W2 gate

Purpose: decide whether the focused hash optimization meets the product goal.

Work:

1. Run the frozen W2 workload with 1, 2, 4, and 8 native threads.
2. Record median, variance, tail, CPU use, RSS, and operator time.
3. Run the sampled oracle parity proof.
4. Record exact native-call evidence.

Expected behavior: the eight-thread run finishes in 600 seconds or less on the frozen host.

Exit gate: correctness passes and the target passes. If the target fails, stop and profile the new dominant cost.

### Phase 2: Add compiled deterministic pool selection

This phase targets deterministic Faker selection. It does not port Faker providers into Rust.

#### Task 2.1: Freeze the `derive_index` contract

Purpose: prevent a compiled implementation from changing pool selection.

Work:

1. Record the exact byte framing, integer conversion, modulus rule, and pool-size errors.
2. Generate vectors from the live Python implementation.
3. Cover every admitted source type, null pattern, seed length, namespace shape, and pool-size boundary.
4. Add partition and thread invariance cases.

Expected behavior: the vectors define one output index for each valid input row.

Exit gate: an independent review approves the vectors before Rust code exists.

#### Task 2.2: Add `derive_index_batch`

Purpose: remove the Python derivation loop from deterministic pool selection.

Work:

1. Add a Rust batch entry point that returns an Arrow unsigned-integer array.
2. Reuse the cached namespace key and bounded Rayon pool.
3. Preserve null positions.
4. Preserve validation order and coded errors.
5. Keep arbitrary pool values outside Rust.

Expected behavior: Rust returns deterministic pool indexes. Arrow `take` selects the existing pool values in their current order.

Exit gate: known-answer, Python differential, property, mutation, fuzz, sanitizer, and allocation gates pass.

#### Task 2.3: Wire the deterministic Faker route

Purpose: give the existing native Faker path the compiled crypto gain.

Work:

1. Convert the admitted C1 source array without per-row Python objects.
2. Call `derive_index_batch` once for each column batch.
3. Select values from the existing bounded pool with Arrow operations.
4. Preserve pool identity, quality gates, warnings, and route counters.
5. Keep non-deterministic and unsupported cardinality modes on their current routes.

Expected behavior: the qualified Faker route does not call Python `derive_index` for each row.

Exit gate: C1 parity passes across batch sizes, row orders, null patterns, and thread counts.

#### Task 2.4: Run the deterministic Faker performance gate

Purpose: prove that the new kernel improves the real product workload.

Work:

1. Run the frozen deterministic Faker workload at all row tiers.
2. Separate pool-build time from pool-selection time.
3. Record native-call evidence and Python scalar-call counts.
4. Record CPU use, RSS, spill, median, variance, and tail.

Expected behavior: pool selection becomes a compiled batch operation. Pool construction remains unchanged.

Exit gate: the approved throughput target passes without weaker pool-quality or parity limits.

### Phase 3: Enable native execution in the controlled product

#### Task 3.1: Define the supported production matrix

Purpose: make installation and performance claims precise.

Work:

1. Record the supported Linux distributions, architectures, Python versions, and container base.
2. Record unsupported machines and their fallback behavior.
3. Add clean-wheel install and import tests for each supported target.
4. Add an exact core-to-companion version rule.

Expected behavior: each supported production target installs and imports the correct native ABI.

Exit gate: every supported target builds, installs, imports, and runs a parity smoke test.

#### Task 3.2: Include the companion in the production image

Purpose: remove optional-performance ambiguity from the official deployment.

Work:

1. Build the companion wheel in the release pipeline.
2. Install the exact wheel in the platform production image.
3. Check the ABI and package version during worker startup.
4. If the required companion is absent or incompatible, stop worker startup.

Expected behavior: an official worker cannot accept jobs in an unintended degraded state.

Exit gate: clean-image tests cover correct, missing, stale, and corrupt companion states.

#### Task 3.3: Enable the qualified route from `decoy-platform`

Purpose: use the measured path for real jobs without changing the portable library default.

Work:

1. Pass `native_route_enabled=True` from the approved production worker.
2. Keep the engine library default unchanged during this phase.
3. Record every native admission and rejection in the job report.
4. Add a platform control that disables native routing for rollback.

Expected behavior: qualified production jobs use native execution. Unsupported jobs select their approved route during preflight.

Exit gate: platform integration tests prove admission, rejection, report output, and rollback behavior.

#### Task 3.4: Run a bounded release

Purpose: observe real product behavior before broad exposure.

Work:

1. Run approved production simulations with native execution enabled.
2. Compare output fingerprints with the oracle evidence.
3. Observe throughput, fallback rate, errors, RSS, CPU, and spill.
4. If an abort criterion occurs, disable native routing.

Expected behavior: the release improves qualified jobs without output drift or resource regression.

Exit gate: Cam approves the evidence and the final default for the official production image.

Rollback: disable native routing in the platform worker. This rollback does not require a data migration.

### Phase 4: Introduce one physical plan and operator interface

This phase starts after the focused throughput target passes. Do not block Phase 1 or Phase 2 on this work.

#### Task 4.1: Approve the physical-plan design

Purpose: define one backend-selection contract before code moves.

Work:

1. Define the immutable node schema from section 8.1.
2. Define backend identities and capability declarations.
3. Define table-level invariants that prevent unsafe mixed execution.
4. Define diagnostic, resource, and publication obligations.
5. Map the design to existing `Plan` and `NativeExecutionPlan` fields.

Expected behavior: the physical plan represents every current production route without changing execution.

Exit gate: independent architecture and compatibility reviews return GO.

#### Task 4.2: Add adapters around current executors

Purpose: create the new boundary without rewriting working code.

Work:

1. Add an internal batch-operator protocol.
2. Wrap the native scalar and keyed paths.
3. Wrap the bounded Python strategy path.
4. Wrap DuckDB relational stages.
5. Keep existing coordinators active behind characterization tests.

Expected behavior: adapters call current implementations and produce current results.

Exit gate: characterization and parity tests show no behavior change.

#### Task 4.3: Build the physical-plan compiler

Purpose: centralize backend selection and rejection reasons.

Work:

1. Compile from the existing logical `Plan` and resolved source profile.
2. Resolve operator schemas and projections.
3. Resolve determinism, key, state, diagnostic, and resource requirements.
4. Select a backend once during preflight.
5. Emit stable coded rejection reasons.

Expected behavior: one plan explains the same decisions that current route gates make.

Exit gate: shadow-mode comparisons match every current route decision in the acceptance corpus.

#### Task 4.4: Build the unified coordinator in shadow mode

Purpose: prove the coordinator contract before it publishes output.

Work:

1. Read the same source snapshots as the current routes.
2. Run the planned operators without publishing customer output.
3. Compare outputs, diagnostics, route evidence, and resource use.
4. Record all differences with stable codes.

Expected behavior: shadow execution matches the active executor for the selected vertical slice.

Exit gate: no unexplained difference remains for the selected slice.

#### Task 4.5: Activate one vertical slice

Purpose: prove production ownership with a small dependency-closed workload.

Initial slice:

- one non-FK Parquet mask table
- fixed Arrow schema
- passthrough, redact, truncate, and keyed hash
- no validators, vault, quarantine, or unsupported provider

Work:

1. Route the slice through the physical plan and unified coordinator.
2. Preserve the old route behind a rollback control.
3. Run exact parity, failure, publication, and performance gates.
4. Record route evidence from the new coordinator.

Expected behavior: the unified coordinator becomes the production owner for this one slice.

Exit gate: exact-artifact review passes. The old and new routes produce identical observable results.

#### Task 4.6: Move routes in dependency order

Purpose: remove overlapping coordinators without a big-bang cutover.

Sequence:

1. deterministic Faker pool selection
2. single-table chunked masking
3. DuckDB out-of-core foreign-key masking
4. bounded Group B and Group C operators
5. approved generation slices
6. approved global and hard-tail slices

Expected behavior: each activated slice uses the same coordinator and publication contract.

Exit gate for each slice: parity, failure, resource, performance, and independent review gates pass.

#### Task 4.7: Delete superseded routing code

Purpose: realize the simplicity benefit after the new owner proves itself.

Work:

1. Identify code with zero production ownership.
2. Prove that no caller or compatibility path uses it.
3. Remove the superseded route and its duplicate eligibility logic.
4. Keep oracle tests and historical evidence.
5. Update `CODEMAP.md`, capability docs, and the cross-repo roadmap.

Expected behavior: one production decision path remains for each migrated slice.

Exit gate: source, consumer, installed-package, and platform integration tests pass.

### Phase 5: Expand common native operators only from evidence

#### Task 5.1: Reprofile the common workload mix

Purpose: select the next operator from measured customer or product evidence.

Candidate order:

1. categorical deterministic pool selection
2. `date_shift`
3. `bucketize`
4. `code_set`
5. selected bounded text operations

Expected behavior: the next slice targets the largest measured remaining CPU cost.

Exit gate: Cam approves the workload, target, resource budget, and dependency-closed scope.

#### Task 5.2: Treat FPE as a separate security decision

Purpose: prevent a performance project from changing the cryptographic contract.

Two choices exist:

1. Port the current HMAC-SHA256 Feistel behavior byte for byte.
2. Adopt reviewed NIST FF1 with a new protocol version and migration plan.

Do not combine these choices. The first choice preserves compatibility. The second choice changes ciphertext and needs separate approval.

Expected behavior: the chosen FPE project states whether it makes a compatibility claim or a standards-conformance claim.

Exit gate: security design review, known-answer tests, differential tests, fuzzing, side-channel decision, unmask tests, and migration evidence pass.

### Phase 6: Reduce product surface and close the program

#### Task 6.1: Decide the Polars product status

Purpose: remove a duplicate substrate when it provides no measured product benefit.

Work:

1. Measure real Polars usage and unique capabilities.
2. Compare its throughput, parity cost, and maintenance cost with the unified path.
3. Keep, freeze, deprecate, or remove it through the compatibility policy.

Expected behavior: Polars has an explicit product role or leaves the active architecture.

Exit gate: Cam approves the decision and consumer migration evidence.

#### Task 6.2: Make the oracle role explicit

Purpose: prevent the test oracle from becoming an accidental large-job route.

Work:

1. Define the maximum approved oracle workload.
2. Reject larger unsupported work during claim-time admission.
3. Report the unsupported operator and available remediation.
4. Keep oracle execution available for parity and approved small jobs.

Expected behavior: unsupported large jobs do not consume unbounded memory or produce misleading performance.

Exit gate: admission, message, and platform tests pass.

#### Task 6.3: Promote durable architecture documentation

Purpose: make the shipped architecture the product source of truth.

Work:

1. Record the final architecture as a decision document.
2. Update `CODEMAP.md` and `docs/capability-matrix.md`.
3. Update the platform roadmap and deployment guides.
4. Add release notes for changed defaults or support.
5. Archive completed implementation plans after durable facts move to reference docs.

Expected behavior: a new contributor can identify the production path, oracle, fallback, and operator boundary from current docs.

Exit gate: the documentation review matches the exact shipped artifact.

## 11. Verification matrix

### 11.1 Correctness

- Python-to-Rust known-answer vectors
- full and sampled byte comparisons
- Arrow type and schema comparisons
- null-position comparisons
- row-order comparisons
- warning and row-error comparisons
- first-error comparisons
- deterministic output across processes
- deterministic output across batch sizes
- deterministic output across thread counts
- compatibility-corpus read and round-trip tests
- vault and unmask tests for affected paths

### 11.2 Native safety

- Rust unit tests
- property tests
- targeted mutation tests
- PyO3 ABI tests
- malformed Arrow FFI fuzzing
- derivation fuzzing
- AddressSanitizer
- ThreadSanitizer
- panic-to-coded-error tests
- secret-redaction tests
- bounded-allocation tests

### 11.3 Performance

- fresh-process benchmark runs
- one discarded warmup or a recorded reason for no warmup
- repeated timed runs
- median, IQR, and tail result
- CPU use and scaling efficiency
- peak RSS from an external sampler
- spill bytes and temporary-disk high-water mark
- operator-level wall time
- route and native-call proof

### 11.4 Packaging and operation

- clean source install without the companion
- clean wheel install with the companion
- exact ABI mismatch
- missing companion
- corrupt companion
- supported x86-64 image
- supported ARM64 image
- worker startup verification
- platform enable and disable controls
- job-report route evidence
- rollback exercise

## 12. Release and rollback policy

Each production activation uses a bounded release. The release record identifies the exact engine, companion, platform, image, and plan versions.

Abort the release for any of these results:

- output fingerprint drift
- warning or row-error drift
- unexpected oracle fallback
- native panic or process crash
- peak RSS beyond the frozen limit
- throughput below the approved non-regression limit
- worker oversubscription
- missing route evidence
- sensitive data in diagnostics

The initial rollback disables native routing in the platform worker. Structural route retirement needs a separate recovery plan before deletion.

## 13. Agent execution protocol

This program is R2 work. No author can approve the author's own implementation.

The main Claude session is the builder. The `dennis` agent provides the independent adversarial review.

The `barry` agent updates durable documentation after behavior exists. Cam approves phase activation, route deletion, and production exposure.

For each task:

1. Read this plan and the named source documents.
2. Record the exact task scope and risk.
3. If the failure can be reproduced safely, add fail-before evidence.
4. Implement one recoverable slice.
5. Inspect the complete diff and relevant surrounding source.
6. Run targeted and required project tests.
7. Measure line coverage, branch coverage, and mutation strength on changed critical units.
8. If the task affects performance, run the frozen performance gate.
9. Send the exact artifact to an independent adversarial review.
10. Correct all BLOCKER and HIGH findings.
11. Repeat tests after each correction.
12. Run the repository gate for the exact final commit.
13. Update durable docs only after behavior exists.

Do not combine several phase gates in one unreviewed change. Do not update a golden file only because a test failed.

## 14. Estimated sequence

The estimates describe engineering-equivalent effort. Evidence gates and long benchmarks limit calendar compression.

| Sequence | Scope | Estimate |
|---|---|---:|
| 1 | Phase 0 baseline, inventory, and acceptance matrix | 1 to 2 weeks |
| 2 | Phase 1 cached key, GIL release, and Rayon | 2 to 4 weeks |
| 3 | Phase 2 compiled pool selection | 1 to 3 weeks |
| 4 | Phase 3 controlled production enablement | 1 to 3 weeks |
| 5 | Phase 4 physical plan and first unified slice | 4 to 8 weeks |
| 6 | Phase 4 route migration and deletion | 6 to 12 weeks, incremental |
| 7 | Phase 5 operator expansion | Evidence-driven slices |
| 8 | Phase 6 surface reduction and closure | 2 to 4 weeks |

The first product throughput result is expected after Phases 1 through 3. The architecture consolidation continues after that result.

## 15. Initial approval decisions

Cam must approve these values before Phase 0 exits:

1. The exact eight-core reference host.
2. The 600-second W2 target.
3. The peak RSS and spill limits.
4. The deterministic Faker workload and target.
5. The official production operating-system and architecture matrix.
6. The large-job policy for arbitrary Python providers.
7. The initial decision to freeze Polars expansion.
8. The decision to defer FPE until after the focused throughput result.

Default recommendation:

- Approve the 600-second W2 target on the current eight-core production-class host.
- Support the official Linux x86-64 and ARM64 containers first.
- Keep the portable Python fallback for other machines.
- Freeze new Polars work during consolidation.
- Defer FPE until a fresh profile identifies it as the next dominant cost.
- Preserve the current FPE construction until a separate migration decision approves FF1.

## 16. Source documents

- `docs/plans/2026-08-26-engine-efficiency-plan.md`
- `docs/plans/PHASE2-BASELINE.md`
- `docs/plans/native-testing-T1-rust-core.md`
- `docs/plans/native-testing-T6-e2e.md`
- `docs/plans/2026-08-30-part1-phase3-c1-slice.md`
- `docs/plans/2026-08-31-part2-phase4-plan.md`
- `docs/plans/2026-09-01-phase5-hard-tail-research.md`
- `docs/native/crypto-testing-reference.md`
- `docs/compatibility-contract.md`
- `CODEMAP.md`
- `decoy-engine-native/Cargo.toml`
- `.github/workflows/native-companion.yml`
- `../decoy-platform/docs/product/benchmarks/scaling-and-capacity.md`
