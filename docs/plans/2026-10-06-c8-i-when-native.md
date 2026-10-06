Status: plan (revision 3, author = Opus). Codex plan gate: rounds 1 and 2 REVISE folded; round 3 (final before escalation) pending. Rev 3 replaces the separate Arrow predicate evaluator with the oracle's own mask function (see §7).
Rules consulted: 00-universal, development-loop, security, testing, architecture, api-and-compatibility, code-review, scope-discipline

# C8-i: a public, closed `when:` language, plus native `when` on the chunked route

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C, C8 ("`when` predicates without a whole-table pandas fallback, and expressible through `PipelineConfig`"). Audit row R017 (`docs/records/2026-09-30-rust-coverage-evidence-audit.md:40`).

Owner decision (Cam, 2026-10-06): a public `when:` accepts ONLY a closed subset, validated at config time, with no method calls, reductions or eval surface. The raw-dict `run_pipeline` path keeps today's pandas-eval `when` until a later cleanup.

Staging (rev 2, narrowed after Codex round 1):
- **C8-i (this plan):**
  - the public field and its closed grammar;
  - native `when` on the CHUNKED route only, for hash, redact, truncate and deterministic categorical over string sources. The row mask comes from the oracle's own predicate function, so any grammar-valid predicate qualifies.
- **C8-ii:** the unified full-frame route. This needs its own reconstruction design, because `_unified_slice_evidence.py:178` rebuilds every masked column from a Python list, which does not reproduce the oracle's subset `.loc` write-back.
- **C8-iii:** more operators (text_redact, date_shift, bucket_perturb, deterministic Faker), auto-chunk relaxation for numeric predicate columns, docs close-out, and moving the raw-dict path onto the closed grammar (its own owner decision).

Branch `feat/c8-i-when-native` off engine main `5095bb3d`. This slice touches `native/_chunk_masking.py`, `native/_operator_step.py` and `native/_chunked_schema_rule.py`, which C5b-ii (building now) also changes. C8-i is built AFTER C5b-ii merges, on a rebase. Risk R2.

## 1. Goal and scope

Today any `when:` column sends its whole table to pandas:
- Chunked native: `when_predicate_not_native:<col>`, `native/_dispatch.py:199-215`, applied at `:406-410`.
- Auto-chunk planner: `when_predicate_not_chunk_stable`, `_planner.py:378-408`. The table stays full-frame, and then unified declines it.
- Unified route: `_has_when_gate`, `_unified_slice_admission.py:409-411`.

`when` also cannot be expressed through the validated config: `ColumnConfig` (`config/_tables.py:45-115`) is `extra="forbid"` with no `when` field.

C8-i:
1. Adds `ColumnConfig.when: str | None`, validated at config time against a closed `when` grammar (3a, 3b).
2. Runs admitted `when` columns natively on the chunked route: per chunk, the row mask comes from the oracle's own `_eval_predicate` applied to the oracle's own conversion of the referenced columns, the native kernel runs, and only selected rows take the masked value (3c to 3f). This applies to the four value-keyed operators over string sources.
3. Lets the auto-chunk planner chunk a table whose `when` columns are ALL natively admitted under 3d (3g). Every other `when` column keeps today's planner rejection, including the operator configs that would decline natively (Codex round 1 HIGH 2).

Out of scope:
- The unified route (C8-ii). It keeps declining `when`.
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
- column refs: identifiers matching `[A-Za-z_][A-Za-z0-9_]*`, no dunders, and NOT in the reserved set (Codex rounds 1 and 2). The reserved set is:
  - numexpr's function names (`numexpr.expressions.functions`, 48 names including `sin`, `log`, `where`);
  - pandas eval's default resolver names, which shadow a column even with empty local and global dicts (`inf`, `Inf`, `nan`, `NaN`, `True`, `False` and whatever else the audit finds in pandas' `DEFAULT_GLOBALS` and the resolvers);
  - Python and pandas-eval keywords.

  It is a frozen list in the module. Tests check it against numexpr's live set and pandas' default globals. A reserved name gives a clear config error instead of the oracle's `when_expression_not_boolean` at run time;
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

