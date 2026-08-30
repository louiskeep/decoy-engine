---
Status: plan
Created: 2026-08-28
Author: Opus (principal-engineer authoring), for Cam
Primary target repo: decoy-engine (native kernels, Rust extension, route lowering)
Parent plan: `docs/plans/2026-08-26-engine-efficiency-plan.md` (Part 1 Phase 2 row of A.3; Decision 4)
Repos: decoy-engine, decoy-platform (eligibility consumer only)
---

# Part 1 Phase 2 (Native Masking Hot Path) - Detailed Task-by-Task

> **For agentic workers:** Use the repository development loop for each task. Complete a task's gate before you start its successor. This document is the detailed build plan for the Phase 2 row of the parent plan. It does NOT re-litigate the substrate decision (Part A.2), the determinism protocol (Phase 0 Task 0.3), or the crypto contract (Phase 0 Task 0.4); it builds on them.

## 1. Goal and scope

**Goal.** Move the measured masking hot path off Python per-value loops for the four strategies Decision 4 admits, and replace the per-row keyed-derivation loop with a single security-reviewed Rust kernel over the Arrow C Data Interface. No logical value changes. The win is fewer Python object allocations and interpreter crossings per row on the exact surface Phase 1 already streams.

**In scope (Decision 4, binding, do not exceed):**

- Native `passthrough`, `redact`, and `truncate`, lowered to Arrow or DuckDB expressions (no Rust needed; these are non-keyed and row-local).
- Native keyed `hash`, executed by a new Rust `KeyedDerivationKernel` over the Arrow C Data Interface. This kernel wraps the established HKDF-SHA256 then HMAC-SHA256 primitive (Phase 0 Task 0.4 contract), never a new construction.
- The Rust build and packaging path, its CI, and its fail-before-output behavior.
- Config-aware `native_route_eligibility` (the Phase 1 carry-forward fix), required before this admitted set can execute natively.

**Deferred to Part 2 (explicitly OUT of scope here):**

- Native FPE (`FpeKernel.encrypt_batch` / `decrypt_batch`). The contract and pure-Python reference exist from Phase 0; the compiled FPE kernel, its NIST or ACVP evidence, and its side-channel gate are Part 2.
- Every other strategy: `date_shift`, `categorical`, `code_set`, `group_key`, `bucketize`, `top_code`, `geo_generalize`, `bucket_perturb`, `composite`, `joint_mask`, `shuffle`, `grouped_series`, `windowed_date`, `formula`, `nested`, `text_mask`, `text_redact`, `derived`, `derived_aggregate`, `orphan`.
- All providers and synthetic generation (Phase 3 and Part 2), vault, diagnostics globalizers, and every global or relational operation (Phase 4).
- Any admitted-set widening beyond `{passthrough, redact, truncate, hash}`.

**Right-sizing.** Single-org, one box, ~100M-row conservative cap. No multi-node planners, no distributed kernels, no service-boundary hardening beyond the crypto contract's fail-closed rules. The side-channel gate (crypto-testing-reference Gate 7) stays conditional and out of scope here per its own threat-model text, because Phase 2 ships keyed derivation (not FPE) for an offline single-org deployment; record that deployment decision rather than running dudect.

---

## 2. Frozen workload and performance gate (BEFORE implementation)

