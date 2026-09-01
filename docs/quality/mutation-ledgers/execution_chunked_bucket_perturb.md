# Mutation grading: `execution/_chunked_bucket_perturb.py` -- 81.15% logic, 0 unresolved

Phase 4 chunked-route slice 5 (`bucket_perturb` explicit-`date_format` admission,
`docs/plans/2026-09-01-p4-slice5-bucket-perturb-chunked.md`).
`_chunked_bucket_perturb.py` holds the slice's gate logic as free functions
(mutmut skips decorated-class bodies): `bucket_perturb_conditional_failures` (the
explicit-`date_format` config gate), `bucket_perturb_source_columns` /
`unsafe_bucket_perturb_source_columns` / `reject_unsafe_bucket_perturb_chunk_schema`
(the schema-layer source-dtype gate trio, mirroring the code_set trio),
`reject_bucket_perturb_when` (the `when:` gate), and `reject_bucket_perturb_fk_keys`
(the both-orientation FK-key gate).

Graded via `scripts/native-testing/python_mutation_pilot.py` (mutmut generation +
standalone-pytest-per-mutant readjudication that promotes mutmut's in-process
false-timeouts to their true verdict), selection
`tests/unit/execution/test_bucket_perturb_chunked.py`.

## Numbers

**198/244 = 81.15% LOGIC, 0 unresolved logic. 46 survivors, ALL accepted
error-message prose** (casing / `XX`-wrap of free-text message fragments whose
coded fields are all independently pinned). No provable-equivalent survivors and
no coded-field survivors: the builder pinned `code`, `PlanCompileError.path`, the
offending column name, and the `"?"` placeholder on every reject gate, and the
registry mutant is killed (below). The corrected tally is the contract (mutmut raw
timeouts are readjudicated standalone).

| Function | Survivors |
|---|---|
| `reject_bucket_perturb_fk_keys` | 19 |
| `reject_unsafe_bucket_perturb_chunk_schema` | 12 |
| `reject_bucket_perturb_when` | 8 |
| `bucket_perturb_conditional_failures` | 7 |

Every one is an `XX`-wrap or uppercase of a rejection message's free text; the
machine-consumed fields on each gate are pinned by direct unit tests, so the
prose survivors carry no unverified behavior. Same policy as
`execution_chunked_code_set.md` / `execution_chunked_text_mask.md`.

## The registry mutant is killed (cross-slice lesson applied up front)

`bucket_perturb_source_columns` calls `build_work_list(plan, registry)`. A chunked
table can carry `bucket_perturb` beside a provider-backed `faker` column, and
`build_work_list` consults the registry (`provider_is_composite`) for the faker
node, so `build_work_list(plan, None)` is reachable-logic, not equivalent. The
slice ships `test_registry_is_load_bearing_with_a_provider_backed_sibling` from
the start, so the `registry -> None` mutant reddens; there is no
`source_columns` survivor. (This is the mis-adjudication corrected across
code_set / text_mask / group_key; it is pinned here on the first pass.)

## What is PINNED

`code`, `PlanCompileError.path`, offending column name, `"?"` placeholder, and the
`", "` join separator on all three reject gates; the explicit-`date_format`
config gate; the source-dtype gate (first + every chunk + planner auto-route);
FK-key rejection both orientations; the registry argument (above); and
byte-parity (string + large_string sources, all three buckets, chunk-boundary
splits, nulls, unparseable passthrough, real secret, empty/all-null via
null-promotion).

## Candidate findings

None. No mutation exposed a wrong admission verdict, a wrong coded error/path, or
a chunked/full-frame divergence current behavior does not already intend.

## Regenerate

```
python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/_chunked_bucket_perturb.py \
  --tests tests/unit/execution/test_bucket_perturb_chunked.py \
  --timeout 30
```

`source_paths` stays at the package root (mutmut 3.x quirk; see `_chunked_dgrn.py`).
The pilot temporarily scopes `[tool.mutmut]` in `pyproject.toml` and restores it
on exit; if a run is interrupted (e.g. a concurrent process), restore by hand with
`git checkout -- pyproject.toml` and readjudicate the existing `mutants/` run by
pointing `[tool.mutmut]` at this module before invoking `scripts/tq_mutate.py`.
