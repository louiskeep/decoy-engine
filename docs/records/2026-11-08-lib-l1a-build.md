# LIB L1a build record: records module

Status: record

Branch `feat/lib-l1a-records`, base engine `origin/main` `a5eb66e2` (registry 1.9.0). Risk R2. Plan: CLI repo
`docs/plans/2026-11-05-local-record-library.md`, section L1a-engine, taken from `origin/plan/local-record-library`
(Revision 7); it is not on CLI main yet.

## What changed

- New package `src/decoy_engine/records/` (about 1 200 lines in 11 files, none over 265): `_canonical.py` (the one
  canonical JSON form, `record_id`, `payload_digest`), `_envelope.py`, `_codec.py` (`build`, `encode`, `decode`),
  `_registry.py` and `_kinds.py` (one `KindSpec` per kind), `_payloads.py` (evidence and sample-run models),
  `_read_model.py` (tolerant read models), `_errors.py`, `_export.py` (`export_lines`, `read_export`), `_joblog.py`.
  Both plan parts (L1a.1 and L1a.2) are in this branch.
- Kinds: `evidence` (`cli-local-1`), `run-result` (points at `RunResult` and `RunResultRead`), `sample-run/v1`,
  `scan-set` (points at `ScanSetCore` and `ScanSetCoreRead` since SCAN-MULTI S1a is on main; path and locator fields from the marker registry);
  `publication` and `checkpoint` are reserved.
