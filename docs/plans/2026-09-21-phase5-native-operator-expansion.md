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
- Registration seams (categorical currently ABSENT unless noted):
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

**A design decision — clamp location (Cam: engine-side).** Stop discarding `host_available` in
`NativeThreadBudget::resolve`. New effective count = `min(requested_or_default, host_available,
NATIVE_MASK_THREAD_KNEE)`. `NATIVE_MASK_THREAD_KNEE` is a NAMED constant (default 4, sourced from the
2026-09-21 8-core sweep where t4=2.7x and t8 regresses), overridable by env for other host classes so
the "4" is not magic. The clamp is the single enforcement point: no caller can exceed the knee. Because
thread count never affects output bytes, the clamp is parity-neutral by construction.

**A design decision — parallelization approach (measure-first, per the scoping doc).** Primary: a
Python thread-pool slice-and-concat around the pyarrow-compute fast path, threaded through both seams
as a `native_threads` arg (redact/truncate take none today). Slice the combined single-chunk Array into
N contiguous ranges (N = clamped thread count), run the existing `redact_array`/`truncate_array` per
range on a bounded pool, concat preserving order + null positions + the `pa.string()` output pinning.
The pure-Python reference fallback path runs serially (documented; it is the non-fast-path minority).

**A0 (GATES Track A) — attribution spike.** Before building the fan-out, confirm the ~142s/~173s is
actually spent in the pyarrow COMPUTE kernel and not in Array materialization/copy/allocation (if it is
materialization-bound, threading the compute cannot help and Track A re-scopes — surface to Cam). Micro
-bench `redact_array`/`truncate_array` on a 100M `pa.string()` array in-process: isolate pure kernel
wall vs materialization; try a 2/4-way manual slice-and-concat locally to see the achievable speedup
shape before wiring seams. This mirrors task #11's D0 parity-spike discipline.

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
- Output type pinned to the oracle's output dtype for the given `categories` (string in the common
  case; the plan validates categories element type and pins accordingly).

**B config gate** — new `categorical_config_rejection` in `_requirements.py`: native accepts ONLY the
deterministic path — reject (decline to oracle) when the strategy would take the unseeded
`np.random.default_rng()` branch; require `namespace`; validate `categories` is a non-empty list and
`weights` (if present) matches shape and is non-negative. This is what keeps categorical clear of the
full-frame diagnostics/quarantine wall: the deterministic path emits NO per-row error diagnostics, so
admission stays clean.

**B registration** — add `categorical` across seams 1-5 above; VERIFY seam 6 unchanged. For
`_ADMITTED_RESIDENT_TYPES`, start with the source types `derive_index_batch` + `_canonicalize_source`
accept for a keyed draw (begin conservative — the types faker admits — and let others decline to the
oracle rather than guess wide).

**Track B acceptance tests (defined now, before impl):**
1. **Byte-identity (HARD):** native categorical == pandas oracle, bit-for-bit, for uniform AND
   weighted, across: null/empty/duplicate/unicode source values, single category, equal weights,
   zero-weight category, highly-skewed weights, CDF bucket-edge values, source values that canonicalize
   identically. At both seams (chunked + full-frame). Extend `tests/unit/execution/
   test_categorical_weighted.py` with a native-vs-oracle differential.
2. **KAT:** add a categorical KAT vector mirroring `decoy-engine-native/vectors/derive_index_kat.json`
   (pin native categorical output for a fixed config+seed corpus), guarding against silent drift.
3. **Decline:** non-deterministic categorical (no seed / unseeded branch) declines to the oracle
   (admission/config-gate test); the deterministic path with a missing namespace is rejected.
4. **searchsorted==bisect_right** differential over random integer CDFs + bucket arrays incl. exact
   boundary hits.

## Build order (two sequential PRs under this plan, per Cam's "Track A first")

- **PR-1 = Track A.** A0 attribution spike -> engine-side knee clamp (Rust `resolve` + Python
  boundary + tests) -> Python thread-pool fan-out for redact/truncate across both seams -> byte-parity
  + clamp tests -> dennis -> Codex FINAL -> CI -> merge. Then a GCP VERIFY run (speedup + knee
  re-confirm) reported before declaring done; A-fork only if the target misses.
- **PR-2 = Track B.** native categorical kernel + config gate + 5-seam registration -> byte-parity +
  KAT + decline tests -> dennis -> Codex FINAL -> CI -> merge.
(Each PR is independently byte-parity-gated and small enough to review cleanly. B needs no GCP.)

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
