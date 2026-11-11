# 02. Sources, targets and file I/O

## What it covers

Reading source tables and writing outputs: local CSV, Parquet and fixed-width files,
S3 and GCS sources and targets (CSV and Parquet), SFTP connectors, in-memory Arrow
tables passed to `run_pipeline`, lazy sources, and the transactional sink.

## Expected behaviour

- A CSV source must say whether it has a header (`has_header`). A headerless file needs
  `column_names` or `column_names_file`. There is no guessing.
- A source column the config does not list, and a source with no table entry, are
  refused before reading (`config.source_file.undeclared_column`,
  `config.source_file.undeclared_table`).
- A declared column that the source does not have is refused before any masking.
- Reader errors name the table, line and counts. They never quote a cell.
- Fixed-width sources need a `layout`. A layout is forbidden on CSV and Parquet.
- A transactional sink publishes every output file at once or none of them. A
  pre-existing non-empty target makes the commit fail closed.
- Passthrough columns come back exactly as the source held them, including types the
  pandas round trip would alter.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| SRC-01 | CSV source with no `has_header`. | Refused with `config.source_file.has_header_required`. |
| SRC-02 | `has_header: false` with no names; then with both `column_names` and `column_names_file`. | First refused; second refused with `config.source_file.column_names_source_conflict`. |
| SRC-03 | `has_header: true` together with `column_names`. | Refused. |
| SRC-04 | CSV whose file has a column the config does not declare. | Refused with `config.source_file.undeclared_column` before any row is read. |
| SRC-05 | Config declares a column the file does not have. | Refused before masking. `decoy validate` reports the same problem. |
| SRC-06 | CSV with one row holding too many fields, one with a NUL byte, one with a lone carriage return. | Each refused. The message gives table, line and counts, and no cell text. |
| SRC-07 | Pipe-delimited and tab-delimited CSV with `delimiter` set. | Read correctly. Output columns match the source columns. |
| SRC-08 | Fixed-width source with a valid layout; then with a record that is too short. | First reads and masks. Second is refused naming file, line, column and length, never the value. |
| SRC-09 | `layout` set on a CSV source; fixed-width source with no layout. | Both refused at validation. |
| SRC-10 | Parquet source with a nullable integer above 2^53 in a passthrough column, on a job large enough to auto-chunk. | The value comes back exact. It was not rounded through a float. |
| SRC-11 | FK job writing through a transactional sink, with a failure forced on the last table. | No output file is visible at the target. Nothing partial is left. |
| SRC-12 | Same job with a non-empty target directory already present. | Commit fails closed. Existing files are untouched. |

## Open points

- The CLI README says `decoy run` does not yet read from or write to S3 or GCS, while
  the engine config accepts `s3` and `gcs` sources and targets. Decide what V1 QA covers
  for cloud I/O: engine API only, or nothing.
- DuckDB sources are a CLI flag (`--duckdb-source`), not an engine source type. Confirm
  whether that belongs in engine QA or CLI QA.
- The six connectors in the capability matrix (S3, GCS, SFTP, source and sink) have no
  checks here. Add them once the cloud scope is decided.
