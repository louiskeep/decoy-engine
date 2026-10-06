Status: plan (revision 1, author = Opus). Codex plan gate: pending.
Rules consulted: 00-universal, development-loop, security, testing, architecture, api-and-compatibility, code-review, scope-discipline

# C8-i: a public, closed `when:` language, evaluated natively for value-keyed operators

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C, C8 ("`when` predicates without a whole-table pandas fallback, and expressible through `PipelineConfig`"). Audit row R017 (`docs/records/2026-09-30-rust-coverage-evidence-audit.md:40`).

Owner decision (Cam, 2026-10-06): a public `when:` accepts ONLY a closed subset, validated at config time, with no method calls, reductions or eval surface. The raw-dict `run_pipeline` path keeps today's pandas-eval `when` until a later cleanup.

Staging:
- **C8-i (this plan):** the public field and its closed grammar, plus native evaluation for hash (string source), redact, truncate and deterministic categorical, on both native routes.
- **C8-ii:** text_redact, date_shift and bucket_perturb (explicit format, error-position remap), and deterministic Faker.
- **C8-iii:** docs and contract close-out, the R017 audit update, and moving the raw-dict path onto the closed grammar (the "later cleanup", which needs its own owner decision).

Branch `feat/c8-i-when-native` off engine main `5095bb3d`. This slice touches `native/_chunk_masking.py`, `native/_operator_step.py` and `native/_chunked_schema_rule.py`, which C5b-ii (building now) also changes. C8-i is built AFTER C5b-ii merges, on a rebase. Risk R2: public config surface plus two routes, parity-gated, no new kernel.

## 1. Goal and scope

Today any `when:` column sends its whole table to pandas on every native route:
- **Chunked native:** `when_predicate_not_native:<col>`, `native/_dispatch.py:199-215`, applied at `:406-410`.
- **Unified:** `_has_when_gate`, `_unified_slice_admission.py:409-411`. The decline carries no reason code.
- **Auto-chunk planner:** `when_predicate_not_chunk_stable`, `_planner.py:378-408`. The table stays full-frame, and then the unified decline sends it to pandas.

Also, `when` cannot be expressed through the validated config at all: `ColumnConfig` (`config/_tables.py:45-115`) is `extra="forbid"` with no `when` field.

C8-i:
1. Adds `ColumnConfig.when: str | None`, validated at config time against a closed `when` grammar.
2. Evaluates closed-grammar predicates natively on Arrow and masks only the selected rows, for hash (string source), redact, truncate and deterministic categorical on the unified and chunked native routes.
3. Lets the auto-chunk planner admit closed-grammar predicates, since they are row-local and give the same result per chunk and whole-frame.

Out of scope:
- All operators beyond the four above (C8-ii).
- Position-keyed operators (non-det categorical, non-det Faker, windowed_date), group_key, text_mask and code_set. These keep their existing `when:` rejections permanently unless a later slice defines match-ordinal semantics.
- Out-of-core, which keeps `out_of_core_when_predicate_unsupported`.
- Vault, coherent_with and top_code `when` combinations, which keep their compile rejections.
- Changing how the pandas oracle evaluates `when`: it keeps `pdf.eval`, both for raw dicts and for validated configs.

## 2. Established facts (main `5095bb3d`; C8 research 2026-10-06)

**Oracle** (`execution/_when_gate.py:161-217`, called from `_pandas_adapter.py:405-413`):
- The predicate is evaluated with `pdf.eval(expr, engine="numexpr", local_dict={}, global_dict={})` on the LIVE table frame (`_pandas_adapter.py:366`). Columns masked by earlier work nodes are therefore seen post-mask.
- `preflight` runs unconditionally. Zero matches return `df` unchanged with no warnings.
- Otherwise the handler runs on `df.loc[mask].copy()`, and the result is written back with `df.loc[mask, column] = sub_df[column]`. Non-matching rows keep their original values.
- Row errors are remapped to full-table positions.
- Error codes: `numexpr_required`, `when_expression_error`, `when_expression_not_boolean` (nullable BooleanDtype is accepted).
- A blank predicate is normalized to None (`plan/_seed_envelope.py:250-251`).
- Compile rejections: `when_with_coherent_with_unsupported`, `top_code_with_when_unsupported`, `date_shift_group_by_with_when_unsupported`, FK self-mask edges with `when` (`_chunked_fk.py:616-645`).

