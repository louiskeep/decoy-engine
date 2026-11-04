# X0a engine build record

Status: record

Slice X0a (engine half) of the cross-cutting V1 foundations plan: the gate runs every sentry,
baselines can only shrink, and the merge gate has a home that does not depend on paid runners.

## What changed

- `tests/sentry/_ratchet.py`: shrink-only baseline logic (registry parse, merge-base subset
  check, reason schema, first-inventory rule, split and replacement rule).
- `scripts/dev/ratchet_check.py`: CLI over that helper. `gates.sh` runs it first in the sentry
  step, and skips it in a tree that predates it (the `--baseline` worktree).
- `scripts/dev/sentry_registry.toml`: sentry id to baseline path. 18 sentries registered, their
  current contents being the initial inventory (`created_in` X0a): the F29 reason baseline,
  producer and consumer inventories, secret-material census, both file-access TOML lists, and
  the in-code allowlists of module-size, public-import-boundary, exception-hierarchy,
  file-access entry seams, locator waivers, log interpolation, eval sites, phase containment,
  validate-no-mutation (empty), the physical-seam connected modules, the Wave B string-target
  exemptions and the Wave B E0 test-file list. The F29 baseline is registered, but
  `test_reason_baseline_frozen.py` (sha256 digest) stays the authoritative, stricter freeze.
  Baselines for in-code lists are mirrors: `test_baseline_registry.py` fails when the live list
  and its baseline differ, so adding an allowlist entry needs a baseline key the ratchet refuses.
  Sources and the generator are in `tests/sentry/_baseline_sources.py`.
- `tests/sentry/test_baseline_registry.py`: also fails when a sentry test reads an unregistered
  allowlist-shaped constant or committed json/toml list (seeded cases for both are red-checked).
  Pinned contract files (F06 matrices, released F29 manifests, proof manifest) are listed with a
  reason in `NOT_A_LIST`.
- `tests/sentry/test_sentry_manifest.py`: the `SENTRIES` manifest (every file in
  `tests/sentry/`, including the H-1 E0 sentries, plus `tests/secret_providers/test_leak_sentry.py`).
- `gates.sh` `SENTRY_PATHS` now also names the leak sentry, which no gate collected before.
- `.github/workflows/gate-selfhosted.yml`: `runs-on: [self-hosted, macos, decoy-gate]`, body
  `flock ~/.cache/pytest-one.lock scripts/dev/gates.sh --keep-going`.
- Every other workflow except `native-companion-release.yml` (tag publish) is now
  `workflow_dispatch` only. The old triggers are in git history.

## Acceptance tests

| AT | Result | Where |
|---|---|---|
| AT-CI0 | green, red first (script absent) | `tests/sentry/test_ratchet_check.py` |
| AT-CI1 | green, red first (leak sentry uncollected) | `test_sentry_manifest.py` |
| AT-CI2 | green | `test_removing_a_sentry_path_is_detected` |
| AT-CI3 | manual, not run | needs the registered runner |
| AT-CI4 | manual, not run | needs the registered runner |

Plan wording said "red today for `tests/policy`"; the engine has no `tests/policy`, so the
engine's red case was the leak sentry. CI2 also seeds a `tests/policy/...` path as a negative.

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

## Introducing-slice gate setting

The 18 baselines are new against `origin/main`, so `ratchet_check.py` treats them as first
inventories and needs `RATCHET_SLICE=X0a` on this branch. After merge they are in the merge base
and the variable is no longer needed.

## Limits

- `RATCHET_SLICE` (or `--slice`) must name the slice that adds a first inventory, or the
  check fails. There is no default, so a forgotten value cannot admit a new inventory.
- The ratchet compares key sets only. A key kept with a rewritten reason passes if the new
  reason is valid.
