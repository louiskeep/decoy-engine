# ENGINE-PAR: run the engine test suite in parallel with pytest-xdist

Status: plan (2026-11-06, not built). Engine, plus the orchestrator scripts `~/bin/decoy-vm-t3`
and `~/bin/vm-run`. Roadmap row 4 (CLI `docs/ROADMAP.md`).

Rules consulted: `~/dev-rules/README.md` (routing table), `00-universal.md`,
`risk-and-exceptions.md`, `development-loop.md` (PLAN, plan-lint), `testing.md` (Test depth and
the escalation triggers: this slice is test tooling, so every part runs a full suite),
`cross-cutting-concerns.md` (choke point and guard for the `serial` rule), `architecture.md`
(Layout), `reuse-first.md`, `catalogs/recurring-defects.md` (all classes). Also read: engine
`origin/main` `55876436` (`scripts/dev/gates.sh`, `scripts/dev/_gates_report.py`,
`scripts/dev/bootstrap.sh`, `pyproject.toml` `[tool.pytest.ini_options]` and the `xdist` extra,
`tests/conftest.py`, `tests/_dp_cert.py`, `src/decoy_engine/quality/dp_provenance.py`); CI-E1
`origin/fix/ci-e1-frozen-installs` `f2b7b3da`; platform `origin/feat/parallel-tests` `2c6efd55`
(18 files, 764 lines) and platform `f53bb56f` (#156, the stored-baseline compare); engine
`origin/plan/layout` (`tests/sentry/` is frozen for placement); `~/bin/vm-run`,
`~/bin/decoy-vm-t3`; skill `mac-mini-node` and memory `mac-mini-test-node` (the Mac engine lane).

Risk: R2. Test tooling and the merge gate's evidence. A wrong partition or a mis-merged JUnit file
would let a failing test go unseen at merge. No product code changes.

## 1. Guarantee and non-goals

The change promises:

1. One engine full suite (`scripts/dev/gates.sh ... tests`) runs its `tests` step under
   pytest-xdist on any host where xdist is installed: a parallel pass over every selected test not
   marked `serial`, then a plain serial pass over the selected tests marked `serial`. The two
   passes partition the selection: every test id runs exactly once, and both JUnit files are read
   as one run.
2. The baseline compare keeps its meaning. The failing-id set is the set of JUnit test ids with a
   `failure` or `error`, whichever worker ran them; the collected count is the sum of both passes.
   A crashed worker shows as a failing id (the test it was running), never as a silently missing
   test. A run that aborts (pytest exit other than 0 or 1, an INTERNALERROR, a missing totals line)
   stays exit 3, as today.
3. The engine gets the stored-baseline compare the platform has had since #156:
   `--write-baseline FILE` on a clean `origin/main` and `--baseline-file FILE` on a candidate, so a
   merge test runs the suite once instead of twice (Q1). A stored baseline records how it ran
   (`workers`, `dist`); a compare whose run mode differs from the stored one is refused (exit 2).
4. `serial` is the only way to keep a test out of the parallel pass, and it is used only for a
   test that needs an exclusive host resource: a wall-clock budget, a process or host memory
   envelope, a CPU speedup ratio, an absolute thread count. A sentry refuses a test module that
   uses one of these without the mark (section 6).
5. Worker count: `GATES_WORKERS=N|auto`, default `auto` = min(usable CPUs, 4), lowered on small
   hosts by memory (the platform's `_gate_workers.py` logic, unchanged). `GATES_WORKERS=1`, or a
   venv without xdist, is today's single plain run.
6. The merge test of an engine candidate finishes in under 60 minutes (owner target) on the Mac
   and on a merge VM with four real cores, measured on P2's head (AT-14). On today's
   `e2-standard-4` (two physical cores) section 2 puts it at about 60 minutes, so that host needs
   Q7.

Non-goals (a finding here is a follow-up, not a blocker):

- CI triggers and hosted workflow changes. CI-E2 owns the triggers; P4 only switches the
  regression-gate job's pytest call after CI-E2 has restored it.
- Fixing tests that fail on `origin/main` serially. They stay in the baseline with their owners.
- Making single slow tests faster (the 25-minute determinism matrix, the 10-minute FF1
  permutation). Q3 and FU-2.
- The native companion lane (`native-companion.yml`, the companion venv). It has its own suite and
  runner; FU-4.
- Splitting one suite across several VMs (sharding). FU-5.

## 2. Measured today

Hosts. GCP `e2-standard-4`: 4 vCPU that are **2 physical cores with 2 threads each** (`lscpu`:
`Core(s) per socket: 2`, `Thread(s) per core: 2`), 16 GB, Python 3.10.20, pytest 9.1.1, the
lock-synced `dev+lint+vault+cloud` venv, no native companion. Mac mini: Apple M4, 10 cores (4
performance, 6 efficiency), 16 GB, arm64, same lock profile, no companion. The spot VMs
`decoy-test-3` and `-4` no longer exist (`gcloud compute instances list`, 2026-11-06 18:09 UTC) and
all four on-demand VMs were running engine merge suites, so the VM trial ran through `~/bin/vm-run`
on `decoy-test-5` next to one serial T3 suite (`resE1c`, one process, about 1.5 GB). The VM parallel
number is therefore contended: five pytest processes on two physical cores.

**Serial wall time per test directory** (VM: `t3-substop2` branch run on `decoy-test-1`, engine
`347ddd27` on main `55876436`, JUnit `time` per case; Mac: this trial's serial run on `55876436`):

| Directory | Cases | VM serial | Mac serial |
|---|---:|---:|---:|
| `tests/unit` | 17,010 | 2,629 s | 544 s |
| `tests/generation` | 3 | 1,540 s | 249 s |
| `tests/parity` | 5,736 | 942 s | 170 s |
| `tests/native` | 7,332 | 552 s | 120 s |
| `tests/property` | 324 | 432 s | 85 s |
| `tests/perf` | 24 | 379 s | 76 s |
| `tests/sentry` | 4,083 | 265 s | 50 s |
| `tests/physical` | 2,299 | 104 s | 31 s |
| `tests/integration` | 499 | 92 s | 23 s |
| `tests/privacy` | 7 | 88 s | 31 s |
| the other 7 directories | 3,453 | 126 s | 15 s |
| **pytest wall** | 40,779 | **2:01:08** | **23:43** |

The whole legacy merge test (`decoy-vm-t3 ... legacy`: lint steps, branch suite, then the same on
`origin/main`) took 4 h 38 min (`t3-substop2`, 02:54 to 07:33 UTC). Lint, mypy, sentry and license
steps are about 5 minutes per side; the rest is the suite twice.

Single tests set the floor: `test_full_matrix_against_golden` 1,503 s serial on the VM (21 percent
of the suite, Q3), `test_exhaustive_six_digit_permutation` 594 s, then 22 more over 30 s (2,000 s
together).

**xdist trial, engine `origin/main` `55876436`, `-m "not benchmark and not packaging and not
codspeed"` (the `gates.sh` expression), pytest-xdist 3.x added to the venv by hand:**

| Run | Host | Wall | Failed + errors | Collected | Notes |
|---|---|---:|---:|---:|---|
| serial (T3 branch run) | VM, alone | 2:01:08 | 0 on main | 40,759 + 20 deselected by branch | baseline for the VM |
| `-n 4 --dist loadfile` | VM, next to one T3 | **1:35:47** | 25 (all xdist-only, below) | 40,759 | sum of per-test times 274.5 min, 2.3 times the serial 119 min: four workers plus a fifth suite on two cores |
| serial | Mac | 23:43 | 547 (the known Mac set) | 40,759 | |
| `-n 4 --dist loadfile` | Mac | **9:24** | 547, the **same ids** | 40,759 | zero xdist-only failures |
| `-n 8 --dist loadfile` | Mac | **7:51** | 547, the same ids | 40,759 | peak 11.5 GB |

The `-n auto --dist worksteal` VM run was stopped: after the loadfile run took 96 minutes, a second
contended run would not have changed the decision (the tail is one 41-minute test, which no mode
splits), and the VMs were needed for merges. Worksteal versus loadfile is estimated from the serial
per-test times instead, by greedy scheduling over 4 workers: loadfile in collection order 36.9 min,
worksteal (per test) 37.2 min, loadfile with the longest files first 29.8 min. The order of the
long files matters more than the mode.

Collected count is identical in every run (40,759). Skipped differs on the VM: +84 skips, all
`dp_certified` ("requires the certified DP proof-stack"), because installing xdist by hand moved
the DP fingerprint (section 4). With that, failing ids are the only difference.

**xdist-only failure inventory (VM, `-n 4 --dist loadfile`; 0 on main serially), by cause:**

| Cause | Count | Ids | Fix |
|---|---:|---|---|
| Wall-clock bound under CPU load | 20 | `tests/connectors/test_gcs_timeouts.py::test_black_holed_endpoint_fails_inside_the_bound[*]` (10) and `::test_timeout_error_text_is_value_free[*]` (10) | `serial` (the test asserts a client timeout fires inside a wall-clock bound; the bound is the point) |
| Memory sampling near a cap, timing-dependent | 4 | `tests/unit/execution/test_isolated_memwatch_run.py::TestRealChild::test_crash_near_the_cap_records_a_qualifying_sample[data,as]`, `::test_crash_far_from_the_cap_is_not_flagged[data,as]` | `serial` (the memwatch sampler misses the peak when the child is starved of CPU) |
| Throughput budget | 1 | `tests/perf/test_throughput_budgets.py::test_categorical_throughput_10k_rows_under_1s` | `serial` (all of `tests/perf/` gets a directory mark, 24 cases, 379 s serial on the VM) |
| Shared state or ordering | 0 | none seen | |
| Fixed ports, files, env | 0 | none seen | the dead `tests/data/` fixtures go anyway (section 4) |

Serial rerun of the three failing files on the same VM, still loaded by the other T3 suite
(`vm-run`, 6:38): 24 of the 25 failed again (all 20 GCS cases and all 4 memwatch cases); the
throughput budget passed. The same 24 passed in that VM's own unloaded serial T3 run (`resE1c`
branch JUnit) and on the Mac under `-n 4` and `-n 8`. So all 25 are host-load failures, not shared
state between tests: the GCS cases assert a client gives up within 30 s against a black-holed
endpoint, the memwatch cases need the sampler to see the child near its cap before it dies, and the
budget is a wall-clock bound. They need the serial pass on a host that runs nothing else, which is
why P3 holds the `vmrun` lock during a merge suite.

The Mac shows none of the 25 under `-n 4`: ten real cores leave headroom, so these are load
failures, not isolation bugs. Statically, the sentry's walk (section 6) also flags tests that passed
here only because they did not coincide with load: the other `tests/perf/` budgets,
`tests/native/test_gil_release.py:194,199` (speedup ratios, skipped without the companion),
`tests/unit/test_lark_parser.py:528` (5 s budget), `tests/unit/execution/test_isolated_run.py:553`
(15 s), `tests/unit/scripts/test_fk_scale_infra.py:198` (30 s). P2 marks those it confirms; the
serial pass then holds about 30 cases, roughly 10 minutes on the VM and 2 on the Mac.

**Memory.** VM: host memory used peaked at 13.3 GB of 16 GB (mean 5.5 GB), including the other T3
suite (about 1.5 GB) and a builder run; the peaks line up with the `tests/perf/` RSS-envelope tests,
which P2 moves to the serial pass. Mac: our processes peaked at 9.4 GB at `-n 4` (memory free
stayed at 90 percent by `memory_pressure`). Four workers fit 16 GB with the envelope tests serial;
eight do not leave the 4 GB per worker the `mac-mini-node` skill sets.

**What this means for the 60-minute target.**

- Mac: a parallel merge test is about 10 minutes plus lint, against 23 minutes serial and about 50
  in legacy mode today.
- VM, `e2-standard-4`: two physical cores cap the gain near 2x. A clean (idle-VM) parallel run is
  expected around 50 to 60 minutes; the contended trial took 96. With P1 the merge test runs the
  suite once, so it lands near the 60-minute line, not well under it. Q7 asks for a machine-type
  change: moving to more real cores is the change that reaches the target with margin.

## 3. Established solutions

Search run: web search "pytest-xdist --dist loadfile loadscope worksteal xdist_group serial tests";
in-codebase, the platform branch and the engine `xdist` extra (2026-09-16, never wired into
`gates.sh`).

- pytest-xdist distribution modes
  ([docs: Running tests across multiple CPUs](https://pytest-xdist.readthedocs.io/en/stable/distribution.html)):
  `load` and `worksteal` spread single tests, `worksteal` rebalancing when a worker runs dry;
  `loadscope` keeps a module or class on one worker; `loadfile` keeps a file on one worker;
  `loadgroup` pins `xdist_group`-marked tests to one worker. None gives a test the machine to
  itself: `xdist_group` only co-locates tests while the other workers keep running.
- The usual answer for "this test needs the box" is two invocations, `-n N -m "not serial"` then
  `-m serial` without xdist. The platform branch does exactly that (`gates.sh` `cmd` and `cmd2`,
  `combine_rc`, `_gates_report._xmls`), and so does this plan.
- Worker isolation that xdist already gives: `tmp_path` and `tmp_path_factory` live under
  `<basetemp>/popen-gwN/`, and `PYTEST_XDIST_WORKER` names the worker.
- execnet cannot serialize a lone surrogate in a report; the platform's
  `tests/support/xdist_safe_reports.py` hook is the fix it found by trial, ported as is.

Followed: the platform branch, file by file (section 4 says what transfers and what differs).
Custom code left: the engine's stored-baseline port (from platform #156), the run-mode check on a
stored baseline, and the exclusive-resource sentry. No library does either job.

## 4. Design notes

**What transfers from the platform branch, and what differs.**

| Platform piece | Engine | Notes |
|---|---|---|
| `scripts/dev/_gate_workers.py` (85 lines) | ported unchanged | same 14 GB small-host line; the VMs are 16 GB, the devbox under 14 GB gets 2 |
| `gates.sh`: `GATES_WORKERS`, `PYTEST_MARK`, two passes, `combine_rc`, `workers:` header line, both JUnit files in the whole-suite check | ported | engine `PYTEST_ARGS` is `-m 'not benchmark and not packaging and not codspeed'`; same `${PYTEST_ARGS[1]}` trick |
| "a tree without the scrubber stays serial" (`[ -f tests/support/xdist_safe_reports.py ] ...`) | ported, path `tests/_xdist_safe_reports.py` | keeps `--baseline` (legacy) on an older `origin/main` honest: both sides then run serial |
| `_gates_report.py`: `_xmls`, `_roots`, `_suites_of`, both totals lines | ported onto the engine reporter | the engine reads failing ids from the `-rfE` summary today (`failing_ids_for`); P1 moves it to JUnit ids as the platform did in #156, so two logs never need merging |
| `--write-baseline` / `--baseline-file` (#156, in platform `main`) | ported in P1 | not on the platform branch; the engine never had it, so `decoy-vm-t3 ... engine` only works in `legacy` mode today |
| `tests/support/xdist_safe_reports.py` | `tests/_xdist_safe_reports.py`, registered from `tests/conftest.py` | engine has no `tests/support/`; its helpers sit at `tests/_*.py` (`_keyed_secret.py`, `_dp_cert.py`) |
| `short_tmp_path` fixture | ported into `tests/conftest.py` | one engine user: `tests/fileaccess/test_policy_rooted.py:97` binds an `AF_UNIX` socket under `tmp_path` |
| thread count relative to the worker | not needed today | the engine has no `threading.active_count() == N` assert; the sentry catches a new one |
| `test_parallel_tests_support.py`, `test_gates_parallel.py` | ported to `tests/unit/scripts/` | engine `test_dev_bootstrap_gates.py` gets `GATES_WORKERS=1` in its `_run` env, as the platform did |
| platform's serial marks | engine list comes from the trial (section 2) | different suite; none of the platform ids exist here |
| pytest-xdist in `dev` extras | **differs**: engine keeps it out of `dev` today | see "DP fingerprint" below |

**DP fingerprint.** The engine's certified DP stack is the exact installed distribution set of the
lock profile `dev+lint+vault+cloud` (`dp_provenance.installed_distribution_set`, `:131`;
`_CERTIFIED_STACKS`, `:273`). The merge-test venv on the VMs is that profile, so the
`dp_certified` tests run there today. Installing pytest-xdist and execnet into that venv changes
the set, `check_fit_environment` refuses, and every `dp_certified` test turns into a skip: more
skipped tests and less coverage than the stored baseline. So xdist cannot just be `uv pip install`ed
into the gate venv. The plan moves `pytest-xdist` into the `dev` extra and re-pins the certified
fingerprint in the same change. CI-E1s (row 2) already re-pins that fingerprint for the security
bump; doing both in one re-pin is Q2's recommendation. Until then the trial numbers in section 2
were taken with xdist added by hand, and their `dp_certified` tests skipped (counted in section 2).

**Determinism under xdist.**

- Ids. A JUnit `testcase` id (`classname` + `name`) does not depend on the worker. The failing-id
  set of a parallel run is the union over both JUnit files.
- Collected count. `X and not serial` plus `X and serial` partitions `X` by marker algebra. AT-3
  pins it: the two passes' ids are disjoint and their union equals a plain `--collect-only` of the
  same selection.
- Order. A test that passes serially only because of state left by an earlier test, or fails only
  because of it, can flip under xdist. That is the inventory in section 2, fixed at the cause
  (shared state made worker-local) or marked `serial` when the shared thing is the host.
- Mode-bound baselines. A baseline written serially and a candidate run in parallel could disagree
  on exactly those order-dependent ids. So a stored baseline records `workers` and `dist`, and
  `--baseline-file` refuses a run whose mode differs (exit 2, "rewrite the baseline in this mode").
  `--baseline` (legacy) runs both sides with the same settings, so it needs no check.
- Distribution mode: `--dist loadfile` everywhere (section 2 compares it with `worksteal`). It
  keeps a file's tests on one worker in file order, so module fixtures run once and intra-file
  order is unchanged from serial; that keeps a failing id meaning what it meant serially.
  `worksteal` balances better but splits files across workers. Q4.
- Flaky tests. No reruns, no `pytest-rerunfailures`: `testing.md` forbids retrying a flaky test
  until it passes. A test that flakes only under xdist is in the inventory and is fixed or marked
  `serial` with a reason; one that also flakes serially is quarantined as today (owner, reason,
  removal path).
- Worker crash. `--max-worker-restart` stays at xdist's default; a crash (a segfault, an OOM kill)
  becomes an error on the test the worker was running, which is a failing id. If xdist gives up
  (too many restarts), pytest exits with a non-1 code and the reporter's crash rule (exit 3) holds.

**Isolation.**

- Temp dirs: `tmp_path` is already per worker. `gates.sh` sets no `--basetemp`.
  `decoy-vm-t3` sets `TMPDIR=/tmp/decoy-out-<label>`, which lengthens every temp path; the one
  socket test moves to `short_tmp_path`.
- Ports: no engine test binds a fixed port (`tests/connectors/test_gcs_timeouts.py:316` binds port
  0); the sentry refuses a new fixed-port bind outside `serial`.
- Repo-tree writes: `tests/conftest.py` `setup_test_directories` (session, autouse) creates
  `tests/data/{input,output,expected,config}` and `rmtree`s `tests/data/output` at session end.
  Under xdist every worker runs that teardown while others may still be writing there. Nothing
  uses it: `sample_csv_file`, `sample_fixed_width_file`, `sample_masking_config`,
  `sample_generator_config` and `mock_logger` have no user outside `conftest.py` (one comment in
  `tests/unit/test_v2_generation.py:819`). P2 deletes the fixture and the five dead fixtures.
- Env and cwd: no test calls `os.chdir`; the two direct `os.environ` writes
  (`tests/property/test_keyprovider_invariants.py:354,552`) restore in `finally` and are
  process-local, so workers do not see each other's.

**Native companion builds.** No engine test builds the companion; tests import it or skip
(`importlib.util.find_spec("decoy_engine_native")`). The VM merge venv has no companion, so 2,290+
native tests skip there today and under xdist. Workers load the same installed `.so` read-only.
The companion lane (built once per host before the run, `~/.cache/decoy-native-venv`) is FU-4:
the native kernels size thread pools from `os.cpu_count()`, so four workers oversubscribe; the
timing tests there (`tests/native/test_gil_release.py:194,199` speedup ratios) are marked `serial`
in P2 now, because the sentry finds them.

**Memory.** Measured in section 2. `auto` keeps 4 workers on a 16 GB host; the memory-envelope
tests run in the serial pass, so the parallel pass never stacks two of them.

**Worker count on a shared VM.** `vm-run` assumes a merge suite takes one core and about 3 GB, so
a builder run can share the VM. A parallel suite takes all four cores. P3 makes `decoy-vm-t3` take
the `vmrun` lock for the duration of the parallel pass (builders queue or go to another VM), and
`vm-run` sets `GATES_WORKERS=2` for builder runs so a builder never takes a whole VM. Q5.

House style applies to the ported code: one place per fact (the worker rule lives in
`_gate_workers.py`, the mark expression in `gates.sh` `PYTEST_MARK`, the exclusive-resource list in
the sentry's registry), no flag argument that switches behavior beyond the env knob the platform
already has.

## 5. Placement

The engine `CODEMAP.md` has no "Where things go" section yet (LAYOUT, `origin/plan/layout`, adds
it). Placement follows the existing locations and LAYOUT's rule that `tests/sentry/` is frozen for
placement and new sentries go in a named sub-package.

| New file | Directory | Map row | Sits beside |
|---|---|---|---|
| `_gate_workers.py` | `scripts/dev/` | local gate scripts (no map row yet; LAYOUT's "dev scripts" row) | `_gates_report.py` |
| `_xdist_safe_reports.py` | `tests/` | test helpers as `tests/_*.py` | `tests/_keyed_secret.py`, `tests/_dp_cert.py` |
| `parallel/__init__.py`, `parallel/_resources.py` (the exclusive-resource registry and AST scan), `parallel/test_serial_resources.py` | `tests/sentry/parallel/` (new sub-package) | map change: LAYOUT's sentry rows gain "parallel-safety sentry: `tests/sentry/parallel/`" | `tests/sentry/layout/` (LAYOUT) |
| `test_gates_parallel.py`, `test_parallel_tests_support.py` | `tests/unit/scripts/` | existing gate-script tests | `test_dev_bootstrap_gates.py` |

`tests/sentry/parallel/test_serial_resources.py` is added to `SENTRIES`
(`tests/sentry/test_sentry_manifest.py`) and its allowlist to `scripts/dev/sentry_registry.toml`
with a baseline under `tests/sentry/baselines/serial-resources.json`.

## 6. Choke points, guards and red mutations

| Rule | Choke point | Guard (walks) | Red mutation |
|---|---|---|---|
| A test that needs an exclusive host resource runs alone | the `serial` marker, applied as a function or class decorator, a module `pytestmark`, or a directory `conftest.py` `pytestmark`; `gates.sh` deselects it from the parallel pass by `-m "(...) and not serial"` | `tests/sentry/parallel/test_serial_resources.py`: an AST walk of every `tests/**/*.py` module that pytest collects. Imports are resolved to `(module, name)` (`import time as t`, `from resource import getrusage as g`, attribute calls on aliases; recurring class 5). It flags, in any test function, class or module not covered by `serial`: (a) an `assert` comparing a value derived from `time.perf_counter`, `time.monotonic`, `time.time` or a `*elapsed*`/`*_seconds`/`p50`/`speedup` name against a numeric bound; (b) an `assert` on `resource.getrusage(...).ru_maxrss`, `psutil` memory reads, `/proc/self/status` `VmHWM`/`VmRSS`, or a `peak_rss`/`*_rss*` name against a bound; (c) `threading.active_count()` compared with a literal; (d) `socket.bind` with a literal non-zero port; (e) in-process `resource.setrlimit` (not inside a string run by a child, not a `preexec_fn`). Known sites that are safe in parallel live in a ratchet allowlist of exact `(path, function)` identities with a reason each | add `assert elapsed < 2.0` (from `time.perf_counter`) to an unmarked test in a scratch module: red; the same through `from time import perf_counter as pc`: red; remove `@pytest.mark.serial` from `tests/perf/test_throughput_budgets.py`: red; add a `serial` mark to a directory `conftest.py` and an unmarked RSS assert below it: green (the walk honours directory marks) |
| Both passes are one run | `_gates_report._xmls` (every `tests` read goes through it) | AT-4, AT-5: reporter counts and ids identical for one or two JUnit files; a serial log without a totals line exits 3 | read only `<label>-tests.xml` in `junit_failing_ids`: AT-5 red |
| A stored baseline is compared only with a run of the same mode | `_gates_report` baseline loader | AT-8 | drop the mode check: AT-8 red |
| Parallel and serial passes partition the selection | `gates.sh` `PYTEST_MARK` (one expression, both passes add to it) | AT-3 | change the parallel pass to `-m "$PYTEST_MARK"`: AT-3 red (duplicated ids) |

The sentry is seeded-bypass tested (each spelling above), registered in `SENTRIES` and
`sentry_registry.toml`, and runs in the merge gate's sentry step. Its allowlist only shrinks.

## 7. Acceptance tests

Red-first evidence is an assertion failure on the base tree, recorded per part.

- AT-1 (`GATES_WORKERS=1`): one plain pytest call, no `-n`, no serial-pass file; EVIDENCE header
  `workers: 1`. Port of the platform test.
- AT-2 (`GATES_WORKERS=2`): a seeded clone with one `serial` and three plain tests runs a parallel
  pass then a serial pass; both JUnit files exist; collected = 4.
- AT-3 (partition): on the real engine tree, `--collect-only -q` of `X and not serial` and of
  `X and serial` are disjoint and their union equals `--collect-only -q` of `X`, where `X` is
  `gates.sh`'s mark expression. Mutation: parallel pass without `and not serial`.
- AT-4 (empty serial pass): a selection with no `serial` test gets pytest exit 5 on the serial pass
  and the step is not a failure (`combine_rc`).
- AT-5 (reporter): counts, failing ids and the totals cross-check are identical for the same cases
  split across one or two JUnit files; a failure only in the serial file is a failing id.
- AT-6 (crash): a seeded test that `os.abort()`s under `-n 2` is reported as a failing id of that
  test, and the run is not read as green.
- AT-7 (surrogate): a failing test that logs a lone surrogate runs under `-n 2` without
  INTERNALERROR (port).
- AT-8 (mode-bound baseline): `--write-baseline` stores `workers` and `dist`; `--baseline-file` with
  a different `GATES_WORKERS` (1 vs 4) exits 2 naming both modes.
- AT-9 (stored baseline, P1): the platform #156 cases ported to the engine fake-pytest harness: new
  failing id exits 1; baseline id that now passes is reported; collected-count delta reported;
  write refused for a selection, a dirty tree, or HEAD not `origin/main`; skipped count above the
  baseline fails.
- AT-10 (old tree stays serial): `--baseline` (legacy) against an `origin/main` without
  `tests/_xdist_safe_reports.py` runs that side serial and says so in the steps line.
- AT-11 (sentry): the seeded spellings in section 6, red; the current tree, green with the
  recorded allowlist size.
- AT-12 (short socket path): `test_policy_rooted.py` passes under `-n 2 --basetemp=<130-char path>`.
- AT-13 (the inventory): every id in section 2's xdist-only list passes in a `-n 4 --dist loadfile`
  full run on P2's head, three runs in a row on one VM, with no new failing id against a baseline
  written in the same mode.
- AT-15 (order): with `DECOY_TEST_ORDER_FILE` (set by `gates.sh` to the `--baseline-file` path)
  naming file seconds, collection puts files in descending seconds and keeps each file's own test
  order; with no file, collection order is unchanged; the collected id set is the same either way.
  Mutation: sort tests instead of files: red (intra-file order changed).
- AT-14 (the target): P3's merge test (`decoy-vm-t3 <vm> engine <head> <label>` stored mode) of
  P2's head on an idle on-demand VM finishes in under 60 minutes end to end, recorded with the
  per-step times.

## 8. Parts (slices)

Commit subjects: Conventional Commits, at most 72 characters, final line `cam`, no body unless it
explains why. Test depth T0 per edit, T1 per commit and slice head, T2 at the gate. Every part is
test tooling, so every part runs one full suite on its merge head (testing.md trigger).

| Part | Branch | Base | Depends on | Hand-written lines |
|---|---|---|---|---:|
| P1 stored baseline | `feat/engine-par-p1-stored-baseline` | engine main after CI-E1 | CI-E1 merged | ~330 code, ~420 tests |
| P2 xdist in gates.sh | `feat/engine-par-p2-xdist` | main after P1 | P1; Q2 (xdist in `dev`, fingerprint re-pin) | ~260 code, ~420 tests, plus the marks and fixes in the inventory |
| P3 tooling defaults | `~/bin` (not a repo) plus engine `docs/` | after P2 | P2 merged | ~60 |
| P4 CI | `ci/engine-par-p4` | after CI-E2 | CI-E2 merged | ~20 |

**P1.** Port platform #156 (and its follow-up fixes in platform main) to engine `gates.sh` and
`_gates_report.py`: `--baseline-file`, `--write-baseline`, `--full`, JUnit-derived failing ids
(`_case_id`, `junit_failing_ids`, the totals cross-check), the skipped-count rule and the
`PYTEST_ADDOPTS`/`PYTEST_PLUGINS` scrub for stored runs. The baseline file gains `workers`, `dist`
(both `1`/`no` in P1) and `file_seconds` (per test file, summed from the JUnit `time` attributes) now,
so P2 adds no format change. AT-9. Write the first engine
baseline from main with `decoy-vm-t3 <vm> engine <main> base-eng write` and promote it. Subject:
`feat(dev): stored failing-id baseline for engine gates`.

**P2.** `pytest-xdist` into `dev` (Q2) with the lock and the DP fingerprint re-pin, or stacked on
CI-E1s if it carries the re-pin. Port `_gate_workers.py`, the `gates.sh` two-pass step,
`_gates_report._xmls`, `tests/_xdist_safe_reports.py`, `short_tmp_path`; delete the dead
`tests/conftest.py` data-dir fixtures; add the `serial` marker (pyproject) and the marks and fixes
for the section 2 inventory; the sentry with its registry and allowlist; the longest-files-first
collection order read from the stored baseline's `file_seconds` (Q4; no file, no reorder); AT-1 to AT-8, AT-10 to
AT-13. Update `docs/` (the gates help text and `CONTRIBUTING.md` testing notes) and the
`pyproject.toml` comment that says "CI still runs the suite serially". Subject:
`test: run the engine suite in parallel with xdist in gates.sh`.

**P3.** `~/bin/decoy-vm-t3`: engine `stored` mode works after P1 (no change to the script beyond
dropping the "engine legacy" note); hold the `vmrun` lock during an engine or CLI parallel suite;
pass `GATES_WORKERS` through. `~/bin/vm-run`: export `GATES_WORKERS=2` for builder runs. Mac lane:
`GATES_WORKERS=4` (Q6) and a Mac-specific stored baseline written in that mode. AT-14. These are
orchestrator scripts, committed to `agent-config`, not the engine repo.

**P4.** After CI-E2 restores the engine triggers: the `regression-gate` job runs
`pytest tests -n auto --dist loadfile -m "(...) and not serial"` then the serial pass, or calls
`gates.sh --steps tests` directly. CI-E2's required-jobs table then names one job, unchanged.

## 9. Test scope and in-flight sequencing

| In flight | Touches | Effect |
|---|---|---|
| CI-E1 (row 1, `f2b7b3da`, merging) | lock install in CI, `bootstrap.sh`, sentries | P1 bases on main after it merges |
| CI-E1s (row 2) | `uv.lock` security bump, DP fingerprint re-pin | Q2: add `pytest-xdist` to `dev` inside CI-E1s's re-pin, so the fingerprint moves once; else P2 does its own re-pin after CI-E1s |
| CI-E2 (row 3) | engine CI triggers, `ci/required-jobs.toml`, marker sentry | P4 waits for it. CI-E2's marker and trigger sentry must accept the new `serial` marker; P2 registers it in `pyproject.toml` markers, which that sentry reads |
| CI-X (row 5a, stacked on CI-E1) | `tests/sentry/ci_scan/` | no overlap |
| LAYOUT (`plan/layout`) | `CODEMAP.md` "Where things go", `tests/sentry/layout/` | P2 adds one map row for `tests/sentry/parallel/` if LAYOUT has landed, else LAYOUT's builder adds it |
| Any engine slice merging during P2 | new tests | the sentry refuses a new exclusive-resource test without `serial`; a test that fails only under xdist shows as a new failing id at that slice's merge test (once P3 is live) |

Full-suite trigger: every part (test runner and selection tooling; P2 also a dependency lock and
fixture changes). P1's T1: `tests/unit/scripts/`, all sentries. P2's T1: the same plus every file
in the inventory and `tests/fileaccess/test_policy_rooted.py`.

## 10. File sizes

| File | Today | After |
|---|---:|---:|
| `scripts/dev/gates.sh` | 219 | ~380 (P1 ~330, P2 +50) |
| `scripts/dev/_gates_report.py` | 201 | ~420 |
| `scripts/dev/_gate_workers.py` | new | ~85 |
| `tests/_xdist_safe_reports.py` | new | ~50 |
| `tests/conftest.py` | 301 | ~140 (dead fixtures out, `short_tmp_path` in) |
| `tests/sentry/parallel/_resources.py` | new | ~260 |
| `tests/sentry/parallel/test_serial_resources.py` | new | ~180 |
| `tests/unit/scripts/test_dev_bootstrap_gates.py` | 380 | ~780 (the #156 cases; split into `test_gates_baseline_file.py` if it passes 800) |
| `tests/unit/scripts/test_gates_parallel.py` | new | ~250 |
| `tests/unit/scripts/test_parallel_tests_support.py` | new | ~190 |

Every file stays under 800 lines.

## 11. Recurring-defect coverage

Class 5 (normalized scanners): the sentry resolves imports and aliases to `(module, name)`, with a
seeded case per spelling (AT-11). Class 7 (no hidden CI failures): `combine_rc` lets only exit 5
pass, and only when the other pass ran tests; no `|| true` on either pass; a crash code wins
(AT-4, AT-6). Class 9: mutations re-run on each part's final head. Class 10: the sentry allowlist
holds exact `(path, function)` identities, and listed functions are still scanned for other
resources. Class 11: no size or ratchet pin is raised; the allowlist starts at the measured size
and only shrinks. Classes 1 to 4, 6 and 8 are not touched: no regex on values, no persisted model
beyond the baseline JSON (written and read by one module, version-tagged by its keys as the
platform's is), no new reader of user input, no private import.

## 11a. General review taxonomy (plan contract)

| # | Class | Applies | Closing structure | Proof and mutation |
|---|---|---|---|---|
| 1 | Value-bearing output | N/A: test tooling, no data values; JUnit text is the test's own output | none | none |
| 2 | Cleanup on an exit path | Yes: the serial pass must run, and the baseline worktree be removed, when the parallel pass fails or crashes | `gates.sh` runs `cmd2` unconditionally after `cmd`; the existing `trap cleanup EXIT` | AT-2, AT-6 |
| 3 | State machine and concurrency | Yes: tests now run concurrently and share the host | worker-local tmp, dead shared fixtures removed, `serial` for host resources, sentry | AT-11 to AT-13 |
| 4 | Route parity and silent fallback | Yes: serial and parallel runs must report the same ids; a venv without xdist silently runs serial | the `workers:` header line names the mode and why; AT-1, AT-5, AT-8 | AT-8 mutation |
| 5 | Referential integrity | N/A: no data or FK handling | none | none |
| 6 | Untrusted producer | N/A: JUnit and logs are written by this run | none | none |
| 7 | Check then use (TOCTOU) | Yes: `_gate_workers.py` reads MemAvailable once at start; memory can drop during the run | accepted: the serial pass holds the memory-heavy tests; an OOM kill is a failing id (AT-6) | recorded limit |
| 8 | Weak tests and overclaimed evidence | Yes: a parallel run that collects fewer tests and still reads green | AT-3 partition, collected-count compare against the stored baseline, totals cross-check per pass | AT-3, AT-5 |
| 9 | Behavior-preserving refactor drift | Yes: `_gates_report` changes its id source from log text to JUnit | P1 runs the new reporter on the last legacy T3 output (substop2) and must produce the same failing-id set | P1 evidence |
| 10 | Frozen contract drift | Yes: the EVIDENCE block format and the baseline JSON are read by `decoy-vm-t3`, `decoy-vm-t3-promote` and the board | 11d | AT-8, AT-9 |

## 11b. Enumerations

### Exit paths

Today's exits are set at `scripts/dev/gates.sh:200` to `:219`; the reporter's crash rule is
`scripts/dev/_gates_report.py:68` to `:73`.

- `gates.sh` 0: every step green, or (baseline modes) no branch-only failure.
- 1: a new failing id, a branch-only failing step, or (no baseline) any failing step.
- 2: usage error, missing venv, explicit path not found, stored-baseline mode mismatch (new), stored
  baseline for another repo.
- 3: no complete EVIDENCE block, a crashed or aborted pytest (rc not 0, 1 or 5), a missing or
  inconsistent totals line in either pass.
- Serial pass exit 5 alone: not a failure (`combine_rc`).

### Routes

- `gates.sh` with paths (selection), `tests` or `--full`, `--baseline` (legacy, both sides),
  `--baseline-file`, `--write-baseline`; each with `GATES_WORKERS` 1 or more than 1.
- The baseline verdict route: `scripts/dev/_gates_report.py:123` `branch_only_steps`.
- Callers: `decoy-vm-t3` (stored, write, legacy), `vm-run`, the Mac lane, CI after P4.

### FK shapes

- N/A: no FK handling.

### Fields

- Baseline JSON (P1): `repo`, `main_sha`, `head_sha`, `collected`, `failing`, `skipped`, `workers`,
  `dist`, `file_seconds`.
- EVIDENCE header: new `workers:` line (count and reason).

### Output surfaces

- The EVIDENCE block on stdout and in `$OUT/evidence` (`scripts/dev/_gates_report.py:166`), with
  the new `workers:` header line.
- The steps file `<label>.steps` (written at `scripts/dev/gates.sh` line 172, read by `scripts/dev/_gates_report.py:21`).
- JUnit and log files per pass: `<label>-tests.xml` and `.log`, plus `<label>-tests-serial.xml`
  and `.log` after a parallel run.
- The stored baseline JSON (P1).

## 11c. Reuse inventory

Platform `feat/parallel-tests` (`_gate_workers.py`, `xdist_safe_reports.py`, the `gates.sh` two-pass
step, `_gates_report._xmls`, `short_tmp_path`, both test files); platform #156 (stored baseline and
its tests); the engine `xdist` extra; the engine sentry registry and ratchet
(`scripts/dev/sentry_registry.toml`, `tests/sentry/_ratchet.py`); `decoy-vm-t3` stored/write modes
and `decoy-vm-t3-promote`.

## 11d. Frozen contracts

- `gates.sh` flags and exit codes keep their meaning; `--baseline-file` and `--write-baseline` are
  new to the engine with the platform's semantics, plus the exit-2 mode check.
- The EVIDENCE block keeps every line and gains `workers:`. Readers that grep it
  (`decoy-vm-t3` logs, the board) match on the `[tests]` line and the `BRANCH-ONLY` lines, which
  do not change.
- The baseline JSON is the platform's format plus `workers` and `dist`; a reader that ignores
  unknown keys is unaffected. A baseline without the two keys is read as `workers=1, dist=no`.
- The `serial` marker is a new public test marker in `pyproject.toml` markers.

## 12. Pre-mortem

- A test fails only under xdist at some later slice's merge and blocks it. That is the intended
  signal; the sentry makes the common causes fail at the slice's own T1 instead.
- The parallel pass is slower than measured because a VM also runs a builder. P3's lock rule; AT-14
  is measured on an idle VM and reported with load.
- Four workers plus a heavy test exhaust 16 GB and the OOM killer takes the controller, not a
  worker. The memory-envelope tests are `serial`; section 2's peak is the evidence; if a P2 run
  sees available memory under 2 GB, `auto` drops to 3 on 16 GB hosts.
- The DP re-pin slips and P2 waits. P1 still halves the merge test on its own (one run instead of
  two).
- A stored baseline goes stale after a main move with code. `decoy-vm-t3` already refuses it;
  `--write-baseline` reruns once per main move, in the same mode.
- The long determinism test starts last and sets the wall time (section 2). Q3 and Q4.

## 13. Questions (owner)

- **Q1. Does the stored-baseline port (P1) belong in ENGINE-PAR?** The engine `gates.sh` has no
  `--baseline-file`, so every engine merge test today is `legacy`: the full suite twice (branch,
  then `origin/main`). That doubling, not the suite itself, is most of the "4 hours". xdist alone
  cannot reach 60 minutes in legacy mode (two parallel runs plus lint steps). Recommendation: yes,
  P1 first; it is a port of platform #156 with known tests.
- **Q2. Where does pytest-xdist get installed?** It has to be in the lock profile the gate venv
  syncs, and that profile is the certified DP stack, so adding it moves the DP fingerprint.
  Recommendation: add `pytest-xdist` to `dev` inside CI-E1s, which re-pins the fingerprint anyway,
  so it moves once. Fallback: P2 re-pins on its own after CI-E1s merges.
- **Q3. The 25-minute determinism matrix in the merge suite.** `pyproject.toml` addopts excludes
  `determinism_harness` and `testflight`, but `gates.sh` and CI pass their own `-m`, which replaces
  addopts, so `test_full_matrix_against_golden` (1,503 s) and `test_fast_subset_matches_committed_golden`
  (37 s) run in every merge test. Under xdist that one test is the floor of the wall time.
  Recommendation: leave it in for ENGINE-PAR (the failing-id set keeps its meaning); when CI-E2's
  nightly `determinism` job runs it, a one-line follow-up adds `not determinism_harness and not
  testflight` to `gates.sh` `PYTEST_ARGS`, matching what `pyproject.toml` already says, and the
  next baseline write records the smaller collected count.
- **Q4. Distribution mode and long-test order.** Recommendation: `--dist loadfile` everywhere. It
  keeps file order and module fixtures as in a serial run, it produced the same failing ids as
  serial on the Mac, and by the section 2 estimate worksteal is no faster (37.2 against 36.9 min),
  because one file holds 21 percent of the suite. The win is order: P2 starts the longest files
  first (a `pytest_collection_modifyitems` hook in `tests/conftest.py` that sorts files by the
  per-file seconds the stored baseline records from its JUnit, so the order has one source and
  goes stale only until the next baseline write). Estimated 36.9 to 29.8 minutes at 4 serial-speed
  workers. Order inside a file does not change.
- **Q5. Sharing a VM.** A parallel merge suite uses all four cores. Recommendation: `decoy-vm-t3`
  holds the `vmrun` lock while the suite runs, and `vm-run` builder runs use `GATES_WORKERS=2`, so a
  merge test never shares its cores and a builder run never takes a whole VM.
- **Q6. Mac worker count.** Measured: serial 23:43, `-n 4` 9:24 (peak 9.4 GB), `-n 8` 7:51 (peak
  11.5 GB), same 547 failing ids in all three. Recommendation: `GATES_WORKERS=4` on the Mac (the
  `auto` value), which keeps the skill's about-4 GB-per-worker rule and leaves room for the `decoyci`
  CI runner. Eight saves 93 seconds for 2 GB more.
- **Q7. VM machine type.** `e2-standard-4` is two physical cores. Section 2 puts a clean parallel
  merge test near 60 minutes there, against about 10 on the Mac. Recommendation: switch the merge
  VMs to a type with four real cores and 16 GB (for example `c3-standard-8` halved by
  `--threads-per-core=1` gives 4 cores, or `n2-standard-8` with 32 GB), after P2, measured with one
  trial run first. It costs more per hour but each merge test is shorter; the owner decides the
  spend. Until then, route engine merge tests that do not touch `fileaccess/`, `secret_providers/`
  or strict-policy paths to the Mac, as `mac-mini-test-node` already allows.

Technical calls taken (recorded, not asked): the platform's two-pass scheme over `xdist_group`;
`serial` only for host resources, shared state fixed at the cause; the sentry is static (AST), with a
runtime env/cwd guard as FU-1; no reruns of flaky tests.

## 14. Follow-ups

- FU-1: a runtime guard that fails a test which leaves `os.environ` or the cwd changed (cheap
  per-test snapshot), once the inventory is clean.
- FU-2: make the 25-minute `test_full_matrix_against_golden` and the 10-minute
  `test_exhaustive_six_digit_permutation` cheaper, or move them to T4 (Q3).
- FU-3: CLI and platform adopt the same mode-bound baseline check.
- FU-4: xdist on the native companion lane, with a native thread cap per worker.
- FU-5: shard the engine suite across two VMs if one VM's floor (the longest file) becomes the
  bottleneck.

## 15. Done when

- [ ] P1 to P3 merged (P4 after CI-E2); the engine merge test runs once, in parallel, against a
      stored baseline written in the same mode.
- [ ] AT-1 to AT-14 green on the final heads; the sentry's seeded spellings shown red on P2's head.
- [ ] Every id in section 2's inventory fixed or marked `serial` with a reason; three consecutive
      parallel full runs on one VM show no new failing id.
- [ ] AT-14 recorded: an engine merge test under 60 minutes on an idle 4-core VM.
- [ ] Roadmap row 4 and `docs/RECENTLY-SHIPPED.md` (CLI repo) updated when P3 lands.
