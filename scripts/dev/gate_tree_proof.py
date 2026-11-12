"""Decide whether a push to main may skip the self-hosted gate (gate-selfhosted.yml, tree-proof).

    python scripts/dev/gate_tree_proof.py --repo OWNER/NAME --tree TREE_SHA >> "$GITHUB_OUTPUT"

A main push runs the full sharded gate. That run is redundant when the pushed tree is
byte-identical to a tree that already passed the same full gate: same files means the same
code, tests, gates.sh and workflow. The proof is a recent gate-selfhosted run that was

- a `workflow_dispatch` in this repo (not a fork), started by the owner, concluded `success`;
- on a head commit whose tree equals TREE_SHA (git tree hashes, from the API);
- in full mode with both shards green: jobs `gate 1/2 full` and `gate 2/2 full`, both `success`,
  and no other gate job.

Prints `skip=true` and `proof_run=<url>` when such a run exists, else `skip=false`. Any API error
prints `skip=false` and exits 0, so a failed lookup runs the gate and never fails the push.
Reads the token from GH_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections.abc import Callable
from typing import Any

OWNER = "louiskeep"
WORKFLOW = "gate-selfhosted.yml"
FULL_GATE_JOBS = {"gate 1/2 full", "gate 2/2 full"}
RECENT_RUNS = 50

Fetch = Callable[[str], Any]


def find_proof(repo: str, tree: str, fetch: Fetch) -> dict[str, Any] | None:
    """Return the newest qualifying dispatch run for `tree`, or None."""
    runs = fetch(
        f"repos/{repo}/actions/workflows/{WORKFLOW}/runs"
        f"?event=workflow_dispatch&status=success&per_page={RECENT_RUNS}"
    )["workflow_runs"]
    for run in runs:
        if run.get("event") != "workflow_dispatch" or run.get("conclusion") != "success":
            continue
        if (run.get("head_repository") or {}).get("full_name") != repo:
            continue
        if (run.get("triggering_actor") or run.get("actor") or {}).get("login") != OWNER:
            continue
        head = fetch(f"repos/{repo}/git/commits/{run['head_sha']}")
        if head["tree"]["sha"] != tree:
            continue
        jobs = fetch(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")
        gate = {
            j["name"]: j.get("conclusion") for j in jobs["jobs"] if j["name"].startswith("gate ")
        }
        if set(gate) == FULL_GATE_JOBS and set(gate.values()) == {"success"}:
            return dict(run)
    return None


def _api(token: str) -> Fetch:
    def fetch(path: str) -> Any:
        req = urllib.request.Request(
            f"https://api.github.com/{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https host
            return json.load(resp)

    return fetch


def decide(repo: str, tree: str, fetch: Fetch) -> list[str]:
    """Output lines for $GITHUB_OUTPUT."""
    try:
        run = find_proof(repo, tree, fetch)
    except Exception as e:
        print(f"tree-proof lookup failed ({type(e).__name__}); running the gate", file=sys.stderr)
        return ["skip=false"]
    if run is None:
        return ["skip=false"]
    return ["skip=true", f"proof_run={run['html_url']}"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tree", required=True)
    args = ap.parse_args(argv)
    lines = decide(args.repo, args.tree, _api(os.environ.get("GH_TOKEN", "")))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
