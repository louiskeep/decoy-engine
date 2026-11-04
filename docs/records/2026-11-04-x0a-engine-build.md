# X0a engine build record

Status: record

Slice X0a (engine half) of the cross-cutting V1 foundations plan: the gate runs every sentry,
baselines can only shrink, and the merge gate has a home that does not depend on paid runners.

## What changed

- `tests/sentry/_ratchet.py`: shrink-only baseline logic (registry parse, merge-base subset
  check, reason schema, first-inventory rule, split and replacement rule).
- `scripts/dev/ratchet_check.py`: CLI over that helper. `gates.sh` runs it first in the sentry
  step, and skips it in a tree that predates it (the `--baseline` worktree).
- `scripts/dev/sentry_registry.toml`: sentry id to baseline path. Empty today: the engine has
  no baseline in the plan format. `tests/fixtures/f29/reason-baseline.json` is frozen by
  `test_reason_baseline_frozen.py` and is not wrapped.
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

## Limits

- `RATCHET_SLICE` (or `--slice`) must name the slice that adds a first inventory, or the
  check fails. There is no default, so a forgotten value cannot admit a new inventory.
- The ratchet compares key sets only. A key kept with a rewritten reason passes if the new
  reason is valid.
