Status: plan

# Engine-efficiency native surface: thorough testing plan

The Phase 2 native masking work (Tasks 2.1-2.7) is built and double-gated, stacked on the
Phase 0/1 native substrate. This plan hardens the whole native surface to a durable bar before
it becomes load-bearing, measuring where coverage and mutation actually stand rather than adding
tests by count. It runs as gated batches, each ending in a dennis + Codex gate. (Revised after
the Codex plan cross-review: a tooling pilot precedes the measurement batches, the 100% bar has
an explicit denominator, and the cross-language harness is defined.)

## 1. Scope

The surface under test is every unit shipped in Tasks 2.1-2.7, in two languages:

- **Rust kernel crate** (`decoy-engine-native/src/`): `derive.rs` (HKDF-SHA256 then HMAC-SHA256),
  `canonicalize.rs` (typed source canonicalization + the admitted-type boundary), `batch.rs`
  (the PyO3-free row loop), `arrow_ffi.rs` (Arrow C Data Interface import + truncate extraction +
  the PyO3 exception/panic boundary), `lib.rs` (ABI constant, `abi_version`, module registration,
  exported `derive_batch`).
- **Python native package** (`src/decoy_engine/execution/native/`): `_crypto_ext.py` KEYED-LOADER
  surface only (loader, ABI check, load-time self-test, `_translate_compiled_kernel_error`, the
  keyed reference kernel) -- `_ReferenceFpe` and the FPE surface are EXCLUDED (Part 2);
  `_kernels_scalar.py`, `_kernels_keyed.py`, `_plan.py` + `_requirements.py`, `_dispatch.py`.

Out of scope: the pandas oracle (unchanged, the parity reference), FPE (Part 2, including
`_ReferenceFpe` inside `_crypto_ext.py`), providers/generation (Phase 3 / Part 2), and any
strategy outside {passthrough, redact, truncate, hash}. `_capabilities.py` and other Phase 0/1
dependencies are touched but not owned here; they are exercised transitively, not graded.

## 2. What already exists (do not duplicate)

The build tasks landed real tests; this plan extends, it does not restart. Rust:
`kat_derive.rs`, `proptest_batch_invariance.rs`, `allocation_bound.rs`, a cargo-fuzz target over
`batch::derive_array` (~2M+ execs, 0 crashes), ASan + TSan clean. Python:
`test_crypto_ext_loader.py` (loader + self-test + the full malformed-companion matrix, both
companion states), `test_kernels_scalar.py` (byte + output-type parity, batch-stability across
value/all-null/empty/chunked), `test_kernels_keyed.py` + `test_keyed_hash_parity.py` (in-process
+ cross-process, 3 mutation proofs), `test_native_plan_config_aware.py` (eligibility gates +
exhaustive four-strategy admitted-set sentry + compiler agreement), `test_native_dispatch.py`
(route-proof, preflight reroute, FK reroute parent/child/composite/single), `test_phase2_gate.py`
(frozen W2 correctness/route/perf gate).

Because so much already exists, every batch below states CANDIDATE gaps only. A test is added
when, and only when, the measured coverage or a surviving mutant demonstrates the gap. Nothing is
added by count.

## 3. Method (binding, per the repo testing rules)

1. **Measure before adding.** For every unit, capture branch coverage AND a mutation result on
   the CHANGED code, then fill only demonstrated gaps.
2. **The bar has an explicit denominator.** "Crypto/RI 100%" does NOT mean a raw mutation
   percentage. Each graded unit reports five fields: (a) branch coverage %, (b) killed semantic
   mutants, (c) equivalent mutants (each with a one-line justification), (d) unreachable-by-
   contract mutants (branches no admitted input can reach under the enforced type limits), (e)
   tool-excluded code (the PyO3/panic boundary graded by the extension lane or seeded faults, not
   by a cargo-mutants number). The crypto/RI bar is: zero UNADJUDICATED semantic survivors. Every
   other survivor is in (c), (d), or (e) with a reason. The length-overflow branches in
   `derive.rs`/`build_frame` are NOT field (d): `derive()` accepts arbitrary slices and strings
   and the overflow error is a defined behavior, so "resource-impractical to hit by allocation" is
   not "contract-unreachable". These MUST be tested by extracting a checked-length helper that
   takes a synthetic size and asserting both the accept and the overflow-error branch; they may
   not be adjudicated unreachable.
