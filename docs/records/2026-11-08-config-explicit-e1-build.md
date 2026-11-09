# CONFIG-EXPLICIT E1 build record: has_header required, column_names_file

Status: record

Branch `feat/config-explicit-e1-schema`, base engine `origin/main` `45553d7e` (LIB L0.1 merged; `b5b1ad16`
is an ancestor, tag `v0.19.0` exists). Handoff `p05-config-explicit-build-handoff.md` r7, Part E1. Risk R2.

## What changed

- `FileSource.has_header` is `bool | None` with `validate_default=True`; a csv source without it raises
  `PydanticCustomError(source_file.has_header_required)` at `loc ... has_header` with the fix line. A failed
  `format` adds no second error.
- `column_names_file` (path marker `x-decoy-path`, exported as `FILE_SOURCE_PATH_FIELDS`). The csv branch
  raises the coded `PydanticCustomError` for the three texts in `DIALECT_PROBLEM_CODES`; the non-csv branch
  raises `csv_param_on_non_csv`; name-list and delimiter checks stay `ValueError`. A csv dump always
  carries `has_header`; a non-csv dump never does; `layout: None` stays.
- `resolve_csv_dialect(descriptor, *, table="source", file_access=None)` is a file-access entry point,
  requires `has_header` before any IO, and reads a header file through `fileaccess.read_checked` with a
  1 MiB `fstat` check plus a check on the bytes read. One `_record_reader` (the module's one `csv.reader`)
  and one `_is_blank_record` serve the data scan and the header file. `_refusal` is `source_refusal`.
- `build_profile_source` is a file-access entry point; `_build_file_source` passes `table`.
- Registry 1.10.0: `has_header_required` and `column_names_source_conflict` (validation_result block),
  `column_names_file_invalid` (PipelineValidationError block), mirrored in `CODES`. Fixture
  `reason-manifest-v1.10-config-explicit-e1.json`; `consumer-contract.json` regenerated (diff:
  `registry_version` only).
- Sentry `tests/sentry/source_config/test_source_dialect_choke_point.py` (AT-E1-9), one `SENTRIES` line.
- Codemod `scripts/dev/codemods/config_explicit.py has-header`: 95 Python and 2 YAML edits in `tests`,
  2 in `scripts` (one row per edit in the run output). Hand fixes: about 115 test lines in 22
  files (comprehensions, assignments, shared helpers such as `tests/native/_chunked_entry_support.py`,
  and three variable-format builders that gain a `_header(fmt)` helper), under STOP 3's 150; the
  two LEGACY scripts (`gen_proof_manifest.py` 1230 to 1230, `bench_c1_oracle.py` 766 to 759, pin
  lowered to 759). The first T1 at `eb2abfb3` failed 3830 tests on missing hand fixes; that list
  drove them.

## Changes on purpose

- `test_csv_dialect_rules.py`: `test_csvdialect_defaults_equal_the_filesource_defaults` and
  `test_dialect_defaults_have_one_home` split `has_header` out (FileSource default `None`); the
  dump test gains `has_header`; `test_resolve_passes_only_the_keys_present` uses
  `{"has_header": True}` and asserts `{}` raises; `test_public_delimiter_errors_are_value_free` passes
  `has_header`.
- `test_filesource_delimited_params.py`: `_csv()` gains `has_header: True`;
  `test_defaults_are_todays_behaviour` is `test_dialect_defaults_other_than_has_header`.
- `test_csv_reader.py::test_profile_source_resolves_the_dialect_once`: spy takes `**kwargs`, asserts
  `{"table": "events"}`.
- `tests/sentry/test_reason_registry.py` `_refusal` to `source_refusal` at the three lines (net 0).
- `test_public_api.py` pins `FILE_SOURCE_PATH_FIELDS`; `test_module_size.py` benchmark pin 759.

## Red first (base `45553d7e` plus name-only stubs)

Stubs: the three reason names as plain strings, a `CODES` attribute, `_is_blank_record` returning
`None`, the resolver signature with `table` and `file_access`. Result: 46 failed, 4 passed (the
characterization rows: inline-names round trip, parquet dump, invalid format, bad delimiter). Reds are
assertion failures, or the base `ConfigError` refusing the unknown `column_names_file` (the handoff's
"field refused").

## Mutations (head `eb2abfb3`, VM snapshot, each reverted)

All 25 red: AT-E1-1 default True; AT-E1-2 dump drops `has_header`; AT-E1-3 a/b/c (conflict as
`ValueError`, headerless mapped to with-header, FileSource text differs); AT-E1-4 a/b/c (comma
delimiter, stripped names, copied blank predicate); AT-E1-5 a/b/c (first record taken, NUL re-raised in
an `except`, no regular-file check); AT-E1-6 names from the descriptor; AT-E1-7 owner and `since`
1.5.0; AT-E1-8 fix sentence dropped; AT-E1-9 a-d; AT-E1-10 a (each decorator), b, c, d, e.

## Deviations

- AT-E1-3 (c): `_sources.py` holds no copy of the dialect text (it formats `csv_dialect_problem`'s
  result), so the mutation changes the prefix format instead.
- AT-E1-10 (d): `except Exception` survives cell (b) because `keeps_class` re-raises a denial under a
  strict policy. Cell `test_only_an_os_error_becomes_a_refusal` (ambient, a `RuntimeError` from the
  read must propagate) catches it.
- AT-E1-6: `run_pipeline` takes in-memory frames and never reads the header file, so the test covers
  `profile_source`, `read_csv_source` and the lexeme path only.
- AT-E1-7: the fixture pins three identities, so it is compared with the same slice of the live
  manifest (`append_only_violations` on the whole catalog reports added identities).
- Blank and duplicate header names: `csv_dialect_problem` decides; `_name_position` only names the
  position for the fixed phrase.
- The field validator reads `format` into a local before comparing, because
  `test_dialect_defaults_have_one_home` rejects any `data.get(...) == <constant>`.
- `profile/_csv_reader.py` is 475 lines (handoff estimate under 440); under 600, no census entry.
- `test_reason_registry.py` (821 lines) keeps net 0 with `# fmt: skip` on the renamed line.
- The codemod also matches `sources=` keywords and `sources = {...}` assignments (95 edits vs the
  measured 84 literals).
- Registry: SCAN-ML M1 merged first (#405) and took the next minor, so after merging main
  `registry_renumber.py` moved E1 to the next minor; derived visibility (#408) later moved it again.
- Not in the allowed files and still showing csv sources without `has_header`: `README.md`,
  `docs/quickstart.md`, `docs/recipes.md`, `examples/`. Needs an orchestrator decision.

## STOP 4 (closed: both hashes re-pinned in `f2d2d346` on the orchestrator's decision)

- `tests/unit/config/test_c8_i_when_config.py::test_a_config_without_when_hashes_exactly_as_before_the_field_existed`:
  `pipeline_config_hash` still equals the pin; `_PRE_WHEN_CANONICAL_SHA` moves (`77c4914f...` to
  `187c7c9c...`) because a csv dump now always carries `has_header`, the physical-hash move the
  compatibility paragraph names.
- `tests/acceptance/test_f06_declared_casting.py::test_b7_legacy_config_and_plan_match_the_baseline_fixture`:
  the frozen `tests/fixtures/f06/legacy-compat-baseline.json` `raw_config` has a csv source without
  `has_header`, so validation now refuses it with the fix line (the intended rule).
- Edited as L0.1 fallout (precondition 1 merge rule), identical to SCAN-ML M1's merge:
  `tests/fileaccess/protected_dir/test_protected_dir.py:222` pinned `REGISTRY_VERSION == "1.7.0"`
  and now checks `>= (1, 7, 0)`.

## V2-LOG

- `read_checked` reads a regular file to EOF; the cap is enforced on the bytes read only after the
  read (`fileaccess/_read.py`, not in the allowed files).
- A FIFO header file can block in the ambient policy's open before the `fstat` check (handoff).
- `has_header` accepts pydantic lax bools (`"yes"`), unchanged from before.

## Gates

T1 `scripts/dev/gates.sh --keep-going --baseline` (VM, 983-file selection against `origin/main`
`45553d7e`):

- `eb2abfb3`: tests 3830 branch-only failures, all from csv sources without `has_header` in
  test helpers the codemod does not reach (fixed by hand at `2eeeb546`).
- `2eeeb546`: env, ruff, format, mypy PASS; sentry 4279 passed; licenses 63 passed; tests 34738
  passed, 2 failed (baseline 34683 passed, 0 failed). Branch-only: the two STOP 4 ids above.

- `e499d802` (after merging main `38951412` with SCAN-ML M1 and renumbering; 997-file
  selection): env, ruff, format, mypy PASS; sentry 4296 passed; licenses 63 passed; tests 34912
  passed, 0 failed (baseline 34855 passed, 0 failed); branch-only none.

The mutation runs at `eb2abfb3` still hold for the E1 source: later commits touch tests, docs and
the registry version, plus two merge fixes after main's derived-visibility merge (#408):
`build_declared_source` became a file-access entry point (it reaches the policy through
`build_profile_source`), and one #408 test declares `has_header`.
Registry renumbered at merge from 1.8.0 to 1.9.0 by scripts/dev/registry_renumber.py; main was at 1.8.0.
Registry renumbered at merge from 1.9.0 to 1.10.0 by scripts/dev/registry_renumber.py; main was at 1.9.0.
