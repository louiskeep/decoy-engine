#!/usr/bin/env bash
# Regenerate sbom-tool-requirements.txt: the fully resolved, hashed environment of the
# SBOM tool (cyclonedx-bom and every transitive dependency), for the Python in TOOL_PYTHON.
# sbom.sh installs exactly this file under --require-hashes --only-binary :all:, so nothing
# floats or builds at run time. Wheels only: a dependency without a wheel fails here.
# --no-config and the UV_* scrub keep ambient uv settings from choosing the index.
#
# To bump the tool: edit the pins in sbom-tool-requirements.in, run this script, review the diff, and rerun
# the SBOM tests. Needs network access to PyPI.
set -euo pipefail
unset CDPATH
TOOL_PYTHON="3.12"
for v in $(compgen -e); do
  case "$v" in
    UV_CACHE_DIR|UV_NATIVE_TLS|UV_HTTP_TIMEOUT) ;;
    UV_*) unset "$v" ;;
  esac
done
here="$(cd -P -- "$(dirname -- "$0")" >/dev/null && pwd)"
(cd -P -- "$here" >/dev/null && uv pip compile -q --no-config --only-binary :all: --generate-hashes \
  --universal --python-version "$TOOL_PYTHON" \
  --custom-compile-command "scripts/release/sbom-tool-lock.sh" \
  sbom-tool-requirements.in -o sbom-tool-requirements.txt)
