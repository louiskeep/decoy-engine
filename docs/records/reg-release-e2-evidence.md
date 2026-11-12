# REG-RELEASE E2 build evidence

Status: built on `feat/reg-release-e2`; `origin/main` merged on 2026-11-12 and the ceiling moved
to the minor main added (see "Merge of main, 2026-11-12"); the stop condition is closed by owner
decision.

Spec: `docs/plans/2026-11-11-reg-release.md` on `origin/plan/reg-release` (`c9d2eb72`). The plan
is not added to this branch; it stays on the plan branch.

## Heads and environment

- Base: `origin/main` `c96e28554613451f5f009849d8eaacbada880d0e`. No registry minor above
  1.23.0 had landed, so `LAST_PER_PR_MINOR` is `1.23.0` as planned.
- Red acceptance commit: `4ac1308a` (tests only).
- Final source head: `3f9576d7` (`feat(reasons): stamp the reason registry at release time`). This
  record is added in a later docs-only commit; no source changed after the mutations below.
- `python -V`: Python 3.11.18 (the `.venv`; the lock profile names CPython 3.10.20). PyArrow
  24.0.0, DuckDB 1.5.4, ruff 0.15.22 and mypy 2.3.0 in the gate venv.
- Gate venv: `uv sync --frozen --extra dev` lacks ruff and mypy (they are in the `lint` extra) and
  the `cloud` extra the strict sentry step expects, so T1 was rerun after
  `uv sync --frozen --extra dev --extra lint --extra vault --extra cloud` (the certified profile
  `release_checks.sh` syncs). Both runs are below.

## Legacy preflight

Before writing format 2, `reason_release_pin.py --migrate-v1` ran the v1 aggregate check
(`legacy_v1_violations`) and the full-entry historical fixture check
(`minor_fixture_violations(..., full=True)`) against the base tree: both returned `[]`. The
migrated pin keeps `registry_version` `1.22.0`, sets `baseline_ceiling` `1.23.0`, captures all
1076 numeric identities (since_counts as v1 plus `"1.23.0": 3`), and equals
`pin_text(migrate_v1(v1, live, fixtures, "1.5.0"))` byte for byte. A second `--migrate-v1` on the
v2 file refuses ("the pin is not in the v1 aggregate format").

## Red first (commit `4ac1308a`, source unchanged from base)

