# 10. Expressions and conditional masking

Status: plan

## What it covers

Config-supplied expressions: `formula`, `derived`, `derived_aggregate`, and the `when:`
predicate that masks only selected rows.

## Expected behaviour

- **derived** computes a column from others in the same row using a closed grammar:
  arithmetic, comparison, `and` / `or` / `not`, `concat`, `slice`, `days_between`,
  a ternary, and literals. Anything else is refused at config parse.
  `null_propagation` is `explicit_null` (default), `sentinel` or `default`. `bounds`
  clip numeric results.
- **derived_aggregate** fills every row with one `sum`, `mean`, `min`, `max` or `count`
  of a column in the same table. Nulls are excluded, as in SQL.
- **formula** evaluates a user expression through a restricted evaluator (`simpleeval`).
- **when:** takes a comparison of one column with one literal, `in` / `not in` with a
  literal list, and `and` / `or` / `not`. Anything else is refused with
  `when_outside_closed_grammar`. Unselected rows keep their source value.
- No path calls `eval`, `exec` or `__import__`. A cell that looks like an expression is
  data and is never evaluated.
- An error while evaluating a row fails the job and names the column and row index.
- Error messages name positions and constructs, never the predicate text.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| EXPR-01 | `derived` with `expression: "age * 12"` and `bounds: {min: 0, max: 1500}`. | Correct products, clipped to the bounds. A null `age` gives null. |
| EXPR-02 | `derived` with `concat(first_name, last_name)` under each `null_propagation` mode, with a null first name. | `explicit_null`: null. `sentinel`: the last name alone. `default`: evaluated with 0 in place of null; record the result. |
| EXPR-03 | `derived` with `__import__('os').system('id')`, with `x.__class__`, and with a function not in the grammar. | Each refused at config parse. Nothing is executed. |
| EXPR-04 | `derived` with `amount / count` where one row has `count` 0. | Job fails, naming the column and row index. No partial output. |
| EXPR-05 | `derived` with `bounds: {min: 10, max: 1}`. | `PlanCompileError(derived_bounds_inverted)`. |
| EXPR-06 | A source cell containing the text `__import__('os')` in a column read by a `derived` expression. | Treated as a string. Never evaluated. |
| EXPR-07 | `derived_aggregate` with each `op` on a column with nulls. | Matches the SQL result: nulls excluded, `count` counts non-nulls. Every row holds the same scalar. |
| EXPR-08 | `derived_aggregate` with `op: median`; with a missing source column. | `derived_aggregate_op_invalid`; `derived_aggregate_missing_column_ref`. |
| EXPR-09 | `formula` attempting attribute access to reach a builtin (`().__class__.__subclasses__()`). | Refused or raises safely. No code outside the evaluator runs. |
| EXPR-10 | `when: "country == 'DE' and consent != 'yes'"` on `redact`. | Only matching rows are redacted. All other rows are byte-identical to the source. |
| EXPR-11 | `when:` with arithmetic, a function call, a comparison of two columns, and a chained comparison. | Each refused with `when_outside_closed_grammar`. The message does not contain the predicate text. |
| EXPR-12 | `when: "status != 'x'"` on a column with nulls, read as a numpy-backed column and as a pandas nullable `string`. | Record which rows are selected in each case. The docs say the two differ; confirm the outputs match what the docs describe. |

## Open points

- `when:` has no null test. Selecting "is null" works only on numpy-backed columns; on a
  pandas nullable dtype the negation selects nothing. That is a real gap users will hit.
- A raw config dict that skips `PipelineConfig` is not held to the `when:` grammar and
  reaches `DataFrame.eval`. See the open point in section 01.
- Min and max in `derived_aggregate` broadcast a real source value to every row. The
  docs flag this as a privacy caveat; nothing warns at run time.
- `formula` is described in two lines. Its allowed operations and its null handling are
  not documented, so EXPR-09 is the only check it has.
