Status: plan (rev 1)

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing, code-review, security.

# C8-iii-c: every `when:` predicate goes through the closed grammar

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C, after C8-i/ii/iii-a. Branch `feat/c8-iii-c-rawdict-when` off engine main `4a08f570`. Owner decision (Cam, 2026-10-07): "Validate", meaning the raw-dict path uses the closed grammar too. Risk R2: a pre-GA input break on a default path that also closes an evaluation surface.

## 1. Problem

C8-i made the public `ColumnConfig.when` field accept only the closed grammar (`expressions/when_grammar.lark`, `_when_parser.parse_when`), validated by the pydantic field validator (`config/_tables.py:129-152`). A config that reaches the engine as a raw dict skips that validator:
- `plan/_seed_envelope.py:250-251` copies any non-blank string into `ColumnSeed.when`;
- `plan/_serialize.py:475` does the same for a deserialized plan;
- the oracle then evaluates it with `DataFrame.eval` (`execution/_when_gate._eval_predicate`, numexpr engine, empty scopes).

So a raw-dict caller can still use anything pandas eval accepts:
- method calls (`x.notnull()`);
- bytes and f-string literals;
- column-to-column comparisons;
- bare names and constants (`x`, `1 == 1`).

The native route already declines these (`when_predicate_outside_native_subset`, `native/_when_admission.py:36`), so they fall back to pandas.

A second defect sits in the same function. `_eval_predicate`'s two error messages echo the predicate (`{expression!r}`, `_when_gate.py:141-155`). A predicate can embed literal data values (`ssn == '123-45-6789'`), and those messages reach job errors and logs. The standing log-hygiene rule forbids that.

## 2. Decision

**2a. One rule, one owner.** `parse_when` stays the only definition of an acceptable predicate. No second grammar or allow-list is added anywhere.

**2b. Compile-time check (both entrypoints).**
- A new `_check_when_grammar(config)` in `plan/_compile.py` sits next to `_check_when_with_coherent_with`. It is called at BOTH current call sites: the compile path (`:288`) and `run_config_only_checks` (`:510`).
- For every column whose `when` is a non-blank string, it calls `parse_when(when.strip())` and raises `PlanCompileError(code="when_outside_closed_grammar", path="tables.<t>.columns.<c>.when")`.
- The message carries the parser's reason. It never echoes the predicate text.
- A non-string, non-None `when` is rejected with the same code. Today the seed envelope silently drops it, so a gate the user asked for disappears without warning.
- Blank or whitespace-only still means "no gate", which is unchanged.
- **Nested children.** If a `strategy: nested` child config can carry `when` (the builder confirms), the check walks the children and reports the child path. If children cannot carry `when`, the record says so and cites the code.
- **Ordering.** The check runs before any masking or output write. Source profiling may already have happened, the same timing as the bucket_perturb guard.

**2c. Run-time backstop.**
- `_eval_predicate` calls a cached `parse_when` on the expression BEFORE `pdf.eval`. On failure it raises `StrategyError(code="when_outside_closed_grammar")`.
- Every evaluator shares this one function: the oracle gate, the unified slice and the native per-chunk mask. So deserialized plans, hand-built `ColumnSeed`s and any future caller are covered.
- The cache is keyed by the expression string, so per-chunk evaluation does not re-parse.

**2d. Message hygiene.** Both `_eval_predicate` failure messages drop the expression text. They name the column and strategy and keep the typed code. The chained cause (`from exc`) is kept for `when_expression_error`, because numexpr exception text can echo the expression, and the chain is NOT rendered into the message. The builder checks whether the engine's error rendering or logging prints chained causes. If it does, the builder uses `from None` and records why.

**2e. Native admission.** `when_predicate_outside_native_subset` becomes unreachable for any config that compiled. Keep the native gate's own parse, because it builds the reference list from the AST. Keep the reason code as defense for raw-`ColumnSeed` callers that skip compile; the backstop then raises on the oracle leg anyway. Delete no code in this slice.

