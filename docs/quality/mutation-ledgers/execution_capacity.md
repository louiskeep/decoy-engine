# Mutation grading: `execution/capacity.py` -- substrate bar 79.94%

TQ substrate sweep (branch `tq/substrate-sweep`), FULL-TRIAGE grade by
`scripts/tq_mutate.py` with default survived-bucket re-adjudication (finding #16
RESOLVED). Every surviving mutant is now individually adjudicated -- killed or
proven equivalent -- with ZERO residual (replaces the earlier "47 survivors not
individually verified" honest-residual note). `capacity.py` exposes one public
function, `estimate_job_capacity`: the estimate-only entrypoint for the
out-of-core-FK memory capacity gate (`decoy preflight` / `decoy run`). It derives
the routing inputs by calling the same engine primitives `run_pipeline` uses,
asks `decide_execution_route` twice (a row-count-only decision, then a worst-case
byte-estimate probe), and defers the final FIT/INSUFFICIENT/UNKNOWN/NOT_APPLICABLE
verdict to the shared `evaluate_capacity`. Not crypto/RI, so the bar is the
measure-first substrate bar max(baseline 64.94 + 15, 75) = **79.94% of LOGIC
mutants** (the +15 over baseline clears the 75% floor).

## Numbers

**TRUE score: 285/328 = 86.89% LOGIC (tool-native, 0 unresolved), above the
measure-first bar max(baseline 64.94 + 15, 75) = 79.94%. 43 survivors, ALL proven
equivalent -- 0 residual (no message-prose survivors: the one prose mutant, 33, is
KILLED; the 43 are fall-through defaults, inert guards, and dead-branch
unreachability).**

Re-grading with the fixed tool (survived re-adjudication) reproduced the prior
281/328 = 85.67% exactly: capacity had NO false-survived (unlike `_chunked`, its
full-struct oracles were properly credited by mutmut's coverage map). The full
triage then individually adjudicated all 47 survivors: **4 additional kills** (via
4 new tests) and **43 proven equivalent**, each verified to survive the full
selection standalone (rc 0).

| Function | Total mutants | Killed | Equivalent (survivors) |
|---|---|---|---|
| `estimate_job_capacity` (+ helpers) | 328 | 285 | 43 |

## Kills added by the full triage (4)

| mut | mutation | killed by (machine field) |
|---|---|---|
| 33 | `type(exc).__name__` -> `type(None).__name__` in the corrupt-source message | `TestCorruptSourceTypeName::test_reader_exception_type_name_is_real` -- a truncated CSV asserts the REAL reader exception type name (`ArrowInvalid`) is in the message and `NoneType` is not; the mutant emits `NoneType`. |
| 116 | call-1 drops `largest_table_rows_exact=` (-> callee default `True`) | `TestCsvEstimatedRejectCode::test_csv_full_frame_reject_uses_estimated_code` -- a CSV + faker + validators full-frame-reject job: the real inexact CSV size raises the `..._estimated` reject code; the mutant's default `exact=True` takes the non-estimated path, so the pinned code/message diverges. |
| 220 | `continue` -> `break` in the parent-rows-unresolved loop | `TestParentRowsLoopContinues::test_all_unresolved_parents_named_not_just_first` -- two non-substring CSV build parents; the real loop names BOTH in the UNKNOWN message, `break` names only the first. |
| 229 | `_max_concurrent_ooc_instances(graph, sink=False)` -> `sink=True` | `TestMaxConcurrentSinkFalse::test_sink_false_fanin_guard_fires` -- a star fan-in of 67 where `child` is also a build table: `sink=False` prices peak concurrency 68 and `resolve_ooc_memory_limit` raises `out_of_core_fanin_exceeds_budget`; `sink=True` prices 67 and does not raise. |

(mut_88 and mut_139, initially flagged as kill candidates, proved equivalent -- see
below. The kills 33 and 116 were found beyond the initial candidate set.)

## EQUIVALENT survivors (43) -- proven, by class

Each was verified to survive the full selection standalone (MUTANT_UNDER_TEST set,
both capacity test files, rc 0).

- **execution_mode fall-through** (95, 124, 125, 152, 183, 184): `"auto"` is the
  fall-through default -- only `== "full_frame"/"out_of_core"/"sequential"` reads
  it, so `None` / `"XXautoXX"` / `"AUTO"` all route identically.
- **use_probe_routing inert** (103, 118, 129, 161, 177, 189): read only as
  `use_probe_routing and probe_recovers_full_frame is True`, and this function
  never passes `probe_recovers_full_frame` (defaults None), so the guard is always
  False for ANY value incl. `True`. 118/177 drop the kwarg (callee default `True`),
  still inert.
- **use_byte_estimate_routing** (102 call-1 `=None`; 175 call-2 dropped): 102's
  `None` is falsy == the passed `False`; 175 dropped -> callee default `True` == the
  passed `True`.
- **fidelity_report `=None`** (94, 151): read as `if fidelity_report:`; `None`
  falsy == `False`.
- **full_frame_fits_estimate** (160 `=None`; 176 dropped): `decide_execution_route`
  reads it as `is True`, so `None` (and the dropped-default) behave identically to
  the passed `False` (its own rule that an unconfirmed estimate == "does not fit").
- **has_generate_table `=None` (call 2)** (148): call 2 runs only under
  `not has_generate_table` (always False there), so `None` behaves identically.
- **resolved_substrate dropped** (112 call 1, 170 call 2): callee default is
  `"pandas"` == the passed value.
- **call-2 reject-branch kwargs** (156, 157, 158, 172, 173, 174): call 2 requires
  `out_of_core_compatible`, so byte-mode routing returns `out_of_core` before these
  are read, and any call-2 reject is swallowed to `byte_route=None`. Verified against
  the existing probe / validators full-struct goldens.
- **seed** (20 `job_seed=None`, 24 `seed=None`, 26 dropped): the seed changes only
  the profile SAMPLE, not the row-count / route the capacity verdict uses.
- **engine_version** (53 `decoy_engine_version=None`): not read by routing / capacity.
- **size-signal default** (88 `(None, True)` -> `(None, False)`): only reached when
  `size_signal is None`, which means no mask table, so `largest_table_rows=None` and
  `largest_table_rows_exact` is never read (every size gate guards on
  `largest_table_rows is not None`).
- **ooc_route_uncertain** (139 `False` -> `None`): read only as
  `if ooc_route_uncertain and ...`; `None` and `False` are both falsy, and it is set
  to `True` on the promotion path regardless.
- **byte_route sentinel** (191 `None` -> `""`): compared only `== "out_of_core"`;
  both sentinels differ from it, and it is assigned only in the swallowed-reject branch.
- **sink `=None`** (226, 243): every read is `1 if sink else ...` / `if sink`; `None`
  falsy == the intended `False`.
- **parent-rows-unresolved branch, code/message** (211, 212, 213, 214, 215, 216, 217):
  equivalent by UNREACHABILITY -- this raise-branch needs a graph parent missing from
  the profile WHILE the route is already `out_of_core`, but a missing parent source
  makes `out_of_core_admission` return `(False, 'out_of_core_parent_seed_missing')`,
  so such a job never routes `out_of_core` and never reaches this branch. Dead code
  under the reachable input space (see tq-findings #17); its mutants are unkillable.

## Candidate findings

**#17 (dead branch):** the parent-rows-unresolved `raise` in `estimate_job_capacity`
(the `_PARENT_ROWS_UNRESOLVED_CODE` branch) appears unreachable under the current
admission logic -- a missing parent source is caught earlier by `out_of_core_admission`
(`out_of_core_parent_seed_missing`), so the route is never `out_of_core` when this
branch's guard holds. Logged in tq-findings.md for a decision: confirm dead and remove,
or confirm a reachable path the sweep did not find. No product bug otherwise: every
kwarg carries its documented routing input, and nulling any load-bearing one degrades
the verdict conservatively (never a false FIT).

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `src/decoy_engine/execution/capacity.py`
and the test selection to `tests/unit/execution/test_capacity_estimate_job_mutation_kills.py`
and `tests/unit/execution/test_capacity_estimate_job.py`, then
`rm -rf mutants && python scripts/tq_mutate.py --run` (survived re-adjudication on
by default). `source_paths` stays at the package root.
