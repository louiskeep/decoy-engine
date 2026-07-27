# Mutation grading: `execution/capacity.py` -- substrate bar 75%

TQ substrate sweep (branch `tq/substrate-sweep`), re-graded and finalized.
`capacity.py` exposes one public function, `estimate_job_capacity`: the
estimate-only entrypoint for the out-of-core-FK memory capacity gate (`decoy
preflight` / `decoy run`). It derives the routing inputs by calling the same
engine primitives `run_pipeline` uses, asks `decide_execution_route` twice (a
row-count-only decision, then a worst-case byte-estimate probe), and defers the
final FIT/INSUFFICIENT/UNKNOWN/NOT_APPLICABLE verdict to the shared
`evaluate_capacity`. This is a substrate module (a route/capacity gate), not
crypto/RI, so the bar is **75% of LOGIC mutants**, not 100%.

This round addresses the surviving mutation class that nulls (or otherwise
mutates) one argument VALUE in the long kwarg list forwarded to each of the two
`decide_execution_route` calls. Nulling a load-bearing kwarg re-routes the job,
which changes the FULL returned `CapacityEstimate`. The reliable oracle is a
full-struct golden: assert every field of the returned struct
(`verdict`/`code`/`needed_bytes`/`available_bytes`/`route`/`message`/`warned`/
`binding_table`/`floor_bytes`/`cap_bytes`) for a set of job shapes chosen so
each kwarg is load-bearing in at least one of them.

## Numbers

**Re-graded (`scripts/tq_mutate.py`): 281/328 killed = 85.67% LOGIC, 0 unresolved
-- clears the measured bar (max(baseline 64.94 + 15, 75) = 79.94%).** Baseline was
213 killed / 115 survived (64.94%). This sweep's new oracles killed 68 of the 115
survivors (16 per-field/verdict oracles + 6 full-`CapacityEstimate` golden shapes),
leaving **47 survivors** at 85.67%. 0 product bugs -- this is the fail-closed-DIRECTION
argument (nulling any load-bearing kwarg only ever degrades the verdict toward
UNKNOWN/NOT_APPLICABLE, never fabricates a false FIT), not a per-survivor verification.

| Function | Total mutants | Killed | Survived |
|---|---|---|---|
| `estimate_job_capacity` (+ helpers) | 328 | 281 | 47 |

### The 47 residual survivors (HONEST scope note)
Unlike `_when_gate` and `_planner` (every survivor individually triaged), the 47
here are NOT each individually verified -- `estimate_job_capacity` threads ~two
dozen kwargs into two `decide_execution_route` calls, and exhaustively pinning
every kwarg's effect across the full routing decision tree is disproportionate
effort for a module already comfortably above its bar. The residual is dominated
by the equivalence CLASSES identified below (whose reasoning holds by
construction), plus route-kwarg `=None` mutants whose effect the tested job shapes
do not distinguish (plausibly equivalent -- the kwarg does not change the verdict
for reachable shapes -- but not each proven). A future pass can squeeze further by
adding job-shape goldens. This is an above-bar residual, deliberately not claimed
as all-equivalent.

Equivalence classes (proven by construction, from the focused-kill analysis):
- **`execution_mode`** (both call sites): `"auto"` is the fall-through default read
  only by `== "..."` checks, so `None` / `"XXautoXX"` route identically.
- **`use_probe_routing`** (both call sites): inert -- read only as
  `use_probe_routing and probe_recovers_full_frame is True`, and
  `estimate_job_capacity` never passes `probe_recovers_full_frame` (defaults None),
  so the guard is always False regardless.
- **`full_frame_fits_estimate=None`** (call 2): read as `is True`, so `None` and the
  real `False` behave identically (the distinct `False->True` flip IS killed).
- **reject-branch kwargs on call 2** (`out_of_core_reject_code`,
  `largest_table_rows(_exact)`): the byte-probe reject branch IS reachable on call 2
  (e.g. an ooc-compatible-but-not-sequential-eligible validators job:
  `_sequential_eligible` is False on `validators_present` while admission ignores
  validators, so call 2 falls into the byte-branch reject). Equivalent anyway because
  `estimate_job_capacity` SWALLOWS every call-2 reject to `byte_route=None`
  (capacity.py:374-382), so `out_of_core_reject_code`'s message never surfaces, and
  `largest_table_rows(_exact)` are not read in the byte-estimate branch at all. Killed
  on call 1 by the reject shape. (If the swallow behavior ever changed to surface the
  byte reject, `out_of_core_reject_code` on call 2 would become killable.)

