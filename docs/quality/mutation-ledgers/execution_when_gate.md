# Mutation grading: `execution/_when_gate.py` -- substrate bar 75%

TQ substrate sweep (branch `tq/substrate-sweep`), DRAFT pending re-grade.
`_when_gate.py` is the pre-strategy `when:` predicate gate: two thin wrappers
(`run_with_when_gate` for pandas, `run_with_when_gate_polars` for polars) that
evaluate `ColumnSeed.when` and dispatch the underlying strategy only to the
matching rows, plus two shared helpers. `_eval_predicate` is the numexpr-pinned,
scope-clamped eval that returns the boolean mask (or one of three typed
`StrategyError`s). `_remap_gated_row_errors` rewrites subset-relative
`RowError.row_index` back to full-table positions (blocker B1). Both gate
variants are byte-parity by construction (same eval substrate, same
`mask.any()` short-circuit).

This is a substrate module (predicate gate), not crypto/RI, so the bar is **75%
of LOGIC mutants**, not 100%.

## Numbers

**101 LOGIC survivors addressed: 71 killed by new oracles, 30 EQUIVALENT.** No
survivor left un-triaged. 0 real product bugs found. Independently re-graded with
`scripts/tq_mutate.py`: 183/213 killed = **85.92% LOGIC** (0 unresolved), above the
75% substrate bar. The re-grade reconciled a 3-mutant drift from the authoring
pass: `_eval_predicate` mut_37/47/61 (error-constructor `message=` kwarg removed
entirely) were first counted as kills but survive -- they are the same
message-prose equivalence class as mut_34/40/41 etc. (`.code` + `.strategy` +
exception type unchanged, only `.message` differs), so they are reclassified
EQUIVALENT here.

Per function:

| Function | Survivors | Killed | Equivalent |
|---|---|---|---|
| `_remap_gated_row_errors` | 15 | 15 | 0 |
| `run_with_when_gate` (pandas) | 18 | 18 | 0 |
| `run_with_when_gate_polars` | 37 | 26 | 11 |
| `_eval_predicate` | 31 | 12 | 19 |

## Tests

New oracle file: `tests/unit/execution/test_when_gate_mutation_kills.py`
(15 tests). Two `.strategy` assertions added to the existing error cells in
`tests/unit/execution/test_when_predicate.py`. All green on unmutated code;
ruff format + check clean.

## LOGIC killed (71)

### `_remap_gated_row_errors` (15)

Killed by `test_pandas_gate_remaps_new_error_and_leaves_prior_untouched` (and
its polars twin). The test seeds a pre-existing `RowError` (full-table index 0)
before a gated, error-appending handler runs; the handler records an error at
subset-relative index 1 (mask matches full positions 1,2,3, so it remaps to
full-table index 2). Asserting the prior error is untouched **and** the new one
lands at index 2, with all four `RowError` fields carried, kills:

| Mutants | Mutation | How caught |
|---|---|---|
| `mut_1`, `2` | `range(None, ...)` / `range(..., None)` | `TypeError` in the loop |
| `mut_3` | `range(len(row_errors))` (remaps ALL, incl. prior) | prior error's index changes 0->1 |
| `mut_4` | `range(err_start)` (remaps the wrong slice) | prior changes, new stays subset-relative |
| `mut_5` | `e = None` | `AttributeError` on `e.column` |
| `mut_6` | `row_errors[j] = None` | new entry is `None`, field asserts fail |
| `mut_7`, `8`, `9`, `10` | `column`/`row_index`/`trigger`/`reason` = `None` | exact field asserts |
| `mut_11`, `12`, `13`, `14` | those kwargs removed | `TypeError` (RowError has no defaults) |
| `mut_15` | `row_index=int(None)` | `TypeError` |

`row_index` (mut_8/15) is the load-bearing B1 field; `column`/`trigger` are
machine fields; `reason` (mut_10) is pinned to the literal `"boom"` the fake
handler set (not recomputed from the module), so it is killed rather than left
prose-equivalent.

### `run_with_when_gate` (pandas, 18)

