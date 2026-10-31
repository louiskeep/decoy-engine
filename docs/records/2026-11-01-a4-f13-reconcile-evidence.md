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
  `native/_chunked_entry.py` -> `native/_chunked_result.py` (666 -> 620). No ceiling raised.
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

Full suite, pyproject addopts, from the lock: see section 7a.

## 8. What was not run

The native companion is not installed (native-only tests skip, as on main). No CI run, no
platform or CLI suite, no live Vault (`test_vault_live.py` skips as on main), no Windows run (the
`os.name == "nt"` branch is covered by a patched-`os.name` test only), no mutmut campaign (one
hand mutation per mechanism, section 5).
