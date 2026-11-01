# A-4 file access reconciled onto main (F13, STORM close-out, main health), 2026-11-01

Branch `claude/jolly-carson-3k5stf`, from 52a3c3f8, merging origin/main 0090f6f9 (merge commit
7ddf9491). Owner decisions: `docs/handoffs/OWNER-DECISIONS-2026-10-30.md` (decoy-platform,
`handoffs/queue`), "2026-10-31 night", items 1-5, plus one new conflict decided in this session
(section 3, item 6).

Environment: `scripts/dev/bootstrap.sh` (Python 3.10.20, `uv sync --frozen`, extras dev, lint,
vault, cloud) after the merge, so the venv matches the merged `uv.lock`. The 3.12 runs use the same
lock and extras through `uv sync --frozen --python 3.12`. Baselines ran in a detached worktree of
origin/main with its own venv from its own `uv.lock`.

## 1. Merge resolutions (19 conflicted files)

| File | Resolution |
|---|---|
| `vault.py` | F13 admission (`AdmissionScope` + `admit_mask_key`) AND A-4 `VaultWriter(mask_key, file_access=file_access)`. The rest of the diff against main is A-4 only. |
| `unmask.py` | `vault_path is not None or vault is not None or _has_fpe`; F13's removal of the pre-GA seed bypass stays removed (`is_pre_ga` import gone). |
| `keyprovider.py` | Main's F13 module plus the A-4 entry point: `key_provider_from_ref(ref, *, key_version: str \| None = None, file_access: FileAccess \| None = None) -> KeyProvider`, `@fileaccess.entry_point`. `resolve_mask_secret_ref` stays deleted and leaves `fileaccess.ENTRY_POINTS`. |
| `tests/conftest.py` | Both autouse fixtures: F13's throwaway secret (with `no_default_secret`) and A-4's latch reset. |
| `_error_base.py` | Main's `DecoyError.__module__ = "decoy_engine.errors"`. |
| `reasons/route/_added.py`, `_chunked_output_sink.py` | `route.post_scan_integrity` / `route.post_validation` from main's `_sink.py` (no duplicate); A-4's `fileaccess` import kept. |
| reason manifest | Union; identical to main's pin (`git diff origin/main -- tests/fixtures/f29/` empty). `run_storm*` tombstones kept. |
| `test_r3_public_signature_freeze.py` | 40 parameters (F13 `secret_registry`, `secret_cache_policy`; A-4 `file_access`), all additive, order as in the live signature. |
| `test_module_size.py` | Main's ceilings; the three over-ceiling modules were split (section 4). |
| `_job_estimate.py`, `_pipeline_routing_signals.py`, `capacity.py` | A-4 `fileaccess` imports. |
| `test_no_residual_removed_surface.py`, `test_probe.py`, `test_v1_canaries.py` | Main's side (tombstone exceptions, keyed probe fixtures, 3.10-safe `__notes__`). |
| `test_reason_registry.py` | Union of UNCLASSIFIED entries. |
| `CHANGELOG.md`, `CODEMAP.md` | Union; main's STORM tombstone wording wins. |

Registry: 893 identities, 14 namespaces, 2 tombstones (main: 891/14/2). The two new identities are
`runtime.secret_file_permissions_unsafe` (E1) and `decoy.file_access_denied` (open item), so
`REGISTRY_VERSION` 1.1.0 -> 1.2.0 (additions raise the minor, `docs/reasons.md`; the F29
consumer-contract fixture's `registry_version` strings follow, as F13 did); manifest append-only
test green. `DecoyError.__module__ == "decoy_engine.errors"`. Shipped-identity test green.

Fixture follow-ups to F13 (no assertion changed): the A-4 route-corpus golden re-captured for the 8
keyed jobs (hash column) because keyed work now runs under F13's throwaway secret; only output
digests changed, route and metric records are identical, and the ambient-object and rooted-policy
cells match the new digests. The strace job passes F13's throwaway key provider (a child process
does not get the conftest fixture).

## 2. Re-pointed A-4 tests (decision 4)

