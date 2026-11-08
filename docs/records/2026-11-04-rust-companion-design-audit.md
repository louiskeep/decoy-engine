Status: record

# Rust companion design audit: cross-cutting concerns

Date: 2026-11-04. Pinned: decoy-engine `ca12e85a` (`claude/busy-franklin-mn1qhc`, equal to main at audit time). Scope: every production file in `decoy-engine-native/src/` (about 3,900 non-test lines over 10 modules) and the Python loaders in `src/decoy_engine/execution/native/` that call them. Method: read-only code review; no build, no runs, no code changed. Line references are relative to `decoy-engine-native/src/` unless they name another path.

Disposition: **V2.** No finding changes a masked value, leaks a value, or lets a run succeed silently with wrong output. Every failure path found still fails closed. The findings are logged in the V2 corrective backlog (`docs/handoffs/V2-CORRECTIVE-BACKLOG.md` on platform `handoffs/queue`, section "Added 2026-11-04"). Class tags follow that log's ten defect classes.

## Summary

The security core is sound. `unsafe` is limited to eight sites, and the ones that matter carry a `// SAFETY:` justification. The mask key is checked before any output. Panics on the Arrow entry points are caught and coded. HKDF and HMAC are checked against RFC 5869 and RFC 4231 vectors, and FF1 against the NIST SP 800-38G samples. Shared known-answer test (KAT) files pin parity with the Python oracle.

The weaknesses are in how the crate is put together:

- The kernels added later (FPE, text_redact, text_mask) do not follow the contracts the first kernels set: load gate, panic policy, Arrow boundary, thread budget.
- Shared plumbing is copied instead of factored out.
- Errors reach Python as plain `code: detail` strings, and one code covers several unrelated failures.

Class 9 (refactor drift) has 7 entries across 5 files. Under the backlog's class-review rule that makes it a candidate for one construction: a single kernel-entry scaffold (item R6) and a single coded error type (items R3 and R5).

## Findings

### High: cross-cutting contracts that have drifted

**R1. Loaders don't share one compatibility gate.** Class 10.
- The hash, index, FPE and group_key loaders (`_crypto_ext.py`, `_index_ext.py`, `_fpe_ext.py`, `_group_key_ext.py`) each check the ABI tag and run a KAT self-test at load.
- `_text_redact_kernel.py:109` probes for symbols with `getattr` and compares its own `CATALOG_VERSION`. It has no ABI check and no self-test.
- `_text_mask_native_splice.py:59` checks only that two symbols exist. It returns `None` on any import failure and does no version check.
- The ABI tag (`lib.rs:42`, `decoy-native-abi-3`) was not bumped when the 10 text functions were added, so it no longer describes what the module exports.

Fix: export one capability manifest from `_kernel` (kernel name to version), and route every loader through one gate that checks the manifest and runs a self-test.

**R2. Panic policy is split.** Class 4.
- The four Arrow entry points wrap their work in `catch_unwind` and map a panic to a coded `internal_panic` `ValueError` (`arrow_ffi.rs:316, 401, 485, 602`).
- The ten text entry points (`text_redact.rs:783-891`, `text_mask.rs:153, 198`) have no wrapper. A panic from `expect` or `unreachable!` (for example `text_redact.rs:120, 131`) reaches Python as PyO3's `PanicException`. That is a `BaseException`, so `except Exception` doesn't catch it, and it carries no code.
- The comment at `text_redact.rs:124-126` says the panic is "caught at the PyO3 boundary". That overstates what happens.

The run still fails, so nothing is lost silently. But a text-kernel bug escapes the coded-error contract every other kernel follows.

**R3. Errors are plain strings, and one code covers several failures.** Class 4.
- Rust raises `ValueError("code: detail")`, and Python splits it with `str(exc).partition(": ")` (`_crypto_ext.py:368`). Code strings are literals in five Rust error types and several Python modules, with no shared registry.
- `CanonError::unsupported` (`canonicalize.rs:58`) produces `mixed_object_not_native` for four different failures:
  - unadmitted Arrow types (the intended use);
  - out-of-range timestamps (`canonicalize.rs:151`);
  - arrow-rs validation failures on malformed imported buffers (`ffi_import.rs:150`);
  - internal output-assembly failures (`batch.rs:313`).
