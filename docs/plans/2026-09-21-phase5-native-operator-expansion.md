# PLAN — Phase 5: native operator expansion (parallelize redact/truncate + native categorical)

Status: plan
Author: Opus (top-tier, per the guides/plans standing rule). Codex cross-reviews as plan-gate.
Roadmap: Stage C item 11 (Phase 5). Scope approved by Cam 2026-09-21 (Track A + categorical, engine-side
knee clamp, up to 8 GCP runs reserving prodsim headroom). FRAME survey: the native-path seam map
(2026-09-21). Base: engine main `831a9f0f` (post task #11).

## FRAME — the goal

After the keyed-hash kernel fell ~13.5x (1280s -> 95s at 8 threads) and the pool/faker selector went
native, the remaining single-threaded costs on the native masking lane are the pure-pyarrow scalar
operators. The 2026-09-21 thread sweep measured, at 100M rows on the 8-core n2-standard-8:
`redact ~142s` and `truncate ~173s` single-threaded, both running as ONE pyarrow-compute call with no
fan-out. This plan does two independent things, both gated on **byte-identical output** (the hard gate
this whole program has held):

- **Track A** — give `redact` and `truncate` a bounded thread fan-out, and move the thread-knee clamp
  from the caller into the engine so the knee holds regardless of who calls.
- **Track B** — add `categorical` (deterministic) as the first genuinely new native operator, reusing
  the existing `derive_index_batch` kernel (no new Rust, parity inherited from an existing KAT family).

### Non-goals (explicit)
- No FPE work (program plan Task 5.2: it is a crypto-contract change, separate security gate).
- No `bucketize` / `date_shift` / `code_set` — they carry row-error diagnostics and hit the full-frame
  quarantine wall, which the streaming lane does not yet handle. The *next* operator is chosen by a
  re-profile after this lands, not by assumption.
- No dynamic thread malleability / live-knee optimization — the clamp is a static conservative cap.
- No change to the non-deterministic (unseeded) categorical path; it stays on the pandas oracle.
- No NON-STRING categorical categories in v1 (they decline to the oracle) — deferred to a later slice
  gated on an exact pandas-oracle dtype-reconciliation algorithm (Codex P0-2).
- No native categorical on the CHUNKED/OOC route in v1 (it declines to the oracle) — the eager
  per-chunk emit has no whole-column assembly point to resolve the oracle's data-dependent output type;
  deferred to a later slice gated on a nullness-prepass/buffering policy (Codex round-4 P0-2).

## Established methodology (survey-first)
- Thread fan-out mirrors the engine's own proven pattern: the keyed-hash `derive_array` slices the
  Arrow array into ranges and runs them on the process-wide `shared_native_pool()` under `py.detach`
  (GIL released), serial when `threads <= 1` (`decoy-engine-native/src/batch.rs:149-160`,
  `arrow_ffi.rs:227-248`).
- Deterministic categorical is the SDV-style keyed selection already used for pooled Faker: a
  weighted-CDF (or uniform) gather over a keyed index draw (`derive_index`), which the native
  `derive_index_batch` kernel already implements byte-identically (KAT-pinned).
- pyarrow-compute kernels release the GIL, so a Python thread-pool slice-and-concat is a legitimate
  first cut for the string fast path (measure before committing to Rust).

## The exact seams (from the survey; file:line on engine main `831a9f0f`)

Track A kernels:
- `execution/native/_kernels_scalar.py` — `native_redact` (L54-73), `native_truncate` (L76-113): thin
  wrappers, no threads today.
- `kernel/_scalar.py` — `redact_array` (L98-134, pyarrow fast path + `_redact_array_reference` L94
  Python fallback); `truncate_array` (L171-247, pyarrow fast path + `_truncate_array_reference` L137-168
  Python fallback).
- Invocation seams: chunked `execution/native/_chunk_masking.py` dispatch L178-192 (`redact` L180-181,
  `truncate` L182-192); full-frame `execution/physical/_shadow_operators.py` `run_operator` L89-98.
- Thread budget: `decoy-engine-native/src/threads.rs` — `NativeThreadBudget::resolve(requested,
  host_available)` L67-101 defaults `None->1` (L77) and currently **ignores `host_available`** (`let _
  = host_available;` L75). `shared_native_pool()` L189-199. No engine-side clamp exists today (knee is
  caller-side).

Track B seams:
- Oracle: `execution/_strategies/_categorical.py` `run` L118-234 (deterministic L173-213: uniform via
  `derive_index(... pool_size=len(categories))`, weighted via `_build_cdf` L60-110 + `derive_index(...
  pool_size=_WEIGHTED_CDF_RES=1_000_000)` + `bisect_right`; non-deterministic L214-231 unseeded rng).
- Kernel: `execution/native/_index_ext.py` `derive_index_batch(values, *, mask_key, namespace,
  pool_size, native_threads=None) -> pa.Array` (uint64) L79-90; faker's null-safe gather exemplar in
  `_chunk_masking.py:sample_faker_array` L54-146.
- Registration seams (survey reference — the AUTHORITATIVE build list is the 9-item executable checklist
  in Design > "B registration", which Codex P0-1 expanded; this table is the seam inventory it draws on.
  categorical currently ABSENT unless noted):
  1. `native/_requirements.py` `NATIVE_KERNEL_STRATEGIES` L124 + a new `categorical_config_rejection`
     (cf. `redact_config_rejection` L403-416).
  2. `native/_chunk_masking.py` dispatch if/elif L178-226.
  3. `physical/_shadow_bindings.py` `SLICE_STRATEGIES` L40-42 + `OPERATOR_ID_BY_STRATEGY` L44-50 + a
     categorical binding branch in `execution_binding_for_slice_node` L144-232.
  4. `physical/_shadow_operators.py` `run_operator` dispatch L86-136 + a `_CATEGORICAL` const.
  5. `execution/_unified_slice_admission.py` `ALLOWED_OPERATOR_IDS` L78-80 + `_ADMITTED_RESIDENT_TYPES`
     L90-97.
  6. `native/_capabilities.py` `_MASK` L214-226 — **ALREADY PRESENT** (survey correction); VERIFY only,
     no edit.

## Design

### Track A — bounded thread fan-out for redact/truncate + engine-side knee clamp

**A design decision — clamp location + scope (Cam: engine-side).** Stop discarding `host_available` in
`NativeThreadBudget::resolve`. New effective count = `min(requested_or_default, host_available,
NATIVE_MASK_THREAD_KNEE)`. `NATIVE_MASK_THREAD_KNEE` is a NAMED constant (default 4, sourced from the
2026-09-21 8-core sweep where t4=2.7x and t8 regresses), overridable by env for other host classes so
the "4" is not magic. Because thread count never affects output bytes, the clamp is parity-neutral by
construction (Codex confirmed: rows are independently derived, errors arbitrated by lowest row index —
`batch.rs:162,268`).

**INTENTIONAL SHARED SCOPE (Codex P1-4).** `resolve` is the shared resolver for `derive_batch` (hash)
AND `derive_index_batch` (faker/index) as well as the new scalar fan-out (`arrow_ffi.rs:213,335`), so
this clamp deliberately applies to ALL native masking paths. This is CORRECT and beneficial, not
collateral: the knee was measured on the hash path itself (t4=116.9s beats t8=126s), so clamping
hash/faker to the knee IMPROVES them too. It IS a behavior change to existing hash/faker thread grants
(a caller requesting 8 now gets the knee) — a beneficial one, made explicit here. Requirements:
- `NATIVE_MASK_THREAD_KNEE` env override has DEFINED semantics: parsed once per process at first
  `resolve`, invalid/`<1`/`>1024` -> coded error (not silent default), absent -> default 4; document it
  is calibrated to the 8-core class and should be raised on larger hosts.
- Tests assert requested-vs-effective grants for hash, faker/index, AND scalar: `requested 8 ->
  effective 4` on an 8-core; `requested 2 -> 2`; `host_available 3 -> 3`; env override moves the cap;
  and a parity regression that hash + faker + categorical outputs are byte-identical across requested
  1/4/8 (grant changes speed, never bytes).
- Rejected alternative: scoping the clamp to scalar-only (leaving hash/faker on the shared resolver
  unclamped) is LESS coherent — the regression is on the hash path — so we clamp the shared resolver and
  own the hash/faker behavior change explicitly. Flagged to Cam as a (beneficial) change to hash/faker
  effective threads.

**A design decision — parallelization approach (measure-first, per the scoping doc).** Primary: a
Python thread-pool slice-and-concat, threaded through both seams as a `native_threads` arg
(redact/truncate take none today). Shard-safety rules (Codex P1-5 — `redact_array` returns NULL-typed
output for empty/all-null fast-path slices; only the `native_redact` wrapper repins to `pa.string()`
at `_kernels_scalar.py:70`, so a raw `redact_array`-per-shard + concat can type-drift or fail on an
all-null shard):
- Each shard invokes the NATIVE WRAPPER (`native_redact`/`native_truncate`), not the raw
  `redact_array`/`truncate_array` — so every shard is already repinned to `pa.string()` before concat
  (alternative: explicitly cast each shard; the wrapper is cleaner). One post-concat `pa.Array`, pinned
  `pa.string()`.
- `N = min(clamped_thread_count, count_of_nonempty_row_ranges)` — never more shards than nonempty
  ranges; N<=1 runs the existing serial path unchanged.
- Contiguous ranges preserve global row order; null positions preserved within each shard.
- The pure-Python reference fallback (`_redact_array_reference` / `_truncate_array_reference`) runs
  SERIALLY, exactly as today (GIL-held; documented; it is the non-fast-path minority).
- Parity tests MUST include MIXED null/non-null shard-boundary cases (an all-null shard adjacent to a
  populated shard; a shard boundary mid-run), not merely all-null whole arrays.

**A0 (GATES Track A fan-out) — attribution spike, benchmarking the FULL wrapper cost (Codex P1-5).**
Before building the fan-out, confirm the ~142s/~173s is actually spent where threading can help.
Micro-bench in-process on a 100M `pa.string()` array, measuring the COMPLETE proposed wrapper cost, not
just Arrow compute: slicing, the full UTF-8 validation the fast path already does
(`_scalar.py:105,180`), the compute, executor scheduling, per-shard casts/repin, and concat. Isolate
that against pure Array materialization/copy/allocation. If the operation is materialization-bound (or
the wrapper overhead swamps the compute win), threading cannot help -> Track A fan-out re-scopes and is
surfaced to Cam (the clamp PR-1a still ships regardless). Try a 2/4-way manual slice-and-concat locally
to see the achievable speedup shape before wiring seams. Mirrors task #11's D0 spike discipline.

**A-fork (measurement-decided, NOT pre-built).** If the Python concat overhead eats the win (or the
fallback path dominates real workloads), implement Rust `redact`/`truncate` kernels mirroring
`derive_array` (range tasks + `shared_native_pool` + `py.detach`), with KAT vectors and a Rust parity
test. This fork is chosen by A0/VERIFY data, not preemptively (avoids new ABI surface unless earned).

**Track A acceptance tests (defined now, before impl):**
1. **Byte-identity (HARD):** for redact and truncate, parallel output (`native_threads` = 2/4/8) is
   bit-identical to serial (`native_threads` = 1/None) AND to the pandas oracle, over: all-null, empty,
   single-row, sub-slice-boundary lengths, unicode/multibyte truncate, non-string dtypes (fallback
   path), `redact_with` non-default, truncate keep=head/tail + mask_char. Same at both seams
   (chunked + full-frame).
2. **Clamp unit tests (Rust + Python):** `resolve` clamps `requested > knee` down to the knee; clamps
   to `host_available` when lower; `None -> 1`; negative/`0`/`>1024` still coded-error; env override
   moves the knee. A parity regression asserts output identical across requested 1/4/8 (clamp changes
   speed, never bytes).
3. **Speedup (VERIFY, GCP):** at 100M, redact+truncate combined wall at 4 threads is materially below
   the ~142+173s serial baseline. Target >=2x at 4 threads (the knee says do not expect the hash
   kernel's 2.7x for a cheaper per-value op; >=2x is the go/no-go, less than that triggers the A-fork
   decision). Re-confirm the knee: t1/t2/t4/t8 medians, t8 not faster than t4.

### Track B — native categorical (deterministic)

**B kernel** — `native_categorical(array, *, categories, weights=None, mask_key, namespace,
native_threads=None) -> pa.Array` in a new `native/_categorical_ext.py` (or `_kernels_scalar.py` if it
stays small), reusing `derive_index_batch`:
- **Uniform** (`weights is None`): `idx = derive_index_batch(col, mask_key=mask_key, namespace=namespace,
  pool_size=len(categories))`; null-safe NumPy gather `out[valid] = categories_arr[idx[valid]]`,
  mirroring `sample_faker_array` L112-146 (validate uint64 / length / null-mask / bounds first).
- **Weighted**: `cdf = _build_cdf(weights)` (reuse the oracle's builder, `_WEIGHTED_CDF_RES`);
  `bucket = derive_index_batch(col, ..., pool_size=_WEIGHTED_CDF_RES)`; vectorized
  `cat_idx = np.searchsorted(cdf, bucket, side="right")` (NOT a Python `bisect` loop — must stay
  vectorized for throughput); gather `categories_arr[cat_idx]`. `searchsorted(side="right")` must be
  proven equivalent to the oracle's `bisect_right` on the integer CDF (it is, for a sorted CDF; a
  differential test pins it).
- **Output typing — STRING categories + an oracle-typing spike (Codex P0-2, refined round 2).** v1
  admits ONLY string categories; non-string categories decline to the oracle. But "emit `pa.string()`
  always" is STILL wrong: for all-null (and empty) output the oracle assigns an all-`None` object list
  into pandas, and `Table.from_pandas` infers Arrow `null`, not `string` — so the plan's own all-null/
  empty byte-identity tests (type is part of parity, `tests/native/test_kernels_scalar.py:51`) would
  fail against a forced `pa.string()`. Root-cause remediation:
  - **Root cause (Codex round-4 P0-2): the oracle's output TYPE is data-dependent** and CANNOT be known
    at bind/preflight time. The chunked/OOC lane emits each masked chunk EAGERLY
    (`_chunk_masking.py:231`) with no whole-column assembly point, so an all-null chunk is
    schema-indistinguishable from a populated one and an after-assembly assertion is too late to "decline
    before output." This is architectural, not a wording gap — so v1 changes SCOPE rather than patching.
  - **v1 SCOPE CUT — native categorical runs on the FULL-FRAME (unified-slice) route ONLY; the chunked/
    OOC route DECLINES categorical to the oracle.** The full-frame coordinator assembles the WHOLE
    column (loops `_batches`, concatenates parts, `_shadow_coordinator.py:373-389`) BEFORE emitting, so
    it is the one route with a point where whole-column null-ness is known and the oracle-matching type
    can be resolved (Codex round 5 confirmed this is the right resolution point). The eager chunked route
    has no such point in v1, so categorical stays on the oracle there — no throughput regression, just no
    native speedup for out-of-core categorical yet.
  - **B0 is the AUTHORITY for the exact types — do NOT assume a mapping (Codex round-5 P0-2).** B0
    measures the ORACLE's real END-TO-END final field type — through the actual unified-slice oracle
    output path, which reconstructs via pandas (`_unified_slice.py:310`), NOT raw `Table.from_pandas` in
    isolation — for {empty, all-null, one-null-mixed, populated} x {uniform, weighted} string categories.
    Native then implements EXACTLY B0's measured mapping. The likely shape (to CONFIRM, not assume) is
    empty -> `pa.float64()` (the coordinator already models empty tokenizing output that way,
    `_shadow_coordinator.py:190`), all-null -> `pa.null()`, otherwise `pa.string()` — but the plan
    asserts only "match B0 exactly," never a specific type.
  - **Full-frame type mechanism:** native categorical emits `pa.string()` PER BATCH (stable across
    batches, so `run_operator` part-concat never type-drifts, `pa.concat_arrays` safe — Codex round 5
    confirmed); THEN at final assembly the coordinator normalizes the assembled WHOLE column to B0's
    measured type for its null-shape, overriding the generic tokenizing default
    (`_shadow_coordinator.py:172,190`) for categorical (resolves the item-7 collision). Native and oracle
    do NOT literally share coordinator assembly (the oracle reconstructs via pandas at
    `_unified_slice.py:310`), so byte-identity (value AND field type) is asserted at the FINAL
    `ExecutionResult` boundary, after both sides' final conversions — not mid-coordinator.
  - **Chunked decline needs an EXPLICIT route-specific veto (Codex round-5):** adding `categorical` to
    `NATIVE_KERNEL_STRATEGIES` alone would admit it in `_static_route_decision` (`_dispatch.py:221`) and
    then hit the missing chunk handler. So the preflight explicitly VETOES categorical on the chunked
    route (routes to oracle) + a positive oracle-route test proves it declined cleanly.
  - Tests (field TYPE, not just value): full-frame byte-identity at the `ExecutionResult` boundary incl.
    empty + all-null + mixed columns (assert final field type == the oracle's, per B0); a test proving
    categorical DECLINES to the oracle on the chunked route. Do not rely on `_requirements.py:274`'s
    default-to-`pa.string()`.
  - Chunked native categorical is a LATER slice, gated on a bounded whole-input/nullness prepass or a
    buffering policy that can resolve the type before eager output.
  - Non-string category support is a later slice, gated on an exact pandas-oracle dtype-reconciliation
    algorithm + per-domain parity tests.

**B config/determinism gate — enforced at admission AND runtime (Codex P0-3, refined round 2).**
Admission-only rejection is insufficient: the native function is always-deterministic and a wiring
regression could route an UNSEEDED plan into it, silently changing its contract instead of declining.
The gate must be ONE rule usable at both boundaries, which see DIFFERENT inputs: `_plan.
native_route_eligibility()` is CONFIG-only (it cannot read a compiled `ColumnSeed`), while the
full-frame binding sees the `ColumnSeed`. Root-cause remediation — a predicate that is a PURE FUNCTION
OF THE RESOLVED CONFIG:
- Define `is_deterministic_categorical(resolved_config) -> bool` = EXACTLY the function whose result
  sets `ColumnSeed.deterministic` (`_categorical.py:173` derives determinism from config today; extract
  that decision into the shared function so `ColumnSeed.deterministic == is_deterministic_categorical(
  config)` by construction — one source of truth, not a duplicate gate).
- Native-route boundary (`_plan.native_route_eligibility` / `_requirements.categorical_config_
  rejection`): calls `is_deterministic_categorical(config)` on the config it already has — no
  `ColumnSeed` needed. Full-frame binding (`_shadow_bindings`): reads `ColumnSeed.deterministic` (equal
  by construction) and writes `ExecutionBinding.categorical_deterministic`.
- Runtime: `run_operator` / dispatch ASSERTS `ExecutionBinding.categorical_deterministic is True`
  before invoking the native kernel (defensive: the unseeded branch can never reach native even under a
  wiring bug).
- `categorical_config_rejection` also validates: `namespace` present, `categories` a non-empty STRING
  list, `weights` (if present) shape-matched + non-negative.
- Negative tests at BOTH admission boundaries (unseeded categorical declines on the native route AND on
  the full-frame binding) + the runtime-assertion test + a test that `is_deterministic_categorical`
  agrees with `ColumnSeed.deterministic` across the config space. This keeps categorical clear of the
  full-frame diagnostics/quarantine wall — the deterministic path emits NO per-row error diagnostics,
  but only if the unseeded path provably cannot reach it.

**B registration — the EXECUTABLE seam checklist (Codex P0-1: the survey's 5-seam list was incomplete;
missing seams would let binding succeed while admission/runtime silently declines or receives no
kernel).** Every item below is a build step with a proving test; the closing seam test asserts BOTH
routes actually EXECUTED categorical (not declined to the oracle):
1. `native/_requirements.py`: add to the native strategy set + new `categorical_config_rejection`.
2. `native/_plan.py` eligibility: `native_route_eligibility()` calls `native_kernel_rejection()`
   directly (`_plan.py:283`) and will reject categorical unless updated — update it. `_phase3_
   eligibility` STAYS faker-only (it is deliberately the faker provider allowlist); add a regression
   proving categorical does NOT ride the faker exception.
3. `native/_dispatch.py` preflight (v1 = EXPLICIT ROUTE VETO): categorical is NOT admitted to the
   native CHUNKED route in v1 (the eager lane can't resolve the oracle's data-dependent output type).
   A route-specific VETO is required — mere absence from `NATIVE_KERNEL_STRATEGIES` is not enough
   (Codex round 5: `_static_route_decision` at `_dispatch.py:221` would otherwise admit it and hit the
   missing chunk handler). Veto categorical on the chunked route so it routes to the oracle; a positive
   oracle-route test proves the clean decline.
4. `native/_chunk_masking.py`: NO chunked native categorical branch in v1 (declined at item 3). This is
   the DEFERRED chunked slice; do not add a gather branch here until a nullness-prepass/buffering policy
   resolves the type pre-output.
5. `physical/_plan.py` `ExecutionBinding` contract (Codex round-2 P0-1): today it has NO
   deterministic-categorical field, so `_shadow_bindings.py` has nowhere to "carry" the flag. Add the
   explicit `categorical_deterministic: bool` (+ the resolved categories/weights/CDF + resolved output
   schema) to the `ExecutionBinding` dataclass FIRST; item 5 populates it, item 6 asserts it.
5b. `physical/_shadow_bindings.py`: `SLICE_STRATEGIES` + `OPERATOR_ID_BY_STRATEGY` (`categorical` ->
   `native_categorical`) + a categorical binding branch that POPULATES the new `ExecutionBinding`
   fields (key + categories/weights/CDF + resolved output schema + `categorical_deterministic`).
6. `physical/_shadow_operators.py`: `run_operator` branch + `_CATEGORICAL` const.
7. `physical/_shadow_coordinator.py`: (a) arrange compiled index-kernel LOADING for categorical —
   today it is keyed only to `pool_binding`/faker (`_shadow_coordinator.py:105`), so categorical would
   otherwise get no kernel; (b) classify categorical in `_TOKENIZING_STRATEGIES` (`:354`) so batch
   assembly treats it as tokenizing; (c) at final assembly (`:172,190`) resolve categorical's output
   type from the ASSEMBLED WHOLE COLUMN to B0's MEASURED oracle mapping (not an assumed rule),
   overriding the generic tokenizing default (the item-7 collision — see "Output typing" for the
   per-batch-string-then-final-normalize mechanism + the `ExecutionResult`-boundary parity assertion).
8. `execution/_unified_slice_admission.py`: `ALLOWED_OPERATOR_IDS` (L78-80) + `_ADMITTED_RESIDENT_
   TYPES` (L90-97) — WITHOUT this, full-frame binding succeeds but unified-slice admission declines.
9. `native/_capabilities.py`: **ALREADY correct** (row-local/static/zero-diagnostic, L214-226) —
   VERIFY only, no edit (Codex confirmed).

**Resident types:** start STRING-only (see output-schema decision below) — `_ADMITTED_RESIDENT_TYPES`
gets categorical -> `frozenset({pa.string()})`; non-string sources/categories decline to the oracle.
**Seam proof test:** run a categorical column through the FULL-FRAME route and assert the native
operator EXECUTED (via `execution_route` evidence / a spy, not a silent decline); run it through the
CHUNKED route and assert it DECLINED to the oracle (v1 scope). Both are positive assertions, not
absence-of-error.

**Track B acceptance tests (defined now, before impl):**
1. **Byte-identity (HARD), STRING categories:** native categorical == pandas oracle, bit-for-bit
   (VALUE and Arrow output type), for uniform AND weighted, across: null/empty/duplicate/unicode source
   values, single category, equal weights, zero-weight category (duplicate CDF thresholds), highly-
   skewed weights, CDF bucket-edge/exact-boundary values, source values that canonicalize identically,
   all-null and empty output (assert the final Arrow FIELD TYPE equals the oracle's, not just values).
   On the FULL-FRAME route (v1 scope); plus a test that categorical DECLINES on the chunked route.
   Extend `tests/unit/execution/test_categorical_weighted.py` with a native-vs-oracle differential.
2. **KAT:** add a categorical KAT vector mirroring `decoy-engine-native/vectors/derive_index_kat.json`
   (pin native categorical output for a fixed config+seed corpus), guarding against silent drift.
3. **Decline:** non-deterministic categorical (no seed / unseeded branch) declines to the oracle
   (admission/config-gate test); the deterministic path with a missing namespace is rejected.
4. **searchsorted==bisect_right** differential over random integer CDFs + bucket arrays incl. exact
   boundary hits.

## Build order (three sequential PRs under this plan; performance gate precedes the fan-out merge —
Codex P1-6)

- **PR-1a = engine-side knee clamp (correctness-only, mergeable on its own).** Rust `resolve` clamp +
  env semantics + Python boundary + the requested-vs-effective grant tests for hash/faker/scalar + the
  cross-thread-count byte-parity regression. Merges on correctness (bounds runaway thread requests, a
  standalone win even if fan-out never ships). -> dennis -> Codex FINAL -> CI -> merge.
- **PR-1b = redact/truncate fan-out (measure-first; the perf go/no-go is a PRE-MERGE gate).** A0
  attribution spike THEN a representative measurement (local shard shape + one GCP run: speedup + knee
  re-confirm) BEFORE merge, so the `>=2x @ 4 threads` decision precedes the merge, not follows it. On
  meeting the target: shard-wrapper fan-out across both seams + byte-parity incl. mixed null/non-null
  shard boundaries -> dennis -> Codex FINAL -> CI -> merge. On MISSING the target: do NOT merge fan-out;
  take the A-fork or defer (PR-1a already banked the clamp).
- **PR-2 = Track B native categorical (FULL-FRAME route only in v1).** B0 oracle-typing spike (gates
  the kernel) -> `ExecutionBinding` contract field + shared `is_deterministic_categorical` predicate +
  the seam checklist (full-frame native + chunked-decline enforcement) + kernel + determinism gate ->
  byte-parity (uniform + weighted, string-only, incl. all-null/empty final field TYPE) + KAT +
  full-frame-executes + chunked-declines + both-boundary determinism-decline + runtime-assertion tests
  -> dennis -> Codex FINAL -> CI -> merge. No GCP needed.

**A-fork spec (if PR-1b misses the target).** Owner: same build lane (Opus-planned, subagent-built).
Approach: Rust `redact`/`truncate` kernels mirroring `derive_array` (range tasks + `shared_native_pool`
+ `py.detach`). ABI impact: adds pyfunctions to the native crate -> an ABI bump + the compiled-kernel
loader/KAT-drift guard must cover them (like `derive_index_batch`). Acceptance: Rust parity test + KAT
vectors + the same byte-identity suite as PR-1b. Rollback: if the Rust fork also misses, ship the clamp
(PR-1a) alone and defer scalar parallelism; decline is to the existing serial pyarrow path (no
regression). Decision to take the fork is Cam-surfaced with the A0/GCP numbers, not automatic.

(Each PR is independently byte-parity-gated and small enough to review cleanly.)

## VERIFY (acceptance summary)
Both tracks: byte-identity is the merge gate (parallel==serial==oracle for A; native==oracle for B),
proven at both native seams, on the pinned CI env (Codex sandbox pyarrow differs — authoritative =
pinned CI, per the standing note). Track A additionally: the clamp unit tests + one GCP speedup/knee
run. Full regression-gate CI (both was-substrate legs now pandas-only after Phase 6) + mypy + the
seam sentries must stay green.

## Risks + rollback
- **A0 says materialization-bound:** threading cannot help -> re-scope Track A (Rust kernel may still
  help by fusing; else defer). Surfaced to Cam, not silently dropped.
- **Concat/thread overhead > win at 4 threads:** take the A-fork (Rust kernels) OR ship the clamp alone
  (still a correctness win: bounds runaway thread requests) and defer redact/truncate parallelism.
- **Categorical resident-type over-admission:** start narrow, let unfitting types decline to the
  oracle; widen only with a parity test per added type.
- **Rollback:** both tracks are additive behind the existing route selection; a native operator that
  fails admission/config-gate declines to the pandas oracle (fail-closed to the proven path). No
  frozen-surface (compat-contract §9) paths are touched. Pre-GA hard-delete rules apply if a seam
  entry must be removed.

## Gates
FRAME (survey done) -> PLAN (this) -> Codex plan-gate -> [PR-1 Track A: build -> dennis -> Codex FINAL
-> CI -> merge -> GCP VERIFY] -> [PR-2 Track B: build -> dennis -> Codex FINAL -> CI -> merge] ->
DOCUMENT (roadmap Stage C item 11 + shipped-log) -> re-profile picks the next operator.
