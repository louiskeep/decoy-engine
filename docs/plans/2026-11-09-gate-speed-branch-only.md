Status: plan

# Merge gate: branch pass only, with a main reference per test file

Rules consulted: 00-universal, risk-and-exceptions, development-loop, testing (Test depth),
feature-dev. Risk: R2 (it changes what the merge gate lets through). Revision r2: resolves Dennis
r1 (see "Dennis r1 response").

Owner decision 2026-11-09: main is green. The stored Linux baselines
(`engine-main-baseline.json`, `cli-main-baseline.json`) record 0 failing. The self-hosted gate
(`.github/workflows/gate-selfhosted.yml`) runs `scripts/dev/gates.sh --keep-going --baseline`.
That runs every step on the branch and again on origin/main in a temporary worktree, so every run
costs about twice what it needs to. Only 3 runners exist (gcp-decoy-engine on test-1,
gcp-decoy-engine-2 on test-5, mac-decoy-engine), and the queue is the bottleneck. This plan makes
CI run the branch pass plus, only when needed, the pytest steps at the merge base for the files
the branch selected. Main's static steps never run.

Guarantee: CI blocks on any failing step or test, any test the branch newly skips, any main test
id the branch lost in a file it did not change, and any main test id the branch still has but did
not run. No branch can write the shared cache with data its own tooling or environment produced.
Non-goals: changing local `--baseline`, `--baseline-file` or `--write-baseline`; the CLI repo's
gate (follow-up once this lands); test selection itself.

## 1. Behavior

1. **Static steps are strict.** env, ruff, format, mypy and licenses get no main comparison. Any
   failure blocks (exit 1). Main is green, so any error on the branch belongs to the branch.
2. **One route.** For each pytest step (sentry, licenses, tests) the gate compares the branch with
   a main reference. The reference comes from the cache (hit) or from a fill at the merge base.
   The compare and the exit rules are the same either way. There is no `--baseline` fallback in
   CI; `--baseline` stays a local mode.
3. **Entries are per (step, file).** A step's file list is its path arguments (`SENTRY_PATHS`,
   `LICENSE_TESTS` or the selection `PATHS`) expanded to tracked `test_*.py` files. So
   `tests/sentry/test_license_policy.py` has one entry under sentry (non-strict) and another under
   licenses (`DECOY_LICENSE_STRICT=1`). Each entry holds: the ids that ran, the skipped ids with
   their skip reason, the failing ids, `workers`, and `absent: true` when the file does not exist
   at the merge base (so a new test file does not trigger a fill on every push).
4. **Compare, per entry:**
   - `NEW-SKIP <id>`: skipped on the branch, not skipped on main. Blocks. This covers a skip
     marker, a `pytest.skip()` in a body, fixture or conftest hook, and a new test that is skipped
     (default 1). A skip on both sides whose reason text changed is listed, not blocked.
   - `LOST <id>`: in a file the branch did not change, a main id that did not run on the branch.
     Blocks. Ids are compared, not counts, so a parametrize swap with the same count blocks.
   - `DESELECTED <id>`: in a file the branch changed, a main id that still exists but did not
     run. Blocks. The gate finds these with one `pytest --collect-only` over the branch's changed
     test files, without the `-m` filter (addopts cleared, `PYTEST_ADDOPTS` unset, as
     `deleted_test_ids` does). Adding `@pytest.mark.benchmark` to a failing test is caught here.
     A main id absent from that collection was removed in the diff; the EVIDENCE lists it.
   - Deleted files: the EVIDENCE lists each deleted file with its id count at the merge base
     (from `deleted_test_ids`, now per file).
   - Whole-suite totals are not compared. A selection runs a few dozen files, so a total against
     the whole suite says nothing.
5. **Cacheable or not.** A run may read and write the shared cache only when none of these differ
   between HEAD and the merge base:
   - tooling: `scripts/dev/gates.sh`, `scripts/dev/_gates*.py`, any `conftest.py` at or above a
     selected file;
   - environment: `uv.lock`, `pyproject.toml` (the whole file), `decoy-engine-native/`, and the
     ABI file `src/decoy_engine/execution/native/_crypto_ext.py`.

   If any differ, the run neither reads nor writes the cache. It fills every selected entry at the
   merge base into `$OUT/ref.json`, compares, and drops the file with `$OUT`. Main's tooling and
   environment then describe main, and nothing the branch produced reaches other runs.