- Python maps all four to `NATIVE_TYPE_NOT_ADMITTED` (`_crypto_ext.py:372`). A kernel bug or corrupt input is therefore reported as a type-admission refusal.

Fix: add distinct `native_input_malformed` and `native_internal` codes, and generate the code list from one Rust enum that Python reads at load.

**R4. Test fault-injection hooks ship in the release wheel.** Class V2-class (release hygiene).
- `DECOY_ENGINE_NATIVE_FORCE_PANIC_IN_DETACH` is checked once per call (`arrow_ffi.rs:244, 345, 428`).
- `DECOY_ENGINE_NATIVE_FORCE_PANIC_IN_WORKER` is checked once per row range (`batch.rs:390, 526, 585`).
- Setting either variable in a production environment makes masking fail on demand.
- The FPE path has neither hook, so its panic path is untested.

Fix: put the hooks behind a `fault-injection` cargo feature that only test builds turn on, and add the hook to FPE.

### Medium: duplicated code

**R5. Five error types, two of them identical.** Class 9.
- `BatchError` (`batch.rs:25`) and `KernelError` (`ffi_import.rs:20`) match variant for variant, with copy-pasted `code()` and `detail()` strings.
- `FpeKernelError` (`fpe.rs:68`) is a third copy.
- The text modules build `PyValueError` inline.
- `DeriveError` is also used for thread-pool build and env-var parse failures (`threads.rs:67, 263`), which have nothing to do with derivation.

`ffi_import` does not depend on PyO3, so one `KernelError` with a `Code` enum can replace all three enums.

**R6. Entry-point boilerplate is repeated four times in `arrow_ffi.rs`.** Class 9.
Each entry point repeats the same steps:
- the mask-key check, twice per call (`:208/298`, `:331/381`, `:416/468`, `:513/581`), and a third time inside `batch.rs:202, 443, 648` and `fpe.rs:385`;
- `available_parallelism` plus budget resolution (`:215, 334, 419, 516`);
- import with the GIL held;
- `detach` with the panic hook;
- export;
- `catch_unwind` with the same message.

FPE has already drifted: it maps budget errors by hand at `:520` and lacks the hook. One `run_arrow_kernel(py, values, mask_key, native_threads, f)` helper removes about 250 lines and stops further drift.

**R7. The batch loops are near-copies, and FPE uses a different design.** Class 9.
- `derive_array` (`batch.rs:194`), `derive_hex_raw_array` (`:435`) and `derive_index_array_typed` (`:640`) each re-implement:
  - the key guard and the admitted-type check;
  - the null-bitmap copy and the all-null shortcut;
  - offset building.
- `RangeTask` and `IndexRangeTask` are the same struct over `u8` and `u64`.
- FPE:
  - splits work by row count (`fpe.rs:313`) instead of the null-balanced `balanced_row_ranges` (`batch.rs:329`);
  - allocates a `String` per row into `Vec<Option<String>>`, then calls `StringArray::from` (`fpe.rs:460`), where the others write into one preallocated buffer;
  - calls Rayon directly instead of going through `run_range_tasks`.

**R8. Derivation and hex encoding are written twice.** Class 9.
- `derive()` (`derive.rs:145`) and `DeriveContext::new()` (`:202`) repeat the same seed-length and namespace validation.
- `hex_token_into` (`:268`) and `hex_prefix_into` (`:319`) repeat the same hex loop.

Fix: `derive()` should call `DeriveContext::new(..)?.derive_row(..)`.

**R9. The thread budget does not bound the threads that exist.** Classes 3 and 10.
- `NativeThreadBudget` only limits how many tasks one call creates.
- The shared Rayon pool is always sized to the host, up to 1024 threads (`threads.rs:315`). It is built on the first call that uses more than one range.
- So a platform worker with a budget of 2 on a 64-core host still gets a 64-thread pool. The module doc (`threads.rs:12-19`) says the budget bounds aggregate threads; the code doesn't.
- The library also reads its own config from `DECOY_NATIVE_MASK_THREAD_KNEE` and caches any parse error for the life of the process (`threads.rs:58, 104`). Per the "library code doesn't know its callers" rule, the caller should pass that value in.

### Medium: hot path and boundary design