**Expression infrastructure:** `expressions/grammar.lark` is a closed Lark grammar for derived and case_when. It allows arithmetic and functions, and its strings use double quotes only (`ESCAPED_STRING`). It is not usable as-is for `when`: pandas eval accepts single quotes, and arithmetic and functions are outside the decided subset.

**Chunked schema rule:** a `when` column loses its fixed `string` output pin (`native/_chunked_schema_rule.py:59-66`).

**Value-keyed operators** (output per row depends only on that row's value and config): hash, redact, truncate, deterministic categorical. For these, "run the kernel on every row, then keep the masked value only where the predicate is true" equals "run the handler on the selected subset and write back", row by row.

## 3. Decisions

**3a. The closed `when` grammar.** New `expressions/when_grammar.lark` plus `expressions/_when_parser.py` exposing `parse_when(expr) -> WhenExpr`, a frozen AST, and `when_column_refs(ast)`. Allowed:
- column refs: identifiers, no dots or dunders, the same lexical rule as `grammar.lark`;
- literals: integers, floats, single- or double-quoted strings, `True`, `False`;
- comparisons `== != < <= > >=` between a column ref and a literal, either order;
- membership `<ref> in [<literal>, ...]` and `<ref> not in [...]`;
- `and`, `or`, `not`, and parentheses.

Everything else is a parse error, raised as `ValidationError(code="when_outside_closed_grammar")`. That includes arithmetic, function or method calls, attribute access, subscripts, `@` scope references, ref-to-ref comparisons, chained comparisons and `None` literals.

Null checks are NOT in C8-i. pandas eval has no method-free spelling for them, and `== None` behaves differently under numexpr than under Python. Adding them needs the C8-iii oracle cleanup.

Every accepted string is also a valid pandas-eval expression with the same meaning on non-null values. Test 3 pins this with a generated corpus.

**3b. Public config.** `ColumnConfig.when: str | None = None`, with a field validator:
- strip the value; blank becomes None, matching the seed envelope;
- otherwise run `parse_when`, failing config validation with `when_outside_closed_grammar`.

The plan compiler is unchanged: it still stores the raw string on `ColumnSeed.when`. The raw-dict path is not re-validated, per the owner decision.

**3c. Native evaluator** (`native/_when_eval.py`). `when_mask(ast, table: pa.Table) -> pa.BooleanArray`, with no nulls in the result. It reproduces the oracle's pandas-eval result on the frame types the oracle actually sees. The builder first characterizes those, as baseline test 2: object columns for strings, float64 or int64 for numerics, object or bool for booleans. The rules:
- A comparison involving a null cell is False, except `!=`, which is True (pandas/numpy semantics for object and float NaN).
- `in` and `not in` give False and True on null.
- `and`, `or` and `not` operate on these null-free booleans.
- A string literal compared with a non-string column, a numeric literal compared with a non-numeric column, or a bool literal compared with a non-bool column declines natively (3d). It is never coerced.

Implemented with `pyarrow.compute`, then `fill_null` per comparison as above.

**3d. Native admission** (both routes). One predicate, `when_native_rejection(col_entry, table_schema, configured) -> str | None`, wired into the chunked dispatcher (replacing `_first_when_column`'s blanket veto for qualifying columns) and into unified admission (replacing `_has_when_gate`'s blanket decline). A `when` column is admitted natively only if ALL of these hold:
- the strategy is hash, redact, truncate or deterministic categorical, and its own config passes today's native gate;
- the source Arrow type is `pa.string()`;
- the predicate parses under 3a (a raw-dict predicate outside the grammar declines with `when_predicate_outside_native_subset:<col>`);
- every referenced column is either the target column itself or a column that NO earlier work node in the table writes. The oracle sees post-mask values in work-node order, so the builder reads the existing node order rather than assuming it. Otherwise it declines with `when_predicate_reads_masked_column:<col>:<ref>`.
- every literal's type matches its referenced column's Arrow type per 3c (otherwise `when_predicate_literal_type_mismatch:<col>:<ref>`).

Any other `when` column keeps today's decline. On the chunked route that is `when_predicate_not_native:<col>`. On unified it is a decline without a code, as today.

**3e. Masked execution.** In the shared step layer (R1b), add `run_kernel_step_masked(params, source, mask, ...)`:
- **Zero matches:** return `source` unchanged, with `ran=None`, and without calling the kernel. The column keeps the source values and type, as the oracle does.
- **Otherwise:** call `run_kernel_step` on the full source, then `pc.if_else(mask, out, source)`. For the four value-keyed operators this equals the oracle row by row. Unmatched nulls stay null.
- Both adapters call this when the column has an admitted `when`. The unified mask is computed per batch from the batch. The chunked mask is computed per chunk from the chunk. Both are valid because the grammar is row-local.

**3f. Output types.**
- A `when`-gated admitted column keeps the source type `string` on both routes. Unified assembly for it is `type_preserving` instead of the operator's usual shape, because non-matching rows keep source values, as the oracle's write-back does.
- On the chunked route, a `when`-gated admitted column is pinned to `string` on both legs by a config-plus-first-schema classifier inside `build_schema_rule` (the `date_shift_pinned_columns` pattern: requires a string source).
- The oracle's per-chunk result for an all-null chunk (Arrow `null` after `from_pandas`) is the documented degenerate exception, as for the existing pins.
- The builder characterizes the oracle's whole-frame types for zero-row, all-null, zero-match and all-match columns as baseline test 4. Native must match them, or the slice records an explicit exception that the plan gate has accepted.

**3g. Planner.** `_whole_column_state_rejections` (`_planner.py:378-408`) stops emitting `when_predicate_not_chunk_stable` for columns whose predicate parses under 3a. Those predicates are row-local, so per-chunk evaluation equals whole-frame. Others keep the reason.

**3h. Evidence.** No new evidence fields in C8-i. An admitted `when` column reports its operator's usual backend. Declines carry the 3d codes on the chunked route and in the `native/_plan.py` eligibility report (unified declines stay codeless, as today).

**3i. Docs.**
- CHANGELOG.
- `docs/strategies.md` and the configuration reference: the `when` field, the closed grammar with examples, and what is rejected.
- Compatibility contract: native `when` for the four operators, plus the null semantics.
- Security note: the closed grammar replaces eval for validated configs. The raw-dict path still evaluates with pandas.
- Roadmap: C8-i shipped; C8-ii and C8-iii planned.

## 4. Design notes

- **Parse, don't evaluate.** The public surface is a closed grammar checked at config time. That is the security boundary, and it also makes native evaluation possible: the AST, not a string handed to an evaluator, drives `pyarrow.compute`.
- **Kernel on all rows, then select.** This is simpler than filter-and-scatter and is exact for value-keyed operators. Its cost is extra kernel work at low selectivity. The builder records timings at 1%, 50% and 100% selectivity. If 1% is materially slower than the oracle, filter-and-scatter is a later optimization, not a correctness change.
- **Order dependence.** The oracle evaluates on post-mask values. Declining predicates that read a masked column keeps parity without modelling node order inside the evaluator.
- **Grammar shared with the oracle.** The oracle keeps pandas eval, so the grammar must be a subset whose meaning pandas eval and the native evaluator agree on. Test 3 enforces this with generated expressions and data.

## 5. Acceptance tests (written first; red-before recorded)

1. **Grammar.**
   - Accepts: each allowed form, both quote styles, nesting and parentheses.
   - Rejects with `when_outside_closed_grammar`:
     - arithmetic;
     - `a.mean()` and `name.str.startswith('A')`;
     - `@x`;
     - subscripts and dunders;
     - ref-to-ref comparisons;
     - chained comparisons;
     - `None`;
     - `import`.
   - Hypothesis fuzz: random token strings never parse into anything outside the AST node set.
2. **Baseline (green-before): oracle frame types.** For a string, an int64, a float64 and a bool source column, record the pandas dtype the oracle's `pdf.eval` sees at `_pandas_adapter.py:366`.
3. **Mask parity (Hypothesis).** For generated grammar-valid predicates over generated tables (with nulls, empty strings and Unicode), `when_mask` equals the oracle's `_eval_predicate` result cast to bool. Covers each operator, `in`/`not in`, `not`, and nulls in every position. `derandomize=True`.
4. **Baseline (green-before): whole-frame output types.** For each of the four operators, record the oracle's output types for zero-row, all-null, zero-match, all-match and partial-match `when` columns.
5. **Route parity.**
   - For each of the four operators, both routes:
     - predicates referencing the target, an unconfigured column and a passthrough column;
     - partial, zero and all matches;
     - nulls;
     - chunk shapes zero-row, single-row, ragged and all-null.
   - Values and order equal the oracle, and types follow 3f.
   - Every case also asserts native-route evidence, so an oracle fallback cannot pass vacuously.
6. **Declines.** Each 3d rule gives its exact code on the chunked route, and a unified decline, with output equal to today's:
   - non-admitted strategy;
   - non-string source;
   - raw predicate outside the grammar;
   - ref to a masked column;
   - literal type mismatch.

   The position-keyed and group_key/text_mask/code_set/bucket_perturb/windowed_date `when` rejections are unchanged.
7. **Public config.** `ColumnConfig(when="status == 'a'")` validates. Each rejected example from test 1 fails validation with `when_outside_closed_grammar`. A blank becomes None. A validated config with `when` runs end to end through `run_pipeline` on the native route.
8. **Auto-router.** `run_pipeline` with a low `auto_chunk_threshold_rows`:
   - A closed-grammar `when` table auto-chunks and runs native.
   - A raw-dict predicate outside the grammar stays full-frame with `when_predicate_not_chunk_stable`.
   - No hard errors.
9. **Security sentry.** New AST check: `.eval(` and `.query(` method calls appear only in `execution/_when_gate.py` and `execution/_transforms.py`. `native/_when_eval.py` contains no `eval`, `exec` or `compile` calls.
10. **Mutation (by hand).**
    - Required mutants:
      - the null rule for `!=` flipped;
      - `fill_null` dropped;
      - zero-match short-circuit removed;
      - `if_else` arguments swapped;
      - each 3d rule removed;
      - grammar accepting a method call;
      - planner relaxation applied to non-grammar predicates.
    - All must be killed. Record the results.

Red-before: tests 1, 3, 5, 6, 7, 8 and 9 fail on the base (no grammar, no field, blanket declines). Tests 2 and 4 are green-before by design.

Every new test also runs under the Python 3.10 mirror.

## 6. Risk, rollback, gates

| Risk | Mitigation |
|---|---|
| Native mask differs from pandas eval on nulls or types | Characterize first (tests 2, 4); Hypothesis mask parity (test 3); declining type mismatches |
| Public field widens the attack surface | Closed grammar at config time; sentry (test 9); no eval in the native path |
| Order dependence on masked columns | Decline rule 3d with code; test 6 |
| Degenerate types differ between routes | 3f pin plus baseline test 4; explicit exceptions recorded |
| Conflicts with C5b-ii | Build after C5b-ii merges, on a rebase |
| Low-selectivity cost | Timings recorded; optimization deferred |

Rollback: revert the merge commit. `when` columns go back to sending their table to pandas, and the public field disappears. Pre-GA, so no compatibility promise is broken.

Gates: Codex plan gate, Sonnet tests-first build (after C5b-ii merges), dennis, Codex final gate, ci-mirror, merge under the standing authority, the post-merge suite including `tests/perf`, and a main CI check.

## 7. Plan-gate history

- Rev 1: initial.
