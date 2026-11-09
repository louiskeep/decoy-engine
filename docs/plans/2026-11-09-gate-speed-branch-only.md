Status: plan

# Merge gate: branch pass only, with a main reference per test file

Rules consulted: 00-universal, risk-and-exceptions, development-loop, testing (Test depth),
feature-dev. Risk: R2 (it changes what the merge gate lets through). Revision r3: resolves Dennis
r1 and Codex r1 (see the response tables at the end).

Owner decision 2026-11-09: main is green. The stored Linux baselines
(`engine-main-baseline.json`, `cli-main-baseline.json`) record 0 failing. The self-hosted gate
(`.github/workflows/gate-selfhosted.yml`) runs `scripts/dev/gates.sh --keep-going --baseline`.
That runs every step on the branch and again on origin/main in a temporary worktree, so every run
costs about twice what it needs to. Only 3 runners exist (gcp-decoy-engine on test-1,
gcp-decoy-engine-2 on test-5, mac-decoy-engine), and the queue is the bottleneck. This plan makes
CI run the branch pass, plus small pytest runs at the merge base only where the branch's result
needs a main fact. Main's static steps never run.

Guarantee: CI blocks on any failing step or test, any test the branch newly skips, and any merge-base
test id the branch no longer runs unless the diff retires it in `tests/RETIRED_TEST_IDS`. A skip or
failure is excused as main's only after a fresh run at the merge base shows it. The shared cache
holds only collected ids, written only by runs whose producer inputs equal the merge base. Non-goals:
changing local `--baseline`, `--baseline-file` or `--write-baseline` behavior; the CLI repo's gate
(follow-up); test selection itself.

## 1. Behavior

1. **Static steps are strict.** env, ruff, format, mypy and licenses get no main comparison. Any
   failure blocks (exit 1). Main is green, so any error on the branch belongs to the branch.
2. **One route.** For each pytest step (sentry, licenses, tests) the gate compares the branch with
   a main reference. The reference comes from the cache or from a fill at the merge base. The
   compare and exit rules are the same either way. `--baseline` stays a local mode.
3. **Entries are per (step, file).** A step's file list is its path arguments (`SENTRY_PATHS`,
   `LICENSE_TESTS` or the selection `PATHS`) expanded to tracked `test_*.py` files, plus, for the
   tests step, test files the branch deleted. So `tests/sentry/test_license_policy.py` has one
   entry under sentry and another under licenses (`DECOY_LICENSE_STRICT=1`). An entry holds the
   collected ids that ran (any outcome) and `absent: true` when the file does not exist at the
   merge base. It holds no outcomes, no skip state and no text.
4. **Compare, per entry.** Ids under a path the branch renamed (`git diff -M`) are rewritten to the
   new path first.
   - `MISSING <id>`: a merge-base id that did not run on the branch, in any file: unchanged,
     changed or deleted. This covers a src change that drops a parametrize case, a same-count id
     swap, an excluded marker (`benchmark`, `packaging`, `codspeed`) added to a test, and a
     deleted test. Blocks, unless the id is on a line this diff adds to `tests/RETIRED_TEST_IDS`
     (default 8). Retired ids are listed as `RETIRED <id>`. Lines already on main excuse nothing.
   - `NEW-SKIP <id>`: skipped on the branch (xfail included) and not skipped in the recheck.
     Blocks. A new test that is skipped blocks (default 1).
   - `MAIN-RED <id>`: failing on the branch and failing in the recheck.
   - Whole-suite totals are not compared in ref mode. A selection runs a few dozen files.
5. **Recheck.** The cache never says what main skips or fails. When the branch skips or fails
   ids that exist at the merge base, the gate runs exactly those node ids at the merge base, per
   step, with that step's environment. Only the recheck can excuse a skip or name a main red. A
   main skip during a service outage therefore cannot excuse a later branch skip. Cost: one git
   worktree and one pytest process over the listed ids. Skipped tests run in milliseconds, so it is
   mostly pytest start and collection of those files; rollout step 2 measures it.
