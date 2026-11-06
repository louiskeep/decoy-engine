# CLI-DIALECT Part E: the public CSV source reader (build record)

Status: record

Head: see git log. Branch `feat/csv-source-reader`, engine `origin/main` base `55876436`. Handoff: CLI-DIALECT revision 5, Part E.

## What changed

- `profile/_csv_reader.py` (new): `read_csv_source`, `resolve_csv_dialect`, the shared core `_read_csv_rows`, the moved headerless width scan, and `_refusal`, the one constructor of this module's refusals (`<code>: source <table> <detail>`).
- `config/_csv_dialect.py` (new): `csv_dialect_problem` and `CsvDialect`. `FileSource` and `CsvDialect` both call it. The text is fixed and value-free (the delimiter is no longer echoed, for `FileSource` too; only the prefix is pinned by tests).
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

## Fix round 1 (review findings)

- Strict policy: `fileaccess/_errors.py` gains `guarded_with_parser_errors` (the one place a library error type passes a strict policy; `guarded` is the empty-tuple case). `adapters/pandas.py` names `PARSER_ERRORS` and adds `read_csv_parser_errors` and `read_csv_chunks_parser_errors`. Only the headerless path uses them, so the reader replaces the parser text with its coded refusal; the header path and every other library failure still map to `FileAccessReadError`.
- `nrows=0`: `_scan_record_widths` returns before touching a record; the read names the empty frame with the declared columns (`names=`), so a malformed first record cannot change its width.
- Value echo: `csv_dialect_problem` texts no longer carry `delimiter!r`. One rendering, no copy: nothing pins the old "(got ...)" suffix.
- Reason sentry: `tests/sentry/_reason_sinks.py` (split out of `_reason_scan.py`, which shrank from 806 to 765 lines) discovers plain functions with a `code` parameter. The scanner treats the forwarding helpers named by the registry test (`HELPER_SINKS`, derived from the `_HELPER` adapter rows) as code sinks and classifies each caller's argument. Helpers whose callers still pass literals or forwards are in the `UNCHECKED_HELPERS` ratchet (`_raise`, `_qerror`, `_row_error`, `_fail`, `_refuse`), each a DESIGN follow-up. `_refusal` is checked.
- Defaults: `resolve_csv_dialect` passes only the keys present; `CsvDialect` field defaults are the one home, asserted equal to `FileSource.model_fields`. AT-E5 compares `resolve_csv_dialect(plain.model_dump())` with `CsvDialect(plain.has_header, plain.column_names, plain.delimiter)`.
- `column_names`: a str, bytes, non-sequence or non-str entry is refused by the shared rule (`ConfigError` from `CsvDialect` and `resolve_csv_dialect`).
- CODEMAP: `_csv_reader.py` in the `profile/` row, `_csv_dialect.py` in the `config/` row.

### Red first (on 2aa24def, with the new tests)

22 failed, 137 passed over `test_csv_reader_strict.py`, `test_csv_dialect_rules.py`, `test_csv_reader.py`, `test_reason_registry.py`. Red: strict-policy whole and chunked refusal (4), `nrows=0` malformed or empty first row (7), delimiter echo (3), non-sequence `column_names` (5), forwarding-helper callers (2). The later-chunk-step test was added after mutation N1f stayed green and is red on that mutation.

### Mutations on the final head (all red)

Handoff set (15 listed in the build record): AT-E1 drop `sep`; AT-E2 default `has_header` False; AT-E3 (a) skip scan, (b) raise inside except, (c) no except mapping; AT-E4 lazy chunk scan; AT-E5 (a) default delimiter tab, (b) own `.get`; AT-E6 remove `@entry_point`; AT-E7 file name as table; AT-E9 converters by name headerless; AT-E10 unguarded chunk iterator; AT-E11 (a) drop uniqueness, (b) own check in `FileSource`; AT-E8 `csv.reader` in `_readers_csv.py`. All red.

New: N1a no parser pass-through in `guarded_with_parser_errors`; N1b adapter passes no types; N1c whole read uses the plain adapter; N1d header path uses the parser variant; N1e chunked read uses the plain adapter; N1f chunk steps unguarded for parser errors; N2a no early return for `nrows=0`; N2b zero-row frame unnamed; N3 and N3b delimiter echo restored; N4a unregistered literal at a `_refusal` caller (`test_no_ad_hoc_reason_codes`); N4b scanner ignores helper sinks; N5 resolve with its own delimiter default; N5b `CsvDialect` default differs from `FileSource`; N6 str `column_names` accepted. All red.

### T1

`tests/unit/profile tests/unit/config tests/fileaccess tests/unit/test_public_api.py tests/sentry`: 5528 passed, 6 skipped. ruff check, ruff format --check and mypy on the touched modules clean. Not run: the full suite.

