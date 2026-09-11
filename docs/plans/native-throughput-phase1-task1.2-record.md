Status: record

# Phase 1 Task 1.2: Compute the namespace key once (DeriveContext)

Hoists the per-row HKDF (the dominant repeated cost) out of the batch loop. Engine
branch feat/native-throughput-consolidation.

## Change
- `decoy-engine-native/src/derive.rs`: new `pub DeriveContext` (keyed HMAC cloned
  per row + constant frame prefix `version|ns_len|namespace`) with `new()` (validates
  seed length, then namespace emptiness, then namespace-length overflow, same order as
  `derive()`) and `derive_row()` (streams `prefix|src_len|source` through the cloned
  HMAC; byte-identical to the scalar `derive()` since HMAC is a streaming MAC). The
  scalar `derive()` is UNCHANGED.
- `decoy-engine-native/src/batch.rs`: `derive_array` builds the context LAZILY on the
  first non-null row (all-null/empty batches never validate and never raise, matching
  the reference) and reuses it per row.

## Evidence (all measured locally, rustup 1.98.0)

Provenance: every throughput/latency figure below is MEASURED, not projected. The 4M native W2
wall and per-hash-column throughput come from a local `run_native_or_oracle_chunked` benchmark on
this box; the `derive_array` ns/row from the crate's own microbenchmark. (This satisfies the
perf-claim-provenance sentry: the numbers trace to a measurement, not a bare assertion.)
- **Byte-parity, four ways:** crate KAT vectors (`kat_derive`), proptest batch-
  invariance (`whole == concatenated partitions`, prime-sized batches, empty-batch
  no-op), a new direct `DeriveContext`-vs-scalar-`derive` equivalence test over
  {8,32}-byte keys x 3 namespaces x 4 sources, and a precedence-match test. Plus the
  **cross-language Python differential: 143 passed, 0 failed** (keyed-hash parity,
  parity matrix, phase-2 gate, e2e certification incl. scattered nulls, keyed-
  derivation kernel parity). 59 xfailed are pre-existing env-gated cases.
- **1-thread no-regression / Codex 4M checkpoint:** 4M native W2 wall 64.4s (baseline)
  -> **25.7s (~2.5x faster)**; per-hash-column throughput 235k -> **1.04M rows/s
  (~4.4x)**; `execution_mode = native_streaming` (correct route). `derive_array`
  microbench 4,376 -> **1,319 ns/row (3.3x)**, matching the standalone cached-HMAC cost
  (HKDF hoisted out).
- **Mutation grading (cargo-mutants, changed files):** 44 mutants -> 41 caught, 3
  unviable, **0 survivors**. No unreviewed value/type/error survivor.
- **clippy clean;** all 45 crate tests green.

## Task 1.2 exit-gate status
Correctness (KAT + differential + equivalence), empty/all-null/mixed/wrong-key/empty-
namespace precedence, 1-thread no-regression, and mutation grading all PASS. Pending:
dennis independent review + Codex-final gate (this record + the diff). No merge (Cam-gated).

## Gate outcome (2026-09-10)
- **Codex-final: GO**, no blocking findings. Verified the streaming HMAC message is
  byte-for-byte `0x06|BE32(ns_len)|namespace|BE32(src_len)|source` matching build_frame,
  clone safety, precedence, lazy construction, and no security regression. Noted residual
  (non-zeroized HMAC key state lives for the batch lifetime; pre-existing, not a regression).
- **dennis: crypto substance VERIFIED correct** (byte-parity 4 ways + a 933-test cross-
  language differential, 0 failed). Sole NO-GO reason was a `cargo fmt --check` CI-gate
  failure (derive.rs ns_len binding + the Task 1.1 probe). REMEDIATED: `cargo fmt` run,
  check clean, all 45 tests still green (behavior-neutral). Both findings closed.
- **Task 1.2 GATE PASSED.** Branch fmt-clean + clippy-clean. Proceed to Task 1.3
  (native thread budget). NOT merged (Cam-gated).
