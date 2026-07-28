# Mutation grading: `execution/_governor.py` -- runtime governor (reroute ladder)

TQ isolated-substrate grade (branch `tq/isolated-substrate-grade`) by
`scripts/tq_mutate.py`. `_governor.py` is the runtime governor: it runs a job down a
supervisor-kills-child reroute LADDER, delegating each rung's actual spawn to
`run_pipeline_isolated`. Finding #15 listed it as an "un-gradeable subprocess
substrate" whose baseline guard would abort ("the tests never call the mutated
functions"). That is EMPIRICALLY WRONG: the governor's own decision logic runs in
the PARENT process, and `test_governor.py` mocks only the spawn (`run_pipeline_isolated`)
+ the RSS monitor, so the reroute-ladder logic IS exercised. The baseline sanity
check passes (forced-fail rc 1), and the module grades in-process with the existing
tool -- no de-mock, no standalone-per-mutant runner. Public surface:
`run_job_with_governor` + helpers (`_run_one_rung`, `_run_flag_off`,
`_is_route_ineligible_error`, `_observed_peak_mb`, `_validate_call`,
`_exhausted_diagnostic`). Not crypto/RI.

## Numbers

Baseline (existing `test_governor.py`): 267/366 = 72.95% LOGIC. Full triage of the
99 survivors (98 from the baseline survivor list + mut 110, see below): **46 LOGIC
kills** + **18 proven equivalent** + **35 accepted non-contract**. Re-grade with two
kill files in the selection: **313/366 = 85.52% LOGIC (tool-native, 0 unresolved)**;
313/313 = 100% of the KILLABLE mutants are killed.

BAR NOTE (honest): the measure-first target is max(72.95 + 15, 75) = 87.95%, and the
85.52% inclusive score falls BELOW it. This is the same situation as `_chunked_fk`:
53/366 = 14.5% of all mutants are UNKILLABLE (18 unreachable/invariant + 35
reachable-branch message prose), which caps the achievable inclusive score below the
+15 heuristic. Every killable (LOGIC) mutant IS killed; the shortfall is entirely the
validation/diagnostic message-prose fraction, not a coverage gap. Per Cam's
honest-taxonomy policy, full triage (kill all killable, honestly classify the rest)
is the DoD; the +15 target assumes killable headroom this diagnostic-heavy module
does not have.

FLAKY-FALSE-KILL NOTE (methodology, flagged for tq-findings): mut 110
(`run_job_with_governor` `on_trip(trip)` -> `on_trip(None)` in the genuine-crash
branch) was flakily marked KILLED by mutmut's in-process runner in one grade pass,
so it was absent from the baseline survivor list and never triaged. It is a REAL
LOGIC survivor (`test_governor.py` does not kill it standalone, rc 0). `tq_mutate`
TRUSTS mutmut's "killed" as monotonic (finding #16 only re-adjudicates SURVIVED), so
a flaky false-KILL slips through the baseline. The AUTHORITATIVE re-grade caught it
(110 showed survived under full re-adjudication); a targeted test now kills it. The
lesson: the final re-grade, not the baseline pass, is authoritative -- and mutmut's
in-process runner can false-KILL as well as false-survive.

| Function | Survivors triaged | Killed | Proven equiv | Accepted non-contract |
|---|---|---|---|---|
| `run_job_with_governor` | 37 | 21 | 16 | 0 |
| `_validate_call` | 25 | 4 | 0 | 21 |
| `_exhausted_diagnostic` | 22 | 9 | 0 | 13 |
| `_run_flag_off` | 6 | 3 | 2 | 1 |
| `_run_one_rung` | 2 | 2 | 0 | 0 |
| `_is_route_ineligible_error` | 4 | 4 | 0 | 0 |
| `_observed_peak_mb` | 3 | 3 | 0 | 0 |
| **total** | **99** | **46** | **18** | **35** |

Kill files: `tests/unit/execution/test_governor_orchestration_kills.py` (15 tests --
14 from the orchestration triage + the mut-110 genuine-crash on_trip kill) and
`tests/unit/execution/test_governor_validate_diag_kills.py` (6 tests).

