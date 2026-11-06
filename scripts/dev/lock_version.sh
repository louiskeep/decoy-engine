#!/usr/bin/env bash
# Print the first locked version of one package from this repo's uv.lock.
# Usage: scripts/dev/lock_version.sh <package>
# The one shell reader for bootstrap.sh, the lock-env action and the CI smoke steps that must
# install the locked pyarrow. It is awk on purpose: those steps run on bare hosts (CPython 3.10
# has no tomllib) before any dependency exists. The structured Python readers
# (scripts/dev/licenses.py locked_versions, quality/dp_provenance.py) need tomllib or tomli.
set -euo pipefail
pkg=${1:?usage: lock_version.sh <package>}
lock=$(cd "$(dirname "$0")/../.." && pwd)/uv.lock
awk -v n="name = \"$pkg\"" '$0 == n { getline; gsub(/version = |"/, ""); print; exit }' "$lock"
