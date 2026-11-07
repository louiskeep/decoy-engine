# REG-RENUMBER Part 1: reason manifest policy module build record

Status: record

## Environment

Branch `feat/reg-policy-central` from origin/main `adc212f3` (descendant of the plan base `41a86f47`).
`REGISTRY_VERSION` on the base is 1.6.0. Tests ran through `vm-run engine` (VM venv from the lock).

## Base-tree guard

The plan's candidate rule, run over the base tree before any consumer edit, flagged the four known
copies plus three record-level checks. Deviation approved by the orchestrator: three exemption rows
(`_progress_events._field_value`, `ReasonRecord.validate`, `ReasonRecord.from_payload`), each a
single-record version check and not a manifest comparison. The guard's existence rule checks that an
exempt function exists, not that the rule selected it. The exemption table is allowlist-shaped, so it
is registered as a shrink-only ratchet (`reason-policy-single-source`).

## Deviations

- `test_release_check_command_runs_by_file_path` accepts exit 0 or 1: between releases the pin is
  stale by design. The assertion that fails on a defect is an import error or missing output.
- New sentry and unit test files are over the plan's line caps after `ruff format`.
- `scripts/dev/sentry_registry.toml`, `tests/sentry/_baseline_sources.py` and one new baseline file
  were touched to register the exemption ratchet.

## Mutations (head `5abf962a`)

| Id | Red test |
|---|---|
| P1-M1 | test_append_only_messages_are_byte_identical |
| P1-M1b | test_append_only_messages_are_byte_identical[back] |
| P1-M2 | test_schema_root_violations |
| P1-M3 | test_summary_version_violations |
| P1-M4, M8 | test_rejects_a_fixture_missing_a_live_identity |
| P1-M5 | test_no_manifest_validator_lives_outside_the_policy_module |
| P1-M5b | test_guard_flags_seeded_validators_outside_any_list |
| P1-M6 | test_release_check_command_runs_by_file_path |

## Known limit

The fixture sentry floor is 1.5.0, so an addition with `since` 1.5.0 and no bump is not caught.
