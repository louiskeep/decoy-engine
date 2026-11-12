Status: record

# Engine merge gate on any decoy-gate runner

Date: 2026-11-07. Branch: ci/gate-any-runner.

## Decision (owner)

- The `gate` job in `.github/workflows/gate-selfhosted.yml` runs on `[self-hosted, decoy-gate]`,
  so the Mac runner and the Linux GCP runner (`gcp-decoy-engine`, VM decoy-test-1) both take it.
- Plan branches need no gate. Plans still get the Dennis and Codex reviews.
- The owner also approved skipping documentation-only pushes. It is not built: sentries read
  files under `docs/` and `*.md` files (CHANGELOG, CODEMAP, `_MAP.md`, the compatibility contract),
  one model file under `docs/` ships in the wheel, and GitHub path filters stop at 3,000 changed
  files. Both reviews (Dennis, Codex) found a skip could pass a change no gate saw. A docs-only push
  runs the impact-selected gate, about 5 minutes.
- V2 and V1+ branches must not use the gate queue.

## Push filters

`branches-ignore`: `plan/**`, `feat/v2-**`, `plan/v2-**`, `feat/v1plus-**`, `plan/v1plus-**`
(`**` so nested branch names match too). No path filter. `workflow_dispatch` still runs any
branch by hand. Only
`push` and `workflow_dispatch` trigger the job, never `pull_request`.

## Pins

`tests/sentry/test_ci_config.py` asserts the runs-on labels, the exact ignore list, that no path filter exists, and that
`main` matches no ignore pattern. `tests/sentry/_ci_lock_scan.py` (`MAC_GATE`, mirrored into the
CLI repo) carries the new labels.

## Seeded red cases

Each edit was applied to the workflow alone, the sentry run, then reverted.

Full `tests/sentry` on the VM for each case (baseline: 4119 passed, 2 skipped):

| Seeded edit | Result |
| --- | --- |
| add `main` to `branches-ignore` | red: `test_selfhosted_gate_push_filters_are_pinned` |
| remove the `feat/v2-*` entry | red: `test_selfhosted_gate_push_filters_are_pinned` |
| add a `pull_request` trigger | red: `test_self_hosted_jobs_are_pr_free_and_actor_guarded`, `test_selfhosted_gate_runs_gates_under_the_shared_lock` |
| add a `paths-ignore` filter | red: `test_selfhosted_gate_push_filters_are_pinned` |