### Placement

| New file | Directory | CODEMAP line | Neighbor |
|---|---|---|---|
| `src/decoy_engine/profile/_csv_reader.py` | `profile/` | `profile/` row (reader modules; "Profile types + `profile_source`; `_fixed_width_reader.py`") | `_fixed_width_reader.py`, `_readers_csv.py` |
| `src/decoy_engine/config/_csv_dialect.py` | `config/` | `config/` row ("source/target descriptors") | `_sources.py`, `_fixed_width.py` |
| `tests/unit/profile/test_csv_reader.py`, `test_csv_reader_strict.py`, `fixtures/csv_reader_base_frames.json` | `tests/unit/profile/` | tests mirror `src/` layout | `test_csv_delimited_params.py` |
| `tests/unit/config/test_csv_dialect_rules.py` | `tests/unit/config/` | same | `test_filesource_delimited_params.py` |
| `tests/sentry/test_csv_scan_single_copy.py` | `tests/sentry/` | CODEMAP `tests/` row names `sentry/`; no finer rule exists | `test_log_interpolation.py` (AST-walk sentry) |
| `tests/sentry/_reason_sinks.py` | `tests/sentry/` | none finer than the `tests/` row (see LAYOUT-T below) | `_reason_scan.py` |
| `tests/sentry/baselines/reason-unchecked-helpers.json` | `tests/sentry/baselines/` | the registered-baseline mechanism (`scripts/dev/sentry_registry.toml`, `_baseline_sources.py`) | the other registered baselines |

The CODEMAP had no entries for the two source files; this round adds them (the `profile/` and `config/` rows).

### Line counts

`_csv_reader.py` 325, `_csv_dialect.py` 78, `_errors.py` 209 (was 184), `adapters/pandas.py` 51 (was 24), `_readers_csv.py` 123 (was 277), `_reason_scan.py` 765 (was 806), `_reason_sinks.py` 91 (new), `test_reason_registry.py` 711 (was 658, over the 600 target; tests are outside the `src` cap), `test_csv_reader.py` 409, `test_csv_reader_strict.py` 233, `test_csv_dialect_rules.py` 155, `test_csv_scan_single_copy.py` 67. Total added on the branch is about 1100 lines against the 800 STOP, most of it tests and the base-frame fixture (154). If reviewers ask, the split: (1) dialect and `FileSource` delegation, (2) reader and `CsvFileSource` delegation, (3) public surface and docs, (4) the sentry and adapter changes.

### V2-LOG and DESIGN follow-ups (recorded, not fixed)

- Sentry AT-E8 evasion shapes: wildcard import, a bound alias of `csv.reader`, importing `_csv`, `getattr(csv, "reader")`.
- Ratchet the callers of the `pandas_adapter` read functions (`read_csv`, `read_csv_chunks`, the parser-error variants) so a new caller outside the reader is flagged.
- Cloud CSV readers (`profile/_cloud_readers.py`, `io/bounded_reader.py`) still bypass the dialect.
- `UNCHECKED_HELPERS` (above): route each helper's callers through registry symbols and remove the entry. Findings behind it: `_check_dp._raise` (12 literal callers), `_window_admission._refuse` (a literal constant), `_quarantine_local._qerror` and `_checks_declared_casting._fail` (forwarded parameters), `_raise` name shared with `config/_declared_casting.py`.

## Fix round 2 (round 2 review: Codex NO-GO, Dennis GO-WITH-FIXES)

