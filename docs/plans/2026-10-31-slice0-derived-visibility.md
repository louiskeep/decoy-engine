Status: plan only, revision 1. No implementation or release approval is granted by this document.

Summary: A `derived` expression is evaluated against the whole live row, keyed by the names pandas `itertuples` invents, so an expression can read a column it never declared (an undeclared, unmasked source column, through a positional alias) and a declared column with a leading underscore cannot be read at all. The fix makes the expression's visible context exactly its declared references, keyed by real column names, at the one evaluation seam.

Baselines: engine `origin/main` `a6e5c252` (2026-10-31), planning branch `plan/slice0`. Probes ran in the `uv.lock` venv built by `scripts/dev/bootstrap.sh` (Python 3.10.20, pandas 2.3.3, pyarrow 24.0.0).

Rules consulted: `DEFECT-CATALOG.md` and `OWNER-DECISIONS-2026-10-30.md` (decoy-platform `handoffs/queue`), the engine `CLAUDE.md`, `docs/compatibility-contract.md` 3.3 and 3.4.

# Slice 0, item 3: derived expression visibility (12.1 residue), PLAN (revision 1)

Risk: R2 (wrong-value and confidentiality fix on the pandas-backed routes; no format, no public signature, one additive reason code). Gate: Codex.

Threat model: V1. In scope: an ordinary config whose table has a column name that is not a Python identifier, or a declared column whose name starts with an underscore, producing a derived output computed from the wrong column. Out of scope: an operator writing an expression to exfiltrate a column they could read anyway (the config author is trusted in V1).

Not open: column ordering. `order_work` already runs a masked driver before its dependent through `column_dependency_edges` (`_runner.py:355-378`, `:381-402`), and non-identifier references cannot parse (`expressions/grammar.lark:70`). This plan changes neither.

## 1. Reproduction on main (yes)

`pandas.DataFrame.itertuples(index=False)` builds a `namedtuple` with `rename=True`: a column name that is not an identifier, or that starts with an underscore, is replaced by `_<position>`. Probe: `pd.DataFrame({"id": [1], "first name": [999], "_2": [5], "_total": [7]})` yields `_asdict()` keys `['id', '_1', '_2', '_3']` with values `[1, 999, 5, 7]`. The handler binds that dict as the row context (`_strategies/_derived.py:65-66`).

Case A (wrong value, undeclared column read). Source columns in order `id`, `first name` (undeclared, pre-GA `unconfigured_column_policy` warn, `_output_projection.py:22-27`), `_1` (declared `passthrough`, values 5 and 6), `d` (declared `derived`, expression `_1 + 0`). `run_pipeline(..., execution_mode="full_frame")` returns `d == [999, 888]`: the value of `first name`, which the plan never declared and never masked, because `first name` at position 1 became `_1` and the real `_1` was renamed away. Plan compile accepts the expression (`_1` is a declared column, `plan/_checks.py:617-624`).

Case B (declared column unreadable). Columns `_total` (declared `passthrough`) and `d` (`derived`, expression `_total * 2`): the run raises an uncoded `ValueError("derived expression evaluation failed at column 'd', row 0: '_total'")` from `transforms/derived.py:291-298`, because the context holds `_0`, not `_total`.

Control: plain names (`total * 2`) compute correctly. Generation mode builds its context from every already-generated column by real name (`generation/synthesize.py:371`), so it has the whole-row visibility but not the aliasing.

## 2. Guarantee (invariants as properties)

- **P1 (visibility).** When an expression is evaluated, the context it can observe contains exactly the column references compiled from its expression (`_get_column_refs`, `transforms/derived.py:180-197`), no more, no fewer, keyed by the real column name.
- **P2 (identity).** Each visible value is read from the live frame column of that exact name; no positional or renamed access exists on the path.
- **P3 (post-mask where declared).** A visible value for a masked sibling is its masked value (unchanged; `order_work` guarantees it) and for an unmasked sibling its source value.
- **P4 (missing is loud).** A declared reference absent from the frame at run time raises one registered code before any row is evaluated, never a per-row uncoded error.
- **P5 (no behavior change for valid names).** For identifier names without a leading underscore, outputs are byte-identical to main.

## 3. Design (smallest structural fix)

- `transforms/derived.py::apply_derived` (`:255-299`) becomes the visibility choke point: it already computes `col_refs`; it now evaluates against `{ref: row_context[ref] for ref in col_refs}` and never passes the caller's dict through. One seam serves both callers (mask handler and `synthesize.py:371`).
- `_strategies/_derived.py::run` (`:54-70`) stops using `itertuples()._asdict()`. It takes `refs = sorted(_get_column_refs(config.compiled))`, checks `set(refs) <= set(df.columns)` once and raises `ExecutionError(code=reasons.runtime.DERIVED_REF_MISSING)` (new, additive registry entry) otherwise, then iterates `df[refs].itertuples(index=False, name=None)` (plain tuples, no renaming) and builds `dict(zip(refs, values))`. The real column name is the key; the undeclared columns are never materialized into the context.
- The per-row error wrapper (`transforms/derived.py:289-298`) is unchanged; it no longer sees a missing-name `KeyError` from mask mode.
- Docstrings corrected: `_strategies/_derived.py:16-22` and `transforms/derived.py:40-48` state the exact-refs context.
- Not changed: `check_derived_column_refs`, `order_work`, the grammar, generation-mode missing-reference behavior (`synthesize.py:352-354`, documented per-row raise; noted for F31).

