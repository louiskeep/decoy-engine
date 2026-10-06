# C8-i public closed `when:` grammar and native chunked `when`: build record

Status: record

Date: 2026-10-06. Plan: `docs/plans/2026-10-06-c8-i-when-native.md` revision 4 (Codex GO after round 4). Branch `feat/c8-i-when-native`, rebased on engine main (`082becb1`, which includes R1, R1b, C6c-i, #211 and #212). Risk R2. Gates pending at the time of writing: dennis, then Codex final. Nothing pushed or merged.

## What shipped, per plan section 3

- **3a, grammar.** `expressions/when_grammar.lark` (contextual LALR) and `expressions/_when_parser.py`: `parse_when(expr) -> WhenExpr` (frozen `Compare`, `InList`, `Not`, `BoolOp`), `when_column_refs(ast)` and the frozen `RESERVED_NAMES` (numexpr's 48 functions, pandas' `DEFAULT_GLOBALS`, `nan`, `NaN`, `NaT`, `index`, `columns`, the Python keywords and soft keywords). Literals: int64 integers, finite floats, `True`, `False`, quoted strings with no backslash, quote or control character (C0, C1, U+2028, U+2029). Dunder-style names are refused. A refusal raises the engine `ValidationError(code="when_outside_closed_grammar")` whose message names the position or the construct, never the expression text. Length (4096), parenthesis depth (50) and recursion are bounded. The module is added to the strict mypy list.
- **3b, public config.** `ColumnConfig.when: str | None` with a `field_validator`: strip, blank becomes `None`, otherwise `parse_when`; a failure is re-raised as `PydanticCustomError("when_outside_closed_grammar", ...)` so it lands in pydantic's `ValidationError` at `tables.<i>.columns.<j>.when`. The plan compiler is unchanged. Raw dicts are not re-validated.
- **3c, the mask.** `native/_when_mask.py`: `WhenSpec`, `when_mask` (the referenced columns of the RAW chunk through the oracle's `to_pandas_fk_safe` with the adapter's protected-column set, then the oracle's own `_eval_predicate(frame, expr, strategy, column=...)`, then `mask.to_numpy(dtype=bool, na_value=False)`), and `WhenMasks.for_chunk`, which runs inside `CarryPlan.diagnose_adapter` with the same table and chunk attribution. The expression is the compiled `ColumnSeed.when`, the exact string the oracle evaluates. There is no second predicate evaluator and no `eval` call in the module.
- **3d, admission.** `native/_when_admission.py`: `when_native_rejection` (one verdict for the route decision, the output pin and the planner), `first_when_rejection`, `admitted_when_columns`, `planner_relaxed_when_columns`. Rule 1 uses `_plan._column_rejection` (the config-only gate that agrees with the compiler by construction) plus the four-strategy set; rule 2 needs the schema's target to be `pa.string()`; rule 3 parses; rule 4 uses the real work-node order (`order_work` sorts by `(table, columns)` and the chunked route has no relationship edges, so it is the column-name order) and each earlier node's own target plus its declared extra writes (`column_access`). `writes_unknown` declines. A passthrough node is not a write. `reads_unknown` on an earlier node also declines, labelled conservative in the docstring. `_dispatch._first_when_column` is deleted; `plan_native_route` calls `first_when_rejection`. Codes: rules 1 and 2 keep `when_predicate_not_native:<col>`, rule 3 is `when_predicate_outside_native_subset:<col>`, rule 4 is `when_predicate_reads_masked_column:<col>:<ref>`.
- **3e, masked execution.** `_operator_step.run_kernel_step_masked(params, source, mask, ...)`: zero selected rows returns the source untouched with `ran=False` and no kernel call; otherwise `run_kernel_step` over every row and `pc.if_else(mask, out, source)`. `_mask_chunk_native` takes `when_masks`; a skipped chunk is added to `kernel_idle` and left uncounted (`counted = when_mask is None and ...`).
- **3f, output types.** `_chunked_schema_rule.when_pinned_columns` (the admission verdict) unioned into `string_columns` by `build_schema_rule`, so the dispatcher entry, the oracle leg and the streamed sink pin identically with or without the companion.
- **3g, planner.** `_whole_column_state_rejections(config, table=, relaxed_when=)`: a `when` column stops emitting `when_predicate_not_chunk_stable` only when `planner_relaxed_when_columns` returns it (admitted under 3d with the target and every referenced column `pa.string()` in the source schema the planner reads; a static classification with no source relaxes nothing). The planner already rejects any config with `relationships`, so no FK ordering case reaches it.
- **3h, evidence.** No new fields. Declines carry the 3d codes.
- **3i, docs.** CHANGELOG entry, `docs/strategies.md` (section "Conditional masking (when:)": field, grammar, reserved names, example, null semantics, route behavior, security boundary), `docs/compatibility-contract.md`, `CODEMAP.md`. The sentry allowlist is `tests/sentry/test_pandas_eval_sites.py` (below). A separate config reference does not exist in this repo; `strategies.md` is it.

The platform repo's roadmap (C8-i shipped, C8-ii and C8-iii planned) is not touched here: it lives in another checkout. The caller needs to do that step.

## Plan citation drift (re-verified against current main)

Line numbers moved; no cited fact was wrong.

- `_dispatch.py:199-215` and `:406-410`: `_first_when_column` was 197-212 and its use 404-408.
- `_planner.py:378-408`: `_whole_column_state_rejections` starts at 381.
- `_chunked_schema_rule.py:59-66`: the `_has_when` guard and `_string_output_is_fixed` are at 74-85.
- `_chunk_masking.py:153`: the idle/uncounted rule is at 178-187 (C5b-ii moved it).
- `_chunked_evidence.py:114`: `executed_backend` is at 112-119.
- `_chunked_oracle.py:343`: the carry diagnosis call is at 354.
- `_pandas_adapter.py:211-218`, `:366`, `:405-413`: the frame build is at 210-225 and the gate call at 408-413.
- Unchanged and correct: `_when_gate.py` (with the #211 keyword-only `column=`), `_unified_slice_admission.py:409-411`, `_unified_slice_evidence.py:178`, `_column_access.py:43-54`, `_chunked_fk.py:616-645`, `errors.py:54`, `_safe_eval.py:153`, `_chunked_carry.py:121-158`.

## Red-before and green-before

Tests were written and committed first (`679a6a09`) and run against an export of that commit (the unchanged source), Python 3.11 with the companion.

- Test 2 and test 4 (`test_c8_i_when_baseline.py`): 45 passed. Green-before by design.
- Test 9 (`test_pandas_eval_sites.py`): 3 failed, 9 passed. The failures are the planted-sentry cases that need the new modules.
- Tests 5, 5a (`test_c8_i_when_parity.py`): 61 failed, 4 passed (the passes are the cases that assert the oracle's own behavior).
- Test 8 (`test_c8_i_when_auto_route.py`): 14 failed, 6 passed (the six keep-full-frame cases pass on the base by design).
- Tests 1, 3, 5b (`test_c8_i_when_grammar.py`, `test_c8_i_when_mask.py`), test 6 (`test_c8_i_when_declines.py`) and test 7 (`test_c8_i_when_config.py`): collection errors, `ModuleNotFoundError` for `expressions._when_parser`, `native._when_mask` and `native._when_admission` (a missing module is the red state for the grammar, config and mask files).
- Whole run on the base: 78 failed, 64 passed, 4 collection errors.

## Existing-test edits (each pinned the blanket `when` veto; the intent is unchanged)

1. `tests/native/test_chunked_entry_evidence.py`, case `when_veto`: predicate `p > 2` becomes `p + 0 > 2`, expected reason `when_predicate_not_native:r` becomes `when_predicate_outside_native_subset:r`. `p > 2` over a redact string target is now admitted, so the case needed a predicate that still vetoes.
2. `tests/native/test_chunked_entry_rev9_read.py`, `test_predicate_read_column_matches_the_public_oracle`: the unconditional `native_admitted is False` and `when_predicate_not_native:s` assertions become per case. Backtick forms: `when_predicate_outside_native_subset:s`. `x > 4` forms: admitted when plain, `crypto_extension_unavailable` when the companion is hidden. Values, the read-passthrough listing and the exactness assertions are unchanged.
3. Same file, `test_string_literal_equal_to_a_column_name_keeps_that_column_carried`: `native_admitted is False` and the reason become `native_admitted is True`. The carried-column and value assertions are unchanged (`s == 'x'` over a redact `s` is admitted).
4. Same file, `test_masked_column_conversion_failure_is_not_wrapped`: predicate `x > 1` becomes `x + 0 > 1` so the table still runs on the oracle leg, which is what the test is about (an admitted `when` would route the table native and the masked-column conversion would not happen).
5. `tests/native/test_unconfigured_passthrough_read.py`, `_reader_cases`: three expected reasons `when_predicate_not_native:s` become `when_predicate_outside_native_subset:s` (the predicates are method calls).
6. `tests/native/test_chunked_entry_values_schema.py`: `test_when_predicate_routes_to_oracle_and_matches_it` becomes `..._matches_the_oracle_and_routes_by_admission`. Value and type assertions are unchanged; the route assertion is per column: redact, truncate and hash over `s` with `tag == 'x'` are admitted (hash reports `crypto_extension_unavailable` without the companion), every other parameter keeps `when_predicate_not_native:<col>`.
7. `tests/sentry/test_physical_seam_disconnection.py`: permits for the new `native/_when_admission.py` and `native/_when_mask.py` (same form as the C5b-ii permit).
8. `tests/sentry/test_module_size.py`: census `native/_chunked_entry.py` 606 to 612.
9. `pyproject.toml`: `decoy_engine.expressions._when_parser` joins the strict mypy list.

## Verification

Interpreters: 3.11 (`decoy-native-venv`, companion present) and 3.10 (`decoy-ci-mirror-venv`, no companion). Tests ran through `pytest-one`. The two interpreters resolve `decoy_engine` from the main checkout's editable install, so the 3.10 runs were started with `PYTHONPATH=<worktree>/src`; pytest's own `pythonpath = ["src"]` covers the 3.11 parent process only.

| Directory | 3.11 | 3.10 |
|---|---|---|
| new C8-i tests (8 files) | 553 passed, 2 skipped | 517 passed, 38 skipped |
| `tests/sentry` + `tests/native` | 8294 passed, 2 skipped | 6856 passed, 1427 skipped |
| `tests/physical` | 1548 passed, 1 skipped | 1084 passed, 465 skipped |
| `tests/unit/execution` | 6384 passed, 4 skipped, 2 failed (below) | 5244 passed, 1146 skipped |
| `tests/unit/config` + `tests/unit/plan` + grammar test | 727 passed, 12 skipped | 727 passed, 12 skipped |
| `tests/parity/native` | 104 passed, 59 xfailed | 40 passed, 64 skipped, 59 xfailed |
| `tests/perf/test_throughput_budgets.py` | 4 passed | 4 passed |

The 3.11 `tests/native` and `tests/sentry` figure is from the run after the test edits and the seam permit. The two `tests/unit/execution` failures on 3.11 (`test_engine_transforms_routes.py` isolated child-process tests) are an environment artifact of running from a worktree: the child process imports the main checkout's `decoy_engine`, which has no `ColumnConfig.when`, so the parent's dumped `when: None` is rejected as an extra input. With `PYTHONPATH=<worktree>/src` the file passes (53 passed). The skips are the companion-only tests on 3.10 and the pre-existing ones.

Lint: `ruff check src tests`, `ruff format --check src tests` and `mypy src` are clean.

Module LOC (before to after): `_planner.py` 595 to 599; `native/_chunked_entry.py` 606 to 612 (census updated); `native/_dispatch.py` 572 to 556; `native/_chunk_masking.py` 289 to 306; `native/_operator_step.py` 377 to 416; `native/_chunked_schema_rule.py` 292 to 314; `config/_tables.py` 332 to 361; new `native/_when_admission.py` 231, `native/_when_mask.py` 101, `expressions/_when_parser.py` 231, `expressions/when_grammar.lark` 47.

## Mutation check (by hand, plan test 10)

Each mutant was applied to the working tree, the C8-i test files ran with `-x`, and the file was restored from a copy. Python 3.11. All killed.

| Mutant | First killer |
|---|---|
| mask built from the wrong chunk (first chunk) | `test_a_validated_config_with_when_runs_end_to_end_on_the_chunked_native_route` |
| mask from a non-protected conversion | `test_a_protected_nullable_integer_reference_excludes_na_like_df_loc` |
| zero-match short-circuit removed | `test_a_zero_match_chunk_makes_no_kernel_call_and_counts_nothing` |
| zero-match result counted as ran (step) | same |
| zero-match counted by the adapter | same |
| scalar node's own target dropped from the write set | `test_a_reference_to_a_column_an_earlier_node_masks_declines_and_equals_today` |
| `if_else` arguments swapped | the end-to-end config test |
| rule 1 strategy set removed | `test_a_non_admitted_strategy_or_config_declines_with_the_legacy_code[passthrough]` |
| rule 1 config gate removed | `...[redact_non_string]` |
| rule 2 (string target) removed | `test_a_non_string_target_declines_with_the_legacy_code` |
| rule 3 (grammar) removed | `test_a_raw_predicate_outside_the_grammar_declines_and_equals_today[p.notnull()]` |
| rule 4 (earlier writes) removed | the earlier-node decline test |
| unknown writes ignored | `test_a_node_with_unknown_writes_declines_any_when_after_it` |
| reserved-name check removed | `test_every_excluded_form_is_refused_with_the_grammar_code[sin == 1]` |
| grammar accepts a no-arg call | `...[f() == 1]` (see below) |
| grammar accepts a one-arg call | `...[len(s) == 1]` |
| planner relaxation without 3d | `test_a_predicate_reading_an_earlier_masked_column_stays_full_frame` |

The first run of the grammar-call mutant (`name()` form) survived: no rejected form in test 1 was a call with no argument. That was a real gap, not an equivalent mutant. Test 1 gained `f() == 1`, `s() > 1` and `len(s, t) == 1`, and both call mutants are now killed. There are no equivalent mutants. Two mutation rows have a weaker first killer than the ideal (the wrong-chunk and `if_else` mutants fall to the end-to-end test; the parity matrix kills them too and was not reached because of `-x`).

## Selectivity timings

`scratchpad` script, 1,000,000 rows, 200,000-row chunks, chunked native route on 3.11 with the companion, one run each (indicative, not a benchmark). `when` is the gated column with `p == 'x'`; `unconditional` is the same table with no `when`.

| Operator | selectivity | `when` | unconditional |
|---|---|---|---|
| redact | 1% | 0.30 s | 0.15 s |
| redact | 50% | 0.29 s | 0.16 s |
| redact | 100% | 0.28 s | 0.16 s |
| hash | 1% | 1.27 s | 1.17 s |
| hash | 50% | 1.28 s | 1.16 s |
| hash | 100% | 1.27 s | 1.19 s |

The cost is flat in selectivity (the kernel runs over every row, as the plan says) and the mask adds about 0.1 to 0.14 s per million rows for a string predicate column. Every chunk of this table has a match at 1%, so the zero-match skip does not show here; it is covered by test 5a. Filter-and-scatter stays a later optimization.

## Consumer audit of `ColumnConfig` (record only, no edits outside the engine)

- `decoy-engine` itself: `plan/_serialize.py:138-139,475` already round-trips `ColumnSeed.when` (test added). Correction (dennis gate): as first built, `model_dump()` stamped `when: None` on every column, which moved `pipeline_config_hash` and the `plan_hash` input for every validated config (outputs and seeds were unchanged). Fixed at the source: a `model_serializer` on `ColumnConfig` omits an unset `when`, so a config without it serializes and hashes byte-identically to main (golden test pins main's digests; a set `when` moves both).
- `/home/cam/vscode/decoy` (CLI): `src/decoy/cli/schema.py:62` prints `PipelineConfig.model_json_schema()`, so the exported schema gains an optional `when` string property (no checked-in schema snapshot found under `decoy/tests`). `src/decoy/cli/init.py` only emits `ColumnConfig`-shaped dicts and does not set `when`. Follow-up: mention `when` in the CLI docs when the field is documented there.
- `/home/cam/vscode/decoy-platform`: `api/pipelines/v2_validation.py:259-285` validates with `PipelineConfig.model_validate`, catches pydantic's `ValidationError` and maps each error to `v2_config_invalid` with `hint=err["type"]` and a dotted path; a grammar refusal arrives as hint `when_outside_closed_grammar` at `tables.<i>.columns.<j>.when` (tested with the same catch pattern). `normalized_config = validated.model_dump()` gains `when: None` per column. `api/evidence/assembly.py` hashes the parsed YAML columns (not the dump), so evidence hashes change only for configs that now write `when`. The web studio types and emitter (`web/src/studio/pipelineTypes.ts`, `pipelineEmitter.ts`, `StudioColumnDetail.tsx`, `pipelineMapper.ts`) have no `when` field: a UI-authored config cannot set it, and a YAML-authored one round-trips only if the emitter preserves unknown column keys. `api/strategies/catalog.py` and `api/capabilities` do not list `when`. Follow-up in the platform: surface the field and the error code in the UI and the capability catalog.

## Judgment calls

- Rules 1 and 2 keep the old `when_predicate_not_native:<col>` code, so no existing test that declines for a strategy or type reason changed. Only predicates outside the grammar got the new code, as the plan specifies.
- `reads_unknown` on an earlier node declines, per the plan's "conservative policy" sentence. In practice it is redundant with rule 3 for the same column; it is labelled in the code.
- An unknown-writes node declines every later `when` column, including one that reads only its own target (the plan says "any `when` after it declines"). The `<ref>` in the code is the first sorted reference.
- The planner relaxation needs the source schema, so a static `classify_job` (no loaded source) relaxes nothing and keeps the rejection. The plan did not say; this is the fail-closed reading.
- A blank-but-nonempty `when` (for example `"  "`) still counts as a `when` column in the planner (`col_entry.get("when")` is truthy), so it keeps the rejection; admission and the compiler treat it as no `when`. This predates the change and is left alone.
- `kernel_calls` counts passthrough columns under `"passthrough"`, so the zero-match tests compare only the operator's key.
- The mask is the oracle's selection, so a predicate that errors on the oracle (for example `p < 1` on a string column) raises the same `StrategyError` on the native leg at the first chunk.
- The grammar parses `x == True` and `x in [True]`; the generated check covers bool columns with `==`, `!=` and `in` only, because ordering a null-bearing object bool column is an oracle error that the compatible-type claim does not cover.

## dennis gate (round 1: NO-GO, 0 BLOCKER / 1 HIGH / 1 MEDIUM / 2 LOW), fixed by the plan author

- HIGH, config hash moved for every validated config (`when: None` in dumps): `ColumnConfig` now omits an unset `when` on serialization (wrap `model_serializer`, works on pydantic 2.0+). Golden test `test_a_config_without_when_hashes_exactly_as_before_the_field_existed` pins main's `pipeline_config_hash` and canonical plan-input digest; `test_a_set_when_moves_both_hashes` shows a real predicate still moves them. This also resolves LOW 2 (older engines re-validating a dump).
- MEDIUM, grammar accepted predicates pandas eval cannot run (64+ string terms, 164+ numeric terms, 200+ stacked `not`): the parser now caps comparisons at 32 and AST nesting at 50, and a test evaluates the largest accepted predicate of each shape under the oracle's `_eval_predicate`.
- LOW 1, `_when_gate.py` error messages carry the predicate text: kept. They are raised to the caller who wrote the config (the log-content rule's separate channel); the engine log sentry (#213) guards log lines. Noted for the Observability program, since a platform may copy error text into its logs.