- Value-free config errors. The engine's config-error boundary is the `hide_input_in_errors=True` model setting every pipeline model already carries, plus `validate_config`. `FileSource`, `S3Source` and `GCSSource` were the only config models without it, so a direct `FileSource` validation error rendered the whole submitted descriptor. They now set it (no second sanitizer). Pydantic still keeps the input in `ValidationError.errors()` and `.json()`, so `validate_config` now raises its `PipelineValidationError` after the `except` block with only the rendered text: no `__cause__` and no `__context__` carries the value-bearing error. The test goes through the public `validate_config` route and checks `str`, `repr`, `args`, cause and context.
- `nrows=0`, headerless: `_zero_rows` opens the handle (policy check) and calls `stat` (existence), then returns the empty object-column frame or, chunked, no chunk (pandas yields none either). Nothing is scanned or tokenized, so malformed content (unterminated quote, NUL, lone CR, wrong width, empty file) reads as the empty frame under ambient and strict policies, whole and chunked. A headered file still reads its header with pandas, because the header is the declared column list.
- Helper identity: `discover_function_sinks` keys sinks by `(path, function)`; `_Index.resolve_function` follows imports to the defining module, so a call resolves to the helper defined in or imported into the calling module. `HELPER_IDENTITIES` come from the `_HELPER` adapter rows; `UNCHECKED_HELPERS` holds exact identities and is registered as the `reason-unchecked-helpers` baseline (shrink-only through `ratchet_check`). With identity resolution all five unchecked helpers still have unregistered callers, so none could be removed. `NOT_POSITIONAL_SINKS` records why five helpers are not positional sinks (keyword-only `code`, or no `code`); a test asserts every checked helper is a discovered sink or has that rationale.
- Defaults: `CSV_DIALECT_DEFAULTS` in `config/_csv_dialect.py` is the one descriptor. `CsvDialect` field defaults, the `FileSource` `Field(default=...)` values and the dump that omits defaults all read it.
- Strict pass-through sentry (AT-E12, in `test_csv_scan_single_copy.py`): `guarded_with_parser_errors` may be named only in `fileaccess/_errors.py` and `fileaccess/adapters/pandas.py`; `read_csv_parser_errors` and `read_csv_chunks_parser_errors` only in the adapter and `profile/_csv_reader.py`. It fails on any other module, and on a stale entry.
- `test_nrows_zero_chunked_yields_no_rows` is replaced by a test that asserts `list(chunks) == []` (zero chunks) for all four modes.

### LAYOUT-T item

`tests/sentry/_reason_sinks.py` is the 51st direct module of `tests/sentry/`. Folding it into `_reason_scan.py` (789 lines) or `test_reason_registry.py` (now over 800) would break the file-size rule, and `_reason_baseline.py` imports the scanner (a cycle). The owner deferred test-folder layout to roadmap row LAYOUT-T, so the module stays and moves with that row. `test_reason_registry.py` is 820 lines (was 658): its helper-identity tests belong in a reason-sentry subpackage once LAYOUT-T creates one.

### Red first (on a4cd737d sources, new tests)

13 failed over `test_csv_dialect_rules.py` and `test_csv_reader_strict.py`: `test_dialect_defaults_have_one_home`, both `test_filesource_validation_error_is_value_free`, `test_public_loader_route_carries_no_submitted_delimiter`, `test_source_models_hide_input_in_errors`, the `bad_quote` zero-row cases (2 plus 4 in the four-mode test), `test_nrows_zero_never_reaches_the_tokenizer`, and the chunked missing-file case. The identity, collision and sentry tests are test-side changes and are red under the mutations below.


### Mutations on the round 2 head (all red)

Handoff set (15): AT-E1 drop `sep`; AT-E2 default `has_header` False; AT-E3 (a) skip scan, (b) raise inside except, (c) no except mapping; AT-E4 lazy chunk scan; AT-E5 (a) default delimiter tab, (b) own `.get`; AT-E6 remove `@entry_point`; AT-E7 file name as table; AT-E9 converters by name headerless; AT-E10 unguarded chunk iterator; AT-E11 (a) drop uniqueness, (b) own check in `FileSource`; AT-E8 `csv.reader` in `_readers_csv.py`.

Round 1 set: N1a to N1f, N2a, N2b, N3, N4a, N4b, N5, N5b, N6 (as listed above).

Round 2: N2c zero-row read skips the file check; N2d zero-row chunked read yields a frame; R1a `FileSource` shows input; R1b `validate_config` chains the pydantic error; R3 default literal restated in `FileSource`; R3b the dump compares a literal; R4 new caller of `guarded_with_parser_errors`; R4b new caller of `read_csv_parser_errors`; R4c stale allowlist entry; R5 unchecked-helper swap; R6 regrowth; R7 same-name collision (same function name, other path); R8 scanner follows no imports (bare-name resolution); R9 a checked helper with no sink and no rationale. All turn a test red; the runner is a throwaway script (apply, run the CSV, config, validation and reason-sentry tests, restore).

### T1 (round 2 head)

`tests/unit/profile tests/unit/config tests/fileaccess tests/unit/test_public_api.py tests/unit/test_validation.py tests/sentry`: 5585 passed, 6 skipped; mypy clean on the touched modules and `fileaccess/`; ruff check and format clean. `ratchet_check.py` reports `reason-unchecked-helpers` as a first inventory of 5 keys (review required) and no finding. Not run: the full suite (frozen-contract and sentry-baseline triggers apply at merge).

### Line counts (round 2)

`_csv_reader.py` 340, `_csv_dialect.py` 90, `_sources.py` 238, `validation/_config.py` 97, `_reason_scan.py` 789 (was 806 on main), `_reason_sinks.py` 97, `test_reason_registry.py` 821 (LAYOUT-T item above), `test_csv_scan_single_copy.py` 134, `test_csv_reader_strict.py` 293, `test_csv_dialect_rules.py` 238.
