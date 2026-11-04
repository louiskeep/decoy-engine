# X0a engine build record

Status: record

Slice X0a (engine half, part 1 of 2) of the cross-cutting V1 foundations plan: the gate runs every
sentry and baselines can only shrink. The self-hosted gate workflow is part 2, record
`2026-11-04-x0a-engine-ci.md`.

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
`scripts/dev/gates.sh --keep-going --baseline` on the part 1 head before this docs
edit (no code differs). An earlier run on the same clean venv failed one sentry test (the
removed-surface token inside the string-target baseline); that was fixed by masking the token.

| step | branch | baseline |
| --- | --- | --- |
| env | PASS | SKIP |
| ruff, format, mypy | PASS | PASS |
| sentry | PASS (4001) | PASS (3956) |
| licenses | PASS (63) | PASS (63) |
| tests | PASS (43) | SKIP (none present) |

Failing ids: branch 0, baseline 0, branch-only 0.
