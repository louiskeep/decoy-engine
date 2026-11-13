Status: plan

# GEN-FORMULA-SEQ: a generate formula reads each column with its own type (roadmap row 24y)

Rules consulted: `/home/cam/dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`,
`development-loop.md`, `bugfix.md`, `feature-dev.md`, `architecture.md`, `reuse-first.md`,
`cross-cutting-concerns.md`, `api-and-compatibility.md`, `security.md`, `testing.md`,
`scope-discipline.md`, `verification.md`, `documentation.md`, `profiles/local-operations.md`,
`catalogs/recurring-defects.md`; engine `CODEMAP.md`, `.importlinter`, `docs/reasons.md`,
`docs/compatibility-contract.md` (3.3); CLI `docs/ROADMAP.md` (rows 24j, 24k, 24y, 24zd),
CLI `CODEMAP.md`.

Revision 3, after Dennis r2 (V1: 1 HIGH, 3 MEDIUM; 6 LOW). Every finding is taken; the
round-by-round change list is in 16.

Bases (2026-11-13): engine `origin/main` `3a30eb89` (includes #564 `is_null_cell`), CLI
`origin/main` `a15afcc` (pins `decoy-engine==0.23.0`). Line numbers are read at these bases.

Owner decisions used: the V1 bar (2026-11-02: a product-function break or data corruption blocks
V1; silent wrong output is corruption); row 24b QA-GATE (2026-11-08: a config error fails with
exit 1 and a code, never exit 3); "names may show in messages; data values never" (2026-11-06);
pre-release, no product decision is locked, so output may change with a `Breaking:` line.

Risk: E1 is R2 (generated output changes, two new reason codes, a recorded parity refreeze).
C1 is R1 (one exit-code mapping, tests and docs).

## 1. Frame

### 1.1 Evidence

A repro on engine `8c9c1162` (Mac node and decoy-test-5 through `vm-run`; `compile_plan`, then
`generate_tables`; 3 to 6 rows; seed 1). The files it touched have not changed in a way that
matters since: between `8c9c1162` and `3a30eb89` only #564 (the missing-cell predicate) and #563
(statistical snapshots) touched them.

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
   The same text binding happens when a different unsafe reader (`group_key.group_by`,
   `grouped_series.group_by`, `derived_aggregate.column`, `statistical.condition_on`,
   `windowed_date.anchor`) demotes the column: the formula gets the text cells
   (`generation/_numeric_typing.py:260-277` returns `pa.string()` for a dropped candidate).
2. **The pandas hop and the swallowed error.** The post-pass builds `pd.DataFrame(data)`
   (`generation/_referenced_formula.py:58`), which turns an int list holding `None` into
   `float64`, so a formula sees `1.0` for `1`. Then every per-row exception becomes `None` with
   one log warning (`generators/_formula.py:220` for referenced formulas, `:283` inline).
   Nothing coded reaches the run result or the CLI.

Kinds checked:

| Generator kind | Type a formula sees today | Same root cause |
|---|---|---|
| `sequence` (plain, fits int64) | `str` | yes, reader typing |
| `faker` int providers (`pyint`, `random_int`, `random_digit*`, `random_number`) | `str` | yes, reader typing |
| `faker` float-annotated providers (`pyfloat`) | `str` | yes, reader typing |
| any of the three above, also read by another unsafe reader | `str` | yes, reader typing (the formula binding follows the output type) |
| `reference` to one of the above | `str` (the reference group is demoted together) | yes, reader typing |
| `categorical` with int or float categories, `constant`, `distribution` | the number, but `float` for an int column that holds a null | yes, the pandas hop |
| `grouped_series` | a number on a non-null row; `""` on a null row because `_generated_type` has no branch for this kind | only the non-null cell is in scope; the nullable case is V2-LOG (13) |
| `sequence` with a prefix, suffix or pad; text faker; unproven range | `str` | no: the column is not a candidate, so text is its own type |
| `faker` date providers, `windowed_date` | ISO `str` | no: dates are text in generate output on every route; out of scope |
| `statistical`, `lookup`, `derived`, `derived_aggregate`, `group_key`, `formula` | unchanged | the binding stays as today (4.3 rule 2) |

### 1.3 Interactions

- **GEN-INHERIT** (remote PRs #523, #546, #555, #557, #561; open; not depended on).
  - #555 deletes `READER_KEYS` and moves reader discovery to `config.iter_generate_input_refs`.
  - #557 moves `_reference_root` to `config.reference_root` and the body of the inner
    `generated()` to `config.generated_arrow_type`.
  - #561 adds a `categorical.condition_on` reader.
  - E1 touches three things in `_numeric_typing.py`: the one `formula.references` entry, a
    shared `_base_types` helper (the pre-fixed-point `base` map, which neither PR moves), and a
    shared `_generated_type` helper. Rebase rules:
    - #555 first: E1 flips the same entry wherever `READERS` lives; AT-3 is defined over the
      `GenerateColumnConfig.type` Literal (`config/_tables.py:274`), so no test name moves.
    - #557 first: `_generated_type` becomes a call to `config.generated_arrow_type`, given the
      full `base` map for `reader_types` and the candidate map for `numeric_plan`; alias
      resolution uses `config.reference_root`. No copy of either body.
    - E1 first: #555 and #557 carry the one-entry flip and the two helpers when they move the
      code. The class-5 mutation in 6.1 is named by behaviour, not by function name.
  - The new GEN-INHERIT readers (`anchor_via`, `via`, `categorical.condition_on`) are not formula
    readers. Under HIGH-1's rule (4.1) they no longer change what a formula sees.
- **DUP-DEFECTS E2** (#564, merged as `de22841d`): `is_null_cell` is the one missing-cell rule
  and `generators/_formula.py:207` already calls it. E1 reuses it and adds no second check.
- **Engine #465 (GEN-STRICT E2)** dropped `round(a / 3)` from the generating case list
  (`tests/unit/plan/checks/test_generate_columns.py:531` keeps only `i + index`) because it
  came back as nulls. AT-1 puts it back.

## 2. Guarantee

After E1:

1. A `formula` sees each referenced cell with the column's candidate type: `numeric_plan`'s
   pre-fixed-point answer for `sequence` and `faker` (through the reference root), the native
   value for the other kinds. This holds whatever other readers the column has. Never a text
   copy of a number, never a float copy of an int.
2. Adding a formula reader never changes the type of the column it reads, in the output or for
   another reader. A column that another reader demotes to text stays text in the output.
3. A formula that raises on a row fails the run with `runtime.generate_formula_evaluation_failed`
   (a `GenerationError`). Evaluation stops at the first failing row. The message names the
   table, the column, the row index and a fixed reason word. It holds no cell value, and no
   exception is chained.
4. The only nulls a formula writes come from the config: `null_probability` on the formula, or a
   missing referenced cell (rules in 4.3).
5. A formula that uses a number reference as text in a way that silently changes meaning (a
   compare with a text literal or a declared string reference) or always fails (concatenation
   with text, a text method, indexing or slicing, `len`, a `join` over it, or membership against
   a declared string reference) is refused at compile with
   `plan.generate_formula_number_used_as_text`, in `decoy validate` and in `decoy run`. The
   check judges on the same candidate type as point 1, so co-readers do not change the verdict.

After C1: `decoy run` exits 1 for `generate_formula_evaluation_failed` and writes no output; the
docs say how a formula sees types and how to rewrite the refused spellings.

Non-goals: mask-mode `formula` (`transforms/formula.py`, reads source cells); date typing; the
output type of a column demoted by another unsafe reader (see 13); the uncoded `ValueError` from
a generate-mode `derived` failure (see 13).

## 3. Established solutions

Search run: a web search for pandas int-with-NaN upcasting and the nullable and object dtypes
(pandas user guide, "Nullable integer data type",
https://pandas.pydata.org/docs/user_guide/integer_na.html), plus the Python references below and
the engine's own registry and expression code.

| Problem | Established answer | Source | Used how |
|---|---|---|---|
| Mixed str and int operands | Python raises `TypeError` for `'a' + 1` and never coerces; `str * int` repeats | Python language reference, "Binary arithmetic operations" | the formula sees the real type; text use is spelled `str(a)` or an f-string |
| Errors passing silently | "Errors should never pass silently. Unless explicitly silenced." | PEP 20 | a failing row fails the run; only config-requested nulls are quiet |
| int column with a missing value | "Because NaN is a float, this forces an array of integers with any missing values to become floating point"; the nullable dtype must be asked for | pandas user guide, "Nullable integer data type" | the post-pass frame uses `dtype=object` columns, which keep the Python `int` and `None` the generator made |
| Null in arithmetic | an operator with a NULL operand yields NULL | ISO SQL null semantics | a missing number reference gives a null result (4.3 rule 1) |
| Value-free errors | build the message from closed text and raise after the handler | PEP 3134; the exact-type reason table `transforms/derived.py:309` | one table and one text builder for derived and formula |
| Name scope in an expression | the compiler's own scope analysis (PEP 227) | stdlib `symtable`, already used by `_unbound_reads` (`plan/checks/_generate_columns.py:372-389`) | the text-use check treats a name as the reference only where it is a global read |

The registry (`READERS`), the cast (`restore_declared`, `generation/_numeric_typing.py:324`) and
the parsed formula tree (`_check_formula`, `plan/checks/_generate_columns.py:417`) already exist.
E1 adds no new mechanism beyond one check module.

## 4. Design

### 4.1 Reader typing (root cause 1)

- `generation/_numeric_typing.py:124`: `formula.references` becomes
  `Reader(True, "formula locals are the candidate values; text use is refused at compile")`, and
  the module docstring bullet changes to match. With no other unsafe reader the column now stays
  numeric in the output too.
- Two private helpers come out of `numeric_plan` with no change in its result:
  `_base_types(config, provider_snapshot)` (today's `base` loop, `:213-230`) and
  `_generated_type(cols, root, numeric)` (today's inner `generated()` body, `:260-277`).
  `numeric_plan` calls `_generated_type` with the candidate map, as today.
- New public `reader_types(config, *, provider_snapshot=None) -> dict[Key, pa.DataType]`,
  re-exported by `decoy_engine.generation`: for
  every generated column, `_generated_type` of its reference root given the full `base` map.
  So a `sequence` or `faker` candidate reads as `int64` or `float64` even when the fixed point
  dropped it. There is no second type table.

### 4.2 The post-pass frame (root cause 2a)

All of this lives in `generation/_referenced_formula.py` (75 lines). `synthesize.py` is at 592 of
600 lines, so it only computes `reader_types` once beside `numeric` (`generation/synthesize.py:140`,
one line) and passes the map to `fill_referenced_formula_columns` as one keyword (the call at
`:208`). Net growth at most 2 lines.

- The frame is built with `pd.Series(values, dtype=object)` columns, and each filled formula
  column is written back the same way.
- The post-pass finds the referenced columns of this table whose `reader_types` entry is int or
  float (the number set). For each one it builds a formula-scope copy of the values with
  `restore_declared(root_col, values, dtype)`, where `root_col` is the reference root's column
  config (so a `reference` alias is cast by its parent's rule). That is the one cast the
  generator already uses; it parses text cells only and leaves ints, floats and `None` alone, so
  it is a no-op on a column that stayed numeric. A candidate's values provably fit, so `int(str)`
  and `float(repr(float))` round-trip exactly. `data` (the output) is not touched: a column
  demoted by another reader stays text.
- The number set goes to `fill_referenced_formula_column` as a new keyword
  `number_refs: frozenset[str] = frozenset()`. The default is empty, so direct callers
  (`tests/unit/generators/test_formula_missing.py:17`) keep rule 2. The map keyword on
  `fill_referenced_formula_columns` also defaults to empty.

### 4.3 Row rule (root cause 2b), in `generators/_formula.py`

For each row, using `is_null_cell` (`generators/_formula.py:207`):

1. A referenced cell in `number_refs` is missing: the output cell is null and the formula does
   not run for that row (null in, null out). Counted per column.
2. A referenced cell of any other column is missing: `""` is bound, as today
   (`generators/_formula.py:209`). If the formula then raises, the cell is null and counted,
   because the config asked for nulls in that column.
3. Otherwise a raise fails the run. On the first failing row, save the reason word and the row
   index, stop evaluating (no later row runs), leave the `except` block, then raise
   `GenerationError(code=GENERATE_FORMULA_EVALUATION_FAILED, message=...)`.
4. The inline path (`_eval_formula_inline`, no references) applies rule 3.

The text comes from one builder in `transforms/derived.py`, beside the reason table:
`failure_text(subject, *, column, row_index, reason, table=None)`. Derived calls it with
`subject="derived expression"` and no table, which keeps its text byte-identical
(`derived expression evaluation failed at column 'c', row 0: <reason>`). Formula calls it with
`subject="formula column"` and the table:
`formula column evaluation failed at table 't', column 'x', row 2: division by zero`. The reason
word comes from the exact-type table through `failure_reason(exc)`. Those two names are the only
public ones; `evaluation_failure_text` is renamed to `failure_text` (one caller,
`transforms/derived.py:302`, and one test import, `tests/unit/transforms/test_derived.py:738-740`).

Logs: the "substituted '' for null cells" warning stays. The "failed to evaluate" warning now
counts only rule-2 rows (counts and names, no values). `_undefined_name` and `missing_names` are
removed: GEN-STRICT E2 refuses undefined names at compile, and a lenient run that bypasses it now
fails under rule 3 with the default reason word.

Per-row seeding is unchanged and every row reseeds, so a row skipped under rule 1 moves no other
row's draws.

### 4.4 Compile check: a number used as text (construction guard)

The class is "a reader sees a value type other than the column's type". The type side is closed
by construction: one owner of the candidate type (`_base_types` behind `numeric_plan` and
`reader_types`) and the closed `READERS` registry with its sentry. The formula side gets a
compile check, because the old text spellings are the realistic bypass.

New `plan/checks/_formula_types.py` with `check_formula_text_use(table, col, tree, declared,
types)`. It lazily imports `reader_types` from the public `decoy_engine.generation` facade, never
from `generation._numeric_typing`. Runtime code and tests take `GenerationError` from the public
`decoy_engine.generation.pool` facade, never `generation.pool._errors`. `check_generate_columns`
computes `types = reader_types(config, provider_snapshot=provider_snapshot)` once and passes it
down through `_check_column` to `_check_formula`, which calls the new check after its existing
checks, with the `tree` it already parsed (`plan/checks/_generate_columns.py:417`) and its
`declared` list. No second parse.

Scope: a name is treated as the reference only when it is a declared reference, its `types` entry
is int or float, and it is a global read. The walk does not descend into a `Lambda`, `ListComp`,
`SetComp`, `DictComp` or `GeneratorExp` for a name that node binds (its targets or arguments), the
same scope rule `_unbound_reads` gets from `symtable`. So a comprehension variable with the same
spelling is never refused; a text use through such a variable is accepted and, if it fails, fails
at run with the code.

Refused shapes (the accidental spellings, catalog entry 5):

- an operand of `+` whose other operand is a `str` constant or an f-string;
- the receiver of an attribute (`a.zfill(5)`, `a.upper()`);
- the value of a subscript (`a[0]`, `a[-4:]`);
- the argument of `len` (`len(a)`);
- an element of a list or tuple literal passed to `<str>.join` (`'-'.join([a, b])`);
- compared with `==` or `!=`, or tested with `in` or `not in`, against a `str` constant, a list,
  tuple or set literal that holds one, or another declared reference whose `types` entry is
  `string`. A number-to-number reference comparison remains valid.

Refusal: `PlanCompileError` with `plan.generate_formula_number_used_as_text`, summary `A formula
uses a number reference as text.`, and path `tables.<t>.generate_columns.<c>.formula`. Extend the
existing `_refusal` through `_located`; do not add a second error builder. `_refusal` accepts an
optional reference name for `where` and an optional detail key. `_located` resolves that key from
the closed table `{compare: "compare with a number", other: "wrap the reference in str()"}` and
appends ` Hint: <detail>.`; callers cannot supply free detail text. The exact messages pinned by
AT-8 are:

- compare: `A formula uses a number reference as text. At table 't', column 'f', option
  'formula', reference 'a'. Hint: compare with a number.`
- other: `A formula uses a number reference as text. At table 't', column 'f', option 'formula',
  reference 'a'. Hint: wrap the reference in str().`

Both name structure only and never include formula text or a data value. Accepted: `str(a)`,
`f'{a}'`, `a == 1`, `a in (1, 2)`, `a == b` when both declared references are numeric,
`str(a).zfill(5)`, `str(a)[-4:]`, `len(str(a))`, and every spelling on a text column. The reversed
membership spelling `'1' in a` has the number on the right; it is not a seeded compile refusal and
fails loudly at run with `generate_formula_evaluation_failed`.

Deliberate evasion (`getattr`, a name rebound through a comprehension, `eval`) is V2-LOG by
catalog entry 5.

### 4.5 CLI (C1)

`src/decoy/cli/run/_errors.py:221`: the `GenerationError` arm that maps `uniqueness_impossible`
to `usage` takes a two-code set, `{uniqueness_impossible, generate_formula_evaluation_failed}`.
`UNIQUENESS_IMPOSSIBLE_CODE` stays as its own name for the rows hint (`_errors.py:266-270`).
`decoy explain exit-codes` names the new code under exit 1 (two lines in `explain.py`, 763
lines). The compile refusal already reaches exit 1 as a `PlanCompileError`. No `--json` field is
added. C1 lands in the CLI repin PR, so there is no window where a formula failure exits 3.

## 5. Behaviour

| Input | Today | After E1 (and C1) |
|---|---|---|
| plain sequence `a`, `round(a / 3)`, 5 rows | all null, exit 0 | `[0, 1, 1, 1, 2]`; `a` is `int64` |
| same, `a * 2` | `'11'`, `'22'` | `2`, `4` |
| same, `a == 1` | always `False` | `True` on row 0 |
| same, `'ORD-' + a`, `a.zfill(5)`, `a[-4:]`, `len(a)` | text works | refused at compile, `generate_formula_number_used_as_text`, exit 1 |
| same, `a == '1'`, `a in ('1', '2')`, `a == code`, or `a in code` where `code` is a declared string reference | text compare works or can silently differ | refused at compile, same code, hint "compare with a number" |
| same, `'ORD-' + str(a)` or `f'ORD-{a}'` | `ORD-1` | `ORD-1` (unchanged) |
| sequence with `null_probability`, `'ORD-' + str(a)` | `ORD-` on a null row | null on a null row (rule 1) |
| categorical `[1, 2, 3]` with nulls, `str(cat)` | `'1.0'`, and `''` on a null row | `'1'`, and null on a null row |
| text column with nulls, `int(code)` | null on a null row | null on a null row (rule 2, unchanged) |
| a formula raises on a row with no missing reference (`1 / (a - 3)`) | null, warning, exit 0 | run fails with `generate_formula_evaluation_failed` at the first failing row; CLI exit 1, no output file |
| inline formula raises (`randint(5, 1)`) | all null | run fails, same code |
| sequence read by `derived: a + 1` and a formula | uncoded `ValueError`, exit 3 | runs; both numeric |
| prefixed sequence or text faker read by a formula | text | text (unchanged) |
| plain sequence read by a `group_key.group_by` and by `a * 2` | `'11'`, `a` is `string` | `2`, `4`; `a` stays `string` in the output |
| same co-read column, `'ORD-' + a` | text works | refused at compile |
| `pyfloat` `amount` read by a `derived_aggregate` and by `amount * 2` | text repeated | a float; `amount` stays `string` |
| child `reference` points at a parent sequence read by a formula | child column `string` | child column `int64` on the legacy whole-plan route; the formula makes the whole plan ineligible for streaming |
| CSV bytes of the read column | `1` | `1` (unchanged) |
| Parquet or Arrow type of the read column (no other unsafe reader) | `string` | `int64` or `float64` |

## 6. Failure modes

### 6.1 Class table

| # | Class | Structure that closes it | Proof | Mutation that turns it red |
|---|---|---|---|---|
| 1 | Value-bearing output | the failure text is built by `failure_text` from names, the row index and the exact-type reason word after the `except` closes; the compile refusal names the reference, never the formula text | AT-5b (a sentinel cell value that `int()` would quote is absent from `str(err)`, `err.message`, the log and the CLI output), AT-5 (`__cause__` and `__context__` are `None`), AT-8 | put `{exc}` in the message; raise inside the `except` |
| 2 | Cleanup missing on an exit path | N/A for new state: E1 adds no file, lock or scratch; the formula post-pass runs before any write on the resident route, and C1 proves no output file exists | AT-C1-2 | write the table before the post-pass |
| 3 | State and concurrency | N/A: no shared or module state is added; per-row seeding is unchanged and every row reseeds, so a skipped row moves no other draw | AT-4 (non-null rows equal the run without nulls) | reuse one RNG across rows without reseeding |
| 4 | Route parity and silent fallback | formula runs only on the legacy whole-table route (`generation/_admission.py:56` declines it from the streamed route; pinned plans reject it), so one post-pass covers every route; no failure becomes a null without a config null | AT-5, AT-6, AT-C1-2 | restore `values.append(None)` at `generators/_formula.py:220` |
| 5 | Referential integrity | a `reference` column reads its root's candidate type and is cast by its root's rule; relationship pairs keep their existing `numeric_plan` check | AT-3 reference cell, AT-8 alias cell, AT-12 | resolve the reference name to itself instead of its root |
| 6 | Untrusted producer | the formula text is user config; the check walks the tree `_check_formula` already parsed and evaluation stays in `safe_eval` | AT-8 | evaluate the formula inside the check |
| 7 | Check then use (TOCTOU) | the run binding and rule 1 read `reader_types` from the run's provider snapshot, so the run agrees with itself. The compile check reads the live custom providers, because `compile_plan` calls `check_generate_columns` without a snapshot (`plan/_compile.py:214`, `:535`); the pipeline takes its snapshot from the same registry in the same process. They differ only if a custom provider is added or changed between compile and run (threading the snapshot is in 13). Pinned and saved plans reject formula | AT-8 and AT-1 on one config | derive the run binding from the cell values instead of `reader_types` |
| 8 | Weak tests and overclaimed evidence | every AT is red on `3a30eb89` except the named controls; AT-3 compares Arrow types, not only "not null"; every existing test that changes is listed in 7b with why it is not weaker | reviewers rerun the named mutations on the final head | relax AT-3 to values only |
| 9 | Behaviour-preserving refactor drift | `numeric_plan` and `reader_types` share `_base_types` and `_generated_type`; derived and formula share `failure_reason` and `failure_text`; derived text stays byte-identical | `tests/unit/transforms/test_derived.py` green with one import rename; numeric typing tests; `numeric_plan` results unchanged on every parity config without a formula | give `reader_types` its own copy of the kind table |
| 10 | Frozen contract drift | the output change is listed in 5 and the compat note; two reason codes `since=UNRELEASED` with one fragment; parity changes stay limited to the two fixtures in 9; the `all_basic_types` golden records its before/after cell digests while its config and fingerprint digests stay fixed; the unlisted variant becomes a coded-error assertion; no `SEED_PROTOCOL_VERSION`, exit map or `--json` shape change | AT-11, reason registry tests, golden suites, AT-C1-2 asserts only existing fields | change another parity or golden fixture; add `error_kind` to the usage arm |

### 6.2 Enumerations

#### Exit paths

| Surface | Where it fails | Result |
|---|---|---|
| `decoy validate`, `ci check` gate | `_check_formula` via `check_generate_columns` (`plan/checks/_generate_columns.py:417`) | exit 1, `generate_formula_number_used_as_text` |
| `decoy run` gate | the same compile pass | exit 1 before any read |
| `decoy run`, a formula raises | `generators/_formula.py:220` and `:283` become the coded raise | exit 1 (C1 ships in the repin PR; `src/decoy/cli/run/_errors.py:221`) |
| library `generate_tables` | the same | raises `GenerationError` |

#### Routes

| Route | Formula runs | Change |
|---|---|---|
| legacy whole-table (`generation/synthesize.py:208`) | yes | all of E1 |
| streamed and windowed (`generation/_admission.py:10`, `:56`, `:91-92`) | no | no streamed-route change: eligibility is whole-plan, so any formula sends every table through `_streamed._run_legacy`; a child `reference` to the formula-read parent changes type on that legacy route |
| pinned | no, rejected (`generation/_pinned_compile.py:341` records numeric types for the rest) | recorded types change only for configs with a formula, which pinned refuses |
| mask-mode `formula` | a separate transform | out of scope |

#### FK shapes

- Relationship pair (parent key, child FK): both ends keep one planned type
  (`generation/_numeric_typing.py:192`); a sequence parent read by a formula now stays `int64` on
  both ends, which `_pairs_agree` already checks.
- `reference` column (FK child copying its parent): a formula reading it sees the root's
  candidate type and is cast by the root's rule (`generation/_numeric_typing.py:177`).
- Composite, self-referencing and cross-table keys: no key, FK or vault path changes; formula is
  never a key generator, so these shapes see only the type change above.

#### Fields

| Field | Change |
|---|---|
| `formula` (with `references`) | sees candidate types; text use refused at compile (new `plan/checks/_formula_types.py`, called from `plan/checks/_generate_columns.py:417`) |
| `formula` (inline) | a raise fails the run (`generators/_formula.py:283`) |
| `references` | no change in meaning |
| `null_probability` on a referenced column | a missing number cell gives a null result (`generators/_formula.py:207`) |
| `sequence`, `faker` read by a formula | keep their numeric type unless another unsafe reader demotes them (`generation/_numeric_typing.py:124`) |

#### Output surfaces

- Generated table values and Arrow types: as in 5 (`generation/_referenced_formula.py:58`).
- Run refusals on stderr and in the `--json` error envelope (`payload["error"]`, and
  `payload["reason"]` where the CLI's reason adapter returns one): two new codes, no new field.
- Logs: the substitution warning and the rule-2 failure count, names and counts only.
- Evidence, manifests, the run result shape: unchanged.

## 7. Acceptance tests, red first

Each test is written and seen red on `3a30eb89` before the fix, except the controls. Synthetic
values only. Engine tests run `compile_plan`, then `generate_tables`.

| Id | Test | Red on main | Mutation | Place |
|---|---|---|---|---|
| AT-1 | add `("round(a / 3)", [0, 1, 1, 1, 2])` back to `test_scoped_shapes_are_accepted_and_generate_values` | yes, all null | set `formula.references` back to unsafe | `tests/unit/plan/checks/test_generate_columns.py:531` |
| AT-2 | sequence `a` read by `a * 2` and by `a == 1`: `[2, 4, ...]` and `[True, False, ...]`; `a`'s Arrow type is `int64` | yes | the same | `tests/unit/generators/test_formula_types.py` (new) |
| AT-3 | type fidelity over every value of the `GenerateColumnConfig.type` Literal (`config/_tables.py:274`): one example config per value-producing type (plain sequence, `pyint`, `pyfloat`, categorical int, float and text, constant int, distribution, grouped_series, reference to a sequence parent), each with `null_probability` 0 and 0.5. A formula over `col` with `references: [col]` returns values whose Python type matches the source column's Arrow type, with equal non-null values. The grouped-series non-null cell stays in; only its `null_probability: 0.5` cell is exempt because `_generated_type` has no grouped-series branch, so it is not in `number_refs` and rule 2 binds `""`. A Literal value with no example and not in the listed exemptions (`formula`, `derived`, `derived_aggregate`, `group_key`, `windowed_date`, `statistical`, `lookup`, plus only that grouped-series null cell, each with its reason) fails the test | yes (sequence, faker, categorical int with nulls) | drop `dtype=object` at `_referenced_formula.py:58`; flip the reader back | the same file |
| AT-4 | nullable sequence, `'ORD-' + str(a)`: non-null rows `ORD-<n>`, null rows null, non-null rows equal the run without nulls | yes (`ORD-` today) | bind `""` for number columns | the same file |
| AT-5 | `1 / (a - 3)`: `GenerationError` with `generate_formula_evaluation_failed`; the message has the table, the column, `row 2` and `division by zero`; `__cause__` and `__context__` are `None`; a counting spy on `safe_eval` shows no row after row 2 ran | yes (null) | restore `values.append(None)` at `:220`; continue after the first failure | the same file |
| AT-5b | value-bearing: text categorical `["SENTINEL-X1"]`, no nulls, `int(code)`. `GenerationError` with the code; `SENTINEL-X1` is absent from `str(err)`, `err.message` and the captured log | yes (null) | put `{exc}` in the message | the same file |
| AT-6 | inline `randint(5, 1)` and `random.randint(1, 100)` both raise the same code; the second pins that `random` is the bound random function, not a module or RNG object | yes | restore `values.append(None)` at `:283` | the same file |
| AT-7 (control) | text categorical `["1", "2"]` with nulls, `int(code)`: null rows null, the others ints, no raise | no | remove rule 2's null branch | the same file |
| AT-8 | seeded spellings refused at both sites with `generate_formula_number_used_as_text` and the formula path: `'ORD-' + a`, `a + 'x'`, `f'x' + a`, `a.zfill(5)`, `a.upper()`, `a[0]`, `a[-4:]`, `len(a)`, `'-'.join([a, b])`, `a == '1'`, `'1' != a`, `a in ('1', '2')`, `a not in ['x']`, `a == code`, `a in code`, and `a not in code` where `code` is a declared text-categorical reference, plus `'ORD-' + r` where `r` is a `reference` to a sequence parent. Every compare/membership cell pins exactly `A formula uses a number reference as text. At table 't', column 'f', option 'formula', reference 'a'. Hint: compare with a number.`; every other cell pins exactly `A formula uses a number reference as text. At table 't', column 'f', option 'formula', reference 'a'. Hint: wrap the reference in str().`; no formula text appears. Controls accepted: `'ORD-' + str(a)`, `f'ORD-{a}'`, `a == 1`, `a in (1, 2)`, `a == b` with both `a` and `b` plain sequences, `str(a).zfill(5)`, `str(a)[-4:]`, `len(str(a))`, `len([a for a in ['x']])` (a comprehension variable with the same spelling), and every refused spelling on a prefixed sequence | yes (accepted today) | drop the compare branch; drop the declared-string-reference branch; drop the subscript branch; drop alias resolution; descend into a binding comprehension | `tests/unit/plan/checks/test_formula_types.py` (new) |
| AT-9 | sequence read by `derived: a + 1` and by formula `str(a)`: runs, the derived column is `int64` | yes (`ValueError`) | flip the reader back | `tests/unit/generators/test_formula_types.py` |
| AT-10 | #445 pins updated as listed in 7b | yes (they pin the old text) | revert the reader | `tests/parity/test_numeric_typing_readers.py` |
| AT-11 (control) | parity: only the two parity fixtures named in 9 change, and only in the named columns; golden: only `all_basic_types.cells` is re-pinned, while its `config_json` and `fingerprints` stay fixed, and the unlisted variant is a coded-error assertion rather than a digest pin | no | change any other fixture or digest | parity freeze tests and `tests/unit/generation/unique/test_gen_unique_old_input_bytes.py` |
| AT-12 | co-reader: plain sequence `a` read by a `group_key.group_by` and by `a * 2`: `[2, 4, ...]`, and `a`'s output Arrow type stays `string`; the same config with `'ORD-' + a` is refused at compile. Second cell: `pyfloat` `amount` read by a same-table `derived_aggregate` and by `amount * 2` gives floats equal to twice the output text parsed | yes (`'11'`) | have `reader_types` return the fixed-point type (`numeric_plan`'s answer) | `tests/unit/generators/test_formula_types.py` |

CLI (slice C1, in the repin PR):

| Id | Test | Mutation |
|---|---|---|
| AT-C1-1 | `decoy run` on a sequence plus `round(a / 3)`: exit 0, CSV column `0,1,1,1,2` | pin the old engine |
| AT-C1-2 | `decoy run` on `1 / (a - 3)`: exit 1; stderr and `payload["error"]` carry `generate_formula_evaluation_failed`, and `payload["reason"]["code"]` does too when the reason adapter returns one; no output file. Second cell: the AT-5b sentinel config; `SENTINEL-X1` is absent from stderr and the whole `--json` payload | drop the code from the usage set |
| AT-C1-3 | `decoy validate` and `decoy run` on `'ORD-' + a`: exit 1, `generate_formula_number_used_as_text`, `run` writes nothing | none (engine refusal; proves the pin) |
| AT-C1-4 | `decoy explain exit-codes` names `generate_formula_evaluation_failed` under exit 1 | remove the line |

Place: `tests/e2e/generate_config/test_formula_types_e2e.py` (new; the CLI CODEMAP puts
generate-column e2e tests in `tests/e2e/generate_config/`).

### 7b. Existing tests that change

None of these is a weakening: each old expectation pinned a defect (a null for a failure, or
text for a number), and each new one asserts more (a coded failure plus value-freedom, or the
real type). Any other test that goes red is a stop (11).

| Test | Today | New expectation | Why not weaker |
|---|---|---|---|
| `tests/unit/execution/test_expression_log_redaction.py:74-104` (`int(first_name)`, strict) | `[None] * 5`, one "failed to evaluate" warning, no name in the log | `GenerationError` with the code; no `first_name` value in `str(err)`, `err.message` or the log | the value-free check stays and now covers the error too |
| same file `:122-129` and `:131-137` (lenient `nope + 1`, `nofn(1)`) | one warning with "undefined name(s)" | `GenerationError` with the code and the default reason word; no warning | the name is refused at compile in a strict run; the lenient run now fails instead of writing nulls |
| same file `:159-176` (lenient referenced `first_name + nope`) | the same warning | the same as the row above | the same |
| `tests/unit/test_v2_generation.py:806` `TestReferencedFormulaPostPass` (lenient) | joined strings, reverse-order chain sees `""` | unchanged: the references are text faker and formula columns, so rule 2 applies | no change expected; the builder confirms green |
| `tests/parity/test_numeric_typing_readers.py:41` | `'ORD-' + id` and `id.zfill(5)` keep the column text | cases one and two move to AT-8 refusals; case three stays | the spellings now fail at compile with a hint instead of hiding a type change |
| same file `:162` (reader table row) | formula reader `False` | `True` | the registry entry it pins changed on purpose |
| same file, nullable-sequence test | no `.0`, `''` on a null row | no `.0`, null on a null row | "no `.0`" stays; null replaces a blank that was never in the config |
| `tests/unit/transforms/test_derived.py:738-740` | imports `evaluation_failure_text` | imports `failure_text`, calls it with `subject="derived expression"`; expected text byte-identical | same assertion, renamed helper |
| `tests/parity/_f0_witness.py:422-449` | records formula result lists | unchanged wrapper; the F0 `bad` column leaves the fixture (9) | the wrapper is not an assertion |
| `tests/unit/generators/test_formula_missing.py` | `""` for a missing cell | unchanged (the keyword defaults to empty) | no change |
| `tests/unit/generation/unique/test_gen_unique_old_input_bytes.py::test_cells_plan_and_fingerprints_are_unchanged[all_basic_types]` | the `cells` digest pins `score` and formula `f = score * 2` as text/repetition | re-pin only `cells`; record its before and after digests at build time; keep `config_json` and `fingerprints` unchanged | `f` intentionally changes from repeated text to numeric multiplication, so GEN-C2's render-back argument and the old "do not re-pin" default do not apply; the explicit refreeze record authorizes this one digest |
| same test, `all_basic_types_unlisted_formula` variant | a `cells` digest pins all-null output for inline `id + score` under `lenient_generate_checks` | remove this variant from the digest table and assert `GenerationError` with `generate_formula_evaluation_failed` instead | GEN-STRICT already refuses the unlisted read at compile; the lenient bypass must now fail loudly rather than preserve null bytes |
| `tests/unit/test_v2_generation.py::TestFormulaParityV1::test_inline_with_random` (`:734-741`) | `random.randint(1, 100)` makes both legacy helpers silently return nulls because `random` is bound to `_rng.random` | change the formula to `randint(1, 100)` and keep the V1 == V2 assertion; AT-6 separately pins `random.randint(1, 100)` as a coded run failure | the parity test now exercises the supported RNG name instead of parity between two swallowed `AttributeError`s |

All-tests formula-fixture scan at fetched engine `origin/main` `3a30eb89`: searched all of
`tests/` for formula configs, `references`, plain sequences, candidate int/float Faker providers,
and inline failures. The changed fixtures found are exactly the lenient error tests above; the
three numeric-typing parity cases and reader-registry row above; `_f0_fixtures.py`'s `bad` formula
and `_f_audit_cells.py`'s `cfg_formula_referenced` in section 9; and the three newly listed golden
or inline-random cases. Referenced formulas over prefixed sequences, text Faker/categorical
columns, numeric values used only through f-strings, and the reverse-order formula chain remain
byte- or assertion-equivalent. Any additional red formula fixture is still a stop.

## 7a. Reuse inventory

| Need | Reused | Not built |
|---|---|---|
| which columns are numbers | `numeric_plan`'s `base` loop and generated-type body, behind two shared helpers | no second type table |
| text cells back to numbers | `restore_declared` | no second cast |
| reader classification | `READERS` and `tests/sentry/test_numeric_reader_registry.py` | no new registry |
| failure reason words and text | the `transforms/derived.py:309` table and the `:319` builder, as `failure_reason` and `failure_text` | no new word list or text shape |
| missing-cell rule | `is_null_cell` (`internal/pandas_missing.py`) | no new predicate |
| formula parse and scope | `_check_formula`'s tree and the `_unbound_reads` scope rule | no second parse |
| run error class | `GenerationError` (`generation/pool/_errors.py:17`) | no new exception class |
| compile refusal helper | `_refusal` in `plan/checks/_generate_columns.py`, extended through `_located` with the one closed detail table and imported, not copied | no second message shape or free-form hint |
| sibling number-as-text analyzer | `generation/_numeric_derived.py:54-155` is the existing derived-grammar analyzer; formula uses the already-parsed Python AST because the grammars differ | no attempted shared walker; policy alignment is V2-LOG (13) |
| CLI usage mapping | the `uniqueness_impossible` arm | no new CLI category or `--json` field |

## 8. Placement

Engine `execution/` and the CLI flat `src/decoy/cli/` get no new files.

| Change | File | CODEMAP row |
|---|---|---|
| reader entry, `_base_types`, `_generated_type`, `reader_types`; public re-export | `src/decoy_engine/generation/_numeric_typing.py` (347 lines), `src/decoy_engine/generation/__init__.py` | `generation/`; checks import the facade |
| object frame, number set, formula-scope cast | `src/decoy_engine/generation/_referenced_formula.py` (75 lines) | `generation/` (the cross-column formula post-pass) |
| one `reader_types` call and one keyword | `src/decoy_engine/generation/synthesize.py` (592 lines, cap 600, at most +2) | `generation/synthesize.py` |
| row rule, coded raise | `src/decoy_engine/generators/_formula.py` (367 lines) | `generators/` |
| `failure_reason`, `failure_text` | `src/decoy_engine/transforms/derived.py` (327 lines) | `transforms/` |
| text-use check | `src/decoy_engine/plan/checks/_formula_types.py` (new, beside `_lookup_tables.py`; the package has 4 modules) | `plan/` row, `checks/`; add the file to that row |
| `types` threaded to `_check_formula`, the call | `src/decoy_engine/plan/checks/_generate_columns.py` (517 lines) | the same |
| reason codes | `src/decoy_engine/reasons/runtime/_part2.py`, `src/decoy_engine/reasons/plan/_part3.py`, `since=UNRELEASED` | reasons |
| engine tests | `tests/unit/generators/test_formula_types.py` (new), `tests/unit/plan/checks/test_formula_types.py` (new), the edits in 7b | beside the owning packages |
| fragment | `changelog.d/gen-formula-seq.md` with a `Breaking:` line and both identities | |
| docs | `docs/strategies.md` formula section (types, row rule, both codes, the migration table in 9); `docs/compatibility-contract.md` 3.3 dated paragraph; refreeze record `docs/records/2026-11-13-formula-typing-refreeze.md` | |
| CLI mapping | `src/decoy/cli/run/_errors.py` | `run/` package |
| CLI explain | `src/decoy/cli/explain.py` (existing module, 763 lines, two text lines) | existing flat module, no new file |
| CLI e2e | `tests/e2e/generate_config/test_formula_types_e2e.py` (new) | generate-config e2e row |
| CLI docs | `docs/guides/shape-and-privacy/03-fit-and-generate.md` (one paragraph on formula types and the migration table), `CHANGELOG.md`, `docs/ROADMAP.md` row 24y, `docs/RECENTLY-SHIPPED.md` | |

Design notes: no new structure beyond one check module, placed beside the other plan checks. One
fact, one place: a column's candidate type lives in `_base_types`; `numeric_plan`, the post-pass
and the compile check read it.

## 9. Frozen contracts touched

- **Generated output (compatibility contract 3.3).** Pre-GA one-time break with no flag, for the
  rows in 5. A dated 3.3 paragraph. Fragment `Breaking:` line: "A generate `formula` now reads a
  plain `sequence`, an int or float `faker` column and a numeric column with nulls as numbers.
  Text use (`'ID-' + id`, `id.zfill(5)`, `id[-4:]`, `id == '1'`) is refused at compile; write
  `str(id)`, or compare with a number. A formula that raises fails the run with
  `generate_formula_evaluation_failed` instead of writing nulls. A null number reference gives a
  null result. A column a formula reads keeps its numeric Parquet type unless another reader
  needs it as text. `x if id else y` is now false for `id` 0."
- **Migration table** (in `docs/strategies.md` and the CLI guide paragraph):

  | Before | After |
  |---|---|
  | `'ORD-' + id` | `'ORD-' + str(id)` or `f'ORD-{id}'` |
  | `id.zfill(5)` | `str(id).zfill(5)` or `f'{id:05d}'` |
  | `id[-4:]`, `len(id)` | `str(id)[-4:]`, `len(str(id))` |
  | `id == '1'` | `id == 1` |
  | `x if id else y` with a sequence from 0 | `x if id is not None else y` (only the numeric zero case changes; rule 1 skips the formula on a null number reference, so a formula cannot replace that null) |

  Dennis r1 scanned engine and CLI docs, `src` and examples for the refused spellings and found
  none outside plans, so no shipped example changes. `docs/strategies.md` also states that rule 1
  prevents formulas from replacing a missing number reference, and that reversed membership such
  as `'1' in id` is not refused at compile but fails at run with
  `generate_formula_evaluation_failed`.
- **Parity freeze.** `tests/parity/_f0_fixtures.py:314` `bad: undefined_name + 1` now raises.
  GEN-STRICT already refuses it at compile and the fixture bypasses compile, so the column leaves
  the frozen fixture. `tests/parity/_f_audit_cells.py:246` (`cfg_formula_referenced`): `seq`
  changes from `string` to `int64`; the `rfm` text is byte-identical. The refreeze record lists,
  for both fixtures, the frozen digest before and after and the column-level diff. The builder
  runs the freeze and lists every diff; any other diff is a stop (11).
- **Golden corpus.** `tests/unit/generation/unique/_gu_corpus.py:78-87` `all_basic_types` has
  `score: pyint` and `f: score * 2`. Re-pin only its `cells` digest, recording the exact before
  and after digests in the refreeze record at build time; `config_json` and `fingerprints` must
  remain unchanged. This is not GEN-C2 render-back: `f` changes meaning from text repetition to
  numeric multiplication. The `all_basic_types_unlisted_formula` variant at `:214-223` stops
  being a digest pin and becomes a `GenerationError(generate_formula_evaluation_failed)`
  assertion under `lenient_generate_checks`, because GEN-STRICT already refuses it at compile.
- **Reason codes.** Added: `runtime.generate_formula_evaluation_failed` and
  `plan.generate_formula_number_used_as_text`; the registry is stamped at release (#506). None
  renamed.
- **Exit codes.** C1 adds one code to the existing exit-1 arm; the map does not change.
- **`--json`.** No new field; the codes ride in the existing `error` and `reason` members.
- **`SEED_PROTOCOL_VERSION`.** Unchanged; no draw moves.

## 10. Slices

| Slice | Content | Risk | Size | Depends on | Tests at T1 |
|---|---|---|---|---|---|
| E1 | 4.1 to 4.4, codes, docs, compat note, migration table, refreeze record, fragment | R2 | M (about 280 source and 350 test lines) | none | AT-1 to AT-12, the 7b tests, all of `tests/unit/generation/`, `tests/unit/test_v2_generation.py`, `tests/unit/generators/`, `tests/unit/plan/checks/`, `tests/unit/transforms/test_derived.py`, `tests/parity/test_numeric_typing_readers.py`, `tests/sentry/test_numeric_reader_registry.py`, reason registry tests, golden and parity freeze suites; re-scan all of `tests/` for changed formula fixtures before gate |
| C1 | 4.5 mapping, explain text, e2e, CLI docs, roadmap row; lands in the CLI repin PR for the engine release that carries E1 (repin checklist) | R1 | S | E1 merged and released | AT-C1-1 to AT-C1-4, `tests/e2e/first_user/test_items_run_unique.py` |

E1 changes pinned tests and parity fixtures (moved or changed tests, a frozen contract), so its
merge head gets the full engine suite against the saved main baseline. Gate: Dennis and Codex
(R2). C1: Codex only.

## 11. Stop lines for builders

1. Do not change any other `READERS` entry, `numeric_plan`'s result, its fixed point, or a
   relationship rule.
2. Do not add a second type table, a second cast, a second missing-cell predicate or a second
   failure text shape.
3. A parity diff outside the two parity fixtures in 9, or a golden change other than the named
   `all_basic_types.cells` re-pin and unlisted-formula assertion, is a stop. An existing test that
   goes red and is not in 7b is a stop.
4. No raised module-size ceiling, sentry baseline, allowlist or pin. `synthesize.py` grows at
   most 2 lines.
5. If a second formula evaluator turns up on another route, stop and report it.
6. No test runs on the devbox: Mac first, then `~/bin/vm-run`.

## 12. Owner questions

None open.

- **OQ-1. Text idioms on a plain sequence: closed, option (a), ruled at plan review
  2026-11-13.** A formula sees the number, and the text spellings are refused at compile with a
  hint (4.4). The migration table is in 9.

## 13. V2-LOG and follow-ups

- A column demoted by another unsafe reader (`group_by`, `condition_on`, `anchor`,
  `derived_aggregate.column`) stays text in the output. After E1 a formula still sees the number
  (4.2), so there is no silent formula output; the open part is only the output type, which is
  the other reader's text-label semantics. Log for the generation QA pass.
- A generate-mode `derived` failure raises an uncoded `ValueError` and exits 3 (mask mode codes
  it as `derived_evaluation_failed`). It fails loud. Log for the generation QA pass.
- Thread the run's provider snapshot into `compile_plan`'s `check_generate_columns` calls
  (`plan/_compile.py:214`, `:535`), so the compile check and the run read one snapshot.
- Deliberate evasion of the compile check (`getattr`, rebinding through a comprehension).
- A nullable non-candidate numeric kind (`grouped_series`, `statistical`, `derived`, `lookup`, or
  `formula`) binds `""` for a missing cell; an expression such as `x * 2` then gives `""`. This
  is outside the candidate-type guarantee and remains for the generation QA pass.
- The new Python-AST check is a second number-used-as-text analyzer beside
  `generation/_numeric_derived.py:54-155`, whose derived-expression grammar currently demotes the
  column instead of refusing the plan. The grammars do not support one shared walker; align the
  derived policy with OQ-1(a) in V2.
- Chained spellings such as `str(a) + '-' + b`, where `b` is numeric, can pass the seeded compile
  check and then fail loudly at run with `generate_formula_evaluation_failed`.

## 14. Pre-mortem

- **A user config uses text idioms.** The compile refusal names the column and the fix; the
  migration table and the `Breaking:` line name the spellings.
- **A co-reader hides the type.** AT-12 pins that the formula sees the number and the check
  still refuses text use when a `group_key` or `derived_aggregate` demotes the column.
- **The refreeze hides a real change.** AT-11, the before and after digests, and stop line 3
  limit it to the two named parity fixtures plus the one named golden cell digest; the unlisted
  golden variant becomes an error assertion instead of being re-pinned.
- **GEN-INHERIT lands first and moves the type code.** E1 follows the rebase rules in 1.3, with
  no copy.
- **A formula that worked by luck on text now fails at run.** That is the intended loud failure,
  exit 1 with the code, since C1 ships in the repin PR.

## 15. Verification status

The root cause and the 1.1 table come from a repro on engine `8c9c1162`, including a spike that
flipped only the reader entry: `round(a / 3)` then gave `[0, 1, 1, 1, 2, 2]` for 6 rows, and with
`null_probability: 0.5`, `str(a)` gave `'4.0'`, which is why the object frame in 4.2 is required.
The co-reader case (HIGH-1) is shown by reading `_numeric_typing.py:260-277` at `3a30eb89`, not
by a run. No test suite ran.

## 16. Review closure

### Revision 3 (Dennis r2)

- V1-1: 7b and 9 name all three missed tests. `all_basic_types` gets one authorized cell-digest
  refreeze with recorded before/after values and stable config/fingerprint digests; the unlisted
  variant becomes a coded `GenerationError` assertion; `test_inline_with_random` uses `randint`
  while AT-6 pins `random.randint` as a coded failure. E1 T1 now includes all of
  `tests/unit/generation/` and `tests/unit/test_v2_generation.py`; the all-tests scan and its
  complete changed-fixture result are recorded in 7b.
- V1-2: the guard refuses compare and membership against a declared string reference; AT-8 adds
  `a == code`, `a in code`, `a not in code`, and the numeric `a == b` control.
- V1-3: AT-3 exempts only the grouped-series nullable cell with the `_generated_type` reason;
  section 1.2 is corrected and 13 records the nullable non-candidate behavior as V2-LOG.
- V1-4: `_refusal` is extended through `_located` with one closed detail table and the reference
  name in `where`; AT-8 pins both exact value-free messages.
- LOW-5 to LOW-10: `reader_types` is re-exported and lazily imported through the generation
  facade; `GenerationError` comes from `generation.pool`; whole-plan legacy routing, the
  null-versus-zero migration wording, reversed membership, and corrected test lines are recorded;
  7a names the derived analyzer; 13 records the analyzer-policy and chained-spelling V2-LOG items.

### Revision 2 (Dennis r1)

- HIGH-1: `reader_types` returns the candidate type; the post-pass casts text cells for the
  formula scope only through `restore_declared`; the check judges on the candidate type; AT-12.
- MEDIUM-2: 7b lists each changed test with its new expectation.
- MEDIUM-3: AT-5b and the second AT-C1-2 cell use a sentinel value.
- MEDIUM-4: subscript, `len` and `join` shapes; comprehension scope; reuse of `_check_formula`'s
  tree.
- MEDIUM-5: AT-C1-2 asserts `error` and `reason`, not `error_kind`.
- MEDIUM-6: one `failure_text` builder with a subject.
- MEDIUM-7: AT-3 over the type Literal; rebase rules for #555 and #557.
- LOWs: rebased to `3a30eb89` and #564 branches dropped; class 7 narrowed; OQ-1 closed with a
  migration table; the child-reference type change is listed; the number set lives in
  `_referenced_formula.py`; keywords default to empty; rule 3 stops at the first failing row;
  C1 in the repin PR; refreeze digests.