3. **Bars by surface criticality:**
   - Crypto derivation + canonicalizer + loader self-test: the zero-unadjudicated-survivor bar.
   - Route-integrity + eligibility (`_plan`, `_requirements`, `_dispatch`): kill every mutant that
     changes an admission verdict, a route tag, a fail-closed path, or the FK reroute.
   - Scalar/keyed kernels + loader mechanics: kill every mutant that changes an output value, an
     output Arrow type, or a raised exception type/code.
   Representational-only survivors (message text, log detail) are adjudicated, not chased.
4. **Property over example** where the space is large. Note: canonicalization is deliberately
   MANY-TO-ONE (NFC/NFD and equivalent tz representations converge; there is no inverse), so the
   property is DIFFERENTIAL EQUALITY against `canonicalize_derive_source` over every admitted
   Arrow type, plus explicit equivalence (NFC==NFD) and boundary (int widths, tz units) cases --
   never a round-trip/injectivity assertion.
5. **Parity oracle discipline:** the pure-Python reference and the pandas oracle are the only
   graders of LOGICAL output; never grade a kernel against a golden it produced (the 2.5 harness
   bug). A wrapper-forwarding test MAY compare against the unchanged handler/kernel it deliberately
   delegates to (that is the point of the wrapper), which is not self-grading.
6. **Mutation-substrate caveats:** mutmut's decorated-class and trampoline-wrapped frame/signature
   bodies are not reliably mutated; the repo's existing mutmut config also records FALSE TIMEOUTS
   on pandas/Arrow execution substrates. Must-grade logic in those shapes is restated as a free
   function or graded by a targeted differential test, not trusted to the mutation score.

## 4. Batches

Each batch: measure -> fill demonstrated gaps -> re-measure to the bar -> dennis + Codex gate ->
hold.

- **T0 Tooling pilot (precondition, before any measurement).** Stand up and prove the
  cross-language harness on a tiny slice, so later batches' numbers are reproducible and honest:
  - Rust TWO LANES. Lane A (Rust-only): `cargo llvm-cov` + a Rust mutation pass (cargo-mutants)
    over `derive`, `canonicalize`, `batch`, and the PyO3-free `import_ffi`. Lane B (instrumented
    extension): build the extension with LLVM coverage and run the Python ABI/parity/loader tests
    (`test_native_ext_abi.py`, `test_keyed_derivation_kernel_parity.py`, the loader tests) against
    THAT build, so `arrow_ffi.rs`'s capsule/exception/panic paths are actually exercised. The
    thin PyO3 bindings are graded by seeded differential faults or a per-mutant extension-build,
    NOT by a bare cargo-mutants score.
  - Python mutation pilot: keep the package-root `source_paths` but set a per-batch `only_mutate`
    plus focused test selection; produce one KILLED and one SURVIVING mutant, and REPRODUCE the
    known false-timeout, then CLASSIFY it by rerunning that same mutant under a standalone pytest
    (recording whether it is a true survivor or a false timeout). The SYSTEMATIC mutation method
    for these Arrow/pandas-substrate units is a
    standalone-pytest-per-mutant runner (mandatory: it is the only method that enumerates the
    survivor denominator honestly under the timeout pathology). Seeded HAND mutations are allowed
    ONLY for explicitly tool-excluded constructs (the PyO3 boundary), and only with a documented
    fault inventory -- they prove test sensitivity, they do not establish the exhaustive denominator
    and never substitute for it. Record per-mutant time limits and shard by module/function.
  - Record the exact tool versions and bootstrap commands (cargo-llvm-cov, cargo-mutants, the
    Python coverage + mutmut already declared in `pyproject.toml`) so the run reproduces.
  Gate T0 on: the harness demonstrably distinguishes a killed from a surviving mutant in BOTH
  languages, AND the known Python false-timeout is reproduced and correctly classified via a
  standalone rerun, all with recorded commands.