| Mutants | Mutation | Killed by |
|---|---|---|
| `mut_10`, `11`, `16`, `17` | `preflight` lookup nulled / wrong attr name | `test_pandas_preflight_called_before_zero_match_shortcircuit` (preflight never runs -> `calls` empty) |
| `mut_19`, `20` | `preflight(None, ctx)` / `preflight(plan, None)` | same (recorded arg is not the seed / ctx) |
| `mut_21`, `22` | `preflight(ctx)` / `preflight(plan,)` | same (`TypeError`, wrong arity) |
| `mut_5` | no-gate `handler.run(..., None)` | `test_pandas_no_gate_passthrough_threads_ctx` |
| `mut_37` | gated `handler.run(..., None)` | `test_pandas_gated_subset_threads_ctx` |
| `mut_26` | `_eval_predicate(df, when, None)` | `test_pandas_expression_error_attributes_strategy` (`.strategy` is `None` not `"redact"`) |
| `mut_43`, `44`, `45` | `_remap_gated_row_errors` arg -> `None` | remap test (`AttributeError`/`TypeError`) |
| `mut_46`, `47`, `48` | a `_remap` positional arg removed | remap test (arg-shift `TypeError`) |
| `mut_49` | `np.flatnonzero(None)` | remap test (raises) |

The preflight test uses a zero-match `when:`, proving preflight fires
**before** the `not mask.any()` short-circuit (the Codex P2 fail-closed fix).

### `run_with_when_gate_polars` (26 of 37)

| Mutants | Mutation | Killed by |
|---|---|---|
| `mut_2`, `3`, `4`, `5` | no-gate `handler.run` arg -> `None` (frame/column/plan/ctx) | `test_polars_no_gate_passthrough_threads_all_args` (each recorded arg checked by identity/value) |
| `mut_6`, `7`, `8`, `9` | a no-gate `handler.run` positional arg removed | same (`TypeError`, wrong arity) |
| `mut_10`, `11`, `16`, `17`, `19`, `20`, `21`, `22` | `preflight` lookup / call mutations | `test_polars_preflight_called_before_zero_match_shortcircuit` |
| `mut_27` | `_eval_predicate(pdf, when, None)` | `test_polars_expression_error_attributes_strategy` |
| `mut_60`, `61` | gated `handler.run(..., plan=None/ctx=None)` | `test_polars_gated_subset_threads_plan_and_ctx` |
| `mut_67`, `68`, `69` | `_remap` arg -> `None` | `test_polars_gate_remaps_new_error_and_leaves_prior_untouched` |
| `mut_70`, `71`, `72` | a `_remap` positional arg removed | same |
| `mut_73` | `sub_frame.get_column(None)` | same (raises) |

### `_eval_predicate` (12 of 31)

ImportError branch -- `test_missing_numexpr_raises_numexpr_required`
monkeypatches `pd.DataFrame.eval` to raise `ImportError`, then asserts
`code == "numexpr_required"` and `strategy == "redact"`:

| Mutants | Mutation | How caught |
|---|---|---|
| `mut_32` | `code=None` | code assert |
| `mut_33` | `strategy=None` | strategy assert |
| `mut_38`, `39` | `code` -> `"XXnumexpr_requiredXX"` / `"NUMEXPR_REQUIRED"` | code assert (code is a machine field) |
| `mut_35`, `36` | `code`/`strategy` kwarg removed | `TypeError` (StrategyError requires code+strategy) -> `StrategyError` not raised (mut_37 `message` removed is EQUIVALENT -- message has a default) |

Expression-error + not-boolean branches -- strategy attribution + required
kwargs:

| Mutants | Mutation | Killed by |
|---|---|---|
| `mut_43` | expr-error `strategy=None` | `test_pandas_expression_error_attributes_strategy` |
| `mut_57` | not-boolean `strategy=None` | `test_pandas_not_boolean_attributes_strategy` |

Scope clamp (the documented security posture) -- empty `local_dict` /
`global_dict` must keep `@var` names undefined:

| Mutants | Mutation | Killed by |
|---|---|---|
| `mut_12`, `16` | `local_dict=None` / removed | `test_local_dict_clamp_blocks_at_local_scope_walk` |
| `mut_13`, `17` | `global_dict=None` / removed | `test_global_dict_clamp_blocks_at_global_scope_walk` |