6. **Producer inputs.** `_gates_ref.py` holds one manifest, `PRODUCER_INPUTS`, of paths that build
   or drive the reference, each marked `env` or `tooling`:
   - env: `uv.lock`, `pyproject.toml`, `decoy-engine-native/**`,
     `src/decoy_engine/execution/native/_crypto_ext.py`, `scripts/dev/bootstrap.sh`,
     `scripts/dev/lock_version.sh`;
   - tooling: `scripts/dev/gates.sh`, `scripts/dev/_gate*.py` (covers `_gate_workers.py`),
     `.github/workflows/gate-selfhosted.yml`, any `conftest.py` at or above a selected file.

   The same manifest decides cacheability and builds the key. A guard test walks the real producer
   closure (the workflow's `run:` scripts, `scripts/dev/*` paths named in gates.sh and
   bootstrap.sh, sibling imports of `scripts/dev/*.py`) and fails if a file it finds matches no
   entry.
7. **Cacheable or not.** If no manifest input differs between HEAD and the merge base, the run
   reads and writes the cache and fills with the gate venv, which then matches the merge base.
   Otherwise it neither reads nor writes the cache, fills every entry into `$OUT/ref.json`, and
   drops it with `$OUT`:
   - tooling differs only: fill and recheck use the gate venv;
   - an env input differs: the gate builds a disposable merge-base venv in `$OUT/ref-venv` with the
     merge-base `bootstrap.sh` (which reads the merge-base `lock_version.sh`, `pyproject.toml` and
     `uv.lock`). If the gate venv has the native companion, the gate also builds it from the
     merge-base `decoy-engine-native/`. Fill and recheck use that venv. Cost: a `uv sync` from the
     warm uv cache (hard links, expected under a minute) and, with the companion, an incremental
     Rust build (minutes). Only branches that touch an env input pay it.
8. **Key.** `<cache>/<repo>/<platform>/<merge-base-sha>-<digest>.json`. `platform` is
   `run_mode()["platform"]` (`_gates_baseline.py:37`). `digest` is the full SHA-256 over the
   manifest version, the merge-base blob ids of the manifest inputs, the Python version, and the
   companion ABI (or `absent`). The header repeats every key field and the loader checks them.
9. **Fill.** For missing entries the gate adds a detached worktree at the merge base and runs only
   the pytest steps, each with only its missing files (`run_steps` gains a paths argument and a
   `ref` label). Absent files are recorded without running. The fill is complete only if each run
   exits 0, 1 or 5, writes its JUnit, and its totals line agrees with the JUnit. Otherwise exit 3
   and nothing is written. The same worktree serves the recheck.
10. **JUnit readers take a tree root.** `_locate` resolves an inherited test against `TEST_ROOT`,
   the checkout that holds the reader (`_gates_report.py:125-134`). Every reader in
   `_gates_junit.py` takes the producer's tree root instead. Fill and recheck results pass the
   merge-base worktree; branch results pass `$ROOT`.
11. **Cache store.** `_gates_ref.py` owns the cache. Directories are created `0700` and files
   `0600`. The loader refuses (treats as a miss, and says why) a file or directory not owned by the
   current user or with group or other bits. It takes an `fcntl` lock on
   `<cache>/<repo>/<platform>/.lock` around read, merge, write and prune, writes a unique temp file
   (name carries the gates.sh PID) and renames it. The gates.sh EXIT trap removes this run's temp.
   The loader ignores `*.tmp.*`; prune deletes temps older than an hour and entries older than 14
   days. The pytest-one lock is not relied on.
12. **Totals check.** With `--ref-cache`, today's totals-line-vs-JUnit check
   (`_gates_report.py:471-482`, exit 4 mapped to 3) applies to the branch tests, the fill and the
   recheck.
13. **Exit.** If every branch failure is a MAIN-RED id, exit 6 and print `gates: MAIN RED at
   <sha>: <ids>; fix main first`. If the branch has other failures too, exit 1 and list both. A
   branch that passes an id main fails exits 0 with a note. The workflow turns exit 6 into an
   `::error::` annotation titled "main is red". There is no skip list.