**3c. The mask is the oracle's mask** (`native/_when_mask.py`). There is no second predicate evaluator. For each chunk, `when_mask(expr, chunk, protected) -> pa.BooleanArray`:
- converts ONLY the predicate's referenced columns of that raw chunk (before any null-type casts) with the oracle's own `to_pandas_fk_safe`, using the same protected-column set the adapter would use for this table (`_pandas_adapter.py:211-218`);
- calls the oracle's own `execution/_when_gate._eval_predicate` (same engine, same empty scopes, same error codes) on that frame;
- converts the boolean Series to Arrow with the oracle's selection rule. The builder characterizes how `df.loc[mask]` treats a nullable `NA` in baseline test 2 and reproduces exactly that (excluded, or the oracle's error).

This removes rev 2's parity gaps (Codex rounds 1 and 2): nullable vs object dtypes, int64 widening, representation drift between chunks, and pandas-global collisions such as `inf`. Each chunk's mask is computed exactly as the chunked oracle leg computes it for that chunk, so native chunked == oracle chunked by construction, for any column type. An oracle error (for example `when_expression_not_boolean` from a name collision) is raised identically.

Cost: one pandas conversion of the referenced columns plus one numexpr evaluation per chunk. Both are vectorized and small next to a full-chunk conversion. Timings are recorded in the build.

**3d. Native admission (chunked route only).** One predicate, `when_native_rejection(col_entry, chunk_schema, work_order) -> str | None`. It replaces `_first_when_column`'s blanket veto for qualifying columns. A `when` column runs natively only if ALL of these hold:
1. The strategy is hash, redact, truncate or deterministic categorical, and its own config passes today's native gate.
2. The target source type is `pa.string()`.
3. The predicate parses under 3a. A raw-dict predicate outside the grammar gives `when_predicate_outside_native_subset:<col>`.
4. Every referenced column is the target itself or a column that NO work node earlier in the oracle's order writes. Otherwise it declines with `when_predicate_reads_masked_column:<col>:<ref>`.
   - The order is the adapter's actual work-node order (the lexicographic topological order), not config-list order.
   - A node's writes are its OWN affected columns (its target column(s)) PLUS its declared extra writes (`handler_written_columns` / access `writes`), per Codex round 2 HIGH 2. The helper alone omits a scalar node's own target.
   - A node whose writes are unknown (`reads_unknown` or an unclassifiable handler) is treated as writing every column, so any `when` after it declines.
   - A genuine passthrough or unconfigured reference stays admissible.

Referenced columns may be of any type: 3c computes the mask exactly as the oracle does. A declined column takes the existing downgrade to the chunked-oracle leg with its code, never a hard error.

**3e. Masked execution.** In the shared step layer, `run_kernel_step_masked(params, source, mask, ...)`:
- **Zero matches:** return `source` unchanged with no kernel call, and report it as SKIPPED (Codex round 2 MEDIUM 4). The chunked adapter must not count a kernel call or credit the compiled backend for a skipped chunk: it adds the column to `kernel_idle` and leaves `counted=False`, like an idle zero-row positional chunk (`_chunk_masking.py:153`, `_chunked_evidence.py:114`). `StepResult.ran=False` carries this.
- **Otherwise:** `run_kernel_step` on the full chunk, then `pc.if_else(mask, out, source)`. For the four value-keyed operators over string sources, this equals the oracle's subset run plus write-back row by row, because both the output and the source are strings. Unmatched nulls stay null.

The chunked adapter calls it per chunk with the chunk's mask. The grammar is row-local, so per-chunk evaluation equals whole-frame.

