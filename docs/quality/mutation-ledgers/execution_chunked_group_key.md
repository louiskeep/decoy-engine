# Mutation grading: `execution/_chunked_group_key.py` -- 0 unresolved logic

Phase 4 chunked-route slice 2 (`group_key` sibling-keyed admission +
group_by-dtype gate). `_chunked_group_key.py` holds the slice's free-function
gate logic: `group_by_effective_type` / `_static_group_by_source_type` (the
work-order-aware effective-type resolver), `unsafe_group_key_group_by_columns`
(the auto-route collector), `reject_unsafe_group_key_group_by_dtype` (the manual
dtype gate), and `reject_group_key_when` (the `when:` gate).

This ledger was added on 2026-09-01 alongside the cross-slice registry fix (see
below); the slice itself shipped earlier without a dedicated ledger.

Graded via `scripts/native-testing/python_mutation_pilot.py` (mutmut generation
+ standalone-pytest-per-mutant readjudication that promotes mutmut's in-process
false-timeouts to their true verdict), selection
`tests/unit/execution/test_group_key_chunked.py`. Regenerate with the command at
the bottom for the exact corrected tally (the corrected count is the contract).

## Numbers

**197/246 = 80.08% LOGIC, 0 unresolved logic. 49 survivors = 37 accepted
error-message prose (casing / `XX`-wrap of free text whose coded fields are all
independently pinned) + 12 provably-equivalent** (10 type-invariant
`_static_group_by_source_type` mutants + 2 `group_by_effective_type` defensive-
guard mutants; adjudicated below). Pinning the reject gates' coded fields (path,
offending column name, `"?"` placeholder) and killing the registry mutant drove
the count from a first pass of 191/246 (77.64%, 55 survivors) to 197/246. The
corrected tally is the contract (mutmut raw timeouts are readjudicated standalone).

## The cross-slice registry fix (2026-09-01)

A prior adjudication (referenced from `execution_chunked_text_mask.md`) treated
the `build_work_list(plan, registry)` -> `build_work_list(plan, None)` mutant in
`reject_unsafe_group_key_group_by_dtype` as equivalent. It is NOT: a chunked
table can carry `group_key` beside a provider-backed `faker` column (both are
chunk-admitted), and `build_work_list` consults the registry
(`provider_is_composite`) for the faker node, so `None` raises on a reachable
mixed plan. Killed by `test_registry_is_load_bearing_with_a_provider_backed_sibling`.

## What is PINNED (so it is not a survivor)

- **Coded error (`code=`)** on both raising gates -- every rejection test asserts it.
- **`PlanCompileError.path`** on `reject_unsafe_group_key_group_by_dtype` and
  `reject_group_key_when` (kills the `path=None` mutants).
- **Offending column name + `"?"` placeholder + `", "` separator** on
  `reject_group_key_when` -- `test_reject_group_key_when_pins_path_columns_and_
  placeholder` uses a probe name sharing no word with the message prose plus a
  name-less column entry, asserting the exact `"?, diagnosiscol"` render.
- **The work-order-aware effective type** (`group_by_effective_type` /
  `_static_group_by_source_type`) -- the `TestEffectiveTypeUnitBranches` /
  scheduling tests pin the per-strategy static output type and the
  before/after-mask ordering.
- **The registry argument** (via the faker-sibling test above).

## Accepted survivors (non-contract)

- **Error-message prose.** `XX`-wrap / uppercase of a free-text message fragment
  whose coded fields are pinned above. Same policy as
  `execution_chunked_text_mask.md` / `execution_chunked_code_set.md`.
- **Type-invariant `_static_group_by_source_type` mutants.** The function returns
  an Arrow *type*, so mutations that change only a string literal's content
  (`"REDACTED"` -> `"redacted"` / `"XXREDACTEDXX"`), the `from_pandas=` flag
  (`True` -> `False`/`None`/absent) on a `pa.array(...).type` expression, or a
  default token that does not participate in the returned type all yield the
  IDENTICAL type -- no test can distinguish them because the observable output
  (the type) is unchanged. EQUIVALENT.
- **`group_by_effective_type` defensive guard** (`or` -> `and` on
  `not isinstance(group_by, str) or not group_by`, and the kwarg-default tweak):
  `group_by` is a validated non-empty string on every compiled plan, so the
  guard never fires and the mutation is unreachable-difference. EQUIVALENT.

## Candidate findings

None. No mutation exposed a wrong admission verdict, a wrong coded error/path, or
a chunked/full-frame divergence current behavior does not already intend.

## Regenerate

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/_chunked_group_key.py \
  --tests tests/unit/execution/test_group_key_chunked.py \
  --timeout 30
```

`source_paths` stays at the package root (mutmut 3.x quirk; see
`_chunked_dgrn.py`'s ledger). The pilot temporarily scopes `[tool.mutmut]` in
`pyproject.toml` and restores it on exit.
