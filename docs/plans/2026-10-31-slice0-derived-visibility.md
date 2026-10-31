Status: plan only, revision 2 (Codex round 1 remediation). No implementation or release approval is granted by this document.

Summary: A `derived` expression is evaluated against the whole live row, keyed by the names pandas `itertuples` invents, so an expression can read a column it never declared (an undeclared, unmasked source column, through a positional alias) and a declared column with a leading underscore cannot be read at all; and a per-row failure echoes the offending cell value into the exception text and cause chain. The fix makes the expression's visible context exactly its declared references, keyed by real column names, at the one evaluation seam, refuses a missing or duplicated reference once before any row with a registered code (mask execution), and translates per-row failures into value-free text.

Baselines: engine `origin/main` `6ce435ce` (2026-10-31, F13 #339 merged), planning branch `plan/slice0` rebased onto it. Probes re-ran on `6ce435ce` in the `uv.lock` venv built by `scripts/dev/bootstrap.sh` (Python 3.10.20, pandas 2.3.3, pyarrow 24.0.0).

Rules consulted: dev-rules `00-universal`, `risk-and-exceptions`, `development-loop` (R2 plan contract, Established solutions), `bugfix`, `security` (trust boundaries, value-free errors), `testing`, `api-and-compatibility`, `observability-and-resilience` (error text); `DEFECT-CATALOG.md`, `OWNER-DECISIONS-2026-10-30.md` (V1 threat model: exception text, `args` and the cause chain are in scope) and `HOSTILE-PLAN-REVIEW.md` (decoy-platform `handoffs/queue`); the engine `CLAUDE.md`; `docs/compatibility-contract.md` 3.3 and 3.4.

# Slice 0, item 3: derived expression visibility (12.1 residue), PLAN (revision 2)

Risk: R2 (wrong-value and confidentiality fix on the pandas-backed routes; no format, no public signature, one additive reason code). Gate: Codex.

Threat model: V1. In scope: an ordinary config whose table has a column name that is not a Python identifier, or a declared column whose name starts with an underscore, producing a derived output computed from the wrong column; and an ordinary cell value (a bad date string, a divisor) reaching exception text, `args` or the cause chain. Out of scope: an operator writing an expression to exfiltrate a column they could read anyway (the config author is trusted in V1); objects with hostile dunders.

Not open: column ordering. `order_work` already runs a masked driver before its dependent through `column_dependency_edges` (`_runner.py:355-378`, `:381-402`), and non-identifier references cannot parse (`expressions/grammar.lark:68-70`). This plan changes neither.

## 1. Reproduction on main (yes)

`pandas.DataFrame.itertuples(index=False)` builds a `namedtuple` with `rename=True`: a column name that is not an identifier, or that starts with an underscore, is replaced by `_<position>`. The handler binds that dict as the row context (`_strategies/_derived.py:65-66`). Re-run on `6ce435ce` through the adapter harness (`tests/unit/execution/test_derived_strategy.py::_run`, `::_col`, which drives `PandasExecutionAdapter.run_single`):

- Case A (wrong value, undeclared column read). Columns in order `id`, `first name` (undeclared), `_1` (declared `passthrough`, values 5 and 6), `d` (declared `derived`, expression `_1 + 0`). Output `d == [999.0, 888.0]`: the values of `first name`, never declared and never masked, because `first name` at position 1 became `_1` and the real `_1` was renamed away. Plan compile accepts the expression (`_1` is a declared column, `plan/_checks.py:581-641`).
- Case B (declared column unreadable). Columns `_total` (declared `passthrough`) and `d` (`derived`, expression `_total * 2`): uncoded `ValueError("derived expression evaluation failed at column 'd', row 0: '_total'")` from `transforms/derived.py:291-298`, because the context holds `_0`.
- Case C (value echo). Column `a` holding `"CANARY-SECRET"` and `d` with expression `days_between(a, "2020-01-02")`: `ValueError("derived expression evaluation failed at column 'd', row 0: Invalid isoformat string: 'CANARY-SECRET'")`; the marker is in `str(exc)`, in `exc.args` and in `exc.__cause__` (`raise ... from exc` at `derived.py:298`). The same wrapper formats a `ZeroDivisionError` as `float division by zero` (no value, by luck of the exception type).
- Duplicate names: a `pa.Table` with two fields named `x` fails in the adapter's `to_pandas` with an uncoded `KeyError('Field "x" exists 2 times in schema')` before any handler runs (full_frame probe); the only registered duplicate guard is transform-specific (`execution/_transforms.py:155-171`).
- Control: plain names (`total * 2`) compute correctly. Chunked entry: `run_mask_pipeline_chunked` refuses a `derived` column at admission with `PlanCompileError(code=strategy_not_chunk_safe)` (`_chunked.py:341-349`; `derived` is absent from `CHUNK_SAFE_STRATEGIES`, `_chunked_fk_vocab.py:44-59`), probed. Generation mode builds its context from every already-generated column by real name (`generation/synthesize.py:371`); the evaluator reads only compiled `col_ref` nodes (`expressions/_lark_parser.py:465-475`), so extra siblings are not observable there today (structural, not behavioral).

## 2. Established solutions

- Least-privilege evaluation context: simpleeval's own API takes an explicit `names` mapping and resolves identifiers only from it (the pattern `expressions/_safe_eval.py:46-95` already follows); Jinja2's sandbox and `ast.literal_eval` follow the same "closed namespace supplied by the caller" rule. The fix moves the closed namespace to `apply_derived`, where the compiled references are already known (`_get_column_refs`, `derived.py:180-197`).
- Positional access is the defect: pandas documents `itertuples(name=None)` as plain tuples with no renaming, and `DataFrame.__getitem__` with a list of names as the exact-name projection; both are used instead of `namedtuple(rename=True)` aliases.
- Value-free exception translation: raising the translated error outside the `except` block so neither `__cause__` nor `__context__` links the value-bearing original (PEP 3134 chaining; `from None` only suppresses display), and an exact-type table of translatable exception classes rather than `type(exc).__name__` (DEFECT-CATALOG class 1, "exact-type tables, never read `__name__`"); F31's closed typed events are the eventual boundary, and this plan's translation is an F31 input.
- Duplicate field names: pyarrow allows duplicate field names in a schema; the repo's registered refusal `duplicate_source_field_names` (`reasons/runtime/_part3.py:195`) is reused as the one code.

## 3. Guarantee (invariants as properties)

- **P1 (visibility).** When an expression is evaluated, the context it can observe contains exactly the column references compiled from its expression (`_get_column_refs`), no more, no fewer, keyed by the real column name.
- **P2 (identity).** Each visible value is read from the live frame column of that exact name; no positional or renamed access exists on the path, and a frame with duplicate column names is refused before projection.
- **P3 (post-mask where declared).** A visible value for a masked sibling is its masked value (unchanged; `order_work` guarantees it) and for an unmasked sibling its source value.
- **P4 (missing is loud, mask execution).** In mask execution a declared reference absent from the frame at run time raises one registered code before any row is evaluated, never a per-row uncoded error. Generation keeps its per-row behavior (section 4, non-goal).
- **P5 (value-free failure).** A per-row evaluation failure, in mask and in generation mode, carries only the column name, the row index and a fixed reason word from a closed table in `str(exc)`, `exc.args`, `__notes__`, `__cause__` and `__context__`; no cell value and no exception `__name__`.
- **P6 (no behavior change for valid names).** For identifier names without a leading underscore and expressions that evaluate, outputs are byte-identical to main.

## 4. Design (smallest structural fix)

- `transforms/derived.py::apply_derived` (`:255-299`) becomes the visibility choke point: it already computes `col_refs`; it now evaluates against `{ref: row_context[ref] for ref in col_refs}` built inside the guarded block and never passes the caller's dict through. A reference missing from `row_context` raises the same translated per-row error with reason `missing_reference` and the reference name (a schema name, an owner result), so generation's per-row behavior keeps its shape. One seam serves both callers (mask handler and `synthesize.py:371-372`).
- Value-free translation (`derived.py:291-298`): a module-level exact-type table `_FAILURE_REASONS: dict[type[BaseException], str]` (`ZeroDivisionError: "division_by_zero"`, `TypeError: "type_mismatch"`, `ValueError: "invalid_value"`, `OverflowError: "overflow"`, `KeyError: "missing_reference"`, `ValidationError: "unsupported_expression"`), looked up by exact `type(exc)`, default `"evaluation_error"`; the `except` block only records the reason word, and the `ValueError` with message `derived expression evaluation failed at column {column!r}, row {row_index}: {reason}` is raised after the block, so `__cause__` and `__context__` are both `None` (probed: `raise ... from None` inside the block leaves `__context__` pointing at the value-bearing exception). The exception type stays `ValueError` (no public signature change); the registered code for the mask path is added by the handler below.
- `_strategies/_derived.py::run` (`:54-70`) stops using `itertuples()._asdict()`. It takes `refs = sorted(_get_column_refs(config.compiled))`; refuses `len(set(df.columns)) != len(df.columns)` with `ExecutionError(code=reasons.runtime.DUPLICATE_SOURCE_FIELD_NAMES)` naming the duplicated names only; refuses `not set(refs) <= set(df.columns)` with `ExecutionError(code=reasons.runtime.DERIVED_REF_MISSING)` (new, additive registry entry) naming the missing names only; then iterates `df[refs].itertuples(index=False, name=None)` (plain tuples, no renaming) and builds `dict(zip(refs, values))`. Undeclared columns are never materialized into the context. The handler wraps the per-row `ValueError` into `ExecutionError(code=reasons.runtime.DERIVED_EVALUATION_FAILED)` (second additive code) with the same value-free text, raised outside the `except` block.
- Docstrings corrected: `_strategies/_derived.py:16-22` and `transforms/derived.py:40-48` state the exact-refs context and the value-free failure contract.
- Not changed: `check_derived_column_refs`, `order_work`, the grammar, generation-mode missing-reference shape (per-row, now value-free; `synthesize.py:352-354`), the adapter's `to_pandas` duplicate-name `KeyError` (pre-existing, uncoded, names only a field; recorded as an F31 input, not fixed here).

## 5. Class table

| # | Class | Closing structure | Proving test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | The two refusals name column names only (owner results); the per-row wrapper formats the column, the row index and a reason word from an exact-type table and raises outside the `except` block, so no cell value and no dynamic class name reaches `str`, `args`, `__notes__`, `__cause__` or `__context__` | T5 marker corpus: frames whose values are markers in str, int, list and dict shapes, under a bad-date expression, a division, a type mismatch and a missing reference, in mask and generation mode; every surface above is marker-free and the reason word is from the table | Format `row_context` or `exc` into the message, keep `from exc`, or raise inside the `except` block: T5 fails |
| 2 | Cleanup missing on an exit path | N/A: the handler owns no resource; `df[column] = out` is assigned only after the loop completes, as today | | |
| 3 | State machine and concurrency | N/A: pure per-row function, no state | | |
| 4 | Route parity and silent fallback | One handler call site for every pandas-backed route (`_pandas_adapter.py:405-424`), one evaluation seam for mask and generation; out-of-core declines `derived` with a registered reason (`_compat.py:115-119`); the chunked entry refuses it at admission (`_chunked.py:341-349`) | T4 route matrix: Case A under full_frame, under sequential (a two-table FK config, since `reject_explicit_sequential` needs a relationship, `_pipeline_routing.py:244`), under forced out_of_core (refusal `OocIneligibleError`, reason `out_of_core_dynamic_output_type_unsupported`) and under `run_mask_pipeline_chunked` (stable refusal `strategy_not_chunk_safe`); generation mode as the structural T9 | Build the context in the handler only and leave `apply_derived` passing the dict through: T9 fails. Add `derived` to `CHUNK_SAFE_STRATEGIES`: the chunked cell's expected refusal disappears and T4 fails |
| 5 | Referential integrity | N/A: derived reads same-table columns only (`plan/_checks.py:581-641`); no FK is read or written | | |
| 6 | Untrusted producer | The context is constructed by the engine from declared names; an expression cannot name a column outside the compiled refs; duplicate names are refused before projection | T6 three-class field walk (section 9) | Reintroduce `_asdict()`: the `first name` undeclared-sibling cell fails. Drop the duplicate check: the duplicate cell misaligns `zip` and fails |
| 7 | TOCTOU on a path | N/A: no path or file | | |
| 8 | Weak tests and overclaimed evidence | T1, T2, T4 (mask cells), T5, T6, T8 recorded red on `6ce435ce`; outputs asserted by value against a hand-computed expectation, not against the previous run; the generation cell is recorded as structural (no behavioral fail-before exists today) | Red baseline file `docs/plans/evidence/2026-10-31-slice0-derived-red-baseline.txt` | Revert the handler: T1 and T2 fail |
| 9 | Behavior-preserving refactor drift | Characterization first: `tests/unit/execution/test_derived_strategy.py` (including `test_row_context_is_columns_not_the_frame_index` at `:109`) and `tests/unit/transforms/test_derived.py` green before and after | T3: the existing suites plus a byte-identical output snapshot for a 5-column identifier-named table in mask and generation mode | Reorder `refs` without reordering values: T3 fails |
| 10 | Frozen contract drift | No public signature changes; two additive reason codes registered through the F29 registry (`tests/sentry/test_reason_registry.py`) | T7: the registry sentry and the reason baseline pass; both codes have records | Spell a code as a literal: the sentry fails |

## 6. Enumerations from the code

### 6.1 Exit paths

- Handler: config parse error (`DerivedConfig.from_dict`, `transforms/derived.py:98-177`), (new) duplicate-name refusal, (new) missing-reference refusal before the loop, per-row evaluation error translated and wrapped (`derived.py:291-298`, handler), normal completion with `df[column] = out` (`_strategies/_derived.py:69`).
- Generation mode: per-row translated error for an undeclared earlier sibling (`synthesize.py:352-354`, shape unchanged, text now value-free).
- Adapter boundary: duplicate Arrow field names raise an uncoded `KeyError` in `to_pandas` before any handler (pre-existing; F31 input).

### 6.2 Routes

- full_frame and sequential run `derived` through `PandasExecutionAdapter` (`_fk_keys.py:11-14`, handler loop `_pandas_adapter.py:405-424`); sequential eligibility needs a relationship (`_pipeline_routing.py:244`).
- out_of_core: `derived` is declined with `out_of_core_dynamic_output_type_unsupported` (`_compat.py:115-119`), so auto routing falls to another route and a forced out_of_core raises `OocIneligibleError`.
- Chunked entry: refused at admission with `strategy_not_chunk_safe` (`_chunked.py:341-349`).
- Generation: `_derived_generate` (`synthesize.py:345-372`).

### 6.3 FK shapes

- N/A: derived reads same-table columns only (`plan/_checks.py:581-641`) and writes a non-key column; FK joins happen on other nodes. A derived column that is also an FK key is ordered by `order_work` like any other writer (`_runner.py:381-402`); its shape is not read here.

### 6.4 Fields

- `DerivedConfig.expression`, `.compiled`, `.bounds`, `.null_propagation` (`transforms/derived.py:92-95`).
- `ColumnSeed.provider_config` as consumed by `provider_config_to_dict` (`_strategies/_derived.py:61`).
- `ExecutionError.code`, `.message` for the three refusals; `QualityWarning` list returned empty by the handler (`_strategies/_derived.py:70`).

### 6.5 Output surfaces

- The derived column values (data plane).
- The `ExecutionError` texts (`derived_ref_missing`, `duplicate_source_field_names`, `derived_evaluation_failed`) and the per-row `ValueError` text (column name, row index, reason word only).
- `undeclared_output_columns` warning from output projection (`_output_projection.py:21-30`), unchanged.

## 7. Reuse inventory

- `_get_column_refs` (`transforms/derived.py:180-197`), `check_derived_column_refs` (`plan/_checks.py:581-641`), `column_dependency_edges` and `order_work` (`_runner.py:355-402`), `provider_config_to_dict`.
- `reasons.runtime.DUPLICATE_SOURCE_FIELD_NAMES` (`reasons/runtime/_part3.py:195`) reused; `reasons` registry (`reasons/runtime/_part3.py` layout) for the two new codes; `tests/sentry/test_reason_registry.py` and `tests/sentry/_reason_baseline.py`.
- `expressions/_safe_eval.py:46-95` (closed `names` scope) as the pattern; `ValidationError` from the evaluator (`_lark_parser.py:470-474`) in the exact-type table.
- Test harness `_run` and `_col` in `tests/unit/execution/test_derived_strategy.py:40-84`; `tests/unit/transforms/test_derived.py` for `apply_derived` unit cases; `tests/parity/_f0_fixtures.py::cfg_derived` for the generation characterization.
- `tests/sentry/test_module_size.py`: `_derived.py` is 70 LOC, `transforms/derived.py` 299; no census change.

## 8. Frozen contracts touched

- Public API (3.4): none. `apply_derived(config, row_context, *, column, row_index)` keeps its signature and exception type; its observable behavior narrows to the compiled refs (a caller passing extra keys sees them ignored; a caller omitting a ref sees the translated per-row error).
- Determinism (3.3): outputs for identifier-named columns are byte-identical (T3); outputs for the two broken shapes change from wrong to right.
- Reason registry: two additive codes, `derived_ref_missing` and `derived_evaluation_failed`.

## 9. Acceptance tests, red first (hostile corpus)

- T1 `tests/unit/execution/test_derived_strategy.py::test_expression_never_reads_an_undeclared_column_through_a_positional_alias`: Case A; expect `d == [5.0, 6.0]` (red on main: `[999.0, 888.0]`).
- T2 `::test_declared_underscore_column_is_readable`: Case B; expect `d == [14.0, 16.0]` (red on main: uncoded `ValueError`).
- T3 characterization snapshot and the existing suites.
- T4 route matrix as in the class table (full_frame, sequential with an FK config, forced out_of_core refusal, chunked refusal).
- T5 marker corpus as in the class table (mask and generation).
- T6 name-shape field walk in three classes: (a) valid references that exist as declared siblings (`total`, `_total`, `_1`, `Index`, `x2`, a reserved-word-looking name such as `class_`), each read correctly; (b) names that exist only as undeclared siblings (`first name`, `a.b`, `_1` undeclared while the expression references a declared `_2`, unicode `naïve`, a column named `Index`): the derived output never depends on them (change their values, output unchanged); (c) parse-time refusals (`first name`, `a.b`, `naïve`, `__class__`) raise at plan compile or evaluation with the existing expression error, never reach the handler; plus (d) the duplicate-name cell: a frame with two `x` columns driven through the adapter harness is refused with `duplicate_source_field_names` before projection, and through `run_pipeline` the pre-existing adapter `KeyError` is recorded as the current behavior.
- T7 registry sentry.
- T8 `::test_missing_declared_ref_at_runtime_is_one_coded_error`: a source whose schema lacks a declared ref (schema drift past plan compile) raises `ExecutionError` with `code == "derived_ref_missing"` before any row evaluation (spy on `apply_derived` call count is zero); a per-row failure raises `code == "derived_evaluation_failed"`.
- T9 `tests/unit/transforms/test_derived.py::test_apply_derived_sees_only_compiled_refs`: spy on `evaluate`; context keys equal `_get_column_refs(compiled)` even when the caller passes extra keys, in a direct call and through `_derived_generate`. Structural: generation has no behavioral fail-before today and none is recorded.
- Commands: `.venv/bin/python -m pytest tests/unit/execution/test_derived_strategy.py tests/unit/transforms/test_derived.py tests/sentry/test_reason_registry.py -q`; full gate `scripts/dev/gates.sh`.

## 10. Slices

- **Slice A (engine, R2, about 45 LOC plus two reason records):** everything in section 4 and tests T1 to T9. Ships alone. Rounds: at most two; the third-round risk is a request to also code generation-mode missing references, which is listed as out of scope with its citation and is Q1.

## 11. Pre-mortem

- A reviewer asks for ordering to be re-verified: the plan cites the existing edge mechanism and adds no ordering test beyond T3.
- A reviewer asks why the per-row error is not itself coded in `apply_derived`: it is a transforms-level pure function used by generation too; the registered code is attached at the mask handler (the F29 boundary this slice owns), and generation's shape is Q1.
- The reason word table misses an exception type: the default `evaluation_error` is still value-free; T5's corpus includes an exception type outside the table.
- `__notes__` or a chained `__context__` carries the value: `raise ... from None` inside the `except` block keeps `__context__` (probed), so the wrapper raises after the block; T5 asserts `__cause__`, `__context__` and `__notes__`.
- Per-row dict construction is slower than `_asdict()`: it is narrower (refs only) and `name=None` tuples are cheaper than namedtuples; the worker records a 1M-row timing in the evidence, no threshold is set.
- Someone proposes narrowing in the handler only: refused, the choke point is `apply_derived` so generation mode and any future caller inherit P1 (class 4 mutation).
- The new reason codes collide with F31's registry work: two additive records in the existing namespace; F31 rebases.

## 12. Open owner questions

- Q1: Should generation-mode missing references also move to a coded pre-row refusal in this slice (about 10 LOC), or stay with F31's error reshaping? Recommendation: stay with F31; this plan fixes visibility and value-free text, not generation's error shape.

## 13. Review history

Codex round 1 on `c97bf0e9` (verdict GO-WITH-FIXES: 4 HIGH, 2 MEDIUM), verified against `6ce435ce`:

- HIGH per-row errors echo values: confirmed (`derived.py:298`; the canary appears in `str`, `args` and `__cause__`, probed). Fixed with the exact-type reason table and a raise outside the `except` block (section 4, P5, class 1 row, T5); recorded as an F31 input.
- HIGH P4 contradicts the generation non-goal: fixed by scoping P4 to mask execution and keeping generation's per-row shape (now value-free) with Q1 for the owner.
- HIGH generation "behavioral red" claim false: confirmed (`synthesize.py:371`, `_lark_parser.py:465-475`). T9 is structural only; no generation fail-before is recorded (class 8 row).
- HIGH chunked success route unreachable: confirmed (`_chunked.py:341-349`, probed `strategy_not_chunk_safe`). The route matrix expects the stable refusal (section 6.2, T4).
- MEDIUM duplicate names undecided: fixed as a contract; the handler refuses non-unique `df.columns` with the registered code before projection (section 4, P2, T6d); the adapter-level uncoded `KeyError` is recorded as pre-existing.
- MEDIUM T6 mixes name classes: fixed; T6 is split into valid references, undeclared-only siblings and parse-time refusals, plus the duplicate cell.
- Missing canonical rulebooks and Established solutions: fixed (Rules consulted line, section 2).
- Self-attack of the same classes elsewhere: the sequential cell of T4 needs an FK relationship to be eligible (`_pipeline_routing.py:244`), which revision 1 did not state; the forced out_of_core refusal is now a matrix cell; `__context__` is covered alongside `__cause__`, and the probe showed `from None` is not enough for it, so the wrapper raises after the `except` block.

## 14. Worker evidence rules

Name the class of every finding and fix the class, not the instance. Write the generated tests first and record them red. Before pushing, attack your own diff: mutate each new mechanism once and show the test goes red. Run baselines with `uv.lock` in the same environment as the branch (`scripts/dev/bootstrap.sh`, `scripts/dev/gates.sh --baseline`) and state what you did not run. Never widen a threshold, skip a test or weaken an assertion to make the change pass. If a defect of a class already in the catalog appears a second time, stop and report it rather than patch again.