`uv run pytest -q -p no:cacheprovider -n 4 -rA <the seven Placement test files> -k "at1 or at2 or
at3 or at4 or at5 or at8 or to_unreleased or local_transition or private_registry or
release_checks_asks"`: 118 failed, 2 passed. Every failure is an absent API
(`reason_manifest_policy` has no `build_pin`, `allowed_registry_version`, `registry_transition`,
`pending_fragment_violations`, ...; `stamp_release` has no `stamp_release`;
`registry_renumber.py` has no `--to-unreleased`; `reason_release_pin.py` has no
`--release-ready`; `decoy_engine.reasons._types` has no `UNRELEASED`; `_registry.py` has no
`PENDING_SUMMARY_EDITS`) or a wrong outcome (the v1 pin CLI reports "stale"; the old
`release_checks.sh` never asks `--release-ready`; the single-source guard does not flag a branch
on a transition's pending sets). Red IDs (parametrized cases counted):

- `tests/sentry/test_reason_minor_fixtures.py::test_at5_an_exact_code_span_names_a_pending_identity` (1)
- `tests/sentry/test_reason_minor_fixtures.py::test_at5_an_unreleased_identity_needs_no_minor_fixture` (1)
- `tests/sentry/test_reason_minor_fixtures.py::test_at5_claims_are_read_from_changelog_d_fragments_only` (1)
- `tests/sentry/test_reason_minor_fixtures.py::test_at5_no_other_spelling_names_a_pending_identity` (11)
- `tests/sentry/test_reason_minor_fixtures.py::test_at5_prefix_collisions_claim_only_their_own_identity` (1)
- `tests/sentry/test_reason_policy_single_source.py::test_guard_flags_a_local_transition_branch` (1)
- `tests/sentry/test_reason_policy_single_source.py::test_release_checks_asks_the_policy_and_parses_no_registry_state` (1)
- `tests/unit/scripts/test_release_checks_fragments.py::test_at1b_release_checks_refuses_marked_edits_until_they_are_stamped` (1)
- `tests/unit/scripts/test_release_checks_fragments.py::test_at4_release_checks_passes_the_pin_steps_after_an_identity_stamp` (1)
- `tests/unit/scripts/test_release_checks_fragments.py::test_at4_release_checks_refuses_a_pending_registry` (3)
- `tests/unit/scripts/test_stamp_release_compile.py::test_at8_a_failed_candidate_check_writes_nothing` (1)
- `tests/unit/scripts/test_stamp_release_compile.py::test_at8_a_failure_while_applying_restores_every_owned_path` (4)
- `tests/unit/scripts/test_stamp_release_compile.py::test_at8_a_patch_release_moves_the_registry_and_keeps_the_ceiling` (2)
- `tests/unit/scripts/test_stamp_release_compile.py::test_at8_a_release_with_nothing_pending_recaptures_the_pin` (1)
- `tests/unit/scripts/test_stamp_release_compile.py::test_at8_an_identity_release_stamps_every_pending_code_at_the_next_minor` (1)
- `tests/unit/scripts/test_stamp_release_compile.py::test_at8_the_cli_accepts_only_the_automatic_registry_minor` (6)
- `tests/unit/test_reason_manifest_policy.py::test_at1_an_unreleased_identity_passes_every_ordinary_gate` (1)
- `tests/unit/test_reason_manifest_policy.py::test_at1_migration_keeps_the_shipped_version_and_baselines_through_the_ceiling` (1)
- `tests/unit/test_reason_manifest_policy.py::test_at1_migration_refuses_a_v1_or_full_fixture_mismatch` (1)
- `tests/unit/test_reason_manifest_policy.py::test_at1_since_counts_must_count_the_captured_identities` (4)
- `tests/unit/test_reason_manifest_policy.py::test_at1_the_reader_rejects_a_duplicate_key_and_non_json` (1)
- `tests/unit/test_reason_manifest_policy.py::test_at1_the_strict_v2_reader_rejects_a_malformed_pin` (33)
- `tests/unit/test_reason_manifest_policy.py::test_at1b_a_declared_pending_edit_passes_ordinary_gates_but_not_release_ready` (2)
- `tests/unit/test_reason_manifest_policy.py::test_at1b_a_numeric_tombstone_cannot_be_retargeted` (1)
- `tests/unit/test_reason_manifest_policy.py::test_at1b_a_patch_stamp_moves_the_registry_and_keeps_the_ceiling` (2)
- `tests/unit/test_reason_manifest_policy.py::test_at1b_every_other_field_stays_frozen_under_a_pending_edit` (12)
- `tests/unit/test_reason_manifest_policy.py::test_at1b_the_same_edits_without_markers_fail` (1)
- `tests/unit/test_reason_manifest_policy.py::test_at2_numeric_fields_are_frozen_per_field_and_pending_additions_are_not` (1)
- `tests/unit/test_reason_manifest_policy.py::test_at3_only_unreleased_additions_pass_the_ceiling` (1)
- `tests/unit/test_reason_manifest_policy.py::test_at3_the_allowed_version_is_the_shipped_version_or_the_last_per_pr_minor` (5)
- `tests/unit/test_reason_release_pin.py::test_at1_field_digests_are_sha256_over_canonical_json` (1)
- `tests/unit/test_reason_release_pin.py::test_at1_one_entry_writer_output_is_the_normative_golden` (1)
- `tests/unit/test_reason_release_pin.py::test_at1_the_checked_in_pin_is_v2_and_round_trips_byte_for_byte` (1)
- `tests/unit/test_reason_release_pin.py::test_at1b_a_marked_summary_and_a_first_deprecation_pass_check_together` (1)
- `tests/unit/test_reason_release_pin.py::test_at1b_unmarked_edits_fail_the_pin_cli` (1)
- `tests/unit/test_reason_release_pin.py::test_at2_a_changed_value_under_its_old_digest_fails_the_pin_cli` (1)
- `tests/unit/test_reason_release_pin.py::test_at2_a_flipped_digest_nibble_under_the_live_value_fails_the_pin_cli` (1)
- `tests/unit/test_reason_release_pin.py::test_at4_release_ready_refuses_each_pending_class_that_check_accepts` (3)
- `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` (1)
- `tests/unit/test_registry_renumber.py::test_to_unreleased_leaves_the_worktree_alone_when_the_candidate_fails` (1)
- `tests/unit/test_registry_renumber.py::test_to_unreleased_refuses_an_edit_to_an_existing_identity` (3)

Unexpectedly green on the base, not acceptance IDs: the two guard tests
`test_no_script_imports_the_private_registry_module` and
`test_private_registry_import_guard_flags_a_seeded_import` (the guarded property already held).
No named plan mutation covers them; on the final head each was turned red by an ad-hoc mutation
(a `from decoy_engine.reasons._registry import PENDING_SUMMARY_EDITS` line in
`reason_release_pin.py`; disabling the guard's match) and restored.

Test-only fixes after the red commit and before the mutations: the pin reader, writer and v1
migration moved to `reason_release_pin.py` (tests now call `rp.*`; `release_ready_violations`
takes `(live, pending)`), a frozen-`since` case used the identity's own version (changed to
`1.21.0`), an `AT-8` filter matched the existing `probe_recovered_full_frame` code, and the
`AT-10` uppercase check now reads only the converted docs line (the reasons doc itself documents
`since=UNRELEASED`).

## Mutations (final source head `3f9576d7`, each applied once, test run, `git checkout --`,
clean tree confirmed after each)

