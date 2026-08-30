#!/usr/bin/env bash
# Lane A of the native-efficiency test harness (T0 of
# docs/plans/2026-08-29-native-efficiency-test-plan.md; scoping updated in T1
# once import_ffi moved out of arrow_ffi.rs): Rust-only coverage and mutation
# over the PyO3-free core -- derive.rs, canonicalize.rs, batch.rs, and
# ffi_import.rs (import_ffi and the KernelError it returns, extracted from
# arrow_ffi.rs in T1 specifically so this file can compile and link with no
# Python interpreter at all -- see ffi_import.rs's module doc comment).
# Everything here runs under plain `cargo test`; arrow_ffi.rs's remaining
# functions (import_array, export_string_array, extract_truncate,
# derive_batch_checked, derive_batch, register) need a live interpreter and
# are Lane B's job (lane_b_instrumented_extension.sh), not this one.
#
# Usage:
#   scripts/native-testing/lane_a_rust_coverage_mutants.sh coverage
#     -> full-crate branch/region coverage table (the baseline every later
#        batch compares against).
#
#   scripts/native-testing/lane_a_rust_coverage_mutants.sh mutants-pilot
#     -> a short, scoped cargo-mutants run proving the tool distinguishes a
#        killed mutant from a surviving one, and that its --file/--exclude-re
#        scoping keeps it off the PyO3-bound functions. NOT a full mutation
#        pass over all four files -- that is T1's job. Runs in well under a
#        minute.
#
#   scripts/native-testing/lane_a_rust_coverage_mutants.sh mutants -- <args>
#     -> cargo-mutants over derive.rs/canonicalize.rs/batch.rs/ffi_import.rs
#        plus whatever PyO3-free slice remains in arrow_ffi.rs, with the
#        PyO3-bound functions excluded by name. Extra args (e.g. --timeout,
#        -F) are forwarded. This is the scoping recipe T2-T6 should extend,
#        not re-derive.
#
# Requires: `source ~/.cargo/env` first (cargo-llvm-cov 0.9.0, cargo-mutants
# 27.1.0, llvm-tools-preview already installed per the T0 brief).

set -euo pipefail

CRATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../decoy-engine-native" && pwd)"
cmd="${1:-}"
shift || true

# The remaining PyO3-bound functions in arrow_ffi.rs (import_array,
# export_string_array, extract_truncate, derive_batch_checked, derive_batch,
# register) need a live Python interpreter to run meaningfully. Left in
# scope, cargo-mutants either can't build a viable mutant for them (their
# bodies return pyo3 types with no Default impl) or "tests" them against a
# suite that never touches them -- neither is a real signal. This is the
# exclude list Lane A's cargo-mutants invocations use; keep it in sync if
# those function names change.
PYO3_ONLY_FNS='import_array|export_string_array|extract_truncate|derive_batch_checked|derive_batch ->|register ->'

case "$cmd" in
  coverage)
    cd "$CRATE_DIR"
    echo "cargo llvm-cov --show-missing-lines (full crate, cargo test only)"
    cargo llvm-cov --show-missing-lines
    ;;

  mutants-pilot)
    cd "$CRATE_DIR"
    echo "=== pilot 1: batch.rs (whole file, ~11 mutants, proves killed vs survived) ==="
    # cargo-mutants exits nonzero when it finds a surviving mutant, which is the
    # expected pilot outcome here, not a script failure -- don't let `set -e` abort
    # the run before pilot 2.
    cargo mutants -f 'src/batch.rs' --timeout 120 -j 2 "$@" || true
    echo
    echo "=== pilot 2: import_ffi only, ffi_import.rs (proves the PyO3-boundary scoping) ==="
    cargo mutants -f 'src/ffi_import.rs' -F 'import_ffi' --timeout 120 -j 2 "$@" || true
    echo
    echo "(pilot 2 is expected to report a mutant that is unviable to build, not a"
    echo " killed/survived split -- see docs/plans/native-testing-T0-harness.md for why"
    echo " that is still a legitimate result and not a harness failure.)"
    ;;

  mutants)
    cd "$CRATE_DIR"
    cargo mutants \
      -f 'src/derive.rs' \
      -f 'src/canonicalize.rs' \
      -f 'src/batch.rs' \
      -f 'src/ffi_import.rs' \
      -f 'src/arrow_ffi.rs' \
      --exclude-re "$PYO3_ONLY_FNS" \
      "$@"
    ;;

  *)
    echo "usage: $0 {coverage|mutants-pilot|mutants} [-- extra cargo-mutants args]" >&2
    exit 2
    ;;
esac
