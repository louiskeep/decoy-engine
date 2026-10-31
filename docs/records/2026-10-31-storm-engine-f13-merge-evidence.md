---
Status: current
---
# STORM engine close-out: the F13 main merge and the last Codex LOW

Base: `f255b073` (Codex delta gate GO-WITH-FIXES, 1 LOW open). Merge commit `ba1b87c4` brings
`origin/main` at `6ce435ce` (three commits since the merge base `b509c7e8`: #347 worker bootstrap
and gate scripts, #348 H-1 Wave A module splits, #339 F13 secret providers). LOW fix: `0cbf0973`.
Evidence commit: this record. `phase.py` is byte-identical to `aefed7d8` (blob
`d7ca30f8`), checked again at the final head. `git merge-tree --write-tree origin/main HEAD`
reports no conflicts at the final head.

## Environment

Same container for every number below. Branch venvs: `uv sync --frozen --all-extras` from the
merged `uv.lock` into Python 3.10.20, 3.11.18 and 3.12.3 (`/home/user/venvs/eng3xx`, outside the
tree). Baseline: `origin/main` in a `git worktree` with its own locked venvs on 3.10.20 and 3.12.3
(`main3xx`), `decoy_engine` imported from the worktree. mypy 2.3.0, ruff 0.15.22. The repo's own
`scripts/dev/gates.sh --keep-going --baseline` (new on main, #347) ran the listed suites on the
branch and on a detached `origin/main` worktree with the same 3.10 venv; its EVIDENCE block is
quoted below.

## The three conflicts (`git merge --no-ff`, no rebase)

| file | branch side | main side | resolution |
|---|---|---|---|
| `CHANGELOG.md` | the STORM `Changed (BREAKING)` section with its merge note | the F13 `Added` / `Removed` sections | both kept under Unreleased, F13 first (newest first, as main orders them); the F29 entry's count line now also states the post-F13 numbers |
| `CODEMAP.md` | the fuller `reasons/` row (`ReasonRef`, `require_known` / `get_reason`, the phase namespaces) | the new `secret_providers/` row, plus main's shorter `reasons/` row | the `secret_providers/` row and the branch's `reasons/` row; nothing from either side dropped |
| `tests/unit/execution/test_probe.py` | config `version: 2` on the two real-probe configs (lines 688, 820) | `"mask_secret_ref": _SECRET_REF` added to the same two `global_settings` dicts, the `_throwaway_secret_env` autouse fixture and the `THROWAWAY_SECRET` import | `version: 2` with `mask_secret_ref`; every assertion from both sides is kept (the conflict was in config fixtures, not in assertions; the branch's three other `version: 2` edits in the file auto-merged) |

What each side added to `test_probe.py`: the branch changed every pipeline config in the file
from `version: 1` to `version: 2` (five sites). F13 added the throwaway-secret fixture so the child
probe process can admit a secret, and the `mask_secret_ref` key on the two configs that run the
real probe. No assertion was weakened or removed.

## Drift the auto-merge hid

- **F13 fixtures at config version 1** (class 9, refactor drift across an integration seam).
  `tests/secret_providers/_helpers.py:43` and `tests/secret_providers/test_classifier_equivalence.py:51`
  built `PipelineConfig` with `version: 1`, which the branch's `Literal[2]` rejects. Both now say
  `version: 2`, as every other fixture already does. Found by grepping every file F13 touched for
  the retired config version; the only other hit is the CHANGELOG sentence that says version 1 is
  rejected.
- **Module-versus-package clash**: none. Every directory under `src/` and `tests/` was checked for
  a sibling `<name>.py`.
- **Registry**: 891 identities, 14 namespaces, 2 tombstones, `REGISTRY_VERSION` 1.1.0 (was 884 / 14
  / 2 / 1.0.0 before F13). The seven F13 identities are all `runtime`, owner `SecretProviderError`:
  `secret_provider_unknown`, `secret_provider_unavailable`, `secret_access_denied`,
  `secret_version_unavailable`, `secret_expired`, `secret_lease_unsupported`,
  `secret_material_invalid`. Namespace counts: config 102, connector 15, decoy 21, physical 26,
  plan 214, post_scan 3, pre_scan 1, provider 15, quality 50, route 116, runtime 285, subset 10,
  verification 16, warning 17. The pinned manifest (`tests/fixtures/f29/reason-manifest-v1.json`)
  stays at 884 / 1.0.0 as F13 left it: the pin test is append-only (`manifest_violations`: a pinned
  entry may not be deleted or change meaning; additions need a minor bump, which 1.1.0 is), so the
  live registry is a superset and the test passes. Not regenerated.
- `tests/unit/test_reason_registry.py`, `tests/sentry/test_reason_registry.py`,
  `tests/unit/test_shipped_reason_identities.py`, `tests/sentry/test_physical_seam_disconnection.py`
  (the exact-line allowlist now diffs against the merged `origin/main`), the residual STORM sentry,
  `tests/sentry/test_secret_provider_semgrep.py`, `tests/unit/test_public_api.py`,
  `tests/unit/test_error_base_pickle.py`: all green on 3.10 after the merge.
  `DecoyError.__module__` and `ConfigError.__module__` are `decoy_engine.errors`.
- `docs/compatibility-contract.md`: F13's paragraph is additive (pre-GA corrections taken) and names
  nothing the branch renamed. `pyproject.toml`: F13 adds the `hashicorp-vault` extra, the
  `no_default_secret` marker, `hvac` to the mypy ignore list and the mutation targets; no path the
  branch removed.
- **Module-size sentry**: red on main, not on the branch. `scripts/fk_memory_probe.py` (1102 LOC,
  census 1093) and `scripts/gen_proof_manifest.py` (1258 LOC, census 1241) grew in F13 (#339)
  without a census change; both ids fail identically on `origin/main` in its own venv
  (`2 failed, 734 passed`). The branch's one touch of those two files (the reframe rename sweep in
  `4f7dc24`) left their LOC unchanged. Not patched: the sentry's rule for a legacy over-max module
  is "may only shrink"; raising the census is what the sentry forbids, and the fix belongs to the
  F13 owner (decompose or document on main). Reported, per the handoff rule for a failure that is on
  main.

## LOW (class 8, weak test; class 10, frozen-contract surface): `cross_table_cycle` omitted

Both `v0.15.1` (`fd51be10`) and `v0.16.0` (`5da40c2c`) emit `cross_table_cycle` from
`decide_execution_route`, at the `full_frame_reason` assignment and inside a conditional expression
(lines 617 and 641 in both tags), never on a `return` line; `_pipeline_routing.py:640` still emits
it as `reasons.route.CROSS_TABLE_CYCLE`. The table in `tests/unit/test_shipped_reason_identities.py`
was read by eye from the `return` lines, which is the class: a hand-read table has no
exhaustiveness check.

Fix: the entry `("cross_table_cycle", None)` and a check that the table equals the set of literals
the two decider modules of each tag can emit, extracted by AST (`_emitted_reason_literals`: the last
element of every returned tuple and every `*_reason` assignment, module string constants resolved,
so `REASON_ELIGIBLE` resolves to `eligible`). The per-tag sets are pinned in the module
(`SHIPPED_EMITTED`, with the tag commits in `SHIPPED_TAGS`) so the check runs in a shallow clone;
`test_pinned_literals_match_the_tags` re-derives them from `git show <tag>:...` and skips only when
the tag is not fetched (CI's gates job checks out with `fetch-depth: 0`, so it runs there). Both
tags emit the same 28 codes (17 route-decider, 12 output-sink, `validators_present` in both).

Red first, on `ba1b87c4` with the entry absent:
`AssertionError: missing from the table: ['cross_table_cycle']; not emitted by any tag: []`
(1 failed, 31 passed). With the entry: 33 passed.

Mutations (restored byte-identical to the backup afterwards, 33 passed again):

| # | Mutation | Red? |
|---|---|---|
| M1 | `eligible` removed from the pinned set | yes: 3 failed (exhaustiveness names it as "not emitted by any tag"; both re-derivations name it as emitted beyond the pin) |
| M2 | pinned commit for v0.15.1 altered by one hex digit | yes: 1 failed, "no longer points at the pinned release commit" |
| M3 | extractor ignores `*_reason` assignments (the reading method that caused the omission) | yes: 2 failed, both tags "pinned but not emitted: ['cross_table_cycle']" |

Self-attack: the new test prints only reason codes and tag names; no path, value or environment
text reaches an assertion message.

## Gates on the merged tree

Listed suites, exact command: `<venv>/bin/python -m pytest tests/unit/phase tests/unit/progress
tests/unit/test_reason_registry.py tests/unit/test_public_api.py tests/unit/execution tests/sentry
-q -p no:cacheprovider -rf`

| Interpreter | result | failing ids |
|---|---|---|
| 3.10.20 | 9 failed, 10027 passed, 1167 skipped, 2 xfailed, 5 xpassed (8:31) | the 7 baseline ids (4x `test_c5c_a_unchanged_jobs::test_job_output_is_unchanged`, 2x `test_faker_strategy`, `test_proof_manifest_is_up_to_date`) plus the 2 module-size ids above |
| 3.12.3 | 11 failed, 10025 passed, 1167 skipped, 2 xfailed, 5 xpassed (8:14) | the same 9, plus `test_categorical_weighted.py::TestCdfBuilder::test_skewed_weights_skew_cdf` and `test_out_of_core_stream_join_reorder.py::test_close_releases_run_file_fd_on_partial_consumption` |

Against the known baseline (previous record: 8 on 3.10 of which one was the since-fixed wording
of `docs/reasons.md`, so 7; 9 on 3.12): the only delta is the two module-size ids, both red on
`origin/main`. On 3.12 all 11 ids were run by id on the `origin/main` worktree with `main312`:
11 failed, 7 passed (the 7 are the other `test_job_output_is_unchanged` cases).

The two pinned trace tests (`tests/unit/execution/test_r3_failure_publication_trace.py`, with
`r3_failure_trace_baseline.json`) are byte-identical to `f255b073` and green on 3.10 and 3.12
(47 passed, 1 skipped each).

Other gates: ruff check clean, ruff format clean (1814 files); mypy on 3.10: 16 errors in 7 files
(checked 677 source files), the same 16 error lines as `origin/main` with `main310` (16 in 7,
667 files); module-size sentry as above; `tests/sentry/test_no_residual_removed_surface.py` green
with this record in the tree.

### `scripts/dev/gates.sh --keep-going --baseline` (3.10 venv, listed suites minus `tests/sentry`, which its sentry step runs)

The `env` step fails because the venv was synced with `--all-extras` (as the previous records' venvs
were) while `bootstrap.sh --check` expects exactly `dev lint vault cloud`; the 13 differences are
packages from the other extras, no missing or mismatched pin. The baseline `tests` step drops
`tests/unit/phase`, which does not exist on main. `branch-only=0`: the branch adds no failing id;
the two `BASELINE-ONLY` ids are the pinned trace tests, red on main and green on the branch, as in
the previous record.

```
============================== EVIDENCE ==============================
repo:       decoy-engine @ 0cbf097476faeb23f438bc49ad68277d08772d56 (claude/eager-clarke-14deh1)
dirty:      0 path(s) uncommitted
origin/main 6ce435ceceb556451aab98975aebea495f750ac4
venv:       /home/user/venvs/eng310
python:     Python 3.10.20
ruff:       ruff 0.15.22
mypy:       mypy 2.3.0 (compiled: yes)
pytest:     pytest 9.1.1
steps:      env,ruff,format,mypy,sentry,tests (keep-going=1)
test paths: explicit: tests/unit/phase tests/unit/progress tests/unit/test_reason_registry.py tests/unit/test_public_api.py tests/unit/execution
baseline:   origin/main @ 6ce435ceceb556451aab98975aebea495f750ac4 in a detached worktree, same venv and lock as branch
            decoy_engine imported from: /tmp/gates.OOfzFO/baseline-tree/src/decoy_engine/__init__.py
logs:       /tmp/gates.OOfzFO
-- branch --
[env] FAIL(rc=1)
    $ bash /home/user/decoy-engine/scripts/dev/bootstrap.sh --check 
[ruff] PASS
    $ /home/user/venvs/eng310/bin/ruff check src tests testflight scripts 
[format] PASS
    $ /home/user/venvs/eng310/bin/ruff format --check src tests testflight scripts 
[mypy] FAIL(rc=1) errors=16
    $ /home/user/venvs/eng310/bin/mypy src/decoy_engine testflight 
[sentry] FAIL(rc=1) passed=3320 failed=3 errors=0 skipped=2
    $ /home/user/venvs/eng310/bin/python -m pytest -p no:cacheprovider -q -rfE --junitxml=/tmp/gates.OOfzFO/branch-sentry.xml -m not\ benchmark\ and\ not\ packaging\ and\ not\ codspeed tests/sentry 
[tests] FAIL(rc=1) passed=6712 failed=6 errors=0 skipped=1167
    $ /home/user/venvs/eng310/bin/python -m pytest -p no:cacheprovider -q -rfE --junitxml=/tmp/gates.OOfzFO/branch-tests.xml -m not\ benchmark\ and\ not\ packaging\ and\ not\ codspeed tests/unit/phase tests/unit/progress tests/unit/test_reason_registry.py tests/unit/test_public_api.py tests/unit/execution 
-- baseline --
[env] SKIP
    $ (same venv as branch)
[ruff] PASS
    $ /home/user/venvs/eng310/bin/ruff check src tests testflight scripts 
[format] PASS
    $ /home/user/venvs/eng310/bin/ruff format --check src tests testflight scripts 
[mypy] FAIL(rc=1) errors=16
    $ /home/user/venvs/eng310/bin/mypy src/decoy_engine testflight 
[sentry] FAIL(rc=1) passed=3264 failed=3 errors=0 skipped=2
    $ /home/user/venvs/eng310/bin/python -m pytest -p no:cacheprovider -q -rfE --junitxml=/tmp/gates.OOfzFO/baseline-sentry.xml -m not\ benchmark\ and\ not\ packaging\ and\ not\ codspeed tests/sentry 
[tests] FAIL(rc=1) passed=6466 failed=8 errors=0 skipped=1165
    $ /home/user/venvs/eng310/bin/python -m pytest -p no:cacheprovider -q -rfE --junitxml=/tmp/gates.OOfzFO/baseline-tests.xml -m not\ benchmark\ and\ not\ packaging\ and\ not\ codspeed tests/unit/progress tests/unit/test_reason_registry.py tests/unit/test_public_api.py tests/unit/execution 
-- FAILING TEST IDS --
  BOTH          tests/sentry/test_module_size.py::test_module_within_size_budget[scripts/fk_memory_probe.py]
  BOTH          tests/sentry/test_module_size.py::test_module_within_size_budget[scripts/gen_proof_manifest.py]
  BOTH          tests/sentry/test_proof_manifest.py::test_proof_manifest_is_up_to_date
  BOTH          tests/unit/execution/test_c5c_a_unchanged_jobs.py::test_job_output_is_unchanged[earlier_composite_writer]
  BOTH          tests/unit/execution/test_c5c_a_unchanged_jobs.py::test_job_output_is_unchanged[group_by_sibling]
  BOTH          tests/unit/execution/test_c5c_a_unchanged_jobs.py::test_job_output_is_unchanged[int64_metadata_source]
  BOTH          tests/unit/execution/test_c5c_a_unchanged_jobs.py::test_job_output_is_unchanged[null_free_int]
  BOTH          tests/unit/execution/test_faker_strategy.py::TestFakerStrategy::test_provider_config_reaches_the_pool_build
  BOTH          tests/unit/execution/test_faker_strategy.py::TestFakerWarmCacheIdentity::test_config_in_lookup_identity_prevents_sibling_pool_contamination
  BASELINE-ONLY tests/unit/execution/test_r3_failure_publication_trace.py::test_failure_and_publication_trace_is_unchanged[f_forced_ooc_incompatible]
  BASELINE-ONLY tests/unit/execution/test_r3_failure_publication_trace.py::test_forced_out_of_core_on_an_ineligible_job_is_a_config_error_before_any_write
  (branch=9 baseline=11 branch-only=0)
======================================================================
```

## Full suite (`pytest tests -q -p no:cacheprovider -rf`, 3.11.18)

| Tree | result |
|---|---|
| merged branch `0cbf0973`, 3.11.18 | 18 failed, 29129 passed, 5342 skipped, 48 deselected, 6 xfailed, 65 xpassed (45:44, alongside the two listed-suite runs) |
| previous record, `f41e1ad1`, 3.11.17 | 16 failed, 28429 passed, 5335 skipped, 48 deselected, 6 xfailed, 65 xpassed |

The 18 failing ids are exactly the previous record's 16 plus the two module-size ids that are red on
`origin/main`:

- Faker-value snapshots (13): `test_c5c_a_unchanged_jobs::test_job_output_is_unchanged` (4 cases),
  `test_faker_strategy` (2), `test_proof_manifest_is_up_to_date`,
  `test_dob_anchor::test_composite_person_masking_matches_pre_change_golden_without_freeze`,
  `providers_v2/test_registry.py::TestFakerProviders::test_known_faker_provider_present`,
  `test_phase_f_f0_oracle.py::test_oracle_fixture` (4 composite cases),
  `test_phase_f_f0_composite_repeatability.py::test_composite_person_repeats_byte_identically_in_process` (2 cases).
- Shadow snapshot as root (1): `tests/physical/test_shadow_snapshot.py::test_fixture_is_written_then_made_read_only`.
- Module-size sentry (2): the two `scripts/` ids, red on `origin/main`.
- The 3.12-only CDF precision and out-of-core fd ids do not appear (3.11 run), as before.
- `google.cloud`: not among the failures because the venv carries the `cloud` extra.

700 more tests passed than on `f41e1ad1`: the merge brings main's new suites (F13's
`tests/secret_providers`, H-1's characterization and golden tests, the dev-script tests).

## Not run

The parity, physical and native suites under their own markers beyond what `tests` collects by
default; the `hashicorp-vault` extra's live Vault tests (no server); CLI and platform repins (the
orchestrator's next steps). The proof manifest and the Faker-value snapshots were not regenerated
(environment-dependent values). The module-size census was not touched.