14. **Workers.** CI keeps `GATES_WORKERS=auto`. Entries hold collected ids only, which pytest
   fixes at collection, so worker count does not change them. A22 and rollout step 2 confirm it.
   CI never points `--baseline-file` at the stored `*-main-baseline.json` files (written at
   `workers: 1`).
15. **Local modes keep their contracts.** `--baseline`, `--baseline-file` and `--write-baseline`
   keep their flags, the stored JSON format, their EVIDENCE lines and exit codes. `--baseline-file`
   now reaches its verdict through the shared compare engine (design note 3).

## 2. Failure modes

| Failure | Result |
| --- | --- |
| Branch adds a failing test | Exit 1. |
| Branch skips an existing test (marker, body, fixture, conftest hook) | Recheck shows main runs it: exit 1, `NEW-SKIP`. |
| Id lost, swapped, deselected or deleted in any file | Exit 1, `MISSING`, unless retired in this diff. |
| Test file renamed with the same tests | Ids rewritten to the new path; no finding. |
| Main skipped a test during an outage when the cache was filled | The cache holds no skip state; the recheck decides. |
| Branch dependency change makes a test skip | Reference built in a merge-base venv; `NEW-SKIP`. |
| Branch edits a producer input | No cache read or write; merge-base venv when it is an env input. |
| Cache file corrupt, wrong format, key fields disagree, bad owner or mode | Treated as a miss, reason printed. |
| Leftover temp file from a cancelled run | Ignored, pruned after an hour. |
| Fill or recheck crashes, no JUnit, totals disagree | Exit 3, cache untouched. |
| Main fails a test, branch fails only that | Exit 6. With other branch failures, exit 1, both listed. |
| Static step red on one host only | Exit 1 on that runner; rollout step 2, question 6. |
| Two processes write one key | Serialized by the tool's lock; both entries kept. |

## 3. Where main's reference comes from

- **A. Committed in the repo.** A file about main SHA X cannot be part of commit X; it needs a bot
  commit after every merge, and parallel branches conflict on it. Rejected.
- **B. CI artifact from a post-merge main run.** One full suite per platform per merge, an
  `actions: read` token, a run-id lookup, and a window after each merge with no reference.
  Rejected.
- **C. Computed at the merge base on the runner, ids cached per producer state.** Behavior 3 to 11.

**Recommend C.** Same host as the branch run, so host-dependent behavior cancels out and the Mac
gets its own reference. Cost follows use: uncached pytest files and rechecked ids only. No token
or post-merge job, so the workflow stays free of `secrets.` (`tests/sentry/test_ci_config.py`).

## 4. Established solutions

