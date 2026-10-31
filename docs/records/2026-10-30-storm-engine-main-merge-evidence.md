---
Status: current
---
# STORM engine close-out: merge of `origin/main` (F29) into `claude/eager-clarke-14deh1`

Merge commit `a5152bf` (parents `aefed7d` branch tip, `b509c7e` origin/main). Main had moved one
commit past the handoff's `fdd08a8` (#346, a docs-only plan-status sync); the same five files
conflicted. `git merge-tree --write-tree origin/main HEAD` after the merge: 0 conflicts;
`git merge-base origin/main HEAD` = `b509c7e`.

## Environment (same container for both sides)

`uv run --frozen` from `uv.lock`; Python 3.11.17, mypy 2.3.0, ruff 0.15.22. The branch ran in
the checkout, origin/main in a separate `git worktree` with its own locked venv.

## The five listed conflicts (what each side changed; how both are kept)

| File | Branch (STORM removal / phase envelope) | Main (F29) | Resolution |
|---|---|---|---|
| `CHANGELOG.md` | Unreleased: "Changed (BREAKING, pre-GA: STORM removed ...)" | Unreleased: "Added (F29 ...)" | Both entries, branch first, plus a merge note on the registry unification |
| `execution/_chunked_output_sink.py` | `from decoy_engine.config import post_scan_integrity_requested` | `from decoy_engine import reasons` (+ observer import, auto-merged) | Both imports |
| `execution/capacity.py` | same import | same import | Both imports |
| `execution/_pipeline_routing.py` | `run_storm` -> `post_scan_integrity` kwarg, literal reasons renamed | literal reasons -> `reasons.route.*` symbols, `RUN_STORM_REQUESTED` | Typed symbols under the branch names: `reasons.route.POST_SCAN_INTEGRITY_REQUESTED`, `POST_VALIDATION_REQUESTED`, `MIXED_GENERATE_MASK_CHUNKED` |
| `tests/sentry/test_physical_seam_disconnection.py` | `_RETIRED`, `_only_these_line_changes`, `exact_line_changes` / `exact_ok` for `_output_projection.py`, STORM wording removed from three comments | allowlist restructured as `DELIBERATELY_CONNECTED | F29_REGISTRY_MIGRATION | {...}` (re-indented) | Main's version verbatim (F29_REGISTRY_MIGRATION kept exactly), branch additions grafted; diff vs main = 43 lines, the only removals are the seven STORM-wording comment lines |

## Sixth conflict git did not flag (class 10, frozen-contract drift; class 9 scope drift)

Both sides created `decoy_engine.reasons`: the branch as `src/decoy_engine/reasons.py`
(`decoy-reasons/v1`, six identities, `ReasonRef` / `require_known` consumed by `phase.py`), F29 as
the `src/decoy_engine/reasons/` package (868 identities, pinned manifest). Python loads the package,
so the branch module would have been shadowed and `decoy_engine.phase` would not import.

Resolution, one registry:

- `reasons/_identity.py` carries the branch's identity form unchanged in meaning (`REGISTRY_SCHEMA`,
  `ReasonRef`, `UnknownReasonError(DecoyError, ValueError)`, `get_reason`, `require_known`,
  `REASONS` as a read-only `(namespace, code) -> ReasonSpec` view) and resolves against the F29
  catalog. `ReasonSpec.ref` is added so `get_reason(...).ref` keeps working.
- The phase identities are F29 catalog entries: new namespaces `pre_scan` (`profile_failed`) and
  `post_scan` (`check_error`, `scan_crashed`, `pass_failed`, owner `decoy_engine.phase`), and
  `route.post_scan_integrity` (owner `_chunked_output_sink`).
- `route.run_storm_requested` -> `route.post_scan_integrity_requested` is a hard rename, not a
  tombstone: the branch's released CHANGELOG line says exactly this, the engine policy is pre-GA hard
  delete, and the F29 build record (items 2 and 12) says the manifest pin is regenerated until the
  first release and only the 841 baseline identities are frozen; `run_storm_requested` is one of the
  27 unfrozen additions. The pin was regenerated with a script whose layout reproduces main's pin
  byte for byte (checked on the worktree: 868 entries identical). Live registry: 873 identities,
  14 namespaces, `registry_version` still `1.0.0`, `check_integrity() == []`.