- Reasons at registry 1.20.0 (the next minor after main's 1.9.0; REG-RENUMBER renumbers at merge): runtime
  `record_invalid`, `record_corrupt`, `record_kind_reserved` (owner `RecordError`, one `_CODED` row in
  `reason_adapter.py`); warning `library_write_failed`, `job_log_write_failed`, `library_degraded`,
  `library_delete_skipped` (owner `LibraryEvent`). Fixture `reason-manifest-v1.20-lib-l1a.json`,
  `consumer-contract.json` restamped, one `docs/reasons.md` token moved.
- Public API: four names in `decoy_engine.__all__` (`DecodedRecord`, `OpaqueRecord`, `RecordError`,
  `RecordNeedsNewer`); `RecordEnvelope` and the functions are `decoy_engine.records.*`. Deviation from the plan's
  five: `decoy_engine/__init__.py` sat at 590 lines and five names took it to 602, over the 600 goal.
- Docs: `compatibility-contract.md` section 3.1, `CODEMAP.md`, `_MAP.md`, `CHANGELOG.md`, corpus samples in
  `tests/integration/compat_corpus/records/`.
- No filesystem access, so no `FileAccess` entry point and no file-access sentry change (plan 4.14).

## Deviations from the plan

- Evidence path fields are `/payload/pipeline_path`, `/payload/input_fingerprints/*/path` and
  `/payload/output_fingerprints/*/path`. The plan's `sources/*/path` and `targets/*/path` do not exist in the
  manifest `build_manifest` writes.
- Failure codes `record_invalid`, `record_corrupt` and `record_kind_reserved` are registered (the plan names them in
  the tests but lists only the four warning codes for the manifest). `RecordNeedsNewer` is a decode result, not an
  error, so it has no code.
- `decode` also returns an `OpaqueRecord` for a reserved kind, since the version has no model for it.
- A1-id-stable: the cross-machine fixture was recorded on the devbox, not another machine; the test also runs a
  second process with a random hash seed.
- Red-first with a stub was not done; tests and code were written together and the mutation table below stands in.
- `_read_model.py` repeats the shape of `run_result._derive_read` (private there, and it substitutes enums with
  tokens, which these payloads do not use). DESIGN item: fold both onto one helper when `run_result` is next touched.

## Open items

- `evidence` has no `signature*` fields in its model; the CLI does not write them today. A signed manifest would be
  refused by the write model until the field is added.
- Error locations come from pydantic and are reduced to identifier-shaped segments; a caller key that is not
  identifier-shaped prints as `?`.
- The CLI half of A1-canonical-shared (`evidence.compute_manifest_hash` calling `records.canonical_bytes`) belongs to
  the CLI slice; the engine test pins the byte form against a copy of the CLI function.

## Security review fixes

| Id | Fix | Red test (red without the fix) |
|---|---|---|
| SEC-1 | Without `keep_paths`, job logs are not exported and are listed under `unstripped` (`log` key); log lines can name paths and this version cannot strip them | `test_sec1_logs_stay_out_without_keep_paths_and_are_listed` |
| SEC-2 | Without `keep_paths`, `labels.pipeline` is reduced to its file name for every record (known, newer, opaque) and the id is recomputed | `test_sec2_label_pipeline_path_is_reduced_and_the_id_follows` |
| SEC-3 | A newer-envelope record must still carry a well-formed id and kind, comes back with `verified=False`, and `read_export` turns it into a `BadLine`, so no importer path upserts by an unverified id | `test_sec3_newer_envelope_is_unverified_and_never_read_as_an_export_record` |

## Mutations (VM, each applied once and reverted)

All red on the named test: MA1-unsorted, MA1-skip, MA1-raw-bytes, MA1-ascii, MA1-extra, MA1-read-strict,
MA1-reencode-view, MA1-unknown-raise, MA1-float, MA1-open, MA1-trust, MA1-crash, MA1-hidden, MA1-copy-result,
MA1-evidence-drift, MA1-unregistered, MA1-batch, MA1-export-view, plus ME-no-remap, ME-no-strip, ME-keep-id,
MJ-argv, MJ-trunc, SEC-1, SEC-2, SEC-3. A first pass showed MA1-ascii, MA1-open and MA1-hidden green because of a
mutation-script pattern hitting a docstring, a duplicated reserved-kind guard and a wrong `-k`; the script and the
guard were fixed and all three went red.

## Gate round 1 fixes

| Id | Fix | Red test (red on 4e57038f, green now) |
|---|---|---|
| B1 | Reason codes are registry symbols (`reasons.runtime.RECORD_*`) everywhere in `records/`; the string `_CODES` set is a frozenset of the three symbols; `RecordError.__init__` is a listed forwarding adapter in the reason sentry | `tests/sentry/test_reason_registry.py::test_no_ad_hoc_reason_codes` |
| H1 | `_dump` validates `producer`, `labels` and each link through their models, and the id is computed over that stored form, so a `None` label no longer makes an undecodable record | `test_a_none_label_is_dropped_before_the_id_is_computed` |
| H2 | One deriver, `_value_free.derive_read`, with a substitution hook (run results swap reason payloads), required tag kinds and exact `Literal` fields (records keep `schema_version`, `producer`, the schema tags). `run_result` and `records/_read_model.py` both call it; both copies are gone. Records now read a later enum value as a checked token | `test_a_later_enum_value_under_the_same_payload_tag_still_reads`, `test_there_is_one_deriver` |
| H3 | The run-result value-free types moved to `src/decoy_engine/_value_free.py` (names still importable from `run_result._model`). Envelope, labels, producer, job-log header and sample-run names use `SafeName`, `SafeRef`, `Version`, `UtcTimestamp`, `InvocationId` (uuid4), `RelPath`, `Sha256`, `Count`. Only the inherited `cli-local-1` names and paths, and sample-run machine paths (export reduces them), stay loose, with a comment | `test_the_envelope_uses_the_shared_value_free_types`, `test_labels_hold_names_and_refs_only`, `test_invocation_id_is_a_uuid4`, `test_sample_run_names_are_safe_names` |
| M1 | Tests that kill two surviving mutants: the `payload_digest` check and the duplicate-key hook (both mutants applied on VM, each now red) | `test_a_stale_payload_digest_is_corrupt_even_when_the_id_matches`, `test_a_duplicate_key_is_corrupt_even_when_the_last_value_verifies` |
| M2 | Every `RecordError` is raised after its `except` block closes, so no validator or JSON error (whose input holds the caller's text) stays reachable as `__context__` | `test_a_record_error_keeps_no_cause_or_context` (13 failure paths) |
| M3 | Withheld logs go in the header field `withheld_logs`, not `unstripped`; a log owner must be an invocation id on write (`record_invalid`) and on read (`BadLine`) | `test_withheld_logs_have_their_own_header_field`, `test_a_log_owner_must_be_an_invocation_id`, `test_a_log_line_with_a_bad_owner_is_a_bad_line` |
| M4 | Corpus samples for `decoy-record/v1`, `sample-run/v1`, `decoy-library-export/v1` and `decoy-job-log/v1` under `tests/integration/compat_corpus/records/`, read by `tests/unit/records/test_corpus.py` | `test_decoy_*_sample_*` in `test_corpus.py` |
| L2 | `export_lines` documents that callers filter out non-v1 envelopes (`RecordNeedsNewer`) before passing `raw` values | docstring |

Deviation recorded for M3: SEC-1 stays as built (no `--include-logs-without-keep-paths` flag in the engine).
Without `keep_paths`, logs are withheld and named in `withheld_logs`. Any CLI flag that includes logs without
`keep_paths` is a deviation from the plan until the log lines can be stripped like payloads.

Test input changes forced by H3, not weakened assertions: the SEC-2 test builds with a relative
`labels.pipeline` (`proj/pipelines/claims.yaml`) because the plan says the pipeline label is project-relative and
`RelPath` refuses an absolute one; the SEC-1 test uses an invocation id as log owner.

### V2-LOG (not fixed this round)

L1, L3, L4 and the remaining low findings from gate round 1 are logged here as V2-LOG items: none breaks
function, loses data or leaks to a non-admin.
Registry renumbered at merge from 1.10.0 to 1.18.0 by scripts/dev/registry_renumber.py; main was at 1.17.0.

## Merge onto main (2026-11-09)

- Registry renumbered to 1.20.0, the next minor after main; fixture is `reason-manifest-v1.20-lib-l1a.json`.
- `scan-set` is now a writable kind pointing at `ScanSetCore` and `ScanSetCoreRead` (SCAN-MULTI S1a is on main).
- `run_result/_model.py`: kept the `_value_free` split and ported main's `ConfigRuleError` validator errors into
  `_value_free.check` and `_check_reason`; the message baseline entry moved to `_value_free.py`.
- New sentries from main applied: `\Z` end anchors in `_envelope.py` and `_registry.py`; one `validated()` choke
  point in `records/_codec.py` for caller-data model validation (the export producer and header used to raise
  a raw pydantic error).

## Review round (2026-11-09)

- A default export carries no raw locator: scan-set path fields are reduced, bucket and endpoint are removed and the
  record is listed under `unstripped`; a record the exporter cannot strip is left out and listed under
  `withheld_records` in the header. `keep_paths` keeps the old copy.
- One tag parser (`family_major`), one JSON-object parse (`json_object`) and typed header fields for the envelope,
  the export header and the job-log header; a missing, malformed or older version is refused.
Registry set to 1.20.0 at merge, the next free minor after the pending E2a slice (#441); the renumber script only knows main, so this was done by hand.
