Status: plan

# GEN-FORMULA-SEQ: a generate formula reads each column with its own type (roadmap row 24y)

Rules consulted: `/home/cam/dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`,
`development-loop.md`, `bugfix.md`, `feature-dev.md`, `architecture.md`, `reuse-first.md`,
`cross-cutting-concerns.md`, `api-and-compatibility.md`, `security.md`, `testing.md`,
`scope-discipline.md`, `verification.md`, `documentation.md`, `profiles/local-operations.md`,
`catalogs/recurring-defects.md`; engine `CODEMAP.md`, `.importlinter`, `docs/reasons.md`,
`docs/compatibility-contract.md` (3.3); CLI `docs/ROADMAP.md` (rows 24j, 24k, 24y, 24zd),
CLI `CODEMAP.md`.

Revision 1, not yet reviewed.

Bases (2026-11-13): engine `origin/main` `8c9c1162`, CLI `origin/main` `5c9dd8e` (pins
`decoy-engine==0.23.0`). Line numbers are read at these bases.

Owner decisions used: the V1 bar (2026-11-02: a product-function break or data corruption blocks
V1; silent wrong output is corruption); row 24b QA-GATE (2026-11-08: a config error fails with
exit 1 and a code, never exit 3); "names may show in messages; data values never" (2026-11-06);
pre-release, no product decision is locked, so output may change with a `Breaking:` line.

Risk: E1 is R2 (generated output changes, two new reason codes, a recorded parity refreeze).
C1 is R1 (one exit-code mapping, tests and docs).

## 1. Frame

### 1.1 Evidence

A repro on engine `8c9c1162` (Mac node and decoy-test-5 through `vm-run`; `compile_plan`, then
`generate_tables`; 3 to 6 rows; seed 1):

| Config | Formula output today | Correct |
|---|---|---|
| `a: sequence start 1`, `round(a / 3)` | `[None, None, None]`, one log warning, exit 0 | `[0, 1, 1]` |
| same, `a * 2` | `['11', '22', '33']` (string repeat) | `[2, 4, 6]` |
| same, `a == 1` | `[False, False, False]` | `[True, False, False]` |
| `pyint` faker, `x / 3` | all null | numbers |
| `pyfloat` faker, `x * 2` | the text repeated twice | a float |
| categorical `[1, 2, 3]` with `null_probability: 0.5`, `str(cat)` | `'1.0'`, `'2.0'` | `'1'`, `'2'` |
| sequence read by `derived: a + 1` and by any formula | the run crashes with an uncoded `ValueError` from the derived column | runs |
| inline formula `randint(5, 1)` (no references) | all null, exit 0 | a coded failure |

Without the formula, `numeric_plan` types the same `sequence`, `pyint` and `pyfloat` columns as
`int64` and `float64`. With it, all three are `string` in the output as well.

### 1.2 Root cause

Two defects, both at the source:

1. **Reader typing.** `numeric_plan` (`generation/_numeric_typing.py:192`) is the one owner
   that decides whether a generated column is a number. A column stays a number only while every
   reader of it is "safe" in the closed `READERS` registry. `formula.references` is registered
   unsafe (`generation/_numeric_typing.py:124`, "formula locals are the cell text"), so one
   formula that reads a plain sequence or an int or float faker turns the column into text for
   every reader and for the output. The formula then does text arithmetic. Engine #445 chose this
   to keep `'ORD-' + id` and `id.zfill(5)` working (`tests/parity/test_numeric_typing_readers.py:41`).
2. **The pandas hop and the swallowed error.** The post-pass builds `pd.DataFrame(data)`
   (`generation/_referenced_formula.py:58`), which turns an int list holding `None` into
   `float64`, so a formula sees `1.0` for `1`. Then every per-row exception becomes `None` with
   one log warning (`generators/_formula.py:219` for referenced formulas, `:282` inline).
   Nothing coded reaches the run result or the CLI.

Kinds checked:

