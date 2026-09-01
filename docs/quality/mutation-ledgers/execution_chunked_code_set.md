# Mutation grading: `execution/_chunked_code_set.py` -- 0 unresolved logic

Phase 4 slice 4 (`code_set` mask mode on the chunked route,
`docs/plans/2026-09-01-p4-slice4-code-set-chunked.md`). `_chunked_code_set.py`
holds the slice's gate logic as free functions (mutmut skips decorated-class
bodies): `code_set_conditional_failures` (mask-mode + no-chapter_preserve config
gate), `code_set_source_columns` / `unsafe_code_set_source_columns` /
`reject_unsafe_code_set_chunk_schema` (the schema-layer source-dtype gate trio,
mirroring slice 3's text_mask trio), `reject_code_set_when` (the `when:` gate),
`reject_code_set_fk_keys` (the both-orientation FK-key gate), and
`resolve_pinned_code_set_records` (the corpus-record pinning resolver).

Graded via `scripts/native-testing/python_mutation_pilot.py`
(mutmut generation, then a standalone-pytest-per-mutant readjudication that
promotes mutmut's in-process false-timeouts to their true verdict, avoiding the
timeout pathology on this pandas/pyarrow-heavy suite), selection
`tests/unit/execution/test_code_set_chunked.py`.

## Numbers

**48 survivors = 47 accepted error-message prose** (casing / `XX`-wrap of free
text whose coded fields are all independently pinned) **+ 1 provably-equivalent**
(the unreachable `isinstance`-guard `continue`->`break` in
`resolve_pinned_code_set_records`; adjudicated below). 0 unresolved logic. The
corrected tally is the contract (mutmut raw timeouts are readjudicated
standalone); regenerate with the command below for the exact LOGIC score.

Two rounds of pinning got here. The first pinned the reject gates' machine
fields (path, offending column name, `"?"` placeholder, the reachable fk
control-flow guards), killing every coded-field and reachable-logic mutant in
those gates. The Codex final gate then caught a mis-adjudication: the two
`build_work_list(plan, registry)` -> `build_work_list(plan, None)` survivors
(in `code_set_source_columns` and `resolve_pinned_code_set_records`) are NOT
equivalent. A chunked table may carry `code_set` beside a provider-backed
`faker` column (both are in `CHUNK_CONDITIONAL_STRATEGIES`), and `build_work_list`
consults the registry (`provider_is_composite`) for the faker node, so `None`
raises on a reachable mixed plan. Both are now killed by
`test_registry_is_load_bearing_with_a_provider_backed_sibling`. The bar (0
unresolved-logic survivors, message-prose + the one equivalent accepted) is met.

## What is PINNED (so it is not a survivor)

Every machine-consumed field is asserted by a test, so a mutation to it reddens
rather than survives:

- **Coded error (`code=`)** on all five raising gates -- every rejection test
  asserts `exc.value.code == "..."`.
- **`PlanCompileError.path`** on `reject_code_set_when`, `reject_code_set_fk_keys`,
  and `reject_unsafe_code_set_chunk_schema` -- pinned by
  `TestRejectionFieldPinning` and the schema/fk direct-unit tests (kills the
  `path=None` mutants).
- **Offending column name + `"?"` placeholder + `", "` join separator** on
  `reject_code_set_when` -- `test_reject_code_set_when_pins_path_columns_and_
  placeholder` uses a probe column whose name shares no word with the message
  prose (the slice-3 self-collision fix) and a name-less column entry, asserting
  the exact `"?, diagnosiscol"` render (kills the `col_entry.get("name", "?")`
  extraction mutants and the `message=None` mutant).
- **Offending column name** on `reject_code_set_fk_keys` -- `"id" in message`.
- **Reachable control-flow guards** on `reject_code_set_fk_keys`: the parent-side
  `parent.table == table` guard (`test_..._parent_guard_is_and_not_or` kills the
  `and`->`or` over-rejection) and the `continue`-not-`break` scan past malformed
  relationship / child entries (`test_..._scans_past_malformed_entries`).
- **Source-dtype gate** (first-chunk + per-chunk + planner auto-route),
  **corpus pinning** (one resolution across chunks; version mismatch fails
  closed pre-stream; swap-after-pin continues), **FK both-orientation
  rejection**, **empty-chunk string normalization**, and **byte-parity** (string
  and `large_string` sources, multi-namespace, chunk-boundary splits) -- pinned
  by their own tests.

## Accepted survivors (non-contract)

- **Error-message prose.** Mutants that `XX`-wrap or uppercase a free-text
  fragment of a rejection message, where the fragment carries no
  machine-consumed value (the code, path, column names, and separators are all
  pinned above). Same policy as `execution_chunked_text_mask.md` /
  `execution_chunked_dgrn.md`: killable only by brittle full-message equality,
  which the sweep does not pursue for pure prose.
- **`resolve_pinned_code_set_records` `continue` -> `break`** on the
  `isinstance(plan_slice, ColumnSeed)` guard: EQUIVALENT. That guard is reached
  only after `node.strategy != "code_set"` is filtered out, and a `code_set`
  scalar node always carries a `ColumnSeed` plan slice, so the guard never fails
  and `break` vs `continue` is an unreachable difference. (The strategy-filter
  `continue` above it, which IS reachable, is killed by
  `test_resolve_pinned_code_set_records_scopes_to_table_and_scans_past_a_skip`.)

## Candidate findings

None. No mutation exposed a wrong admission verdict, a wrong coded error/path,
a corpus-pinning gap, or a chunked/full-frame divergence current behavior does
not already intend.

## Regenerate

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/_chunked_code_set.py \
  --tests tests/unit/execution/test_code_set_chunked.py \
  --timeout 30
```

`source_paths` stays at the package root (mutmut 3.x quirk; see
`_chunked_dgrn.py`'s ledger). The pilot temporarily scopes `[tool.mutmut]` in
`pyproject.toml` and restores it on exit.
