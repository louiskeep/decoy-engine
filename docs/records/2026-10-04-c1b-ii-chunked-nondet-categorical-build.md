# C1b-ii chunked seeded non-deterministic categorical: build record

Status: record

Date: 2026-10-04. Plan: `docs/plans/2026-10-04-c1b-ii-chunked-nondet-categorical.md` revision 5. Branch `feat/c1b-ii-chunked-nondet-categorical` off engine main `d93e3bde` (C1b-i merged). Risk R2. Gates pending at the time of writing: re-gate by dennis, then Codex final.

## What shipped

A seeded non-deterministic `categorical` runs on the native chunked route. Each non-null row is drawn at its global position (`base_row_offset` plus the local index, as a `uint64` key column fed to the existing `derive_index_batch`), so native chunked equals the oracle chunked route and the whole-frame run. No new Rust.

Admission has two stages. Stage A is config-only (namespace, explicit all-string categories, no `from_profile`, a buildable CDF) and is shared by the compatibility veto, `_static_route_decision`, `plan_column_backends` and `prepare_chunked_categoricals`. Stage B is the source dtype and picks the leg: a `string` source runs the native kernel, anything else runs the chunked oracle.

## Remediation after the dennis build-gate

The first build made stage B a fail-closed raise (`reject_non_string_positional_sources`). The gate found a BLOCKER: a seeded non-deterministic categorical over an int64 or float64 source ran full-frame before, but hard-errored once `run_pipeline` auto-routed it to the chunked route, with no mid-run fallback. `auto_chunk` must be a transparent optimization, so the decision was reversed (plan revisions 4 and 5).

- Removed `reject_non_string_positional_sources` and its call in `native/_dispatch.py`. A non-string source now takes the existing `real_type_rejection` downgrade (`categorical_source_type_not_string:<col>:<type>`) to the chunked oracle leg, the same path the deterministic variant uses. The oracle leg runs the seeded draw keyed by `ctx.row_offset` and is the parity reference.
- The pre-existing `chunked_leading_null_type` gate (`native/_chunk_schema.py`) is untouched. A null-typed first chunk followed by a typed chunk raises it for every chunked strategy.
- Migrated the stage-B tests in `tests/native/test_chunked_nondet_categorical_admission.py` from "raises `categorical_nondeterministic_not_chunk_safe`" to "runs the oracle leg": no raise, `native_admitted` false, executed backend `pandas_oracle`, output equal to a forced-oracle run and reproducible. The stage-A config-veto tests and the deterministic non-string reroute test are unchanged.
- Added `tests/native/test_chunked_nondet_categorical_auto_route.py`, the missing end-to-end guard: `run_pipeline` with a low `auto_chunk_threshold_rows`, per source dtype {string, int64, float64, all-null}, uniform and weighted. The job must succeed and equal the full-frame run of the same config on values, column order and Arrow field types; string reports `rust_companion`, non-string reports `pandas_oracle`, both `mode='chunked'`. It does not assert IPC or schema-metadata identity, because the dispatcher drops schema metadata. A null-first-then-typed source asserts the pre-existing `chunked_leading_null_type`, and that the deterministic variant raises the same.
- Sentries: `_chunked.py` census 616 to 618 in `tests/sentry/test_module_size.py`; `native/_categorical_positional.py` added to `permitted_non_physical` in `tests/sentry/test_physical_seam_disconnection.py` (it imports nothing from `execution.physical`).

## Evidence

Red-before: on the pre-remediation code, the end-to-end probe raised `PlanCompileError categorical_nondeterministic_not_chunk_safe` for int64, float64 and all-null sources and passed for string. After the fix all four succeed and equal the full-frame output.

Suites (companion venv, `PYTHONPATH=src`, `pytest-one`): auto-route 16, admission 106, parity 123, positional kernel 30, deterministic admission 50, C1b-i route regression 19, `tests/sentry` 2240 passed, 1 skipped.

Changed-unit coverage (line and branch, over the C1b-ii suites plus `test_dispatch_faker.py`): `native/_categorical_positional.py` 100%. The categorical branch of `_real_type_admission.py` is covered; its remaining misses are the hash, faker and bucket_perturb branches this slice does not touch. `_dispatch.py` misses are in untouched paths.

Hand-mutation (both killed): (1) `categorical_source_type_rejection` returns None for every type, so non-string sources reach the native kernel: 27 failures. (2) `positional_config_of_entry` drops the deterministic exclusion: 2 failures. Re-adding the raise is covered by the red-before run above. Earlier mutation passes on the offset handoff, zero-row accounting and evidence classification stand (commit `9fffdd9e`).

Divergence from the plan: none.
