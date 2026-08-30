Status: record

# T1 Rust kernel core: measurement, fixes, and adjudication

- **Plan:** `docs/plans/2026-08-29-native-efficiency-test-plan.md`, batch T1 (section 4) and
  the method in section 3.
- **Scope:** `decoy-engine-native/src/` -- `derive.rs`, `canonicalize.rs`, `batch.rs`,
  `arrow_ffi.rs`, `lib.rs`, plus a new pyo3-free `ffi_import.rs` split out of `arrow_ffi.rs`
  during this batch (see "Production change 2" below).
- **Branch:** `docs/native-efficiency-test-plan`, worktree
  `.claude/worktrees/native-test-plan`.
- **Reused, not re-derived:** the T0 Lane A/Lane B harness
  (`scripts/native-testing/lane_a_rust_coverage_mutants.sh`,
  `lane_b_instrumented_extension.sh`) and its `--exclude-re` PyO3-boundary scoping. Lane A's
  script is updated in place (not forked) to add `ffi_import.rs` to its scope and to add
  `import_array` to the PyO3-only exclude list -- see "Harness update" below.

## Method: measure first

Ran Lane A coverage + a full (not pilot) `cargo-mutants` pass across
`derive.rs`/`canonicalize.rs`/`batch.rs`/the PyO3-free slice of `arrow_ffi.rs` BEFORE writing
any test, per section 3 rule 1. The BEFORE numbers below reproduce T0's own pilot findings
almost exactly (same uncovered lines, same one demonstrated mask-key survivor), confirming the
harness is stable and the T0 carry-forwards were still live at T1's start.

## BEFORE / AFTER: Lane A coverage

| File | Region (before) | Region (after) | Line (before) | Line (after) | Function (before) | Function (after) |
|------|---:|---:|---:|---:|---:|---:|
| derive.rs | 96.08% | 99.18% | 93.49% | 98.96% | 86.36% | **100.00%** |
| canonicalize.rs | 92.24% | 94.64% | 94.35% | 97.84% | 87.50% | 96.15% |
| batch.rs | 95.69% | 97.24% | 91.92% | 96.26% | 91.67% | 92.31% |
| ffi_import.rs (new; `import_ffi` lived in `arrow_ffi.rs` before) | -- | 95.19% | -- | 92.00% | -- | 83.33% |
| arrow_ffi.rs | 49.44% | 0.00%* | 45.82% | 0.00%* | 36.84% | 0.00%* |
| lib.rs | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |

\* `arrow_ffi.rs` reads 0% under Lane A AFTER because `import_ffi` (the one function Lane A
could exercise there) moved to `ffi_import.rs`; everything left in `arrow_ffi.rs` is now
genuinely PyO3-bound and graded by Lane B (see below), which is the intended split, not a
regression.

## BEFORE / AFTER: Lane B coverage (arrow_ffi.rs / lib.rs / ffi_import.rs)

Lane B still runs the same three Python test files
(`test_native_ext_abi.py`, `test_keyed_derivation_kernel_parity.py`, `test_crypto_ext_loader.py`)
against an instrumented build; 74 passed, 1 skipped, unchanged from T0.

| File | Region (before) | Region (after) |
|------|---:|---:|
| arrow_ffi.rs | 79.44% | 81.44% |
| lib.rs | 80.00% (100% lines) | 80.00% (100% lines) |
| ffi_import.rs | -- | 72.22% |

`arrow_ffi.rs`'s tuple-arity check (`import_array`'s `if tuple.len() != 2`) executes on every
real Lane B call (a genuine 2-tuple every time), which is the empirical basis for the
tool-excluded adjudication below -- confirmed by exercise, not only by reasoning.

## BEFORE / AFTER: mutation (Lane A, full pass, not pilot)

Recipe: `scripts/native-testing/lane_a_rust_coverage_mutants.sh mutants --timeout 180 -j 4`.

**BEFORE** (101 mutants, original four-file scope): 8 missed, 77 caught, 16 unviable.

**AFTER** (101 mutants, corrected five-file scope including `ffi_import.rs`, `import_array`
added to the PyO3-only exclude list): **0 missed, 86 caught, 15 unviable.**