| Test | Before | After | Equivalence |
|---|---|---|---|
| `test_x1_secret.py` (all cells) | `resolve_mask_secret_ref(ref, ...)` | `key_provider_from_ref(ref, ...).mask_key()` | Same exception classes and leak checks. `== bytes.fromhex(GOOD)` becomes `== SecretKeyProvider(bytes.fromhex(GOOD)).mask_key()`: F13 returns the prepared mask root, so the same derivation of the expected material is compared. Fault injection patches `fileaccess.read_checked` (the provider's read) instead of `read_text`. |
| `test_char_error_text.py::test_r6` | old path-bearing ambient text | main's text, measured on a main checkout: `missing_mask_secret: The mask secret could not be resolved.`, `bad_secret_ref: The secret material has an invalid encoding.` | Property kept: A-4 leaves ambient text as main has it. |
| `test_policy_denials.py::test_v9` | `resolve_mask_secret_ref` | `key_provider_from_ref(...).mask_key()` | Same `FileAccessDenied` + `_assert_denial`; same `len == 32`. |
| `test_policy_errors.py` (2 cells) | `resolve_mask_secret_ref` | `key_provider_from_ref(...).mask_key()` | Identical assertions (the position cell holds through section 3, item 6). |
| `test_locator_hygiene.py` | waiver for `resolve_mask_secret_ref` | removed | The function no longer exists (a stale waiver). |

E1 fixture corrections (files written 0600, plan 3.6, no assertion changed): `test_x1_secret.py` (4),
`test_char_error_text.py` (2), `test_policy_denials.py` (2), `test_policy_errors.py` (1),
`test_de02_keyprovider.py` (2), `test_keyprovider_invariants.py` (2), `integration/_f29_jobs.py` (1).
`test_r4_mutation_kills.py` and `test_refs_and_descriptor.py` only parse refs and need none.

## 3. Decisions implemented

1. **`file:` read through the policy, with E1** (`secret_providers/_local.py`,
   `fileaccess/_read.py::read_checked`). E1 had not landed (`feat/f13-e1-file-secret-permissions`
   absent on the remote), so it is implemented here from the plan's section 3.6: the check runs on
   `fstat` of the policy-issued descriptor and the bytes come from that descriptor (`pread` for a
   regular file). Group/other bits are refused with `secret_file_permissions_unsafe`; a directory is
   a missing secret; Windows skips the mode check. The orchestrator can retire or rebase the E1 branch.
2. **Closed kept set** (`_errors.KEPT_FILE_FAILURES`, `_preflight._call_closed`). Under a strict
   policy the file provider raises a value-free carrier (`KeptFileFailure(kind)`) for exactly
   `FileAccessDenied`, `FileAccessUnavailable`, `MemoryError`, ENOMEM, ENOSPC, EDQUOT. Only the
   outermost boundary rebuilds the real class from constants (an inner one re-raises the carrier).
   The carrier is honored only from `FileSecretProvider` (exact type); from any other provider it is
   `secret_provider_unavailable`. Under the ambient policy main's behavior is unchanged.
3. **Local providers never cached** (`_preflight._is_local`, enforced in the resolver).
4. **Signature** as in section 1.
5. **Splits, not ceilings; Vault TLS exception** (section 4).
6. **New conflict, escalated and decided by the owner in this session:** A-4 requires a strict
   decode failure of a `file:` secret to name `global_settings.mask_secret_ref`; F13 rebuilds
   `bad_secret_ref` from a closed table of texts. Decision: add one engine-owned constant
   (`BAD_ENCODING_AT_POSITION_MESSAGE`) to that table, used for the built-in file provider under a
   strict policy only. Ambient text and every code are unchanged.

Found during the work, and fixed as a class: every F13 admission boundary attached a caller's
in-flight exception as `__context__` when admission was called inside the caller's `except` block (A-4
AT-X1 failed on the merged tree). A-4's `raise_clean` structure is ported to `_call_closed`
(`_raise_bare`); `test_boundary_caller_context.py` covers all six public boundary entries. For
Dennis's boundary review together with item 2.

## 4. Smaller items

- Module splits (pure moves, re-exported where callers import them): `_chunked.py` ->
  `_chunked_aggregate.py` (586 lines, entry removed, under the 600 goal);
  `_pipeline_routing_signals.py` -> `_pipeline_routing_samples.py` (639 -> 613);
  `native/_chunked_entry.py` -> `native/_chunked_oracle_rows.py` (666 -> 665; `_oracle_preserved_rows`
  only). A first cut also moved `_native_chunk_result`, which took `ExecutionResult` out of
  `_chunked_entry`'s namespace and broke the monkeypatch in
  `tests/native/test_chunked_entry_side_channels.py::test_native_route_builds_no_result_objects_without_a_sink`
  (caught by the 3.12 full run). Class-level check afterwards: every name each split removed from its
  module namespace was grepped against every test reference; that was the only one. No ceiling raised.
  Behavior equality: the route corpus golden (no re-capture after the split) and the full suite.
  The three new modules join the physical-seam permitted list (they import nothing from
  `execution.physical`).
- Vault KV TLS paths: `operator_environment` entry in `tests/sentry/file_access_modules.toml` with
  owner, reason and removal note; `test_operator_environment_exceptions_are_current` fails if the
  function goes away or reads another path variable. Adapter unchanged. Doc line in
  `docs/security/file-access-policy.md`.
- Platform private import: `fileaccess.reset_for_tests()` (public) lifts both latches; it replaces
  B-5's `_entry._reset_for_tests()` and `_entry._ambient_forbidden = False` in
  `api/file_access/startup.py:106-110` and `tests/conftest.py:61-64`.
- Strict-policy cleanup after abort: recorded `attempted` (F29: cleanup ran, outcome not
  confirmed) instead of `unknown`; ambient stays `confirmed`/`failed` from the stage path.
- `FileAccessDenied` identity: `decoy.file_access_denied` via `reason_adapter._FIXED` (append only).
  `FileAccessReadError` and `FileAccessUnavailable` stay unclassified.

## 5. Red-first and mutation proofs

