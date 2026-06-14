"""Pre-flight CI gate: frozen-surface changes must acknowledge the contract.

compatibility-contract section 9 is a checklist meant to be pasted into the PR
description. This gate makes it mechanical: when a PR changes a frozen-surface
path (section 3), the PR body must contain the section-9 checklist with every
item ticked. A PR that touches nothing frozen is unaffected.

Inputs (CI passes these in):
  - changed files: env CHANGED_FILES (newline-separated) if set, else
    `git diff --name-only origin/$BASE_REF...HEAD`.
  - PR body: env PR_BODY.

Exit 0 = pass (no frozen path touched, or checklist satisfied). Exit 1 = the PR
touches a frozen surface without an acknowledged checklist.

The pure functions (touched_frozen, checklist_problems) are unit-tested in
tests/unit/test_compat_preflight.py.
"""

from __future__ import annotations

import os
import subprocess
import sys

# Frozen-surface paths (compatibility-contract section 3). Prefix match against
# the repo-relative changed-file path. Extend as new artifact writers are added;
# over-inclusion only asks for the checklist, it never silently passes.
FROZEN_PREFIXES = (
    "src/decoy_engine/vault.py",
    "src/decoy_engine/unmask.py",
    "src/decoy_engine/determinism/",
    "src/decoy_engine/disguises/",
    "src/decoy_engine/config",
    "src/decoy_engine/sdk.py",
    "src/decoy_engine/__init__.py",
)

# Distinctive fragments of each section-9 checklist item (lowercased). Each must
# appear on a ticked checkbox line in the PR body.
CHECKLIST_ITEMS = (
    "read this document",
    "additive",
    "artifact shape",
    "determinism golden baseline",
    "vault read/write",
    "released disguise version",
    "deprecation shim",
    "compatibility corpus",
)


def touched_frozen(changed_files: list[str]) -> list[str]:
    """Changed files that fall under a frozen-surface prefix."""
    return [f for f in changed_files if f.startswith(FROZEN_PREFIXES)]


def _ticked_lines(pr_body: str) -> list[str]:
    out = []
    for line in pr_body.splitlines():
        low = line.lower()
        if "[x]" in low.replace(" ", ""):  # tolerate "[x]" / "[ x ]" / "[X]"
            out.append(low)
    return out


def checklist_problems(pr_body: str) -> list[str]:
    """Return the section-9 items not present on a ticked line. Empty = satisfied."""
    ticked = _ticked_lines(pr_body)
    missing = []
    for item in CHECKLIST_ITEMS:
        if not any(item in line for line in ticked):
            missing.append(item)
    return missing


def _changed_files() -> list[str]:
    env = os.environ.get("CHANGED_FILES")
    if env is not None:
        return [f.strip() for f in env.splitlines() if f.strip()]
    base = os.environ.get("BASE_REF", "main")
    # base is a CI-controlled branch name (not PR-author input); list form, no
    # shell, so there is no injection surface. git is on PATH in CI.
    cmd = ["git", "diff", "--name-only", f"origin/{base}...HEAD"]
    diff = subprocess.run(cmd, capture_output=True, text=True, check=True)  # noqa: S603
    return [f.strip() for f in diff.stdout.splitlines() if f.strip()]


def main() -> int:
    changed = _changed_files()
    frozen = touched_frozen(changed)
    if not frozen:
        print("compat pre-flight: no frozen-surface paths changed; nothing to acknowledge.")
        return 0

    problems = checklist_problems(os.environ.get("PR_BODY", ""))
    if not problems:
        print(
            f"compat pre-flight: {len(frozen)} frozen-surface file(s) changed; checklist acknowledged."
        )
        return 0

    print("compat pre-flight FAILED.", file=sys.stderr)
    print(
        "This PR changes frozen-surface paths (compatibility-contract section 3):", file=sys.stderr
    )
    for f in frozen:
        print(f"  - {f}", file=sys.stderr)
    print(
        "\nPaste the section-9 checklist into the PR description and tick every box. "
        "These items are not yet ticked:",
        file=sys.stderr,
    )
    for p in problems:
        print(f"  - [ ] ...{p}...", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
