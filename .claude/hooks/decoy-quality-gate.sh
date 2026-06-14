#!/usr/bin/env bash
# Decoy pre-PR/push quality gate (PreToolUse hook on Bash).
#
# Blocks `git push` and `gh pr create` until the Decoy quality gate has passed
# for the EXACT current HEAD. The pass is recorded in .git/decoy-quality-gate by
# decoy-gate-pass.sh, which should be run only after:
#   1. the `dennis` agent reviewed the change (he runs /code-review +
#      /security-review + /qa and returns a verdict) and every BLOCKER/HIGH is
#      resolved, and
#   2. the `barry` agent synced the docs to the change.
#
# The receipt is tied to the commit SHA, so any new commit after the gate
# invalidates it and the gate fires again. This is the local "automatic standard
# while developing" layer; GitHub branch protection is the complementary
# server-side hard gate.
set -euo pipefail

input=$(cat)
cmd=$(printf '%s' "$input" \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('tool_input',{}).get('command',''))" \
  2>/dev/null || echo "")

# Only gate commands that send work off the branch.
if ! printf '%s' "$cmd" | grep -Eq 'git[[:space:]]+push|gh[[:space:]]+pr[[:space:]]+create'; then
  exit 0
fi

head=$(git rev-parse HEAD 2>/dev/null || echo "")
git_dir=$(git rev-parse --git-dir 2>/dev/null || echo ".git")
receipt=$(cat "$git_dir/decoy-quality-gate" 2>/dev/null || echo "")

if [ -n "$head" ] && [ "$head" = "$receipt" ]; then
  exit 0
fi

short=${head:0:8}
reason="Decoy quality gate not satisfied for HEAD ${short}. Before pushing or opening a PR: (1) dispatch the 'dennis' agent to review this change (he runs /code-review, /security-review, and /qa and returns a verdict) and resolve every BLOCKER and HIGH; (2) dispatch the 'barry' agent to sync the docs to the change; (3) record the pass: bash .claude/hooks/decoy-gate-pass.sh. Then re-run the push. Committing more after the gate re-arms it, by design."

python3 - "$reason" <<'PY'
import json, sys
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": sys.argv[1],
    }
}))
PY
exit 0