Per Decisions 8 and 10 and Part E T3, this gate is frozen here, before any kernel is written. No later contributor may weaken a number without a recorded Cam decision. **Targets 4 and 5 are now FROZEN from the Task 2.0 measured pandas oracle baseline (`PHASE2-BASELINE.md`, captured 2026-08-29 before any kernel); they are no longer proposals.** Task 2.0 was moved ahead of Task 2.1 per the Codex plan-gate so the numbers could not be back-fit to native results, and Task 2.7 consumes them unchanged (it does not recapture or re-freeze the oracle side). A native result that misses a frozen target FAILS the slice; changing a frozen number is a separately-approved plan revision, never an in-flight re-freeze. (Cam's 2026-08-29 overnight authorization let the author freeze the derived numbers and proceed, reviewing them alongside the built work.) The correctness and route-proof criteria are NOT proposals; they are hard and non-negotiable.

### 2.1 Representative workload (frozen as W2)

- One mask table. Every column uses only `{passthrough, redact, truncate, hash}` (the Phase 1 admitted set, which is exactly the Phase 2 strategy set, so W2 also exercises the Phase 1 streaming coordinator end to end).
- Shape (as measured): 10 columns. 3 keyed-`hash` columns (the dominant cost) over utf8 and int64 sources; 3 `passthrough` over int64, bool, and timestamp-with-tz; 2 `redact` and 2 `truncate` over utf8. So the frame's column types span utf8 / int64 / bool / timestamp-with-tz, but the keyed-`hash` throughput this workload measures reflects utf8 and int64 sources; bool and timestamp-with-tz appear as passthrough. The Rust canonicalizer's FULL hash-input type surface (bool and tz-timestamp as hash sources) is covered by the Task 2.2 KAT vector fixture, not by this throughput workload; W2 exists to fix the oracle's end-to-end cost and hash throughput, not to enumerate canonicalizer types.
- Size tiers, one fresh process or container per tier: 1x = 1,000,000 rows, 4x = 4,000,000 rows, 16x = 16,000,000 rows. NOTE (measured, see `PHASE2-BASELINE.md`): the 16x full-frame ORACLE does not fit the 12 GiB single-org box (peak RSS extrapolates to ~18 GB), so the oracle's 16x wall and RSS are extrapolated from the measured 1x/4x fit, not run to completion; the NATIVE side at 16x is measured at the Task 2.7 gate on hardware that can hold the oracle, or with the oracle streamed.
- One fixed `mask_key` and seed. Row order fixed for the parity comparison; a second permuted order is used only for the partition-invariance check.

### 2.2 Method (frozen)

- Warmup: 1 discarded run per tier. Timed repetitions: 5 per tier, each in a fresh process, for the tiers the oracle can hold (1x and 4x). The 16x oracle full-frame run is NOT executed (it exceeds the box, see the size-tiers note above); its figures are extrapolated, so no 16x oracle repetitions are claimed.
- Wall time: report median and interquartile range across the 5; tail is the max of the 5 (reported as p95-of-5).
- Peak RSS: measured EXTERNALLY (a wrapper process sampling RSS, or `/usr/bin/time -v`), never from inside the measured process. Report the max across the 5.
- Spill: report DuckDB spill bytes and whether spill occurred at each tier.
- Baseline: the pinned pandas full-frame oracle (`substrate="pandas"`, `execution_mode="full_frame"`, `auto_chunk=False`) on the identical W2 workload and seed, captured and frozen as numbers in Task 2.0 before any kernel is written (moved ahead of implementation per the Codex plan-gate).

### 2.3 Targets (the gate)

1. **Correctness guard (HARD, non-negotiable).** Exact seeded logical parity of the native route vs the pinned oracle for every column at every tier: identical values, row order, null placement, warnings (none expected for this set), and row errors (none expected). Physical differences permitted only from the enumerated allow-list (Part E T1). A single logical mismatch fails the gate outright, ahead of any speed or memory result.
2. **Intended-route proof (HARD, non-negotiable, Decision 10).** The job evidence must record that the native route ran AND that the Rust `KeyedDerivationKernel` executed each keyed-`hash` column. If the oracle completes the job, admission rejects it, the engine reroutes to the oracle, or the keyed-`hash` column ran on the pure-Python reference instead of the compiled kernel, the gate FAILS. The pure-Python reference is a test oracle only; it is never the production native path.
3. **KeyedDerivationKernel gate (HARD).** Byte-parity of the Rust kernel to `reference_keyed_derivation` over the full committed hash corpus plus every `HASH_KAT` vector; the primitive RFC 5869 (HKDF-SHA256) and RFC 4231 (HMAC-SHA256) known-answer vectors pass at the layers that specify them; the Arrow C Data Interface boundary accepts only `pa.Array` and rejects the mixed-object Python list form with the coded `mixed_object_not_native`; missing or `None` `mask_key` raises `MaskKeyRequiredError` before any output; a missing or ABI-incompatible extension raises `CryptoExtensionUnavailableError` before any staging; cross-process and cross-batch determinism holds (concatenated partitioned output equals the whole-column output, verified in fresh processes).
4. **Wall time (FROZEN from the Task 2.0 measured oracle, see `PHASE2-BASELINE.md`).** Native-route keyed-`hash` throughput **>= 163,403 rows/s per hash column** (2.0x the measured oracle 81,701 rows/s). Native 16x end-to-end wall **<= 410 s** (0.60x the ~683 s oracle 16x wall extrapolated from the measured 1x/4x linear fit; the oracle full-frame cannot complete 16x on the 12 GiB box, so 16x is extrapolated, and the full native-vs-oracle 16x comparison is measured at the Task 2.7 gate on hardware that holds the oracle side). Non-regression floor: native wall <= oracle wall at every tier (<= 50.7 s at 1x, <= 177.3 s at 4x). A native result that misses any of these FAILS the slice (§ Task 2.7 Step 4); changing a frozen number requires a separately-approved plan revision, never an in-flight re-freeze.
5. **Peak RSS (FROZEN, see `PHASE2-BASELINE.md`).** Two concrete, separately-measured bounds, both hard:
   a. **Absolute ceiling:** native-route external peak RSS (the Task 2.2 method: a parent samples the child's `VmHWM`) **<= 6.5 GiB (6,656 MB)** at 1x, 4x, and 16x. This is the Phase 1 streaming ceiling, well under the ~18.2 GB the full-frame oracle extrapolates to at 16x.
   b. **Flatness (numeric slope bound):** native-route peak RSS at 4x and at 16x is **<= 1.5x the native-route peak RSS at 1x** (a 16x row increase raises peak RSS by at most 50%, proving it does not scale with row count; a streaming/bounded route stays near-constant, so 1.5x is generous headroom for fixed overhead and sampling noise). If the native peak RSS grows faster than this across tiers, the gate fails even if it is under the absolute ceiling.
   c. **Bounded transient SCRATCH (excludes the required output buffer).** Measured in a Rust test build via a peak-tracking `#[global_allocator]` around a single `derive_batch` call over a FROZEN fixture: a fixed 4,096-row batch for each admitted input type (utf8, int64, bool, tz-timestamp), taken from the KAT vectors. Assert `(peak_outstanding_bytes - returned_output_buffer_bytes) <= 2x the input batch's Arrow buffer byte size`. Subtracting the output is essential: the derived hash strings are ~64 bytes per row and legitimately exceed a narrow int64 (8 B) or bool (1 B) input, so a raw "peak <= 2x input" bound is impossible, not strict. What this bound actually forbids is whole-column intermediate materialization and per-row heap-string scratch; the output itself is bounded by construction (exactly one derived string per input slot, allocated once). This is a Rust unit-level assertion over the frozen fixture, distinct from the process-RSS bounds (a) and (b).
   Phase 2 must not regress the Phase 1 streaming memory result.

If a target cannot be met, the failure is reported with its measured numbers, variance, and tail; the slice does not land by weakening the number.

---

## 3. Determinism (why every admitted strategy is partition-safe)

Phase 2 adds no new draw site. It executes existing Phase 0 draw sites through faster kernels, so the frozen protocol (Task 0.3) and its goldens remain the authority. No global or non-partitionable site is in scope.

- **Keyed `hash`** uses draw site `mask.source_keyed_hmac`, family `source_keyed_hmac`, `partitionable=True`. The token is a pure function of `(mask_key, namespace, canonicalize_derive_source(value))`. It has no row-order, batch-boundary, or prior-state dependence, so a whole-column result equals the concatenation of fresh-process partitioned results by construction. The Rust kernel reproduces the EXACT shipped sequence: HKDF-SHA256 (salt `decoy-engine/determinism/v1`, info = namespace UTF-8) to a 32-byte key, then HMAC-SHA256 over the frame `[version byte][BE u32 ns len][ns utf8][BE u32 src len][canonical src]` at `SEED_PROTOCOL_VERSION` 6, hex-encoded, optionally truncated. Same construction, same bytes; the kernel is a re-implementation of the primitive, not a new protocol, so no determinism family version bumps.
- **`passthrough`** draws nothing (identity). Row-local and trivially partition-safe.
- **`redact`** draws nothing (constant replacement of non-null values, nulls preserved). Row-local and partition-safe.
- **`truncate`** draws nothing (pure string slice or mask-fill per value, nulls preserved). Row-local and partition-safe.

No global site (`mask.shuffle`, `gen.grouped_series_walk`) and no `partitionable=False` site is admitted. There is nothing here for the protocol to re-version. The partition-invariance test (Part E T2) still runs on all four, because a kernel re-implementation is exactly where a batch-boundary bug would hide.

---

## 4. Task-by-task breakdown

Sequence by dependency: the Rust build scaffold and the pure-Rust kernel land first, then the Python loader shim, then the three non-keyed native kernels, then the keyed-hash node, then the eligibility fix and the route integration that runs the frozen gate. Pure-Rust tasks (2.1 core, 2.2) are separated from Python-orchestration tasks (2.3 to 2.7).

Module sizing: every new Python orchestration module caps at ~600 LOC (`internal/` import-linter-enforced). The Rust crate is organized so the security-sensitive derivation unit is small and independently testable (crypto-testing-reference §4, §5).

### Task 2.0: Freeze the performance gate from a measured pandas-oracle baseline (BEFORE any kernel)

Codex plan-gate finding: the numeric perf targets must be frozen from a measured baseline BEFORE Task 2.1, not captured at Task 2.7 after the native path already exists (which biases target selection toward whatever the native code happened to achieve). This task measures the pinned pandas full-frame oracle on the frozen W2 workload (§2.1) and freezes the numbers into §2.3 targets 4 and 5. No Rust, no kernel; measurement only.

**Files:**
- Create: `decoy-engine/docs/plans/PHASE2-BASELINE.md` (the frozen oracle numbers plus the exact W2 config, method, and environment, so the perf gate is reproducible).
- No source changes.

**Method:** exactly §2.2 (W2 at 1x/4x/16x; `substrate="pandas"`, `execution_mode="full_frame"`, `auto_chunk=False`; 1 warmup + 5 timed reps per tier in fresh processes; wall median/IQR/p95-of-5; external peak RSS; keyed-hash strategy throughput; spill). The 16x full-frame oracle does NOT fit the 12 GiB box (peak RSS extrapolates to ~18 GB), so its wall and RSS are EXTRAPOLATED from the measured 1x/4x linear fit, not run to completion (recorded explicitly in `PHASE2-BASELINE.md`); 1x and 4x are each measured at 5 fresh-process reps. The full native-vs-oracle comparison at 16x is measured at the Task 2.7 perf gate on hardware that holds the oracle side (or with the oracle streamed); Task 2.0 fixes the oracle side (measured 1x/4x, extrapolated 16x) and the frozen target multipliers.

**Steps:**
- [ ] **Step 1: Build the W2 config and a fresh-process bench harness** (external RSS sampling, per-strategy hash timing).
- [ ] **Step 2: Run the oracle baseline** at 1x/4x/16x per the method.
- [ ] **Step 3: Freeze §2.3 targets 4 and 5** as concrete numbers relative to the measured oracle (target 4: native 16x wall <= 0.60x the measured oracle 16x wall AND hash throughput >= 2.0x the measured oracle hash throughput, with a non-regression floor at every tier; target 5: the declared peak-RSS ceiling as a concrete MB number, from the Phase 1 streaming ceiling and the measured full-frame RSS curve). Record the frozen numbers in PHASE2-BASELINE.md and rewrite §2.3 targets 4 and 5 from PROPOSED to frozen so no later contributor re-derives them.
- [ ] **Step 4: Cam sign-off checkpoint (recorded).** The frozen numbers are presented for Cam's confirmation. Overnight authorization 2026-08-29: the author may freeze the derived numbers and proceed to Task 2.1; Cam reviews the frozen targets together with the built work.

**Acceptance:** PHASE2-BASELINE.md exists with per-tier oracle numbers, variance, and the exact W2 config and environment; §2.3 targets 4 and 5 carry concrete frozen numbers, not PROPOSED placeholders; the correctness, intended-route, and kernel criteria (targets 1, 2, 3) are unchanged and remain hard and non-negotiable.

---

### Task 2.1: Rust build and packaging scaffold (build-system, precondition)

Establish how a Rust extension is built, shipped in the wheel, and tested in CI, before any kernel code depends on it. This is a build-system task, not a crypto task.

**Files:**
- Create (NEW companion package `decoy-engine-native`, a sibling package directory, its own pyproject/crate): `decoy-engine-native/pyproject.toml` (maturin build backend), `decoy-engine-native/Cargo.toml`, `decoy-engine-native/src/lib.rs` (exported module stub returning an ABI-version tag only), `decoy-engine-native/README.md` (package purpose, toolchain, wheel mapping, relationship to the core), `decoy-engine-native/rust-toolchain.toml`.
- Modify: `decoy-engine/pyproject.toml` -- add an OPTIONAL `native` extra depending on `decoy-engine-native==<pinned>`. The core build backend STAYS hatchling; the core wheel and sdist stay `py3-none-any` and Rust-free (do NOT switch the core to maturin).
- Do NOT touch `_crypto_ext.py` in this task. The production loader `load_compiled_crypto_kernel` stays exactly as Phase 0 shipped it (always raising `CryptoExtensionUnavailableError`) until Task 2.3, which OWNS the real load + ABI check + wrapper. This task is build-system only and tests the compiled module DIRECTLY, so it needs no loader change (this is the loader-ownership split the Codex plan-gate required).
- Create: two CI paths -- the companion builds its wheel across the supported target matrix with the Rust toolchain and asserts the compiled module is present in the artifact; the core CI runs its existing suite in BOTH companion-present and companion-absent environments.
- Test: `decoy-engine/tests/native/test_native_ext_abi.py`.

**Interfaces:**
- Produces the CANONICAL compiled module `decoy_engine_native._kernel` inside the `decoy-engine-native` companion package (name pinned once here; used verbatim by the Rust crate in Task 2.2 and the loader in Task 2.3), exposing a single `abi_version() -> str`. Nothing calls it from the masking path yet, and the production loader is UNCHANGED in this task.
- The core imports and runs WITHOUT the companion installed (the `native` extra is optional). Absence of the companion continues to surface as `CryptoExtensionUnavailableError` through the still-Phase-0 `load_compiled_crypto_kernel`; making that loader actually load the companion + check the ABI tag is Task 2.3.

**Steps (strict TDD):**
- [ ] **Step 0 (setup prerequisite, DO NOT perform here, record only):** the companion's build host and CI runners need a pinned stable Rust toolchain (`rustup`, an `edition = "2021"`-capable stable `rustc`, pinned in the companion's `rust-toolchain.toml`). The CORE needs no Rust. Environment provisioning for whoever executes the plan; the author does not install it.
- [ ] **Step 1: Write the failing tests against the compiled MODULE directly (NOT the loader, which Task 2.3 owns).** Two cases:
  1. **companion-present (initially FAILING):** `import decoy_engine_native._kernel` succeeds and `decoy_engine_native._kernel.abi_version()` equals the pinned ABI tag. FAILS today (no companion built) and drives the implementation.
  2. **companion-absent (guard, passes today):** with the companion not installed, `import decoy_engine` and its existing suite still succeed (no hard dependency), and `import decoy_engine_native` raises `ImportError`. This proves the core stays installable and importable Rust-free.
- [ ] **Step 2: Run to verify:** case 1 fails (no companion built yet); case 2 passes (the core already imports without the companion).
- [ ] **Step 3: Implement** the companion Cargo crate stub (exporting only `abi_version()`), its maturin `pyproject.toml`, the core `native` extra, and the two CI paths. No change to the core build backend and no change to `_crypto_ext.py`.
- [ ] **Step 4: Run to verify passing:** with the companion built and installed, case 1 is green; with it absent, case 2 is green.
- [ ] **Step 5: Commit.** `git commit -m "build(native): decoy-engine-native companion scaffold + core native extra + ABI-tag stub (build-system only)"`

**Acceptance tests:**
- A companion wheel built on the target matrix imports and `decoy_engine_native._kernel.abi_version()` returns the pinned tag.
- A core install WITHOUT the companion imports `decoy_engine`, runs its existing suite, and `import decoy_engine_native` raises `ImportError` (no hard dependency).
- The loader's load-and-ABI-check behavior is NOT asserted here; it is Task 2.3's acceptance (the loader is untouched in this task).

**Failure modes and guards:**
- Core accidentally hard-depends on the companion -> the companion-absent CI job importing `decoy_engine` and running its suite catches it.
- Companion CI builds the wheel but skips the Rust step -> a CI assertion that `_kernel` is present in the built wheel fails the pipeline.
- ABI-tag drift and loader fail-closed behavior are guarded in Task 2.3 (the loader owner), not here.

### Task 2.2: Rust `KeyedDerivationKernel` (pure Rust, security-sensitive core)

Implement `derive_batch` in Rust to reproduce `reference_keyed_derivation` byte for byte over the Arrow C Data Interface. This is the one security-sensitive unit; keep it small and heavily tested (crypto-testing-reference §4, §5, §6).

**Files (all Rust lives in the `decoy-engine-native` companion package from Task 2.1; nothing Rust goes in the core):**
- Create: `decoy-engine-native/src/derive.rs` (HKDF-SHA256 + HMAC-SHA256 framing + canonical source encoding).
- Create: `decoy-engine-native/src/canonicalize.rs` (typed canonical source bytes, mirroring `kernel/_canonicalize.canonicalize_derive_source` for the supported Arrow types only).
- Create: `decoy-engine-native/src/arrow_ffi.rs` (import a `pa.Array` over the C Data Interface, export a string `pa.Array`).
- Modify: `decoy-engine-native/src/lib.rs` (export `derive_batch` from the canonical compiled module `decoy_engine_native._kernel`).
- Test (Rust): `decoy-engine-native/tests/kat_derive.rs`, proptest regressions under `decoy-engine-native/proptest-regressions/`.
- Test (Python parity, added in Task 2.5's harness, referenced here): the compiled kernel is graded against `reference_keyed_derivation` and `HASH_KAT`.

**Interfaces:**
- `derive_batch(array, mask_key, namespace, truncate) -> string array`, matching the `KeyedDerivationKernel` Protocol in `_crypto_ext.py` exactly (mask_key NAMED, not "seed").
- Accepts ONLY a typed `pa.Array` over the C Data Interface. Supported input Arrow types: utf8 / large_utf8, signed and unsigned integer widths, bool, and timestamp-with-timezone. Any other type (float, naive timestamp, mixed-object) is rejected with `mixed_object_not_native` (the compiled kernel never sees the Python list form; eligibility excludes those columns upstream, Task 2.6).
- Uses reviewed crates for the primitive: `hkdf` + `hmac` + `sha2` (RustCrypto), never a hand-rolled hash or HMAC. Cite RFC 5869 and RFC 4231 in the module docstring per the established-methodology rule.
- Null policy: a null slot maps to a null output slot and consumes no derivation (matches `_is_missing`).

**Steps (strict TDD):**
- [ ] **Step 1: Write failing Rust KATs** loading the shared `HASH_KAT` vectors and RFC 5869 / RFC 4231 primitive vectors, asserting exact output bytes. Include the framing ambiguity vector (`("ab","c")` vs `("a","bc")` change the result) and the namespace-vs-source boundary vector.
- [ ] **Step 2: Run to verify they fail** (no `derive.rs` yet).
- [ ] **Step 3: Implement** HKDF-SHA256 (salt `decoy-engine/determinism/v1`, info = namespace UTF-8), the exact HMAC frame, and the typed canonicalizer for the supported Arrow types. Use checked integer arithmetic for length prefixes. No secret-indexed table or early-exit comparison in the derivation path.
- [ ] **Step 4: Run to verify passing.**
- [ ] **Step 5: Add Rust proptest** for batch invariance (whole array equals concatenated partitions, including empty and prime-sized batches) and null-vs-empty distinction; save minimized regressions.
- [ ] **Step 6: Commit.** `git commit -m "feat(native-rust): KeyedDerivationKernel derive_batch over Arrow C Data Interface (HKDF-SHA256/HMAC-SHA256, KATs pass)"`

**Acceptance tests:**
- Every `HASH_KAT` vector reproduces exactly (string and integer sources, truncated and untruncated).
- RFC 5869 test cases 1-3 and RFC 4231 HMAC-SHA256 cases pass at the primitive layer.
- Batch-invariance proptest: for any partition of any input, concatenated output equals the whole-array output.
- A float or naive-timestamp array is rejected with `mixed_object_not_native`; no partial output.

**Failure modes and guards:**
- Endianness drift on length prefixes -> caught by the framing golden vectors with explicit byte dumps (crypto-testing-reference §3.2).
- Unicode normalization drift (NFC vs NFD) -> caught by composed/decomposed KAT pairs.
- Canonicalizer disagreement with Python for a supported type -> caught by the Python parity harness in Task 2.5 (byte-for-byte over the full corpus).
- A supported-but-unhandled Arrow subtype silently coerced -> rejected explicitly; a coercion path is a bug, not a fallback.
- Key or source bytes in an error message -> a snapshot test asserts sentinel key/source bytes are absent from every error string.

**Shared KAT vector fixture (Codex plan-gate finding, fully-specified language-neutral contract).** The Rust KATs and the Python parity harness MUST load the SAME committed vector file, not two hand-kept copies. Create `decoy-engine-native/vectors/keyed_derivation_kat.json` (canonical, `format_version`-tagged; a symlink or path constant makes the core test tree read the same file). It is a JSON object `{format_version, cases: [...]}`. Each case is a BATCH (so null slots fit naturally) with an unambiguous schema:
```
{
  "name": "...",
  "arrow_type": {                     // constructs the typed Arrow array unambiguously
    "kind": "utf8" | "large_utf8" | "int" | "bool" | "timestamp",
    "bits": 8|16|32|64,               // int only
    "signed": true|false,             // int only
    "unit": "s"|"ms"|"us"|"ns",       // timestamp only
    "tz": "UTC" | "<IANA name>" | null // timestamp only
  },
  "logical_values": [ ... ],          // parallel to the slots; JSON string for utf8, JSON bool for
                                      // bool, DECIMAL STRING for every int width (JSON numbers lose
                                      // 64-bit precision), ISO-8601 with offset for timestamp, and
                                      // JSON null for a null slot
  "mask_key_hex": "...",
  "namespace": "...",
  "truncate": <int> | null,
  "expected_canonical_source_hex": [ ... ], // per non-null slot, the exact canonical source bytes
                                            // the kernel derives from (null for a null slot)
  "expected_output": [ ... ]                // per slot, the derived string (null for a null slot)
}
```
Cases cover every admitted input Arrow type (utf8/large_utf8; int8/16/32/64 signed and unsigned; bool; timestamp with each unit and a tz), the NFC/NFD normalization pair, the framing-ambiguity pair (`("ab","c")` vs `("a","bc")` as two cases with identical bytes but different namespace/source splits), the namespace-vs-source boundary, null slots interleaved with non-null, and truncated/untruncated forms. Both `expected_canonical_source_hex` (canonicalizer output) and `expected_output` (full derivation) are asserted, so a canonicalizer disagreement is caught even when the final HMAC would coincide. The Rust `kat_derive.rs` test and the Python `HASH_KAT` grader both read this file; the existing Phase 0 Task 0.3 goldens and the Task 0.6 public parity harness are the parity oracles, referenced explicitly (not re-implemented as a new similar harness). A `format_version` bump requires a recorded determinism-family decision.

**Rust release gate (Codex plan-gate finding, blocking acceptance before the kernel replaces the Python path).** The parent's Rust release requirement (packaging, ABI, threading, security, fail-before-output) is made explicit and blocking here, applying only to the keyed-derivation portions of the crypto reference:
- **Ownership/threading:** the kernel is `Send`/`Sync`-audited and stateless; a test drives `derive_batch` concurrently across threads on shared read-only inputs and asserts identical output and no data race (run under `cargo test` and, in CI, once under ThreadSanitizer).
- **Panic/FFI boundary:** no `unwrap`/`expect`/`panic!` on the FFI path; every fallible step returns a typed error mapped to `mixed_object_not_native` / `MaskKeyRequiredError` / `CryptoExtensionUnavailableError`. A test feeds malformed Arrow C Data Interface metadata (bad length, null buffer, wrong type tag) and asserts a clean typed error crosses the boundary, never a panic unwinding into Python.
- **Fuzz/sanitizer evidence:** a `cargo fuzz` target over `derive_batch`'s canonicalizer runs a bounded corpus in CI; the crate's tests run once under AddressSanitizer. Evidence (no crash, no leak) is recorded.
- **Independent crypto-aware review bound to the exact artifact:** the derivation unit gets a security review against the crypto-testing-reference (constant-time-where-required, salt/info framing, no secret-indexed table or early-exit compare), sign-off tied to the reviewed commit hash before the kernel is admitted to the production path.

### Task 2.3: Python loader shim, ABI-version check, fail-before-output wiring

Replace the Phase 0 always-raising `load_compiled_crypto_kernel` body with a real load that verifies the ABI tag and returns a kernel object satisfying the `KeyedDerivationKernel` Protocol, preserving the fail-before-output contract exactly.

**Files:**
- Modify: `decoy-engine/src/decoy_engine/execution/native/_crypto_ext.py` (`load_compiled_crypto_kernel` body only; the Protocols, references, KATs, and `CRYPTO_EXT_ABI` doc are unchanged).
- Test: `decoy-engine/tests/native/test_crypto_ext_loader.py`.

**Interfaces:**
- `load_compiled_crypto_kernel() -> KeyedDerivationKernel`: loads the canonical compiled module `decoy_engine_native._kernel` (the `decoy-engine-native` companion from Task 2.1), checks `abi_version()` against the core's pinned expected tag, and returns a thin wrapper exposing `derive_batch(...)`. On any failure (companion absent, load error, tag mismatch) it raises `CryptoExtensionUnavailableError` BEFORE returning, so no caller ever holds a half-initialized kernel.
- The FPE kernel loader stays deferred: this task loads only the keyed-derivation entry point. FPE remains reference-only.

**Steps (strict TDD):**
- [ ] **Step 1: Write failing tests**: with the compiled module present, the loader returns a kernel whose `derive_batch` matches `reference_keyed_derivation` on a small array; with the module monkeypatched to report a wrong ABI tag, the loader raises `CryptoExtensionUnavailableError`; with a `None` mask_key, `derive_batch` raises `MaskKeyRequiredError` before any output.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** the load + ABI check + wrapper. Do not catch and swallow; map every load failure to `CryptoExtensionUnavailableError` with a redacted message.
- [ ] **Step 4: Run to verify passing**, including the pure-Python-install path (loader raises cleanly).
- [ ] **Step 5: Commit.** `git commit -m "feat(native): compiled keyed-derivation loader with ABI-version check + fail-before-output"`

**Acceptance tests:**
- Loader returns a working kernel when the module is present; byte-parity with the reference on a sample array.
- Wrong ABI tag, absent module, and load error each raise `CryptoExtensionUnavailableError`, never another exception type, never a partial object.
- `None` / missing mask_key raises `MaskKeyRequiredError` before any row is processed.

**Failure modes and guards:**
- Silent degradation to the reference kernel inside production -> forbidden: the loader returns the compiled kernel or raises; it never substitutes the reference.
- Half-initialized kernel after a partial load -> guarded by raising before return.

### Task 2.4: Native `passthrough`, `redact`, `truncate` lowering (Arrow/DuckDB expressions)

Lower the three non-keyed row-local strategies to Arrow or DuckDB expressions so they run without a Python per-value loop, producing output byte-identical to the shipped handlers.

**Files:**
- Create: `decoy-engine/src/decoy_engine/execution/native/_kernels_scalar.py` (<600 LOC: `native_passthrough`, `native_redact`, `native_truncate` over an Arrow array, reusing the existing shared `kernel/_scalar` functions where they already express the logic).
- Test: `decoy-engine/tests/native/test_kernels_scalar.py`, plus parity cases added to the Phase 0 matrix.

**Interfaces:**
- `native_passthrough(array) -> array` (identity).
- `native_redact(array, *, redact_with) -> array` (non-null values replaced with the constant; nulls preserved; matches `RedactHandler` including the `redact_with` default "REDACTED").
- `native_truncate(array, *, length, keep, mask_char) -> array` (reuses `kernel.truncate_array`, already the shared source of truth the pandas handler calls; nulls preserved; the same fail-closed `StrategyError` codes on invalid config).
- Each function is a pure array-to-array transform with no draw and no state.

**Steps (strict TDD):**
- [ ] **Step 1: Write failing parity tests** comparing each native kernel against its shipped handler over the Phase 0 fixture arrays: strings, integers, nulls, empty strings, `redact_with` override, `truncate` head/tail/mask_char combinations, and the invalid-config fail-closed branches (`truncate_length_invalid`, `truncate_keep_invalid`, `truncate_mask_char_invalid`).
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** the three kernels, reusing `kernel.truncate_array`, `kernel.redact_array`, and `kernel.passthrough_array` so there is ONE logic source. The native path differs from the handler only in that it does not round-trip through pandas `to_pylist`.
- [ ] **Step 4: Run to verify passing**, and confirm no Phase 0 golden fingerprint moves.
- [ ] **Step 5: Commit.** `git commit -m "feat(native): native passthrough/redact/truncate scalar kernels (byte-parity with handlers)"`

**Acceptance tests:**
- Byte-parity with each shipped handler across the fixture matrix (values, nulls, dtype where the allow-list permits, and the null-typed normalization already enumerated in Phase 0).
- Invalid `truncate` config raises the SAME `StrategyError` code as the handler; a masking strategy never silently passes a source value.
- `redact` with a custom `redact_with` matches; nulls stay null.

**Failure modes and guards:**
- Extension-dtype coercion drift in `redact` -> the parity test includes an extension-dtype column so the Arrow path matches the handler's object-dtype fallback result.
- A `truncate` character-count model mismatch (grapheme vs code point) -> the fixture includes multi-byte and combining-mark strings; parity must hold against the shared `truncate_array`.

### Task 2.5: Native keyed-`hash` node wired to the Rust kernel

Wire the compiled `KeyedDerivationKernel` into a native keyed-`hash` execution node, with the pure-Python reference used ONLY as the test oracle and the cross-process parity harness.

**Files:**
- Create: `decoy-engine/src/decoy_engine/execution/native/_kernels_keyed.py` (<600 LOC: resolve namespace + truncate from the node, call the loaded compiled kernel, return the Arrow array).
- Create: `decoy-engine/tests/parity/native/test_keyed_hash_parity.py` (cross-language, cross-process harness per crypto-testing-reference §5.3).
- Test: `decoy-engine/tests/native/test_kernels_keyed.py`.

**Interfaces:**
- `native_keyed_hash(array, *, mask_key, namespace, truncate) -> array`, calling the compiled kernel from `load_compiled_crypto_kernel`. Namespace is required (mirrors `HashStrategyHandler`'s `hash_requires_namespace` fail-closed). A `None` mask_key fails closed via the loader contract.
- The parity harness launches the reference kernel and the compiled kernel through their PUBLIC entry points (never re-implementing framing in the harness) and asserts serialized equality, including at least one run with reference and compiled kernel in DIFFERENT processes (detects shared state and import-time config).

**Steps (strict TDD):**
- [ ] **Step 1: Write failing tests**: `native_keyed_hash` equals `reference_keyed_derivation.derive_batch` over the full committed corpus plus `HASH_KAT`; a whole-column result equals concatenated partitioned results across three batch sizes and two orders; a cross-process run agrees; missing namespace and missing key fail closed.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** the node and the harness. Add the harness mutation checks (crypto-testing-reference §5.4): removing the namespace from the frame, flipping a length prefix's endianness, and truncating at the wrong width each MUST cause at least one harness failure, proving the harness can fail.
- [ ] **Step 4: Run to verify passing**, in-process and cross-process.
- [ ] **Step 5: Commit.** `git commit -m "feat(native): native keyed-hash node on the Rust kernel + cross-process parity harness"`

**Acceptance tests:**
- Byte-parity with the reference over the full corpus and `HASH_KAT`, in-process and in a fresh subprocess.
- Partition invariance across batch sizes, orders, empty batches, and multiple Arrow chunk layouts.
- Missing namespace -> fail closed; missing / `None` mask_key -> `MaskKeyRequiredError` before output.
- Every harness mutation produces a failing test (the harness is proven able to catch a critical-field omission).

**Failure modes and guards:**
- The harness is green while omitting a field -> the §5.4 mutation tests block that.
- A compiled-kernel bug masked by comparing against itself -> forbidden by §3 of the reference: never grade the kernel against a golden generated by the kernel under test in the same run; goldens come from the reference and committed vectors.
- Route silently used the reference in production -> Task 2.7's route-proof asserts the compiled kernel ran.

### Task 2.6: Config-aware `native_route_eligibility` (Phase 1 carry-forward fix)

`native_route_eligibility` currently classifies by strategy + capabilities and is config-blind: it does not verify that a column's resolved CONFIG shape and INPUT type are ones the native kernels support. This must be fixed BEFORE the admitted set executes natively, so a config the kernel cannot honor is excluded at preflight rather than failing mid-execution.

**Files:**
- Modify: `decoy-engine/src/decoy_engine/execution/native/_plan.py` (`native_route_eligibility` / `_column_rejection`).
- Modify: `decoy-engine/src/decoy_engine/execution/native/_requirements.py` if the resolved input Arrow type is needed on `NodeRequirements` (it already carries `output_arrow_schema`; add the resolved input type check for keyed-hash type support).
- Test: `decoy-platform`-side consumer test stays in platform; engine test: `decoy-engine/tests/native/test_native_plan_config_aware.py`.

**Interfaces:**
- `native_route_eligibility` gains config/type awareness for the admitted set:
  - keyed `hash`: reject a column whose resolved input Arrow type is not in the supported native-hash type set (utf8/large_utf8, integer widths, bool, timestamp-with-tz) with a coded reason `hash_input_type_not_native:<col>:<type>`; reject a mixed-object column with `mixed_object_not_native:<col>`.
  - `truncate`: reject a config that would fail the handler's fail-closed checks at compile time with the same coded reason, so an invalid config never reaches the native path as a would-be `StrategyError`.
  - `redact`, `passthrough`: no additional config gate (constant / identity), but confirm the resolved output type is static.
- The query stays TOTAL over the live registry and drift-sentried (the Phase 0 totality test still passes).

**Steps (strict TDD):**
- [ ] **Step 1: Write failing tests**: a `hash` column over a float or naive-timestamp input is rejected with `hash_input_type_not_native`; a mixed-object column is rejected with `mixed_object_not_native`; a `hash` column over utf8/int64/bool/timestamp-tz is admitted; a valid `truncate`/`redact`/`passthrough` column is admitted; an invalid `truncate` config is rejected at eligibility, not deferred to execution.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** the config/type-aware rejections, reading the resolved input type from the compiled node, not a guess. Keep agreement with `compile_native_plan` (same shared predicates).
- [ ] **Step 4: Run the Phase 0 totality + drift tests** to prove the query stays total and no previously admitted case silently changes.
- [ ] **Step 5: Commit.** `git commit -m "fix(native): make native_route_eligibility config-aware and input-type-aware before hot-path admission"`

**Acceptance tests:**
- Unsupported input type or mixed-object column for keyed `hash` -> coded rejection, table reroutes to the oracle.
- Invalid `truncate` config -> rejected at eligibility.
- Supported types and valid configs for all four strategies -> admitted.
- Totality and drift sentries still pass against the live registry.

**Failure modes and guards:**
- A widening slips in via config-blindness -> this task closes exactly that gap; the exhaustive live-registry test asserts the admitted set is still `{passthrough, redact, truncate, hash}` and nothing more.
- Eligibility disagrees with the compiler -> the shared-predicate construction plus a compiler-vs-eligibility agreement test guards it.

### Task 2.7: Route integration and the frozen Phase 2 gate

Route an admitted table's four strategies through the native kernels within the Phase 1 streaming coordinator, prove the intended route ran, and run the frozen §2 gate.

**Files:**
- Modify: `decoy-engine/src/decoy_engine/execution/native/__init__.py` or the native dispatch entry the Phase 1 coordinator calls (dispatch admitted nodes to `_kernels_scalar` / `_kernels_keyed`; everything else stays on the oracle).
- Modify: the job evidence / manifest writer to record the executed route per node (native-kernel vs oracle) and, for keyed `hash`, that the COMPILED kernel ran.
- Create: `decoy-engine/tests/parity/native/test_phase2_gate.py` (the frozen W2 correctness + route-proof harness), and a benchmark script under the existing benchmark harness for the perf/RSS numbers.
- Test: `decoy-engine/tests/native/test_native_dispatch.py`.

**Interfaces:**
- The dispatch runs each admitted node through its native kernel and records the route tag. A missing or ABI-incompatible extension at PREFLIGHT reroutes the whole table to the oracle (no mid-stream fallback, no partial-native output), consistent with the whole-job preflight rule.
- The evidence records: route per node, compiled-kernel-executed flag for keyed hash, and the parity + resource results.

**Steps (strict TDD):**
- [ ] **Step 0: Consume the FROZEN oracle baseline (do NOT recapture or re-freeze).** The oracle numbers are already frozen in Task 2.0 (`PHASE2-BASELINE.md`); Task 2.7 reads them as-is and does not re-measure or move them. This step measures the NATIVE route on the identical W2 (wall, external peak RSS, spill, keyed-hash throughput) at 1x and 4x, and at 16x where the gate hardware can hold the oracle side (or with the oracle streamed) for the full native-vs-oracle 16x comparison; otherwise the frozen extrapolated 16x oracle wall from PHASE2-BASELINE.md is the reference.
- [ ] **Step 1: Write failing tests**: an all-`{passthrough,redact,truncate,hash}` table dispatches every node to a native kernel (assert route tags); exact seeded parity vs the oracle across three batch sizes and two orders; the evidence records the compiled kernel ran for keyed hash; a table with the compiled extension absent reroutes to the oracle at preflight (assert no native kernel ran and no partial output staged).
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** the dispatch, the route recording, and the preflight reroute-on-absent-extension behavior.
- [ ] **Step 4: Run the frozen gate:** correctness (hard), route-proof (hard), KeyedDerivationKernel gate (hard), then wall time and peak RSS vs the FROZEN Task 2.0 numbers. Report median/IQR/tail and external peak RSS. Any failure -- a hard criterion OR a frozen target 4/5 miss -- FAILS the slice; the slice does not land by weakening a number. Changing a frozen target is a separately-approved plan revision, never an in-flight re-freeze.
- [ ] **Step 5: Commit.** `git commit -m "feat(native): route admitted masking through native kernels + frozen Phase 2 correctness/route/perf gate"`

**Acceptance tests:**
- Every admitted node runs on a native kernel; keyed hash runs on the compiled kernel; evidence records it.
- Exact seeded logical parity vs the oracle at every tier and batch size.
- Extension absent at preflight -> whole table on the oracle, zero native execution, zero partial output.
- Perf and RSS results captured with variance and tail against the frozen baseline; hard criteria pass.

**Failure modes and guards (the Decision 10 traps):**
- Oracle completed the job -> route-proof FAILS the gate.
- Engine fell back mid-stream -> forbidden by the preflight-only reroute; a mid-stream fallback assertion fails.
- Keyed hash ran on the reference instead of the compiled kernel -> route-proof FAILS.
- A green oracle run mistaken for proof the native route ran -> the route tag, not job success, is the evidence.

---

## 5. Rust build and packaging: recommendation

**Recommendation (Cam decision 2026-08-29: Option B): a separate companion package `decoy-engine-native`, built with maturin + PyO3 and bridging the Arrow C Data Interface via `pyo3-arrow` (or the `arrow` crate's `ffi` module), that ships the compiled `KeyedDerivationKernel`. `decoy-engine` stays pure-Python (hatchling, unchanged build backend) and depends on the companion OPTIONALLY (a `native` extra), loading it at runtime through `load_compiled_crypto_kernel` and rerouting to the pandas oracle at preflight when it is absent or ABI-incompatible.**

One-line rationale: only the companion package keeps `pip install decoy-engine` a pure-Python, Rust-free install at both build and install time (a maturin core sdist cannot install without a Rust toolchain), which matches the self-hosted single-org deployment story and the "optional accelerator, fail-closed reroute" design exactly. maturin + PyO3 (inside the companion) remains the standard, cibuildwheel-friendly way to ship a Rust extension with memory-safe bindings and first-class Arrow C Data Interface support, so we avoid hand-rolling `cffi` FFI on the one security-sensitive boundary.

Supporting notes:
- **Two-package layout:** `decoy-engine` (core, hatchling, `py3-none-any` wheel + pure-Python sdist, Rust-free install) and `decoy-engine-native` (companion, maturin, platform wheels). The core's `native` extra pins the compatible companion version; the companion exposes the compiled module under its own import namespace, loaded through `load_compiled_crypto_kernel`.
- **Core-to-companion compatibility (named acceptance criterion, Task 2.1/2.3):** the loader checks the companion's `abi_version()` against the core's pinned expected tag on every load; a stale, mismatched, or absent companion raises `CryptoExtensionUnavailableError` before any native admission, so an incompatible binary reroutes to the oracle rather than running. This is the one new correctness surface Option B introduces and it is a hard, tested acceptance criterion, not prose.
- **Toolchain prerequisite (setup step, not performed here):** a pinned stable Rust toolchain via `rust-toolchain.toml` on the companion's build hosts and CI runners only (the core needs no Rust). Provision it before Task 2.1 executes.
- **Wheel shipping:** prebuilt manylinux (x86_64 and aarch64) companion wheels for cp310, cp311, and cp312 via cibuildwheel + maturin cover the self-hosted single-org deployment targets (the core's `requires-python = ">=3.10"`, so the native extra matches cp310-cp312; a narrower matrix would strand 3.10 installs on the oracle silently); the core wheel/sdist are unaffected and stay Rust-free.
- **CI build/test:** the companion CI matrix builds its wheel with the Rust step, asserts the compiled module is present in the artifact, and runs the Rust KATs/proptests plus the Python cross-process parity harness (crypto-testing-reference Gates 1, 2, 4). Core CI separately tests the companion-present and companion-absent states.
- **Fail-closed when the companion is absent:** REROUTE to the pandas full-frame oracle at PREFLIGHT. Rationale: the oracle produces byte-identical logical output for this set, so a correct route always exists; the constraint is "no mid-stream fallback" and "fail or reroute before staging," not "reject the job." The pure-Python reference kernel is a TEST oracle only and is never substituted into the production native path (that would silently lose the perf claim and blur the route-proof). A hard reject is reserved for admission's priced small-job limit, not for a missing accelerator.
- **Versioning:** release core and companion together at first; the core's `native` extra pins the exact compatible companion version, and the ABI tag reroutes any stale binary safely. A later compatibility range can permit companion-only security releases without rebuilding the core.

---

## 6. Risks and carry-forwards

### 6.1 Phase 2 risks (with mitigations)

1. **A kernel re-implementation drifts from the oracle at a batch boundary.** The frozen protocol goldens (Task 0.3) plus the partition-invariance harness (Part E T2) and the cross-process parity harness (Task 2.5) run on all four strategies.
2. **The Rust canonicalizer disagrees with Python on a supported type.** The byte-for-byte parity harness over the full corpus, framing byte-dump vectors, and NFC/NFD KAT pairs guard each supported type; unsupported types are rejected, not coerced.
3. **The extension fails through packaging, ABI, or key handling.** The two-state build test, the ABI-tag check, fail-before-output, and redacted errors (no key/source bytes) cover this. The full crypto-testing-reference gate ladder (KATs, parity, property, cross-process) applies before the kernel replaces the Python path.
4. **Route evidence hides a fallback.** The route tag and compiled-kernel flag in the evidence are the proof, not job success (Decision 10).
5. **A config-blind admission lets an unsupported column reach the kernel.** Task 2.6 closes this before the hot path is admitted.

### 6.2 Inherited carry-forwards (tracked, not resolved by Phase 2)

- **Phase 0 Task 0.4: the FPE checksum-priority branch is untested.** This lives in the pure-Python FPE reference (`_ReferenceFpe`), which stays reference-only in Phase 2. The branch is inherited by Part 2's native FPE work and is NOT exercised here; Phase 2 must not be read as having covered it.
- **Phase 0 native-substrate placeholder.** The native execution substrate carries a placeholder from Phase 0; Phase 2 wires real kernels for the admitted set only. Any node outside the admitted set still resolves to the oracle, and the placeholder for deferred node kinds remains until its Part 2 slice activates.
- **Phase 1 carry-forward: `native_route_eligibility` was config-blind.** Task 2.6 fixes this specifically for the admitted set and is a hard precondition of Phase 2 admission. Any FUTURE widening must extend the same config/type-aware gate before admitting a new strategy; the exhaustive live-registry test is the sentry.
- **Phase 1 landing dependency (unchanged).** The admission claim-time route classifier remains the landing dependency for streaming route selection; Phase 2 consumes the chosen route and lease and does not acquire another.

---

## 7. Global constraints alignment

- **No em-dashes** anywhere in this document or the code and comments it plans (period, comma, colon only).
- **Comments explain why, not what**; one line unless a real invariant needs more; no references to task, PR, or author.
- **Module sizing:** every new Python orchestration module (`_kernels_scalar.py`, `_kernels_keyed.py`) caps at ~600 LOC; the Rust crate keeps the security-sensitive derivation unit small and independently tested.
- **Established methodology, cited:** HKDF-SHA256 (RFC 5869) and HMAC-SHA256 (RFC 4231, RFC 2104) via reviewed RustCrypto crates; Arrow C Data Interface via `pyo3-arrow` / arrow-rs FFI; maturin + PyO3 packaging. We roll no new crypto and invent no new RNG; the kernel reproduces the shipped primitive. Citations go in each implementing module's docstring.
- **Pre-GA hard delete.** Pre-GA is a hard cutover; the keyed-hash kernel reproduces `SEED_PROTOCOL_VERSION` 6 exactly, no manifests exist in the wild, and no compatibility shim is owed. Any output-shifting change would bump the determinism family version with a release-notes line (none is expected, since Phase 2 changes no logical value).
- **Parity oracle permanent.** The pandas full-frame path stays the parity oracle and the reroute target when the extension is absent; Phase 2 promises no full native cutover.

---

## 8. Self-review

- **Scope held to Decision 4:** `passthrough`, `redact`, `truncate`, keyed `hash`, and the Rust `KeyedDerivationKernel`. Native FPE and every other strategy, provider, and global operation are deferred to Part 2 and named as out of scope.
- **Gate frozen before implementation:** W2 workload, method, and targets are fixed in §2; the correctness and route-proof criteria are hard; the speed and RSS numbers are frozen from a MEASURED oracle baseline in Task 2.0 before any kernel (moved ahead of implementation per the Codex plan-gate), which the author freezes under Cam's 2026-08-29 overnight authorization and Cam reviews with the built work.
- **Sequenced by dependency:** measure-and-freeze the oracle baseline (Task 2.0), then the Rust build scaffold and pure-Rust kernel, then the loader, then the three non-keyed kernels, then keyed hash, then the config-aware eligibility fix, then route integration and the gate. Pure-Rust tasks are separated from Python-orchestration tasks.
- **Determinism:** all four strategies are row-local and partition-safe; keyed hash reproduces the exact shipped `mask.source_keyed_hmac` draw site; no global or `partitionable=False` site is in scope.
- **Carry-forwards tracked:** the Phase 0 FPE checksum-priority branch and native-substrate placeholder are inherited and unresolved here; the Phase 1 config-blind eligibility gap is fixed as a hard precondition.
- **Rust build recommended (Option B, Cam 2026-08-29):** a separate `decoy-engine-native` companion package (maturin + PyO3, Arrow C Data Interface bridge); the core stays pure-Python hatchling with an optional `native` extra and a Rust-free install; fail-closed when the companion is absent or ABI-incompatible is a preflight reroute to the oracle, never a mid-stream fallback and never the reference kernel in production.
- **This plan defines its acceptance tests and failure modes per task up front;** no later contributor may weaken them.