Red records: `docs/plans/evidence/2026-11-01-a4-f13-reconcile-red-baseline.txt` (45 new cells plus
the sink sentry on the merge commit; then the caller-context cells with `_raise_bare` reverted). The
`prepare_legacy` caller-context cell was rewritten after record 2 (its first form raised in the
test's own constructor); re-run reverted it is red, restored green (6 failed / 6 passed). The
strict-abort and denial-identity tests were run red before their fixes; the position-text cell is the
A-4 test recorded red on the merge commit.

Each new mechanism mutated once (the scratch script restores every file):

| Mutant | Result |
|---|---|
| bare `open()` back in `FileSecretProvider._read` | RED |
| E1 mask `0o077` -> `0o007` | RED |
| E1 check by path `stat` then reopen | RED (after strengthening the race test; first run survived) |
| closed set widened (EIO kept) | RED |
| file-provider-only gate removed | RED (after adding the carrier-from-another-provider cell; first run survived) |
| inner boundary hands out the real class | RED |
| `_raise_bare` dropped | RED |
| cache enabled for local providers | RED |
| `resolve_mask_secret_ref` re-added to `ENTRY_POINTS` | RED |
| `key_provider_from_ref` without `@entry_point` | RED |
| strict abort back to `unknown` | RED |
| `FileAccessDenied` removed from `_FIXED` | RED |
| Vault adapter reads another path variable | RED |
| `reset_for_tests` leaves `forbid_ambient` set | RED |
| strict position text applied under ambient | RED |
| strict position text dropped | RED |

Self-attack: a keyed `run_pipeline` whose `file:` secret path carries a canary and sits outside the
root leaks nothing to the error text, repr, args, formatted traceback, reason record or logs. The
E1 refusal leaks no path or mode; engine frame locals never hold the file bytes. (Caller-facing
frames hold the caller's own `ref` argument, as on main; neither contract covers that.)

## 6. Platform B-5 (`claude/vibrant-shannon-5lspen`) surface

Engine symbols B-5 calls with `file_access=` (AST scan of `api/`, imports resolved):
`unmask_pipeline`, `vault.vault_writer_for_config`, `run_pipeline` (two import paths),
`execution.run_mask_pipeline_chunked`, `run_mask_chunked`, `execution._sequential.run_sequential`,
`execution.out_of_core.run_fk_out_of_core`, `plan.compile_plan`,
`generation.pinned.compile_pinned_plan_with_generation`, `generation.window.generate_rows`,
`profile.profile_source`, `read_fixed_width` (two import paths), `write_fixed_width` (importlib),
`apply_table_transforms`, `quarantine.apply_quarantine`, `subset.plan_subset`, `run_discovery_sql`,
`generators.derivation.strategy_config_fingerprint`, `transforms.joint_mask.validate_joint_mask_config`,
`execution.ParquetTransactionalSink`, and `storm.run_storm`. Correction to the earlier list:
`iter_parquet_chunks` / `iter_csv_chunks` in B-5 are the platform's own `api.jobs.native_reader`.

Signature comparison 52a3c3f8 -> head over these, plus every name in `fileaccess.__all__` and
`vault.iter_vault_columns`, `vault.collect_vault_entries`, `keyprovider.SecretKeyProvider` (68 symbols):
unchanged except
- `run_pipeline`: gains F13's keyword-only `secret_registry=None`, `secret_cache_policy=None`
  (additive, from main);
- `keyprovider.key_provider_from_ref`: `key_version` default `"v1"` -> `None`, return
  `SecretKeyProvider` -> `KeyProvider` (decision 4; B-5 does not call it);
- `fileaccess`: new `read_checked`, `reset_for_tests`; `ENTRY_POINTS` loses `resolve_mask_secret_ref`.

For the B-5 re-merge and F13 slice 3a:
- `decoy_engine.storm.run_storm` does not exist on main or on this branch (STORM removed);
  `api/storm/router.py` breaks.
- `api/jobs/v2_vault.py:44` calls `vault_writer_for_config(seeded, file_access=...)` with no
  key provider: fails closed after F13 for a vaulted config without a `mask_secret_ref`.
- Switch `fileaccess._entry` uses to `fileaccess.reset_for_tests()`.
- Any `file:` secret the platform writes must be 0600/0400.

## 7. Suites (3.10.20, lock venv, same environment for both columns)

| Lane | Before | After |
|---|---|---|
| A-4 file-access suite + three sentries (`tests/fileaccess`, `test_file_access_boundary.py`, `test_file_access_entry_points.py`, `test_locator_hygiene.py`; the lane of the 2026-10-31 record) | 52a3c3f8: 673 passed, 2 skipped | head: 685 passed, 2 skipped |
| F13 `tests/secret_providers` | main 0090f6f9: 236 passed, 6 skipped | head: 297 passed, 6 skipped |
| fileaccess + secret_providers + all sentries + keyprovider + registry + freeze + integration | n/a | e0e7f274: 5088 passed, 33 skipped |
| ruff check / format (`src tests testflight scripts`) | main: clean | head: clean, 1888 files formatted |
| mypy (`src/decoy_engine testflight`) | main: 0 errors (677 files) | head: 0 errors (702 files) |

The handoff's "646 passed / 1 skipped" could not be matched to any lane in the 2026-10-31 record; the
same-environment numbers above replace it.

### 7a. Full suite (pyproject addopts, `pytest tests`, venvs from `uv.lock` with the bootstrap extras)

| Python | main 0090f6f9 | head 2cea0ecc |
|---|---|---|
| 3.10.20 | 0 failed, 28982 passed, 5286 skipped, 6 xfailed, 65 xpassed | 0 failed, 29836 passed, 5288 skipped, 6 xfailed, 65 xpassed |
| 3.12.3 | 1 failed (`tests/unit/execution/test_categorical_weighted.py::TestCdfBuilder::test_skewed_weights_skew_cdf`, known), 28897 passed, 5370 skipped | 1 failed (the same id), 29751 passed, 5372 skipped |

main 3.12 exact: 1 failed, 28897 passed, 5370 skipped. Failing ids head vs main: identical on both
interpreters. An earlier full run at e0e7f274 failed
`tests/native/test_chunked_entry_side_channels.py::test_native_route_builds_no_result_objects_without_a_sink`
on both interpreters (the first `_chunked_entry` split, section 4); 2cea0ecc fixes it.

## 8. What was not run

The native companion is not installed (native-only tests skip, as on main). No CI run, no
platform or CLI suite, no live Vault (`test_vault_live.py` skips as on main), no Windows run (the
`os.name == "nt"` branch is covered by a patched-`os.name` test only), no mutmut campaign (one
hand mutation per mechanism, section 5).

## 9. Remediation round 3 (Codex FINAL NO-GO on d8850f00: four HIGH, one MEDIUM)

Merged origin/main 1ee89e68 first (OOC harvest #354, SBOM #355, native release script #353; merge
58706fa7). Only `CHANGELOG.md` conflicted (both entries kept); main did not touch
`REGISTRY_VERSION` or the identity count, so neither changed. Environment as above: venvs from the
merged `uv.lock` (3.10.20 and 3.12.3, extras dev, lint, vault, cloud); baselines in a detached
worktree of origin/main 1ee89e68 with venvs from its own lock. Red records and mutant results:
`docs/plans/evidence/2026-11-01-a4-f13-reconcile-r3-red.txt`.

All five findings were reproduced before any fix (probe, then the tests below red). None is
rejected.

| # | Class | Finding | Fix | Red first | Mutants |
|---|---|---|---|---|---|
| 1 | 3/7 | `_resolve` took an explicit policy before looking at the installed one, so a nested call could swap a tenant policy for `LocalFileAccess` or another strict one | The only ordering policies have: ambient is the widest; two strict policies are incomparable unless the same object. Under an installed policy an explicit one is used iff it `is` the installed one or the installed one is ambient; otherwise `FileAccessUnavailable`. Applies to `entry_point`, `scope`, `capture` (all resolve through `_resolve`). | `tests/fileaccess/test_nested_explicit_policy.py`: 6 ordering pairs (strict/ambient, strict/other strict, strict/same, ambient/strict, ambient/same, ambient/other ambient) x 3 routes, plus a nested entry inside a strict call; 7 red | M1 `_not_broader` -> `True`: 7 red |
| 2 | 1 | `key_provider_from_ref` kept `ref` as a local and the decorator kept it in `args`/`kwargs`; `capture_locals=True` printed the path | Wrappers (`entry_point`, its generator variant, `run_in_context`) move arguments into `_Arguments` (fixed repr) and delete them before anything can raise, unpacking only inside the call expression. `key_provider_from_ref` uses the admission boxed-entry pattern (`_OpaqueBox`, `del`, `_call_closed(_ref_boxed)`). `parse_ref` deletes `text` before raising. | `tests/fileaccess/test_x1_secret.py` locals check extended to path and ref, reprs and strings nested in tuple/list/set/dict, on every chain link; `tests/fileaccess/test_entry_frame_locals.py` covers every admission entry and every wrapper; 16 red | M2a-M2f (each wrapper's `del`, the holder's repr, `key_provider_from_ref`'s `del`, `parse_ref`'s `del`): all red |
| 3 | 1/4 | Resolution ran before the closed boundary, so a refusal inherited the caller's in-flight exception as `__context__` | Every resolution refusal (`_resolve`, `current`, `require_ambient`) goes through `_unavailable`, which raises with cause and context cleared and `__suppress_context__` set. The class keeps its type (`FileAccessUnavailable`). | `tests/secret_providers/test_boundary_caller_context.py`: the six admission routes with ambient forbidden and nothing installed (1 red: `key_provider_from_ref`; the other five already met the refusal inside the boundary); `test_nested_explicit_policy.py`: six resolution routes inside a caller's handler (6 red) | M3 (drop the context clearing): 13 red |
| 4 | 1 | `write_fixed_width` put the raw path in row-failure and layout-failure text under a strict policy | Both texts use `fileaccess.shown(path, "target.path")`: the path under ambient (main's text unchanged), `target.path` under strict | `tests/fileaccess/test_fixed_width_writer_strict_text.py`: overflow and bad layout, absolute and relative locator, canary over str, repr, args, notes, cause, context and the formatted traceback; ambient text pinned to start with the path; 4 red | M4a/M4b (each text back to the raw path): red |
| 5 | 8/9 | The ambient and strict replace publishes dropped main's best-effort directory fsync, and `test_fsync_dir_survives_close_error` was deleted | `LocalOutputTxn.publish` and `RootedOutputTxn.publish` fsync the parent after a file-stage replace, swallowing fsync and close errors. The deletion was not justified (the guard it tested was lost with it); the test is restored under its name, now end to end through `write_fixed_width` on both policies, asserting the target directory's inode was fsynced and that fsync/close faults do not fail the write | 4 red | M5a/M5b (drop each fsync), M5c/M5d (stop swallowing the fsync error): all red |

Scope notes:

- Finding 5 is restored for file stages published by replace, which is where main had it (the
  fixed-width writer). The other file-replace outputs through the same transactions (`pack`,
  `quarantine_direct`, vault replace) also get the best-effort fsync; exclusive (`link`) and
  directory publishes do not, as on main. The ambient directory open is a new sink:
  `tests/sentry/file_access_registry.toml` gains exactly one entry (`_local.py:_sync_dir`,
  `os.open`), count 191 -> 192. No other allowlist or threshold changed.
- Finding 1 refusal raises `FileAccessUnavailable`, the class the forbidden-ambient refusal already
  used. That class has no registry identity (only `FileAccessDenied` does); unchanged here.

Class enumeration for finding 2 (public entries that keep a ref, path or secret in a frame local
or a decorator's arguments):

- Covered and tested: the admission entries `prepare_secret`, `prepare_legacy`, `prepare_ref`,
  `admit_mask_key` (already boxed), `key_provider_from_ref` and `parse_ref` (fixed here); the
  wrapper frames of all 41 `fileaccess.ENTRY_POINTS` members and of `run_in_context` (fixed here).
- Not covered, reported (see the round-3 hand-off): the entry functions' own frames hold their own
  parameters. Path or locator parameters: `iter_csv_chunks`, `iter_parquet_chunks`,
  `read_fixed_width`, `write_fixed_width`, `reference_tables.load_table`, `load_corpus`,
  `load_corpus_provenance`, `verify_corpus`, `load_custom_providers`, `estimate_job`,
  `estimate_job_capacity` (`base_dir`), `run_pipeline_isolated` (`staging_dir`, `output_dir`),
  `run_fk_out_of_core` (`temp_dir`), `run_subset` (`output_dir`), `unmask_pipeline`
  (`vault_path`), `vault.load_vault` (`path` and the raw `mask_key` bytes). Config parameters that
  can hold `mask_secret_ref` and file locators: `run_pipeline`, `run_mask_pipeline_chunked`,
  `run_mask_chunked`, `run_job_with_governor`, `compile_plan`, `validate_plan`,
  `run_config_only_checks`, `validate_config`, `vault_writer_for_config`, `profile_source`,
  `plan_from_yaml` (`s`). `key_provider` parameters render redacted (`SecretKeyProvider`,
  `SeedKeyProvider`, `PreparedKeyProvider` reprs). The documented promise
  (`docs/security/secret-providers.md`) covers the admission boundary only, and
  `docs/security/error-reporting-and-data-exposure.md` tells operators to turn off locals capture
  for everything else; closing these frames means boxing every public entry, a new mechanism, so
  it is reported rather than patched.

Sentries: module size and physical seam pass unchanged (no ceiling raised; `_entry.py` 322 lines).
ruff check and format clean; mypy 2.3.0 `src/decoy_engine testflight` 0 errors (703 files).

Class sweep for finding 4 (engine error text naming a file under a strict policy), reported, not
patched: `profile/_readers.py:_build_file_source` renders `{path!r}` when the descriptor's `path`
is not a `str` (a `PathLike` shows the path); `plan/_serialize.py` names an embedded snapshot's
recorded `source_path` in `dp_pinned_snapshot_digest_mismatch` text and `path`, whatever policy is
installed. Both are main's text, outside this branch's diff.

Full suite, round 3 (pyproject addopts, `pytest tests`, venvs from each tree's `uv.lock`, run on
the code at e5ac344b; the later commits change only docs):

| Python | main 1ee89e68 | head e5ac344b |
|---|---|---|
| 3.10.20 | 0 failed, 29053 passed, 5286 skipped, 6 xfailed, 65 xpassed | 0 failed, 29962 passed, 5288 skipped, 6 xfailed, 65 xpassed |
| 3.12.3 | 1 failed (`tests/unit/execution/test_categorical_weighted.py::TestCdfBuilder::test_skewed_weights_skew_cdf`, known), 28968 passed, 5370 skipped | 1 failed (the same id), 29877 passed, 5372 skipped |

The failing ids are the same for head and main on both interpreters.

Not run in round 3: native companion (native-only tests skip, as on main), CI, platform or CLI
suites, live Vault, Windows, a mutmut campaign (one hand mutation per mechanism, fourteen in all),
ruff/mypy on 3.12 (run on 3.10 only).

## 10. Remediation round 4 (Codex FINAL NO-GO on round 3: one BLOCKER, three HIGH, all class 1)

Scope by orchestrator decision: the three HIGH and every frame that holds secret material. The
general entry-frame construction for paths and configs is F31 revision 3.

### 10.1 Merge of origin/main 1132d29e (R-a release prep 0.17.0)

Merge commit 8ea85a45. Textual conflicts: `CHANGELOG.md` only. Both entries kept: this branch's
unreleased sections stay under `[Unreleased]` above main's new dated `[0.17.0]`; git had placed the
branch's 2026-10-29 "file-access coverage" section inside `[0.17.0]`, and it was moved back up
(it is not in that release).

Registry version: the hand-off expected 1.2.0 on both sides; main is at **1.1.0** (shipped in
0.17.0, pinned by `tests/fixtures/f29/reason-registry-release.json`, 891 identities). Main did not
move it, so this branch's single bump to 1.2.0 stands and was not bumped again.

What the merge then required (commit dbeec5a4):

- Main's new release pin rejects a new identity that claims an already-released `since`. The
  branch's two identities (`decoy.file_access_denied`, `runtime.secret_file_permissions_unsafe`)
  now declare `since="1.2.0"`; `docs/reasons.md` states 1.2.0.
- The merged `quality/dp_provenance.py` was 606 lines against the branch's census entry of 602
  (main's four re-pin comment lines plus this branch's policy open). The branch side of the
  lock-file open is written in four fewer lines with the same behavior: census stays 602, no
  ceiling raised.

**Semantic conflict, not resolved (owner decision needed):** main's
`tests/unit/test_reason_release_pin.py::test_an_unbumped_addition_to_the_live_catalog_fails_the_checked_in_pin`
appends an identity to the live catalog and expects the "changed without a registry version bump"
violation against the checked-in pin. That violation is only produced while the live registry
version equals the pinned one (1.1.0). Any correctly bumped addition after a release (this
branch's 1.2.0) makes it report the "already-released since" violation instead, so the test fails
on this branch and will fail on main at its first post-release identity. It is main's pinned test,
so it is not edited here; it is the one new failure in the full suites below. Likely fix (main):
build the "unbumped" live catalog at the pin's version (`live["registry_version"] = pin[...]`).
Relatedly, `scripts/release/reason_release_pin.py --check` reports the pin stale (1.1.0/891 vs
live 1.2.0/893), which is expected until the next release re-captures it with `--write`.

### 10.2 Findings and fixes

Red records and mutant results: `docs/plans/evidence/2026-11-01-a4-f13-reconcile-r4-red.txt`.
Red runs are the new tests against the code as committed before each fix (stash of the source
change), in the same lock venv (3.10.20).

| # | Class | Finding | Fix | Red first | Mutants |
|---|---|---|---|---|---|
| H1 | 1 | `parse_ref` (exported) raised `bad_secret_ref` with the caller's in-flight exception as `__context__` | `raise_bare` (moved from `_preflight` to `_errors` so `_types` can use it): cause and context cleared, context suppressed | `tests/secret_providers/test_parse_ref_caller_context.py`: 7 malformed shapes inside a handler holding a canary (message, note, decode object), plus a canary in `args`; 8 red | M1 (raise without `raise_bare`), M2 (`raise_bare` keeps context): red |
| H2 | 1 | `build_profile_source` interpolated `{path!r}` for a non-str path; the same function echoed `layout`, `format` and `type` | each through `fileaccess.shown`: the repr under ambient (text unchanged), "the value at `<position>`" under any other | `tests/fileaccess/test_profile_source_path_text.py` through the exported `build_profile_source`: `Path` with a canary directory, bytes path, layout string, format and type; strict 5 red; ambient text pinned | M3: red |
| H3 | 1 | `plan_from_yaml`: digest-mismatch and non-JSON refusals echoed the embedded `source_path`; the non-JSON one chained the decoder error; same function's unpinned-`snapshot_file` refusal echoed the path | closed-set text naming `generation.snapshots[<i>]` / the column's `snapshot_file` position under every policy; non-JSON raised outside the handler | `tests/fileaccess/test_plan_snapshot_strict_text.py`: corrupted plan under a strict policy and ambient, three cases; 6 red | M4, M5: red |
| B1 | 1 | secret material in public entry frames (`load_vault` and the rest, 10.3) | drop or box (`decoy_engine._opaque.OpaqueBox`) before anything can raise; unbox only inside the consuming call expression | `tests/fileaccess/test_secret_material_frames.py`: seeded failure per entry, every non-test frame on every chain link (engine, library, generated) checked for the raw secret and each derived key in raw, hex and escaped-repr form; 28 red, then 1 + 4 more red for the cells added below | M6-M20, M25, M26: red (second pass) |
| S | 1 | `raise` inside `except` sweep (10.4) | raise after the handler | `tests/fileaccess/test_raise_in_handler_sweep.py`; 8 red | M19, M21, M22: red |

Sentry (item 7): `tests/sentry/test_locator_hygiene.py` loses the `_build_file_source` waiver and
the "compile-time plan metadata is out of the model" limit; `source_path` and `snapshot_file` join
`PATH_NAMES`. The one site that then surfaced (`plan/_generation.resolve_pinned_snapshot`: its
text already used `fileaccess.shown`, the path was only a dict-lookup key inside the raise
expression) now raises a local, so no waiver was added. Mutant M4b (an interpolated
`source_path` in `_serialize`) turns the sentry red.

Mutation passes: first pass 22 mutants, two survived and were real gaps: M7 (`vault._fernet`
keeping the derived Fernet key as a local: no cell failed inside `_fernet`) and M14
(`make_key_resolver`'s second `del master` guarded nothing, because the HKDF call that can raise
ran before it, with the master in the frame). Fixed in 3c0ad1c8 with two new cells; second pass 24 mutants, all red; M25/M26 for `client_from_env`, red.
No commit was made with a mutant applied.

### 10.3 Public entries checked for secret material

Secret material = raw key bytes, mask keys, secret values, tokens, passwords, derived keys (not
paths, refs, seeds or configs). "None" means no such value is a parameter or local of that frame
(key providers render redacted; configs, paths and refs are F31's).

The 42 `fileaccess.ENTRY_POINTS` (the hand-off said 41; `resolve_mask_secret_ref` left and
`key_provider_from_ref` joined in round 2):

| Entry | Holds | Result |
|---|---|---|
| `vault.load_vault` | `mask_key` parameter; `_fernet`'s derived Fernet key | fixed |
| `vault.vault_writer_for_config` | resolved mask key | fixed |
| `unmask.unmask_pipeline` | resolved mask key; per-column FPE key | fixed |
| `out_of_core._runner.run_fk_out_of_core` | `pre` (mask key in repr), `mask_key`, `table_kwargs["mask_key"]` | fixed (key boxed in `OocPreflight`, passed in the driver calls only; line-neutral, census 681) |
| `_sequential.run_sequential` | `ctx` (`StrategyContext` rendered `mask_key`) | fixed (`repr=False`) |
| `keyprovider.key_provider_from_ref` | ref (boxed, round 3) | none |
| `run_pipeline`, `run_mask_pipeline_chunked`, `run_mask_chunked` | `key_provider` objects (redacted), `derive_key` callable, contexts holding the provider | none |
| `generate_tables`, `generate_rows`, `execute_pinned` | `derive_key` callable, generation `seed` | none |
| `profile_source` (the `_source.py:48` site) | `config`, sampling `seed` | none (config: F31) |
| `estimate_job`, `estimate_job_capacity`, `run_job_with_governor`, `run_pipeline_isolated`, `run_fk...`'s siblings `plan_subset`, `run_subset`, `run_subset_preflight` (`job_seed`) | configs, paths, seeds | none |
| `compile_plan`, `validate_plan`, `run_config_only_checks`, `validate_config`, `plan_from_yaml`, `compile_pinned_plan`, `compile_pinned_plan_with_generation`, `apply_table_transforms`, `validate_joint_mask_config`, `strategy_config_fingerprint` | configs / plan text | none |
| `read_fixed_width`, `write_fixed_width`, `iter_csv_chunks`, `iter_parquet_chunks`, `load_table`, `load_corpus`, `load_corpus_provenance`, `verify_corpus`, `load_custom_providers`, `apply_quarantine`, `run_discovery_sql`, `profile_columns` | paths, frames, SQL | none |

Other exported names (`decoy_engine.__all__`, and the public `keyprovider`, `vault`, `context`,
`determinism`, `secret_providers`, `secret_providers.vault` modules):

| Name | Holds | Result |
|---|---|---|
| `VaultWriter.__init__`, `.assert_keyed_with`, `vault.assert_vault_writer_keyed` | `mask_key` parameter | fixed |
| `context.make_key_resolver` (and its resolver closure) | master, pipeline key | fixed (boxed; closure holds the box) |
| `keyprovider.require_mask_key`, `resolve_mask_key` | a short key on refusal | fixed |
| `keyprovider.SecretKeyProvider` | `secret` in the generated `__init__` on a weak-secret refusal | fixed (hand-written `__init__`) |
| `determinism.derive`, `derive_index`, `derive_value` | `seed` (the mask key on a keyed surface), derived HMAC key | fixed (every failure drops `seed`; `derive_index` still checks the pool size first) |
| `secret_providers.REQUIREMENTS[*].prepare` | material | fixed (refusals outside the handler) |
| `secret_providers.SecretBuffer(data)` | data on a type error | fixed |
| `secret_providers.vault.client_from_env` | the environment mapping (VAULT_TOKEN; all of `os.environ`) | fixed |
| `prepare_secret`, `prepare_legacy`, `prepare_ref`, `admit_mask_key` | boxed (round 3) | none |
| `parse_ref` | ref (deleted, round 3) | none; now raises bare (H1) |
| `keyprovider.mask_key_from_provider`, `SeedKeyProvider`, `job_seed_for_config` | seeds; the key only in a return expression | none |
| `ExecutionContext`, `PandasExecutionAdapter.run/run_sequential/run_single`, `register_secret_provider`, `SecretProviderRegistry`, `ResolvedSecret`, `PreparedSecret`, `PreparedKeyProvider`, `SecretCache`, `VaultContext` | callables, key providers, buffers (all render redacted); `VaultContext.token` is a `repr=False` frozen dataclass built and used inside the admission boundary | none |

Not covered, reported (the same class one level down; not patched in this round): internal frames
below an entry still take the key as a parameter or local, for example
`native._chunked_entry._native_route` (`mask_key`), the out-of-core drivers and kernels
(`_stream_table`, `stream_table`, `_mask.mask_batch`/`mask_column`, `_hash_native`, ...),
`_vault_collect.assert_vault_inputs`, `transforms.text_mask.mask_cell`,
`transforms.fpe.fpe_encrypt_value`, `native.protocol.providers_mask.*`,
`determinism._hkdf.hkdf_sha256`, `context._hkdf_sha256`, and library frames (cryptography,
hvac) handed a key. A failure inside one of them leaves the key in that frame. The docs say so
and keep "leave locals capture off" as the operator rule.

### 10.4 Sweep: `raise` inside `except` in fileaccess/ and secret_providers/

| Site | Form | Result |
|---|---|---|
| `fileaccess/_local.py` `LocalOutputTxn.publish` | `FileExistsError ... from None`, context = the link error naming both paths | fixed |
| `fileaccess/_output.py` `RootedOutputDir.is_empty_or_absent` | `deny(...) from None`, context = the OSError naming the leaf | fixed |
| `secret_providers/_requirements.py` `_prepare_fernet`, `_prepare_aes_gcm` | `SecretMaterialInvalid from None`, context = library validation error | fixed (B1) |
| `secret_providers/_requirements.py` `decode_material`, legacy decode (three sites) | `from None`, context = `UnicodeDecodeError` holding the bytes | fixed |
| `secret_providers/_types.py` `_parse_vault` (two sites) | `from None` | listed: runs inside `parse_ref`'s `contextlib.suppress`; swallowed; `parse_ref` raises bare |
| `secret_providers/vault.py` `_parse_rfc3339`, `_to_resolved` | `from None` | listed: provider code run inside the admission boundary, which records the code only and raises fresh and bare |
| `secret_providers/vault.py` `_hvac` | `from None`, context = `ImportError` (no value) | listed: also reached from `client_from_env` outside the boundary; the context names only the missing module |
| every other `raise` inside an `except` in both packages | bare re-raise | unchanged (re-raises the same exception) |

### 10.5 Suites

Full suite (pyproject addopts, `pytest tests`, venvs from each tree's `uv.lock`, extras dev, lint,
vault, cloud; main in a detached worktree of origin/main 1132d29e; head = c133fb3d, the code of
this round; the record commit after it changes only docs):

| Python | main 1132d29e | head c133fb3d |
|---|---|---|
| 3.10.20 | 0 failed, 29095 passed, 5286 skipped, 6 xfailed, 65 xpassed | 1 failed, 30081 passed, 5288 skipped, 6 xfailed, 65 xpassed |
| 3.12.3 | 1 failed, 29010 passed, 5370 skipped, 6 xfailed, 65 xpassed | 2 failed, 29996 passed, 5372 skipped, 6 xfailed, 65 xpassed |

Failing ids, head versus main: 3.12 both have
`tests/unit/execution/test_categorical_weighted.py::TestCdfBuilder::test_skewed_weights_skew_cdf`
(known). Head adds, on both interpreters,
`tests/unit/test_reason_release_pin.py::test_an_unbumped_addition_to_the_live_catalog_fails_the_checked_in_pin`,
the semantic merge conflict of 10.1 (main's test, not edited). No other difference.

ruff check and format (`src tests testflight scripts`): clean on 3.10 and 3.12. mypy
(`src/decoy_engine testflight`, 3.10): 0 errors, 704 files. Module-size and physical-seam
sentries pass; this round changes no census entry and raises no ceiling (the only census delta
since round 3 is main's own shrink of `scripts/gen_proof_manifest.py`, 1237 -> 1230).

### 10.6 Not run

The native companion is not installed (native-only tests skip, as on main). No CI run, no
platform or CLI suite, no live Vault, no Windows run, no mutmut campaign (hand mutants only, 26
in the final passes). mypy on 3.12 stops on the same two environment errors as main (`tomli` stub
missing, numpy stub syntax under the configured `python_version` 3.10); it is run on 3.10.

## 11. Round 5: the release-pin test (class 8)

`tests/unit/test_reason_release_pin.py::test_an_unbumped_addition_to_the_live_catalog_fails_the_checked_in_pin`
held only while the live `registry_version` equalled the pinned one (the semantic conflict of
10.1). It now sets its copied catalog to the pinned version, adds an entry claiming that version
and asserts the exact "changed without a registry version bump" violation; a new sibling adds the
same entry under a version above the pin and asserts no violation. Only that file changed; no
other pinned test, sentry, fixture or golden; the pin is not re-captured
(`scripts/release/reason_release_pin.py --check` still reports 1.1.0/891 vs 1.2.0/893 stale, as
expected until the release step writes it).

Red first and mutants: `docs/plans/evidence/2026-11-01-a4-f13-reconcile-r5-red.txt`. Original test
red on be26383f; edited file 11 passed on the same head. Mutants of `pin_violations`: inverted
version comparison, both tests red; version comparison ignored (digest checked at any version),
the bumped sibling red; digest rule dropped, the unbumped test red.

Full suite (pyproject addopts, `pytest tests`, lock venvs, extras dev, lint, vault, cloud), head
e60c93f9 versus main 1132d29e (main unchanged since round 4; its runs in section 10.5 are the
baseline):

| Python | main 1132d29e | head e60c93f9 |
|---|---|---|
| 3.10.20 | 0 failed, 29095 passed, 5286 skipped | 0 failed, 30083 passed, 5288 skipped |
| 3.12.3 | 1 failed (`test_skewed_weights_skew_cdf`), 29010 passed, 5370 skipped | 1 failed (the same id), 29998 passed, 5372 skipped |

Failing ids are identical to main on both interpreters. ruff check and format clean on 3.10 and
3.12; mypy (3.10) 0 errors, 704 files.

Not run: native companion, CI, platform or CLI suites, live Vault, Windows, mutmut.

## 12. Round 6: secret material in engine frames, closed by census (class 1)

Codex re-check at f0d33076: NO-GO, one BLOCKER, the round-3 class again (class 1, secret
material in engine-frame locals): `ModelPackLoader._check_signature.key` (the raw
`DECOY_PACK_SIGNING_KEY` bytes while it raises), `derive_pack_signing_key.master` on an invalid
master, `sign_pack.key` on a corrupt manifest. Origin main a3fe49e8 (SBOM only) merged first,
no conflicts (2aa00d7e).

### 12.1 The census, derived from the code

`scripts/secret_material_census.py` walks the AST of `src/decoy_engine` (no selected list) and
lists every binding where secret material can sit in a frame a traceback keeps: secret-named
(`key|secret|master|signing|password|token|seed|ikm|prk|okm|material|root`) parameters annotated
bytes-like or unannotated; secret-named locals bound to an environment read, a
`FileRole.SECRET` read, box contents, a bytes-returning package function, a bytes operation, a
bytes class attribute, or an alias, slice or sum of tracked material; closure cells (nested
`def` and lambda); environment reads of KEY/SECRET/TOKEN or computed names; calls passing
`FileRole.SECRET`; `OpaqueBox`es holding tracked material; secret-named bytes class fields.
Committed: `tests/sentry/secret_material_census.json`, 257 entries over 227 functions or classes
in 87 files. Verdicts: wiped 183, boxed 28, hidden 20, not-secret 15, no-raise 7, unbound 2,
contained 2. `scripts/_secret_census_verdicts.py` re-checks wiped, boxed, unbound, hidden and
contained from the AST; no-raise (five Protocol stubs, `_require_mask_key` on an empty key,
`GenDeriveContext.__init__`) and not-secret (the configured `job_seed`, 8-byte pool RNG seeds,
formula text, a vault ciphertext block, a version label and a provider object under the
admission boundary) carry their reason in the census.

Beyond the three listed sites the census found the class in ~190 more functions (masking
kernels, FF1, text_mask, code_set, joint_mask, date_shift, pools, OOC drivers, native providers,
vault, unmask), derived keys bound as locals, closure cells, a lambda cell, the env secret
provider, the dataclass-generated `__init__` of `ShadowContext` and `ProviderSpec` (a raising
`__post_init__` runs in the frame that holds every field), and secret-named bytes fields printed
by dataclass reprs. Found by review next to the census: `verify_manifest` bound the expected MAC
of the manifest under check (a forgery value on a corrupt `manifest_hmac`), and
`fileaccess.read_checked` kept the bytes already read when a later read or the close failed.

### 12.2 Fixes (`b5fc1a68`)

Wiped (`try` / `except BaseException: del <name>; raise`) for every parameter entry; boxed or
never bound for derived keys; `_opaque.validate_after_init` for the two validating dataclasses;
`repr=False` on the fields; env text wiped and the signing key boxed in
`load_signing_key_from_env` / `_check_signature`; `read_checked` clears its buffer. No module
ceiling raised: `text_mask` synthetic values moved to `_text_mask_synthetic.py` (census 708 ->
650), `code_set` picks to `_codeset_pick.py`, the two drivers' `_open_joiner` to one
`open_joiner` (runner 681 -> 644, stream_driver leaves the census), `_column_seed` duplicates to
`_compat`, two `SeqNativeRun` helpers to `_sequential_native_ops`; `columns.py` 660 -> 658.
`d23794cd` points two OOC test fixtures at `_compat._column_seed` (import path only).

Docs (`8710fa9c`): `file-access-policy.md` states exactly what the census covers and what stays
out (paths, refs and configs in entry frames: F31, owner decision).

### 12.3 Tests, red first, mutants

- `tests/sentry/test_secret_material_census.py`: re-derives the census (missing, stale, every
  verdict), plus the seeded violation `tests/sentry/fixtures/secret_census_seeded.py` (a new
  function taking `signing_key: bytes` and raising) copied into a scratch tree: reported missing;
  a census row claiming it wiped fails the verdict check; the wiped version passes.
- `tests/fileaccess/test_secret_census_frames.py`: one cell per census function taking material
  (165), canary key in raw/16/8-byte and str forms, every other argument a bomb; walks the locals
  of every `decoy_engine` frame on every chain link (cause, context, group).
- `tests/fileaccess/test_secret_census_dedicated.py`: the three sites with real bad inputs
  (unsigned and tampered packs with the env key set; a non-32-byte master as bytes and str; a
  corrupt and an incomplete manifest), the expected-MAC case, a closure cell, the validated
  dataclasses, a SECRET read failing part way, an unencodable env secret, the vault writer under
  a refusing policy.

Red on the pre-fix head 2aa00d7e (new files copied into a detached worktree): 181 failed,
29 passed (165 frame cells, 13 dedicated cells, 3 sentry cells red). Mutants on the fixed tree,
one at a time, restored and hash-checked, none committed: the box removed at `_check_signature`,
the wipe removed at `derive_pack_signing_key` and at `sign_pack`, a census entry dropped
(`sign_pack.key`), `validate_after_init` removed from `ShadowContext`, the closure owner's wipe
removed, `read_checked`'s clear removed, a kernel wipe removed (frame cell and verdict checker),
the expected MAC bound again: all ten red. Record:
`docs/plans/evidence/2026-11-01-a4-f13-reconcile-r6-red.txt`.
