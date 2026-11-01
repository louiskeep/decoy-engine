#!/usr/bin/env bash
# Generate a CycloneDX SBOM (JSON) for the base-profile runtime closure of a uv.lock.
#
# Usage: scripts/release/sbom.sh [LOCK [OUT]]      (defaults: uv.lock, sbom.cdx.json)
#        scripts/release/sbom.sh --validate FILE    (schema-check an SBOM with the same tool)
#
# Pipeline (established tools only, nothing hand-rolled):
#   uv export     -> requirements.txt of the locked runtime closure, with the lock's hashes
#   cyclonedx-py  -> CycloneDX JSON (the upstream CycloneDX Python tool; see
#                    https://cyclonedx-bom-tool.readthedocs.io/, "requirements" input)
#
# The tool runs as `python -I -m cyclonedx_py` from a throwaway CPython TOOL_PYTHON venv
# installed from sbom-tool-requirements.txt, the fully resolved and hashed environment
# (cyclonedx-bom plus every transitive dependency), under `uv pip install --require-hashes
# --no-deps --only-binary :all:`: nothing is resolved or built at run time, and a changed or
# tampered release fails the install. Every uv call runs with --no-config and without UV_*
# settings other than cache and transport, and -I keeps PYTHONPATH and user site out of the
# tool. Regenerate the tool file with sbom-tool-lock.sh. Network: the first run downloads
# those wheels (and CPython TOOL_PYTHON if uv does not have it) from PyPI; later runs reuse
# uv's cache. pyproject.toml and uv.lock are never touched.
#
# Profile: [project].dependencies only (no extras, no dev groups; the project itself is the
# root component). The public-PyPI-only source rule applies to that runtime closure.
# Checks (sbom_checks.py): the tool file's grammar before the install; the lock's source
# rule; the export's grammar and a packaging.requirements re-parse; and after generation,
# that the components equal the exported pins and every pin lies in the checked closure.
# Exit is non-zero, and OUT is not written, on any failure.
#
# Reproducibility: --output-reproducible drops serialNumber and timestamp, and
# SOURCE_DATE_EPOCH is unset so the environment cannot re-add a timestamp. Same lock and
# same hashed tool environment give the same bytes.
#
# Output: stdout is empty except "VALID"/"INVALID" in --validate mode; stderr is one fixed
# line, "sbom.sh: wrote SBOM" or a refusal from a closed set. Neither names the caller's paths,
# quotes the lock or SBOM, or passes on uv's or cyclonedx-py's own output: a failing uv or
# tool call reports its label and exit code only. DECOY_SBOM_DEBUG=1 also prints that raw
# subprocess output, which may contain local paths, lock text and environment details; do
# not set it where the output is kept (CI logs, release records).
#
# Environment: every PIP_* variable and every PYTHON* variable except PYTHONDONTWRITEBYTECODE
# is unset before any subprocess runs, so neither uv nor an interpreter it starts can see an
# index, a config file, a startup file or a search path from the caller. python -I already
# ignores PYTHON* for the tool itself; the scrub covers uv and keeps the two in step.
#
# Not signed, not published; release wiring lives in the release workflow.
set -euo pipefail
unset CDPATH

TOOL_PYTHON="3.12"   # must match sbom-tool-lock.sh
SPEC_VERSION="1.6"

# Ambient uv settings must not choose the index, the interpreter, the build mode or
# whether the lock is checked against pyproject.toml.
for v in $(compgen -e); do
  case "$v" in
    UV_CACHE_DIR|UV_NATIVE_TLS|UV_HTTP_TIMEOUT) ;;
    UV_*) unset "$v" ;;
    PYTHONDONTWRITEBYTECODE) ;;
    PIP_*|PYTHON*) unset "$v" ;;
  esac
done

here="$(cd -P -- "$(dirname -- "$0")" >/dev/null && pwd)"
tool_reqs="$here/sbom-tool-requirements.txt"
checks="$here/sbom_checks.py"

die() { echo "sbom.sh: $*" >&2; exit 1; }

