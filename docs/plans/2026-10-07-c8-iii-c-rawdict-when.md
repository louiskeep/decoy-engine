Status: plan (rev 3, BUILD-READY: Codex plan gate GO in round 3)

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, testing, code-review, security.

# C8-iii-c: every `when:` predicate goes through the closed grammar

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C, after C8-i/ii/iii-a. Branch `feat/c8-iii-c-rawdict-when` off engine main `4a08f570`. Owner decision (Cam, 2026-10-07): "Validate", meaning the raw-dict path uses the closed grammar too. Risk R2: a pre-GA input break on a default path that also closes an evaluation surface.

## 1. Problem

C8-i made the public `ColumnConfig.when` field accept only the closed grammar (`expressions/when_grammar.lark`, `_when_parser.parse_when`), validated by the pydantic field validator (`config/_tables.py:129-152`). A config that reaches the engine as a raw dict skips that validator:
- `plan/_seed_envelope.py:250-251` copies any non-blank string into `ColumnSeed.when`;
- `plan/_serialize.py:475` copies `data.get("when")` for a deserialized plan with NO type or blank filtering (a YAML round trip keeps `a.notnull()`); `plan_from_yaml` plus the adapter's `run`, `run_single` and `run_sequential` never compile again;
- the oracle then evaluates it with `DataFrame.eval` (`execution/_when_gate._eval_predicate`, numexpr engine, empty scopes).

So a raw-dict caller reaches pandas eval with anything. These work as gates (Codex round 1 probed them):
- method calls (`x.notnull()`);
- bytes and f-string literals;
- column-to-column comparisons;
- a boolean bare column (`x`).

Others reach evaluation but fail there: `s == f'{x}'` errors, and `1 == 1` gives a scalar that fails the boolean check.

The native route already declines these (`when_predicate_outside_native_subset`, `native/_when_admission.py:36`), so they fall back to pandas.

A second defect sits in the same function. `_eval_predicate`'s two error messages echo the predicate (`{expression!r}`, `_when_gate.py:142` and `:157`). A predicate can embed literal data values (`ssn == '123-45-6789'`), and those messages reach job errors through `str(exc)`.

Chained causes leak the same text: pandas' `DateParseError` for `x < 'SENTINEL'`, and the Lark diagnostic that `parse_when` deliberately chains (`_when_parser.py:177`, `err.__cause__ = cause`). Any traceback or `logger.error(..., exc_info=True)` renders them. The standing log-hygiene rule forbids all of this.

**Single evaluator (verified, Codex round 1).** `_eval_predicate` is the only production evaluator of `when` strings:
- every pandas, sequential, multi-table, generate-plus-mask and chunked-oracle path goes through `run_with_when_gate`;
- the unified slice (`_unified_slice_when.py:88`) and the native mask (`native/_when_mask.py:59`) call it directly;
- out-of-core rejects an effective `when` (`out_of_core/_compat.py:224`);
- chunked FK, group-key and text_mask reject the chunked combinations;
- filter and derive transforms evaluate other expressions.

**Nested.** A nested child cannot carry an effective gate: `_nested.py:190` builds the child seed with `when=None`. A `when` key inside a child's `strategy_config` is inert provider configuration. This slice leaves both alone, because validating such a key would not implement child gating.

## 2. Decision

**2a. One rule, one owner.** `parse_when` stays the only definition of an acceptable predicate. No second grammar or allow-list is added anywhere.

**2b. Compile-time check (both entrypoints).**
- A new `_check_when_grammar(config)` in `plan/_compile.py` sits next to `_check_when_with_coherent_with`. It is called at BOTH current call sites: the compile path (`:288`) and `run_config_only_checks` (`:510`).
- For every column whose `when` is a non-blank string, it calls `parse_when(when.strip())` and raises `PlanCompileError(code="when_outside_closed_grammar", path="tables.<t>.columns.<c>.when")`.
- The message carries the parser's reason. It never echoes the predicate text.
- A non-string, non-None `when` is rejected with the same code. Today the seed envelope silently drops it, so a gate the user asked for disappears without warning.
- Blank or whitespace-only still means "no gate", which is unchanged.
- **Nested children.** Nothing is walked, per the section 1 finding. The record cites `_nested.py:190`.
- **Ordering.** The check runs before any masking or output write. Source profiling may already have happened, the same timing as the bucket_perturb guard.

**2c. Plan-level validation, then an evaluator backstop.**
- **Plan level (primary).** A new `validate_plan_when(plan)` sits next to the parser. It parses EVERY `ColumnSeed.when` in a supplied `Plan`, whether or not that seed will reach the scalar gate. That includes FK-resolved and composite nodes and seeds for tables absent from the supplied sources. It raises `ValidationError(when_outside_closed_grammar)` naming table and column only.
  - It runs at the start of each public entrypoint that accepts a Plan, BEFORE any handler, provider, sink or output write:
    - `PandasExecutionAdapter.run`, `run_single` and `run_sequential`;
    - `generate_tables(plan)`, which reaches the providers without an adapter;
    - any other Plan-taking public entry the builder finds, each listed in the record.
  - It iterates the whole seed envelope, independently of the supplied sources and the work-node selection.
  - It also runs in plan deserialization (`_serialize`) on the reconstructed seed envelope before the Plan is returned, so a stored plan fails at load. Runtime guards stay, because Plans can be built directly.
  - The reason: sequential execution writes each finished table (`_sequential.py:493`). An evaluator-only check found a bad predicate on table 2 after table 1 was already written, which Codex round 1 reproduced.
