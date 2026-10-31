---
Status: current
---
# STORM engine close-out: fixes from the Codex gate on the main merge (addendum)

Base: `ab29ef0c` (merge commit `a5152bf1`). Fix commit: `f41e1ad1`; this record and one doc
wording fix follow it. `phase.py` is byte-identical to `aefed7d8` (595 lines); the wire shapes
`phase-result/v1` and `phase-event/v1` are untouched. `git merge-tree --write-tree origin/main HEAD`
(origin/main at `a6e5c252`) reports no conflicts.

## Environment

Same container for every number below. Branch: `uv sync --frozen --all-extras` venvs on Python
3.10.20 (`.venv310`), 3.11.17 (`uv run --frozen`, the default) and 3.12.3 (`.venv312`).
Baseline `fdd08a89` (F29): a `git worktree` with its own locked venvs on 3.10.20 (default pick) and
3.12.3. mypy 2.3.0, ruff 0.15.22. Free disk at start: 29 GB. The worktree is removed at the end.

## HIGH 1 (class 10, frozen-contract drift): a shipped identity was deleted

Checked every wire value the merge or the branch renamed against `git show v0.16.0` and `v0.15.1`
(neither tag has the F29 registry; the reason surface there is the literal strings):

| v0.16.0 value | surface | disposition |
|---|---|---|
| `run_storm_requested` | route reason (`_pipeline_routing.py:231`), F29 identity on main | tombstone `route.run_storm_requested`, `deprecated_in` 1.0.0, `replaced_by` `route.post_scan_integrity_requested` |
| `run_storm` | resident reason of the chunked output sink (`_chunked_output_sink.py:123`) | tombstone `route.run_storm`, `replaced_by` `route.post_scan_integrity` (now a registry identity) |
| the other 25 route / sink literals (`no_relationships` ... `streaming_disabled`, `eligible`) | same two deciders | resolve live under their shipped names; the nine sink literals F29 had not registered are now `route` identities (`reasons/route/_sink.py`) |
| `storm_postmask` quality key, `storm-post-mask/v1` schema, `storm.scan` event, `run_storm` config key, config `version: 1` | not reason identities (no registry namespace holds them) | renamed by the branch's released breaking change; outside the registry's append-only rule; listed in CHANGELOG |

`Declarer` now takes `deprecated_in` / `replaced_by`, so the tombstones use the registry's own
mechanism (`check_integrity` verifies the replacement is a declared identity). The residual STORM
sentry gets three exact, documented exceptions (the tombstone module, the pinned manifest, the
shipped-identity table); `_excepted("src/decoy_engine/anything.py")` is still false.

Test, red first on ab29ef0c: `tests/unit/test_shipped_reason_identities.py` (27 v0.16.0 route
reasons must resolve live or as a tombstone linking to a live replacement; the pinned manifest
carries both). On ab29ef0c, 11 of the 27 did not resolve.

Manifest: 884 identities, 14 namespaces, 2 tombstones, `registry_version` 1.0.0 (pre-release
regeneration per the F29 build record; byte-identical to `reasons.manifest()`).

## HIGH 2 (class 10, frozen-contract drift): forced out-of-core rejection changed type

`seq_ineligible(message)` / `ooc_ineligible(message)` in `execution/_errors.py` return exactly
`decoy_engine.errors.ConfigError`, tagged with the typed identity through F29's own capture channel
(`reason_adapter.tag_exception`, the capability-held record that `reason_from_exception`, the
worker envelope and every `FailureScope` already read first). No `.code` attribute is set, so the
pinned trace (`code: null`, type `ConfigError`, module `decoy_engine.errors`) is reproduced exactly.
`SeqIneligibleError` / `OocIneligibleError` are removed; importers were `_pipeline_routing.py`,
`reason_adapter._FIXED`, the governor comment, and three F29 tests (`test_governor.py`,
`test_governor_orchestration_kills.py`, `test_isolated_safe_error.py`), all updated to assert the
exact type plus the identity. The two pinned trace tests are green without edits; on `fdd08a89`
they fail (2 failed), as the gate said.

## MEDIUM (class 8, weak test): dormant exact-line sentry

`_output_projection.py` is removed from `F29_REGISTRY_MIGRATION`; the exact-line check is its only
allowance. Mutate-and-revert (committed temporarily, since the guard diffs commits): an extra line
in `_output_projection.py` fails `test_production_execution_modules_are_byte_identical_to_origin_main`
naming the file; the branch's exact one-line change alone passes. The tree was reset afterwards.

## LOW

