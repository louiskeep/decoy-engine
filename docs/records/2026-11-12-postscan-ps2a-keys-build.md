# POSTSCAN-STREAM PS-2a build record: declared key checks

Status: record

Plan: `docs/plans/2026-11-12-postscan-stream.md` on `plan/postscan-stream` (sections 4.2, 5, 7).
Branch `feat/postscan-ps2a`, stacked on `feat/reg-release-e2` with `origin/main` merged in.

## What was built

- `verification/postmask/_keys.py`: `check_keys` returns one `KeyResult` per declared key (each
  distinct parent end as a primary key, each distinct child end as a foreign key). Each key is
  measured on the output and, when the output is checked, on the inputs. A key blocks only on
  what the run introduced.
- A child end shared by several relationships is one check against the union of its parents'
  keys. Every extra parent is passed to `key_facts` as a child, so the profiling core compares
  its types with the first parent's. Its counts are dropped.
- Five `post_scan` reason codes in `reasons/post_scan/_part2.py`, all `since=UNRELEASED` and
  `CONFIGURATION_REPAIR`.

## Deviation from the plan

- AT-12 was written as an open choice and is built differently from the plan's exception-chain
  test. An uncoded failure in a key check gives an `error` result with `post_scan.check_error`
  and the safe class name, and raises nothing. A coded engine error (`lazy_source_changed`, the
  file-access errors, `PipelineValidationError`, any registered family) propagates unchanged,
  decided by `decoy_engine.profiling.passes_through`, the same rule pre-scan applies. The raise
  with no cause and no context belongs to the gate slice (PS-7). A new exception class here would
  need an entry in `reason_adapter._CODED` or the sentry's `UNCLASSIFIED` list, both outside this
  slice. The test that stands in for the chain check: no message text reaches the report.

## Decisions the plan left open

- A table or key column absent from the inputs has a baseline of zero (a generated table). Only a
  missing output column is a code.
- `parent` in a foreign key's result is the tuple of parent table names.
- A report code is the mapping `{"namespace", "code"}`, the engine's wire form for a reason.

## Red and mutations

The first test commit ran against a stub (13 failed). The review fixes were also red first: the
new cells failed against the old code (3 failed, the rest were characterization or mutation
cells), and the predicate was a stub that returned false. Each mutation below was applied alone
in a detached throwaway worktree of the final head and turned the listed number of tests red in
`test_keys.py` and `test_scan_set.py`. The tree was restored after each.

| AT | Mutation | Red tests |
|---|---|---|
| AT-4 | compare output duplicates with 0 | 2 |
| AT-5, AT-7 | read `k0` only | 3 |
| AT-6 | drop the baseline | 5 |
| AT-8 | count null-key rows as orphans | 1 |
| AT-9 | merge key ends that share a table | 2 |
| AT-10 | stop after the first relationship | 4 |
| AT-11 | return no code for a missing column | 1 |
| AT-12 | re-raise instead of an error result | 3 |
| AT-14 | store `str(exc)` in the result | 3 |
| AT-34 | union the first parent only | 1 |
| review 1 | drop the `passes_through` re-raise in `_keys.py` | 1 |
| review 1 | `passes_through` ignores registered families | 2 |
| review 1 | `passes_through` always false | 3 |
| review 2 | raw class name in `error_class` | 1 |
| review 3 | hand-built `namespace.code` string | 1 |
| review 4a | drop extra parents from the type check | 1 |
| review 4b | absent source table inherits the output as baseline | 1 |
| review 4c | source type mismatch inherits the output as baseline | 1 |
| review 4f | drop a foreign key whose end is also a primary key | 1 |
| review 4g | one foreign result per parent | 1 |

No mutation exists for the empty-table cell (4e) or the AT-5 composite output assertion (4d):
4e is a characterization cell, and 4d is covered by the AT-5 mutation above.
