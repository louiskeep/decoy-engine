#!/usr/bin/env bash
# Record that the Decoy quality gate passed for the current HEAD.
#
# Run this ONLY after both are true for the commit you are about to push:
#   1. `dennis` reviewed it (/code-review + /security-review + /qa) and no
#      BLOCKER or HIGH finding is left unresolved, and
#   2. `barry` synced the docs to the change.
#
# Running it without doing those defeats the gate. The receipt is the commit
# SHA, so the gate re-arms automatically on the next commit.
set -euo pipefail

git_dir=$(git rev-parse --git-dir)
head=$(git rev-parse HEAD)
printf '%s\n' "$head" > "$git_dir/decoy-quality-gate"
echo "Decoy quality gate recorded for HEAD $head. push / gh pr create is now unblocked for this commit."
