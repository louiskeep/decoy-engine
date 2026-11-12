# Release 0.19.0 prep: build record

Status: record
Branch `chore/release-0.19.0-prep`, from engine `origin/main` `b4c6915d`. Facts for the consumers are in
`docs/records/release-r0190-facts.md`; this record covers what changed and how it was checked.

## Pins moved

| Pin | File | Old -> new |
|---|---|---|
| Version | `pyproject.toml`, `src/decoy_engine/__init__.py`, `uv.lock` root entry | `0.18.0` -> `0.19.0` |
| Proof manifest stamp | `docs/proof-manifest.json` `engine_version` | `0.18.0` -> `0.19.0` (only line that moved) |
| CHANGELOG | `CHANGELOG.md` | `[Unreleased]` content promoted to `[0.19.0] - 2026-11-07`; Breaking list "since v0.18.0" |
| CHANGELOG pin | `tests/fixtures/changelog_released_sections.json` | gains `0.18.0` -> `ab60536e...` (no 0.19.0 row) |
| CHANGELOG triage | `tests/unit/test_changelog_breaking_ratchet.py` | two 0.19.0 `TRIAGED` keys (declared casting refusal; `column_names` type refusal) |
| Reason release pin | `tests/fixtures/f29/reason-registry-release.json` | `1.2.0`/893/`8effa84c...` -> `1.6.0`/1006/`cabb82cb...` |
| DP dev row | `src/decoy_engine/quality/dp_provenance.py` `_CERTIFIED_STACKS` | adds `7d1c1a2a...` (77 dists); `b9b42047...`, `31ecf945...`, `5a76ea92...` kept |
| DP CLI row | same | adds `795732eb...` (65 dists); `b643894d...`, `a7b40d54...` kept |
| Prior-row test | `tests/unit/generation/test_dp_prior_release_rows.py` | adds `_V0_19_0_ROWS`; every released row stays admitted |
| FR0 manifest | `tests/fixtures/phase_f_fr0/manifest.json` | `engine_minor` `0.18` -> `0.19`, 15 envelope digests; no golden table digest moved |
| Module-size census | `tests/sentry/test_module_size.py` | not changed: `dp_provenance.py` stays 605 lines (comments merged) |
| Cut script | `scripts/release/cut-release.sh`, its test | not changed: no check needs the new version |

No test pin or ratchet was raised to make room for growth. The `0.18.0` grep (`git grep -n '0\.18\.0'` outside
CHANGELOG, records, plans, `uv.lock`) found only the pins above plus historical comments in `dp_provenance.py` and
`test_changelog_breaking_ratchet.py`.

## Deviations from the handoff

- The dependency set moved since v0.18.0 (#394), so the dev row differs from the released `31ecf945...` row by five
  packages as well as the engine; recorded in the facts record. No proof-critical library moved, so no STOP.
- The reason pin is captured at registry 1.6.0, not 1.5.0 (#389 added `config.subset_run_unsupported`).
- The Breaking list carries four entries beyond the subset refusal (plan version 3, registry range, headerless
  refusal wording, `column_names` type refusal). They were already in `[Unreleased]` as Added/Changed text.

## Checks

Run on GCP VM `decoy-test-6` (CPython 3.10.20 lock venv) through `vm-run`, on the snapshot of head `2592b44a`
(the release commit; later commits add records only).

| Run | Result |
|---|---|
| `tests/unit/test_changelog_breaking_ratchet.py`, `tests/unit/generation/test_dp_prior_release_rows.py`, `tests/unit/test_cut_release_script.py`, `test_version_agreement.py`, `test_reason_release_pin.py`, `test_public_api.py`, `test_pinned_generation.py`, `tests/native/test_native_version.py`, the four `tests/parity/test_phase_f_fr0_*.py` files, all of `tests/sentry` | 4393 passed, 4 skipped, 4 failed on a malformed reason pin fixture (a stray `wrote ...` line from my capture); fixed, then `test_reason_release_pin.py`, `test_version_agreement.py`, `test_dp_lock_row.py`, `test_module_size.py` re-run: 865 passed |
| `scripts/dev/gates.sh --keep-going --baseline` (T1) | env, ruff, format, mypy, sentry, licenses OK; branch tests (impact selection, 3197 s): 2 failed, 34236 passed, 4599 skipped, 6 xfailed, 65 xpassed. The baseline half (main) was stopped after the two failures below were understood: both pass on main (pin 1.2.0) and fail only because of this branch's pin |
| `reason_release_pin.py --check` | `reason release pin current: registry 1.6.0, 1006` |
| `mypy src/decoy_engine/quality/dp_provenance.py` | no issues |

### STOP: two tests assume live registry is newer than the release pin

- `tests/unit/safe_output/test_platform_reasons.py::test_registry_version_is_at_least_the_floor` asserts
  `Version(reasons.REGISTRY_VERSION) > Version(pin["registry_version"])`.
- `tests/unit/verification/canary/test_canary_reasons.py::test_since_is_unreleased_registry_version` asserts
  `Version(live) > Version(pinned)`.

They are true between releases and false at the moment a release re-captures the pin at the live version (1.6.0 ==
1.6.0), which is what this PR must do (`test_reason_release_pin.py` requires the pin to be current). Neither test
was edited. The fix is a test-design call (for example compare against the pin of the previous release, or allow
equality when the pin was captured by a release commit), not a mechanical one, so it is left to the orchestrator.

## Review round 1 (Dennis GO-WITH-FIXES)

- CHANGELOG `[0.19.0]`: added the merges since v0.18.0 that had no entry: #366 (subset lossless keys, as a Breaking
  bullet), #396 (NER click and model wheel pin), #365 / #370 / #371 (canary types, normalization, typed Arrow text),
  #368 (`safe_output`), #369 (`telemetry`), #377 (registry 1.5.0 namespaces, 71 identities), #382 (private
  isolated-run payload) and #384 (GCS bounds). #384 changes what a consumer catches: GCS connector calls and GCS
  profile reads now raise `TransientError` or `PermanentError` with fixed text, and `google.auth` transport errors
  moved from permanent to transient, so it also has a Breaking bullet. The other merges since the tag are CI, dev
  tooling, refactors and docs with no consumer-visible change. The ratchet test passes; no TRIAGED key was added
  and the released-sections fixture has no 0.19.0 row. The dangling "See" in the F06 entry is fixed.
- `scripts/release/reason_release_pin.py` `pin_violations` now checks every released bucket (each pinned
  `since_counts` key at or below the pinned version, plus live keys at or below it), so a bucket that drops to 0 is
  reported. New mutant in `test_pin_mutants_are_rejected`: all `since 1.3.0` entries moved to 9.9.0 under registry
  9.9.0. It failed before the fix (`assert False`) and passes after.
- `test_canary_reasons.py`: the since test pins the literal `"1.3.0"` and requires the pin count to equal 12 exactly.
  The fixed-version seeded test is unchanged.
- `release-r0190-facts.md`: the CLI pin-site table lists the two comment-only references.

## Not done

- Mutations on the final head, hosted CI, the full suite and the cut: the orchestrator or a fresh agent.
- Merge of `origin/main` and re-derivation of the dev row if RESULT or ENGINE-PAR P2 merge first.
