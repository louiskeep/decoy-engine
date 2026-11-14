# POSTSCAN-STREAM PS-8 build record: the post-scan gate wired into run_pipeline

Status: record

Plan: `docs/plans/2026-11-12-postscan-stream.md` on `plan/postscan-stream` (4.2, 4.3, 4.5, 4.7, AT-14, AT-16, AT-20, AT-24, AT-25).
Branch `feat/postscan-ps8`, cut from `origin/main` 8c9c1162.

## Plan amendments (tech-lead rulings, 2026-11-13)

These go into the plan branch as written here.

- 4.2, generated parent. The baseline of an edge whose parent is generated is "inherited": the count of input child key values that match no output parent key. The gate then measures introduced orphans as output orphans minus that count, both against the output parent keys. "A generate table has baseline 0" still holds for a generated child. A source orphan under `preserve` or `warn` therefore does not block, and a mask that breaks a matched child key still does.
- 4.3, deferred routes (supersedes the earlier hold). PS-8 refuses no run that succeeds on main. On a route where the gate cannot read the run back it defers: the run is checked as before (the column suite, report only, only under `post_scan.integrity`) and the result carries `quality_metrics["post_scan_gate_deferred"]`, reason `post_scan.gate_deferred_route`. PS-10 makes those routes blocking.
- Route types: routing takes `ReasonCode | None` (the type route reasons already have), not `ReasonRef`.
- 4.2, quarantine (2026-11-13, tech-lead ruling on review B4). Quarantine and row-error removal run before the gate, so a removed parent row would read as an orphan the run introduced. The gate records the parent keys quarantine removed (keys only, in memory, never reported or logged) and counts orphans against the output parents plus those keys. A child that points at a removed parent is not introduced; it is reported as the warning `post_scan.foreign_key_orphans_quarantined`. Any other new orphan still blocks.
- 4.2, mixed parents (review H1). The inherited baseline applies to an edge only when every parent end of it is absent from the inputs. With one generated and one masked parent the edge is measured against its inputs, so a mask that breaks every child key blocks. An inherited type mismatch (child inputs against a generated parent) is reported as baseline `inherited_type_mismatch`, not `no_source`, and blocks as before.
- 4.5, key scan resources (review B2, M3). The key scan's DuckDB cap is min(512MB, the run budget share), never below 128MB. It binds the run's `out_of_core_threads` and spills to the gate's scratch directory. AT-15 keeps "at most 512MB".
- 4.2, null-typed keys (review B3). A null-typed (all-null or zero-row) key column is comparable with every family: it holds no value that can match or orphan.

## What was built

- `config/_post_scan.py`: the model predicate and `post_scan_resident_reason` (integrity keeps a job resident, as on main); `execution/_transforms_gate.py::post_scan_resident_for` is the one helper every routing site calls.
- `execution/pipeline/_post_scan.py`: `bind_gate`, `active_gate`, `settle`; lazy inputs are re-read in batches and checked against a footer snapshot taken at bind time; a deferred run is checked report-only.
- `execute_route` binds the gate and settles it after the route; `observed_commit` asks the gate first; `compute_post_scan_integrity` is deleted.
- Sentries: `post-scan-commit`, `post-scan-model-predicate`, `post-scan-readability`.

## Route table: which shapes block and which defer

The gate defers only where the run's output cannot be read back before commit, or its inputs cannot be re-read.

| Shape | Gate |
|---|---|
| Full-frame, no sink | blocks |
| Full-frame, resident output, sink (output committed after the gate) | blocks, sink untouched |
| Sequential, out-of-core or mixed returning resident outputs (no sink) | blocks |
| `post_scan.integrity: true`, any sink, `LazySource` inputs | resident full-frame, blocks (also on a failing column finding) |
| Lazy inputs, no sink | blocks, inputs re-read in batches with the footer check |
| Sequential or out-of-core writing tables to a sink as it goes | defers (the gate has no resident output at commit) |
| Output streamed to a sink | defers |
| Inputs from a `source_loader` | defers |
| Declared casting on a bounded route | defers |
| Declared casting on full-frame | blocks |

Cells: `test_wiring.py` (`test_at16_*`, `test_at20_*`), `test_gate.py` (`before_commit` and non-resident outputs). `test_b6a_publish.py` is back to main's version.

## Test edits