6. **Key.** `<cache>/<repo>/<platform>/<merge-base-sha>-<env12>.json`. `platform` is
   `run_mode()["platform"]` (`_gates_baseline.py:37`). `env12` is the first 12 hex digits of a
   SHA-256 over the `uv.lock` hash, the Python version, and the companion ABI (or `absent`). The
   header repeats every key field and the loader checks them. A venv rebuilt with or without the
   companion gets a different key, so its native-only skips never mix with another state.
7. **Fill.** For entries the reference lacks, the gate adds a detached worktree at the merge base
   and runs only the pytest steps, each with only its missing files (`run_steps` gains a paths
   argument and a `ref` label). Files absent there are recorded absent without running. The fill
   is complete only if each pytest run exits 0, 1 or 5, writes its JUnit, and its totals line
   agrees with the JUnit. An incomplete fill exits 3 and writes nothing.
8. **Cache writes.** `_gates_ref.py` owns the cache. It takes an `fcntl` lock on
   `<cache>/<repo>/<platform>/.lock` around read, merge, write and prune. It writes a unique temp
   file in the same directory (name carries the gates.sh PID) and renames it. The gates.sh EXIT
   trap removes this run's temp file. The loader ignores `*.tmp.*`; prune deletes temps older than
   one hour and entries older than 14 days. The pytest-one lock is not relied on.
9. **Totals check.** With `--ref-cache`, the reporter applies today's totals-line-vs-JUnit check
   (`_gates_report.py:471-482`, exit 4 mapped to 3) to the branch tests JUnit as well as the fill.
