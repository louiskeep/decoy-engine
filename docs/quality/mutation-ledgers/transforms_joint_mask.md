# Mutation grading: `transforms/joint_mask.py` -- LOGIC-100%

TQ step-4 pass, graded 2026-07-26. `transforms/joint_mask.py` (317 LOC) masks a
correlated column set jointly: `validate_joint_mask_config` fail-closes on bad
config (distinct `PlanCompileError` code/path per failure kind),
`_pick_rows_mask` selects a replacement row per source row via a secret-derived
HMAC key (DE-02) with a seeded per-row fallback for null keys, `_pick_rows_gen`
samples rows for generate mode, and `apply_joint_mask` routes mask/gen and writes
the correlated columns as a unit.

**Grade scope: FOCUSED selection** (`test_joint_mask.py` + `test_joint_mask_sp08b.py`).

## Numbers

**196 mutants: 118 killed (60% baseline), 78 survived -> 168 killed after this
pass, 28 EQUIVALENT.** LOGIC-mutant score 100%. 0 timeouts.

- **50 LOGIC survivors killed** with 16 new tests. 0 product bugs.
- **28 EQUIVALENT survivors** (25 validation message-prose, 1 unsupported-mode
  ValueError message, 2 `integers(0, n)` -> `integers(n)` numpy no-ops).

## LOGIC (50): killed by new tests

`validate_joint_mask_config` -- machine fields (`code`, `path`) asserted per branch
(the **reference-missing branch had no prior test at all** -- 9 pure coverage gaps):
- `test_each_failure_kind_has_exact_code_and_path` kills the columns / key_by /
  reference-missing / not-found branch code+path+removed-arg mutants (incl.
  removed-positional-arg -> TypeError caught by `pytest.raises(PlanCompileError)`).
- `test_column_not_in_reference_code_and_path`, `test_unreadable_reference_table_reports_invalid`
  (ValueError -> `reference_invalid`), `test_id_column_is_not_a_maskable_target`
  (finding #9: the module-level `{"id"}` set -> `{"XXidXX"}`/`{"ID"}` must still
  reject masking `id`).

`apply_joint_mask`: `test_default_mode_is_mask` (default `mode="mask"` -> XX/upper
sends the implicit call to the ValueError path); `test_namespace_feeds_the_hmac_key`
(dropping the namespace kwarg collapses distinct namespaces to one mapping).

`_pick_rows_mask`: `test_selection_uses_secret_derived_hmac_key` (dropping
`hmac_key=` falls back to the public salt; output pinned over 6 keys);
`test_missing_key_column_falls_back_per_row` (`pd.Series(None)` len 0 / `[None]/len`
TypeError); `test_float_key_values_are_treated_as_present` (`np.isnan(None)`);
`test_null_key_fallback_is_seed_deterministic` (`default_rng(None)` non-det);
`test_null_key_fallback_seed_is_first_eight_bytes` (`job_seed[:9]`);
`test_null_keys_produce_varied_fallback_rows` (`or`->`and` collapses nulls);
`test_null_key_fallback_can_select_first_row` (`integers(1, n)` excludes 0).

`_pick_rows_gen`: `test_gen_seed_is_first_eight_bytes` (`job_seed[:9]`);
`test_gen_sampling_can_select_first_row` (`integers(1, n)` excludes 0).

## EQUIVALENT (28)

| Mutants | Category | Why equivalent |
|---|---|---|
| validate `10/18/19/20/21/22` (columns), `28/36-43` (key_by), `51/59-65` (ref-missing), `70` (not-found), `99` (col-not-in-ref) | error `message=` prose | the raised `PlanCompileError`'s `code` and `path` (the machine fields) are unchanged and asserted; only the human-readable message text differs. |
| `apply_joint_mask_26` | `raise ValueError(None)` (unsupported mode) | still raises `ValueError` with the same type/contract; only the message text changes. |
| `_pick_rows_mask_24`, `_pick_rows_gen_16` | `rng.integers(0, row_count)` -> `rng.integers(row_count)` | numpy treats the single arg as `high` with `low=0`, the identical `[0, row_count)` range; byte-identical RNG output (verified). |

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `transforms/joint_mask.py`, selection to
`test_joint_mask.py` + `test_joint_mask_sp08b.py`, then `rm -rf mutants && python -m mutmut run`.