| ID | Row | Mutation | Result | First test that went red |
|---|---|---|---|---|
| 1a | AT-1 | remove the v2 format check | RED | `tests/unit/test_reason_manifest_policy.py::test_at1_the_strict_v2_reader_rejects_a_malformed_pin[format-1]` |
| 1b | AT-1 | replace the identities array with a count | RED | `tests/unit/test_reason_release_pin.py::test_at1_one_entry_writer_output_is_the_normative_golden` |
| 1c | AT-1 | accept an empty code | RED | `tests/unit/test_reason_manifest_policy.py::test_at1_the_strict_v2_reader_rejects_a_malformed_pin[empty-code]` |
| 1d | AT-1 | ignore a wrong since_counts total | RED | `tests/unit/test_reason_manifest_policy.py::test_at1_the_strict_v2_reader_rejects_a_malformed_pin[counts-total]` |
| 1e | AT-1 | baseline only identities at or below 1.22.0 | RED | `tests/unit/test_reason_manifest_policy.py::test_at1_migration_keeps_the_shipped_version_and_baselines_through_the_ceiling` |
| 1f | AT-1 | hash pretty JSON instead of canonical bytes | RED | `tests/unit/test_reason_release_pin.py::test_at1_field_digests_are_sha256_over_canonical_json` |
| 1g | AT-1 | flip one nibble of the golden summary digest | RED | `tests/unit/test_reason_release_pin.py::test_at1_one_entry_writer_output_is_the_normative_golden` |
| 1h | AT-1 | skip the legacy preflight | RED | `tests/unit/test_reason_manifest_policy.py::test_at1_migration_refuses_a_v1_or_full_fixture_mismatch` |
| 1b-a | AT-1b | exempt every field of a pending identity | RED | `tests/unit/test_reason_manifest_policy.py::test_at1b_every_other_field_stays_frozen_under_a_pending_edit[_deprecate-pending0-kind-warning]` |
| 1b-b | AT-1b | allow a non-null tombstone baseline | RED | `tests/unit/test_reason_manifest_policy.py::test_at1b_a_numeric_tombstone_cannot_be_retargeted` |
| 1b-c | AT-1b | route ordinary --check through release-ready refusal | RED | `tests/unit/test_reason_release_pin.py::test_at4_release_ready_refuses_each_pending_class_that_check_accepts[deprecation]` |
| 1b-d | AT-1b | make release-ready pass before stamping | RED | `tests/unit/test_reason_manifest_policy.py::test_at1b_a_declared_pending_edit_passes_ordinary_gates_but_not_release_ready[_deprecate-pending0]` |
| 1b-e | AT-1b/AT-8 | set baseline_ceiling to the live (shipped) version | RED | `tests/unit/test_reason_manifest_policy.py::test_at1b_a_patch_stamp_moves_the_registry_and_keeps_the_ceiling[_deprecate-pending0]` |
| 1b-f | AT-1b | leave the summary marker after stamping | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_a_patch_release_moves_the_registry_and_keeps_the_ceiling[summary]` |
| 2a | AT-2 | skip numeric entries in the checker | RED | `tests/unit/test_reason_manifest_policy.py::test_at2_numeric_fields_are_frozen_per_field_and_pending_additions_are_not` |
| 2b | AT-2 | digest all entries including UNRELEASED | RED | `tests/unit/test_reason_manifest_policy.py::test_at2_numeric_fields_are_frozen_per_field_and_pending_additions_are_not` |
| 2c | AT-2 | compare only field names without checking digests | RED | `tests/unit/test_reason_manifest_policy.py::test_at2_numeric_fields_are_frozen_per_field_and_pending_additions_are_not` |
| 3a | AT-3 | remove unreleased_violations | RED | `tests/unit/test_reason_manifest_policy.py::test_at3_only_unreleased_additions_pass_the_ceiling` |
| 3b | AT-3 | compute the allowed version from the pin alone | RED | `tests/unit/test_reason_manifest_policy.py::test_at3_the_allowed_version_is_the_shipped_version_or_the_last_per_pr_minor[1.22.0-1.23.0]` |
| 4a | AT-4 | drop the --release-ready invocation | RED | `tests/unit/scripts/test_release_checks_fragments.py::test_at4_release_checks_refuses_a_pending_registry[add_pending_summary]` |
| 4b | AT-4 | release-ready ignores pending identities | RED | `tests/unit/test_reason_release_pin.py::test_at4_release_ready_refuses_each_pending_class_that_check_accepts[identity]` |
| 4c | AT-4 | release-ready ignores pending deprecations | RED | `tests/unit/test_reason_release_pin.py::test_at4_release_ready_refuses_each_pending_class_that_check_accepts[deprecation]` |
| 4d | AT-4 | release-ready ignores pending summary markers | RED | `tests/unit/test_reason_release_pin.py::test_at4_release_ready_refuses_each_pending_class_that_check_accepts[summary]` |
| 4e | AT-4 | bypass the central policy with shell parsing | RED | `tests/sentry/test_reason_policy_single_source.py::test_release_checks_asks_the_policy_and_parses_no_registry_state` |
| 5a | AT-5 | remove the pending-code fragment check | RED | `tests/sentry/test_reason_minor_fixtures.py::test_at5_no_other_spelling_names_a_pending_identity[Adds config/code in prose.]` |
| 5b | AT-5 | search only CHANGELOG.md | RED | `tests/sentry/test_reason_minor_fixtures.py::test_at5_claims_are_read_from_changelog_d_fragments_only` |
| 5c | AT-5 | substring matching | RED | `tests/sentry/test_reason_minor_fixtures.py::test_at5_no_other_spelling_names_a_pending_identity[Adds config/code in prose.]` |
| 5d | AT-5 | accept a dotted spelling | RED | `tests/sentry/test_reason_minor_fixtures.py::test_at5_no_other_spelling_names_a_pending_identity[Adds `config.code`.]` |
| 8a | AT-8 | make deprecation-only unchanged | RED | `tests/unit/test_reason_manifest_policy.py::test_at1b_a_patch_stamp_moves_the_registry_and_keeps_the_ceiling[_deprecate-pending0]` |
| 8b | AT-8 | force the patch component to 1 | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_a_patch_release_moves_the_registry_and_keeps_the_ceiling[deprecation]` |
| 8c | AT-8 | remove the explicit-minor equality and state restriction | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_the_cli_accepts_only_the_automatic_registry_minor[summary-1.24.0-False]` |
| 8d | AT-8 | leave the ceiling at 1.23.0 after stamping new identities | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_an_identity_release_stamps_every_pending_code_at_the_next_minor` |
| 8e | AT-8 | use the dotted package version as the fixture slug | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_an_identity_release_stamps_every_pending_code_at_the_next_minor` |
| 8f | AT-8 | rewrite every matching string | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_an_identity_release_stamps_every_pending_code_at_the_next_minor` |
| 8g | AT-8 | omit no-op pin capture | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_a_release_with_nothing_pending_recaptures_the_pin` |
| 8h | AT-8 | disable rollback for the created fixture | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_a_failure_while_applying_restores_every_owned_path[7]` |
| 10a | AT-10 | emit JSON value "UNRELEASED" | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` |
| 10b | AT-10 | emit quoted catalog since="unreleased" | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` |
| 10c | AT-10 | emit bare uppercase UNRELEASED in Markdown | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` |
| 10d | AT-10 | omit changelog.d/*.md | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` |
| 10e | AT-10 | allow a marked existing-identity edit through conversion | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_refuses_an_edit_to_an_existing_identity[_first_deprecation-keys0]` |
| 10f | AT-10 | copy main's contract | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` |
| 10g | AT-10 | skip constant-import insertion | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` |
| 10h | AT-10 | apply before candidate verification | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_leaves_the_worktree_alone_when_the_candidate_fails` |
| G1 | guard | add a deprecation-only branch to stamp_release.py | RED | `tests/sentry/test_reason_policy_single_source.py::test_no_manifest_validator_lives_outside_the_policy_module` |
| G2 | guard | fixture sentry accepts a plain-text or prefix-collision claim | RED | `tests/sentry/test_reason_minor_fixtures.py::test_at5_no_other_spelling_names_a_pending_identity[Adds config/code in prose.]` |

