# Mutation grading: `execution/_pipeline_routing_signals.py` -- routing-signal computation

TQ isolated-substrate grade (branch `tq/isolated-substrate-grade`) by
`scripts/tq_mutate.py`. Like the other three pure substrates, this module was on
finding #15's "un-gradeable subprocess substrate" list but is PURE and in-process
(zero subprocess references): it computes the routing signals + largest-mask-table
row counts a pipeline uses to pick full-frame / out-of-core / sequential, and
threads arguments to the routing collaborators. Every mutant grades in-process with
the existing tool. Not crypto/RI, so the bar is the measure-first substrate bar
max(baseline 66.45 + 15, 75) = **81.45%**; the re-grade clears it at 98.36%.

SLOW-SUITE NOTE: this module's baseline coverage comes from two integration suites
(`test_byte_estimate_routing.py` ~42s, `test_probe_routing.py` ~41s), so the FIRST
grade's survived-bucket re-adjudication ran ~130s per survivor (~1 hour for 102).
The full-triage kills are FAST direct-unit-tests (0.31s + 0.36s), so the post-kill
re-grade re-adjudicates only the 5 residual survivors and completes quickly. Keep
the fast kill files in the selection; the slow integration files stay for baseline
coverage but the kills do not depend on them.

## Numbers

Baseline (existing `test_byte_estimate_routing.py` + `test_out_of_core_budget.py` +
`test_probe_routing.py`): 202/304 = 66.45% LOGIC, 0 unresolved, 102 survivors. Full
triage of the 102 (two fast kill files, authored per-cluster): **97 LOGIC kills** +
**2 proven equivalent** + **3 accepted non-contract**. Re-grade with both kill files
in the selection: **299/304 = 98.36% LOGIC (tool-native, 0 unresolved)**; all 97
LOGIC targets absent from the survivor set.

| Function | Survivors | Killed | Proven equiv | Accepted non-contract |
|---|---|---|---|---|
| `resolve_execution_route` | 35 | 35 | 0 | 0 |
| `resolve_probe_recovery` | 10 | 10 | 0 | 0 |
| `byte_estimate_full_frame_fits` | 1 | 1 | 0 | 0 |
| `_resolve_largest_mask_table_rows` | 30 | 25 | 2 | 3 |
| `largest_mask_table_rows_from_profile` | 12 | 12 | 0 | 0 |
| `largest_mask_table_rows` | 6 | 6 | 0 | 0 |
| `out_of_core_routing_signals` | 8 | 8 | 0 | 0 |
| **total** | **102** | **97** | **2** | **3** |

Kill files: `tests/unit/execution/test_pipeline_routing_route_kills.py` (10 tests,
0.31s) and `tests/unit/execution/test_pipeline_routing_masktable_kills.py`
(48 tests, 0.36s).

## Kills (97)

### Route + probe wiring (46) -- route kills file
`resolve_execution_route` / `resolve_probe_recovery` / `byte_estimate_full_frame_fits`
are pure DELEGATION: they thread arguments to collaborators
(`out_of_core_routing_signals`, `decide_execution_route`, `resolve_probe_recovery`,
`enforce_ooc_disk_preflight`, the `_probe` / `_mem_estimate` primitives) without
computing a verdict, so each mutant is killed by spying the collaborator in-process
and asserting the forwarded value (the `_mem_telemetry` "forwards every keyword to
the delegate" pattern). Covers the nulled + dropped `decide_execution_route` kwargs
(one `.get(k)==expected` assertion catches both null and drop per param), the
`route == "out_of_core"` OOC-D guard (enforce fires iff the decider chose
out_of_core, pinned from both sides), the `enforce_ooc_disk_preflight` args, the
probe `error_band` default (0.30) + its forward, the resident-sample keyword, the
raw-bytes pre-filter boundary (`raw*k > budget` strict), and the byte-estimate
`error_band` forward.

### Mask-table-rows + OOC signals (51) -- masktable kills file
`largest_mask_table_rows` / `largest_mask_table_rows_from_profile` /
`_resolve_largest_mask_table_rows` / `out_of_core_routing_signals` compute row-count
maxima and the OOC routing default. Killed by direct calls with attribute-only
stubs: the mask-set selection (null / miskey / `!=` / literal / drop-`not`), the
no-mask fallback args, the resident-vs-lazy pick (`is not None` inversion, the
`>`->`>=` equal-row tie), the warn-EMISSION gate (`and`->`or`, `!=`->`==` must stay
silent) and the warn CATEGORY (mutants to UserWarning / a `stacklevel=None`
TypeError, killed collectively by one `pytest.warns(RuntimeWarning)` test -- the
emitted category is machine-observable), and the OOC-signals gate (a FK+mask job
must PROCEED; a relationships-only / has_mask=False job must return the default).

## Proven equivalent (2)

- `_resolve_largest_mask_table_rows` 13, 14: mutate the `best_exact = True`
  initializer (-> None / False). The first loop iteration always hits
  `best_rows is None` (true) and unconditionally reassigns `best_exact` before any
  return, so the init value is never observed. Unkillable by construction.

## Accepted non-contract (3)

- `_resolve_largest_mask_table_rows` 23 (warn message -> None), 28 (drop
  `stacklevel`), 29 (`stacklevel=2` -> 3). The warning still fires as a
  `RuntimeWarning` with unchanged routing behavior; only the diagnostic prose /
  reported frame differs. No message- or stacklevel-equality test, per house style.
  (The warn CATEGORY mutants ARE killed -- category is a machine contract; only the
  message text and frame pointer are non-contract.)

## Candidate findings

None. No mutation exposed a wrong execution route, a wrong signal forwarded to the
decider, a wrong probe verdict, a wrong pre-filter boundary, a wrong largest-mask
row count, a wrong resident-vs-lazy pick, or a wrong OOC default.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to
`src/decoy_engine/execution/_pipeline_routing_signals.py` and the test selection to
`tests/unit/execution/test_pipeline_routing_route_kills.py`,
`tests/unit/execution/test_pipeline_routing_masktable_kills.py`,
`tests/unit/execution/test_byte_estimate_routing.py`,
`tests/unit/execution/test_out_of_core_budget.py`, and
`tests/unit/execution/test_probe_routing.py`; then `rm -rf mutants && python
scripts/tq_mutate.py --run --jobs 6`. The two integration files are slow (~42s
each); the fast kill files carry the full-triage kills. `source_paths` stays at the
package root.
