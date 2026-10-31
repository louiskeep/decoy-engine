#!/usr/bin/env bash
# Generate a CycloneDX SBOM (JSON) for the base-profile runtime closure of a uv.lock.
#
# Usage: scripts/release/sbom.sh [LOCK] [OUT]      (defaults: uv.lock, sbom.cdx.json)
#
# Pipeline (established tools only, nothing hand-rolled):
#   uv export     -> requirements.txt of the locked runtime closure, with the lock's hashes
#   cyclonedx-py  -> CycloneDX JSON (the upstream CycloneDX Python tool; see
#                    https://cyclonedx-bom-tool.readthedocs.io/, "requirements" input)
# The tools run through uvx, so pyproject.toml and the lock are never touched.
#
# Profile: [project].dependencies only (no extras, no dev groups, project itself
# is the root component, not a dependency). Exit is non-zero, and OUT is not
# written, on any failure.
#
# Reproducibility: --output-reproducible drops serialNumber and timestamp, and
# SOURCE_DATE_EPOCH is unset so the ambient environment cannot re-add a timestamp.
# Component and ref order come from the (sorted) lock export, so the same lock and
# the same pins give the same bytes.
#
# Not signed, not published; release wiring lives in the release workflow.
set -euo pipefail

CYCLONEDX_BOM_VERSION="7.6.0"
CYCLONEDX_LIB_VERSION="11.12.0"
SPEC_VERSION="1.6"

lock="${1:-uv.lock}"
out="${2:-sbom.cdx.json}"

die() { echo "sbom.sh: $*" >&2; exit 1; }

[ -f "$lock" ] || die "lock file not found: $lock"
command -v uv >/dev/null 2>&1 || die "uv is required on PATH"
project_dir="$(cd "$(dirname "$lock")" && pwd)"
[ "$(basename "$lock")" = "uv.lock" ] || die "expected a file named uv.lock: $lock"

# uv export drops a private registry's URL and emits a bare "name==version", which
# would be labelled pkg:pypi in the SBOM. Check the lock's own source records: only
# the public index and the root project (virtual or editable at ".") are allowed.
badsrc="$(grep -E '^source = ' "$lock" | grep -vE '^source = \{ registry = "https://pypi\.org/simple" \}$|^source = \{ (virtual|editable) = "\." \}$' || true)"
[ -z "$badsrc" ] || die "non-public source in $(basename "$lock") (SBOM covers public-index pins only)"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# --locked: refuse a lock that is stale against pyproject.toml rather than SBOM a lie.
uv export --project "$project_dir" --locked --no-dev --no-emit-project \
  --no-header --no-annotate --format requirements-txt -o "$tmp/requirements.txt" >/dev/null 2>"$tmp/uv.err" \
  || { cat "$tmp/uv.err" >&2; die "uv export failed"; }

# Fail closed on anything that is not "name==version [; marker]" plus --hash lines:
# index URLs, local paths, git or direct URLs would put infrastructure locations
# into the SBOM, which names public packages only.
bad="$(grep -vE '^[A-Za-z0-9][A-Za-z0-9._-]*==[^ ]+( ;.*)?( \\)?$|^[[:space:]]+--hash=sha256:[0-9a-f]{64}( \\)?$|^[[:space:]]*$' "$tmp/requirements.txt" || true)"
[ -z "$bad" ] || die "non-public source or unsupported requirement in the lock export (SBOM covers public-index pins only)"

pyproject_args=()
[ -f "$project_dir/pyproject.toml" ] && pyproject_args=(--pyproject "$project_dir/pyproject.toml" --mc-type library)

env -u SOURCE_DATE_EPOCH uvx --quiet \
  --from "cyclonedx-bom==$CYCLONEDX_BOM_VERSION" \
  --with "cyclonedx-python-lib==$CYCLONEDX_LIB_VERSION" \
  cyclonedx-py requirements "$tmp/requirements.txt" "${pyproject_args[@]}" \
  --sv "$SPEC_VERSION" --output-reproducible --validate -o "$tmp/sbom.json" \
  || die "cyclonedx-py failed"

mkdir -p "$(dirname "$out")"
mv "$tmp/sbom.json" "$out"
echo "wrote $out" >&2