All 46 named mutations turned their tests red; none stayed green. `1b-e` is the plan's "set
`baseline_ceiling` to live" patch version (AT-1b) and "set a patch transition's ceiling to live"
patch version (AT-8), one writer line. `4b`-`4d` are "make that mode ignore each pending class in turn". `G1`
and `G2` are the two guard mutations of "Central choke points".

## T1

Selected paths: every test file in the Placement table plus the repository guard set found by
grep for the changed symbols and files (68 files; the EVIDENCE block lists them); the sentry step
runs all of `tests/sentry` (module size, fan-out, reason registry, policy single source, minor
fixtures), the layers step runs `tests/sentry/layout/test_import_layers.py`.

```
uv sync --frozen --extra dev
scripts/dev/gates.sh --keep-going --steps ruff,format,mypy,sentry,layers,licenses,tests <68 paths>
```

Dev-only venv on `3f9576d7`: ruff, format and mypy fail with rc 127 (not installed); sentry 4964
passed, 4 failed, 21 errors (the residual-surface sentry below; `test_outbound_clients` and
`test_serial_resources` strict mode need the `cloud` extra); layers 15 passed; licenses 63
passed; tests 3226 passed, 0 failed, 84 skipped.

```
uv sync --frozen --extra dev --extra lint --extra vault --extra cloud
scripts/dev/gates.sh --keep-going --steps ruff,format,mypy,sentry,layers,licenses,tests <68 paths>
```

Certified-profile venv on `3f9576d7`: ruff PASS, format PASS, mypy FAIL (1 error,
`src/decoy_engine/quality/dp_provenance.py:517` cannot find `tomli` under Python 3.11), sentry
4988 passed, 1 failed (`tests/sentry/test_no_residual_removed_surface.py::test_no_residual_storm_surface_in_tracked_files`),
layers 15 passed, licenses 63 passed, tests 3226 passed, 0 failed, 84 skipped.

Failure sets, branch against main (`c96e2855` in a detached worktree, same venv):

- mypy `tomli` error: on main too (pre-existing; the venv is CPython 3.11, the lock profile 3.10).
- `test_no_residual_storm_surface_in_tracked_files`: branch only. See "Stop condition".
- `test_outbound_clients`, `test_serial_resources` strict mode: dev-only venv only; pass on the
  branch and on main in the certified profile.

## CLI paths on `3f9576d7`

- `python scripts/release/reason_release_pin.py --check`: "reason release pin current", exit 0.
- `python scripts/release/reason_release_pin.py --release-ready`: "reason release pin
  release-ready", exit 0.
- `bash scripts/release/release_checks.sh 0.22.0`: exit 1 at the pre-`uv` step, "changelog.d still
  holds uncompiled fragments" (E1's refusal, retained). The pin steps of the real script, including
  `--release-ready`, run in `test_release_checks_fragments.py` with a stub `uv` that builds a venv
  running the engine copy; a full lock sync was not run.
