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
- Kinds: `evidence` (`cli-local-1`), `run-result` (points at `RunResult` and `RunResultRead`), `sample-run/v1`;
  `scan-set`, `publication` and `checkpoint` are reserved. `scan-set` stays reserved because SCAN-MULTI S1a
  (`ScanSetCore`) is not on main; its merge points the entry at the model.
- Reasons at registry 1.10.0 (the next minor after main's 1.9.0; REG-RENUMBER renumbers at merge): runtime
  `record_invalid`, `record_corrupt`, `record_kind_reserved` (owner `RecordError`, one `_CODED` row in
  `reason_adapter.py`); warning `library_write_failed`, `job_log_write_failed`, `library_degraded`,
  `library_delete_skipped` (owner `LibraryEvent`). Fixture `reason-manifest-v1.10-lib-l1a.json`,
  `consumer-contract.json` restamped, one `docs/reasons.md` token moved.
- Public API: five names in `decoy_engine.__all__` (`DecodedRecord`, `OpaqueRecord`, `RecordEnvelope`,
  `RecordError`, `RecordNeedsNewer`); the functions are `decoy_engine.records.*`.
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