| File | Total | Killed | Missed (before fix) | Missed (after fix) | Unviable |
|------|---:|---:|---:|---:|---:|
| derive.rs | 31 | 31 | 1 | 0 | 0 |
| canonicalize.rs | 50 | 43 | 1 | 0 | 7 |
| batch.rs | 11 | 8 | 1 | 0 | 3 |
| ffi_import.rs | 8 | 4 | n/a (didn't exist yet) | 0 | 4 |
| arrow_ffi.rs | 1 | 0 | n/a | 0 | 1 |

## Harness update

`import_ffi` moved out of `arrow_ffi.rs` into a new, always-compiled (no `extension-module`
gate) `ffi_import.rs` module, so the FFI-metadata fuzz target (required by this batch) can
link it without pulling in pyo3. This makes `ffi_import.rs` a Lane A file and shrinks
`arrow_ffi.rs` to purely PyO3-bound code. `lane_a_rust_coverage_mutants.sh` is updated to add
`-f 'src/ffi_import.rs'` to its scoping and `import_array` to `PYO3_ONLY_FNS` (that function
stayed behind in `arrow_ffi.rs` and still needs a live Python object to call meaningfully). T2-T6
should read the updated script, not the original T0 recipe text.

## Production change 1 (required by the plan): the checked-length helper

`derive.rs`'s `build_frame` extracted a checked-length helper:

```rust
fn checked_frame_length(len: usize, code: &'static str, what: &str) -> Result<u32, DeriveError> {
    u32::try_from(len).map_err(|_| DeriveError::new(code, format!("{what} exceeds u32 bytes")))
}
```

`build_frame` calls it twice (namespace length, then canonical source length) with the exact
same codes and messages it used inline before. Three new unit tests drive it directly with a
synthetic `usize`: the realistic-length accept case, the exact `u32::MAX` boundary accept case,
and `(u32::MAX as usize) + 1` producing the caller's overflow code. `cargo test` (both the KAT
fixture test and the allocation-bound test) passed unchanged before and after this change,
confirming the derivation output is byte-identical -- this was a refactor plus tests, not a
behavior change. Two lines inside `build_frame` itself (the `?` early-return columns for its two
`checked_frame_length` calls) remain line-coverage-negative, because `build_frame` itself is
never invoked with an actual >4 GiB input (deliberately, to avoid allocating that much in a
test); cargo-mutants generates zero viable mutants at those two lines (confirmed in the full
mutants.json), so the zero-unadjudicated-survivor bar is met without needing to allocate 4 GiB
anywhere.

## Production change 2 (required by the plan): the bounded FFI-metadata fuzz target

