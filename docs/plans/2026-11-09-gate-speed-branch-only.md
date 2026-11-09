Status: plan

# Merge gate: branch pass only, with a cached main reference

Rules consulted: 00-universal, risk-and-exceptions, development-loop, testing (Test depth),
feature-dev. Risk: R2 (it changes what the merge gate lets through).

Owner decision 2026-11-09: main is green. The stored Linux baselines
(`engine-main-baseline.json`, `cli-main-baseline.json`) record 0 failing. The self-hosted gate
(`.github/workflows/gate-selfhosted.yml`) runs `scripts/dev/gates.sh --keep-going --baseline`.
That runs every step on the branch and again on origin/main in a temporary worktree, so every run
costs about twice what it needs to. Only 3 runners exist (gcp-decoy-engine on test-1,
gcp-decoy-engine-2 on test-5, mac-decoy-engine), and the queue is the bottleneck. This plan makes
CI run the branch pass only. It keeps the two integrity checks the compare gave (no extra skips,
no lost tests) without running main's static steps, and reruns main tests only on a cache miss.

Guarantee: CI blocks on any failing step or test, on any test the branch newly skips, and on lost
tests in files the branch did not change. When the reference is missing or not usable, CI runs
today's `--baseline` and says so. Non-goals: changing local `--baseline`, `--baseline-file` or
`--write-baseline`; the CLI repo's gate (follow-up once this lands); test selection itself.

## 1. Behavior

1. CI runs the branch pass only: env, ruff, format, mypy, sentry, licenses and impact-selected
   tests. Any failing step or failing test blocks (exit 1). No failure is excused as shared with
   main.
2. env, ruff, format, mypy and licenses get no main comparison. Main is green, so any error on
   the branch belongs to the branch.
3. For each pytest step (sentry, licenses, tests), the gate compares the branch against a main
   reference, per test file, for the files that step ran:
   - **Skips:** a test id skipped on the branch but not on main blocks. This covers a
     `@pytest.mark.skip` added to an existing test, a `pytest.skip()` added in a body or fixture,
     and a new test that is skipped (open question 1).
   - **Lost tests:** a file the branch did not change must collect as many ids as on main. Fewer
     blocks. A file the branch changed may collect fewer, because the removal is in the diff; the
     EVIDENCE lists it. Deleted files are left out, as `deleted_test_ids` does today.
   - Whole-suite totals (main: 42,446 collected, 5,304 skipped) are not compared. A selection runs
     a few dozen files, so a total against the whole suite says nothing. That is why the current
     `--baseline-file` check refuses a selection and why this check is per file.
4. **Reference key:** repo, platform (`run_mode()["platform"]`, for example `linux-x86_64` or
   `darwin-arm64`) and the merge-base SHA of HEAD with origin/main. The selection diffs against
   the merge base, so "what the branch changed" and the reference describe the same tree. One JSON
   file per key holds, per test file: collected count, skipped ids, failing ids, and the
   `workers` value that filled it.
5. **Fill on miss:** if the cache file lacks some selected files, the gate adds a detached
   worktree at the merge base and runs only the pytest steps, only for the missing files. It
   merges the result into the cache file (temp file, then rename). Static steps never run on main.
   A second push of the same branch at the same merge base runs nothing on main.
6. **Fail safe:** the gate falls back to today's full `--baseline` rerun, and prints the reason
   on a `ref:` line in the EVIDENCE header, when:
   - the cache file is unreadable, has the wrong `format_version`, names another repo or platform,
     or has a per-file entry with missing fields;
   - the fill did not complete (pytest exit other than 0, 1 or 5, a totals line that disagrees
     with the JUnit, or no JUnit file);
   - the branch changes gate or test tooling: `scripts/dev/gates.sh`, `scripts/dev/_gates*.py`,
     any `conftest.py` at or above a selected file, or the pytest section of `pyproject.toml`.
     Counts made with main's tooling would not describe the branch's.

   The gate exits 0 only after a completed reference compare or a completed baseline verdict, and
   the EVIDENCE names which one ran. A run with neither exits 3, as an incomplete block does today.
