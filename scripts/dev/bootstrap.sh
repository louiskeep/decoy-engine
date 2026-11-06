#!/usr/bin/env bash
# Make the venv match uv.lock exactly, then print the environment header.
# Usage: scripts/dev/bootstrap.sh [--check]
#   --check  verify only, never install (gates.sh runs this as its env step)
# Venv: $DECOY_VENV, else $UV_PROJECT_ENVIRONMENT, else ./.venv. Exits non-zero on any
# mismatch; there is no fallback install path.
set -uo pipefail

REPO_NAME=decoy-engine
EXTRAS=(--extra dev --extra lint --extra vault --extra cloud)
ABI_SRC=src/decoy_engine/execution/native/_crypto_ext.py

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT" || exit 2
LOCK_VERSION="$ROOT/scripts/dev/lock_version.sh"  # the lock profile: Python patch, key deps, locked versions
PY_VERSION=$(bash "$LOCK_VERSION" --python)
export UV_PROJECT_ENVIRONMENT=${DECOY_VENV:-${UV_PROJECT_ENVIRONMENT:-$ROOT/.venv}}
VENV=$UV_PROJECT_ENVIRONMENT
SYNC=(uv sync --frozen ${EXTRAS[@]+"${EXTRAS[@]}"} --python "$PY_VERSION")
CHECK=0
case ${1:-} in
  --check) CHECK=1 ;;
  "") ;;
  *) sed -n 2,6p "$0"; exit 2 ;;
esac

ERRORS=()
err() { ERRORS+=("$1|$2"); }

command -v uv >/dev/null || { echo "BOOTSTRAP FAIL: uv not on PATH (fix: install uv, https://docs.astral.sh/uv/)" >&2; exit 1; }
if [ $CHECK = 0 ]; then
  echo "bootstrap: ${SYNC[*]+${SYNC[*]}}" >&2
  ${SYNC[@]+"${SYNC[@]}"} >&2 || { echo "BOOTSTRAP FAIL: uv sync --frozen failed (fix: read the uv error above)" >&2; exit 1; }
fi
PY=$VENV/bin/python
[ -x "$PY" ] || { echo "BOOTSTRAP FAIL: no venv at $VENV (fix: scripts/dev/bootstrap.sh)" >&2; exit 1; }

echo "== $REPO_NAME environment =="
echo "venv:       $VENV"
py_have=$("$PY" -c 'import platform; print(platform.python_version())')
echo "python:     $py_have (lock profile expects $PY_VERSION)"
[ "$py_have" = "$PY_VERSION" ] ||
  err "python is $py_have, expected $PY_VERSION" "rm -rf $VENV && scripts/dev/bootstrap.sh"
echo "lock:       uv.lock sha256 $(if command -v sha256sum >/dev/null; then sha256sum uv.lock; else shasum -a 256 uv.lock; fi | cut -c1-16)  extras: ${EXTRAS[*]+${EXTRAS[*]}}"

key_out=$(DECOY_VENV="$VENV" bash "$LOCK_VERSION" --check 2>&1)
key_rc=$?
echo "$key_out"
key_bad=0
while read -r dep _ have _ want mark; do
  if [ "$mark" = MISMATCH ]; then
    key_bad=1
    err "$dep installed $have but uv.lock pins $want" "scripts/dev/bootstrap.sh (runs ${SYNC[*]+${SYNC[*]}})"
  fi
done <<<"$key_out"
if [ $key_rc != 0 ] && [ $key_bad = 0 ]; then
  err "key dependency check failed (see output above)" "scripts/dev/bootstrap.sh"
fi

abi_want=$(sed -n 's/^_EXPECTED_ABI_VERSION = "\(.*\)"/\1/p' "$ABI_SRC" 2>/dev/null)
abi_have=$("$PY" -c 'from decoy_engine_native import _kernel; print(_kernel.abi_version())' 2>/dev/null)
if [ -z "$abi_have" ]; then
  echo "native:     companion NOT installed; tree expects ${abi_want:-unknown} (native-only tests skip)"
elif [ "$abi_have" = "$abi_want" ]; then
  echo "native:     companion installed, ABI $abi_have; tree expects $abi_want  OK"
else
  echo "native:     companion installed, ABI $abi_have; tree expects $abi_want  MISMATCH"
  err "native companion ABI $abi_have, tree expects $abi_want" \
    "uv pip install --python $PY -e ./decoy-engine-native (rebuild from this tree), or uninstall it"
fi

if sync_out=$(${SYNC[@]+"${SYNC[@]}"} --check 2>&1); then
  echo "full env:   in sync with uv.lock ($(grep -o 'Checked [0-9]* packages' <<<"$sync_out"))"
else
  echo "full env:   DIFFERS from uv.lock:"
  grep -E '^ [-+] |Would' <<<"$sync_out" | sed 's/^/            /'
  err "venv does not match uv.lock (see diff above)" "scripts/dev/bootstrap.sh"
fi
if ! uv lock --check --offline >/dev/null 2>&1; then
  err "uv.lock is stale against pyproject.toml" "do not edit the lock on a worker branch; report it"
fi

if [ ${#ERRORS[@]} -gt 0 ]; then
  for e in ${ERRORS[@]+"${ERRORS[@]}"}; do
    echo "BOOTSTRAP FAIL: ${e%%|*}" >&2
    echo "  fix: ${e#*|}" >&2
  done
  exit 1
fi
echo "RESULT:     OK, environment matches uv.lock"