10. **Main red.** A failing id in the reference means main fails it at the merge base. If those
   are the branch's only failures, the gate exits 6 and prints `gates: MAIN RED at <sha>: <ids>;
   fix main first`. If the branch has other failures too, it exits 1 and lists both groups. If
   the branch passes those ids, it exits 0 and the EVIDENCE notes the fix. The workflow turns exit
   6 into an `::error::` annotation titled "main is red". There is no skip list.
11. **Workers.** CI keeps `GATES_WORKERS=auto`. The reference records `workers` but does not
   refuse on it: ids and skip states should not depend on worker count or on what else ran in
   the selection. A15, A18 and rollout step 2 prove that before CI relies on it. CI must never
   point `--baseline-file` at the stored `*-main-baseline.json` files: they were written at
   `workers: 1` and would exit 2 at `auto`.
12. **Local use is unchanged.** `--baseline`, `--baseline-file` (used by `decoy-vm-t3 stored`)
   and `--write-baseline` keep their flags, file format and exit codes. The `Baseline` model,
   `MODE_KEYS` and `check_stored` do not change.

## 2. Failure modes

| Failure | Result |
| --- | --- |
| Branch adds a failing test | Exit 1, id under FAILING TEST IDS. |
| Branch skips an existing test (marker, body, fixture, conftest hook) | Exit 1, `NEW-SKIP <id>`. |
| src change drops or swaps an id in an unchanged test file | Exit 1, `LOST <id>`. |
| Branch adds an excluded marker to an existing test | Exit 1, `DESELECTED <id>`. |
| Collection error in a selected file | Error in the JUnit, exit 1 (as today). |
| Cache file corrupt, wrong format, key fields disagree | Treated as a miss; fill; rewritten under lock. |
| Leftover temp file from a cancelled run | Ignored by the loader, pruned after an hour. |
| Fill crashes, no JUnit, or totals disagree | Exit 3, cache untouched. |
| Branch edits tooling or the environment files | Uncached fill; cache neither read nor written. |
| Service down at fill time inflates main's skips | Hides a skip for that key only; reasons are stored; see pre-mortem. |
| Main fails a test, branch fails only that | Exit 6, MAIN RED. |
| Main fails a test, branch has its own failures too | Exit 1, both listed. |
| Static step fails on one host only (e.g. Mac mypy) | Exit 1 on that runner; rollout step 2, question 6. |
| Two processes write one key | Serialized by the tool's lock; both entries kept. |

## 3. Where main's reference comes from

- **A. Committed in the repo.** A file about main SHA X cannot be part of commit X. It needs a
  bot commit after every merge, and parallel branches would conflict on it. Rejected.
- **B. CI artifact from a post-merge main run.** One full suite per platform per merge, an
  `actions: read` token, a run-id lookup by SHA, and a window after each merge with no reference.
  One shared Linux artifact also assumes test-1 and test-5 skip the same tests. Rejected.
- **C. Computed at the merge base on the runner, cached per environment.** Behavior 5 to 8.

**Recommend C.** The reference comes from the same host and venv as the branch run, so
host-dependent skips cancel out, and the Mac gets its own reference. Cost follows use: only
selected pytest files that are not cached run at main, and repeated pushes hit the cache. No
token or post-merge job, so the workflow stays free of `secrets.` (`tests/sentry/test_ci_config.py`).
A merge-base key cannot go stale; tooling and environment changes bypass the cache.

## 4. Established solutions

Searched: per-commit cached test data in CI (pytest-testmon, Bazel remote cache). pytest-testmon
users store its dependency database per commit hash
([write-up](https://medium.com/@boxed/vastly-faster-python-integration-tests-9d8106b3693c),
[pytest-testmon](https://pypi.org/project/pytest-testmon)). Bazel caches a test result keyed by
all its inputs, including toolchain and environment
([remote cache](https://bazel.build/remote/cache-remote)). Option C follows Bazel: the key is the
commit plus an environment digest, and inputs that are not cheap to hash (tooling) skip the cache.
Neither tool checks skips or lost ids against a reference. The custom part is a per-file id and
skip compare over the JUnit files the gate already writes.

## 5. Design notes

- `_gates_junit.py` (new, public): the JUnit readers moved out of `_gates_report.py` (`xmls`,
  `roots`, `suites_of`, `locate`, `case_id`, `failing_ids_of`, `pytest_totals_failed`,
  `junit_failure_elements`). `_gates_report.py` and `_gates_ref.py` both import it. No cycle.
- `_gates_ref.py` (new): reference format, strict loader, key, lock, merge, atomic write, prune,
  compare. gates.sh calls it directly; `_gates_report.py` only prints its result file into the
  EVIDENCE.
- gates.sh keeps control flow only: cacheable check, fill worktree, collect-only of changed test
  files, exit code.
- One choke point for "CI runs in ref mode": the workflow command, pinned by
  `test_ci_config.py`.

## Class table

| # | Class | Applies? | Structure that closes it | Proof | Mutation that turns it red |
| --- | --- | --- | --- | --- | --- |
| 1 | value-bearing output | N/A: cache and EVIDENCE hold test ids, skip reasons, counts and SHAs only | - | - | - |
| 2 | cleanup on exit paths | Yes: fill worktree, temp file | EXIT trap removes both; loader ignores and prune removes stale temps | A13, A14 | Drop the temp file from `cleanup` |
| 3 | state and concurrency | Yes: one cache file per key | `fcntl` lock in `_gates_ref.py`, unique temp then rename | A13 (concurrent writer, leftover temp) | Write in place without the lock |
| 4 | route parity | Yes: cache hit vs fill | One compare and one exit rule for both; the uncached fill runs the same compare | A11, A12 | Skip the compare on the uncached route |
| 5 | referential integrity | N/A: no data relations in gate tooling | - | - | - |
| 6 | untrusted producer | Yes: cache content and who writes it | Cacheable check; env digest in the key; strict loader | A12, A13 | Write the cache when `uv.lock` differs |
| 7 | toctou | Yes: read, merge and write of one key | All three under one lock hold | A13 | Release the lock between read and write |
| 8 | weak tests | Yes: skip, lost, deselect checks | Id-level compare; real-pytest e2e file | A2 to A7, A18 | Compare counts instead of ids |
| 9 | refactor drift | Yes: local modes, moved JUnit readers | Existing gates tests stay untouched | A16 | Change the id format in the moved `case_id` |
| 10 | frozen contract drift | Yes: stored baseline format | `Baseline`, `MODE_KEYS` untouched; new format in its own module | A16 | Add a field to `Baseline` |

## Enumerations

### Exit paths of gates.sh

- 0 pass; 1 branch failure; 2 usage or refused input; 3 incomplete (no EVIDENCE, incomplete fill,
  totals mismatch); new 6 MAIN RED only.
- The fill worktree joins `$WT` and `$MBT` in the EXIT trap; so does this run's temp file.

### Routes through the gate

- Ref mode (new, CI). Reference from a cache hit, a cached fill, or an uncached fill; one compare.
- Local `--baseline`, verdict from `_gates_report.py:266` `branch_only_steps`. Unchanged.
- Local `--baseline-file`, diff from `_gates_report.py:309` `_stored_diff`, mode checked by
  `_gates_baseline.py:152` `check_stored`. Unchanged.

### FK shapes

N/A: gate tooling handles no relational data.

### Fields of the reference file

- Top: `format_version`, `repo`, `platform`, `main_sha` (the merge base), `env` (lock hash,
  Python version, companion ABI or `absent`), `written_at`.
- Per (step, file): `ids`, `skipped` (id to reason), `failing`, `workers`, `absent`. Ids come from
  `case_id`, the same ids the stored baseline uses.

### Output surfaces

- EVIDENCE: `ref:` header line (hit, cached fill, or uncached fill and why), then NEW-SKIP, LOST,
  DESELECTED, MAIN-RED, removed ids, deleted files with id counts, changed skip reasons.
- stderr `gates:` messages, the workflow `::error::` annotation on exit 6, the cache file.

## Reuse inventory

- JUnit readers, moved to `_gates_junit.py`.
- `run_steps` for the fill, with a `ref` label and per-step paths.
- `deleted_test_ids`, extended to report per file.
- The EXIT trap, `run_mode()["platform"]`, and the bootstrap ABI probe (`bootstrap.sh:61-62`).

## Frozen contracts

- Stored baseline JSON (`Baseline`, `format_version` 1, `MODE_KEYS`) read by `decoy-vm-t3`.
- gates.sh flags `--baseline`, `--baseline-file`, `--write-baseline`, `--order-from` and exit
  codes 0 to 3. New code is additive: one flag, one exit code.

## Slices

1. `_gates_junit.py` move (no behavior change; A16 green), then `_gates_ref.py` with unit tests.
2. gates.sh `--ref-cache`, cacheable check, fill, collect-only, exit 6; A1 to A15, A17, A18.
3. Workflow switch and `test_ci_config.py` (A19), after rollout steps 2 and 3.

## Pre-mortem

- A service is down at fill time, so main's skip set is inflated and a real branch skip of that
  test passes for that key. Stored reasons make it visible; the owner can delete the key. Watch
  the first week.
- A runtime skip depends on order or selection under xdist: A15, A18 and rollout step 2.
- A runner is red on main for a static step and nobody knew: rollout step 2.
- The branch's own gates.sh runs the fill and the compare. A branch that edits gates.sh gets an
  uncached fill, so it cannot poison the cache, but a hostile edit could still fake its own pass.
  The tooling rule protects against honest drift only. That fits the V1 threat model (accidental
  leaks), and review sees every gates.sh change.

## 6. Acceptance tests

`tests/unit/scripts/test_gates_ref.py` uses `_gates_harness.py` (scratch clone, fake pytest
driven by `FAKE_*`, extended to emit per-case skip and id lists). Each case asserts the `ref:`
line too. `tests/unit/scripts/test_gates_ref_e2e.py` uses real pytest from the venv.

| # | Seed | Expected |
| --- | --- | --- |
| A1 | Branch adds a failing test | Exit 1. No ruff, format or mypy logs at main. |
| A2 | `@pytest.mark.skip` on an existing passing test | Exit 1, `NEW-SKIP`. |
| A3 | `pytest.skip()` in an existing test body | Exit 1, `NEW-SKIP`. |
| A4 | New test that is skipped | Exit 1 (default 1). |
| A5 | src change drops a parametrize case in an unchanged test file | Exit 1, `LOST`. |
| A6 | Same-count parametrize id swap in an unchanged file | Exit 1, `LOST`. |
| A7 | `@pytest.mark.benchmark` on an existing failing test in a changed file | Exit 1, `DESELECTED`. |
| A8 | Branch deletes a test from a file it changed; branch deletes a test file | Exit 0; removed id, and the file with its id count, listed. |
| A9 | Empty cache | Fill runs only missing (step, file) entries at the merge base; a rerun creates no worktree. A new test file is recorded absent; the next push does not fill. |
| A10 | License test file in both sentry and licenses steps, skipped in one only | Two entries, no NEW-SKIP. Changing the skip state of either one blocks. |
| A11 | Conftest hook that skips a test in an unchanged file | Uncached fill, `NEW-SKIP`, exit 1. Cache bytes and mtime unchanged. |
| A12 | Branch edits `_gates_report.py`; branch edits `uv.lock` | Uncached fill each time; cache unchanged. |
| A13 | Corrupt file, wrong `format_version`, other platform, other env digest; leftover temp; two concurrent writers | Each bad file is a miss and is replaced by a full write. Temp ignored, then pruned. Both writers' entries present. |
| A14 | Fill pytest exits 2; fill totals disagree with JUnit; branch tests totals disagree | Exit 3; cache unchanged; no worktree left. |
| A15 | Cache filled at `GATES_WORKERS=1`, compared at 2 | No exit 2, no NEW-SKIP or LOST. |
| A16 | `test_gates_baseline_file.py`, `test_gates_parallel.py`, `test_dev_bootstrap_gates.py` | Unchanged and green. |
| A17 | Merge base fails `test_x`; branch fails only it / it plus `test_y` / neither | Exit 6 / exit 1 listing both / exit 0 with a note. |
| A18 | e2e, real pytest at workers 1 and 2: marker skip, runtime skip, xfail on both sides, benchmark marker in a changed file, parametrize loss in an unchanged file, the A10 two-step file; one file filled alone vs inside a larger selection | Each seeded regression blocks with the right line; xfail and unchanged cases raise nothing; the alone and in-selection entries are equal. |
| A19 | `tests/sentry/test_ci_config.py` | Command is `gates.sh --keep-going --ref-cache ~/.cache/decoy-gate-ref`; the exit-6 handler writes `::error::` and re-exits 6, so the job still fails. |

Mutations for the T2 gate: drop the skip compare (A2, A3), compare counts not ids (A6), drop the
collect-only (A7), write the cache when uncacheable (A11, A12), drop the lock (A13), map an
incomplete fill to 0 (A14), drop the exit-6 branch (A17), revert the workflow command (A19).

## 7. Placement

CODEMAP "Where Things Go", row "Dev tooling": `scripts/dev/`, named by tool concern, sized by
`tests/sentry/test_module_size.py`. Row "Unit tests": `tests/unit/scripts/`. Neither directory is
near a fan-out limit (15 and 8 files).

| File | Change | Beside | Size after |
| --- | --- | --- | --- |
| `scripts/dev/gates.sh` | `--ref-cache DIR`; cacheable check; fill with per-step paths (`run_steps` paths argument, `ref` label); collect-only; per-file deleted counts; exit 6; usage text | - | 485 now, about 570 |
| `scripts/dev/_gates_junit.py` (new) | JUnit readers moved from `_gates_report.py`, public names | `_gates_report.py` | about 140 |
| `scripts/dev/_gates_ref.py` (new) | Format, loader, key, lock, merge, atomic write, prune, compare | `_gates_baseline.py` | about 260 |
| `scripts/dev/_gates_report.py` | Import readers; print the ref result; totals check under `--ref-cache` | - | 493 now, about 380 |
| `.github/workflows/gate-selfhosted.yml` | New command; exit-6 handler; comment | - | about 80 |
| `tests/sentry/test_ci_config.py` | Assert the new command and handler (line 137) | - | small |
| `tests/unit/scripts/test_gates_ref.py` (new) | A1 to A17 | `test_gates_baseline_file.py` | about 420 |
| `tests/unit/scripts/test_gates_ref_e2e.py` (new) | A18 | `test_gates_ref.py` | about 200 |
| `tests/unit/scripts/_gates_harness.py` | Per-case skip and id output; seed a failing merge-base test and a cache file | - | 228 now, about 280 |

Size caps: `test_module_size.py` covers `*.py` under `src/` and `scripts/` (goal 600, max 700).
gates.sh is not in that census; the 800-line project cap applies, so compare and cache logic goes
in `_gates_ref.py`. `test_gates_baseline_file.py` is already 871 lines, so new tests get their own
files.

## 8. Rollout

1. Land slices 1 and 2 with the workflow command unchanged. The slice touches test tooling, an
   escalation trigger, so the merge needs the full suite diffed against the saved main baseline.
2. On test-1, test-5 and the Mac, at one main SHA: run `gates.sh --full` and confirm every step
   is green, static steps included (today `--baseline` excuses shared mypy or ruff fails on a
   host). Run main at `GATES_WORKERS=1` and `auto`, and one file alone vs the full run; per-file
   ids and skipped ids must match. Record each runner's bash version, `flock` path and companion
   state (the `native:` line of `bootstrap.sh --check`).
3. Run `test_gates_ref.py` and the e2e file on the Mac with the bash the runner uses. New shell
   code avoids bash 4 features (associative arrays, `mapfile`), and locking is `fcntl` in Python,
   so the cache needs no `flock` binary.
4. Switch the workflow command. Watch the first pushes per runner: the `ref:` line and run times.

## 9. Owner questions

Defaults the owner set, each still open to override:

1. A new test that is skipped on the branch blocks. A new Linux-only test is then red on the Mac
   unless it is skipped there by a marker main already uses.
2. A main failure that only one host has: accept exit 6 on that runner until main is fixed.
3. No cache pre-fill on a push to main. The fill covers repeated pushes.
4. The cache is keyed by merge base, not the origin/main tip.
5. node-watch sends a Slack ping when a gate job ends with exit 6. That change is in node-watch,
   outside this repo; it is a follow-up, not a merge condition.

New questions:

6. A static step red on one host only (rollout step 2 finds it): fix it before the switch
   (recommended), or keep that runner off the `decoy-gate` label until fixed?
7. Native companion: should every gate venv have it installed, so native-only tests run in CI, or
   is "absent" acceptable on some runners? Bootstrap only checks it today (`bootstrap.sh:64-69`).

## Follow-ups (V2-LOG)

- Route the stored `--baseline-file` compare (`_stored_diff`, `_gates_report.py:309-345`, and the
  shortfall logic in `gates.sh:456-469`) through the per-file compare, once the stored format may
  change. Two compares exist until then.
- Cite `_gates_baseline.py:37` (`run_mode()` takes arguments, `:33`) wherever a plan quotes the
  platform expression. Done in this plan.
- CLI repo gate: same design.
- node-watch Slack ping on exit 6 (default 5).

## Dennis r1 response

| Finding | Resolution | Where |
| --- | --- | --- |
| H1 fallback weakest | Adopted. One route; uncached fill into `$OUT`; same compare; static steps strict; `--baseline` local only | §1.2, §1.5, A11, A12 |
| H2 cache poisoning | Adopted. Cacheable check; env digest in key; tooling and env changes bypass the cache; skip reasons stored | §1.3, §1.5, §1.6, A12, A13 |
| H3 key collides across steps | Adopted. Key (step, file); `run_steps` paths argument and `ref` label | §1.3, §1.7, A10 |
| H4 deselect and id swap | Adopted. Id sets; LOST on ids; collect-only for DESELECTED | §1.4, A6, A7 |
| M1 import cycle | Adopted. `_gates_junit.py`; gates.sh calls `_gates_ref.py` directly | §5, §7 |
| M2 lock in the tool | Adopted. `fcntl` lock, unique temp, EXIT trap, prune | §1.8, A13 |
| M3 totals check | Adopted for branch tests and fill | §1.9, A14 |
| M4 host-only static red | Adopted. Full run on all three runners before the switch; question 6 | §8.2, §9 |
| M5 exit parity | Moot under one route; one exit rule | §1.10 |
| M6 fake pytest only | Adopted. Real-pytest e2e file | A18, §7 |
| L1 branch fail plus main red | Exit 1, both listed | §1.10, A17 |
| L2 bash -e handler | Handler captures, annotates, re-exits 6 | A19 |
| L3 deleted files | Per-file id counts in EVIDENCE | §1.4, A8 |
| L4 files absent at merge base | Recorded `absent` | §1.3, A9 |
| L5 branch runs its own gates.sh | Stated; V1 threat model | Pre-mortem |
| L6 platform citation | Follow-up, and cited in §1.6 | Follow-ups |
| Sameness | Follow-up | Follow-ups |
| Not verified: worker and selection stability | A15, A18, rollout step 2 | §6, §8 |
| Not verified: Mac bash 3.2 and flock | Rollout step 3; no bash 4 features; `fcntl` lock | §8 |
| Not verified: companion per runner | Env digest; rollout step 2 records it; question 7 | §1.6, §9 |