## Kills (46)

### Orchestration (32 + mut 110) -- orchestration kills file
`run_job_with_governor` / `_run_one_rung` / `_run_flag_off` /
`_is_route_ineligible_error` / `_observed_peak_mb`. Tests monkeypatch
`run_pipeline_isolated` + the RSS monitor to controlled in-process fakes (as
`test_governor.py` does) so the governor's OWN logic runs, and assert the exact
machine field each mutation flips: the reroute-ladder trip records per branch (route
/ budget_bytes / error / trip_kind on the route_ineligible, genuine-crash, and
oom/self_oom paths), the self-OOM-vs-governor-kill classifier
(`monitor is not None and monitor.tripped`), the completed-run result carriage, the
config/sources forwarding through both `_run_flag_off` and `_run_one_rung`, the
`use_runtime_governor` default (True) + its typed-knob validation, the `on_trip`
callback payload (BOTH call sites -- the shared reroute one, mut 158, and the
genuine-crash-branch one, mut 110), the route-ineligibility verdict, and the
monitor-vs-result peak selection.

### Validation + diagnostics (13) -- validate/diag kills file
`_validate_call` (4): the `or`->`and` bool-guard collapses (a Python bool IS an int,
so the `and` form makes each bool guard always-False -- `True` would be silently
accepted as budget/poll `1` or fraction `1.0`) and the `budget_bytes <= 0`->`<= 1`
boundary (1 is the smallest valid budget). `_exhausted_diagnostic` (9): the
budget-MB arithmetic (structured data: "exceeded the 100.0MB budget"), the
observed-peak rendering ("peak=95.0MB"), and the route_ineligible branch selection
(misrouting an ineligible rung to the peak-exceeded branch drops `trip.error`).

## Proven equivalent (18)

- `run_job_with_governor` 176-191 (16): mutate the trailing `return GovernorResult(...)`
  AFTER the ladder loop. Unreachable by construction -- the loop over a validated
  non-empty ladder always returns on its last iteration (completed / genuine-crash /
  `next_route is None`); the trailing return exists only for mypy exhaustiveness.
- `_run_flag_off` 19, 29: drop an explicit `trips=()` whose `GovernorResult.trips`
  dataclass field already defaults to `()` -- byte-identical.

## Accepted non-contract (35)

Reachable-branch message prose, killable only by brittle full-message-equality
(the raised error's identifying substring -- `budget_bytes` / `hard_threshold_fraction`
/ `poll_interval_s` / `ladder` / `isolate`, and the diagnostic's structured data --
is pinned; the explanatory sentence is not). Each verified to survive standalone (rc 0).
- `_validate_call` 3, 20, 36-41, 48-60 (21): the ValueError message prose across the
  budget / fraction / poll / ladder / isolate validators, plus the them/it grammar
  mutants (36-41: `>`/`>=` comparisons whose ONLY observable effect is the
  pluralization word; the collision set carried as data is unchanged).
- `_exhausted_diagnostic` 17, 18, 22, 23, 25-33 (13): the leading/trailing
  explanatory sentences, the "unknown" no-peak sentinel word, the "; " join
  delimiter, and letter case.
- `_run_flag_off` 33 (1): the flag-off diagnostic prose (the `use_runtime_governor=False`
  substring survives).

## Candidate findings

No product bug. One METHODOLOGY finding (flagged above, owed to tq-findings): mutmut's
in-process runner can flakily FALSE-KILL a genuine survivor (mut 110), which
`tq_mutate` trusts; the authoritative re-grade is the reliable check.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `src/decoy_engine/execution/_governor.py`
and the test selection to `tests/unit/execution/test_governor_orchestration_kills.py`,
`tests/unit/execution/test_governor_validate_diag_kills.py`, and
`tests/unit/execution/test_governor.py`; then `rm -rf mutants && python
scripts/tq_mutate.py --run --jobs 6`. `source_paths` stays at the package root.
