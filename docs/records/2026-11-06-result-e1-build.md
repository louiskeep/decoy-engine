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
| `src/decoy_engine/run_result/__init__.py`, `_model.py`, `_outcome.py` | New package `run_result/`. CODEMAP "Directory Map" and `_MAP.md` have no row for it, so it needs a new row (not added: those files are outside the allowed list) | `reasons/` (the reason wire types the model reuses), `plan/` (the hash it exports) |
| `tests/unit/run_result/` | tests mirror the package (`tests/unit/<package>/`) | `tests/unit/plan/` |
| `tests/sentry/test_run_result_constructor.py` | `tests/sentry/` holds sentries; listed in `test_sentry_manifest.py` | `tests/sentry/test_reason_registry.py` |
| `tests/fixtures/compat/run-result-v1*.json` | the handoff names this directory (new; `tests/integration/compat_corpus/` is the older corpus) | `tests/fixtures/f29/` |

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

- A read of a `schema` major other than `/v1` is not refused in E1 (the field reads as a string); the `RecordNeedsNewer`
  refusal belongs to LIB L1a.
- `RunResultRead` has no `to_payload`; callers dump with `by_alias=True` (the `pass` count key is an alias).
- `Version` accepts a bounded release suffix; tighten if the CLI version format is fixed.

## Not verified

The full suite, the E2/E3 builder, projection and collector, and any CLI consumer.