- CHANGELOG: the F29 entry now states 868 identities across 12 namespaces when F29 landed and 884
  across 14 (2 tombstones) after the merge; the merge note describes the tombstones.
- Sink literal: every `decide_output_mode` return uses a `reasons.route.*` symbol. Why the scanner
  missed it: `ROUTE_DECIDER` matched only `decide_.*route` names. Class fix: every `decide_*`
  function is a decider (the AST survey shows `decide_output_mode` is the only other `decide_*`
  function returning literal reasons). Seeded mutant `decider_not_named_route` added; red on the
  old regex.
- `DecoyError.__module__ = "decoy_engine.errors"`; `tests/unit/test_error_base_pickle.py` pickles
  `DecoyError` and `ConfigError` and checks the global name (red first: module was
  `decoy_engine._error_base`).

## Python 3.10 canaries

`_error_envelope` sets `exc.__notes__ = [...]` instead of `add_note`. Assertions unchanged. On
ab29ef0c under 3.10: 2 failed, 80 passed; now 82 passed. Non-vacuity: with `phase.py` temporarily
exporting `__notes__` (reverted; byte-identical afterwards) the note marker is caught (3 failed).

## Mutations (each new mechanism once; restored, verified byte-identical to the backups)

| # | Mutation | Red? |
|---|---|---|
| M1 | tombstone declaration removed | yes: 1 failed, 28 passed |
| M2 | raise site no longer tags the ConfigError | yes: 6 failed (typed-identity tests); trace tests stay green, as they only pin the type |
| M3 | scanner regex back to `decide_.*route` | yes: `decider_not_named_route` 1 failed |
| M4 | sink literal reinstated | yes: `test_no_ad_hoc_reason_codes` 1 failed |
| M5 | `__module__` assignment removed | yes: 2 failed |
| M6 | note removed from the canary fixture | not red (the assertion is "no leak", so an absent note is vacuous); M6b above proves the note is read |
| M7 | unlisted line in `_output_projection.py` (committed temporarily) | yes: 1 failed |

## Gates on the fix commit

Exact commands: `<venv>/bin/python -m pytest tests/unit/phase tests/unit/progress
tests/unit/test_reason_registry.py tests/unit/test_public_api.py tests/unit/execution tests/sentry -q`

| Interpreter | result | failing ids |
|---|---|---|
| 3.10.20 | 8 failed, 9761 passed, 1166 skipped, 2 xfailed, 5 xpassed | 4x `test_c5c_a_unchanged_jobs::test_job_output_is_unchanged`, 2x `test_faker_strategy` (provider_config / warm-cache identity), `test_proof_manifest_is_up_to_date`, and `test_no_residual_storm_surface_in_tracked_files` (this record's first wording of `docs/reasons.md` named the retired codes; reworded, 10 passed on 3.10, 3.11 and 3.12 afterwards) |
| 3.12.3 | 10 failed, 9759 passed, 1166 skipped, 2 xfailed, 5 xpassed | the same, plus `test_categorical_weighted.py::TestCdfBuilder::test_skewed_weights_skew_cdf` and `test_out_of_core_stream_join_reorder.py::test_close_releases_run_file_fd_on_partial_consumption` |
| 3.11.17, targeted (trace, governor x2, isolated_safe_error, reason sentries, shipped identities, pickle, phase, residual, seam, module-size, hierarchy, public API, progress) | 1255 passed, 1 skipped, 2 xfailed, 5 xpassed | |

Every failing id above fails on `fdd08a89` in this environment: on 3.10 the seven Faker-value and
proof-manifest ids (7 failed, 7 passed when run by id); on 3.12 those plus the CDF and fd ids
(9 failed). The Faker-value ids (`test_c5c_a_unchanged_jobs`, `test_faker_strategy`,
`test_proof_manifest`) compare pinned Faker samples that differ in this container's Faker data;
not touched.

Other gates: ruff check clean, ruff format clean (1764 files); mypy 16 errors in 7 files on 3.10
(`fdd08a89` on 3.10: 16 in 7; the 3.11 venv shows 17 because the 3.10-only `tomli` backport is not
installed there, on both trees); module-size sentry passes, `_pipeline_routing.py` stays at census
699; `test_exception_hierarchy` passes.

Full suite (`pytest tests -q`, 3.11 on the branch; 3.10 on `fdd08a89`): still running when this
record was first committed; the failing ids and their baseline proof are appended in the follow-up
commit.

## Not run

The parity / physical / native suites under their own markers beyond what `tests` collects by
default; CLI and platform repins. The proof manifest and the Faker-value snapshots were not
regenerated (environment-dependent values).