- `test_job_estimate.py`: the router spy reads `post_scan_resident` (renamed kwarg); assertion unchanged.
- `test_fk_findings.py`: the test of the deleted in-run helper calls `run_post_mask(keys=None)`; assertions unchanged.
- `test_f29_remediation.py`: the stale `post_scan_integrity=` kwarg is the renamed `post_scan_resident=`. A rename.
- `r3_within_route_baseline.json`: regenerated, additive only (0 lines removed): `post_scan_integrity` gains a block on declared-key jobs. `test_r3_route_executors.py` compares a direct executor call without the keys `execute_route` stamps (`post_scan_integrity`, `post_scan_gate_deferred`). `test_r3_hypothesis_parity.py` lists `post_scan_integrity` as route evidence on the bounded FK job.
- Fixtures that collapsed a declared key (real duplicates counted: 190240 excess of 200000 rows, 4 of 5 rows) moved to the key-preserving `hash` strategy for the key columns, nothing else: `_faker_col` in `test_probe_routing.py` and `test_byte_estimate_routing.py`; `_dup_pk_fk_config(keep_keys=True)` for `test_run_pipeline_fk_job_runs_full_frame_and_reaches_seam` and `test_post_validation_off_at_finalize_seam_is_inert`.

## e1c test expectations changed on purpose (tech-lead ruling 2026-11-13)

The gate's DuckDB connection is a run connection: `execute_route` binds one recorder for the route and the gate, and refreshes `resources.out_of_core.applied` after the gate ran. The gate's cap is `min(512MB, run budget share)`, at least 128MB, so its thread count follows its own limit. In `test_e1c_duckdb_threads.py`:

- `test_every_nested_connection_sees_the_cap`: was "every connection has threads == 2"; now every connection equals the formula from its own memory limit and the cap, and the route connections still reach 2. The gate's 512MB connection gives 1 thread by that formula.
- `test_no_cap_keeps_no_user_cap_but_honors_detected_cpus`: same change, with 3 CPUs and no cap.
- `test_a_cap_above_the_detected_cpus_is_clamped_to_them`: was `{threads} == {4}`; now every connection equals the formula with the cap 64 clamped to the 4 CPUs, and the largest is 4, so the cap is still clamped to the CPUs.
- `test_applied_pairs_are_the_distinct_connection_settings`: unchanged. It now holds because `applied` includes the gate's connection.
- `test_a_small_memory_limit_takes_threads_below_the_cap`: unchanged.

## Key type comparison (orchestrator ruling, 2026-11-13, revised)

The gate counts orphans, nulls and duplicates by the FK join's own key identity, not by an Arrow
cast. Every join route matches through the token functions (`fk_key_value`, `fk_join_key`,
`fk_join_key_tuple`), which now live in `decoy_engine/internal/fk_tokens.py`, a tier both the
profiling and execution packages may import from, so there is no back import (`execution/_fk_keys.py` re-exports them). In
token space the string "7" never equals the integer 7 and a date never equals a midnight
timestamp, so a cast-based scan disagreed with the join in both directions: a string parent with an
integer child blocked a run the join published, and an orphan to the join could look like a match.

When every end of a key label has one type, or the ends differ only in integer width or string
encoding (plain, large, dictionary), the values still compare by type in DuckDB (BIGINT or
HUGEINT, VARCHAR), which agrees with the tokens. Any other mix maps each batch's values to the
join token as VARCHAR before loading, batch by batch; a null stays SQL NULL, so null counting is
unchanged. Zone-aware timestamps and durations that differ only in unit or zone also compare by type, in the finest common unit: their token is an object repr that does not fold two units of one instant, and the pandas join, the only route that takes those types, matches them by value (`test_gap6_prefer_bounded_fk.py` T5c caught the false block). A value the token functions refuse ends the key count as `key_type_mismatch`, as the
join refuses it. `keys_comparable`, `key_comparison_type` and the cast path are gone; one
incomparable parent end no longer switches off the check for a comparable one. `exact_key_cast`
stays only for the out-of-core join's output-chunk unification. `postmask/_keys.py` `_baseline` now
reads the status of the source measurement (a mismatch is the `key_type_mismatch` baseline, any
other unchecked status is `error`), and `key_facts` catches only its private
a private return signal, so a read error reaches the check-error path.

`tests/parity/test_post_scan_key_identity.py` runs the real pandas full-frame join and the gate on
the same data for each mixed pair and asserts the counts are equal. Tests that had been changed to
use a list or struct key as the mismatch fixture now use the real pair with the token-rule
expectation, or the one string `unencodable` that the test helper makes the token function refuse
(no real column value does).

The gate never defers the whole check for one key: the verdict
blocks a mismatch the run caused (the input ends compared, or the parent is generated) and reports
one the input already had without blocking. An inherited mismatch (child inputs against a
generated parent) still blocks. The duplicate-parent-key block is pinned by
`test_at24_a_subset_run_with_a_broken_key_still_blocks` and
`test_at16_a_blocking_verdict_commits_nothing_on_every_no_sink_route`.