The clamp oracle exploits the eval frame: `@strategy` names a **local** of
`_eval_predicate` and `@TYPE_CHECKING` names a **module global** of
`_when_gate`. Under the clamp both raise `UndefinedVariableError` ->
`when_expression_error`. Drop the clamp (dict -> `None`) and pandas resolves
them from the surrounding scope, yielding a scalar -> `when_expression_not_boolean`
instead. Asserting `code == "when_expression_error"` distinguishes the two.
Verified empirically against a frame mirroring `_eval_predicate`'s locals/globals.

## EQUIVALENT (30)

### `run_with_when_gate_polars` (11)

All verified behavior-preserving in polars 1.x (see the empirical check in the
grading session):

| Mutants | Mutation | Why equivalent |
|---|---|---|
| `mut_33`, `39`, `40` | `mask_pl` name `None` / `"XX_when_maskXX"` / `"_WHEN_MASK"` | the mask series is only consumed by `frame.filter(mask_pl)`, which reads values not the name; filter result identical |
| `mut_36` | mask name removed (data becomes first positional) | `pl.Series(np_bool_array)` infers Boolean, same values -> identical filter |
| `mut_35`, `38` | mask `dtype=None` / removed | polars infers `Boolean` from the numpy bool array -> identical |
| `mut_47`, `50` | positions `dtype=None` / removed | polars infers `Int64` from `range(height)` -> identical anchor |
| `mut_42`, `43` | `anchor_col` -> `"XX_decoy_when_row_posXX"` / `"_DECOY_WHEN_ROW_POS"` | the anchor name is used consistently (add -> filter -> read -> drop) and never leaks into the returned frame (`frame.with_columns(masked_col)` builds from the original `frame`); no collision with real columns |

### `_eval_predicate` (19)

| Mutants | Mutation | Why equivalent |
|---|---|---|
| `mut_11`, `15` | `engine="numexpr"` -> `None` / removed | with `local_dict`/`global_dict` both clamped to `{}`, an undefined name raises under both the numexpr and the `None` (auto) engine (verified), and `engine=None` yields byte-identical masks for every valid and tested-invalid expression -- no oracle can distinguish it. The explicit pin is defense-in-depth alongside the dict clamp, which is the actual scope-walk block. **Noted for the gate:** the engine pin is a security belt-and-suspenders; the dict clamp carries the real posture and IS killed (mut_12/13/16/17). Not a product bug. |
| `mut_6` | `simplefilter("always", RuntimeWarning)` -> `simplefilter("always")` | under `catch_warnings(record=True)` both record all warnings; the follow-up loop filters to `RuntimeWarning` subclasses regardless, so the logged set and the returned mask are unchanged |
| `mut_25`, `26`, `30` | fallback log record: `expression`/`_w.message` -> `None`, log format string cased | log-message content only, on the numexpr->python fallback path; no machine field, no mask change |
| `mut_34`, `40`, `41` | `numexpr_required` message `None` / cased | `.message` prose; `.code` + `.strategy` asserted and unchanged |
| `mut_44`, `50`, `51` | `when_expression_error` message `None` / cased | same prose rationale |
| `mut_58`, `66`, `67`, `68` | `when_expression_not_boolean` message `None` / `type(None)` / `""` / `")"` cased | same prose rationale |
| `mut_37`, `47`, `61` | error-constructor `message=` kwarg REMOVED entirely (numexpr_required / when_expression_error / when_expression_not_boolean) | `.message` defaults away but `.code` + `.strategy` + exception type unchanged; same prose-equivalence class as the rows above. Confirmed by re-grade: the `.code`/`.strategy` oracle tests still pass against the mutant. |

## Candidate findings

None. The `engine=None` equivalence (mut_11/15) is called out above as a
defense-in-depth observation, not a defect: the observable scope-walk block is
carried by the empty local/global dicts, which the new clamp oracles kill.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to
`src/decoy_engine/execution/_when_gate.py` and the test selection to
`tests/unit/execution/test_when_gate_mutation_kills.py`,
`tests/unit/execution/test_when_predicate.py`, and
`tests/unit/execution/test_when_gate_polars_writeback.py`, then
`rm -rf mutants && python -m mutmut run`. `source_paths` stays at the package
root.
