# RESULT E1 build record: the run-result model and outcome

Status: record

Branch `feat/run-result-model`, base engine `origin/main` `55876436` (a docs-only commit on top of the handoff's `41a86f47`).
Rules consulted: universal, development-loop, cross-cutting-concerns, architecture, testing, and the local builder brief.

## Environment

Tests ran on a GCP test VM through `vm-run engine` (lock-faithful venv). The red-first run, the mutation runs and the
local lint, format and mypy checks ran on the devbox with the engine `.venv` and `PYTHONPATH=src` (pydantic 2.13.4,
Python 3.10); the VM queue was backed up behind other builders for over an hour.

## Remeasure

`origin/main` is `55876436`, one docs commit past `41a86f47`. `reasons/_record.py` was 409 lines at the base.
`src/decoy_engine/records/` does not exist (LIB L1a has not merged), so `records/_kinds.py` is untouched.
REGISTRY_VERSION is `1.5.0` and is unchanged.

## Placement

| New file | Directory and CODEMAP line | Neighbour module |
|---|---|---|
| `src/decoy_engine/run_result/__init__.py`, `_model.py`, `_outcome.py` | New package `run_result/`. CODEMAP "Directory Map" and `_MAP.md` have no row for it, so it needs a new row (added in fix round 1) | `reasons/` (the reason wire types the model reuses), `plan/` (the hash it exports) |
| `tests/unit/run_result/` | tests mirror the package (`tests/unit/<package>/`) | `tests/unit/plan/` |
| `tests/sentry/test_run_result_constructor.py` | `tests/sentry/` holds sentries; listed in `test_sentry_manifest.py` | `tests/sentry/test_reason_registry.py` |
| `tests/integration/compat_corpus/run_result/run-result-v1*.json` | moved here in fix round 3 (the handoff named `tests/fixtures/compat/`, which made two corpus locations) | `tests/integration/compat_corpus/manifest.json` artifacts |

Fan-out: `run_result/` is new (3 modules). No package over its ratchet gained a module.

## Red runs

Stubs first (empty `RunResult`, `derive_outcome` returning `succeeded`/0, `parse_payload` returning `(from_payload, None)`,
`pipeline_config_hash = None`), then the tests, on a scratch copy of the tree:
`45 failed, 26 passed`. The failures are assertions (round trip, status table, reason reads, write refusals, missing
`ValueError`). `test_public_api` and the sentry pass on the stub: the export is present on the stub, and the sentry
has nothing to flag until a builder exists (its seeded-violation self-test is what proves it can fail).

## Green runs

Local: `tests/unit/run_result`, `tests/sentry/test_run_result_constructor.py`, `test_reason_registry.py`,
`test_public_api.py`, `test_sentry_manifest.py`: all pass. VM runs are in the Gates section.

## Mutations

Each applied once, tests rerun, reverted (the tree was clean after each).

| Mutation | Result |
|---|---|
| AT-E1 add `message: str` | red, `test_every_field_is_value_free` |
| AT-E1 add `extra: dict[str, Any]` | red, same test |
| AT-E1 add `str` computed field | red, same test |
| AT-E1 add `field_serializer` | red, same test (`custom serializer`) |
| AT-E2 `model_copy(update=...)` in `run_result/_project.py` | red, `test_only_the_builder_constructs_a_run_result` |
| AT-E3 `extra="ignore"` on the write model | red, `test_write_strict_read_tolerant` (DID NOT RAISE) |
| AT-E3 enum-typed read model | red, `test_write_strict_read_tolerant` |
| AT-E3 `ReasonPayload` validator checks keys only | red, `test_reason_payload_is_checked_against_the_registry_on_write` |
| AT-E3b keep submitted values after parsing | red, `test_forged_semantics_read_as_the_registry_says` |
| AT-E3b accept malformed version syntax | red, `test_unsupported_reasons_read_as_null` |
| AT-E3b compare no registry major | red, `test_unsupported_reasons_read_as_null` |
| AT-E3b second version check in the read validator | red, `test_one_reason_parser` |
| AT-E3b strict `ReasonPayload` on the read side | red, cross-minor read tests |
| AT-E18 one enum left un-substituted | red, read tests (the read model rejects the unknown value) |
| AT-E18 read sub-model missing a field | red, `test_corpus_sample_round_trips` |
| AT-E4 drop `partially_delivered` | red, `test_outcome_table` |
| AT-E4 blocking check outranks a failure | red, `test_outcome_table` |
| AT-E5 hash that includes `sources` | red, `test_semantic_hash_is_plan_hash` |

The tree walks in AT-E1 and AT-E18 also carry seeded-violation self-tests (scratch models), and the
constructor sentry has a scratch-tree self-test.

## Gates

`scripts/dev/gates.sh --keep-going --baseline` on the VM, head `5c5c5fcd`: the env, ruff, format, mypy, sentry and
licenses steps printed no failure (the first run, on the earlier head, printed one sentry failure,
`test_no_ad_hoc_reason_codes`, for two reason-code strings in `_model.py`; they now come from registry symbols). The tests
step ran past 30 minutes behind other builders on the VM lock, so it was stopped before the baseline diff and no gates
summary block was printed. Replacement T1 selection, run on the devbox with `pytest-one` because `vm-run` queued for over
an hour: `tests/unit/run_result`, `test_public_api`, `tests/unit/plan`, `tests/unit/progress`, `tests/unit/safe_output`, the
F29 integration and security tests, the reason registry, release-pin and identity tests, and the sentries for reasons,
constructor, manifest, module size, package map and public import boundary:
`1841 passed, 10 skipped` (35 s). No baseline diff was computed.

## Files vs allowed list

All files are in the E1 list, plus the rows the E1 change needs in two keep-both ledgers
(`tests/sentry/test_sentry_manifest.py` SENTRIES, `tests/unit/test_public_api.py` expected set) and
`tests/unit/run_result/__init__.py`.

## Size

Hand-written added lines come to about 1170 against the handoff's 710 estimate and 800 budget: `_model.py` 374,
`_outcome.py` 56, `__init__.py` 10, `_record.py` +17/-7, tests 672 (`test_model.py` 518 after formatting, `test_outcome.py`
89, sentry 65), docs and ledger lines about 40; the JSON fixtures (125 lines, mostly generated) are extra. Each source file is
under 400 lines.

## DESIGN copies

None added. `read_reason` and `from_payload` share `parse_payload`. The `_derive_read` substitution map is the one place
that makes the read tree.

## V2-LOG

- Round 1: a read of a `schema` other than `decoy-run-result/v1` is now refused by the model. The `RecordNeedsNewer`
  wording for it still belongs to LIB L1a.
- Round 1: both models have `to_payload()` (alias dump); the models are alias-only (`pass`, no `passed`).
- Round 1: `Version` is a PEP 440 public release plus a lowercase local part. A CLI version outside that shape is refused.

## Not verified

The full suite, the E2/E3 builder, projection and collector, and any CLI consumer.

## Fix round 1

Source: Dennis and Codex findings on `2a4025e3`. Red runs are on the devbox against the old source plus a one-line
`Token = str` shim so the tests import (68 assertion failures of 146 tests; the rest already held):
`test_every_field_is_value_free`, `test_every_read_field_is_value_free`, `test_another_major_or_free_text_tag_is_refused`
(read), `test_the_tag_is_required_on_read_and_defaulted_on_write`, `test_lax_coercion_is_refused` (all 42 cells),
`test_the_count_key_has_one_spelling`, `test_both_models_dump_the_wire_form`, `test_other_version_shapes_are_refused`,
`test_read_refuses_free_text_where_an_enum_was` (7), `test_a_trailing_newline_is_not_a_version`,
`test_read_tree_derived_from_write_tree`, `test_tree_walk_sees_a_hand_written_read_tree`.

| Finding | Fix |
|---|---|
| HIGH any `schema` read | `SchemaTag` named type (only `decoy-run-result/v1`), kept out of the str substitution, required on read |
| Lax coercion | `Strict()` on `Count`, `Seconds`, `ExitCode`; new `Flag`; write and read |
| Bare marker trusted | the walk needs a kind from the module's named types and that type's validator in the field metadata; it also demands `Strict` on every int, bool and float |
| Read enums as bare `str` | `Token` (approved identifier) replaces them; the walk now runs over `RunResultRead` |
| Placement | CODEMAP Directory Map and Where Do I Find rows, `_MAP.md` row |
| Second corpus location | round 1 kept them in `tests/fixtures/compat/` and named that in contract section 6 (smaller than moving nine files and adding a manifest kind; they are hand-made reader samples with nothing to regenerate) |
| Private import | `safe_error_class` from `decoy_engine.progress` |
| `Outcome` export | removed from `run_result/__init__` (not in the handoff list); the type is reached as `run_result._outcome.Outcome` |
| LOW items | diagnostic-code null cell; alias-only models plus read `to_payload` (works on any pydantic 2, no 2.11 setting); PEP 440 `Version`; `in_place` comment; unknown values asserted kept; `\Z` on `_VERSION` |

`reasons/_record.py` also had `$` in `_SAFE_REF` and `_SAFE_IDENTITY`: `approve_ref("abc\n")` passed, so a trailing
newline rode through the `SafeRef` and `Token` types. Both now end in `\Z`.

Pydantic floor: `pyproject` says `>=2.0`, but the engine already imports `Discriminator` (2.5) in
`contracts/_declared_formats.py`, so the floor is not enforced. The new code uses only `Strict`, `StrictInt`, `create_model(__base__=...)`
and `model_dump(by_alias=True)`, all present in 2.0.

Mutations (new ones are 19 to 34; each turned a named test red): schema tag mapped to `str`; tag accepts any major; tag
defaulted on read; `Count`, `Flag`, `ExitCode`, `Seconds` lax; walk trusts a bare marker (seeded `_BareMarker` and
`_ForgedValidator` cells); read enum to bare `str`; `Token` accepts anything; `populate_by_name` back; read model without
`to_payload`; diagnostic nulling removed; old `Version` suffix; `_VERSION` and `_SAFE_REF` back to `$`. The earlier 18
were re-run on the new tree and are red. Not caught: the private `_progress_events` import (no sentry checks
imports inside `src/decoy_engine`; `telemetry/_vocab.py` does the same). Listed as a DESIGN item, not guarded here.

## Fix round 3

Commits on top of `75bb1f7e`. Test home for the new cases: `tests/unit/run_result/test_model_shapes.py` (117 lines; `test_model.py` stays at 776, under the 800 guideline).

| Item | Fix | Red on `75bb1f7e` |
|---|---|---|
| Two corpus locations | nine samples moved to `tests/integration/compat_corpus/run_result/`; contract 3.1 and 6, CODEMAP and this record updated. They stay hand-authored, have no manifest entry and no regeneration; the manifest-driven corpus test never read the directory, so no new kind was needed | `test_run_result_samples_live_in_the_corpus_subfolder` (module-level path check) |
| `\Z` untested | `test_an_identity_ending_in_a_newline_is_not_kept`, `test_write_side_refuses_an_identity_ending_in_a_newline` | green on `75bb1f7e` by design (the fix was already there); they guard it. Mutation `\Z` to `$` on `_SAFE_IDENTITY` turns the first one red |
| Hand-written `_VERSION_SHAPE` | `_canonical_release`: `packaging.version.Version`, `str(v) == v`, length cap 64, at least two release parts, no epoch | `test_a_version_must_be_canonical_and_epoch_free[01.0]` |
| `registry_version` type | `RegistryVersion` (kind `registry_version`) built on `reasons.REGISTRY_VERSION_RE`, the old private `_VERSION` renamed and exported from `decoy_engine.reasons` | `test_registry_version_refuses_wider_release_shapes` (4 cells), `test_registry_version_reuses_the_reasons_shape` |
| Read `to_payload` | `_ReadBase.to_payload` override with its own docstring; `test_the_read_dump_is_the_read_projection` shows the non-null reason shape without claiming it re-validates as `RunResult` | same test |
| Kind set pin | `EXPECTED_VALUE_FREE_KINDS` | `test_the_value_free_kind_set_is_pinned` |

Epoch decision: refused. Decoy never releases an epoch, so `1!2.0` can only be a mistake or a probe. Accepted: `0.18.0`, `0.1.0`, `0.18.1.dev3+g1a2b3c4.d20261106`, `1.2.3rc1`, `2.0.0.post1`, `1.0`. Refused: `1`, `v1.0.0`, uppercase, zero-padded, trailing whitespace or newline, over 64 characters.

Mutations, each red: `\Z` to `$` on `_SAFE_IDENTITY`; drop `str(parsed) == value`; drop the epoch check; `registry_version` back to `Version`; remove the `_ReadBase.to_payload` override; add a ValueFree kind.

Follow-ups, not done here:
- A repo-wide private-import sentry (`telemetry/_vocab.py:16` and `telemetry/_recorder.py:20` already import `_progress_events`).
- Other `$`-anchored validators: `connectors/postgres_snapshot.py:73`, `secret_providers/_types.py:43-44`.
