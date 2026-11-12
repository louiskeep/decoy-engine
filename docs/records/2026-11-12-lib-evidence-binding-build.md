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

## Fix round 1

Finding: the public decoder accepted a sealed evidence record with only one binding key, `path_base: null` or `path_base: "cwd"`. `derive_read` built the read model from fields only, so the write model's validators were dropped and the `Literal` became a tolerant `Token`.

Fix: `_value_free.SharedRules` is a marker base for a validators-only class. `derive_read` builds the read model on `(base, *rules)`, where `rules` are the `SharedRules` classes in the write model's MRO. It fails closed with `TypeError` when the write model holds a validator no rules class has, or a rules class declares a field. `EvidencePayload` inherits `_BindingRules`, which holds the pair rule and a field rule that accepts only `"project"`. `CODEMAP.md`, the compatibility contract and the changelog fragment name the reader-side refusal.

Red first: commit `438952c4` (tests plus a `SharedRules` stub so the import resolves) on top of `d6c1508f`: 12 failed, 28 passed (4 decode, 4 read-model, 4 deriver tests failed on assertions).

Green: `tests/unit/records`, `tests/unit/run_result`, `tests/unit/test_value_free_rules.py`, `tests/sentry/test_module_size.py`, `tests/sentry/test_public_import_boundary.py`; `gates.sh --steps layers` 15 passed; ruff check and format, mypy on `_value_free.py` and `records`: clean.

Mutations on the final source head:

| Mutation | Red tests |
|---|---|
| drop `*rules` from the read bases | 4 decode, 4 read-model, `test_both_validators_run_on_the_read_model`, `test_a_nested_write_models_rules_reach_its_nested_read_model` |
| drop the fail-closed checks | `test_a_validator_straight_on_a_write_model_is_refused`, `test_a_rules_class_with_a_field_is_refused` |
| field rule accepts any non-null string | decode and read-model `other_path_base` |

## Fix round 2

Two gaps in the fail-closed check. First, it compared validator names, so a write model (or a non-rules base ahead of the rules class in the MRO) could redefine a rules validator's name and the writer and reader would run different callables. The check now decides by owner: a validator name is shared only when the first class in the MRO that holds it in its own namespace is a rules class. Second, a rules class that set `model_config` would override the read base's `extra="ignore"` and `frozen=True`; it now raises `TypeError`. The module and `derive_read` docstrings say a rules class holds validators only.

Red first on `8a6c076f` plus the new tests (commit `ced`-prefixed test commit before the fix): 3 failed, 6 passed. Red ids: `test_a_write_model_redefining_a_rules_validator_name_is_refused`, `test_a_non_rules_base_before_the_rules_class_cannot_shadow_a_name`, `test_a_rules_class_with_config_is_refused`. The control `test_control_a_rules_owned_shape_derives_ignores_unknown_and_is_frozen` passed throughout.

Green: `tests/unit/test_value_free_rules.py` 9 passed; with `tests/unit/records` and `tests/unit/run_result` 406 passed. The owner check fired on no existing write model.

| Mutation | Red tests |
|---|---|
| owner check replaced by the old name subtraction | the redefined-name test and the non-rules-base test |
| `model_config` check dropped | `test_a_rules_class_with_config_is_refused` |

Pydantic 2.0.x behaviour was not checked; only public Python and the key names of `__pydantic_decorators__` are used.
