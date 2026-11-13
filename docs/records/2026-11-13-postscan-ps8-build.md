# POSTSCAN-STREAM PS-8 build record: the post-scan gate wired into run_pipeline

Status: record

Plan: `docs/plans/2026-11-12-postscan-stream.md` on `plan/postscan-stream` (4.2, 4.3, 4.5, 4.7, AT-14, AT-16, AT-20, AT-24, AT-25).
Branch `feat/postscan-ps8`, cut from `origin/main` 8c9c1162.

## Plan amendments (tech-lead rulings, 2026-11-13)

These go into the plan branch as written here.

- 4.2, generated parent. The baseline of an edge whose parent is generated is "inherited": the count of input child key values that match no output parent key. The gate then measures introduced orphans as output orphans minus that count, both against the output parent keys. "A generate table has baseline 0" still holds for a generated child. A source orphan under `preserve` or `warn` therefore does not block, and a mask that breaks a matched child key still does.
- 4.3, declared casting. Until PS-10, a job with declared columns and declared keys stays on the resident route (`route.post_scan_keys_resident`), because the bounded routes cast per chunk and the gate has no cast re-read yet.
- PS-10 note: lift that clause when the cast attachment lands (AT-29), and give the two B-6 publish cells in `test_b6a_publish.py` their sink back once a staged sink is readable.
- Route types: routing takes `ReasonCode | None` (the type route reasons already have), not `ReasonRef`.
- 4.2, quarantine (2026-11-13, tech-lead ruling on review B4). Quarantine and row-error removal run before the gate, so a removed parent row would read as an orphan the run introduced. The gate records the parent keys quarantine removed (keys only, in memory, never reported or logged) and counts orphans against the output parents plus those keys. A child that points at a removed parent is not introduced; it is reported as the warning `post_scan.foreign_key_orphans_quarantined`. Any other new orphan still blocks.
- 4.2, mixed parents (review H1). The inherited baseline applies to an edge only when every parent end of it is absent from the inputs. With one generated and one masked parent the edge is measured against its inputs, so a mask that breaks every child key blocks. An inherited type mismatch (child inputs against a generated parent) is reported as baseline `inherited_type_mismatch`, not `no_source`, and blocks as before.
- 4.5, key scan resources (review B2, M3). The key scan's DuckDB cap is min(512MB, the run budget share), never below 128MB. It binds the run's `out_of_core_threads` and spills to the gate's scratch directory. AT-15 keeps "at most 512MB".
- 4.2, null-typed keys (review B3). A null-typed (all-null or zero-row) key column is comparable with every family: it holds no value that can match or orphan.

## What was built

- `config/_post_scan.py`: sink kind, output readability, model predicate and the resident reason; `execution/_transforms_gate.py::post_scan_resident_for` is the one helper every routing site calls.
- `execution/pipeline/_post_scan.py`: `bind_gate`, `active_gate`, `settle`; lazy inputs are re-read in batches and checked against a footer snapshot taken at bind time.
- `execute_route` binds the gate and settles it after the route; `observed_commit` asks the gate first; `compute_post_scan_integrity` is deleted.
- Sentries: `post-scan-commit`, `post-scan-model-predicate`, `post-scan-readability`.

## Test edits forced by the plan

- `test_job_estimate.py`: the router spy reads `post_scan_resident` (renamed kwarg); the assertion is unchanged.
- `test_fk_findings.py`: the test of the deleted in-run helper calls `run_post_mask(keys=None)`; assertions unchanged.
- `test_b6a_publish.py`: the sequential and out_of_core cells run without a sink, since a sink plus declared keys is resident until PS-10; a new cell pins the refusal.

## e1c test expectations changed on purpose (tech-lead ruling 2026-11-13)

The gate's DuckDB connection is a run connection: `execute_route` binds one recorder for the route and the gate, and refreshes `resources.out_of_core.applied` after the gate ran. The gate's cap is `min(512MB, run budget share)`, at least 128MB, so its thread count follows its own limit. In `test_e1c_duckdb_threads.py`:

- `test_every_nested_connection_sees_the_cap`: was "every connection has threads == 2"; now every connection equals the formula from its own memory limit and the cap, and the route connections still reach 2. The gate's 512MB connection gives 1 thread by that formula.
- `test_no_cap_keeps_no_user_cap_but_honors_detected_cpus`: same change, with 3 CPUs and no cap.
- `test_a_cap_above_the_detected_cpus_is_clamped_to_them`: was `{threads} == {4}`; now every connection equals the formula with the cap 64 clamped to the 4 CPUs, and the largest is 4, so the cap is still clamped to the CPUs.
- `test_applied_pairs_are_the_distinct_connection_settings`: unchanged. It now holds because `applied` includes the gate's connection.
- `test_a_small_memory_limit_takes_threads_below_the_cap`: unchanged.