## Tests

Six full-struct oracles added to
`tests/unit/execution/test_capacity_estimate_job_mutation_kills.py`
(class `TestRouteKwargFullStructKills`), each asserting the ENTIRE returned
`CapacityEstimate` via `est == CapacityEstimate(...)` against hardcoded golden
literals read off the real (unmutated) code. Two new config builders
(`_generate_plus_mask_config`, `_reject_code_config`) join the existing
`_ooc_config` helper. All 36 tests in the two capacity files green on unmutated
code; ruff format + check clean.

Explicit budgets pin `available_bytes` / `cap_bytes` deterministically instead
of depending on the host's detected RAM, so the goldens are portable.

The shapes and the call-site kwargs each makes load-bearing:

| Shape (test) | Route / verdict | Row-count call (1) kwargs killed | Byte-probe call (2) kwargs killed |
|---|---|---|---|
| FIT out_of_core (low_threshold, 64 GiB) | out_of_core / FIT | has_mask_table, out_of_core_compatible, largest_table_rows, resolved_substrate, graph, fidelity_report, vault_writer | -- |
| INSUFFICIENT out_of_core (low_threshold, 1 MiB, 300k) | out_of_core / INSUFFICIENT | (reinforces above; pins INSUFFICIENT code/needed/floor/cap) | -- |
| UNKNOWN probe-promoted (no low_threshold, 64 GiB) | out_of_core / UNKNOWN | -- | has_mask_table, out_of_core_compatible, resolved_substrate, use_byte_estimate_routing, full_frame_fits_estimate, graph |
| generate+mask (no low_threshold) | full_frame / NOT_APPLICABLE | has_generate_table | -- |
| validators + ooc-compatible (no low_threshold) | full_frame / NOT_APPLICABLE | use_byte_estimate_routing (flip -> reject) | validators |
| reject-before-read (low_threshold, faker+validators) | rejected_before_read / NOT_APPLICABLE | validators, out_of_core_reject_code, largest_table_rows_exact, largest_table_rows | -- |

## New-oracle kill shapes (what the added tests pin)

### Row-count-only call (call 1)

- **has_mask_table / out_of_core_compatible / largest_table_rows**: nulled, each
  drops `out_of_core_ready` to False, so the FIT/INSUFFICIENT out_of_core job
  falls to `sequential` -> NOT_APPLICABLE. The full-struct FIT and INSUFFICIENT
  goldens (route `out_of_core`) diverge.
- **resolved_substrate**: nulled, `None != "pandas"` makes the job
  sequential-INeligible (`non_pandas_substrate_requested`), so it leaves the
  out_of_core route. FIT golden diverges.
- **graph**: nulled, `_has_cross_table_fk_cycle(None)` raises `AttributeError`
  (no `.edges`); the FIT shape expects a struct, so the mutant errors -> killed.
- **has_generate_table**: nulled, a generate+mask job looks pure-mask and
  reroutes off `full_frame` (to `sequential` here). The generate+mask golden
  (route `full_frame`, NOT_APPLICABLE) diverges.
- **validators**: nulled, the faker+validators job becomes sequential-eligible
  and no longer hits the reject-before-read branch. The reject golden (route
  `rejected_before_read`) diverges.
- **out_of_core_reject_code**: nulled, the reject message renders the
  `or 'not a pure-mask FK recipe'` fallback instead of the real
  `out_of_core_faker_pool_unsupported`; the pinned reject message diverges.
- **largest_table_rows_exact**: nulled (falsy), the reject branch takes the
  ESTIMATED path (`fk_full_frame_oom_risk_rejected_estimated`) with different
  wording; the pinned reject message diverges.
- **fidelity_report / vault_writer**: a truthy/non-None flip makes the FIT job
  sequential-INeligible -> route change; the FIT golden diverges. (These are
  killed by the FIT shape, not equivalent -- the flip is observable even though
  a hypothetical exact `=None` on a value already falsy/None would not be.)
- **use_byte_estimate_routing (flip)**: turned ON for the validators+compatible
  shape, call 1 enters the byte-estimate branch and rejects-before-read instead
  of returning `full_frame`; that golden (route `full_frame`) diverges.

