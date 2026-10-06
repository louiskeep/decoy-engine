Status: plan (revision 2, author = Opus). Codex plan gate: round 1 REVISE (3 HIGH, 2 MEDIUM, 1 LOW) folded by narrowing scope; round 2 pending.
Rules consulted: 00-universal, development-loop, security, testing, architecture, api-and-compatibility, code-review, scope-discipline

# C8-i: a public, closed `when:` language, plus native `when` on the chunked route for string predicates

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C, C8 ("`when` predicates without a whole-table pandas fallback, and expressible through `PipelineConfig`"). Audit row R017 (`docs/records/2026-09-30-rust-coverage-evidence-audit.md:40`).

Owner decision (Cam, 2026-10-06): a public `when:` accepts ONLY a closed subset, validated at config time, with no method calls, reductions or eval surface. The raw-dict `run_pipeline` path keeps today's pandas-eval `when` until a later cleanup.

Staging (rev 2, narrowed after Codex round 1):
- **C8-i (this plan):**
  - the public field and its closed grammar;
  - native `when` on the CHUNKED route only, for hash, redact, truncate and deterministic categorical over string sources, when the predicate compares STRING columns to STRING literals.
- **C8-ii:** the unified full-frame route. This needs its own reconstruction design, because `_unified_slice_evidence.py:178` rebuilds every masked column from a Python list, which does not reproduce the oracle's subset `.loc` write-back.
- **C8-iii:** numeric and boolean predicates (nullable dtypes, int64 above 2**53 widened to float64), more operators (text_redact, date_shift, bucket_perturb, deterministic Faker), docs close-out, and moving the raw-dict path onto the closed grammar (its own owner decision).

Branch `feat/c8-i-when-native` off engine main `5095bb3d`. This slice touches `native/_chunk_masking.py`, `native/_operator_step.py` and `native/_chunked_schema_rule.py`, which C5b-ii (building now) also changes. C8-i is built AFTER C5b-ii merges, on a rebase. Risk R2.

## 1. Goal and scope

Today any `when:` column sends its whole table to pandas:
- Chunked native: `when_predicate_not_native:<col>`, `native/_dispatch.py:199-215`, applied at `:406-410`.
- Auto-chunk planner: `when_predicate_not_chunk_stable`, `_planner.py:378-408`. The table stays full-frame, and then unified declines it.
- Unified route: `_has_when_gate`, `_unified_slice_admission.py:409-411`.

`when` also cannot be expressed through the validated config: `ColumnConfig` (`config/_tables.py:45-115`) is `extra="forbid"` with no `when` field.

C8-i:
1. Adds `ColumnConfig.when: str | None`, validated at config time against a closed `when` grammar (3a, 3b).
2. Evaluates admitted predicates natively on Arrow per chunk, and masks only the selected rows. This applies to the four value-keyed operators over string sources on the chunked native route (3c to 3f).
3. Lets the auto-chunk planner chunk a table whose `when` columns are ALL natively admitted under 3d (3g). Every other `when` column keeps today's planner rejection, including the operator configs that would decline natively (Codex round 1 HIGH 2).

Out of scope:
- The unified route (C8-ii). It keeps declining `when`.
- Numeric and boolean predicates natively (C8-iii). They are accepted by the public grammar and run on pandas.
- Other operators: text_redact, date_shift, bucket_perturb and deterministic Faker go to C8-iii. Position-keyed operators, group_key, text_mask and code_set keep their `when` rejections.
- Out-of-core, which keeps `out_of_core_when_predicate_unsupported`.
- Changing how the pandas oracle evaluates `when`.

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

**3a. The closed `when` grammar.** New `expressions/when_grammar.lark` and `expressions/_when_parser.py` exposing:
- `parse_when(expr) -> WhenExpr`, a frozen AST;
- `when_column_refs(ast)`.

