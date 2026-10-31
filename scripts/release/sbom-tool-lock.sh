#!/usr/bin/env bash
# Regenerate sbom-tool-requirements.txt: the fully resolved, hashed environment of the
# SBOM tool (cyclonedx-bom and every transitive dependency), for the Python in TOOL_PYTHON.
# sbom.sh installs exactly this file under --require-hashes, so nothing floats at run time.
#
# To bump the tool: edit the pins in sbom-tool-requirements.in, run this script, review the diff, and rerun
# the SBOM tests. Needs network access to PyPI.
set -euo pipefail
TOOL_PYTHON="3.12"
here="$(cd "$(dirname "$0")" && pwd)"
(cd "$here" && uv pip compile -q --generate-hashes --universal --python-version "$TOOL_PYTHON" \
  --custom-compile-command "scripts/release/sbom-tool-lock.sh" \
  sbom-tool-requirements.in -o sbom-tool-requirements.txt)