## 4. Class table

| # | Class | Closing structure | Proving test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | The missing-reference refusal names the column name only (owner result, a schema name), never a value; the per-row wrapper already formats only the column and row index | T5 marker: a frame whose values are markers; `DERIVED_REF_MISSING` text and the per-row error text contain no marker | Format `row_context` into the message: T5 fails |
| 2 | Cleanup missing on an exit path | N/A: the handler owns no resource; `df[column] = out` is assigned only after the loop completes, as today | | |
| 3 | State machine and concurrency | N/A: pure per-row function, no state | | |
| 4 | Route parity and silent fallback | One handler call site for every pandas-backed route (`_pandas_adapter.py:396-421`), one evaluation seam for mask and generation; out-of-core declines `derived` (`_compat.py:34-62` allowlist) | T4 route matrix: Case A under full_frame, sequential and the chunked entry; generation mode with an undeclared generated sibling | Build the context in the handler only and leave `apply_derived` passing the dict through: the generation cell of T4 fails |
| 5 | Referential integrity | N/A: derived reads same-table columns only (`plan/_checks.py:587-591`); no FK is read or written | | |
| 6 | Untrusted producer | The context is constructed by the engine from declared names; an expression cannot name a column outside the compiled refs | T6 field walk: every column name shape (`first name`, `_1`, `_total`, `Index`, `a.b`, unicode, a name equal to a reserved word) as a declared or undeclared sibling; the derived output never depends on an undeclared column | Reintroduce `_asdict()`: the `first name` cell fails |
| 7 | TOCTOU on a path | N/A: no path or file | | |
| 8 | Weak tests and overclaimed evidence | T1, T2, T4, T6 recorded red on `a6e5c252`; outputs asserted by value against a hand-computed expectation, not against the previous run | Red baseline file `docs/plans/evidence/2026-10-31-slice0-derived-red-baseline.txt` | Revert the handler: T1 and T2 fail |
| 9 | Behavior-preserving refactor drift | Characterization first: `tests/unit/execution/test_derived_strategy.py` (`:88-300`, including `test_row_context_is_columns_not_the_frame_index` at `:109`) and `tests/unit/transforms/test_derived.py` green before and after | T3: the existing suites plus a byte-identical output snapshot for a 5-column identifier-named table | Reorder `refs` without reordering values: T3 fails |
| 10 | Frozen contract drift | No public signature changes; one additive reason code registered through the F29 registry (`tests/sentry/test_reason_registry.py`) | T7: the registry sentry and the reason baseline pass; the new code has a record | Spell the code as a literal: the sentry fails |

## 5. Enumerations from the code

### 5.1 Exit paths

- Handler: config parse error (`DerivedConfig.from_dict`, `transforms/derived.py:98-177`), (new) missing-reference refusal before the loop, per-row evaluation error wrapped in `ValueError` (`transforms/derived.py:289-298`), normal completion with `df[column] = out` (`_strategies/_derived.py:69`).
- Generation mode: per-row `KeyError` for an undeclared earlier sibling (`synthesize.py:352-354`), unchanged.

### 5.2 Routes

- full_frame, sequential and chunked slices all run `derived` through `PandasExecutionAdapter` (`_fk_keys.py:11-14`, handler loop `_pandas_adapter.py:396-421`).
- out_of_core: `derived` is not in the supported work set (`_compat.py:34-62`), so the job falls to another route.
- Generation: `_derived_generate` (`synthesize.py:352-372`).

### 5.3 FK shapes

- N/A: derived reads same-table columns only (`plan/_checks.py:587-591`) and writes a non-key column; FK joins happen on other nodes. A derived column that is also an FK key is ordered by `order_work` like any other writer (`_runner.py:381-402`); its shape is not read here.

### 5.4 Fields

- `DerivedConfig.expression`, `.compiled`, `.bounds`, `.null_propagation` (`transforms/derived.py:92-95`).
- `ColumnSeed.provider_config` as consumed by `provider_config_to_dict` (`_strategies/_derived.py:61`).
- `ExecutionError.code`, `.message` for the new refusal; `QualityWarning` list returned empty by the handler (`_strategies/_derived.py:70`).

### 5.5 Output surfaces

