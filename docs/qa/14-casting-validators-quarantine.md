# 14. Declared casting, validators and quarantine

Status: plan

## What it covers

Three related controls: declared parsing and output formats on a column
(`logical_type`, `parse_format`, `sentinel_to_null`, `error_policy`, `output_format`),
the job-level `validators:` block, and the two quarantine stores (the JSONL
`quarantine:` block and the encrypted `protected_quarantine:` block).

## Expected behaviour

- **Declared casting** reads text or bytes exactly as declared, with no inference: no
  float, no rounding, no locale, no default NA conversion. Order is sentinel, parse,
  mask, format. The formatter checks its own output by parsing it back.
- `error_policy` is `fail` (abort the run), `null` (the field becomes null) or
  `quarantine` (the whole row leaves the output and is stored encrypted).
- Counts of substitutions and failures are reported with no values under
  `quality_metrics["declared_casting"]`.
- A route that cannot honour a declared contract is refused before any source is read.
  Automatic routing never picks one.
- **Validators** run after all columns are masked, in the order declared. A failure
  fails the job by default. Eleven are built in: `luhn`, `npi`, `iban`, `vin`,
  `fk_intact`, `no_orphan_children`, `leak_check`, `regex_match`, `column_in_set`,
  `parent_window_respected`, `reconciliation_holds`.
- **Row errors** (`format_error`, `mask_error`) fail the job with
  `RowErrorsFailedError`, or route the row to quarantine when that trigger is enabled.
  The source value is never silently kept.
- **Quarantine JSONL** holds each failing row once, with `_quarantine_trigger`,
  `_quarantine_reason` and `_source_table`. The file is not written when nothing is
  quarantined.
- **Protected quarantine** stores rows encrypted, with finite retention and size limits
  proven before any source is read, and returns an opaque retrieval token.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| CAST-01 | The README example: `integer/ascii/v1` in, `bucketize` width 10, `integer/zero-pad/v1` width 3 out, on `042`. | The strategy sees 42. The output is exactly three characters. |
| CAST-02 | `sentinel_to_null: {values: [""]}` on a column with empty strings and physical nulls. | Both are null in the output. Only the empty strings are counted as `sentinel_to_null`. |
| CAST-03 | `integer/ascii/v1` column containing `12a` under `fail`, `null` and `quarantine`. | `fail`: run aborts with `DeclaredCastValueError` and value-free counts. `null`: the field is null and the row stays. `quarantine`: the row leaves the output. |
| CAST-04 | `decimal/plain/v1` with `precision: 10`, `scale: 2` on `1234.50`, `1,234.50` and `1e3`. | First parses exactly. The other two fail the parse and follow the error policy. |
| CAST-05 | `cobol/comp-3/v1` on packed bytes with positive, negative and invalid sign nibbles. | Valid values decode exactly. The invalid nibble is a parse failure, not a guess. |
| CAST-06 | `output_format` whose `width` is too small for a masked value. | An output failure following the error policy. Never a truncated or overflowing field. |
| CAST-07 | `error_policy: quarantine` with no `protected_quarantine` block; then forced onto the out-of-core route. | Both refused before any source is read, with a registered code. |
| CAST-08 | `validators: [{name: luhn, columns: {orders: [card]}}]` on FPE output with and without `checksum: luhn`. | Passes with the checksum. Fails the job without it. |
| CAST-09 | `leak_check` on a job where one `faker` column is a no-op for some rows. | The column is flagged. With quarantine on, the leaking rows are removed and stored. |
| CAST-10 | `fk_intact` and `no_orphan_children` on a masked parent and child with orphans under `preserve`. | Findings are raised for the orphans. With a misnamed parent table in `relationships:`, the validator raises instead of flagging every row. |
| CAST-11 | `regex_match` with a pattern, `column_in_set` with `allow_null: false` on a column with nulls. | Whole-cell matching. Nulls are findings only when `allow_null` is false. |
| CAST-12 | A row that fails two validators and has a row error, with all triggers on. | The row appears once in the JSONL. `total_quarantined` is 1. `counts_by_trigger` may sum higher. |
| CAST-13 | Protected quarantine run that seals rows; then a run that fails after the seal. | First returns `ExecutionResult.quarantine_access`; the token opens the rows and is not in evidence or `repr`. Second leaves no sealed artifact and returns no token. |

## Open points

- The JSONL quarantine writes "all original column values" in plain text to a path in
  the config. The protected quarantine exists because quarantined rows are raw data.
  Decide whether V1 should warn when the plain JSONL store is used.
- An unknown validator name: the docs say `name` must be one of the eleven. Confirm it
  is refused at validation.
- `leak_check` excludes passthrough, FK-child and `when:`-gated columns by construction,
  so a leak in those is never reported.
- Features listed as "not in V1" for declared casting (locale parsing, time zones,
  exponent notation, copybook import) should be refused clearly, not half-handled. Pick
  two and add checks.