7. **Main red:** a failing id in the reference means main fails it at the merge base. If the
   branch fails that id too, the gate exits 6 and prints `gates: MAIN RED at <sha>: <ids>; fix
   main first`. The workflow turns exit 6 into an `::error::` annotation titled "main is red".
   Per the owner rule (red main = stop and fix), the fix lands on main. There is no skip list and
   no excuse in the gate. If the branch passes those ids, the gate exits 0 and the EVIDENCE notes
   that the branch fixes a main failure. In CI fallback, a `--baseline` verdict with any BOTH
   failure also exits 6 instead of passing as shared.
8. **Local use is unchanged.** `--baseline`, `--baseline-file` (the mode `decoy-vm-t3 stored`
   uses) and `--write-baseline` keep their flags, file format and exit codes. The reference cache
   is a separate format in a separate module. The `Baseline` model, `MODE_KEYS` and
   `check_stored` do not change.
9. **Workers:** CI keeps `GATES_WORKERS=auto`. The stored baseline records `workers` and refuses a
   compare in another mode, because its failing-id diff can depend on order and parallelism. The
   reference compares counts and skipped ids, which do not depend on the worker count (the
   parallel and serial passes split the selection and are read as one run). The reference records
   `workers` but does not refuse on it. Acceptance test A12 proves this on each runner before CI
   relies on it. So CI must never point `--baseline-file` at the stored
   `~/.local/state/decoy-ci/*-main-baseline.json` files: they were written at `workers: 1` and
   would exit 2 at `auto`.

## 2. Failure modes

| Failure | Result |
| --- | --- |
| Branch adds a failing test | Exit 1, id listed under FAILING TEST IDS. |
| Branch skips an existing test (marker or runtime) | Exit 1, `NEW-SKIP <id>`. |
| src change makes an unchanged test file collect fewer ids | Exit 1, `LOST <file> main=N branch=M`. |
| Collection error in a selected file | Error in the JUnit, exit 1 (as today). |
| Cache file corrupt, wrong platform, old format | Fallback to `--baseline`, `ref: fallback (<reason>)`. |
| Fill crashes or its totals disagree with the JUnit | Fallback to `--baseline`. |
| Branch edits gate tooling or a conftest | Fallback to `--baseline`. |
| Main fails a test the branch also fails | Exit 6, MAIN RED message and annotation. |
| Failure only one host has on main (e.g. macOS) | Exit 6 on that runner; open question 2. |
| Two jobs write one cache file | Cannot overlap: each job holds `~/.cache/pytest-one.lock`. Writes are temp then rename; a torn file falls back. |
| Cache grows without limit | Entries older than 14 days are removed at gate start. |

## 3. Where main's reference comes from

- **A. Committed in the repo.** A file about main SHA X cannot be part of commit X. It needs a
  bot commit after every merge, and parallel branches would conflict on it. Rejected.
- **B. CI artifact from a post-merge main run.** One full suite per platform per merge (about 98
  single-thread minutes for the engine, roughly 25 to 30 at 4 workers), `actions: read` on the
  token, a run-id lookup by SHA, and a window after each merge in which every push falls back.
  One shared Linux artifact also assumes test-1 and test-5 skip the same tests. A host difference,
  such as a service present on one VM only, would read as a branch skip.
- **C. Computed per main SHA and cached on the runner.** The fill in behavior 5.

**Recommend C**, keyed by merge-base SHA and filled per file on demand:

- Same environment: the reference comes from the same host, venv and services as the branch run,
  so host-dependent skips cancel out. This matters most on the Mac, whose skips and failures
  differ from Linux (Linux-only tests, macOS-only failures). The Mac gets its own reference with
  no extra setup.
- Cost follows use: main reruns only selected pytest files that are not cached yet, and never the
  static steps. Repeated pushes to one branch, the common case, hit the cache.
- No token, artifact API or post-merge job. The workflow stays free of `secrets.`, which
  `tests/sentry/test_ci_config.py` requires.
- Missing or stale data cannot pass silently. A missing entry is computed. Anything unreadable or
  incomplete falls back to the full `--baseline`. A merge-base key cannot go stale (it describes
  exactly that commit); tooling changes force a fallback (behavior 6).

