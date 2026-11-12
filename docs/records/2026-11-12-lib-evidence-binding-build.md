Status: record

# LIB evidence project binding build (2026-11-12)

CLI #200 writes `path_base: "project"` and `project_manifest` into the evidence manifest for a run inside a `decoy.yaml` project. `EvidencePayload` forbade extras, so `records.build("evidence", ...)` refused those manifests.

## What changed

- `records/_payloads.py`: `EvidencePayload` gains optional `path_base` (`Literal["project"]`, an explicit null is refused) and `project_manifest` (`RelPath | None`). A before-validator refuses one key without the other and names the missing key only. Both default to `None` but are never stored by default: the stored form is `exclude_unset`, so absent stays absent and an explicit null stays.
- `records/_kinds.py`: `/payload/project_manifest` joins `_EVIDENCE_PATHS`, so a default export reduces it to a file name like `pipeline_path`. `path_base` exports unchanged.
- Corpus: new hand-authored `evidence-cli-local-1-project.json`; the two older samples are untouched.
- Docs: one paragraph in `docs/compatibility-contract.md`; fragment `changelog.d/lib-evidence-binding.md`. No reason code added.

## Dump paths checked

`_codec.project` (`exclude_unset`) is the only dump of the evidence payload. `_kinds._strict_dump` serves descriptors and layouts, and the diff and export paths read the stored dict. No change was needed there.

## Tests

`tests/unit/records/test_evidence_binding.py`, 19 cases. Red first: 15 failed before the model change. Green after. Broad run (records, compat_corpus, sentry): 5219 passed; the only failures are 9 `test_import_layers` cases that need import-linter, absent from the VM venv.

## Mutations

| Mutation | Red tests |
|---|---|
| remove `project_manifest` from the model | build, null, enum, export, canary, corpus |
| `exclude_none` in the dump | build, null, export, corpus |
| drop `exclude_unset` | build, null, absent, corpus, older-samples |
| `path_base: str` and no null check | enum |
| pairing validator off | pair (both) |
| `project_manifest` out of `_EVIDENCE_PATHS` | export, canary |

## Chosen

The pairing refusal names the missing key's field path, since the pair has no single culprit. The model-level check raises a `ValidationError` with a custom error so the location is a field path and no value is echoed.