Allowed:
- column refs: identifiers matching `[A-Za-z_][A-Za-z0-9_]*`, no dunders, and NOT in the reserved set (Codex round 1 MEDIUM 4). The reserved set is numexpr's function names (`numexpr.expressions.functions`, 48 names including `sin`, `log`, `where`), plus pandas-eval and Python keywords (`and`, `or`, `not`, `in`, `is`, `True`, `False`, `None`, `if`, `else`, `lambda`). It is a frozen list in the module, checked against numexpr's live set by a test;
- literals:
  - integers in the int64 range;
  - finite floats;
  - strings in single or double quotes, whose content has no backslash, no quote character of either kind, and no control characters (so no escape processing is needed and pandas and the parser read the same characters);
  - `True` and `False`;
- comparisons `== != < <= > >=` between ONE column ref and ONE literal, in either order;
- `<ref> in [<literal>, ...]` and `<ref> not in [...]` with a non-empty list;
- `and`, `or`, `not`, and parentheses.

Everything else fails with `ValidationError(code="when_outside_closed_grammar")`. That includes arithmetic, calls, attributes, subscripts, `@`, ref-to-ref comparisons, chained comparisons, `None`, empty lists and reserved identifiers. Null checks are not in the grammar (no method-free pandas spelling).

Every accepted string must evaluate under the oracle's `pdf.eval(engine="numexpr", local_dict={}, global_dict={})` without error, for a frame holding the referenced columns. Test 1b enforces this with generated identifiers and literal spellings.

**3b. Public config.** `ColumnConfig.when: str | None = None`, with a field validator: strip the value; blank becomes None; otherwise `parse_when`. The plan compiler is unchanged and stores the raw string. Raw dicts are not re-validated (owner decision).

Audit required by Codex round 1 LOW: list and test every consumer of `ColumnConfig`:
- model serialization, JSON-schema export (if any), and manifest serialization (`plan/_serialize.py:138-139,475`, round-trip test);
- CLI and platform config schemas that mirror it. Grep `/home/cam/vscode/decoy` and `/home/cam/vscode/decoy-platform` and record which consumers need a follow-up (no edits outside the engine in this slice).

**3c. Native evaluator** (`native/_when_eval.py`). `when_mask(ast, chunk: pa.Table) -> pa.BooleanArray`, null-free. Admitted predicates (3d) reference only `pa.string()` columns, compare them only to string literals, and only for columns the oracle holds as plain object strings. For those, the oracle semantics are pinned by baseline test 2:
- `==`, `<`, `<=`, `>`, `>=` against a null cell are False;
- `!=` against a null cell is True;
- `in` is False on null, and `not in` is True on null;
- `and`, `or` and `not` combine these null-free booleans.

Built with `pyarrow.compute` plus `fill_null` per comparison. String ordering (`<`) must match Python and numpy object comparison, which is code-point order. pyarrow's string comparison is also code-point/binary order on UTF-8; test 3 pins this, including non-ASCII.

**3d. Native admission (chunked route only).** One predicate, `when_native_rejection(col_entry, chunk_schema, oracle_dtypes, written_before) -> str | None`. It replaces `_first_when_column`'s blanket veto for qualifying columns. A `when` column runs natively only if ALL of these hold:
1. The strategy is hash, redact, truncate or deterministic categorical, and its own config passes today's native gate.
2. The target source type is `pa.string()`.
3. The predicate parses under 3a. A raw-dict predicate outside the grammar gives `when_predicate_outside_native_subset:<col>`.
4. Every literal is a string (otherwise `when_predicate_non_string_literal:<col>`, routed to the chunked oracle).
5. Every referenced column is `pa.string()`, AND the oracle's conversion (`to_pandas_fk_safe` with the same protected-column set the adapter uses, `_pandas_adapter.py:211-218`) holds it as plain `object` dtype, not `StringDtype` or another extension dtype (otherwise `when_predicate_ref_not_object_string:<col>:<ref>`). The builder derives the oracle dtype by running that same conversion on the first chunk, not by assuming it (Codex round 1 HIGH 1).
6. Every referenced column is the target itself or a column that no work node earlier in the oracle's order writes (otherwise `when_predicate_reads_masked_column:<col>:<ref>`). The order is the adapter's actual work-node order (the lexicographic topological order) with each node's declared write set (`handler_written_columns`, composites included), not config-list order.