**R10. Per-row work that belongs per batch.** Class V2-class (performance).
`canonicalize_row` (`canonicalize.rs:179`), on every row:
- re-matches `data_type()` and re-downcasts the array;
- returns a fresh `Vec<u8>`;
- builds a new NFC `String` in `encode_utf8` (`:121`), even for text that is already NFC (`unicode_normalization::is_nfc_quick` gives a fast path);
- calls `format!` in `encode_timestamp`.

Resolving a typed accessor once per batch and writing into a reused scratch buffer is the next throughput step for hash, index and group_key. The allocation-bound test (`tests/allocation_bound.rs`) measures peak bytes, not allocation count, so it would not catch this.

**R11. The text kernels sidestep the native boundary.** Class V2-class (design).
- They take and return Python lists, not Arrow.
- They hold the GIL the whole time, run on one thread, and take no `native_threads` budget.
- They are the most CPU-heavy kernels in the crate (regex plus byte scanners per cell), so they would gain most from the Arrow, GIL-release and Rayon path the crypto kernels already use.

**R12. Fuzzing and proptest skip the riskiest code.** Class 8.
- The fuzz targets (`fuzz/fuzz_targets/`) and the ASan/TSan jobs cover `derive_array`, `derive_index` and FFI import only.
- They do not cover:
  - the ~350 lines of hand-written byte scanners in `text_redact.rs:170-505` (ssn, us_zip, street_address), which index by computed position;
  - the FPE wrapper;
  - `derive_hex_raw_array`.
- Proptest (`tests/proptest_batch_invariance.rs`) covers `derive_array` only.

The scanners have a differential corpus against Python (`tests/native/test_c6c_ii_text_redact_rust.py`), but no coverage-guided input.

### Low: hygiene

**R13. Dead check and swallowed error in FPE.** Class 9.
- `key.len() != 32` (`fpe.rs:397`) can never be true, because `derive` returns `[u8; 32]`.
- `FF1::new(..).ok()` (`:427`) discards the construction error, and the row then reports an unencryptable-domain code.

**R14. Key material is not wiped.** Class 1 (admin-only surface; same shape as the A-4 frame-local items).
- `mask_key` is copied into a `Vec<u8>` at each entry point.
- The derived HMAC key lives on in a `Clone`-able `DeriveContext` (`derive.rs:193`) and in its per-row clones.
- The derived FF1 key is not wiped either.
- Nothing is zeroed on drop. The established fix is the `zeroize` crate on the key buffers.

**R15. Comments narrate task history, and some are stale.** Class 10.
- There are 88 references to plan IDs ("Task 1.3", "Phase-0 Codex requirement", "C6a plan §3g"), against the engine guide's "no references to the current task".
- `arrow_ffi.rs:270` and `threads.rs:9` still say "execution stays sequential"; it is parallel now.
- Comments make up 38 to 47% of `derive.rs`, `threads.rs` and `lib.rs`.

**R16. Some modules are over the size target, and no check covers Rust.** Class 9.
- Non-test lines: `text_redact.rs` about 907, `batch.rs` about 708.
- `tests/sentry/test_module_size.py` covers Python only.
- `text_redact.rs` splits naturally into detectors, validators and the PyO3 layer.

**R17. No lint requires a `// SAFETY:` note on `unsafe`.** Class 8.
- `derive.rs:281, 335` (`from_utf8_unchecked` over hex output, which is sound) have no note.
- There is no crate-level `#![deny(unsafe_op_in_unsafe_fn, clippy::undocumented_unsafe_blocks)]` to require one.

**R18. Docs are stale.** Class 10.
- `decoy-engine-native/README.md` and the `lib.rs` module doc describe two kernels.
- `_kernel` exports six kernel families: derive, derive_index, derive_hex_raw, FPE, text_redact and text_mask.

## Suggested order for the V2 corrective pass

1. **One coded error type** with distinct internal and malformed-input codes (R3, R5). This is mostly mechanical and fixes the mislabelled failures.
2. **One loader gate and one panic policy** for all kernels, with the fault hooks behind a feature (R1, R2, R4).
3. **One kernel-entry scaffold and one generic batch scaffold**, with FPE moved onto both (R6, R7, R8, R13).
4. **Thread budget that bounds real threads**, with the knee passed in by the caller (R9).
5. **Performance and boundary work:** per-batch typed canonicalization (R10), text kernels on Arrow (R11), fuzz targets for the text scanners and FPE (R12).
6. **Hygiene:** R14 to R18.
