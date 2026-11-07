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

## Codex final-review fixes (on 5100e0d7)

- `--check` fails closed. It rebuilds the consumer contract from the tree and requires the
  committed file to match, reports a non-string `registry_version` as `<non-string>`, requires at
  least one version, refuses zero fixtures when identities are new and more than one fixture
  always, and requires exactly one `docs/reasons.md` anchor with every match checked. AT-R23 covers
  wrong type, missing key, duplicate fixtures, no fixture, duplicate anchor, stale duplicate anchor.
- The guard resolves module aliases: `import m as p`, `import a.b.m`, `from a.b import m`, then
  `p.SCHEMA_ROOTS` and `p.version_of(...)`.
- Flat test folders (`tests/sentry` and `tests/unit` hold many direct modules): not moved here.
  The LAYOUT slice carries that, already logged.

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

Every row below was run on code head `6bd9e934` (record-only commits follow it), on a GCP test
VM through `vm-run engine` (the Mac's shared test lock was held by a long gate run). Each turned
the named test red and was reverted. This replaces the earlier rows that ran on `03a233fe`,
`1e2b5fd2` and `5100e0d7`, and it includes Part 1's mutations (P1-*), which now run on the
stacked head.

| Id | What it breaks | Red test |
|---|---|---|
| M1, M12, M16 | branch-added line rewrite, sentence-final lookahead, renumber-line exclusion | AT-R1, AT-R12 |
| M2 | record line written when the version did not move | AT-R2 |
| M3 | target from the branch instead of main | AT-R3 |
| M4 | `--check` gap case | AT-R4 |
| M5 | edited-existing check | AT-R5 |
| M6, M6b | duplicate-identity, main-and-branch-changed | AT-R6 |
| M7 | several-minors | AT-R7 |
| M8, M17 | main-not-merged precondition | AT-R8, AT-R16 |
| M9 | unknown-surface | AT-R9 |
| M10 | foreign-import check | AT-R10 |
| M11, M22 | stale contract; plan applied before validation | AT-R11 |
| M13 | mixed-line | AT-R13 |
| M14 | stale-since | AT-R14 |
| M18 | fixture rename before fixture-exists | AT-R17 |
| M19, M20, M23, M24a, M24b | schema-root attribution | AT-R18 (side cases for M19, M20) |
| M21 | summary-version | AT-R19 |
| M25 | path block | AT-R20 |
| N-M2 | header lines inside a hunk | AT-R21 |
| N-L1a, N-L1b | main-moved fact | AT-R22, AT-R14 |
| N-M3a, N-M3b | central fixture-name helpers | AT-R1, minor-fixture sentry |
| N-C2, N-C12 | `--check` contract regeneration and version types | AT-R23 wrong type, missing key |
| N-F1, N-F2 | `--check` fixture count | AT-R23 duplicate and missing fixture |
| N-A1, N-A12 | `--check` anchor count and every match | AT-R23 duplicate anchors |
| G1, G2, G3 | guard: qualified names, aliases, constant tuples | seeded guard tests |
| N-M1a, N-M1d | guard: policy constants, accessors | seeded guard tests |
| N-G1, N-G2, N-G3 | guard: module aliases (import-as, package import, attribute) | qualified-use test |
| P1-M1, P1-M1b | policy message text | byte-identical message test |
| P1-M2 | schema roots | `test_schema_root_violations` |
| P1-M3 | summary lookahead | `test_summary_version_violations` |
| P1-M4 | fixture sentry live identity | `test_minor_fixtures` rejects a missing identity |
| P1-M5, P1-M5b | guard discovery | seeded validators test |
| P1-M6 | release pin path block | `test_release_check_command_runs_by_file_path` |

## Known limit

The token rewrite assumes a claimed version on a branch-added line means the slice's own minor.

## V2-LOG

- `--check` is not wired into `gates.sh` or CI.
- `renumber_check` repeats some classification findings (admin-only diagnostics).
- Flat `tests/sentry` and `tests/unit` folders: deferred to the LAYOUT slice.
- Part 1 P1-AT6 accepts exit 0 or 1 where the plan says both invocations exit 0; documented, and it
  still catches an import failure.
- The unbumped-addition acceptance case in `test_reason_minor_fixtures.py` asserts only a non-empty
  list, not the exact failure.
- Several files exceed their plan estimates; none crosses the 800-line design threshold.
- Dennis L2 to L6 (gate of 2c91d99f): deferred to V2-LOG; texts are in his gate report.

## Not verified

`gates.sh --baseline` on a VM, the live rehearsal on two real branches, hosted CI, sphinx build.