Cache path: `~/.cache/decoy-gate-ref/<repo>/<platform>/<merge-base-sha>.json`, outside the
workspace so checkout cleanup cannot remove it.

## 4. Established solutions

Searched: per-commit cached test data in CI (pytest-testmon, Bazel remote cache). pytest-testmon
users store its dependency database per commit hash and pick the nearest ancestor that has one
([write-up](https://medium.com/@boxed/vastly-faster-python-integration-tests-9d8106b3693c),
[pytest-testmon](https://pypi.org/project/pytest-testmon)). Bazel caches a test result until its
inputs change ([remote cache](https://bazel.build/remote/cache-remote)). Option C follows the
same pattern: data keyed by an exact commit, a miss is computed and never assumed. Neither tool
checks skips or lost tests against a reference. Adopting testmon would replace this repo's
selection and adds its own coverage gaps, which is out of scope. The custom part is small: a
per-file count and skip-id compare over the JUnit files `_gates_report.py` already parses.

## 5. Design notes

One new module, `_gates_ref.py`, holds the reference format and the compare. It sits beside
`_gates_baseline.py`, which holds the stored-baseline format, and reuses `_gates_report`'s JUnit
readers (`_roots`, `_case_id`, `_locate`) rather than parsing again. gates.sh keeps only control
flow: key, fill or fallback, exit code. The CI rule "the gate runs in ref mode" has one choke
point, the workflow command, pinned by `test_ci_config.py`. Mutation: change the workflow back to
bare `--baseline` and that test turns red.

## Class table

| # | Class | Applies? | Structure that closes it | Proof | Mutation that turns it red |
| --- | --- | --- | --- | --- | --- |
| 1 | value-bearing output | N/A: the cache and EVIDENCE hold test ids, counts and SHAs only, as today | - | - | - |
| 2 | cleanup on exit paths | Yes: the fill worktree | The existing `cleanup` EXIT trap removes it on every exit | A9 (crashing fill) asserts no worktree is left | Drop the fill worktree from `cleanup` |
| 3 | state and concurrency | Yes: one cache file per key | Writes under the pytest-one lock, temp then rename; the reader rejects a torn file | A8 | Write in place without rename |
| 4 | route parity, silent fallback | Yes: ref compare vs fallback | Exit 0 needs a `ref:` compare or a baseline verdict; every fallback prints its reason | A8, A9, A10 | Fallback returns 0 |
| 5 | referential integrity | N/A: no data relations in gate tooling | - | - | - |
| 6 | untrusted producer | Yes: cache file content | Strict loader: format version, repo, platform, typed fields; anything else falls back | A8 | Accept unknown fields |
| 7 | toctou | N/A: the job holds the lock from cache read to cache write | - | - | - |
| 8 | weak tests | Yes: the skip and lost-test checks | Seeded cases per check plus the A12 real-run invariance check | A2, A3, A5, A12 | Drop the skip compare |
| 9 | refactor drift | Yes: local modes must not change | Existing gates tests stay untouched | A13 | Route `--baseline-file` through the new loader |
| 10 | frozen contract drift | Yes: stored baseline format | `Baseline` and `MODE_KEYS` untouched; new format in its own module | A13 (`test_a_baseline_written_by_gates_is_comparable_by_gates`) | Add a field to `Baseline` |

## Enumerations

### Exit paths of gates.sh

- 0 pass; 1 branch failure; 2 usage or refused input; 3 incomplete EVIDENCE
  (`scripts/dev/_gates_report.py:381` `main` must print the closing rule); new 6 MAIN RED.
- The fill worktree joins `$WT` and `$MBT` in the EXIT trap, so every exit removes it.

### Routes through the gate

- Ref compare (new, CI default). Reads the cache, fills misses.
- Fallback `--baseline`, verdict from `scripts/dev/_gates_report.py:266` `branch_only_steps`.
- Stored `--baseline-file`, diff from `scripts/dev/_gates_report.py:309` `_stored_diff`; mode
  checked by `scripts/dev/_gates_baseline.py:152` `check_stored`. Unchanged.

### FK shapes

N/A: gate tooling handles no relational data.

### Fields of the reference file

- Top: `format_version`, `repo`, `platform`, `main_sha` (the merge base), `written_at`.
- Per test file: `collected`, `skipped` (ids), `failing` (ids), `workers`. Ids come from
  `scripts/dev/_gates_report.py:141` `_case_id`, the same ids the stored baseline uses.

### Output surfaces

- EVIDENCE block on stdout: new `ref:` header line and NEW-SKIP, LOST, MAIN-RED lines.
- stderr `gates:` messages, the workflow `::error::` annotation on exit 6, and the cache file.

## Reuse inventory

- JUnit readers `_roots`, `_case_id`, `_locate`, `_xmls` in `_gates_report.py`.
- `run_steps` in gates.sh for the fill, with a `main` label and the missing-file list.
- `deleted_test_ids` in gates.sh for files the branch deleted.
- The EXIT trap, the pytest-one lock, and `run_mode()["platform"]`.

## Frozen contracts

- Stored baseline JSON (`Baseline`, `format_version` 1, `MODE_KEYS`) read by `decoy-vm-t3`.
- gates.sh flags `--baseline`, `--baseline-file`, `--write-baseline`, `--order-from` and exit
  codes 0 to 3. New code is additive: one flag, one exit code.

## Slices

1. `_gates_ref.py` with its unit tests (format, compare) and no gates.sh wiring.
2. gates.sh `--ref-cache`, fill and fallback, exit 6; A1 to A13.
3. Workflow switch and `test_ci_config.py` (A14), after the rollout checks in section 8.

## Pre-mortem

- Host-dependent skips differ between fill time and branch time (a service went down between
  runs): reads as NEW-SKIP. Mitigation: the reason names main's skip state; the owner can clear
  the cache key. Watch the first week.
- A runtime skip that depends on test order under xdist: A12 catches it before rollout.
- The Mac is red on main and nobody knew: section 6 runs the Mac full suite first.
- The cache hides a regression in gate tooling: tooling changes always fall back (behavior 6).

## 6. Acceptance tests

New file `tests/unit/scripts/test_gates_ref.py`, built on `_gates_harness.py` (scratch clone,
fake pytest driven by `FAKE_*`). Each case also asserts which result the EVIDENCE names: `ref:`
compare or fallback.

| # | Seed | Expected |
| --- | --- | --- |
| A1 | Branch adds a failing test | Exit 1. Meta says origin/main not rerun; no baseline step logs. |
| A2 | Branch adds `@pytest.mark.skip` to an existing passing test | Exit 1, `NEW-SKIP`. |
| A3 | Branch adds `pytest.skip()` in an existing test body | Exit 1, `NEW-SKIP`. |
| A4 | Branch adds a new test that is skipped | Exit 1 (default; open question 1). |
| A5 | src change drops a parametrize case in an unchanged test file | Exit 1, `LOST`. |
| A6 | Branch removes a test from a file it modified | Exit 0, shortfall listed. |
| A7 | Empty cache | Fill runs only the selected files at the merge base (no ruff or mypy main logs) and writes the cache. An immediate rerun creates no main worktree. |
| A8 | Corrupt cache, wrong `format_version`, other platform | Fallback to `--baseline`; exit follows its verdict. |
| A9 | Fill pytest exits 2, or its totals disagree with the JUnit | Fallback to `--baseline`. |
| A10 | Branch edits `tests/conftest.py` or `scripts/dev/_gates_report.py` | Fallback to `--baseline`. |
| A11 | Merge base fails `test_x`; branch fails it too, or fixes it | Exit 6 with MAIN RED, or exit 0 with a note. The same seed in fallback mode also exits 6. |
| A12 | Cache filled at `GATES_WORKERS=1`, compared at 2 | Same counts, no exit 2. Plus a one-time real run per runner: main at `GATES_WORKERS=1` and `auto` gives identical per-file counts and skipped ids. |
| A13 | `test_gates_baseline_file.py`, `test_gates_parallel.py`, `test_dev_bootstrap_gates.py` | Unchanged and green (`--baseline`, `--baseline-file`, `--write-baseline`). |
| A14 | `tests/sentry/test_ci_config.py` | Workflow runs `gates.sh --keep-going --ref-cache`, not bare `--baseline`; the cache dir is outside the workspace. |

Mutations for the T2 gate: drop the skip compare (A2 and A3 must fail), drop the unchanged-file
collected check (A5), make the fallback return 0 (A8), drop the exit-6 branch (A11), revert the
workflow command (A14).

Before CI switches: run the full engine suite on the Mac at the current main SHA and confirm it
is green. The 0-failing baselines cover the Linux VMs only.

## 7. Placement

CODEMAP "Where Things Go", row "Dev tooling": `scripts/dev/`, named by tool concern, sized by
`tests/sentry/test_module_size.py`. Row "Unit tests" for tool tests: `tests/unit/scripts/`
already holds the gates tests. Neither directory is near a fan-out limit (15 and 8 files).

| File | Change | Beside | Size after |
| --- | --- | --- | --- |
| `scripts/dev/gates.sh` | `--ref-cache DIR` flag; key; tooling-change check; fill run (reuses `run_steps` with a `main` label and the missing-file list); fallback switch; exit 6; header usage text | - | 485 now, about 560 |
| `scripts/dev/_gates_ref.py` (new) | Reference format: load, validate, merge, atomic write, prune. Per-file counts and skipped ids from JUnit. Compare producing NEW-SKIP, LOST, MAIN-RED | `_gates_baseline.py` | about 200 |
| `scripts/dev/_gates_report.py` | Dispatch `--ref-check` and `--ref-fill` to `_gates_ref.py`; EVIDENCE `ref:` section | - | 493 now, under 530 |
| `.github/workflows/gate-selfhosted.yml` | Gate command `--keep-going --ref-cache ~/.cache/decoy-gate-ref`; exit 6 annotation; comment | - | about 75 |
| `tests/sentry/test_ci_config.py` | Assert the new command (line 137) | - | small |
| `tests/unit/scripts/test_gates_ref.py` (new) | A1 to A12 | `test_gates_baseline_file.py` | about 350 |
| `tests/unit/scripts/_gates_harness.py` | Helpers to seed a failing merge-base test and a cache file | - | 228 now, about 260 |

Size caps: `test_module_size.py` covers `*.py` under `src/` and `scripts/` (goal 600, max 700),
so `_gates_report.py` must stay under 600. `gates.sh` is not in that census; the 800-line project
cap applies. It is at 485 on origin/main, so the flag fits, but the compare and cache logic goes
in `_gates_ref.py` and the shell keeps only control flow. `test_gates_baseline_file.py` is
already 871 lines, so the new tests go in their own file.

## 8. Rollout

1. Land the code with the workflow command unchanged. Run A1 to A14 (T1 plus the T2 risk cells
   above). The slice touches test tooling, an escalation trigger, so the merge needs the full
   suite diffed against the saved main baseline.
2. Run the Mac full suite at main (section 6) and A12's real check on test-1, test-5 and the Mac.
3. Switch the workflow command. Watch the first pushes per runner: the `ref:` line and run times.

## 9. Open questions for the owner

1. A new test that is skipped on the branch: block it (default; stops a test being swapped for a
   skipped copy) or allow it? If blocked, a new Linux-only test is red on the Mac.
2. A failure on main that only one host has (e.g. macOS): exit 6 blocks every push that lands on
   that runner until main is fixed. Accept that, or take the runner off the `decoy-gate` label
   until the fix lands?
3. Should a push to main warm the cache with a full suite on each runner? Default no: it costs a
   full suite per runner per merge, and the fill already covers repeated pushes.
4. Key by merge base (default), or require branches to rebase onto the origin/main tip before the
   gate? The merge base avoids a fill after every merge but compares against an older main.
5. Exit 6 shows as a red job and an annotation. The workflow has no secrets, so a Slack ping for
   MAIN RED would come from node-watch reading the job result. Is that enough?