- `python scripts/dev/registry_renumber.py --check --main origin/main` on this branch: "registry
  consistent", exit 0. `--to-unreleased` runs through its real CLI in `test_registry_renumber.py`.
- `stamp_release.py` real CLI on an unshallowed scratch clone of `3f9576d7`:
  `--registry-minor 1.24.0` refused (no identity transition), tree unchanged; `--version 0.23.0
  --date 2026-11-12` stamped the no-op transition (pin `registry_version` 1.22.0 to 1.23.0,
  ceiling 1.23.0, eight fragments compiled), then the pin `--check` and `--release-ready` passed.
  This session's own checkout is shallow, so the stamp refuses there by E1's shallow-clone rule.

## Not run

- The full default suite (the orchestrator runs it on the merge head).
- `release_checks.sh` with a real lock sync past the pin steps.

## Stop condition

Closed by owner decision (PR #506 comment, 2026-11-12): one exact-path `EXCEPTIONS` entry for
`tests/fixtures/f29/reason-registry-release.json`, beside the `reason-manifest-v1.json` entry that
already carries the same two tombstone codes. `test_no_residual_storm_surface_in_tracked_files`
now passes. If the v1 manifest file is deleted later in this chain, its entry goes in that slice.
The text below is the condition as first reported.

`tests/sentry/test_no_residual_removed_surface.py::test_no_residual_storm_surface_in_tracked_files`
fails on the branch only. The plan's v2 pin stores each identity's `namespace` and `code`
verbatim, so `reason-registry-release.json` now carries the two route tombstone codes
(`route.run_storm`, `route.run_storm_requested`), which the residual sentry permits only in its
`EXCEPTIONS` list (`reason-manifest-v1.json` and the tombstone module are there). Passing it needs
a new `EXCEPTIONS` entry for the pin, which is an allowlist change, so it was not made. The plan
does not mention this sentry.

## Deviations from the Placement table and plan text

- `scripts/dev/_registry_renumber_git.py` (not in the table): its extractor also reports
  `pending_summary_edits()` (with a fallback for trees that predate it), so the stamp and the
  renumber tool read pending summaries through the public accessor of the tree they judge.
- The strict v2 reader, writer and the v1 migration live in `reason_release_pin.py`; every
  comparison against the live catalog stays in `reason_manifest_policy.py`. Keeping them in the
  policy module would take it past 600 lines (a new census entry); the policy module is at 600.
- `reason_release_pin.py --migrate-v1` is the one-time migration writer.
- `registry_renumber.py --check` rejects any numeric slice, so the existing AT-R tests that
  expected `--check` to pass after a numeric renumber now expect exactly the pending-era refusals
  (`registry-version-bump`, `branch-added-fixture`, `numeric-since`) and no older carrier message.
  The numeric renumber mode itself is unchanged until its separate cleanup.
- The v1 byte-identical message tests now target `legacy_v1_violations`, the retained v1 check.

## V2-LOG

- `registry_minor_violations` reads "is not the automatic auto (this release adds no identity)"
  for a patch or no-op state; clearer wording later.
- `reason_manifest_policy.py` sits exactly at the 600-line goal; the next rule needs a split.
- `stamp_release.py` loads `scripts/dev/registry_renumber.py` for the source rewrites; a shared
  home for the registry source helpers would remove the release-to-dev import.
- `RenumberFacts.pending_summaries` doubles as the "pending edits allowed" switch (None refuses).
- `--to-unreleased` runs ruff only when the environment has it (the `lint` extra); syntax and the
  converted tree's import always run.
- `uv sync --frozen --extra dev` alone cannot run `gates.sh` (ruff and mypy live in `lint`).
- mypy reports `tomli` missing under Python 3.11 on main too.

## Merge of main, 2026-11-12

`origin/main` `7fc12e7f` was merged into the branch (merge commit `a5d060b8`; no rebase, per the
handoff). Main had moved eight commits, among them #481, which registered the two FPE fallback
codes (`runtime.fpe_unencryptable_value`, `runtime.fpe_checksum_unsupported`) under the old per-PR
process: `REGISTRY_VERSION` 1.24.0 and `reason-manifest-v1.24-fpe-fallbacks.json`. The plan's
"Numeric ceiling and fixtures" covers this case, so the ceiling moved; no test cap was raised to
pass.

### Heads and environment

- Source head for every mutation: `aca975eb` (`fix(reasons): move the per-PR registry ceiling to
  1.24.0`). `4af6b39c` adds only the owner-approved sentry entry (a test file no mutation
  targets); T1 below ran on `4af6b39c`. This section is added in a later docs-only commit.
- `python -V`: Python 3.11.18. PyArrow 24.0.0, DuckDB 1.5.4, ruff 0.15.22, mypy 2.3.0,
  pytest 9.1.1; venv from `uv sync --frozen --extra dev --extra lint --extra vault --extra cloud`.

### Legacy preflight on main

In a detached worktree of `origin/main` `7fc12e7f` (same venv, removed afterwards), with main's own
code: `release_pin_violations(v1 pin, live)` returned `[]` and
`minor_fixture_violations(fixtures, live, "1.5.0")` (whole entries) returned `[]`; live was
registry 1.24.0 with 1078 identities. Main's own pin, fixture, policy, registry and single-source
tests passed there (94 passed).

### What changed

- `LAST_PER_PR_MINOR` is `1.24.0`.
- The pin was re-migrated, not hand-edited: main's v1 pin was restored to the tree and
  `reason_release_pin.py --migrate-v1` rewrote it after its own v1 and whole-entry fixture
  preflight passed on the merged tree. `registry_version` stays `1.22.0`, `baseline_ceiling` is
  `1.24.0`, 1078 numeric identities are captured, and `since_counts` gains `"1.24.0": 2`. The diff
  against the previous v2 pin is the ceiling, that bucket and the two new identity records; no
  existing digest changed. A second `--migrate-v1` refuses.
- Migration and ceiling assertions: the synthetic history in `test_reason_manifest_policy.py` is
  now shipped 1.22.0 with one per-PR minor at 1.23.0 and one at 1.24.0 (one fixture each), and the
  allowed-version table, the stamp tests and the release-check tests expect the transitions from
  allowed 1.24.0 (identity release 1.25.0 with `reason-manifest-v1.25-release-0-23-0.json`, patch
  1.24.1 then 1.24.2 with the ceiling kept at 1.24.0, no-op 1.24.0). The normative one-entry
  golden in `test_reason_release_pin.py` is the plan's bytes and is unchanged.
- `docs/reasons.md` names 1.24.0 as the last per-PR minor.
- `tests/sentry/test_no_residual_removed_surface.py`: the one owner-approved `EXCEPTIONS` entry.
- This record: one sentence above no longer spells a patch version between the fork point's
  registry version and the head's, which `registry_renumber.py --check` reports on a branch-added
  line as a leftover version now that the head is at 1.24.0.

Before these edits the merge head failed 24 E2 tests (the pin lacked the two 1.24.0 identities and
the allowed version was 1.23.0); after them the seven Placement test files plus
`test_reason_registry.py`, `test_qa_checklist_reasons.py` and `test_consumer_contract.py` pass
(296 passed).

### Mutations on the new head

Source head `aca975eb`. Each mutation was applied once in a detached worktree of that commit, its
test run, then `git checkout -- .`; the tree was confirmed clean before and after each.

| ID | Row | Mutation | Result | First test that went red |
|---|---|---|---|---|
| 1a | AT-1 | remove the v2 format check | RED | `tests/unit/test_reason_manifest_policy.py::test_at1_the_strict_v2_reader_rejects_a_malformed_pin[format-1]` |
| 1b | AT-1 | replace the identities array with a count | RED | `tests/unit/test_reason_release_pin.py::test_at1_one_entry_writer_output_is_the_normative_golden` |
| 1c | AT-1 | accept an empty code | RED | `tests/unit/test_reason_manifest_policy.py::test_at1_the_strict_v2_reader_rejects_a_malformed_pin[empty-code]` |
| 1d | AT-1 | ignore a wrong since_counts total | RED | `tests/unit/test_reason_manifest_policy.py::test_at1_the_strict_v2_reader_rejects_a_malformed_pin[counts-total]` |
| 1e | AT-1 | baseline only identities at or below 1.22.0 | RED | `tests/unit/test_reason_manifest_policy.py::test_at1_migration_keeps_the_shipped_version_and_baselines_through_the_ceiling` |
| 1f | AT-1 | hash pretty JSON instead of canonical bytes | RED | `tests/unit/test_reason_release_pin.py::test_at1_field_digests_are_sha256_over_canonical_json` |
| 1g | AT-1 | flip one nibble of the golden summary digest | RED | `tests/unit/test_reason_release_pin.py::test_at1_one_entry_writer_output_is_the_normative_golden` |
| 1h | AT-1 | skip the legacy preflight | RED | `tests/unit/test_reason_manifest_policy.py::test_at1_migration_refuses_a_v1_or_full_fixture_mismatch` |
| 1b-a | AT-1b | exempt every field of a pending identity | RED | `tests/unit/test_reason_manifest_policy.py::test_at1b_every_other_field_stays_frozen_under_a_pending_edit[_deprecate-pending0-kind-warning]` |
| 1b-b | AT-1b | allow a non-null tombstone baseline | RED | `tests/unit/test_reason_manifest_policy.py::test_at1b_a_numeric_tombstone_cannot_be_retargeted` |
| 1b-c | AT-1b | route ordinary --check through release-ready refusal | RED | `tests/unit/test_reason_release_pin.py::test_at4_release_ready_refuses_each_pending_class_that_check_accepts[deprecation]` |
| 1b-d | AT-1b | make release-ready pass before stamping | RED | `tests/unit/test_reason_manifest_policy.py::test_at1b_a_declared_pending_edit_passes_ordinary_gates_but_not_release_ready[_deprecate-pending0]` |
| 1b-e | AT-1b/AT-8 | set baseline_ceiling to the live (shipped) version | RED | `tests/unit/test_reason_manifest_policy.py::test_at1b_a_patch_stamp_moves_the_registry_and_keeps_the_ceiling[_deprecate-pending0]` |
| 1b-f | AT-1b | leave the summary marker after stamping | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_a_patch_release_moves_the_registry_and_keeps_the_ceiling[deprecation]` |
| 2a | AT-2 | skip numeric entries in the checker | RED | `tests/unit/test_reason_manifest_policy.py::test_at2_numeric_fields_are_frozen_per_field_and_pending_additions_are_not` |
| 2b | AT-2 | digest all entries including UNRELEASED | RED | `tests/unit/test_reason_manifest_policy.py::test_at2_numeric_fields_are_frozen_per_field_and_pending_additions_are_not` |
| 2c | AT-2 | compare only field names without checking digests | RED | `tests/unit/test_reason_manifest_policy.py::test_at2_numeric_fields_are_frozen_per_field_and_pending_additions_are_not` |
| 3a | AT-3 | remove unreleased_violations | RED | `tests/unit/test_reason_manifest_policy.py::test_at3_only_unreleased_additions_pass_the_ceiling` |
| 3b | AT-3 | compute the allowed version from the pin alone | RED | `tests/unit/test_reason_manifest_policy.py::test_at3_the_allowed_version_is_the_shipped_version_or_the_last_per_pr_minor[1.22.0-1.24.0]` |
| 4a | AT-4 | drop the --release-ready invocation | RED | `tests/unit/scripts/test_release_checks_fragments.py::test_at4_release_checks_refuses_a_pending_registry[add_pending_identity]` |
| 4b | AT-4 | release-ready ignores pending identities | RED | `tests/unit/test_reason_release_pin.py::test_at4_release_ready_refuses_each_pending_class_that_check_accepts[identity]` |
| 4c | AT-4 | release-ready ignores pending deprecations | RED | `tests/unit/test_reason_release_pin.py::test_at4_release_ready_refuses_each_pending_class_that_check_accepts[deprecation]` |
| 4d | AT-4 | release-ready ignores pending summary markers | RED | `tests/unit/test_reason_release_pin.py::test_at4_release_ready_refuses_each_pending_class_that_check_accepts[summary]` |
| 4e | AT-4 | bypass the central policy with shell parsing | RED | `tests/sentry/test_reason_policy_single_source.py::test_release_checks_asks_the_policy_and_parses_no_registry_state` |
| 5a | AT-5 | remove the pending-code fragment check | RED | `tests/sentry/test_reason_minor_fixtures.py::test_at5_no_other_spelling_names_a_pending_identity[Adds config/code in prose.]` |
| 5b | AT-5 | search only CHANGELOG.md | RED | `tests/sentry/test_reason_minor_fixtures.py::test_at5_claims_are_read_from_changelog_d_fragments_only` |
| 5c | AT-5 | substring matching | RED | `tests/sentry/test_reason_minor_fixtures.py::test_at5_no_other_spelling_names_a_pending_identity[Adds ` config/code`.]` |
| 5d | AT-5 | accept a dotted spelling | RED | `tests/sentry/test_reason_minor_fixtures.py::test_at5_no_other_spelling_names_a_pending_identity[Adds `config.code`.]` |
| 8a | AT-8 | make deprecation-only unchanged | RED | `tests/unit/test_reason_manifest_policy.py::test_at1b_a_patch_stamp_moves_the_registry_and_keeps_the_ceiling[_deprecate-pending0]` |
| 8b | AT-8 | force the patch component to 1 | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_a_patch_release_moves_the_registry_and_keeps_the_ceiling[deprecation]` |
| 8c | AT-8 | remove the explicit-minor equality and state restriction | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_the_cli_accepts_only_the_automatic_registry_minor[identity-1.26.0-False]` |
| 8d | AT-8 | leave the ceiling at the per-PR minor after stamping new identities | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_an_identity_release_stamps_every_pending_code_at_the_next_minor` |
| 8e | AT-8 | use the dotted package version as the fixture slug | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_an_identity_release_stamps_every_pending_code_at_the_next_minor` |
| 8f | AT-8 | rewrite every matching string | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_an_identity_release_stamps_every_pending_code_at_the_next_minor` |
| 8g | AT-8 | omit no-op pin capture | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_a_release_with_nothing_pending_recaptures_the_pin` |
| 8h | AT-8 | disable rollback for the created fixture | RED | `tests/unit/scripts/test_stamp_release_compile.py::test_at8_a_failure_while_applying_restores_every_owned_path[5]` |
| 10a | AT-10 | emit JSON value "UNRELEASED" | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` |
| 10b | AT-10 | emit quoted catalog since="unreleased" | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` |
| 10c | AT-10 | emit bare uppercase UNRELEASED in Markdown | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` |
| 10d | AT-10 | omit changelog.d/*.md | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` |
| 10e | AT-10 | allow a marked existing-identity edit through conversion | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_refuses_an_edit_to_an_existing_identity[_marked_summary-keys1]` |
| 10f | AT-10 | copy main's contract | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` |
| 10g | AT-10 | skip constant-import insertion | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` |
| 10h | AT-10 | apply before candidate verification | RED | `tests/unit/test_registry_renumber.py::test_to_unreleased_converts_an_identity_addition_branch` |
| G1 | guard | add a deprecation-only branch to stamp_release.py | RED | `tests/sentry/test_reason_policy_single_source.py::test_no_manifest_validator_lives_outside_the_policy_module` |
| G2 | guard | fixture sentry accepts a plain-text or prefix-collision claim | RED | `tests/sentry/test_reason_minor_fixtures.py::test_at5_no_other_spelling_names_a_pending_identity[Adds config/code in prose.]` |

All 46 named mutations turned their tests red; none stayed green. The first attempt at `8e` was an
invalid edit (a syntax error, so the module did not import); it was corrected and rerun, and the
row above is the valid run. `8d` leaves the ceiling at `LAST_PER_PR_MINOR`.

### T1 on `4af6b39c`

```
scripts/dev/gates.sh --keep-going --baseline --steps ruff,format,mypy,sentry,layers,licenses,tests <46 paths>
```

ruff PASS; format PASS; mypy FAIL (1 error, `src/decoy_engine/quality/dp_provenance.py:517`
cannot find `tomli` under Python 3.11); sentry 5080 passed, 0 failed, 2 skipped (all of
`tests/sentry`, including the residual-surface, module-size, fan-out, lane, QA-checklist and
reason sentries); layers 15 passed; licenses 63 passed; tests 1228 passed, 0 failed, 200 skipped.

Failure sets, branch against `origin/main` `7fc12e7f` (detached worktree, same venv):

- Branch: the mypy `tomli` error only. Main: the same error. Branch-only: none.
- The same run on `aca975eb`, before the sentry entry, had one branch-only failure,
  `test_no_residual_storm_surface_in_tracked_files`; main's sentry step passed (5059 passed).

Paths: `tests/fileaccess/protected_dir/test_protected_dir.py` `tests/native/test_c1b_i_metadata_inventory.py` `tests/native/test_c6a_fpe_chunked.py` `tests/native/test_crypto_ext_contract.py` `tests/native/test_crypto_ext_fpe_empty.py` `tests/parity/test_gap9_native_thread_grant.py` `tests/parity/test_ooc_fpe_native_real.py` `tests/parity/test_ooc_fpe_native_routing.py` `tests/parity/test_out_of_core_route_seam_parity.py` `tests/parity/test_phase_d_d2c_fpe_real.py` `tests/parity/test_phase_d_d2c_fpe_seq.py` `tests/parity/test_phase_d_fpe_int_key_seq.py` `tests/parity/test_phase_d_fpe_key_seq.py` `tests/physical/test_shadow_fpe.py` `tests/security/test_f06_quarantine_boundary.py` `tests/unit/config/test_reason_registry_config_explicit_e1.py` `tests/unit/config/test_scan_ml_reasons.py` `tests/unit/execution/test_auto_chunk_bookkeeping.py` `tests/unit/execution/test_b6a_bookkeeping.py` `tests/unit/execution/test_b6b_bookkeeping.py` `tests/unit/execution/test_fpe_fail_closed_pipeline.py` `tests/unit/execution/test_fpe_remap_out_of_charset_fail_closed.py` `tests/unit/limits/test_hard_limits_sweep.py` `tests/unit/profile/test_reason_registry_config_explicit_e2.py` `tests/unit/progress/test_consumer_contract.py` `tests/unit/progress/test_versioned_contract.py` `tests/unit/records/test_registry.py` `tests/unit/run_result/test_model.py` `tests/unit/run_result/test_model_shapes.py` `tests/unit/safe_output/test_platform_reasons.py` `tests/unit/safe_output/test_value_domains.py` `tests/unit/scripts/test_check_fragment.py` `tests/unit/scripts/test_fragment_grammar.py` `tests/unit/scripts/test_release_checks_fragments.py` `tests/unit/scripts/test_stamp_release_compile.py` `tests/unit/scripts/test_unreleased_text.py` `tests/unit/test_changelog_breaking_ratchet.py` `tests/unit/test_cut_release_script.py` `tests/unit/test_reason_manifest_policy.py` `tests/unit/test_reason_registry.py` `tests/unit/test_reason_registry_subset_run.py` `tests/unit/test_reason_release_pin.py` `tests/unit/test_registry_renumber.py` `tests/unit/test_shipped_reason_identities.py` `tests/unit/test_version_agreement.py` `tests/unit/verification/canary/test_canary_reasons.py`.

### CLI paths on `4af6b39c`

- `reason_release_pin.py --check`: "reason release pin current", exit 0.
- `reason_release_pin.py --release-ready`: "reason release pin release-ready", exit 0.
- `bash scripts/release/release_checks.sh 0.22.0`: exit 1 at the pre-`uv` step, "changelog.d still
  holds uncompiled fragments" (E1's refusal). The pin steps of the real script run in
  `test_release_checks_fragments.py` with a stub `uv`.
- `python scripts/dev/registry_renumber.py --check --main origin/main`, run on the commit that
  carries this record: "registry consistent", exit 0. On `4af6b39c` it exited 1 on the leftover
  version in this record described under "What changed".

### Not run

- The full default suite (the orchestrator runs it on the merge head).
- `release_checks.sh` with a real lock sync past the pin steps.
- The `stamp_release.py` CLI on an unshallowed scratch clone (this checkout is shallow); its CLI
  parsing path runs in `test_stamp_release_compile.py`.
