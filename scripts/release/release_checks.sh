#!/usr/bin/env bash
# The checks cut-release.sh runs before it may tag VERSION. Runnable on its own (any branch):
#
#   scripts/release/release_checks.sh VERSION
#
# 0. changelog.d/ holds nothing but its README: every fragment was compiled into CHANGELOG.md by
#    stamp_release.py. A fragment that merged after the release prep PR is caught here.
# 1. uv.lock agrees with pyproject.toml (uv lock --check: nothing is written).
# 2. A throwaway venv synced --frozen from the lock in the DP certified profile (the lock-profile CPython,
#    extras dev+lint+vault+cloud: the same profile as ci.yml's dp-certified-gate).
# 3. In that venv: the version-agreement and reason-release-pin tests, the reason pin --check,
#    the reason pin --release-ready (no pending reason, deprecation or summary correction is left
#    for stamp_release.py; the pin script asks the one registry policy), and the DP fit gate
#    (check_fit_environment), which fails unless this engine version's own 79-dist profile is a
#    certified row.
# 4. The CycloneDX SBOM from the lock (scripts/release/sbom.sh), written to $SBOM_OUT or a temp
#    path; its sha256 is printed. A path inside the checkout dirties the tree and cut-release.sh
#    then refuses to tag.
# Network: uv may download the lock-profile CPython, the locked wheels and the SBOM tool on a cold cache.
set -euo pipefail
unset CDPATH

die() { echo "release_checks: $*" >&2; exit 1; }
[ $# -eq 1 ] || die "usage: release_checks.sh VERSION"
version=$1

for v in $(compgen -e); do
  case "$v" in
    UV_CACHE_DIR|UV_NATIVE_TLS|UV_HTTP_TIMEOUT) ;;
    UV_*) unset "$v" ;;
  esac
done

here="$(cd -P -- "$(dirname -- "$0")" >/dev/null && pwd)"
root="$(git -C "$here" rev-parse --show-toplevel)"
cd "$root"

if [ -d changelog.d ]; then
  left="$(find changelog.d -mindepth 1 ! -name README.md | sort | head -n 5)"
  [ -z "$left" ] || die "changelog.d still holds uncompiled fragments (run scripts/release/stamp_release.py): $(echo "$left" | tr '\n' ' ')"
fi

command -v uv >/dev/null 2>&1 || die "uv is required on PATH"

have="$(sed -n '/^\[project\]$/,/^\[/{s/^version = "\([^"]*\)"$/\1/p;}' pyproject.toml)"
[ "$have" = "$version" ] || die "pyproject.toml says $have, asked to check $version"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "== lock agrees with pyproject"
uv lock --check --no-config

echo "== certified-profile venv from the lock"
UV_PROJECT_ENVIRONMENT="$tmp/venv" uv sync --frozen --no-config --quiet \
  --extra dev --extra lint --extra vault --extra cloud --python "$(bash "$here/../dev/lock_version.sh" --python)"
py="$tmp/venv/bin/python"

echo "== version agreement and reason release pin"
"$py" -m pytest -q -p no:cacheprovider tests/unit/test_version_agreement.py \
  tests/unit/test_reason_release_pin.py
"$py" scripts/release/reason_release_pin.py --check
"$py" scripts/release/reason_release_pin.py --release-ready

echo "== DP certified profile admits decoy-engine $version"
"$py" -I -c 'import decoy_engine, sys
from decoy_engine.quality.dp_provenance import check_fit_environment
assert decoy_engine.__version__ == sys.argv[1], decoy_engine.__version__
check_fit_environment()
print("certified profile ok")' "$version"

echo "== SBOM"
out=${SBOM_OUT:-$tmp/sbom.cdx.json}
"$here/sbom.sh" uv.lock "$out"
sha256sum "$out"
echo "release checks passed for $version"
