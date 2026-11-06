#!/usr/bin/env bash
# The one shell reader of the lock profile: what uv.lock pins and which CPython and key
# dependencies the engine environment is checked against.
# Usage: scripts/dev/lock_version.sh <package>        first locked version of one package
#        scripts/dev/lock_version.sh --python         the CPython patch of the lock profile
#        scripts/dev/lock_version.sh --check [pkg...] compare installed vs locked (default: key deps)
# --check looks in $DECOY_VENV, else $UV_PROJECT_ENVIRONMENT, else `python` on PATH. It prints one
# line per package and exits 1 on any mismatch.
# bootstrap.sh, the lock-env action, release_checks.sh and the CI smoke steps all call this, so the
# Python version and the key-dependency list exist here only. It is awk on purpose: those steps run
# on bare hosts (CPython 3.10 has no tomllib) before any dependency exists. The structured Python
# readers (scripts/dev/licenses.py locked_versions, quality/dp_provenance.py) need tomllib or tomli.
set -euo pipefail

PROFILE_PYTHON=3.10.20   # the dp-certified-gate profile ci.yml syncs
KEY_DEPS="duckdb pyarrow polars pandas"

lock=$(cd "$(dirname "$0")/../.." && pwd)/uv.lock

locked() {  # first locked version of package $1
  awk -v n="name = \"$1\"" '$0 == n { getline; gsub(/version = |"/, ""); print; exit }' "$lock"
}

check() {
  local py=python bad=0 pkg have want mark
  local venv=${DECOY_VENV:-${UV_PROJECT_ENVIRONMENT:-}}
  if [ -n "$venv" ]; then py=$venv/bin/python; fi
  # shellcheck disable=SC2086  # KEY_DEPS is a space-separated word list on purpose
  [ $# -gt 0 ] || set -- $KEY_DEPS
  local installed
  installed=$("$py" - "$@" <<'PY'
import importlib.metadata as m, sys
for name in sys.argv[1:]:
    try:
        print(name, m.version(name))
    except m.PackageNotFoundError:
        print(name, "absent")
PY
  )
  for pkg in "$@"; do
    have=$(awk -v n="$pkg" '$1 == n { print $2 }' <<<"$installed")
    want=$(locked "$pkg")
    if [ "$have" = "$want" ]; then mark=OK; else mark=MISMATCH; bad=1; fi
    printf '%-11s installed %-10s lock %-10s %s\n' "$pkg" "$have" "$want" "$mark"
  done
  if [ $bad = 1 ]; then echo "lock_version: installed packages differ from uv.lock" >&2; fi
  return $bad
}

case ${1:-} in
  --python) echo "$PROFILE_PYTHON" ;;
  --check) shift; check "$@" ;;
  "" | -*) sed -n 2,7p "$0" >&2; exit 2 ;;
  *) locked "$1" ;;
esac
