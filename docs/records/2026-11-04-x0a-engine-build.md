# X0a engine build record

Status: record

Slice X0a (engine half, part 1a: ratchet framework) of the cross-cutting V1 foundations plan:
the gate runs every sentry and baselines can only shrink. The self-hosted gate workflow is part 2, record
`2026-11-04-x0a-engine-ci.md`, on top of part 1b.

## What changed

- `tests/sentry/_ratchet.py`: shrink-only baseline logic (registry parse, merge-base subset
  check, reason schema, first-inventory rule, split and replacement rule).
- `scripts/dev/ratchet_check.py`: CLI over that helper. `gates.sh` runs it first in the sentry
  step, and skips it in a tree that predates it (the `--baseline` worktree).
- `scripts/dev/sentry_registry.toml`: sentry id to baseline path, empty here. The 18 existing
  baselines are registered in part 1b (`2026-11-04-x0a-engine-registry.md`).
- `tests/sentry/test_sentry_manifest.py`: the `SENTRIES` manifest (every file in
  `tests/sentry/`, including the H-1 E0 sentries, plus `tests/secret_providers/test_leak_sentry.py`).
- `gates.sh` `SENTRY_PATHS` now also names the leak sentry, which no gate collected before.

## Acceptance tests

| AT | Result | Where |
|---|---|---|
| AT-CI0 | green, red first (script absent) | `tests/sentry/test_ratchet_check.py` |
| AT-CI1 | green, red first (leak sentry uncollected) | `test_sentry_manifest.py` |
| AT-CI2 | green | `test_removing_a_sentry_path_is_detected` |

Plan wording said "red today for `tests/policy`"; the engine has no `tests/policy`, so the
engine's red case was the leak sentry. CI2 also seeds a `tests/policy/...` path as a negative.

## First inventories

A sentry id that is newly added to `sentry_registry.toml` in the diff against the merge base may
bring its first inventory. Nothing else admits a new key: no env var and no flag, and a registered
baseline that gains a key fails even when the registry is edited in the same diff
(`test_env_var_does_not_allow_growth`, `test_growth_fails_even_when_the_registry_is_also_edited`).

## Limits

- A first inventory is reviewed by a person: the check reports it but cannot judge its content.
- The ratchet compares key sets only. A key kept with a rewritten reason passes if the new
  reason is valid.

## Clean-environment gates run

Venv bootstrapped from uv.lock (`DECOY_VENV=<fresh> bash scripts/dev/bootstrap.sh`), run with
`scripts/dev/gates.sh --keep-going --baseline` on this part's head, no env var.

| step | branch | baseline |
| --- | --- | --- |
| env | PASS | SKIP |
| ruff, format, mypy | PASS | PASS |
| sentry | PASS (3981) | PASS (3956) |
| licenses | PASS (63) | PASS (63) |
| tests | PASS (23) | SKIP (none present) |

Failing ids: branch 0, baseline 0, branch-only 0.