| Generator kind | Type a formula sees today | Same root cause |
|---|---|---|
| `sequence` (plain, fits int64) | `str` | yes, reader typing |
| `faker` int providers (`pyint`, `random_int`, `random_digit*`, `random_number`) | `str` | yes, reader typing |
| `faker` float-annotated providers (`pyfloat`) | `str` | yes, reader typing |
| `reference` to one of the above | `str` (the reference group is demoted together) | yes, reader typing |
| `categorical` with int or float categories, `constant`, `distribution`, `grouped_series` | the number, but `float` for an int column that holds a null | yes, the pandas hop |
| `sequence` with a prefix, suffix or pad; text faker; unproven range | `str` | no: the column is text in the output too, so the formula already sees the column's own type |
| `faker` date providers, `windowed_date` | ISO `str` | no: dates are text in generate output on every route; out of scope |
| `statistical`, `lookup`, `derived`, `derived_aggregate`, `group_key`, `formula` | unchanged | the binding stays as today (4.3 rule 2) |

### 1.3 Interactions

- **GEN-INHERIT** (remote PRs #523, #546, #555, #557, #561; open; not depended on). #557 moves
  `_reference_root` to `config.reference_root` and the generated-type fallback to
  `config.generated_arrow_type`; #555 and #561 add `READERS` entries. E1 changes one existing
  `READERS` entry and adds one public wrapper next to `numeric_plan`. Rule: the wrapper calls the
  same function `numeric_plan` uses for the generated type, never a second copy. If #557 merges
  first, E1 rebases its wrapper onto `config.generated_arrow_type`. If E1 merges first, #557
  replaces the inner `generated()` as it already does and the wrapper follows. The new
  GEN-INHERIT readers (`anchor_via`, `via`, `categorical.condition_on`) are not formula readers
  and are untouched.
- **DUP-DEFECTS E2** (engine #564, open): `is_null_cell` in `internal/pandas_missing.py`
  becomes the one missing-cell rule for derived and formula, and `generators/_formula.py:206`
  calls it. E1 uses the same predicate at the same line. If #564 merges first, E1 imports it; if
  E1 merges first, #564 rebases one line. E1 adds no second missing-cell check.
- **Engine #465 (GEN-STRICT E2)** dropped `round(a / 3)` from the generating case list
  (`tests/unit/plan/checks/test_generate_columns.py:531` keeps only `i + index`) because it
  came back as nulls. AT-1 puts it back.

## 2. Guarantee

After E1:

1. A `formula` sees each referenced cell with the type the column is generated with
   (`numeric_plan`'s answer for `sequence` and `faker`, the native value for the other kinds),
   never a text copy of a number and never a float copy of an int.
2. Adding a formula reader never changes the type of the column it reads, in the output or for
   another reader.
3. A formula that raises on a row fails the run with `runtime.generate_formula_evaluation_failed`
   (a `GenerationError`). The message names the table, the column, the row index and a fixed
   reason word. It holds no cell value, and no exception is chained.
4. The only nulls a formula writes come from the config: `null_probability` on the formula, or a
   missing referenced cell (rules in 4.3).
5. A formula that uses a number reference as text in a way that would silently change meaning
   (a compare with a text literal) or always fail (concatenation with text, a text method) is
   refused at compile with `plan.generate_formula_number_used_as_text`, in `decoy validate` and
   in `decoy run`.

After C1: `decoy run` exits 1 for `generate_formula_evaluation_failed` and writes no output; the
docs say how a formula sees types.

Non-goals: mask-mode `formula` (`transforms/formula.py`, reads source cells); date typing; the
other text-demoting readers (`group_by`, `condition_on`, `anchor`, `derived_aggregate`, see 13);
the uncoded `ValueError` from a generate-mode `derived` failure (see 13).

## 3. Established solutions

Search run: a web search for pandas int-with-NaN upcasting and the nullable and object dtypes
(pandas user guide, "Nullable integer data type",
https://pandas.pydata.org/docs/user_guide/integer_na.html), plus the Python references below and
the engine's own registry and expression code.

| Problem | Established answer | Source | Used how |
|---|---|---|---|
| Mixed str and int operands | Python raises `TypeError` for `'a' + 1` and never coerces; `str * int` repeats | Python language reference, "Binary arithmetic operations" | the formula sees the real type; text use is spelled `str(a)` or an f-string |
| Errors passing silently | "Errors should never pass silently. Unless explicitly silenced." | PEP 20 | a failing row fails the run; only config-requested nulls are quiet |
| int column with a missing value | "Because NaN is a float, this forces an array of integers with any missing values to become floating point"; the nullable dtype must be asked for | pandas user guide, "Nullable integer data type" | the post-pass frame uses `dtype=object` columns, which keep the Python `int` and `None` the generator made (no `pd.NA` that today's null check misses before #564) |
| Null in arithmetic | an operator with a NULL operand yields NULL | ISO SQL null semantics | a missing number reference gives a null result (4.3 rule 1) |
| Value-free errors | build the message from closed text and raise after the handler | PEP 3134; the exact-type reason table `transforms/derived.py:312` | reused for the formula failure word |

The registry (`READERS`) and the AST walk over a formula (`plan/checks/_generate_columns.py:417`)
already exist. E1 adds no new mechanism beyond one check module.

## 4. Design

### 4.1 Reader typing (root cause 1)

- `generation/_numeric_typing.py:124`: `formula.references` becomes
  `Reader(True, "formula locals are the generated values; text use is refused at compile")`, and
  the module docstring bullet changes to match. Nothing else in `numeric_plan` changes.
- Add `reader_types(config, *, provider_snapshot=None) -> dict[Key, pa.DataType]` beside
  `numeric_plan`: for every generated column, the type its readers see (the inner `generated()`
  answer, which already resolves `reference` columns to their root). `numeric_plan` and
  `reader_types` share one inner function; there is no second type table.

### 4.2 The post-pass frame (root cause 2a)

`generation/_referenced_formula.py:58` builds each frame column as
`pd.Series(values, dtype=object)`, and writes each filled formula column back the same way.
Values reach the formula as the generator made them (`restore_declared` already ran in
`synthesize.py`). The set of referenced columns whose `reader_types` entry is a number or bool is
passed to `fill_referenced_formula_column` as one new keyword; `synthesize.py` computes it once,
next to `numeric` at `generation/synthesize.py:140`.

### 4.3 Row rule (root cause 2b), in `generators/_formula.py`

For each row, using the missing-cell predicate from 1.3:

1. A referenced cell of a number-typed column is missing: the output cell is null and the
   formula does not run for that row (null in, null out). Counted per column.
2. A referenced cell of any other column is missing: `""` is bound, as today
   (`generators/_formula.py:208`). If the formula then raises, the cell is null and counted,
   because the config asked for nulls in that column.
3. Otherwise a raise fails the run. Keep the first failing row, leave the `except` block, then
   raise `GenerationError(code=GENERATE_FORMULA_EVALUATION_FAILED, ...)` with the text
   `formula column 't'.'x' failed at row N: <reason>`. The reason word comes from the derived
   exact-type table through a new public `transforms.derived.failure_reason(exc)`, which derived
   also calls. Undefined names cannot reach this point (GEN-STRICT E2 refuses them).
4. The inline path (`_eval_formula_inline`, no references) applies rule 3.

Per-row seeding is unchanged and every row reseeds, so a row skipped under rule 1 moves no other
row's draws. The two existing log warnings stay (counts and column names only).

### 4.4 Compile check: a number used as text (construction guard)

The class is "a reader sees a value type other than the column's type". The type side is closed
by construction (one owner, `numeric_plan`, and the closed `READERS` registry with its sentry).
The formula side gets a compile check, because the old text spellings are the realistic bypass.

New `plan/checks/_formula_types.py`, called from `check_generate_columns` just before
`check_generated_parent_keys(config)` (`plan/checks/_generate_columns.py:120`). For each
`formula` with `references`, parse with `ast`. For each `Name` bound to a reference whose
`reader_types` entry is a number (aliases through `reference` columns are resolved by
`reader_types`), refuse when the name is:

- an operand of `+` whose other operand is a `str` constant or an f-string;
- the receiver of an attribute (`a.zfill(5)`, `a.upper()`);
- compared with `==`, `!=`, `in` or `not in` against a `str` constant, or against a list, tuple
  or set literal that holds one.

Refusal: `PlanCompileError` with `plan.generate_formula_number_used_as_text`, path
`tables.<t>.generate_columns.<c>.formula`. The message names the table, the column and the
reference, and says to wrap the reference in `str()`; it never quotes the formula. Accepted:
`str(a)`, `f'{a}'`, `a == 1`, `a in (1, 2)`, `str(a).zfill(5)`, and every spelling on a text
column. A name rebound inside a comprehension is not followed (recorded in 13).

### 4.5 CLI (C1)

`src/decoy/cli/run/_errors.py:221`: the `GenerationError` arm that maps `uniqueness_impossible`
to `usage` takes a two-code set, `{uniqueness_impossible, generate_formula_evaluation_failed}`.
`decoy explain exit-codes` names the new code under exit 1. The compile refusal already reaches
exit 1 as a `PlanCompileError`. Docs are listed in 8.

## 5. Behaviour

| Input | Today | After E1 (and C1) |
|---|---|---|
| plain sequence `a`, `round(a / 3)`, 5 rows | all null, exit 0 | `[0, 1, 1, 1, 2]`; `a` is `int64` |
| same, `a * 2` | `'11'`, `'22'` | `2`, `4` |
| same, `a == 1` | always `False` | `True` on row 0 |
| same, `'ORD-' + a` or `a.zfill(5)` | text works | refused at compile, `generate_formula_number_used_as_text`, exit 1 |
| same, `a == '1'` or `a in ('1', '2')` | text compare works | refused at compile, same code |
| same, `'ORD-' + str(a)` or `f'ORD-{a}'` | `ORD-1` | `ORD-1` (unchanged) |
| sequence with `null_probability`, `'ORD-' + str(a)` | `ORD-` on a null row | null on a null row (rule 1) |
| categorical `[1, 2, 3]` with nulls, `str(cat)` | `'1.0'`, and `''` on a null row | `'1'`, and null on a null row |
| text column with nulls, `int(code)` | null on a null row | null on a null row (rule 2, unchanged) |
| a formula raises on a row with no missing reference (`1 / (a - 3)`) | null, warning, exit 0 | run fails with `generate_formula_evaluation_failed`; CLI exit 1, no output file |
| inline formula raises (`randint(5, 1)`) | all null | run fails, same code |
| sequence read by `derived: a + 1` and a formula | uncoded `ValueError`, exit 3 | runs; both numeric |
| prefixed sequence or text faker read by a formula | text | text (unchanged) |
| sequence read by a formula and a `group_by` | text | text (the `group_by` reader still demotes); arithmetic in the formula now fails loud with the code instead of writing nulls |
| CSV bytes of the read column | `1` | `1` (unchanged) |
| Parquet or Arrow type of the read column | `string` | `int64` or `float64` |

## 6. Failure modes

### 6.1 Class table

| # | Class | Structure that closes it | Proof | Mutation that turns it red |
|---|---|---|---|---|
| 1 | Value-bearing output | the failure text is built from the column name, row index and the exact-type reason word after the `except` closes; the compile refusal names the reference, never the formula text | AT-5, AT-8 (no cell value, `__cause__` and `__context__` are `None`, no formula text) | put `{exc}` in the message; raise inside the `except` |
| 2 | Cleanup missing on an exit path | N/A for new state: E1 adds no file, lock or scratch; the formula post-pass runs before any write on the resident route, and C1 proves no output file exists | AT-C1-2 | write the table before the post-pass |
| 3 | State and concurrency | N/A: no shared or module state is added; per-row seeding is unchanged and every row reseeds, so a skipped row moves no other draw | AT-4 (non-null rows equal the run without nulls) | reuse one RNG across rows without reseeding |
| 4 | Route parity and silent fallback | formula runs only on the legacy whole-table route (`generation/_admission.py:56` declines it from the streamed route; pinned plans reject it), so one post-pass covers every route; no failure becomes a null without a config null | AT-5, AT-6, AT-C1-2 | restore `values.append(None)` at `generators/_formula.py:219` |
| 5 | Referential integrity | a `reference` column copies its root's type; `reader_types` resolves the alias through `_reference_root`, so a formula over an FK child sees the parent's type; relationship pairs keep their existing `numeric_plan` check | AT-3 reference cell, AT-8 alias cell | resolve the reference name without `_reference_root` |
| 6 | Untrusted producer | the formula text is user config; the check reads it with `ast.parse` only (the config layer already refused unparsable text) and evaluation stays in `safe_eval` | AT-8 | evaluate the formula inside the check |
| 7 | Check then use (TOCTOU) | the compile check and the run read the same `reader_types` answer from the same config; pinned and saved plans reject formula, so no later recompute can differ | AT-8 and AT-1 on one config | derive the run binding from the values instead of the plan |
| 8 | Weak tests and overclaimed evidence | every AT is red on `8c9c1162` except the named controls; AT-3 compares the formula output's Arrow type with the source column's, not only "not null" | reviewers rerun the named mutations on the final head | relax AT-3 to values only |
| 9 | Behaviour-preserving refactor drift | `reader_types` and `numeric_plan` share one inner type function; `failure_reason` is the derived table behind a public name and derived reads it too | `tests/unit/transforms/test_derived.py` unchanged and green; numeric typing tests | give `reader_types` its own copy of the kind table |
| 10 | Frozen contract drift | the output change is listed in 5 and the compat note; two reason codes `since=UNRELEASED` with one fragment; parity refreeze limited to the fixtures in 9; no `SEED_PROTOCOL_VERSION`, exit map or `--json` shape change in E1 | AT-11, reason registry tests, golden suites | change a third parity fixture |

### 6.2 Enumerations

#### Exit paths

| Surface | Where it fails | Result |
|---|---|---|
| `decoy validate`, `ci check` gate | `check_generate_columns` (`plan/checks/_generate_columns.py:120`) | exit 1, `generate_formula_number_used_as_text` |
| `decoy run` gate | the same compile pass | exit 1 before any read |
| `decoy run`, a formula raises | `generators/_formula.py:219` and `:282` become the coded raise | exit 3 after E1, exit 1 after C1 (`src/decoy/cli/run/_errors.py:221`) |
| library `generate_tables` | the same | raises `GenerationError` |

#### Routes

| Route | Formula runs | Change |
|---|---|---|
| legacy whole-table (`generation/synthesize.py:208`) | yes | all of E1 |
| streamed and windowed | no, declined at `generation/_admission.py:56` | none; a plain sequence read only by `derived` stays as today |
| pinned | no, rejected (`generation/_pinned_compile.py:341` records numeric types for the rest) | recorded types change only for configs with a formula, which pinned refuses |
| mask-mode `formula` | a separate transform | out of scope |

#### FK shapes

- Relationship pair (parent key, child FK): both ends keep one planned type
  (`generation/_numeric_typing.py:192`); a sequence parent read by a formula now stays `int64` on
  both ends, which `_pairs_agree` already checks.
- `reference` column (FK child copying its parent): a formula reading it sees the root type
  through `_reference_root` (`generation/_numeric_typing.py:177`).
- Composite, self-referencing and cross-table keys: no key, FK or vault path changes; formula is
  never a key generator, so these shapes see only the type change above.

#### Fields

| Field | Change |
|---|---|
| `formula` (with `references`) | sees generated types; text use refused at compile (new `plan/checks/_formula_types.py`, called at `plan/checks/_generate_columns.py:120`) |
| `formula` (inline) | a raise fails the run (`generators/_formula.py:282`) |
| `references` | no change in meaning |
| `null_probability` on a referenced column | a missing number cell gives a null result (`generators/_formula.py:206`) |
| `sequence`, `faker` read by a formula | keep their numeric type (`generation/_numeric_typing.py:124`) |

#### Output surfaces

- Generated table values and Arrow types: as in 5 (`generation/_referenced_formula.py:58`).
- Run refusals on stderr and in the `--json` error envelope: two new codes.
- Logs: the two existing warnings, counts and names only.
- Evidence, manifests, the run result shape: unchanged.

## 7. Acceptance tests, red first

Each test is written and seen red on `8c9c1162` before the fix, except the controls. Synthetic
values only. Engine tests run `compile_plan`, then `generate_tables`.

| Id | Test | Red on main | Mutation | Place |
|---|---|---|---|---|
| AT-1 | add `("round(a / 3)", [0, 1, 1, 1, 2])` back to `test_scoped_shapes_are_accepted_and_generate_values` | yes, all null | set `formula.references` back to unsafe | `tests/unit/plan/checks/test_generate_columns.py:531` |
| AT-2 | sequence `a` read by `a * 2` and by `a == 1`: `[2, 4, ...]` and `[True, False, ...]`; `a`'s Arrow type is `int64` | yes | the same | `tests/unit/generators/test_formula_types.py` (new) |
| AT-3 | type fidelity over the registry: one example config per value-producing kind in `READER_KEYS` (plain sequence, `pyint`, `pyfloat`, categorical int, float and text, constant int, distribution, grouped_series, reference to a sequence parent), each with `null_probability` 0 and 0.5; a formula `col` with `references: [col]` has the source's Arrow type and equal non-null values. A kind in `READER_KEYS` with no example and no listed exemption fails the test | yes (sequence, faker, categorical int with nulls) | drop `dtype=object` at `_referenced_formula.py:58`; flip the reader back | the same file |
| AT-4 | nullable sequence, `'ORD-' + str(a)`: non-null rows `ORD-<n>`, null rows null, non-null rows equal the run without nulls | yes (`ORD-` today) | bind `""` for number columns | the same file |
| AT-5 | `1 / (a - 3)`: `GenerationError` with `generate_formula_evaluation_failed`; the message has the column, `row 2` and `division by zero`, and no cell value; `__cause__` and `__context__` are `None` | yes (null) | restore `values.append(None)` at `:219` | the same file |
| AT-6 | inline `randint(5, 1)` raises the same code | yes | restore `values.append(None)` at `:282` | the same file |
| AT-7 (control) | text categorical `["1", "2"]` with nulls, `int(code)`: null rows null, the others ints, no raise | no | remove rule 2's null branch | the same file |
| AT-8 | seeded spellings refused at both sites with `generate_formula_number_used_as_text` and the formula path: `'ORD-' + a`, `a + 'x'`, `f'x' + a`, `a.zfill(5)`, `a.upper()`, `a == '1'`, `'1' != a`, `a in ('1', '2')`, `a not in ['x']`, and `'ORD-' + r` where `r` is a `reference` to a sequence parent; no formula text in the message. Controls accepted: `'ORD-' + str(a)`, `f'ORD-{a}'`, `a == 1`, `a in (1, 2)`, `str(a).zfill(5)`, and every refused spelling on a prefixed sequence | yes (accepted today) | drop the compare branch; drop alias resolution | `tests/unit/plan/checks/test_formula_types.py` (new) |
| AT-9 | sequence read by `derived: a + 1` and by formula `str(a)`: runs, the derived column is `int64` | yes (`ValueError`) | flip the reader back | `tests/unit/generators/test_formula_types.py` |
| AT-10 | #445 pins updated: `tests/parity/test_numeric_typing_readers.py:41` cases one and two become AT-8 refusals and case three stays; the `:162` formula row expects `True`; the nullable-sequence test keeps "no `.0`" and accepts null | yes (they pin the old text) | revert the reader | `tests/parity/test_numeric_typing_readers.py` |
| AT-11 (control) | parity: only the two fixtures named in 9 change, and only in the named columns | no | change any other fixture | `tests/parity/` freeze tests |

CLI (slice C1, after the engine release and repin):

| Id | Test | Mutation |
|---|---|---|
| AT-C1-1 | `decoy run` on a sequence plus `round(a / 3)`: exit 0, CSV column `0,1,1,1,2` | pin the old engine |
| AT-C1-2 | `decoy run` on `1 / (a - 3)`: exit 1; stderr and `--json` carry `generate_formula_evaluation_failed` and `error_kind` usage; no cell value; no output file | drop the code from the usage set |
| AT-C1-3 | `decoy validate` and `decoy run` on `'ORD-' + a`: exit 1, `generate_formula_number_used_as_text`, `run` writes nothing | none (engine refusal; proves the pin) |
| AT-C1-4 | `decoy explain exit-codes` names `generate_formula_evaluation_failed` under exit 1 | remove the line |

Place: `tests/e2e/generate_config/test_formula_types_e2e.py` (new; the CLI CODEMAP puts
generate-column e2e tests in `tests/e2e/generate_config/`).

## 7a. Reuse inventory

| Need | Reused | Not built |
|---|---|---|
| which columns are numbers | `numeric_plan` and its inner generated-type function | no second type table |
| reader classification | `READERS` and `tests/sentry/test_numeric_reader_registry.py` | no new registry |
| failure reason words | the `transforms/derived.py:312` table behind `failure_reason` | no new word list |
| per-row failure text shape | `evaluation_failure_text` (`transforms/derived.py:322`) | |
| missing-cell rule | `is_null_cell` from #564, or today's check until it lands | no new predicate |
| run error class | `GenerationError` (`generation/pool/_errors.py:17`) | no new exception class |
| compile refusal helper | `_refusal` in `plan/checks/_generate_columns.py` (imported, not copied) | |
| CLI usage mapping | the `uniqueness_impossible` arm | no new CLI category |

## 8. Placement

Engine `execution/` and the CLI flat `src/decoy/cli/` get no new files.

| Change | File | CODEMAP row |
|---|---|---|
| reader entry, `reader_types` | `src/decoy_engine/generation/_numeric_typing.py` (347 lines) | `generation/` |
| object frame, number set | `src/decoy_engine/generation/_referenced_formula.py` | `generation/` (the cross-column formula post-pass) |
| number set computed | `src/decoy_engine/generation/synthesize.py` | `generation/synthesize.py` |
| row rule, coded raise | `src/decoy_engine/generators/_formula.py` (366 lines) | `generators/` |
| `failure_reason` | `src/decoy_engine/transforms/derived.py` | `transforms/` |
| text-use check | `src/decoy_engine/plan/checks/_formula_types.py` (new, beside `_lookup_tables.py`; the package has 4 modules) | `plan/` row, `checks/`; add the file to that row |
| call site | `src/decoy_engine/plan/checks/_generate_columns.py` (517 lines) | the same |
| reason codes | `src/decoy_engine/reasons/runtime/_part2.py`, `src/decoy_engine/reasons/plan/_part3.py`, `since=UNRELEASED` | reasons |
| engine tests | `tests/unit/generators/test_formula_types.py` (new), `tests/unit/plan/checks/test_formula_types.py` (new), the edits named in 7 | beside the owning packages |
| fragment | `changelog.d/gen-formula-seq.md` with a `Breaking:` line and both identities | |
| docs | `docs/strategies.md` formula section (types, row rule, both codes); `docs/compatibility-contract.md` 3.3 dated paragraph; refreeze record `docs/records/2026-11-13-formula-typing-refreeze.md` | |
| CLI mapping | `src/decoy/cli/run/_errors.py` | `run/` package |
| CLI explain | `src/decoy/cli/explain.py` (existing module, text only) | existing flat module, no new file |
| CLI e2e | `tests/e2e/generate_config/test_formula_types_e2e.py` (new) | generate-config e2e row |
| CLI docs | `docs/guides/shape-and-privacy/03-fit-and-generate.md` (one paragraph on formula types), `CHANGELOG.md`, `docs/ROADMAP.md` row 24y, `docs/RECENTLY-SHIPPED.md` | |

Design notes: no new structure beyond one check module, placed beside the other plan checks. One
fact, one place: a column's type lives in `numeric_plan`; the compile check reads it through
`reader_types`.

## 9. Frozen contracts touched

- **Generated output (compatibility contract 3.3).** Pre-GA one-time break with no flag, for the
  rows in 5. A dated 3.3 paragraph. Fragment `Breaking:` line: "A generate `formula` now reads a
  plain `sequence`, an int or float `faker` column and a numeric column with nulls as numbers.
  Text use (`'ID-' + id`, `id.zfill(5)`, `id == '1'`) is refused at compile; write `str(id)`. A
  formula that raises fails the run with `generate_formula_evaluation_failed` instead of writing
  nulls. A null number reference gives a null result. A column a formula reads keeps its numeric
  Parquet type."
- **Parity freeze.** `tests/parity/_f0_fixtures.py:314` `bad: undefined_name + 1` now raises.
  GEN-STRICT already refuses it at compile, so the column leaves the frozen fixture with the
  record. `tests/parity/_f_audit_cells.py:246` (`cfg_formula_referenced`): `seq` changes from
  `string` to `int64`; the `rfm` text is byte-identical. The builder runs the freeze and lists
  every diff; any other diff is a stop (11).
- **Reason codes.** Added: `runtime.generate_formula_evaluation_failed` and
  `plan.generate_formula_number_used_as_text`; the registry is stamped at release (#506). None
  renamed.
- **Exit codes.** C1 adds one code to the existing exit-1 arm; the map does not change.
- **`SEED_PROTOCOL_VERSION`.** Unchanged; no draw moves.

## 10. Slices

| Slice | Content | Risk | Size | Depends on | Tests at T1 |
|---|---|---|---|---|---|
| E1 | 4.1 to 4.4, codes, docs, compat note, refreeze record, fragment | R2 | M (about 250 source and 300 test lines) | none | AT-1 to AT-11, `tests/unit/generators/`, `tests/unit/plan/checks/`, `tests/parity/test_numeric_typing_readers.py`, `tests/sentry/test_numeric_reader_registry.py`, reason registry tests, golden and parity freeze suites |
| C1 | 4.5 mapping, explain text, e2e, CLI docs, roadmap row | R1 | S | E1 merged, in a daily engine release, CLI repinned (repin checklist) | AT-C1-1 to AT-C1-4, `tests/e2e/first_user/test_items_run_unique.py` |

E1 changes pinned tests and parity fixtures (moved or changed tests, a frozen contract), so its
merge head gets the full engine suite against the saved main baseline. Gate: Dennis and Codex
(R2). C1: Codex only.

## 11. Stop lines for builders

1. Do not change any other `READERS` entry, `numeric_plan`'s fixed point, or a relationship rule.
2. Do not add a second type table or a second missing-cell predicate.
3. A parity or golden diff outside the two fixtures in 9 is a stop.
4. No raised module-size ceiling, sentry baseline, allowlist or pin.
5. If a second formula evaluator turns up on another route, stop and report it.
6. No test runs on the devbox: Mac first, then `~/bin/vm-run`.

## 12. Owner questions

- **OQ-1. Text idioms on a plain sequence.** #445 kept `'ORD-' + id` and `id.zfill(5)` working
  by keeping the column text. Options: (a) the formula sees the number, and those spellings are
  refused at compile with a hint to use `str(id)` (this plan); (b) judge each formula per use, as
  `derived` is judged, and keep the column text when any formula uses it as text.
  **Recommended: (a).** Under (b) one formula's spelling still changes the type that every other
  reader and the output see, which is the defect itself. Under (a) the failure is at `validate`,
  and `str(id)` and f-strings keep working.

## 13. V2-LOG and follow-ups

- The other text-demoting readers (`group_by`, `condition_on`, `anchor`, `derived_aggregate`)
  still turn a numeric column into text for every reader. After E1 the formula case fails loud
  instead of silently. The fix is a text view at those readers instead of demoting the column.
  Below the V1 bar (no silent output); log for the generation QA pass.
- A generate-mode `derived` failure raises an uncoded `ValueError` (mask mode codes it as
  `derived_evaluation_failed`). Log for the generation QA pass.
- The compile check does not follow a name rebound in a comprehension.
- Truthiness changes for a sequence that starts at 0 (`x if a else y`: `'0'` was true, `0` is
  false). Named in the `Breaking:` line; no check.

## 14. Pre-mortem

- **A user config uses text idioms.** The compile refusal names the column and the fix; the
  `Breaking:` line names the spellings.
- **The refreeze hides a real change.** AT-11 and stop line 3 limit it to two named fixtures.
- **GEN-INHERIT lands first and moves the type function.** E1 rebases its wrapper onto
  `config.generated_arrow_type`, with no copy (1.3).
- **#564 lands first.** E1 imports `is_null_cell` at the same line.
- **A formula that worked by luck on text now fails at run.** That is the intended loud failure,
  and after C1 it is exit 1 with the code.

## 15. Verification status

The root cause and the 1.1 table come from a repro on engine `8c9c1162`, including a spike that
flipped only the reader entry: `round(a / 3)` then gave `[0, 1, 1, 1, 2, 2]` for 6 rows, and with
`null_probability: 0.5`, `str(a)` gave `'4.0'`, which is why 4.2 is required. No test suite ran.