- **T1 Rust kernel core.** Lane A over `derive`/`canonicalize`/`batch`/`import_ffi`; Lane B for
  `arrow_ffi` + `lib.rs`. Candidate gaps: canonicalizer differential-equality properties across
  every admitted type incl int-width and tz-unit boundaries, NFC/NFD and framing-ambiguity pairs;
  negative/huge/hostile truncate inputs as permanent proptest cases; a BOUNDED FFI-metadata fuzz
  target. The fuzz safety contract: start from an OWNED, OVER-ALLOCATED valid array and mutate the
  declared metadata (length/offset/buffer-count/null-count/type) ONLY within the over-allocated
  backing storage, so `from_ffi` can never address past the real allocation -- an owned array alone
  is not enough, since a larger declared length/offset/buffer-count would read out of bounds, and
  `catch_unwind` does NOT catch invalid memory access. Run this target under ASan. Bar: zero
  unadjudicated survivors on derive + canonicalize.
- **T2 Loader + keyed reference (FPE excluded).** Restrict to the keyed-loader symbols in
  `_crypto_ext.py`. Candidate gaps beyond the existing malformed-companion matrix: exception
  type + code parity for every keyed-reference rejection; the self-test's type/value branches.
  Bar: zero unadjudicated survivors on the self-test and the keyed reference derivation.
- **T3 Scalar + keyed kernels.** Candidate gaps: property-based byte + output-type parity vs the
  handlers/reference across the full dtype matrix and all batch shapes; the characterized
  pandas-artifact divergences pinned as expectations. Bar: kill every value/type/exception mutant.
- **T4 Eligibility.** Candidate gaps: a property sweep over strategy x dtype x config proving the
  admitted set stays exactly the four kernel-backed strategies; compiler-vs-eligibility agreement
  as a property; input-type correspondence to the Rust admitted set across every width/label.
  Bar: kill every admission-verdict mutant.
- **T5 Dispatch + route integrity.** Candidate gaps: route-tag atomicity, the runtime
  compiled-kernel flag, the FK reroute (composite/single/parent/child, self-referential + chained
  edges) as a differential property vs the oracle's FK gating. Scope-corrected claims: "no partial
  output" holds ONLY for PREFLIGHT-detectable failures (dispatch returns a lazy per-chunk iterator
  after the first chunk, so a later-chunk fault can follow already-consumed output; do not promise
  more without a staging contract). Add SCHEMA-DRIFT cases the current tests miss: a configured
  column MISSING from the first chunk, and a schema/type change AFTER the first chunk -- and fix
  the column-coverage diagnostic, which reports only `actual - covered` and so misses a missing
  configured column (test both sides of the symmetric difference; define subsequent-chunk
  behavior). Bar: kill every route-tag / fail-closed / FK-reroute mutant.
- **T6 End-to-end gate hardening (right-sized).** Small exhaustive correctness/property tests
  (partition invariance already carries the batch-size/order space), ONE medium end-to-end
  mixed admitted/non-admitted-route case, and ONE large perf/RSS certification at EXACTLY 100M
  rows (the product's conservative cap) with defined pass criteria: exact seeded parity vs the
  oracle on a sampled slice, route-proof (every admitted node ran native + the compiled kernel
  executed), wall time <= the frozen non-regression bound extrapolated to 100M, and peak RSS <=
  6.5 GiB and flat (within 1.5x the 1x RSS). Assert the perf/RSS harness stays honest (out-of-band
  generation, lazy read). Determinism: re-run the NAMED Phase 0 golden sentries
  (`tests/native/test_determinism_goldens.py` and the draw-site inventory tests) and assert no
  fingerprint moves -- not an unbounded "anywhere in the program" claim.

## 5. Acceptance

- Per unit: the five denominator fields recorded (coverage %, killed, equivalent, unreachable-by-
  contract, tool-excluded), with the crypto/RI zero-unadjudicated-survivor bar met and every other
  survivor adjudicated in writing.
- Every property test demonstrably able to fail (a seeded mutation makes it red).
- The parity oracles are the reference/oracle for logical output; no self-grading path exists.
- dennis + Codex GO per batch; nothing weakens an existing gate or a frozen Phase 2 target.

## 6. Sequencing note

This runs against the stacked Phase 2 branches (held). It does not depend on the merge and does
not itself merge; whether it runs before or after Cam merges Phase 2 is Cam's call, since the
surface is frozen at the stacked HEAD. T0 gates the rest: no measurement batch starts until the
harness is proven in both languages.