### Byte-estimate probe call (call 2)

Reached only for an out-of-core-COMPATIBLE job whose row-count route is below
threshold (the UNKNOWN probe-promotion shape) or that is full-frame-bound but
compatible (the validators+compatible shape).

- **use_byte_estimate_routing**: nulled/off, the probe stops taking the byte
  branch, so a below-threshold job is not promoted to out_of_core; the UNKNOWN
  golden (route `out_of_core`) collapses to NOT_APPLICABLE `sequential`.
- **has_mask_table / out_of_core_compatible**: nulled, the byte branch's scope
  guard (`... and has_mask_table`) or its bounded-route test
  (`eligible and not cyclic and out_of_core_compatible`) fails, so the job is
  not promoted; the UNKNOWN golden diverges.
- **resolved_substrate**: nulled, the probe's `_sequential_eligible` fails,
  the byte branch reject-raises, the raise is swallowed to `byte_route=None`,
  and the job is not promoted; the UNKNOWN golden diverges.
- **full_frame_fits_estimate (flip to True)**: the probe returns `full_frame`
  instead of a bounded route, so the job is not promoted to out_of_core; the
  UNKNOWN golden diverges.
- **validators**: nulled, the validators+compatible job becomes eligible, the
  probe promotes it to out_of_core, and the verdict flips NOT_APPLICABLE ->
  UNKNOWN (verified empirically); that golden diverges.
- **graph**: nulled, the second `_has_cross_table_fk_cycle(None)` raises inside
  the probe; the UNKNOWN shape errors -> killed.

## Sample proven-equivalent survivors (see equivalence classes above for the full rationale)

| Kwarg (call site) | Why equivalent |
|---|---|
| `execution_mode` (call 1) | `"auto"` is the FALL-THROUGH default: the three `if execution_mode == "full_frame"/"out_of_core"/"sequential"` checks are the only reads, so any value that is not one of those three (a `None`, or the `"XXautoXX"` string-wrap) routes through the identical auto logic. The machine fields are asserted independently and do not change. |
| `execution_mode` (call 2) | Same fall-through reason, at the probe call. |
| `use_probe_routing` (call 1) | INERT: the only read is `if use_probe_routing and probe_recovers_full_frame is True`, and `estimate_job_capacity` never supplies `probe_recovers_full_frame` (it defaults `None`), so `None is True` is always False and the whole condition is False regardless of the flag. Additionally, on call 1 the byte branch is off entirely. |
| `use_probe_routing` (call 2) | Same inert reason: `probe_recovers_full_frame` is never passed, so the recovery condition can never fire whatever this flag is. |
| `full_frame_fits_estimate` (call 2, `=None` form) | The only read is `if full_frame_fits_estimate is True`; `None` and the real `False` both fail that test identically and fall to the same bounded-route logic -> the job is still promoted to out_of_core -> UNKNOWN unchanged. (The `False -> True` FLIP form is a DISTINCT mutant and IS killed by the UNKNOWN shape.) |

Note on the call-2 reject-branch-only kwargs (`out_of_core_reject_code`,
`largest_table_rows`, `largest_table_rows_exact`): the byte branch's
reject-before-read IS reachable on call 2 (an ooc-compatible job that is NOT
sequential-eligible -- e.g. validators present -- falls through to the byte-branch
reject). They are equivalent anyway because `estimate_job_capacity` swallows every
call-2 reject to `byte_route=None` (capacity.py:374-382) so the reject-code message
is discarded, and `largest_table_rows(_exact)` are not read in the byte-estimate
branch. Killed on call 1 by the reject shape.

## Candidate findings

None. Every kwarg carries the routing input its docstring specifies. Nulling any
load-bearing one degrades the verdict conservatively (a false FIT never appears;
the worst outcome is an over-cautious UNKNOWN or a NOT_APPLICABLE), consistent
with the module's fail-closed / never-report-fine-on-a-refused-job contract.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to
`src/decoy_engine/execution/capacity.py` and the test selection to
`tests/unit/execution/test_capacity_estimate_job_mutation_kills.py` and
`tests/unit/execution/test_capacity_estimate_job.py`, then
`rm -rf mutants && python -m mutmut run`. `source_paths` stays at the package
root.