**3f. Output types (chunked).** An admitted `when` column is pinned to `string` on both chunked legs and the streamed sink. A classifier inside `build_schema_rule` (`date_shift_pinned_columns` pattern) applies the same 3d config rules and the first chunk's source type, so the oracle leg pins identically. Its oracle output is always strings or nulls, so the pin only fixes degenerate chunks, where the oracle gives Arrow `null` after `from_pandas`. The whole-frame route keeps its own inference for an entirely null or empty column (the documented exception for pinned columns). Baseline test 4 characterizes it, and the docs record it.

**3g. Planner.** `_whole_column_state_rejections` (`_planner.py:378-408`) stops emitting `when_predicate_not_chunk_stable` for a column only when its config passes 3d rules 1, 3 and 4, its target and EVERY referenced column are `pa.string()` in the source schema the planner sees, and the 3f pin applies. String references are required here (not in 3d) because auto-chunking must also equal WHOLE-FRAME, not only the chunked oracle. A numeric reference can widen int64 to float64 in some chunks and not others, so per-chunk and whole-frame masks can differ at values above 2**53. That relaxation is C8-iii. Codex's counterexample (integer column, redact `0.5`, `when x == 1`) keeps the rejection. Multi-table split uses the same classification; test 8 covers it.

**3h. Evidence.** No new fields. An admitted `when` column reports its operator's usual backend. Declines carry the 3d codes in chunked evidence.

**3i. Docs.**
- CHANGELOG.
- `docs/strategies.md` and the config reference: the field, the grammar, the reserved names, examples.
- Compatibility contract: chunked-native `when` for the four operators over string predicates, the null semantics, and the degenerate-type exception.
- Security note (corrected per Codex round 1 LOW): validation restricts public expressions to the closed grammar; the pandas oracle still evaluates the string with `pdf.eval`, and raw dicts are not validated.
- Roadmap: C8-i shipped, C8-ii and C8-iii planned.

## 4. Design notes

- **Parse at the boundary, reuse the oracle for meaning.** The public boundary is a closed grammar. The row mask reuses the oracle's own predicate function per chunk, so its meaning can never drift from pandas. That function already pins numexpr with empty scopes. Only the masking kernel runs natively.
- **One implementation of predicate meaning.** Rev 1 and 2 tried to re-derive pandas semantics on Arrow, and each round found a new type or representation gap. Reusing the oracle function closes the whole class, at the cost of a small per-chunk pandas conversion of the referenced columns.
- **Kernel on all rows, then select.** This is exact for value-keyed operators over strings. Timings at 1%, 50% and 100% selectivity are recorded. Filter-and-scatter is a later optimization.

## 5. Acceptance tests (written first; red-before recorded)

1. **Grammar.** Accepts every allowed form and both quote styles. Rejects every excluded form with `when_outside_closed_grammar`, including `sin == 1`, any reserved identifier, backslashes and quotes inside strings, `None`, and empty lists.
   - 1b. Hypothesis: generated grammar-valid predicates, with generated identifiers and literal spellings, all evaluate under the oracle's `pdf.eval` without error.
   - 1c. The reserved list contains numexpr's live function set, pandas' default eval globals and the keywords.
   - 1d. For each reserved name used as a string column name, the oracle `_eval_predicate` does NOT return a boolean column mask. This proves each reservation is needed, asserting the Series result rather than mere evaluation.
2. **Baseline (green-before): oracle selection.** Record the oracle's `_eval_predicate` result and its `df.loc[mask]` selection for:
   - object strings, `StringDtype` (pandas metadata) and int64 with nulls;
   - `==`, `!=`, `<`, `in`, `not in` and `not` on null cells;
   - including whether a nullable `NA` in the mask is excluded or raises.
3. **Mask parity.**
   - Hypothesis (`derandomize=True`): `when_mask` on a chunk equals the oracle's selection for the same chunk across generated predicates and column types (string, int64, float64, bool, with nulls).
   - A two-chunk case where chunk 1 converts to object and chunk 2 to `StringDtype` (metadata drift, Codex round 2 HIGH 1), with null predicate references and non-null targets, gives the chunked oracle's selection per chunk.