**2f. Out of scope, recorded on the roadmap:**
- the `filter` and `derive` transforms' `_eval_clamped` (`execution/_transforms.py:71-83`), which is a separate pandas-eval surface for transform expressions;
- simplifying `_column_access.predicate_names` (the pandas tokenizer) to `when_column_refs(parse_when(...))`, which is now possible but is a refactor;
- null checks (`x.notnull()`), which are NOT in the closed grammar. A user who needs "only rows where x is not null" has no way to say it after this slice. Recorded as a grammar-extension candidate for Cam, not decided here.

## 3. Acceptance tests (written first; never weakened)

1. **Compile rejection, both entrypoints.** Each of these raises `PlanCompileError(when_outside_closed_grammar)` with the column path and WITHOUT the predicate text in the message:
   - `x` and `1 == 1`;
   - `x.notnull()`;
   - `s != b'zz'` and `s == f'{x}'`;
   - `a != b` (ref to ref);
   - `` `oops `` and `` `unterminated ``;
   - `index > 1` (reserved name);
   - `amount > amount.mean()` and `name.str.startswith('A')`;
   - a 5000-character predicate;
   - a non-string `when` (`1`, `True`, a list).
   Blank and whitespace-only predicates compile with no gate. `  region == 'US'  ` compiles and behaves like the stripped form.
2. **Nested** (if children carry `when`): rejected with the child path.
3. **Backstop.** A raw-dict `ColumnSeed` or a deserialized plan with `when: "x.notnull()"`, run without compile, raises `StrategyError(when_outside_closed_grammar)` on each route:
   - the oracle gate;
   - the unified full-frame route;
   - the chunked native route;
   - each out-of-core path that evaluates `when`. The builder lists which out-of-core paths do. If none do, the record says so with the evidence.
   It also raises with an empty frame and with a zero-row chunk. An output-write spy proves nothing is written.
4. **No echo.** For `when_expression_error` (an in-grammar predicate on a missing column) and `when_expression_not_boolean`, the error message and every captured log record contain no literal from the predicate. Use a distinctive literal such as `'SENTINEL-4417'`.
5. **No change for grammar predicates.** Every existing test whose predicate is inside the grammar stays green unmodified.
6. **Inventory first.** BEFORE implementing, the builder lists every test, fixture and helper whose raw-dict predicate is outside the grammar (survey hits: `tests/native/` has `b'zz'`, `.notnull()`, bare names, `1 == 1`, f-strings and backticks across about 20 files). For each one, record its intent and pick one of two moves:
   - (a) rewrite it to an in-grammar predicate with the same selection, when the predicate was just a vehicle for "some `when`" (for example, `s != b'zz'` becomes `s != 'zz'` with the column's literal type checked); or
   - (b) turn it into a rejection test, when its point was the non-grammar shape (for example, the C8-i "stays full-frame" decline cases).
   The record lists every change. No test is deleted without (b).
7. **Testflight:** STOP if a fingerprint moves, because no golden config uses a non-grammar `when`. Run the sentries, including log-interpolation, plus mutation on `_check_when_grammar` and the backstop branch.
8. **Docs:**
   - CHANGELOG under "Breaking (pre-GA)", with a migration table from common pandas forms to grammar forms;
   - `docs/strategies.md`'s `when:` section says the grammar applies to every caller.

## 4. Failure modes

| Risk | Closed by |
|---|---|
| A caller path that skips compile still evaluates arbitrary pandas | 2c backstop in the one shared evaluator; test 3 per route |
| Predicate literals leak through errors or logs | 2d; test 4 |
| A valid grammar predicate is newly rejected | The compile check calls the same `parse_when` the public validator already uses; test 5 |
| A silently dropped non-string `when` | 2b rejects it; test 1 |
| Test migration launders a behavior change | The test 6 inventory with a recorded intent per change; dennis checks it against the diff |

Rollback: revert the merge commit.

## 5. Review log

(none yet)
