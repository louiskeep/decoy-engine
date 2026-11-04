# X0a engine CI record

Status: record

Slice X0a (engine half, part 2 of 2): the hardened self-hosted gate workflow and its guard
sentry. Parts 1a (ratchet framework, `2026-11-04-x0a-engine-build.md`) and 1b (registered baselines, `2026-11-04-x0a-engine-registry.md`) come first.

## What changed

- `.github/workflows/gate-selfhosted.yml`: `runs-on: [self-hosted, macos, decoy-gate]`, body
  `flock ~/.cache/pytest-one.lock scripts/dev/gates.sh --keep-going`.
- Every other workflow except `native-companion-release.yml` (tag publish) is now
  `workflow_dispatch` only. The old triggers are in git history.
- `tests/sentry/test_ci_config.py`: the workflow guard sentry (`selfhosted_violations`) and the
  check that every other workflow is dispatch-only.

## Acceptance tests

| AT | Result | Where |
|---|---|---|
| AT-CI3 | manual, not run | needs the registered runner |
| AT-CI4 | manual, not run | needs the registered runner |

## Manual checks (owner ops, after the runner is registered)

- AT-CI3: push a scratch branch with a seeded failing sentry. The `gate-selfhosted` run must
  fail, and its EVIDENCE block must print the head it tested (`repo: ... @ <sha>`).
- AT-CI4: hold `~/.cache/pytest-one.lock` with a T3 on the Mac, start the job. The log must show
  it waiting and the gate starting only after the T3 releases the lock (compare timestamps).
- The Mac needs `flock` (not shipped with macOS; `brew install flock`) and a venv at
  `~/.cache/decoy-gate-venv` built by `DECOY_VENV=... scripts/dev/bootstrap.sh`.

## Guard sentry shown red (seeded workflows)

`selfhosted_violations` run on two scratch workflows:

- `on: pull_request` plus a self-hosted job: `gate: self-hosted job triggered by ['pull_request']`
- self-hosted job with no actor and fork guard: `gate: self-hosted job lacks the actor and fork guard`
- the real `gate-selfhosted.yml`: no findings.

`native-companion-release.yml` has no self-hosted job and no `pull_request` trigger (tag push and
manual dispatch only, hosted runners), so it needed no guard change.

## Runner ops requirement (security)

The repo is public and the runner executes branch code. The workflow triggers on `push` and
`workflow_dispatch` only, runs only for the owner (`github.actor`, not a fork), has
`contents: read`, `persist-credentials: false` and no secrets; `test_ci_config.py` fails on any
self-hosted job with a `pull_request` or `pull_request_target` trigger or without that guard.
The runner itself must run as a dedicated low-privilege macOS user with no access to the
owner's home directory, keys or git credentials, and with an ephemeral work directory
(wiped between runs).

## Clean-environment gates run

Venv bootstrapped from uv.lock, `scripts/dev/gates.sh --keep-going --baseline` on this part's
head, no env var.

| step | branch | baseline |
| --- | --- | --- |
| env | PASS | SKIP |
| ruff, format, mypy | PASS | PASS |
| sentry | PASS (4008) | PASS (3956) |
| licenses | PASS (63) | PASS (63) |
| tests | PASS (51) | PASS (3) |

Failing ids: branch 0, baseline 0, branch-only 0.
