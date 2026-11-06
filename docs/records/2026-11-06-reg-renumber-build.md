# REG-RENUMBER Part 2: renumber tool build record

Status: record

## Environment

Branch `feat/reg-renumber-tool` from Part 1's head `1c46f6f1` (origin/main `adc212f3`).
`REGISTRY_VERSION` on the base is 1.6.0. Tests ran on the Mac mini (macOS arm64, the engine venv
at `~/decoy/engine/.venv`) because all four GCP test VMs were busy with other gate runs; the
scratch repos build a real second and third slice, so the results do not depend on Linux.

## What was built

- `scripts/dev/registry_renumber.py` and `scripts/dev/_registry_renumber_git.py` (git, hunk
  parsing and the guarded extractor; split out under "Central registries and sizing").
- `RenumberFacts`, `renumber_classification`, `renumber_check` in the policy module, plus the small
  helpers the script needs so it holds no comparison of its own: `renumber_claim`, `next_minor`,
  `version_of`, `entries_since`, `contract_versions`, `restamp`, `owned_schema_roots`, and the
  public `SCHEMA_ROOTS` and `VERSION_LITERAL`.
- `tests/unit/test_registry_renumber.py` (AT-R1 to AT-R14, AT-R16 to AT-R20).
- One paragraph in `docs/reasons.md`.

## Part 1 follow-ups (coordinator, from Dennis M2 and Codex DESIGN)

1. Exemptions in the single-source guard are keyed by qualified name (`ReasonRecord.validate`,
   `ReasonRecord.from_payload`). The existence check matches the qualified name. The ratchet
   baseline keys and reasons match the `EXEMPT` text. Seeded test: a same-named method on another
   class is still flagged (mutation G1).
2. The guard follows local aliases: names assigned or iterated from a registry-key expression
   (subscript, attribute, or a module constant tuple made only of key strings) count as key uses in
   comparisons and filters. Seeded cases: alias, assign-then-compare, and a copy of
   `schema_root_violations` over a constant tuple (G2, G3).
   The wider rule flagged no live function except one false positive (`version_pins`, a tuple of
   words that included a non-key), fixed by requiring every element of the tuple to be a key.
   `reason_release_pin.py::main` and `_record.py::for_reason` were not flagged.
   Three of this slice's own functions were flagged and were restructured, not exempted: the
   registry-key helpers moved into the policy module (`contract_versions`, `restamp`,
   `owned_schema_roots`, `entries_since`, `version_of`).

## Dennis gate fixes (on 2c91d99f)

- M2: the hunk parser reads the old and new line counts from each `@@` header and consumes exactly
  that many lines, so an added "++ " line or a removed "-- " line is content (AT-R21).
- M1: the guard seeds key constants imported from the policy module (`SCHEMA_ROOTS`) and treats
  `version_of`, `entries_since` and `contract_versions` calls as key expressions. Seeded cases:
  imported tuple filter, and `version_of(m) != version_of(h)`. The only newly flagged live function
  was this slice's own `owned_roots`, restructured through the policy's `owned_schema_roots`.
- M3: `fixture_prefix`, `fixture_name`, `parse_fixture_name` and `FIXTURE_GLOB` in the policy
  module replace the script's own regex and prefix strings and the sentry's hand-built glob.
- L1: stale-since uses the fact `main_moved` (fork != main sha) instead of comparing manifests.
- L2 to L6: deferred to V2-LOG per the coordinator; the texts are in Dennis's gate report.

## Deviations

- Sizes: the test file is about 690 lines (cap 360) after `ruff format`; the whole part is about
  1,440 changed lines (budget 800). No test was cut. The policy module is 340 lines (cap 320).
- The git helpers live in a second script module, as the sizing section allows.
- `gates.sh --baseline` was not run: every VM was occupied. Ruff, format, a mypy pass on the script
  and the impact-selected tests ran on the Mac instead.
- The extractor runs in a throwaway cwd, because the engine writes `logs/` under its cwd.
- A branch change to a schema root that arrives through a side-branch merge is caught only by the
  policy comparison (no first-parent commit owns it). The plan's cases 1 and 2 are also caught by
  the history fact, so M19 and M20 needed two extra cases (`side`) to turn red.

## Red runs

Every AT is red on the base tree because the script does not exist (the plan's expected red);
the mutations below carry the right-reason evidence.

## Mutations

| Id | Red test |
|---|---|
| M1, M12, M16 | test_second_slice_is_renumbered..., test_a_second_renumber_keeps_both_record_lines_true |
| M2 | test_renumber_is_idempotent |
| M3 | test_a_slice_at_main_plus_one_is_a_noop |
| M4 | test_check_rejects_a_version_that_is_not_main_plus_one[_gap] |
| M5 | test_refuses_an_edit_to_an_existing_code |
| M6, M6b | test_refuses_when_main_and_branch_define_the_same_code |
| M7 | test_refuses_more_than_one_new_minor |
| M8, M17 | test_refuses_preconditions, test_only_the_first_of_two_green_heads_can_land |
| M9 | test_refuses_a_version_token_on_an_unknown_surface |
| M10 | test_refuses_a_manifest_imported_from_another_tree |
| M11, M22 | test_refuses_a_stale_consumer_contract |
| M13 | test_refuses_a_mixed_line |
| M14 | test_refuses_a_stale_since |
| M18 | test_refuses_before_writing[fixture-exists] |
| M19, M20, M23, M24a, M24b | test_schema_root_changes_are_attributed (M24b also the direct policy test) |
| M21 | test_refuses_a_version_in_a_summary |
| M25 | test_merge_procedure_commands_run_by_file_path |
| G1, G2, G3 | single-source guard seeded tests |

Heads: M1, M2, M9, M12, M13, M16 on `03a233fe`; the rest on `1e2b5fd2` (the later commit changes
only `plan_rewrites`). M2 was an equivalent mutant on `1e2b5fd2`; the code was changed so the
record condition is reachable and M2 turns red again.

## Known limit

The token rewrite assumes a claimed version on a branch-added line means the slice's own minor.

## V2-LOG

- `--check` is not wired into `gates.sh` or CI.
- `renumber_check` repeats some classification findings (admin-only diagnostics).
- Flat `tests/sentry` folder: deferred to the LAYOUT slice.

## Not verified

`gates.sh --baseline` on a VM, the live rehearsal on two real branches, hosted CI, sphinx build.