- **Evaluator backstop (retained).** `_eval_predicate` calls a cached `parse_when` before `pdf.eval`, for hand-built calls that reach the evaluator without a Plan. On failure it raises `StrategyError(code="when_outside_closed_grammar")`. The cache is keyed by the expression string.

**2d. Message hygiene, including exception chains.**
- Both `_eval_predicate` failure messages drop the expression text. They name the column and strategy and keep the typed code.
- **Chains are suppressed at the source.**
  - `parse_when` stops attaching the Lark cause (`_when_parser.py:177`) and raises its `ValidationError` with no cause.
  - `_eval_predicate`'s pandas-error boundary raises `from None`.
  - The new compile, plan-level and backstop rejections raise `from None`.
  - Every explicit `raise ... from exc` in `parse_when` is replaced, not only `_reject`'s `__cause__` assignment.
  - `from None` suppresses rendering but keeps `__context__` on the object. The guarantee is that tracebacks and logging output carry no predicate text. It is not object-level erasure.
  - The numexpr and pandas exception class name may be logged at debug level, as a class name only. Diagnosing a failing predicate then relies on its position and construct, not the text.

**2e. Native admission.** `when_predicate_outside_native_subset` becomes unreachable for any config that compiled. Keep the native gate's own parse, because it builds the reference list from the AST. Keep the reason code as defense for raw-`ColumnSeed` callers that skip compile; the backstop then raises on the oracle leg anyway. Delete no code in this slice.

**2f. Out of scope, recorded on the roadmap:**
- the `filter` and `derive` transforms' `_eval_clamped` (`execution/_transforms.py:71-83`), which is a separate pandas-eval surface for transform expressions;
- simplifying `_column_access.predicate_names` (the pandas tokenizer) to `when_column_refs(parse_when(...))`, which is now possible but is a refactor;
- a general null test. The grammar has no general-purpose null predicate. Some known dtype domains admit equivalent comparisons (strings: `x >= ''`; numbers: `x < 0 or x >= 0`), but these are not universal replacements for `.notnull()`. An explicit null-test grammar extension is tracked separately for Cam. It does not block this slice, which carries out Cam's pre-GA decision.

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
2. **Plan-level, zero writes.**
   - (i) **Deserialization.** A serialized two-table plan whose second table has `when: "s.notnull()"` is rejected by `plan_from_yaml` (and the dict loader) with `ValidationError(when_outside_closed_grammar)`. Run that case on its own.
   - (ii) **Adapters and entrypoints.** Build the invalid Plan with `dataclasses.replace` from a VALID fixture (no guard bypassed and no deserialization involved), then call each entrypoint directly:
     - `run_sequential` into an in-memory callable sink: the typed rejection with ZERO sink writes and zero handler calls (spy);
     - `run`: the typed rejection before any handler call;
     - `run_single` targeting table A while the invalid seed stays on table B: the typed rejection;
     - `generate_tables`: the typed rejection with the provider never called (spy).
   - (iii) The same rejection in each of these places: an FK-resolved node, a composite node, and a seed whose table is absent from the supplied sources.
3. **One test per boundary, each with its expected class (no guard bypassed to label a test native):**
   - (a) public entrypoints (`run_pipeline`, the chunked config entrypoints including zero-chunk input, `run_config_only_checks`): `PlanCompileError`;
   - (b) admission, unchanged (section 2e), each tested directly:
     - native admission returns the decline code `when_predicate_outside_native_subset:<col>`;
     - unified admission returns `False`;
     - separately, `when_specs` raises `ValidationError(when_outside_closed_grammar)`.
   - (c) the direct mask helpers (`_eval_predicate`, the unified-slice mask, the native mask) with explicitly constructed bindings: `StrategyError(when_outside_closed_grammar)`, on a non-empty and on a zero-row frame.
4. **No echo, anywhere it can render.** Use the sentinel literal `'SENTINEL-4417'` in:
   - a grammar-valid datetime comparison with an invalid date literal (`x < 'SENTINEL-4417'` on a datetime column), which hits `when_expression_error`;
   - a malformed predicate whose Lark diagnostic would carry it (`s == 'SENTINEL-4417' and x.notnull()`), on every boundary of 1 to 3 that RAISES (the admission declines in 3(b) return values and are not rendered);
   - an accepted predicate whose eval is patched to return a non-boolean, which hits `when_expression_not_boolean`.

   For each, assert that the sentinel is absent from `str(exc)`, from `traceback.format_exception(exc)` (the full chain), and from the formatted output of a `logging` handler that calls `logger.error(..., exc_info=True)`. Asserting only on `str(exc)` or `record.getMessage()` is not enough.
