#!/usr/bin/env bash
# Lane B of the native-efficiency test harness (T0 of
# docs/plans/2026-08-29-native-efficiency-test-plan.md): builds the
# decoy-engine-native extension with LLVM source-based coverage instrumentation
# and runs it through the real PyO3/capsule/exception paths, which Lane A's
# `cargo test` never reaches (arrow_ffi.rs sat at 49% region coverage and
# lib.rs at 0% under Lane A alone -- both are only exercised by calling the
# compiled extension from Python).
#
# The tricky part is that this venv is shared across several stacked
# decoy-engine worktrees (`.claude/worktrees/native-*`), and its
# `decoy_engine_native` package is an editable install (a .pth file) pointing
# at ONE of those worktrees. Running `maturin develop` here would repoint that
# shared .pth at this worktree's build, which could break whichever other
# worktree it currently targets. Instead, this script stages the instrumented
# .so in a private scratch directory and puts that FIRST on PYTHONPATH: Python
# resolves `decoy_engine_native` from there before ever reaching the .pth's
# site-packages entry, so the shared venv is never touched.
#
# Usage:
#   scripts/native-testing/lane_b_instrumented_extension.sh
#
# Requires: `source ~/.cargo/env`; PYO3_PYTHON pointed at the venv's own
# python (plain `cargo build` otherwise picks up whatever `python3` resolves
# to on PATH, which was Python 3.11 here against a 3.10 venv -- an ABI
# mismatch that fails at import with "undefined symbol: PyType_GetName").

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CRATE_DIR="$REPO_ROOT/decoy-engine-native"
VENV="${DECOY_VENV:-/home/cam/vscode/decoy-engine/.venv}"
SCRATCH="${1:-$(mktemp -d)}"
STAGE="$SCRATCH/lane_b_stage"

echo "staging instrumented extension at: $STAGE"
mkdir -p "$STAGE/decoy_engine_native"

cd "$CRATE_DIR"
eval "$(cargo llvm-cov show-env --sh 2>/dev/null | grep -v '^warning\|^info')"
export PYO3_PYTHON="$VENV/bin/python"

echo "=== building instrumented cdylib ==="
touch src/lib.rs  # force pyo3's build script to re-run under PYO3_PYTHON
cargo build

EXT_SUFFIX="$("$VENV/bin/python" -c "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))")"
cp target/debug/lib_kernel.so "$STAGE/decoy_engine_native/_kernel${EXT_SUFFIX}"
cp decoy_engine_native/__init__.py "$STAGE/decoy_engine_native/__init__.py"

# `python -c`/`pytest` both prepend cwd to sys.path ahead of PYTHONPATH. This
# crate's own decoy_engine_native/ (the maturin-generated stub package, with
# no compiled extension inside) sits right here in $CRATE_DIR -- if cwd stayed
# $CRATE_DIR, that stub would shadow our staged, instrumented package instead
# of the other way around. Move to REPO_ROOT (which has no decoy_engine_native/
# of its own) before any Python invocation.
cd "$REPO_ROOT"

echo "=== sanity check: instrumented extension loads and shadows the shared venv ==="
PYTHONPATH="$STAGE:$REPO_ROOT/src" "$VENV/bin/python" -c "
import decoy_engine_native._kernel as k
print('loaded from:', k.__file__)
print('abi_version:', k.abi_version())
"

echo "=== running the ABI/parity/loader Python tests against it ==="
PYTHONPATH="$STAGE:$REPO_ROOT/src" "$VENV/bin/python" -m pytest -q \
  tests/native/test_native_ext_abi.py \
  tests/native/test_keyed_derivation_kernel_parity.py \
  tests/native/test_crypto_ext_loader.py

echo
echo "=== merging profraw and reporting coverage (arrow_ffi.rs / lib.rs vs Lane A) ==="
cd "$CRATE_DIR"
cargo llvm-cov report --show-missing-lines

echo
echo "(compare this table's arrow_ffi.rs/lib.rs rows against Lane A's coverage"
echo " baseline -- Lane B should read materially higher there, and materially"
echo " lower on derive.rs/canonicalize.rs/batch.rs, since it only ran three"
echo " Python-side test files, not the full Rust unit-test suite.)"
