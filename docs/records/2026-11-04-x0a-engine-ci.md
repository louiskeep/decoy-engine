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

## Codex V1 fixes

- `scripts/dev/gates.sh` used `mapfile`, which the Mac runner's bash 3.2 lacks. Selection came back empty and the gate could skip tests and pass. It now reads the selection with a `while read` loop, and a failing `changed_tests` exits 2.
- `tests/sentry/test_bash32_compat.py` greps `scripts/dev/*.sh` for mapfile, readarray, `declare -A`, `${v,,}` and `${v^^}`, `&>>` and negative array indexes, with a seeded case for each. No other bash 4 features were found in gates.sh.
- This Linux box has bash 5.2, so the sentry is a text check only. Real bash 3.2 verification happens on the Mac at AT-CI3.
- `compat-preflight.yml` and `regression-proof.yml` read pull_request context (base_ref, PR body, labels), so their `pull_request` triggers are restored from origin/main. They run on paid hosted runners; billing failures there are the known status quo. `test_ci_config.py` exempts both from the dispatch-only rule. The self-hosted guard sentry is unaffected.
- The first live run on the Mac (platform run 41767760740) failed at "gates: no venv at .../.venv". `gate-selfhosted.yml` now has a "Bootstrap the gate venv" step before the gate. It runs `scripts/dev/bootstrap.sh` under the shared pytest-one flock with `DECOY_VENV=$HOME/.cache/decoy-gate-venv-engine`, and the gate step exports the same variable. gates.sh and bootstrap.sh defaults are unchanged. Both steps put `~/bin` (flock shim) and `~/.local/bin` (uv) on PATH. The job guard and permissions are unchanged.

## CI behaviour fixes (second Codex round and live Mac findings)

- Empty selection. `gates.sh` used to pass with zero selected test paths. Now, when the diff against the merge base touches code (`CODE_GLOBS`, defined once in the repo config block: `src/*`, `testflight/*`) and no test is selected, the tests step fails with a message. When the diff touches only docs, records, workflows or other non-code paths, the step reports "not applicable" and passes. An explicit test path missing from the branch exits 2 ("explicit test path not found"), never a skip. Regression tests: code change with empty selection fails, docs-only change passes, absent explicit path fails.
- Baseline mode. `gate-selfhosted.yml` now runs `gates.sh --keep-going --baseline` (same host, `origin/main` in a detached worktree; the checkout already has `fetch-depth: 0`). The verdict covers every step that can fail for host reasons. A pytest step (sentry, tests, licenses) fails only on a failing id main lacks. A mypy or ruff step fails only on a diagnostic main lacks (line numbers stripped, so a moved line still matches). A step with no parseable failures (crash, collection error, ratchet check) fails unless main's same step failed too. `env` has no baseline and still fails on its own. The EVIDENCE block gains a "BRANCH-ONLY FAILING STEPS" section. The flock, the bootstrap step and the job guard are unchanged.
- Cause on the Mac: failures that main also has failed every run, for example a mypy unused-ignore at `src/decoy_engine/fileaccess/_openat2.py:62` and two tests in `tests/sentry/test_native_release_workflow.py` that call the macOS LibreSSL `/usr/bin/openssl`.
- macOS: `bootstrap.sh` used `sha256sum`, absent on macOS. It now falls back to `shasum -a 256`.
- AT-CI4 passed live: engine run 41807648013 waited 46 minutes behind a T3 on the shared pytest-one lock, then ran.
- Limit: when a sentry pytest id fails on both branch and main, a ratchet-check failure in the same step is not separated from it (the step has no ids of its own). Ratchet failures with no shared failing ids are still caught.

## Codex V1 fixes (third round, by construction)

Decision: stop listing the paths that count as code, and list the paths that do not. The
allow-list missed `scripts/codeset_etl` and `decoy-engine-native`, so a source-only change there
selected no tests and was called "not applicable". The baseline verdict had the same shape of
bug: it asked "is this failure shared?" with a rule that could answer yes for a failure it had not
identified. Both now fail closed.

- `NON_CODE_GLOBS` (one place, repo config block of `gates.sh`) lists documentation and repo metadata: `docs/*`, `docs/records/*`, `*.md`, `.github/*`, `LICENSE*`, `NOTICE*`, `CHANGELOG*`, dotfile metadata. Every other changed path is code. A diff with any code path and no selected test fails. Only a diff made entirely of non-code paths reports "not applicable". `CODE_GLOBS` is gone.
- Test selection also maps a changed non-module code file (scripts, native, build files) to tests that name its file name, so editing `gates.sh` selects its own test file.
- `ruff check` now runs with `--output-format concise` so its failures are one parseable line each (the default full format has no `file:line:col: CODE msg` line).
- `_gates_report.branch_only_steps`: a branch step that fails is branch-only when its exit code is not 1 (crash, kill, timeout, pytest 2 to 5), whatever main did. It is also branch-only when its failures cannot all be identified (no JUnit ids, no parseable diagnostic, or a `Found N errors` total that disagrees with the parsed lines), whatever main did. Otherwise failures are compared as `Counter` multisets of (file, code, message) without line numbers, or of pytest ids, so a count increase is branch-only. Identical failures on both sides stay shared.
- Closes the old limit on ratchet failures: the ratchet check exits before pytest, so the sentry step has no JUnit and counts as unidentified.
- Regression tests: change under `scripts/`, a `.sh` file and `Makefile` with no matching test fail; mixed docs and code fails; docs-only passes; branch crash with baseline failure, timeout on both sides (124, 137, 2), unidentified failure, duplicated identical diagnostic, disagreeing total, ruff multiset growth are branch-only; identical failures are shared.
- Known cost: a change to a non-test helper under `tests/` (for example a `conftest.py`) now needs a named test or explicit paths.
