#!/usr/bin/env bash
# Regenerate NOTICE from uv.lock with metadata from the locked wheels themselves.
# Usage: scripts/dev/licenses_write.sh
# Exports the runtime + runtime-extras pins from uv.lock (hashed), installs them with
# --no-deps --require-hashes --only-binary into throwaway --target directories for every
# locked Python (3.10, 3.11, 3.12) on Linux and Windows (Windows-only pins such as tzdata
# and colorama), then runs licenses.py --write over those directories. Nothing is
# imported from them; only dist-info metadata is read. Needs uv and network (or a warm
# uv cache).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
command -v uv >/dev/null || { echo "LICENSES FAIL: uv not on PATH" >&2; exit 1; }
PY=${DECOY_VENV:-${UV_PROJECT_ENVIRONMENT:-$ROOT/.venv}}/bin/python
[ -x "$PY" ] || { echo "LICENSES FAIL: no venv; run scripts/dev/bootstrap.sh" >&2; exit 1; }

# No read-into-array builtin: bash 3.2 compatible.
EXTRAS=()
while IFS= read -r e; do EXTRAS+=("$e"); done < <("$PY" -c '
import json, sys
for e in json.load(open(sys.argv[1]))["extras"]["runtime"]:
    print("--extra"); print(e)' scripts/dev/license_policy.json)

TMP=$(mktemp -d "${TMPDIR:-/tmp}/licenses.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
uv export --no-config --frozen --no-dev --no-emit-project --no-header ${EXTRAS[@]+"${EXTRAS[@]}"} >"$TMP/req.txt"
SITES=()
for py in 3.10 3.11 3.12; do
  for plat in x86_64-unknown-linux-gnu x86_64-pc-windows-msvc; do
    dir=$TMP/site-$py-$plat
    uv pip install --no-config -q --target "$dir" --no-deps --require-hashes --only-binary :all: \
      --python-version "$py" --python-platform "$plat" -r "$TMP/req.txt"
    SITES+=(--site "$dir")
  done
done
"$PY" scripts/dev/licenses.py --write ${SITES[@]+"${SITES[@]}"}
"$PY" scripts/dev/licenses.py --check