# quiet LABEL CMD...: run CMD with stdout and stderr captured; on failure print LABEL and the
# exit code only (the raw output too when DECOY_SBOM_DEBUG=1).
quiet() {
  local label=$1 rc=0
  shift
  "$@" >"$tmp/sub.log" 2>&1 || rc=$?
  if [ "${DECOY_SBOM_DEBUG:-}" = 1 ]; then cat "$tmp/sub.log" >&2; fi
  [ "$rc" -eq 0 ] || die "$label (exit $rc)"
}
usage() { echo "usage: sbom.sh [LOCK [OUT]] | sbom.sh --validate FILE" >&2; exit 2; }

command -v uv >/dev/null 2>&1 || die "uv is required on PATH"
[ -f "$tool_reqs" ] || die "tool requirements file not found: $(basename "$tool_reqs")"
[ -f "$checks" ] || die "input checker not found: $(basename "$checks")"

mode=generate
if [ "${1:-}" = "--validate" ]; then
  [ $# -eq 2 ] || usage
  mode=validate
  target="$2"
  [ -f "$target" ] || die "file to validate not found"
else
  [ $# -le 2 ] || usage
  lock="${1:-uv.lock}"
  out="${2:-sbom.cdx.json}"
  [ -f "$lock" ] || die "lock file not found"
  [ "$(basename "$lock")" = "uv.lock" ] || die "expected a file named uv.lock"
  project_dir="$(cd -P -- "$(dirname -- "$lock")" >/dev/null && pwd)"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

quiet "tool environment install failed (no CPython $TOOL_PYTHON)" \
  uv venv -q --no-config --python "$TOOL_PYTHON" "$tmp/tool"
py="$tmp/tool/bin/python"
"$py" -I -c 'import platform, sys
sys.exit(platform.python_implementation() != "CPython" or "%d.%d" % sys.version_info[:2] != sys.argv[1])' \
  "$TOOL_PYTHON" || die "tool interpreter is not CPython $TOOL_PYTHON"

# The checker is stdlib-only in this mode, so the bare interpreter can vet the tool file
# before uv reads it: hashed pins and comments only, no options, URLs or paths.
"$py" -I "$checks" pins-with-comments "$tool_reqs" || exit 1
quiet "tool environment install failed (hashes are enforced)" \
  uv pip install -q --no-config --python "$py" --require-hashes --no-deps --only-binary :all: \
  -r "$tool_reqs"

if [ "$mode" = validate ]; then
  exec "$py" -I "$checks" validate "$target"
fi

# Public-PyPI-only rule on the runtime closure (uv export drops a private registry URL
# and would label the package pkg:pypi).
"$py" -I "$checks" lock "$lock" || exit 1

# --locked: refuse a lock that is stale against pyproject.toml rather than SBOM a lie.
quiet "uv export failed" \
  uv export --no-config --project "$project_dir" --locked --no-dev --no-emit-project \
  --no-header --no-annotate --format requirements-txt -o "$tmp/requirements.txt"

# The export must be well-formed hashed pins only: no index URLs, paths, git or direct
# URLs, options or comments, which would put infrastructure text into the SBOM.
"$py" -I "$checks" pins "$tmp/requirements.txt" || exit 1

# The tool warns on stderr with its own install path; that output stays in the log.
quiet "cyclonedx-py failed" \
  env -u SOURCE_DATE_EPOCH "$py" -I -m cyclonedx_py requirements "$tmp/requirements.txt" \
  --pyproject "$project_dir/pyproject.toml" --mc-type library \
  --sv "$SPEC_VERSION" --output-reproducible --validate -o "$tmp/sbom.json"

"$py" -I "$checks" complete "$lock" "$tmp/requirements.txt" "$tmp/sbom.json" || exit 1

{ mkdir -p -- "$(dirname -- "$out")" && mv -- "$tmp/sbom.json" "$out"; } 2>/dev/null \
  || die "cannot write the SBOM to the output path"
echo "sbom.sh: wrote SBOM" >&2