- The derived column values (data plane).
- The missing-reference `ExecutionError` and the per-row `ValueError` text (column name and row index only).
- `undeclared_output_columns` warning from output projection (`_output_projection.py`), unchanged.

## 6. Reuse inventory

- `_get_column_refs` (`transforms/derived.py:180-197`), `check_derived_column_refs` (`plan/_checks.py:581-641`), `column_dependency_edges` and `order_work` (`_runner.py:355-402`), `provider_config_to_dict`.
- `reasons` registry (`reasons/runtime/_part3.py:1-40` layout) for the one new code; `tests/sentry/test_reason_registry.py` and `tests/sentry/_reason_baseline.py`.
- Test harness `_run` and `_col` in `tests/unit/execution/test_derived_strategy.py:85-107`; `tests/unit/transforms/test_derived.py` for `apply_derived` unit cases.
- `tests/sentry/test_module_size.py`: `_derived.py` is 70 LOC, `transforms/derived.py` 299; no census change.

## 7. Frozen contracts touched

- Public API (3.4): none. `apply_derived(config, row_context, *, column, row_index)` keeps its signature; its observable behavior narrows to the compiled refs (a caller passing extra keys sees them ignored; a caller omitting a ref sees the same `KeyError`-derived error as today).
- Determinism (3.3): outputs for identifier-named columns are byte-identical (T3); outputs for the two broken shapes change from wrong to right.
- Reason registry: one additive code `derived_ref_missing`.

## 8. Acceptance tests, red first (hostile corpus)

- T1 `tests/unit/execution/test_derived_strategy.py::test_expression_never_reads_an_undeclared_column_through_a_positional_alias`: Case A; expect `d == [5, 6]` (red on main: `[999, 888]`).
- T2 `::test_declared_underscore_column_is_readable`: Case B; expect `d == [14, 16]` (red on main: uncoded `ValueError`).
- T3 characterization snapshot and the existing suites.
- T4 route matrix as in the class table (full_frame, sequential, chunked entry, generation mode).
- T5 marker test, T6 name-shape field walk, T7 registry sentry.
- T8 `::test_missing_declared_ref_at_runtime_is_one_coded_error`: a source whose schema lacks a declared ref (schema drift) raises `ExecutionError` with `code == "derived_ref_missing"` before any row evaluation (spy on `apply_derived` call count is zero).
- T9 `tests/unit/transforms/test_derived.py::test_apply_derived_sees_only_compiled_refs`: spy on `evaluate`; context keys equal `_get_column_refs(compiled)` even when the caller passes extra keys.
- Commands: `.venv/bin/python -m pytest tests/unit/execution/test_derived_strategy.py tests/unit/transforms/test_derived.py tests/sentry/test_reason_registry.py -q`; full gate `scripts/dev/gates.sh`.

## 9. Slices

- **Slice A (engine, R2, about 30 LOC plus one reason record):** everything in section 3 and tests T1 to T9. Ships alone. Rounds: at most two; the third-round risk is a request to also code the generation-mode missing-reference path, which is listed as out of scope with its citation.

## 10. Pre-mortem

- A reviewer asks for ordering to be re-verified: the plan cites the existing edge mechanism and adds no ordering test beyond T3; T4's generation cell exercises declared-order semantics unchanged.
- `df[refs]` on a frame with duplicate column names returns a wider frame and misaligns `zip`: the transform path refuses duplicates (`reasons.runtime.DUPLICATE_SOURCE_FIELD_NAMES`, `reasons/runtime/_part3.py:195`, raised at `execution/_transforms.py:169`), but the plain mask path was not probed. T6 includes a duplicate-name cell; if the refusal does not fire before the handler, the handler refuses a duplicate among `refs` with that same code before the loop (one extra check, no new code).
- Per-row dict construction is slower than `_asdict()`: it is narrower (refs only) and `name=None` tuples are cheaper than namedtuples; the worker records a 1M-row timing in the evidence, no threshold is set.
- Someone proposes narrowing in the handler only: refused, the choke point is `apply_derived` so generation mode and any future caller inherit P1 (class 4 mutation).
- The new reason code collides with F31's registry work: it is one additive record in the existing namespace; F31 rebases.

## 11. Open owner questions

- Q1: Should generation-mode missing references also move to the coded refusal in this slice (about 10 LOC), or stay with F31's error reshaping? Recommendation: stay with F31; this plan fixes visibility, not error shape.

## 12. Worker evidence rules

Name the class of every finding and fix the class, not the instance. Write the generated tests first and record them red. Before pushing, attack your own diff: mutate each new mechanism once and show the test goes red. Run baselines with `uv.lock` in the same environment as the branch (`scripts/dev/bootstrap.sh`, `scripts/dev/gates.sh --baseline`) and state what you did not run. Never widen a threshold, skip a test or weaken an assertion to make the change pass. If a defect of a class already in the catalog appears a second time, stop and report it rather than patch again.
