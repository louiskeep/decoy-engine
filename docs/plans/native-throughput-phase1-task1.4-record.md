Status: record

# Phase 1 Task 1.4: Release the GIL around the pure-Rust compute

## Change (commit on feat/native-throughput-consolidation)
`decoy-engine-native/src/arrow_ffi.rs::derive_batch_checked`: imports + validates the Arrow
array with the GIL HELD (PyO3/FFI), then runs the PyO3-free `derive_array` inside
`py.detach(...)` (PyO3 0.29 `Python::detach`) with the GIL RELEASED, then reacquires the GIL to
export. The `py.detach` closure captures no Python object (no `py`), runs on the same thread, and
the outer `catch_unwind` in `derive_batch` still covers it (a panic inside becomes the coded
`internal_panic` error). Output is byte-identical; Task 1.5's Rayon parallelism runs inside this
same detached region.

## Evidence (local, rustup 1.98.0 + nightly for TSan)
- **GIL-release PROOF** (acceptance-matrix requirement, not just concurrent-call agreement):
  `tests/native/test_gil_release.py::test_native_compute_releases_gil_for_wallclock_parallelism`
  measures wall-clock parallel speedup. An earlier interior-window sentinel proof was DROPPED:
  Codex committed a counterexample where a GIL-holding `PyDLL.usleep(1s)` with
  `sys.setswitchinterval(0.25)` sprinkled activity into the sentinel's interior window and passed
  it, so timestamp/counter sampling is not discriminating. The replacement runs W copies of the
  compute serially then on W threads and takes `speedup = serial/concurrent`: wall-clock overlap
  of a fixed-work callable cannot be manufactured by switch-interval tuning. A pure-Python CPU
  loop (provably GIL-held) runs through the same harness as a live in-test negative control.
  Measured on the 4-core devbox: **control 0.99x, kernel ~3.3x**; Codex's exact `PyDLL.usleep`
  attack measures **1.00x and fails closed** against the committed control. Hardening: effective
  CPU affinity (`sched_getaffinity`) drives the >=2-CPU skip so a `taskset -c 0` run skips rather
  than false-fails; best-of-3 kernel trials + median control blunt shared-runner noise; worker
  exceptions are captured and re-raised so a concurrent-only crash cannot shorten the interval
  into a false pass; a free-threaded (no-GIL) build skips (the control would be invalid there).
- **CI coverage**: `.github/workflows/native-companion.yml` triggers on the whole `tests/native/**`
  tree (was an enumerated file list that silently excluded this file) and runs
  `test_gil_release.py` in the `core-companion-present` job, so the proof actually executes in CI.
- **Concurrent-call correctness**: `test_concurrent_derive_batch_calls_agree` (4 threads deriving
  concurrently, all byte-identical to the reference; no shared mutable state to race).
- **Byte-parity**: keyed-hash + parity-matrix differential (47 passed, 0 failed), unchanged output.
- **Panic**: the `catch_unwind` in `derive_batch` covers the detached region (module doc + the
  panic-safety design); panic + ABI tests pass (10 passed).
- **ThreadSanitizer** (`RUSTFLAGS=-Zsanitizer=thread cargo +nightly test --lib --tests -Zbuild-std`):
  **56 tests pass, 0 data races, exit 0**. (A full `cargo test` under TSan exits 1 ONLY on the
  doctest phase, a known TSan+build-std doctest ABI-mismatch artifact; there are 0 doctests.)
- **fmt + clippy (with pyo3 feature) clean; ruff clean.**

## Exit-gate status
ABI, panic, concurrent-call, and ThreadSanitizer all PASS, plus the GIL-release proof and
byte-parity. Codex final-gate: the original "proof is non-discriminating" BLOCKER is CLOSED (Codex
confirmed the wall-clock-speedup proof discriminates and its own attack fails closed). Codex's
follow-up findings are remediated in the same pass: the CI-coverage BLOCKER (proof never ran in
CI), the affinity-vs-cpu_count skip, shared-runner noise (trials), worker-exception propagation,
and the free-threaded caveat. NOT merged (Cam-gated). Next: Task 1.5 wires the shared pool into
`derive_array`'s row loop inside this detached region (the multi-core speedup).