4a. **Kept defenses, tested directly.**
   - The pandas scope clamps: an eval spy on an ACCEPTED predicate asserts `engine="numexpr"`, `local_dict={}` and `global_dict={}`.
   - The `when_expression_not_boolean` branch, using an accepted predicate plus an injected non-boolean result. This replaces the `n + 1` style tests, which become grammar rejections.
5. **No change for grammar predicates.** Every existing test whose predicate is inside the grammar stays green unmodified.
6. **Inventory first.** BEFORE implementing, the builder lists every test, fixture and helper with a predicate outside the grammar. That includes raw-config integration tests AND direct helper tests (`predicate_names`, `_column_access`, `_when_gate` mutation kills, composite admission, rev9 read sets). For each one, record what it protects, which may be any of: selection, routing, read-set, dtype or metadata preservation, or a side effect. Then pick exactly one disposition:
   - (a) **Rewrite to a grammar predicate.** Allowed only when every protected property is preserved and asserted. For example, `s != b'zz'` is NOT the same as `s != 'zz'`, because the bytes form selects every row; a valid rewrite must keep the same selection on the fixture, and the same read set and route.
   - (b) **Turn it into a rejection test.** Allowed only when the non-grammar shape WAS the point, with nothing else protected.
   - (c) **Keep or relocate the lower-level defensive coverage unchanged.** Use this when the test exercises a retained defense through a helper below compile, such as conservative read sets for backticks and f-strings (`test_chunked_entry_rev9_read.py:438, 445`), or `2**53` nullable-int preservation under conservative reads (`test_composite_admission.py:625`). Call the helper directly so compile cannot reject the input first.

   Mixed-route coverage (`test_c8_i_when_auto_route.py:166`) is kept with a grammar-valid predicate that still declines native admission. Compile-rejection tests are added separately and never replace one of the above. The record lists every test with its protected properties and its disposition. No test is deleted without (b).
7. **Testflight:** STOP if a fingerprint moves, because no golden config uses a non-grammar `when`. Run the sentries, including log-interpolation, plus mutation on `_check_when_grammar` and the backstop branch.
8. **Docs:**
   - CHANGELOG under "Breaking (pre-GA)", with a migration table from common pandas forms to grammar forms;
   - `docs/strategies.md`'s `when:` section says the grammar applies to every caller.

## 4. Failure modes

| Risk | Closed by |
|---|---|
| A caller path that skips compile still evaluates arbitrary pandas | 2c backstop in the one shared evaluator; test 3 per route |
| Predicate literals leak through errors, tracebacks or logs | 2d with chains suppressed at the source; test 4 renders the full chain and the logging output |
| A bad predicate on a later table after earlier output is written | 2c plan-level validation before any handler or sink; test 2 |
| A valid grammar predicate is newly rejected | The compile check calls the same `parse_when` the public validator already uses; test 5 |
| A silently dropped non-string `when` | 2b rejects it; test 1 |
| Test migration erases unrelated coverage | Test 6's three dispositions with protected properties recorded; test 4a; dennis checks the record against the diff |

Rollback: revert the merge commit.

## 5. Review log

- **Codex plan gate, round 1: REVISE** (3 HIGH, 1 MEDIUM, 1 LOW). It confirmed `_eval_predicate` is the single production evaluator. Rev 2:
  - **HIGH (exception chains):** chains are suppressed at the source (`parse_when`, the evaluator, and every new rejection), and test 4 renders tracebacks and logging output;
  - **HIGH (writes before detection):** plan-level validation of every seed before any handler or sink, also at deserialization, plus the two-table zero-write test;
  - **HIGH (migration erasing coverage):** test 6 records protected properties and adds disposition (c); test 4a; mixed-route coverage kept;
  - **MEDIUM:** test 3 is split by boundary, each with its exception class;
  - **LOW:** section 2f's null-test wording;
  - section 1 factual corrections (the serializer's lack of filtering, usable versus reachable predicates, line `:157`, the nested finding).
- **Codex plan gate, round 2: REVISE** (2 MEDIUM). All three round-1 HIGHs are closed at plan level. Chain suppression is safe: 367 grammar and config cases pass, and nothing depends on the Lark cause. Rev 3:
  - test 2 splits deserialization rejection from the entrypoint guards; the entrypoint tests use `dataclasses.replace` Plans, a handler or provider spy, and zero sink writes;
  - test 3(b) tests native decline, unified `False` and `when_specs` separately;
  - `generate_tables` is named in the entrypoint inventory;
  - every `raise ... from exc` is replaced, and the `__context__` limit is stated.
- **Codex plan gate, round 3: GO** (high confidence). Both round-2 MEDIUMs closed. Runtime probes confirmed that today's `run`, `run_single`, sequential and `generate_tables` all proceed past an invalid seed, so the new tests will detect missing guards.
