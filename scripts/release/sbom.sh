#!/usr/bin/env bash
# Generate a CycloneDX SBOM (JSON) for the base-profile runtime closure of a uv.lock.
#
# Usage: scripts/release/sbom.sh [LOCK] [OUT]      (defaults: uv.lock, sbom.cdx.json)
#        scripts/release/sbom.sh --validate FILE    (schema-check an SBOM with the same tool)
#
# Pipeline (established tools only, nothing hand-rolled):
#   uv export     -> requirements.txt of the locked runtime closure, with the lock's hashes
#   cyclonedx-py  -> CycloneDX JSON (the upstream CycloneDX Python tool; see
#                    https://cyclonedx-bom-tool.readthedocs.io/, "requirements" input)
#
# The tool runs from a throwaway venv installed from sbom-tool-requirements.txt, the fully
# resolved and hashed environment (cyclonedx-bom plus every transitive dependency), under
# `uv pip install --require-hashes --no-deps`: nothing is resolved at run time and a changed
# or tampered release fails the install. Regenerate it with sbom-tool-lock.sh. Network: the
# first run downloads those wheels (and CPython TOOL_PYTHON if uv does not have it) from
# PyPI; later runs reuse uv's cache. pyproject.toml and uv.lock are never touched.
#
# Profile: [project].dependencies only (no extras, no dev groups; the project itself is the
# root component). The public-PyPI-only source rule applies to that runtime closure.
# Exit is non-zero, and OUT is not written, on any failure.
#
# Reproducibility: --output-reproducible drops serialNumber and timestamp, and
# SOURCE_DATE_EPOCH is unset so the environment cannot re-add a timestamp. Same lock and
# same hashed tool environment give the same bytes.
#
# Not signed, not published; release wiring lives in the release workflow.
set -euo pipefail

TOOL_PYTHON="3.12"   # must match sbom-tool-lock.sh
SPEC_VERSION="1.6"

here="$(cd "$(dirname "$0")" && pwd)"
tool_reqs="$here/sbom-tool-requirements.txt"
checks="$here/sbom_checks.py"

die() { echo "sbom.sh: $*" >&2; exit 1; }

command -v uv >/dev/null 2>&1 || die "uv is required on PATH"
[ -f "$tool_reqs" ] || die "tool requirements file not found: $(basename "$tool_reqs")"
[ -f "$checks" ] || die "input checker not found: $(basename "$checks")"

mode=generate
if [ "${1:-}" = "--validate" ]; then
  mode=validate
  target="${2:-}"
  [ -f "$target" ] || die "file to validate not found: $(basename "${target:-?}")"
else
  lock="${1:-uv.lock}"
  out="${2:-sbom.cdx.json}"
  [ -f "$lock" ] || die "lock file not found: $(basename "$lock")"
  [ "$(basename "$lock")" = "uv.lock" ] || die "expected a file named uv.lock, got: $(basename "$lock")"
  project_dir="$(cd "$(dirname "$lock")" && pwd)"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Throwaway tool environment from the hashed file; --require-hashes makes a missing or
# mismatching hash a hard failure, --no-deps stops uv resolving anything on its own.
{ uv venv -q --python "$TOOL_PYTHON" "$tmp/tool" \
  && uv pip install -q --python "$tmp/tool/bin/python" --require-hashes --no-deps -r "$tool_reqs"; } \
  >/dev/null 2>"$tmp/tool.err" \
  || { cat "$tmp/tool.err" >&2; die "tool environment install failed (hashes are enforced)"; }
py="$tmp/tool/bin/python"

if [ "$mode" = validate ]; then
  "$py" -I -c '
import sys
from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator
import json
t = open(sys.argv[1]).read()
v = SchemaVersion.from_version(json.loads(t)["specVersion"])
e = JsonStrictValidator(v).validate_str(t)
print("INVALID", e) if e else print("VALID")
sys.exit(1 if e else 0)' "$target"
  exit $?
fi

# Public-PyPI-only rule on the runtime closure (uv export drops a private registry URL
# and would label the package pkg:pypi).
"$py" -I "$checks" lock "$lock" || exit 1

# --locked: refuse a lock that is stale against pyproject.toml rather than SBOM a lie.
uv export --project "$project_dir" --locked --no-dev --no-emit-project \
  --no-header --no-annotate --format requirements-txt -o "$tmp/requirements.txt" >/dev/null 2>"$tmp/uv.err" \
  || { cat "$tmp/uv.err" >&2; die "uv export failed"; }

# The export must be well-formed hashed pins only: no index URLs, paths, git or direct
# URLs, options or comments, which would put infrastructure text into the SBOM.
"$py" -I "$checks" pins "$tmp/requirements.txt" || exit 1

pyproject_args=()
[ -f "$project_dir/pyproject.toml" ] && pyproject_args=(--pyproject "$project_dir/pyproject.toml" --mc-type library)

env -u SOURCE_DATE_EPOCH "$tmp/tool/bin/cyclonedx-py" requirements "$tmp/requirements.txt" \
  "${pyproject_args[@]}" --sv "$SPEC_VERSION" --output-reproducible --validate -o "$tmp/sbom.json" \
  || die "cyclonedx-py failed"

mkdir -p "$(dirname "$out")"
mv -- "$tmp/sbom.json" "$out"
echo "wrote $(basename "$out")" >&2
