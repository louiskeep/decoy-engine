# GEN-TYPES E3 (`lookup`) build record

Status: record

Slice: roadmap row 24q, the `lookup` generate type. Branch `feat/gen-types-e3`.

## Fix round 1

Review findings M1 to M3 and L1.

- Three cells added to `test_a_bad_file_is_refused_with_its_own_code`, one per table check that
  had no test failing without it. Each was run red first by breaking the check in
  `generation/kinds/_lookup.py` and restoring it:
  - `v,w` / `a,-1` / `b,5` with `weight_column: w` (`lookup_weight_invalid`): red with the
    negative-weight guard loosened to `number < -5`.
  - `v,w` / `a,` / `b,1` with `weight_column: w` (`lookup_weight_invalid`): red with a blank
    weight cell read as `'0'`.
  - `v` / `a` with `weight_column: w` (`lookup_column_missing`): red with the weight-column
    presence check dropped.
- `pinned_lookup_unsupported` now has a row in `test_rejected_construct_is_a_coded_error`.
- Imports: `lookup_from_codeset` is exported from `generation.kinds` and `checked_lookup` from
  `plan.checks`, and their callers import from there. The code set loader comes from the public
  `transforms.code_set`, which now re-exports `load_corpus` with an explicit `as` form for mypy.
  That import stays inside the function: a top-level import makes `import decoy_engine.generation.kinds`
  fail with a circular import through `transforms`, `plan` and `execution`.
- The changelog fragment says an older engine does not check `plan_version` at load and fails
  when a run reaches a `lookup` column.
