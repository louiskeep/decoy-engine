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

## What was built

- `config/_post_scan.py`: sink kind, output readability, model predicate and the resident reason; `execution/_transforms_gate.py::post_scan_resident_for` is the one helper every routing site calls.
- `execution/pipeline/_post_scan.py`: `bind_gate`, `active_gate`, `settle`; lazy inputs are re-read in batches and checked against a footer snapshot taken at bind time.
- `execute_route` binds the gate and settles it after the route; `observed_commit` asks the gate first; `compute_post_scan_integrity` is deleted.
- Sentries: `post-scan-commit`, `post-scan-model-predicate`, `post-scan-readability`.

## Test edits forced by the plan

- `test_job_estimate.py`: the router spy reads `post_scan_resident` (renamed kwarg); the assertion is unchanged.
- `test_fk_findings.py`: the test of the deleted in-run helper calls `run_post_mask(keys=None)`; assertions unchanged.
- `test_b6a_publish.py`: the sequential and out_of_core cells run without a sink, since a sink plus declared keys is resident until PS-10; a new cell pins the refusal.