Searched: per-commit cached test data in CI (pytest-testmon, Bazel remote cache). pytest-testmon
users store its dependency database per commit hash
([write-up](https://medium.com/@boxed/vastly-faster-python-integration-tests-9d8106b3693c),
[pytest-testmon](https://pypi.org/project/pytest-testmon)). Bazel keys a cached action by the
digest of all its declared inputs, toolchain included, and keeps cached results to deterministic
facts ([remote cache](https://bazel.build/remote/cache-remote)). Option C follows Bazel: a declared
input manifest in the key, and only deterministic facts (collected ids) cached; outcomes that can
flake (skips, failures) are recomputed. Neither tool checks skips or lost ids against a reference.
The custom part is the per-file compare over the JUnit files the gate already writes.

## 5. Design notes

1. `_gates_junit.py` (new, shared): the JUnit readers moved out of `_gates_report.py`, each taking
   a tree root. `_gates_report.py`, `_gates_compare.py` and `_gates_ref.py` import it. No cycle.
2. `_gates_ref.py` (new): `PRODUCER_INPUTS`, key, store (modes, lock, atomic write, prune) and the
   strict loader. It owns no compare logic.
3. `_gates_compare.py` (new): the one compare engine. Input: a main side and a branch side, each a
   map from scope to facts (`ids`, `count`, `failing`, `skipped`, `skip_count`), where any fact
   may be `UNKNOWN`. It emits NEW-FAILING, NOW-PASSING, MISSING, RETIRED, NEW-SKIP, MAIN-RED, a
   collected shortfall and a skip increase. A rule whose facts are unknown on either side falls
   back to the count rule, or is skipped and says so. Two adapters feed it: the ref adapter (scope
   = (step, file); main ids from the reference, main skip and failing state from the recheck) and
   the stored adapter (scope = whole tests step from the frozen `Baseline`: `count`, `skip_count`,
   `failing` known, ids unknown). `_stored_diff` becomes a call to the engine with the stored
   adapter and keeps its printed lines and the `new-failing`, `shortfall` and `skip-increase`
   files byte for byte. The shell's deleted-test count stays input gathering, not a compare.
4. gates.sh keeps control flow: cacheability, venv choice, worktree, fill, recheck, exit code.
5. One choke point for "CI runs in ref mode": the workflow command, pinned by
   `test_ci_config.py`. One for "what the reference depends on": `PRODUCER_INPUTS`, pinned by the
   closure guard (A15).

## Class table

| # | Class | Applies? | Structure that closes it | Proof | Mutation that turns it red |
| --- | --- | --- | --- | --- | --- |
| 1 | value-bearing output | Yes: skip reasons and failure text can hold values (`tests/sentry/test_exception_hierarchy.py:80`) | The cache holds ids only; EVIDENCE prints ids only; dir `0700`, file `0600`, owner and mode checked | A20 (canary) | Store the skip message in an entry |
| 2 | cleanup on exit paths | Yes: worktree, temp file, ref venv | EXIT trap removes all three | A16, A17 | Drop the temp file from `cleanup` |
| 3 | state and concurrency | Yes: one cache file per key | `fcntl` lock, unique temp then rename | A16 | Write in place without the lock |
| 4 | route parity | Yes: cache hit vs fill | One engine, one exit rule; the recheck runs on both | A12, A14 | Skip the recheck on a cache hit |
| 5 | referential integrity | N/A: no data relations in gate tooling | - | - | - |
| 6 | untrusted producer | Yes: who writes the cache and with what | `PRODUCER_INPUTS` gates writes and builds the key; merge-base venv on env change; strict loader | A13, A14, A15 | Drop `lock_version.sh` from the manifest |
| 7 | toctou | Yes: read, merge, write of one key | All under one lock hold | A16 | Release the lock between read and write |
| 8 | weak tests | Yes: skip, missing, main-red checks | Id-level compare; fresh recheck; real-pytest e2e | A2 to A9, A18, A24 | Excuse a skip from cached state |
| 9 | refactor drift | Yes: moved readers, stored mode on the engine | Existing gates tests stay untouched and green | A21, A23 | Resolve reference ids against the branch root |
| 10 | frozen contract drift | Yes: stored baseline JSON and its EVIDENCE lines | `Baseline`, `MODE_KEYS` untouched; adapter only reads | A23 | Add a field to `Baseline` |
| 11 | duplicate mechanism | Yes: two compare paths | One engine, two adapters | A23 | Restore the old `_stored_diff` body |

## Enumerations

### Exit paths of gates.sh

- 0 pass; 1 branch failure; 2 usage or refused input; 3 incomplete (no EVIDENCE, incomplete fill or
  recheck, totals mismatch); new 6 MAIN RED only.
- The EXIT trap removes `$WT`, `$MBT`, the fill worktree, this run's temp file and `$OUT/ref-venv`.
- The reporter exits 4 on a totals mismatch (`scripts/dev/_gates_report.py:482`); gates.sh maps it to 3.

### Routes through the gate

- Ref mode (new, CI): cache hit, cached fill, uncached fill with the gate venv, uncached fill with
  a merge-base venv. All end in the same recheck and engine.
- Local `--baseline`: step verdict from `scripts/dev/_gates_report.py:266` `branch_only_steps`.
  Unchanged; it compares step results, not test facts.
- Local `--baseline-file`: engine with the stored adapter; mode still checked by
  `scripts/dev/_gates_baseline.py:152` `check_stored`.

### FK shapes

N/A: gate tooling handles no relational data.

### Fields of the reference file

- Top: `format_version`, `repo`, `platform`, `main_sha`, `digest` and its parts (manifest version,
  Python version, companion ABI or `absent`), `written_at`.
- Per (step, file): `ids`, `absent`.

### Output surfaces

- EVIDENCE: `ref:` line (hit, cached fill, uncached fill and which input, venv used), then
  MISSING, RETIRED, NEW-SKIP, MAIN-RED and recheck counts. Ids only.
- stderr `gates:` messages, the workflow `::error::` annotation on exit 6, the cache file.

## Reuse inventory

- JUnit readers, moved and given a root parameter.
- `run_steps` for fill and recheck, with a `ref` label and per-step paths.
- `bootstrap.sh` from the merge-base tree for the disposable venv.
- The EXIT trap, `run_mode()["platform"]`, and the bootstrap ABI probe (`bootstrap.sh:61-62`).

## Frozen contracts

- Stored baseline JSON (`Baseline`, `format_version` 1, `MODE_KEYS`) read by `decoy-vm-t3`, and
  the stored-mode EVIDENCE lines.
- gates.sh flags `--baseline`, `--baseline-file`, `--write-baseline`, `--order-from` and exit
  codes 0 to 3. New code is additive: one flag, one exit code.

## Slices

1. `_gates_junit.py` with root parameters; `_gates_compare.py` with the stored adapter;
   `_stored_diff` moved onto it (no behavior change; A21, A23 green).
2. `_gates_ref.py` and gates.sh `--ref-cache`: manifest, venv choice, fill, recheck, exit 6;
   A1 to A20, A22, A24.
3. Workflow switch and `test_ci_config.py` (A25), after rollout steps 2 and 3.

## Pre-mortem

- A runtime skip depends on the rest of the selection, so the recheck (ids alone) does not skip
  where the branch run did: a false NEW-SKIP, which blocks rather than hides. A24 and rollout step
  2 sample it.
- A runner is red on main for a static step and nobody knew: rollout step 2.
- The branch's own gates.sh runs the fill, recheck and compare. A branch that edits it cannot
  write the cache, but a hostile edit could fake its own pass. The manifest protects against honest
  drift only. That fits the V1 threat model (accidental leaks), and review sees every gates.sh
  change.
- A guard walk misses a dynamic reference (a path built at runtime). The manifest lists whole
  globs (`scripts/dev/_gate*.py`) to narrow that gap.

## 6. Acceptance tests

`tests/unit/scripts/test_gates_ref.py` uses `_gates_harness.py` (scratch clone, fake pytest driven
by `FAKE_*`, extended to emit per-case ids and outcomes and a stub bootstrap). Each case asserts
the `ref:` line. `tests/unit/scripts/test_gates_ref_e2e.py` uses real pytest from the venv.

| # | Seed | Expected |
| --- | --- | --- |
| A1 | Branch adds a failing test | Exit 1. No ruff, format or mypy logs at main. |
| A2 | `@pytest.mark.skip` on an existing passing test | Exit 1, `NEW-SKIP`. |
| A3 | `pytest.skip()` in an existing test body | Exit 1, `NEW-SKIP`. |
| A4 | New test that is skipped | Exit 1 (default 1). |
| A5 | src change drops a parametrize case in an unchanged file | Exit 1, `MISSING`. |
| A6 | Same-count parametrize swap, in an unchanged file and in a changed file | Exit 1, `MISSING`. |
| A7 | `@pytest.mark.benchmark` on an existing failing test in a changed file | Exit 1, `MISSING`. |
| A8 | Branch deletes a test; deletes a test file | Exit 1, `MISSING`. With the ids added to `tests/RETIRED_TEST_IDS` in the diff: exit 0, `RETIRED`. With the ids already on main's list: exit 1. |
| A9 | Test file renamed with the same tests; renamed with one test dropped | Exit 0; exit 1 `MISSING`. |
| A10 | Empty cache | Fill runs only missing (step, file) entries; a rerun with no branch skips or failures creates no worktree. A new file is recorded absent; the next push does not fill. |
| A11 | License file in both sentry and licenses steps, skipped in one only | Two entries; recheck per step; no NEW-SKIP. A new skip in either one blocks. |
| A12 | Conftest hook that skips a test in an unchanged file | Uncached fill, `NEW-SKIP`, exit 1. Cache bytes and mtime unchanged. |
| A13 | Branch edits each `PRODUCER_INPUTS` entry in turn (parametrized) | No cache read or write. Env entries: `ref:` names `$OUT/ref-venv`, built by the merge-base bootstrap stub. |
| A14 | Branch `uv.lock` change makes the stub venv report `dep=2`, under which fake pytest skips `test_x`; merge base has `dep=1` | Exit 1, `NEW-SKIP test_x`. |
| A15 | Closure guard over the real repo | Passes. Mutation: drop any one manifest entry, or add an unlisted `scripts/dev` file that gates.sh calls; the guard fails. |
| A16 | Corrupt file, wrong `format_version`, other platform or digest, file mode `0644`, other owner (simulated), leftover temp, two concurrent writers | Each refused file is a miss, reason printed. Temp ignored then pruned. Both writers' entries present. |
| A17 | Fill or recheck pytest exits 2; totals disagree with JUnit (fill, recheck, branch) | Exit 3; cache unchanged; no worktree or ref venv left. |
| A18 | Fill while `test_x` skips at runtime (fake service down); service recovers; branch adds a skip of `test_x` | Exit 1, `NEW-SKIP` (recheck runs it). |
| A19 | Merge base fails `test_x`: branch fails only it / it plus `test_y` / neither. Cache filled while `test_z` flaked red; branch fails `test_z` | Exit 6 / exit 1 both listed / exit 0 with a note. `test_z` passes in the recheck: exit 1. |
| A20 | A test skips and another fails with a canary string in the reason and exception | Canary absent from the cache file and the EVIDENCE block; cache dir `0700`, file `0600`. |
| A21 | Inherited test: base in `tests/base_x.py`, child module renamed on the branch | Reference ids resolve against the merge-base tree; no finding after the rename map. Mutation: branch root gives `MISSING`. |
| A22 | Cache filled at `GATES_WORKERS=1`, compared at 2 | Same ids, no finding, no exit 2. |
| A23 | `test_gates_baseline_file.py`, `test_gates_parallel.py`, `test_dev_bootstrap_gates.py` | Unchanged and green, with `--baseline-file` on the engine. |
| A24 | e2e, real pytest at workers 1 and 2: marker skip, runtime skip, xfail on both sides, benchmark marker in a changed file, parametrize loss in an unchanged file, same-count swap in a changed file, deleted test, deleted file, renamed file, the A11 two-step file; one file filled alone vs inside a larger selection | Each regression blocks with the right line; xfail on both sides and the rename raise nothing; alone and in-selection entries are equal. |
| A25 | `tests/sentry/test_ci_config.py` | Command is `gates.sh --keep-going --ref-cache ~/.cache/decoy-gate-ref`; the exit-6 handler writes `::error::` and re-exits 6, so the job still fails. |

Mutations for the T2 gate: excuse a skip from cached state (A18), compare counts not ids (A6),
exempt changed files from MISSING (A6, A8), accept retire lines already on main (A8), use the gate
venv on an env change (A14), drop a manifest entry (A13, A15), store skip text (A20), parse
reference JUnit with the branch root (A21), drop the lock (A16), map an incomplete fill to 0 (A17),
drop the exit-6 branch (A19), restore the old `_stored_diff` body (A23), revert the workflow (A25).

## 7. Placement

CODEMAP "Where Things Go", row "Dev tooling": `scripts/dev/`, named by tool concern, sized by
`tests/sentry/test_module_size.py`. Row "Unit tests": `tests/unit/scripts/`. `scripts/dev` holds 15
files and `tests/unit/scripts` 8; neither is near a fan-out limit. `tests/RETIRED_TEST_IDS` sits at
the tests root beside `tests/conftest.py`, as a data file the gate reads.

| File | Change | Beside | Size after |
| --- | --- | --- | --- |
| `scripts/dev/gates.sh` | `--ref-cache DIR`; venv choice and ref venv; fill and recheck through `run_steps` (paths argument, `ref` label); rename map; exit 6; usage | - | 485 now, about 580 |
| `scripts/dev/_gates_junit.py` (new) | JUnit readers from `_gates_report.py`, each with a tree root | `_gates_report.py` | about 150 |
| `scripts/dev/_gates_compare.py` (new) | Compare engine, ref and stored adapters | `_gates_report.py` | about 220 |
| `scripts/dev/_gates_ref.py` (new) | `PRODUCER_INPUTS`, key, store, loader | `_gates_baseline.py` | about 200 |
| `scripts/dev/_gates_report.py` | Import readers; `_stored_diff` calls the engine; print ref results; totals check under `--ref-cache` | - | 493 now, about 360 |
| `tests/RETIRED_TEST_IDS` (new) | Empty list with a header comment | `tests/conftest.py` | 3 |
| `.github/workflows/gate-selfhosted.yml` | New command; exit-6 handler; comment | - | about 80 |
| `tests/sentry/test_ci_config.py` | Assert the new command and handler (line 137) | - | small |
| `tests/unit/scripts/test_gates_ref.py` (new) | A1 to A23 except A15 | `test_gates_baseline_file.py` | about 520 |
| `tests/unit/scripts/test_gates_ref_inputs.py` (new) | A15 closure guard | `test_gates_ref.py` | about 90 |
| `tests/unit/scripts/test_gates_ref_e2e.py` (new) | A24 | `test_gates_ref.py` | about 240 |
| `tests/unit/scripts/_gates_harness.py` | Per-case ids and outcomes; stub bootstrap; seed a failing merge-base test and a cache file | - | 228 now, about 300 |

Size caps: `test_module_size.py` covers `*.py` under `src/` and `scripts/` (goal 600, max 700).
gates.sh is not in that census; the 800-line project cap applies, so logic goes in the Python
modules. `test_gates_baseline_file.py` is already 871 lines, so new tests get their own files.

## 8. Rollout

1. Land slices 1 and 2 with the workflow command unchanged. The slices touch test tooling, an
   escalation trigger, so each merge needs the full suite diffed against the saved main baseline.
2. On test-1, test-5 and the Mac, at one main SHA: run `gates.sh --full` and confirm every step
   is green, static steps included. Run main at `GATES_WORKERS=1` and `auto`, and one file alone
   vs the full run; per-file ids must match. Time a typical recheck and a merge-base venv build.
   Record each runner's bash version and companion state (`native:` line of
   `bootstrap.sh --check`).
3. Run the new test files on the Mac with the runner's bash. The workflow already requires bash 3.2
   clean scripts and uses `flock` there; new shell code adds no bash 4 features, and the cache lock
   is `fcntl` in Python.
4. Switch the workflow command. Watch the first pushes per runner: the `ref:` line and run times.

## 9. Owner questions

Defaults the owner set, each open to override:

1. A new test that is skipped on the branch blocks.
2. A main failure only one host has: accept exit 6 on that runner until main is fixed.
3. No cache pre-fill on a push to main.
4. The cache is keyed by merge base, not the origin/main tip.
5. node-watch sends a Slack ping when a gate job ends with exit 6 (a node-watch change, outside
   this repo; follow-up, not a merge condition).
8. A test id the branch removes, in any file, blocks unless the diff adds it to
   `tests/RETIRED_TEST_IDS`. Reviewers see the retirement in the diff.

New questions:

6. A static step red on one host only: fix it before the switch (recommended), or keep that
   runner off the `decoy-gate` label until fixed?
7. Native companion: should every gate venv have it installed, or is `absent` acceptable on some
   runners? Bootstrap only checks it today (`bootstrap.sh:64-69`). Its presence also decides
   whether a merge-base venv pays a Rust build.

## Follow-ups (V2-LOG)

- Recheck and A24 sample selection independence; they do not prove it for every test.
- `test_gates_baseline_file.py` is 871 lines; split it when it next grows.
- CLI repo gate: same design.
- node-watch Slack ping on exit 6 (default 5).

## Dennis r1 response

| Finding | Resolution | Where |
| --- | --- | --- |
| H1 fallback weakest | One route; uncached fill; same engine; static steps strict | §1.2, §1.7, A12 |
| H2 cache poisoning | `PRODUCER_INPUTS`, digest key, merge-base venv. Stored skip reasons replaced by the recheck (Codex 4, 5) | §1.6 to §1.8, A13 to A15 |
| H3 key collides across steps | Key (step, file); `run_steps` paths argument | §1.3, §1.9, A11 |
| H4 deselect and id swap | `MISSING` on ids in every file; no collect-only needed | §1.4, A6, A7 |
| M1 import cycle | `_gates_junit.py` | §5.1 |
| M2 lock in the tool | `fcntl` lock, unique temp, EXIT trap, prune | §1.11, A16 |
| M3 totals check | Branch, fill and recheck | §1.12, A17 |
| M4 host-only static red | Rollout step 2; question 6 | §8, §9 |
| M5 exit parity | One engine, one exit rule | §1.13 |
| M6 fake pytest only | Real-pytest e2e | A24 |
| L1 to L4 | Both listed on exit 1; exit-6 handler; deleted files now `MISSING`; `absent` entries | §1.3, §1.4, §1.13, A8, A10, A25 |
| L5 branch runs its own gates.sh | V1 threat model | Pre-mortem |
| L6 platform citation | Cited | §1.8 |
| Sameness | One engine, now in scope | §5.3 |
| Not verified items | A22, A24, rollout steps 2 and 3, question 7 | §6, §8, §9 |

## Codex r1 response

All seven findings checked against the code at `aeb47829`; none rejected.

| Finding | Evidence checked | Resolution | Where |
| --- | --- | --- | --- |
| 1 branch venv for the reference | `gate-selfhosted.yml:60` bootstraps from the branch; `gates.sh` uses one `$BIN` | Merge-base venv from the merge-base bootstrap on any env change; cost stated | §1.7, A14 |
| 2 missing producer inputs | `bootstrap.sh:10-16`, `_gates_report.py:428` imports `_gate_workers` | One `PRODUCER_INPUTS` manifest for cacheability and key; closure guard; one seed per input | §1.6, §1.8, A13, A15 |
| 3 ids lost in changed or deleted files | Plan r2 A8 passed them | `MISSING` everywhere; retire in `tests/RETIRED_TEST_IDS` in the diff; rename map | §1.4, A8, A9, A24, default 8 |
| 4 transient skip cached 14 days | Plan r2 cached skip state | Cache holds ids only; skips and main reds come from a fresh recheck | §1.5, A18, A19 |
| 5 skip reasons are values | `test_exception_hierarchy.py:80`, `test_secret_census_frames.py:162` | No reasons stored or printed; `0700`/`0600`, owner and mode checked; canary test | §1.11, A20, class 1 |
| 6 readers use branch `TEST_ROOT` | `_gates_report.py:125-134`; tests patch it (`test_gates_baseline_file.py:838`) | Tree-root parameter on every reader | §1.10, A21 |
| 7 second compare engine | `_stored_diff` at `_gates_report.py:309` | `_gates_compare.py` with ref and stored adapters, in scope | §5.3, A23 |
| V2: 48-bit digest | - | Full SHA-256 | §1.8 |
| V2: sampling only | - | Follow-ups | Follow-ups |
| V2: "public" private module | - | Called shared, not public | §5.1 |
| V2: 871-line test file | - | Follow-ups | Follow-ups |