- `DecoyError` moves to the leaf `decoy_engine/_error_base.py` and is re-exported from
  `decoy_engine.errors` (same object, same public path) so the registry can derive its exception
  from the engine base without the `errors -> reasons -> errors` cycle.
- `phase.py` is byte-identical to the branch tip. Wire shapes `phase-result/v1`, `phase-event/v1`
  and the `{namespace, code}` reason form are unchanged.

Owner call to confirm at the delta gate: the hard rename of a one-day-old F29 identity, and the
`UnknownReasonError` / `DecoyError` placement.

## Main's tests brought onto the branch's contract (class 9)

- `tests/integration/_f29_jobs.py`, `test_f29_outcomes.py`, `tests/unit/progress/test_retry_checkpoint.py`:
  config `version: 2`; `{"run_storm": True}` -> `{"post_scan": {"integrity": True}}`;
  `quality_metrics["storm_postmask"]` -> `["post_scan_integrity"]`.
- `tests/security/test_f29_remediation.py`: `post_scan_integrity=True` -> `post_scan_integrity_requested`.
- `tests/sentry/test_reason_registry.py`: `PhaseContractError` and `UnknownReasonError` listed as
  unclassified families with their rationale; `phase._reason_wire` listed as the dict-payload adapter
  that exports an identity `require_known` has just resolved. No assertion weakened.
- `docs/reasons.md` (namespaces, identity form, one STORM wording), `CODEMAP.md` reasons row.

## Codex r2 low (record vs canary test)

`test_accepted_field_content_round_trips_unchanged` now carries a per-field reader and asserts exact
equality of the exported value against `to_wire` and `canonical_bytes` for all 11 fields; the
exact-payload test at the old line 361 is untouched; the close-out record describes the test that way.
No change to `phase.py`.

## Gates (branch = merge commit tree; main = `b509c7e`)

| Gate | origin/main | merged branch |
|---|---|---|
| `pytest tests/unit/phase tests/sentry -q` | 3077 passed, 1 failed, 1 skipped (no `tests/unit/phase` on main) | 3361 passed, 1 failed, 1 skipped, 2 xfailed, 5 xpassed |
| the 1 failure | `test_proof_manifest_is_up_to_date`: `docs/proof-manifest.json` stale by the same 18 Faker sample values in this environment on both trees; pre-existing, not merge-related, left alone | same |
| F29 files (governor x2, isolated_safe_error, progress/, unit test_reason_registry, 4 integration f29, security f29_remediation + reason_boundary) | 268 passed | 283 passed; with `test_post_scan_integrity_in_run`, `test_job_estimate`, `test_public_api` added: 298 passed |
| `ruff check .` / `ruff format --check .` | clean / 1748 formatted | clean / 1760 formatted |
| `mypy src/decoy_engine testflight` | 17 errors in 8 files (643 files) | 17 errors in 8 files (651 files) |
| module-size sentry | pass | pass; `_pipeline_routing.py` 699 LOC = census 699, no census change |
| residual STORM sentry | n/a (branch-only) | pass on the merged tree |
| `git diff origin/main --stat` | | 363 files, +3583 / -952; files changed vs main that the branch alone did not change are exactly the registry files, the four F29 test files, the F29 sentry allowlist and `docs/reasons.md` listed above |

## Mutations (each new mechanism once; restored afterwards, tree clean)

| # | Mutation | Red? |
|---|---|---|
| M1 | `phase._wire` exports `"changed-" + check_id` | yes: canary exact-value test 1 failed, 10 passed (the old marker-presence form passed this) |
| M2 | `get_reason` returns a spec for an unknown identity | yes: 11 failed in `tests/unit/phase` |
| M3 | route code `post_scan_integrity_requestedx` | yes: 3 failed (manifest pin, routing resolve, remediation) |
| M4 | `run_storm` token appended to a registry module | yes: residual sentry 1 failed |
| M5 | extra line committed in `_output_projection.py` with the F29 allowlist entry for that file removed | yes: seam byte-identity test fails naming the file; with the branch's exact line only, it passes. Note: as merged, main's `F29_REGISTRY_MIGRATION` already permits this file wholesale, so the branch's exact-line guard is dormant until that entry is dropped; kept exactly as on main per the handoff |
| M6 | one phase identity removed from the live registry | yes: `test_v1_manifest_is_append_only` 1 failed |

## Not run

The full regression gate and the parity / physical / native suites; CLI and platform repins (the
owner's release steps). The proof-manifest regeneration was not committed (environment-dependent values).
