# CLI-DIALECT Part E: the public CSV source reader (build record)

Status: record

Head: see git log. Branch `feat/csv-source-reader`, engine `origin/main` base `55876436`. Handoff: CLI-DIALECT revision 5, Part E.

## What changed

- `profile/_csv_reader.py` (new): `read_csv_source`, `resolve_csv_dialect`, the shared core `_read_csv_rows`, the moved headerless width scan, and `_refusal`, the one constructor of this module's refusals (`<code>: source <table> <detail>`).
- `config/_csv_dialect.py` (new): `csv_dialect_problem` and `CsvDialect`. `FileSource` and `CsvDialect` both call it; the `FileSource` error texts are unchanged.
- `CsvFileSource._read` delegates to `_read_csv_rows`; `_assert_record_widths`, `_positional_lexemes` and the field-size lift were deleted from `_readers_csv.py`.
- `fileaccess/adapters/pandas.py`: `read_csv_chunks`, the guarded chunk iterator (the one chunked pandas read on a policy handle).
- `build_profile_source` and `_build_file_source` take `table`; `_build_file_source` calls `resolve_csv_dialect`; `profile_source` passes the table name.
- Public surface: three names in `__all__`, `ENTRY_POINTS`, `test_public_api.py`, compatibility contract 3.4, the security doc bullet, CHANGELOG.

## Base frames

`tests/unit/profile/fixtures/csv_reader_base_frames.json` was generated from the unmodified `CsvFileSource.to_frame()` on engine main `55876436` (commit `8d280398`) for the 14 AT-E1 fixtures, before the refactor. The head must reproduce it.

## Deviations from the handoff

1. Refusals are raised after the `except` block closes, not `raise ... from None` inside it. `from None` leaves `__context__` set, and AT-E3 asserts `__context__ is None`. Mutation (b) of AT-E3 is therefore "move the raise inside the except".
2. AT-E4 covers the refusals the scan catches. An unterminated quote passes the scan and pandas rejects it while reading, so with `chunksize` it is raised on the step that reads it. AT-E3 checks it there.
3. A chunked read of a headered file stats the file at the call, so a missing or denied file fails at the call (the headerless scan already does this).
4. AT-E5 and AT-E7 call `profile_source` with a minimal `{"sources": ...}` config.
5. The base was `55876436`, a descendant of the handoff base `41a86f47`.

## Not folded (DESIGN items)

`profile/_cloud_readers.py:144,292,396` call `pd.read_csv` for S3 and GCS CSV sources with no dialect fields; `io/bounded_reader.py` `iter_csv_chunks` (Arrow) takes no dialect. Both route through `resolve_csv_dialect` when they gain the fields.

## Evidence

- Red first: with the three names stubbed (`NotImplementedError`), `test_csv_reader.py` ran 58 failed, 15 passed.
- Mutations, each turning its test red: AT-E1 drop `sep=`; AT-E2 default `has_header` False; AT-E3 (a) skip the scan, (b) raise inside the except, (c) no except mapping; AT-E4 lazy chunk scan; AT-E5 (a) default delimiter tab, (b) own `.get` in `_build_file_source`; AT-E6 remove `@entry_point`; AT-E7 file name as table; AT-E9 converters by name headerless; AT-E10 unguarded chunk iterator; AT-E11 (a) drop uniqueness, (b) own check in `FileSource`; AT-E8 `csv.reader` in `_readers_csv.py`.
- T1: ruff, format, mypy, sentry and licenses steps of `gates.sh --baseline`, plus 7639 tests (named tests, behavior-kept list, `tests/sentry`, `tests/fileaccess`, `tests/unit/config`, every file naming `CsvFileSource`, `build_profile_source` or `profile_source`), no failures. The first sentry run found three branch-only failures (manifest listing, reason-registry forwarding helper, GCS-name heuristic on `.update`), fixed in the second commit.
- Not verified: macOS arm64 run; the full suite (orchestrator, frozen-contract trigger).