4. **Baseline (green-before): oracle output types.** For each operator with `when`, on whole-frame and both chunked legs: zero-row, all-null, zero-match, all-match and partial-match.
5a. **Zero-match evidence.** Kernel-call spies plus per-column evidence. A zero-match chunk makes no kernel call, counts nothing and is in `kernel_idle`. A mixed run (some chunks zero-match, some partial) credits the compiled backend only for chunks that ran.
5. **Chunked parity.**
   - For each of the four operators: predicates referencing the target, an unconfigured string column and a passthrough string column; partial, zero and all matches; nulls; chunk shapes zero-row, single-row, ragged and all-null.
   - Native chunked output equals oracle chunked output and the whole-frame oracle, with values, order and types per 3f.
   - Every case asserts native-route evidence.
6. **Declines.**
   - Each 3d rule gives its exact code, takes the chunked-oracle leg, and produces output equal to today's.
   - Covered rules: non-admitted strategy, non-string target, raw predicate outside the grammar, a reference to a column an earlier node writes, and a node with unknown writes.
   - The earlier-node case is Codex round 2's counterexample: `a` redacts to `"REDACTED"` before `z`, with `z`'s predicate `a == 'REDACTED'`, and the config order reversed from the execution order. A composite's extra written column is also covered.
   - A numeric-reference predicate is ADMITTED on the explicit chunked route and equals the chunked oracle.
   - The unified route still declines every `when` column.
7. **Public config.** Valid strings pass, every test-1 rejection fails, blank becomes None, the manifest round-trips, and a validated config with `when` runs end to end through `run_pipeline` on the chunked native route.
8. **Auto-router and split.**
   - A table whose `when` columns are all admitted auto-chunks and runs native.
   - Codex's integer/redact-0.5 counterexample stays full-frame with `when_predicate_not_chunk_stable` and succeeds.
   - A raw-dict non-grammar predicate stays full-frame.
   - A two-table split, with one admitted `when` table and one non-admitted, routes each table correctly and equals forced whole-frame.
9. **Security sentry.** `.eval(` and `.query(` method calls appear only in `execution/_when_gate.py` and `execution/_transforms.py`. `native/_when_mask.py` and `expressions/_when_parser.py` contain no `eval`, `exec` or `compile` calls of their own (`_when_mask.py` reaches pandas eval only through the oracle's `_eval_predicate`).
10. **Mutation (by hand).** Required mutants:
    - the mask built from the converted frame of the WRONG chunk, or from a non-protected conversion;
    - the zero-match short-circuit removed;
    - the zero-match result counted as ran;
    - a scalar node's own target dropped from the write set;
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
| Native mask differs from pandas eval | The mask IS the oracle's function on the oracle's conversion, per chunk (3c); tests 2 and 3 |
| Chunk-variant output via planner relaxation | Relax only for admitted configs with string references (3g); counterexample test 8 |
| False kernel evidence on zero-match chunks | Skipped is idle and uncounted (3e); test 5a |
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
- Codex round 2, REVISE (2 HIGH, 2 MEDIUM). Rev 3 changes the mask design rather than patching each gap:
  - H1 (representation drift between chunks) and M3 (pandas globals such as `inf`): the separate Arrow evaluator is replaced by the oracle's own `_eval_predicate` on the oracle's own per-chunk conversion of the referenced columns, so the mask matches by construction. Reserved names gain pandas' default globals for clear config-time errors.
  - H2: a node's write set is its own target plus its declared extra writes, and unknown writes decline, with the counterexample added.
  - M4: zero-match is skipped, idle and uncounted, with evidence tests.
  - This is a design change made during review (review discipline rule 3). It is recorded here because it removes the defect class both rounds found instead of adding special cases. If round 3 is not GO, it escalates to the owner.
