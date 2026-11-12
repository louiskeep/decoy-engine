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

The tests were committed first against a stub (13 failed). Each mutation below was applied alone
to a copy of `_keys.py` and turned the named tests red; the file was restored after each.

| AT | Mutation | Red tests |
|---|---|---|
| AT-4 | compare output duplicates with 0 | 2 |
| AT-5, AT-7 | read `k0` only | 3 |
| AT-6 | drop the baseline | 5 |
| AT-8 | count null-key rows as orphans | 1 |
| AT-9 | reuse one read per key end | 10 |
| AT-10 | stop after the first relationship | 2 |
| AT-11 | return no code for a missing column | 1 |
| AT-12 | re-raise inside the `except` | 1 |
| AT-14 | store `str(exc)` in the result | 1 |
| AT-34 | union the first parent only | 1 |