A declined column takes the existing downgrade to the chunked-oracle leg with its code, never a hard error.

**3e. Masked execution.** In the shared step layer, `run_kernel_step_masked(params, source, mask, ...)`:
- **Zero matches:** return `source` unchanged, `ran=None`, with no kernel call.
- **Otherwise:** `run_kernel_step` on the full chunk, then `pc.if_else(mask, out, source)`. For the four value-keyed operators over string sources, this equals the oracle's subset run plus write-back row by row, because both the output and the source are strings. Unmatched nulls stay null.

The chunked adapter calls it per chunk with the chunk's mask. The grammar is row-local, so per-chunk evaluation equals whole-frame.

**3f. Output types (chunked).** An admitted `when` column is pinned to `string` on both chunked legs and the streamed sink. A classifier inside `build_schema_rule` (`date_shift_pinned_columns` pattern) applies the same 3d config rules and the first chunk's source type, so the oracle leg pins identically. Its oracle output is always strings or nulls, so the pin only fixes degenerate chunks, where the oracle gives Arrow `null` after `from_pandas`. The whole-frame route keeps its own inference for an entirely null or empty column (the documented exception for pinned columns). Baseline test 4 characterizes it, and the docs record it.

**3g. Planner.** `_whole_column_state_rejections` (`_planner.py:378-408`) stops emitting `when_predicate_not_chunk_stable` for a column only when its config passes 3d rules 1, 3 and 4 (config-only), its target and referenced columns are string-typed in the source schema the planner sees, and the 3f pin applies. Codex's counterexample (integer column, redact `0.5`, `when x == 1`) keeps the rejection. Multi-table split uses the same classification; test 8 covers it.

**3h. Evidence.** No new fields. An admitted `when` column reports its operator's usual backend. Declines carry the 3d codes in chunked evidence.

**3i. Docs.**
- CHANGELOG.
- `docs/strategies.md` and the config reference: the field, the grammar, the reserved names, examples.
- Compatibility contract: chunked-native `when` for the four operators over string predicates, the null semantics, and the degenerate-type exception.
- Security note (corrected per Codex round 1 LOW): validation restricts public expressions to the closed grammar; the pandas oracle still evaluates the string with `pdf.eval`, and raw dicts are not validated.
- Roadmap: C8-i shipped, C8-ii and C8-iii planned.

## 4. Design notes

- **Parse, don't evaluate.** The public boundary is a closed grammar. The native evaluator walks the AST and never hands a string to an evaluator.
- **Prove parity where it is simple.** String columns held as object dtype have two-valued comparison semantics with a fixed null rule. Numeric widening, nullable NA and the unified reconstruction path each need their own design, so they are separate slices rather than special cases here.
- **Kernel on all rows, then select.** This is exact for value-keyed operators over strings. Timings at 1%, 50% and 100% selectivity are recorded. Filter-and-scatter is a later optimization.

## 5. Acceptance tests (written first; red-before recorded)

1. **Grammar.** Accepts every allowed form and both quote styles. Rejects every excluded form with `when_outside_closed_grammar`, including `sin == 1`, any reserved identifier, backslashes and quotes inside strings, `None`, and empty lists.
   - 1b. Hypothesis: generated grammar-valid predicates, with generated identifiers and literal spellings, all evaluate under the oracle's `pdf.eval` without error.
   - 1c. The reserved list equals numexpr's live function set, plus the keywords.