Building the target required `import_ffi` (and the `KernelError` type it returns) to compile
and link without pyo3, since a standalone libFuzzer binary cannot link libpython (see
Cargo.toml's own comment on why `extension-module` exists). `import_ffi` and `KernelError` had
zero pyo3 dependency already; only their placement inside a `#[cfg(feature = "extension-module")]`
module blocked this. Moved both (with their existing tests, byte-for-byte) into `ffi_import.rs`,
made that module unconditional in `lib.rs`, and moved `arrow-array`'s/`arrow-data`'s/
`arrow-schema`'s `ffi`/`force_validate` cargo features from the `extension-module` feature list
to unconditional dependency features (`ffi_import.rs` needs the C Data Interface types
regardless of the pyo3 feature). `arrow_ffi.rs` now imports `import_ffi`/`KernelError` from the
new module; its own remaining functions (`import_array`, `export_string_array`,
`extract_truncate`, `derive_batch_checked`, `derive_batch`, `register`) are unchanged. Verified
both feature configurations (`cargo test` and `cargo test --no-default-features`) build, clippy
clean under both, and the KAT/allocation-bound/proptest suites are unaffected.

## New tests and evidence each is non-vacuous

- **Mask-key `Some(&[])` gap** (`batch.rs`, T0 carry-forward): `derive_array(&array, Some(&[]),
  "ns", None)` now asserts `BatchError::MaskKeyRequired`. Before this test, cargo-mutants'
  `replace match guard !k.is_empty() with true` mutant at `batch.rs:79` survived; after, the
  full mutation pass reports it caught (0 missed in `batch.rs`).
- **`checked_frame_length` accept/overflow tests** (`derive.rs`): see Production change 1.
  Non-vacuous by construction -- the overflow test would fail if the helper stopped checking
  the length at all (e.g. an unconditional `Ok(len as u32)`), and the accept test would fail if
  it started rejecting valid lengths.
- **`DeriveError`/`CanonError` Display tests**: `err.to_string() == "code: detail"`. Before these
  tests, cargo-mutants' `replace fmt with Ok(Default::default())` survived at both
  `derive.rs:66` and `canonicalize.rs:63` (an empty-string `Display` output would still pass
  every other test, since nothing else calls `.to_string()`/`{}` on these types). Both now
  caught.
- **`encode_timestamp` out-of-range tick** (`canonicalize.rs`): `TimeUnit::Second` admits any
  `i64`; `i64::MAX` seconds is far past chrono's representable calendar range, so
  `encode_timestamp(TimeUnit::Second, i64::MAX)` must return `Err` with code
  `mixed_object_not_native`. This closes a genuine coverage gap (lines 148-149 were previously
  unexercised); no cargo-mutants operator existed at that specific closure either way (same
  category as the length-overflow branches), so this test is a coverage improvement, not a
  mutation-driven one -- added anyway since it costs one line and needs no synthetic helper
  (the real `i64` domain already reaches it).
- **`KernelError::code`/`detail` per-variant test** (`ffi_import.rs`): before this test,
  cargo-mutants' whole-function replacements (`code -> ""`, `code -> "xyzzy"`, `detail ->
  "xyzzy".into()`, `detail -> String::new()`) all survived, because the only existing test
  touching `KernelError` (the sentinel-redaction test) never asserted the CONTENT of `.code()`/
  `.detail()`, only their absence of embedded secrets. The new test builds a real instance of
  every variant (via the actual public constructors, not hand-rolled data for `DeriveError`,
  whose constructor is private outside its module) and asserts the exact code/detail. All four
  mutants now caught.
- **`python_slice_stop` bounds + monotonicity proptests** (`derive.rs`): sweeps the full `isize`
  domain (not just the boundary examples already pinned), asserting the result always stays in
  `[0, HEX_LEN]` and is monotonic in the input. This is the Rust-layer half of the plan's
  "truncate edge" ask; the "huge" (2^63, 10^100) and "hostile" (int-subclass, custom
  `__index__`) cases are inherently PyO3-object-level concepts that cannot be expressed as a
  pure-Rust `isize` (they get clamped to `isize::MIN`/`MAX` one layer up, in
  `arrow_ffi::extract_truncate`) and are already covered there by the existing Python parity
  tests (`test_huge_magnitude_truncate_matches_the_live_reference`,
  `test_hostile_index_object_is_refused_by_both_kernels`) -- not duplicated here since that
  would just re-test a value the Rust layer never actually receives.
- **Canonicalizer differential-equality properties** (the plan's other T1 candidate gap):
  MEASURED, not re-added. `generate_kat.py` already generates every admitted Arrow type
  (utf8/large_utf8/bool/all eight int widths signed+unsigned/timestamp at all four units, both
  UTC and a non-UTC tz) plus an explicit NFC/NFD equivalence pair and an explicit
  framing-ambiguity pair, computed against the LIVE Python `canonicalize_derive_source` (never a
  Rust-produced golden), and `kat_derive.rs` asserts differential equality per row against that
  fixture. The full mutation pass confirms this is not merely present but effective: 43/50
  `canonicalize.rs` mutants caught, covering `encode_int`, `twos_complement_be`, `encode_utf8`,
  `encode_bool`, `encode_timestamp`, and `canonicalize_row`'s own dispatch. No new differential
  test was added for this candidate gap; it was already met before this batch started.

## Bounded FFI-metadata fuzz target

`decoy-engine-native/fuzz/fuzz_targets/ffi_metadata.rs` (new `[[bin]]` in `fuzz/Cargo.toml`).
Builds one real, over-allocated `Int64Array` (4,096 elements, real nulls at every 7th index) and
mutates ONLY its exported `FFI_ArrowArray`'s `length`, `offset`, and one buffer pointer
(nulled), plus the paired schema (a genuine schema swap to a real boolean array's schema,
buffer-count-compatible), calling `ffi_import::import_ffi` directly. Every mutated value is
computed to stay inside the real 4,096-element allocation (`length`/`offset` reduced modulo the
real size; `null_count` computed as the TRUE count for the chosen slice, not fuzzed; the nulled
buffer index is 0 or 1, both real, existing pointer slots) -- the safety contract from the plan
("an owned array alone is not enough... a larger declared length/offset/buffer-count would read
out of bounds").

**Two real findings during setup, both adjudicated, neither a new safety gap:**

1. An earlier draft additionally fuzzed `n_buffers` (bounded to 0-2, never above the real count)
   and a raw arbitrary `null_count`. Both independently reproduced the same underlying arrow-rs
   behavior: `from_ffi` -> `consume` -> `ArrayData::new_unchecked` runs `validate_data()`
   internally and `.unwrap()`s the result rather than returning the clean `Result::Err`
   `force_validate` otherwise gives, so ANY metadata that fails validation panics instead of
   erroring cleanly. Both panics occurred INSIDE `import_ffi`'s own `catch_unwind` (confirmed by
   the crash backtraces), so the real, `panic = "unwind"`, maturin-built extension catches them
   and reports a clean `KernelError::ProtocolError` -- this is exactly `catch_unwind`'s job, and
   it works. The reason each one still aborted the FUZZ BINARY is `cargo-fuzz`'s own hardcoded
   `panic = "abort"` build convention (not overridable from this crate's Cargo profile, confirmed
   by trying), under which `catch_unwind` can never catch anything, since abort never unwinds
   regardless of where it sits in the call stack. This is a genuine confirmation that
   `import_ffi`'s `catch_unwind` is load-bearing, not merely defensive, for two DIFFERENT
   validation-failure shapes (buffer-count mismatch, wrong null_count) -- not a new gap.
2. Since re-fuzzing either dimension would only rediscover the same adjudicated panic on nearly
   every run (arrow-rs validates both strictly), the final target pins `n_buffers` at its real
   value and computes `null_count` exactly for the chosen slice, leaving `length`/`offset`/
   buffer-nulling/schema-type free to vary -- the dimensions that can plausibly reveal a genuine
   out-of-bounds read rather than a plain (and separately, already-verified-caught) validation
   panic.

**Run:** `cargo +nightly fuzz run ffi_metadata -- -max_total_time=180` (ASan default-enabled
per `cargo fuzz build`'s own help text). **791,291 executions in 181 seconds, zero crashes, zero
ASan reports.** Both `derive_array` (existing) and `ffi_metadata` (new) fuzz targets build
cleanly under `cargo +nightly fuzz build`.

## Five-field adjudication: derive.rs and canonicalize.rs (the zero-unadjudicated-survivor bar)

Fields: (a) region coverage, (b) killed, (c) equivalent, (d) unreachable-by-contract,
(e) tool-excluded.

### derive.rs

- (a) 99.18% region / 98.96% line / **100% function**.
- (b) 31/31 mutants killed. Zero missed.
- (c) none.
- (d) none.
- (e) none.
- Residual line-coverage gap (lines 112, 117, the `?` early-return columns for
  `checked_frame_length`'s two call sites inside `build_frame`): no viable mutant exists there
  (confirmed in `mutants.json`); the underlying logic is separately 100%-tested via
  `checked_frame_length`'s own standalone tests. **Zero unadjudicated survivors.**

### canonicalize.rs

- (a) 94.64% region / 97.84% line / 96.15% function.
- (b) 43/50 mutants killed (was 42/50 before the Display-impl fix; the survivor is now killed).
- (c) 7 unviable, all classified equivalent: cargo-mutants' `Default::default()`-substitution
  operator cannot construct a body for these return types (`CanonError`, a leaked `&T`/
  `&GenericStringArray<O>`, `CanonError` again), since none implement `Default` -- no
  alternative body compiles, so no semantic mutant exists to adjudicate as a survivor at these
  four sites (`CanonError::unsupported` line 57; `downcast` line 225; `typed_string` line 233,
  four variant attempts; `map_arrow_error` line 279). Each site's real behavior is independently
  proven via its callers: `canonicalize_row`'s own whole-function-replacement mutants (caught),
  and the KAT fixture's per-type differential checks.
- (d) `downcast`'s type-mismatch error branch (lines 226-227): reachable only if an arrow-rs
  `ArrayRef`'s `data_type()` disagreed with its own physical layout, an invariant `make_array`
  (arrow-rs's own array constructor) enforces for every array this crate can obtain, including
  through the FFI import path (`force_validate`'s `validate_data()` already rejects any capsule
  whose declared type and buffers disagree, before `make_array` ever wraps it). No input this
  crate's callers can construct reaches this branch. Genuinely unreachable-by-contract, not
  merely untested.
- (e) none.
- **Zero unadjudicated survivors.**

## batch.rs and ffi_import.rs (supporting crypto surfaces, same bar applied)

### batch.rs

- (a) 97.24% region / 96.26% line / 92.31% function.
- (b) 8/11 killed (was 7/11 before the mask-key fix).
- (c) 3 unviable, same "no Default impl" reasoning as above (`From<CanonError>`,
  `From<DeriveError>`, `derive_array`'s whole-function replacement) -- each proven independently
  via its real callers' mutants (all caught).
- (d)/(e): none.
- **Zero unadjudicated survivors.**

### ffi_import.rs

- (a) 95.19% region / 92.00% line / 83.33% function (Lane A); 72.22% region (Lane B, the three
  ABI/parity/loader Python files only).
- (b) 4/8 killed.
- (c) 4 unviable, same reasoning (`From<CanonError>`, `From<DeriveError>`,
  `From<BatchError>`, `import_ffi`'s whole-function replacement) -- proven via
  `import_ffi_rejects_unadmitted_type_cleanly` / `import_ffi_rejects_null_data_buffer_cleanly` /
  `import_ffi_never_panics_on_implausible_length`, and, for the exact malformed shapes this
  batch's fuzz target explored, by 791,291 crash-free executions under ASan.
- (d)/(e): none.
- **Zero unadjudicated survivors.**

### arrow_ffi.rs (PyO3 boundary; graded by Lane B / tool-excluded per the plan, not by Lane A)

- Lane A: 1 mutant total (`to_py_err -> PyErr` via `Default::default()`), unviable (`PyErr` has
  no `Default`) -- not a real survivor, same reasoning as above; ALSO doubly tool-excluded on
  its own terms, since `to_py_err` needs a live interpreter to construct a `PyErr` regardless.
- The one mutant EXCLUDED from Lane A's scope by name (`import_array`'s `if tuple.len() != 2`,
  i.e. the `!=`/`==` operator mutant): (e) tool-excluded. `import_array` takes a
  `Bound<'_, PyAny>`, which needs a live GIL-held interpreter to construct; this crate's
  `extension-module` feature deliberately omits linking libpython (see Cargo.toml), so no
  `cargo test` build of this crate -- with or without that feature -- can construct one, making
  this genuinely impossible to grade in Lane A, not merely inconvenient. Empirically, Lane B's
  coverage report shows this exact line executing on every real call (all three Python test
  files use well-formed 2-tuples), which is consistent with the mutant being caught there: a
  flipped `!=`-to-`==` would reject every well-formed input, which every passing parity/ABI test
  would immediately catch. (Not independently re-verified by actually building a mutated `.so`
  and rerunning Lane B -- noted as reasoning-plus-coverage-evidence, not a literal Lane B
  mutation run, to keep the claim honest about what was and wasn't executed.)

## Constraint checks

- `cargo fmt --check`: clean (both feature configurations share one formatting).
- `cargo clippy --all-targets -- -D warnings`: clean.
- `cargo clippy --all-targets --no-default-features -- -D warnings`: clean.
- `cargo test`: 43 tests passed (36 unit + 7 integration), 0 failed, both feature
  configurations.
- `cargo +nightly fuzz build derive_array` / `ffi_metadata`: both build clean.
- Python `tests/native/`: 381 passed, 1 skipped, 0 failed, run against a fresh `cargo build
  --release` of this worktree staged into a private scratch `PYTHONPATH` directory (the shared
  venv's `.pth` points at a different stacked worktree; per the T0 harness's own established
  method, this run never touched that shared file -- verified by not writing to it at all).
- Only production change beyond the plan-mandated checked-length extraction: the `ffi_import.rs`
  split, required to link the mandated fuzz target without pyo3. No behavior changed on any
  existing call path; every existing test (KAT, allocation-bound, proptest, the moved
  `arrow_ffi.rs` unit tests) passes unchanged in both feature configurations.