2. **Baseline (green-before): oracle representation and semantics.** For string columns with nulls through `to_pandas_fk_safe` (plain, with pandas metadata, and protected by FK, group_key or top_code), record the pandas dtype, and record the oracle mask for `==`, `!=`, `<`, `in`, `not in` and `not` on null cells.
3. **Mask parity (Hypothesis).** On admitted shapes, `when_mask` equals the oracle `_eval_predicate` result. Covers nulls, empty strings, Unicode and ordering of non-ASCII strings. `derandomize=True`.
4. **Baseline (green-before): oracle output types.** For each operator with `when`, on whole-frame and both chunked legs: zero-row, all-null, zero-match, all-match and partial-match.
5. **Chunked parity.**
   - For each of the four operators: predicates referencing the target, an unconfigured string column and a passthrough string column; partial, zero and all matches; nulls; chunk shapes zero-row, single-row, ragged and all-null.
   - Native chunked output equals oracle chunked output and the whole-frame oracle, with values, order and types per 3f.
   - Every case asserts native-route evidence.
6. **Declines.**
   - Each 3d rule gives its exact code, takes the chunked-oracle leg, and produces output equal to today's.
   - Covered rules: non-admitted strategy, non-string target, raw predicate outside the grammar, a numeric literal, a reference to a `StringDtype` column, and a reference to a column an earlier node writes (including a composite's extra written column).
   - The unified route still declines every `when` column.
7. **Public config.** Valid strings pass, every test-1 rejection fails, blank becomes None, the manifest round-trips, and a validated config with `when` runs end to end through `run_pipeline` on the chunked native route.
8. **Auto-router and split.**
   - A table whose `when` columns are all admitted auto-chunks and runs native.
   - Codex's integer/redact-0.5 counterexample stays full-frame with `when_predicate_not_chunk_stable` and succeeds.
   - A raw-dict non-grammar predicate stays full-frame.
   - A two-table split, with one admitted `when` table and one non-admitted, routes each table correctly and equals forced whole-frame.
9. **Security sentry.** `.eval(` and `.query(` method calls appear only in `execution/_when_gate.py` and `execution/_transforms.py`. `native/_when_eval.py` and `expressions/_when_parser.py` contain no `eval`, `exec` or `compile` calls.
10. **Mutation (by hand).** Required mutants:
    - the `!=` null rule flipped;
    - `fill_null` dropped;
    - the zero-match short-circuit removed;
    - `if_else` arguments swapped;
    - each 3d rule removed;
    - the reserved-name check removed;
    - the grammar accepting a call;
    - the planner relaxation applied without 3d.

    All must be killed. Record the results.

Red-before: tests 1, 3, 5, 6, 7, 8 and 9 fail on the base. Tests 2 and 4 are green-before by design.

Every new test also runs under the Python 3.10 mirror.

## 6. Risk, rollback, gates

| Risk | Mitigation |
|---|---|
| Native mask differs from pandas eval | Strings-only native scope; the oracle representation is derived, not assumed (3d.5); baseline test 2; Hypothesis test 3 |
| Chunk-variant output via planner relaxation | Relax only for admitted configs (3g); counterexample test 8 |
| Unified reconstruction mismatch | Unified out of scope (C8-ii) |
| Public field widens surface | Closed grammar; reserved names; sentry; consumer audit (3b) |
| Order dependence | Real work-node order and write sets (3d.6); test 6 |
| Conflicts with C5b-ii | Build after C5b-ii merges |

Rollback: revert the merge commit.

Gates: Codex plan gate, Sonnet tests-first build after C5b-ii merges, dennis, Codex final gate, ci-mirror, merge under the standing authority, the post-merge suite including `tests/perf`, and a main CI check.

## 7. Plan-gate history

- Rev 1: initial.
- Codex round 1, REVISE (3 HIGH, 2 MEDIUM, 1 LOW). Rev 2 narrows scope instead of adding machinery:
  - H1 (nullable and numeric semantics): native only for string refs held as object dtype, with string literals; the oracle representation is derived; numeric and nullable predicates move to C8-iii.
  - H2 (planner): relaxed only for natively admitted configs, with the counterexample tested.
  - H3 (unified reconstruction): the unified route moves to C8-ii.
  - M4: reserved identifiers and literal spelling rules, with generated pandas-eval checks.
  - M5: moot, since unified is out of scope; unconfigured references are tested on chunked.
  - L6: security wording corrected; a consumer and serialization audit is added.
