Status: plan, rev 2 (Opus, 2026-11-01). Handoff. Risk class R2 (plan only, no code).

# PLAN: B-6 tokenization vault, crypto remediation (engine, platform, CLI)

Branch `plan/b6-vault-crypto-remediation` off engine `origin/main` `1132d29e` (R-a, 0.17.0). Rev 2
resolves the Codex plan gate NO-GO on rev 1 (`27032a1b`: 1 BLOCKER, 2 HIGH, 4 MEDIUM, 1 LOW) and
applies the owner decisions of 2026-11-01 (§7). The plan builds after A-4 (`claude/jolly-carson-3k5stf`)
merges; A-4 is not on main at the time of writing. Nothing here is built; every slice is specified
red first.

Bases read for this plan (every line cited below was re-read at these refs on 2026-11-01):

| Repo | Ref | Sha | Role |
|---|---|---|---|
| decoy-engine | `origin/main` | `a3fe49e8` | current main; one commit past the branch base `1132d29e` (#359, SBOM scripts and `docs/sbom.md` only, no file this plan cites) |
| decoy-engine | `origin/feat/b6-batch-vault-writer-engine` | `e3c539d6` | B-6 batch writer branch under review (merge-base with main `0090f6f9`) |
| decoy-engine | `origin/claude/jolly-carson-3k5stf` | `a19889a5` | A-4 file access, merging before this plan builds (`fileaccess`, `_opaque`, registry 1.2.0). Rev 1 read `e60c93f9`; every A-4 line below is re-cited at `a19889a5` |
| decoy-platform | `origin/main` | `2d40c9a` | #131-#133 landed since rev 1 (`19b5007`); the vault files cited are byte-identical between the two (`git diff --stat 19b5007 2d40c9a -- api/vaults/store.py api/jobs/v2_full_frame.py api/jobs/v2_vault.py api/jobs/v2_token_vault.py tests/test_vault_durability.py` is empty) |
| louiskeep/decoy (CLI) | `origin/main` | `a5d36b4` | #76 (`57e8b81`) landed since rev 1 (`df4c069`): repin to `decoy-engine==0.17.0` and `LocalTokenVault.put -> str`; see N2 |

Every rev 1 measurement was taken from the engine lock (`uv.lock`, `cryptography 49.0.0`, Python
3.11.18, `uv sync --frozen --all-extras` on `1132d29e`, `.venv/bin/python`) against a worktree of
the branch named in each row; those numbers stand as recorded in §9. The one new measurement in
rev 2 (the PyCA empty-salt probe, §1) ran on the container's system interpreter (Python 3.13.18,
`cryptography 50.0.1`) against a standalone copy of main's `_hkdf.py`, because no lock environment
was built in this session; it is labelled as such where it is used. The platform and CLI were read,
not executed: every platform and CLI claim is VERIFIED-BY-READ and its slice names the test that
proves it in CI.

## 0a. Owner decisions applied (2026-11-01; delegated, not reopened)

- D2 / Q1: HARD CUT. Pre-release rule: no backward compatibility, no migration of data. A vault
  minted under the old derivation is refused with one registered code,
  `token_vault_derivation_unsupported`: a new tokenization against it fails closed before the
  engine runs and never continues with the new key. No marker row, no dual derivation, no schema
  change for compatibility. The rev 1 "option (b)" design and its machinery (`token_derivation`
  columns, the `TOKEN_DERIVATION` export, the label-recording sentence of D3) are removed from
  this plan.
- Q4: the CLI refuses to overwrite an existing vault it did not create in this run
  (`vault_exists`, registered, EXIT_USAGE), on both `run --vault` routes.
- Q3: the platform's documented non-fatal file-vault write becomes fatal. The test that decides
  it is named in D1 ("Platform: fatal, decided by the failure order").

## 0b. Gate findings at 27032a1b and where each is resolved

| Gate finding | Class | Resolved in | Slice | Red-first test |
|---|---|---|---|---|
| 1 BLOCKER, D1 §6 (rev 1 lines 121, 370): "no leaf and no stage" is unreachable; monkeypatching `os` is not a crash test; recovery semantics unspecified | 3, 2 | D1 steps 1-7, the kill-point table and the recovery table | S4a, S4b | `tests/unit/test_vault_publish.py::test_sigkill_at_each_publish_step_leaves_a_loadable_leaf_or_none[K1..K7]` (subprocess, SIGKILL), `::test_publish_fsyncs_file_then_directory` (strace), `::test_a_failure_after_the_stage_exists_leaves_no_leaf_and_no_stage[write, file_fsync, link, dir_fsync]` (fault injection on the error path, labelled as such) |
| 2 HIGH, D1 inverse partial commit (rev 1 lines 128, 375) | 2 | D1 "Callers" table: NEW vs EXISTING, retained vs rolled back, per caller | S6a-ii, S7a, S7b | platform `tests/test_vault_durability.py::test_output_failure_after_a_file_vault_commit_removes_the_vault`, `::test_output_failure_after_the_token_commit_keeps_rows_and_a_rerun_adds_none`; CLI `tests/e2e/test_run_vault_gating.py::test_output_failure_after_the_vault_commit_removes_the_vault`; CLI `tests/e2e/test_tokenize_cli.py::test_output_failure_after_commit_removes_a_vault_this_run_created`, `::test_output_failure_after_commit_keeps_a_preexisting_vault_and_its_new_rows` |
| 3 HIGH, reason registry (rev 1 lines 338, 356) | 10 | §5 "Reason registry" rule: main's value at build time plus one minor, `since` equals that literal; manifest v1 immutable; release pin recaptured only at the cut | S2, S4b | `tests/unit/test_reason_release_pin.py::test_live_registry_honours_the_released_pin` (red the moment an identity is added without the bump), `::test_no_identity_claims_a_since_newer_than_the_live_registry_version` (new) |
| 4 MEDIUM, N1 (rev 1 lines 259, 390) | 7 | D6 (N1): a second session's `complete()` under `lock_timeout`, one parameter per boundary | S6b | platform `tests/test_vault_concurrency_pg.py::test_write_lock_is_reacquired_after[commit, rollback, savepoint_rollback]` |
| 5 MEDIUM, N3 (rev 1 lines 125, 374) | 10, 8 | D1 step 6 and the N3 row | S4a | `tests/unit/test_vault_publish.py::test_an_existing_vault_in_a_read_only_parent_is_preserved_and_no_stage_remains` (replaces pin 2), `::test_a_hardlinked_twin_is_not_rewritten` (replaces pin 1) |
| 6 MEDIUM, D7 empty salt (rev 1 lines 268, 391) | 9 | D7: the empty-salt refusal is preserved on the one-shot, vectors cover it, probe in §1 | S1 | `tests/unit/determinism/test_hkdf.py::test_oneshot_rejects_empty_salt`, `::test_engine_vectors_are_unchanged_by_the_pyca_swap` (the vector file holds the refusal case) |
| 7 MEDIUM, slice sizes (rev 1 line 299) | n/a | §4 budget table; S4, S6a and S7 split | all | n/a |
| 8 LOW, F4 canary (rev 1 line 383) | 8 | D4 "Canary": ASCII key bytes searched in the representation the helper inspects; mutation retains only `mask_key` | S3 | `tests/unit/execution/test_tokenize_frame_locals.py::test_vault_failure_traceback_holds_no_plaintext_or_key[per_row, batch, lazy_mapping]` |

Two defects found while re-reading for rev 2 that no gate finding lists are registered as N4 and N5
in §0 and in the repeat-class register (§8); each has its own test and slice. They are reported,
not folded into another finding.

## 0. Findings register

Classes: (1) value-bearing output; (2) cleanup missing on an exit path; (3) state machine and
concurrency; (4) route parity and silent fallback; (5) referential integrity; (6) untrusted producer;
(7) check then use; (8) weak tests and overclaimed evidence; (9) behavior-preserving refactor drift;
(10) frozen contract drift.

| Id | Sev | Class | Where (re-read) | Design | Slice | Red-first test (file::function) |
|---|---|---|---|---|---|---|
| F1 | HIGH | 3 (+2 crash window, +10 pinned partner) | engine `vault.py:305` on main; A-4 `vault.py:332-345`, `fileaccess/_local.py:225-237`, `_output.py:71-77, 246-273`, `adapters/serial.py:45-47`; platform `v2_full_frame.py:166-172, 205-206`, `v2_vault.py:78-100`; CLI `run.py:840, 854`, `tokenize.py:341, 348` | D1 | S4a, S4b, S6a-ii, S7a, S7b | `tests/unit/test_vault_publish.py::test_sigkill_at_each_publish_step_leaves_a_loadable_leaf_or_none`, `::test_two_real_writers_one_publishes_one_fails_closed`, `::test_publish_fsyncs_file_then_directory`; platform `tests/test_vault_durability.py::test_file_vault_commit_failure_publishes_nothing`; CLI `tests/e2e/test_run_vault_gating.py::test_vault_commit_failure_writes_no_outputs` |
| F2 | HIGH | 1 | B-6 `_tokenize.py:253-258`; `_adapter.py:98-157` (no vault scope in either protocol); platform `v2_token_vault.py:158-166` supplies none; CLI `_token_vault.py:215-251` supplies none | D2 | S2, S6a-i, S7a | `tests/unit/execution/test_tokenize_domain_separation.py::test_two_vaults_same_key_namespace_plaintext_mint_different_proposals`, `::test_writer_without_derivation_scope_fails_closed` |
| F3 | HIGH | 1 | B-6 `_tokenize.py:255` vs `kernel/_scalar.py:88` and `generation/pool/_canonicalize.py:120` (NFC string branch) | D3 | S2 | `tests/unit/execution/test_tokenize_domain_separation.py::test_token_payload_never_equals_hash_truncate_32`, `::test_tokenize_subkey_vector` |
| F4 | MEDIUM | 1 (second appearance, see §8) | B-6 `_tokenize.py:147-155` (`df`, `col`, `normalized`, `distinct`, `ctx`), `:233-258` (`distinct`, `mask_key`, `plaintext`, `minted`), `:181` (`args`) | D4 | S3 | `tests/unit/execution/test_tokenize_frame_locals.py::test_vault_failure_traceback_holds_no_plaintext_or_key` |
| F5 | MEDIUM | 4 (guard placement) | B-6 `_tokenize.py:169-186` sanitizes `fn(*args)` only; `:212` consumes `result.items()` outside it; `:192` `bool(token)` on an unchecked type | D5 | S3 | `tests/unit/execution/test_tokenize.py::test_lazy_mapping_whose_items_raises_is_sanitized`, `::test_hostile_key_and_token_objects_are_rejected_inside_the_boundary` |
| F6 | MEDIUM | 8 | B-6 `tests/unit/execution/test_tokenize.py:535-541` (one fixed parity example), `:565-579` (fabricated winner); platform has no batch seam on main (`v2_token_vault.py:146-166`) | D6 | S5, S6b | engine `tests/property/test_tokenize_parity.py::test_batch_and_per_row_emit_identical_tokens`; platform `tests/test_vault_batch_pg.py::test_two_sessions_interleave_lookup_many_and_put_many`, `::test_partial_put_many_after_a_concurrent_commit`, `::test_batch_commit_failure_publishes_nothing` |
| F7 | MEDIUM | 9 (the swap must be byte-identical; the refusal must survive it) | `determinism/_hkdf.py:1-95` (stdlib HKDF; docstring lines 8-13 say PyCA is not a dep, which is false since Task 5.2; `hkdf_extract` refuses an empty salt at `:54-59` and `hkdf_sha256` at `:90-95` inherits the refusal through it); callers `_derive.py:213,303`, `context.py:348-371` (zero salt), `profiling/model_pack/provenance.py:114` (zero salt) | D7 | S1 | `tests/unit/determinism/test_hkdf.py::test_engine_vectors_are_unchanged_by_the_pyca_swap`, `::test_oneshot_rejects_empty_salt`, `tests/sentry/test_hkdf_is_pyca.py::test_hkdf_module_delegates_to_cryptography` |
| F8 | LOW | 8 | B-6 `docs/handoffs/inputs/b6-engine-api-note.md:2` ("platform half not started"), `:93-96` ("the engine will never relay it"; false for F5), `:114-115` ("or the job seed pre-GA"; false since F13: `vault.py:349-350`, `test_tokenize.py::test_tokenize_requires_a_secret_key`) | D8 | S5 | `tests/sentry/test_b6_note_currency.py::test_note_signatures_and_claims_match_code` |
| N1 | HIGH | 7 | platform `api/vaults/store.py:88,160-165`: `_write_locked` is never cleared on commit or rollback (`grep after_commit\|after_rollback\|after_soft_rollback api/vaults` finds only the rotation launcher at `:391`), so a store reused after `session.commit()`, `session.rollback()` or a savepoint rollback skips `SELECT ... FOR UPDATE` and writes unlocked, racing `complete()` (`rotation.py:205-214`, which locks first) | D6 (N1) | S6b | platform `tests/test_vault_concurrency_pg.py::test_write_lock_is_reacquired_after[commit, rollback, savepoint_rollback]` |
| N2 | resolved upstream | 10 | rev 1 read CLI `df4c069` where `put` returned `None`. CLI main `a5d36b4` (#76, `57e8b81`) returns the stored token (`_token_vault.py:223-251`, docstring "return the stored token, which the engine (>=0.16.0) emits") and pins `decoy-engine==0.17.0` (`pyproject.toml:103`); `tests/unit/test_token_vault.py::test_put_is_idempotent_on_same_token` asserts the return on both paths. Nothing to build; S7a repins to R-b only | D2 (CLI) | S7a | existing `test_put_is_idempotent_on_same_token` stays; no new test |
| N3 | MEDIUM | 10 | A-4 `tests/fileaccess/test_char_vault_in_place.py:23-45` pins the in-place truncate rewrite (a hardlinked twin written through, a writable vault in a read-only directory rewritten) that F1 removes; the pin and the behavior move in one commit, and both pins are replaced, not only the first | D1 step 6 | S4a | `tests/unit/test_vault_publish.py::test_a_hardlinked_twin_is_not_rewritten` (replaces `:23-30`), `::test_an_existing_vault_in_a_read_only_parent_is_preserved_and_no_stage_remains` (replaces `:33-45`) |
| N4 (new, rev 2) | MEDIUM | 7 | CLI `tokenize.py:326` reads `vault_preexisted = vault.exists()`, then `_token_vault.py:109` creates with `os.open(path, O_RDWR \| O_CREAT, 0o600)` (no `O_EXCL`). Two first runs on one path both observe "absent", both create, both believe they own the file; the one that fails removes the other's committed vault (`tokenize.py:383`). D1's rollback for a NEW vault relies on ownership being exact, so this is on the path this plan changes | D1 (CLI tokenize) | S7a | CLI `tests/unit/test_token_vault.py::test_created_is_false_for_a_path_that_already_exists`, `::test_exactly_one_of_two_processes_creates_one_path` |
| N5 (new, rev 2) | HIGH | 4 (route parity; second class-4 instance, §8) | platform relationship routes publish table by table at `v2_sequential.py:450` (`_materialize_staged_sequential_outputs`, shared by the sequential and out-of-core FK routes through `_run_relationship_job_to_targets`, `v2_out_of_core.py:406-420`) and persist the file vault afterwards at `route_dispatch_exec.py:246-247` ("the call above already published every declared target"). Rev 1 reordered the full-frame route only (`v2_full_frame.py`); admission prices vault state on these routes (`admission.py:570-581`), so vault jobs do reach them | D1 (platform, both route families) | S6a-ii | platform `tests/test_vault_durability.py::test_relationship_routes_persist_the_vault_before_the_first_table_publishes[sequential_fk, out_of_core_fk]`, `::test_relationship_route_output_failure_removes_the_vault` |

Defects found in the existing code that are NOT folded into any move: N1, N3, N4, N5 each have
their own row, test and slice step. N2 is closed by read and kept for the record.

## 1. What was verified, and how

Probe script: `scratchpad/probes/probe_b6.py` (not committed; its content is reproduced in §9) run
with the lock env against the B-6 worktree. Baseline on B-6 `e3c539d6`:
`tests/unit/execution/test_tokenize.py tests/unit/determinism tests/unit/test_vault.py`: 132 passed.

| Claim | Result | Where it fails before the fix |
|---|---|---|
| F3: `hash` with `truncate: 32` equals the token payload for the same `(mask_key, namespace, NFC(plaintext))` | True (probe) | `_tokenize.py:255` and `kernel/_scalar.py:88` share `derive(mask_key, namespace, NFC(pt).encode())` |
| F2: the proposal is a pure function of `(mask_key, namespace, plaintext)` | True (probe, and by construction at `_tokenize.py:253-258`) | no vault input exists on either protocol (`_adapter.py:98-157`) |
| F4: frames on the `StrategyError` traceback that hold the plaintext or key | `run: df, ctx, col, normalized, distinct`; `_resolve_tokens: distinct, mask_key, plaintext, minted`; `_vault_call: args` (probe) | listed lines |
| F5: a batch writer whose returned mapping raises from `items()` | `RuntimeError` escapes `_vault_call`, message quotes the plaintext (probe) | `_tokenize.py:212` |
| F7: PyCA `HKDF`/`HKDFExpand` equal `_hkdf` byte for byte | True on the engine salt, the RFC A.2-shaped vector, the 255*32 maximum, the 32-zero-byte salt, and expand-only (probe, lock env) | n/a (behavior-preserving) |
| F7 empty salt (rev 2): PyCA `HKDF(salt=b"")` derives the same bytes as `salt=None` and as `salt=b"\x00" * 32`; main's `hkdf_sha256(salt=b"")` raises `ValueError("hkdf_extract: salt must be non-empty...")` | True, True, raises (probe, system Python 3.13.18 + `cryptography 50.0.1`, standalone copy of main `_hkdf.py`; §9) | a naive one-shot swap would accept the empty salt and silently derive under the all-zero salt, the degrade QA-10 F12 (2026-06-01) refused |
| F1 engine: `Path.write_bytes` straight to the final path | main `vault.py:305`; A-4 ambient route opens the target with `O_TRUNC` in place (`_local.py:34, 225-231`) and never fsyncs; A-4 strict route stages and links (`_output.py:254-268`) but never fsyncs the file data (`serial.py:45-47`), swallows the directory fsync error (`_output.py:71-77`, `_local.py:178-193`) and unlinks the stage after the link with the error swallowed (`_output.py:266-269`) | by read |
| F1 callers publish outputs before the vault commits | platform `v2_full_frame.py:168` (`write_v2_outputs`) precedes `:206` (`_persist_job_vault`), and `v2_vault.py:78-80` documents that a vault write failure does not fail the job; relationship routes: `v2_sequential.py:450` precedes `route_dispatch_exec.py:247` (N5); CLI `run.py:840` precedes `:854`; CLI `tokenize.py:341` precedes `:348` | VERIFIED-BY-READ |
| Platform two-writer serialization | `store.py:138-158` takes `SELECT ... FOR UPDATE` on the vault row per transaction on Postgres (nothing on SQLite); real two-session races exist for the per-row seam only (`tests/test_vault_tokenize_race_pg.py::_RaceWriter`), gated on `DECOY_TEST_DB_BACKEND=postgres` in the `backend-postgres` CI leg ("PG-semantic tests must run (by marker, fail the leg if any skip)") | VERIFIED-BY-READ; N1 is the gap |
| Platform batch seam | absent on main; the old batch branch is not a usable base (its diff against main deletes `test_vault_concurrency_pg.py`, `test_vault_durability.py`, `rate_limit.py`) | VERIFIED-BY-READ |
| Platform relationship routes stage before they publish | the engine run commits every table to `stage_dir/{table}.parquet` (`ParquetTransactionalSink`) before `_materialize_staged_sequential_outputs` publishes them one at a time (`v2_sequential.py:291-350`); the vault writer is complete when that loop starts, so the vault can be persisted before the first table publishes without buffering | VERIFIED-BY-READ |
| CLI local vault | SQLite, cleartext by design, `put -> str` since #76, no scope, commit after outputs, create without `O_EXCL` (N4) | VERIFIED-BY-READ at `a5d36b4` |
| CLI chunked route | `_run_chunked_mask` (`run.py:1398-1450`) writes each table's output as chunks arrive (`_write_chunked_output`) while the vault writer accumulates entries; the vault is written once, after every table (`:854`), so vault-first needs the outputs staged | VERIFIED-BY-READ |
| Registry version | main `a3fe49e8`: `REGISTRY_VERSION = "1.1.0"` (`reasons/_registry.py:24`), release pin at 1.1.0 (891 identities); A-4 `a19889a5`: `"1.2.0"` with `since="1.2.0"` on `runtime/_secret_provider.py:85` and `decoy/_core.py:53`; A-4 regenerates neither fixture (`git diff --stat origin/main..a19889a5 -- tests/fixtures/f29` touches `consumer-contract.json` only) | by read |
| CLI `rule_codes` | the engine half is derived from the pinned engine's registry at first use (`rule_codes.py:9-15`); a registered code needs no literal there | by read |

Unverifiable from this checkout: PostgreSQL behavior and crash behavior of the platform store. Each
is covered by a named test in S6a/S6b that runs in the `backend-postgres` leg with a real
`postgres:15`, and by a crash-injection test that uses two sessions, not mocks. Power-loss
durability of `fsync` is a kernel property and is not tested; the engine tests prove the order of
the calls (strace) and the crash consistency of names and contents (SIGKILL).

## 2. Established methodology (surveyed; the designs follow it, nothing is invented)

- Purpose separation of derived keys: RFC 5869 §3.2 (`info` binds the derived key to its
  application context; may carry identifiers), NIST SP 800-108r1 §4 (a KDF input holds a
  `Label` naming the purpose and a `Context` naming the parties or identifiers; keys for
  different purposes come from different label/context pairs). The engine already uses this
  shape: `vault-key/v1` (`vault.py:88`) and `ff1-key/v1` (`_derive.py:126`) are purpose labels
  fed through `derive` to mint a subkey from the mask root. D2/D3 add `tokenize-key/v1` with the
  vault scope as the context, through the same `derive` envelope. No new primitive.
- KDF implementation: PyCA `cryptography.hazmat.primitives.kdf.hkdf.HKDF` and `HKDFExpand`
  (RFC 5869), PyCA `cryptography.hazmat.primitives.hmac.HMAC` for the extract step. The platform
  already derives fingerprint keys with PyCA `HKDF` (`api/crypto/vault_key.py:187`). PyCA maps an
  absent salt to `HashLen` zero bytes and an empty salt to the same HMAC key (§1 probe); the
  engine's explicit refusal of an empty salt is an application rule layered on the primitive, as
  QA-10 F12 recorded, and D7 keeps it on both entry points.
- Crash-atomic file publication: write to a same-directory stage, `fsync` the file, `link` or
  `rename` into the leaf, `fsync` the directory (POSIX; the pattern SQLite's atomic-commit notes
  and PostgreSQL's `durable_rename` implement). Recovery follows the same sources: the leaf name
  is the only authoritative name, a stage is never read as data, and a stage left by a crash is
  reclaimed only by something that can prove no writer owns it (SQLite reclaims a hot journal
  because the journal is named after the database and the database is locked; this plan has no
  lock, so it reclaims nothing, §D1). A-4 already stages and links through `fileaccess` (cap-std
  `Dir`, Spark `FileOutputCommitter`). D1 adds the two missing `fsync` calls, makes their failure
  fail the commit, and uses the existing `exclusive` mode (`link(2)` refusing an existing leaf) as
  the compare-and-set between two writers. No lock file, no new mechanism.
- Crash tests: a child interpreter killed with `SIGKILL` at a numbered step, state asserted by the
  parent after the child is gone (the shape of SQLite's `crashtest` and PostgreSQL's
  `test_fsync`-adjacent recovery tests). `SIGKILL` runs no `finally`, no `__del__` and no
  `weakref.finalize` (A-4's `_finalizer`), which is what a monkeypatched `raise` cannot model.
- Lock proofs: a second session that must block is given PostgreSQL `lock_timeout` and the test
  expects `lock_not_available` (SQLSTATE `55P03`); `pg_locks` is not read, because a row in
  `pg_locks` proves a lock exists, not that the session under test holds it on the row that
  matters.
- Secret-free frames: the A-4 boxed-entry construction (`decoy_engine/_opaque.py`,
  `secret_providers/_preflight.py:165-198`): value-bearing arguments move into an `OpaqueBox`,
  the plain names are deleted, the work runs in a contained call whose frames never reach the
  raised error.
- Idempotent concurrent insert: `INSERT ... ON CONFLICT DO NOTHING` plus re-read (already in
  `store.py:255-319`); the batch form is the same statement over many rows.
- Exclusive create as ownership: `O_CREAT | O_EXCL` (POSIX; what `tempfile.mkstemp` and
  A-4's `_CREATE` use) is the only portable way for a process to learn that it created a path;
  N4 replaces an `exists()` pre-check with it.

## 3. Designs

### D1. Durable, exclusive vault publication; callers gate on it (F1, N3, N4, N5)

Engine (`vault.py`, A-4 `fileaccess`), after the A-4 merge. `VaultWriter.write(path | OutputDir)`
always publishes through a stage beside the leaf in `exclusive` mode. The ambient path-string
route stops using the `vault_direct` in-place label: `_local.py:225-231` and the label itself are
deleted (pre-release hard delete). Under the ambient policy a path string stages in the target's
parent exactly as the strict route does (`_CREATE` with `O_EXCL` on a `_decoy_vault_stage_<hex>`
sibling, `_local.py:244-246`, prefix table `_output.py:32-41`). The publication is these numbered
steps, in both `RootedOutputTxn` (`_output.py:246`) and `LocalOutputTxn` (`_local.py:260`):

1. `open(parent/_decoy_vault_stage_<hex>, O_CREAT | O_EXCL | O_WRONLY)`.
2. write every byte of the vault (`serial.write_bytes`, `durable=True`).
3. `fsync(stage fd)`, then close it (`serial.write_bytes(..., durable=True)`: `flush()` then
   `os.fsync(fd)` before the handle closes; the vault is the only caller that passes it).
4. `link(stage, leaf)` (`follow_symlinks=False`): the compare-and-set. `EEXIST` means another
   vault owns the leaf: the stage is unlinked and `write` raises `VaultError(code="vault_exists")`.
5. `fsync(parent directory fd)`: the leaf's directory entry is durable. A failure raises
   (`OutputTxn.publish(durable=True)` no longer swallows it as `_output.py:71-77` and
   `_local.py:178-193` do); before raising, the leaf and the stage are unlinked on a best-effort
   basis and `write` raises `VaultError(code="vault_commit_failed")`.
6. `unlink(stage)`.
7. `fsync(parent directory fd)`: the stage's removal is durable. A failure raises exactly as in
   step 5 (the leaf is complete and durable at this point, but `write` has promised nothing until
   it returns, and the caller's rollback rule below is written for that).

`write` returns the entry count only after step 7. Every `OSError` from steps 2-7 becomes
`vault_commit_failed` with no path and no value in the message; a failure in step 1 (parent
missing, parent unwritable) keeps the A-4 shape (`FileNotFoundError` / `PermissionError` naming
the path under the ambient policy, as `test_char_vault_in_place.py:48-54` pins; locator-free
`FileAccessUnavailable` under a strict policy), because no stage exists and nothing was promised.
Existing callers of `publish()` and `write_bytes()` are unchanged (`durable` defaults to `False`).

Kill points (state on disk after `SIGKILL` at each step, as the next process sees it; `K7` is the
control, no kill):

| Kill after | Leaf | Stage | Invariant that holds |
|---|---|---|---|
| K1 step 1 | absent | present, empty | no leaf |
| K2 step 2 | absent | present, some or all bytes | no leaf |
| K3 step 3 | absent | present, complete | no leaf |
| K4 step 4 | present (directory entry may not have reached disk) | present, same inode as the leaf | a leaf that exists is complete (its bytes were fsynced in step 3) |
| K5 step 5 | present, durable | present, same inode | leaf complete; stage identical by construction |
| K6 step 6 | present, durable | absent (its unlink may not have reached disk) | leaf complete |
| K7 step 7 | present, durable | absent | leaf only |

The rev 1 claim "no leaf and no stage at every step" is withdrawn; it is unreachable with two
names and no transaction, and the invariant above is what the tests assert.

Recovery (what the next open does; the leaf is the only authoritative name):

| Residue found | `load_vault(leaf)` | `VaultWriter.write(leaf)` |
|---|---|---|
| no leaf, one or more stages (K1-K3, or a loser's residue) | `vault_unreadable` (existing code; the file is absent) | writes through its own fresh stage name and publishes; the old stages are not opened, not removed |
| leaf and a stage of the same inode (K4-K6) | reads the leaf; the stage is identical bytes and is never opened | `vault_exists` |
| leaf and a stage of a different inode (another writer's K1-K3 residue beside a published leaf) | reads the leaf; the stage is never opened | `vault_exists`; the stage is not touched |
| leaf only | reads | `vault_exists` |

Nothing reclaims a stage automatically: with no lock, a stage cannot be attributed to a dead
writer rather than a live one, and removing a live writer's stage is the class-3 race this design
exists to avoid. A stage holds Fernet ciphertext only (the plaintext is encrypted chunk by chunk
before any byte reaches `serial.write_bytes`, `vault.py:306-324`), so a residue is junk, not
exposure. `docs/security/token-vault.md` "Operational rules" gains the operator rule: a
`_decoy_vault_stage_*` file beside a vault with no running job may be deleted; it is never a vault.

Two writers: `exclusive` publication is the compare-and-set. The first `link(2)` wins; the second
gets `EEXIST`, its stage is removed, and `write` raises `vault_exists`. A vault leaf is never
replaced in place. Hardlinks (N3): a twin hardlink of an existing leaf is no longer rewritten
through (it keeps the old inode) and an existing vault in a read-only parent is preserved (step 1
fails, nothing else runs); these are the behaviors N3's two pins protected and both pins are
replaced in S4a, the commit that deletes `vault_direct`.

Tests (S4a, S4b; `tests/unit/test_vault_publish.py` unless noted):

- `test_sigkill_at_each_publish_step_leaves_a_loadable_leaf_or_none[K1..K7]`: the parent starts
  `sys.executable tests/unit/vault_publish_child.py <K> <dir>`; the child, before importing the
  writer, wraps `os.fsync`, `os.link` and `os.unlink` as seen by `fileaccess._output` and
  `fileaccess._local` so that the wrapper calls the real function and then
  `os.kill(os.getpid(), signal.SIGKILL)` once the numbered step has completed (`K2` kills before
  the first `fsync`, after the write). The parent waits for the child (`returncode == -9`), then
  asserts the row of the kill-point table: the leaf, when present, loads with the expected
  entries through `load_vault`; every other name in the directory matches
  `_decoy_vault_stage_*`; a second `write` from the parent succeeds exactly when the leaf is
  absent and raises `vault_exists` otherwise; after it, the leaf loads and the old residue is
  unchanged. Red first on A-4 `a19889a5`: `K4`-`K6` cannot be reached (the ambient route has no
  stage and `K2` leaves a truncated leaf in place, `_local.py:231`), which the test reports as
  "leaf present but does not load".
- `test_publish_fsyncs_file_then_directory`: `tests/fileaccess/strace_support.py` (`-f -yy`,
  decodes descriptors to paths) over a child that writes one vault; assert one `fsync` on the
  stage path before `linkat`, one `fsync` on the parent directory after `linkat` and before
  `unlinkat`, and one after `unlinkat`. The helper fails, never skips, when `strace` is absent.
  Red first on A-4: no `fsync` on the stage path (`serial.py:45-47`), none on the directory in the
  exclusive branch (`_output.py:254-269`).
- `test_a_failure_after_the_stage_exists_leaves_no_leaf_and_no_stage[write, file_fsync, link,
  dir_fsync]`: fault injection, not a crash test: `monkeypatch` the named call as seen by the
  `fileaccess` module to raise `OSError(EIO)`; expect `vault_commit_failed`, no leaf, no stage,
  no path and no `EIO` text in the message. Red first on A-4: the directory fsync error is
  swallowed (`_output.py:71-77`), so `dir_fsync` returns success.
- `test_two_real_writers_one_publishes_one_fails_closed`: two threads, real `tmp_path`,
  `threading.Barrier` after both staged; exactly one leaf, loser raises `vault_exists`, no
  `_decoy_vault_stage_*` residue, `load_vault` reads the winner. Red first on main `vault.py:305`:
  both succeed, last writer wins.
- `test_a_hardlinked_twin_is_not_rewritten` (N3, replaces pin 1): a twin keeps the old bytes and
  the leaf is a new inode.
- `test_an_existing_vault_in_a_read_only_parent_is_preserved_and_no_stage_remains` (N3, replaces
  pin 2): a writable vault in a read-only parent; `write` fails with `PermissionError` naming the
  path (ambient), the vault's bytes are unchanged, the directory listing is unchanged. Skipped
  when `os.geteuid() == 0`, the same condition the deleted pin carried; the engine CI legs run on
  `ubuntu-latest` without a container (`.github/workflows/ci.yml`), where the test runs.

Callers (publication gated on the vault commit). For every caller the table says whether the vault
is NEW or EXISTING at its commit, the order, and what an output failure after the commit retains
or rolls back; each cell is a test.

| Caller (read) | Vault | New or existing at the commit | Order after this plan | Output failure after the commit | Test |
|---|---|---|---|---|---|
| platform full-frame, `v2_full_frame.py:166-172, 205-206` | file vault `job_<id>.dcyvault` | NEW: per job id, exclusive | token commit, file vault, `write_v2_outputs` | ROLLED BACK: `_persist_job_vault` moves before `write_v2_outputs`; the publication is wrapped so that any exception unlinks `job_vault_path(job.id)` (idempotent, `missing_ok`, logged by exception type only) and re-raises; the `target.vault` node is added after publication succeeds | `tests/test_vault_durability.py::test_output_failure_after_a_file_vault_commit_removes_the_vault` |
| platform relationship routes (N5), `v2_sequential.py:450`, `route_dispatch_exec.py:246-247` | file vault | NEW | file vault persisted inside `_run_relationship_job_to_targets` immediately before `_materialize_staged_sequential_outputs`; `route_dispatch_exec.py:246-247` deleted | ROLLED BACK, same wrapper; tables already published by an earlier loop iteration stay (each publish is atomic per table; the manifest records the count), the vault does not | `::test_relationship_routes_persist_the_vault_before_the_first_table_publishes[sequential_fk, out_of_core_fk]`, `::test_relationship_route_output_failure_removes_the_vault` |
| platform token vault, `_commit_token_vault_before_publish` (`v2_full_frame.py:47, 166`) | Postgres rows | EXISTING: the vault row predates the job | commit, then publish (already so) | RETAINED: rows are never deleted after a commit. A concurrent job may already have read them (`lookup_existing`) and emitted their tokens; deleting them would orphan that job's output. A rerun emits the same tokens (the derivation is a pure function of scope, key, namespace, plaintext, and the vault is authoritative) and inserts no row | `::test_output_failure_after_the_token_commit_keeps_rows_and_a_rerun_adds_none` |
| CLI `run.py` plain route, `:840, :854` | file vault | NEW: Q4, an existing leaf is refused with `vault_exists` (EXIT_USAGE, the message names the `--vault` argument the operator typed) before any output is written | vault, then `_write_mask_outputs` | ROLLED BACK: the leaf this run created is unlinked; exit EXIT_RUNTIME | CLI `tests/e2e/test_run_vault_gating.py::test_vault_commit_failure_writes_no_outputs`, `::test_output_failure_after_the_vault_commit_removes_the_vault`, `::test_an_existing_vault_is_refused_before_any_output` |
| CLI `run.py` chunked route, `_run_chunked_mask:1398`, `_write_chunked_output:1455` | file vault | NEW | each table streams to a same-directory stage `_decoy_stage_<hex><suffix>` beside its target; after the last table the vault is written; then each stage is `os.replace`d onto its target | vault failure: every stage unlinked, no leaf, exit EXIT_RUNTIME; a failure inside the replace loop: the leaf and the remaining stages are unlinked, the tables already renamed stay (each rename is atomic) and the error names how many of N tables published | `::test_chunked_vault_commit_failure_leaves_no_output_and_no_stage`; existing `tests/e2e/test_vault_cli.py::test_chunked_run_writes_equivalent_vault` stays green |
| CLI `tokenize.py`, `:333-348, :377-383` | SQLite token vault | NEW when `LocalTokenVault(create=True)` won the exclusive create (N4: `created` is the only ownership signal); EXISTING otherwise | `commit()`, then `_write_mask_outputs` | NEW: the file is unlinked on output failure although rows were committed (the cleanup condition becomes `created and not output_written`); EXISTING: committed rows are retained, same rule as the platform | CLI `tests/e2e/test_tokenize_cli.py::test_output_failure_after_commit_removes_a_vault_this_run_created`, `::test_output_failure_after_commit_keeps_a_preexisting_vault_and_its_new_rows`; existing `::test_failed_run_leaves_no_empty_vault`, `::test_failed_run_keeps_a_preexisting_vault` stay |

Platform: fatal, decided by the failure order. `v2_vault.py:78-80` documents that a file vault
write failure does not fail the job and `:89-100` records it as a `status="error"` node. The test
`tests/test_vault_durability.py::test_file_vault_commit_failure_publishes_nothing` orders the vault
before publication and asserts that a `vault_commit_failed` from `VaultWriter.write` ends the job
before `write_v2_outputs` is called. Once that order holds, a vault failure that did not fail the job
would publish an output whose vaulted columns can never be recovered, which is F1 itself; there is
no state in which output publishes without a durable vault commit, so there is nothing for a
non-fatal write to be non-fatal about. The node path at `:89-100` and the docstring sentence are
deleted (hard delete); `_persist_job_vault` raises `VaultCommitError` (new, `api/jobs/v2_vault.py`,
carrying the engine's registered `vault_commit_failed` or `vault_exists`), which the runner's
existing terminal handler (`runner.py:124-156`) maps to a failed job whose manifest carries the
reason and no path.

N4 (CLI tokenize ownership): `LocalTokenVault(create=True)` opens the path with
`O_CREAT | O_EXCL | O_RDWR, 0o600` first and, on `FileExistsError`, with `O_RDWR`; the result is
`self.created: bool`. `tokenize.py:326` (`vault_preexisted = vault.exists()`) is deleted and the
`finally` reads `token_vault.created`. The 0600 and parent 0700 rules (`_token_vault.py:95-115`)
are unchanged.

### D2. Vault-scoped token derivation (F2, N2)

Protocol (`execution/_adapter.py`): `TokenVaultWriter` gains one required attribute,
`derivation_scope: bytes`, an opaque, non-secret, vault-unique byte string of 16 to 64 bytes that the
vault owner mints once at vault creation and stores with the vault. The engine never logs it, never
writes it into output, and never derives anything from it alone. `BatchTokenVaultWriter` inherits it.
A writer without the attribute, or with one outside the length bound or not `bytes`, fails closed with
`StrategyError(code="tokenize_vault_scope_required")` before any plaintext is normalized.

Derivation (`execution/_strategies/_tokenize.py`, new helper module
`execution/_strategies/_tokenize_key.py`):

```
TOKENIZE_KEY_LABEL = b"tokenize-key/v1"
K_tok   = derive(mask_key, namespace, TOKENIZE_KEY_LABEL + len(scope).to_bytes(4, "big") + scope)
token   = "tok_" + DeriveContext.for_column(K_tok, namespace).derive_source(namespace, NFC(pt).encode())[:16].hex()
```

`K_tok` is 32 bytes, a valid `derive` IKM (`_derive.py:144`). The label is the purpose (RFC 5869
§3.2 `info`, SP 800-108 `Label`); the length-prefixed scope is the context. The outer envelope
(`SEED_PROTOCOL_VERSION`, HKDF salt, length prefixes) is untouched, so `hash`, `fpe`, generation
and the file vault key produce the same bytes as today. The subkey is minted once per column and
the per-value HMAC runs through the existing `derive_sources` fast path.

Scope sources:

- Platform: `tokenization_vaults.derivation_scope BYTEA NULL` (migration
  `vault_v5_derivation_scope`), 32 bytes from `secrets.token_bytes(32)` set by `create_vault`
  (`store.py:93-113`) on every vault created after the cut; `TokenVaultWriterAdapter.derivation_scope`
  returns it. The column is nullable only because a `NOT NULL` column cannot be added to a
  populated table without a data migration (a default or a delete), which the pre-release rule
  forbids; a `NULL` is the hard cut's refusal signal, not a compatibility state.
- CLI: `meta.derivation_scope` (hex) written at `LocalTokenVault(create=True)`, 32 bytes from
  `secrets.token_bytes`; `_SCHEMA_VERSION` 1 to 2 (`_token_vault.py:63`).
- CLI N2: closed on CLI main (`57e8b81`); `put` returns the stored token on both paths. `put_many`
  and `lookup_existing_many` are added over one `executemany` and one `IN (...)` query; both
  namespace-scoped; `put_many` returns the stored token per key the way `put` does.

Hard cut (owner decision D2/Q1): a vault minted before the cut is refused for tokenization with the
one registered code `token_vault_derivation_unsupported`, raised before the engine runs:

- Platform: `_resolve_vault` (`v2_token_vault.py:177-195`, where `revoked_at` is already refused)
  raises `TokenVaultConfigError(code="token_vault_derivation_unsupported")` when
  `vault.derivation_scope is None`; the job fails at vault resolution, no `run_pipeline` call.
- CLI: `_ensure_schema` (`_token_vault.py:203-211`) raises
  `TokenVaultError(code="token_vault_derivation_unsupported")` for a file whose
  `meta.schema_version` is `1` (the version with no scope), ahead of the generic
  `vault_schema_mismatch` branch, which keeps covering every other mismatch.
- Engine: `tokenize_vault_scope_required` stays as the defense in depth for a writer that reaches
  the engine without a valid scope; it is not the old-vault code.

No marker column, no second derivation path, no rows are migrated. Detokenize is not changed by this
plan: it reads rows and makes no derivation; the plan promises nothing about rows minted before the
cut, and the operator deletes such vaults. The `TOKEN_DERIVATION` export and the `token_derivation`
columns of rev 1 do not exist in this plan.

### D3. Tokenize and hash separate PRF domains (F3)

D2 already separates them: `hash` is `HMAC(HKDF(mask_key, ns), ...)` and `tokenize` is
`HMAC(HKDF(K_tok, ns), ...)` with `K_tok = derive(mask_key, ns, label || scope)`. The two HMAC keys
are outputs of HKDF under different IKMs, so equality of a hash prefix and a token payload for the
same plaintext has probability `2^-128` per value. The negative-equality regression test holds a
generated corpus (Hypothesis `st.text()`, NFC-equivalent pairs included, both seams) and a pinned
vector so the mutation "drop the label" and the mutation "use `mask_key` as the IKM" both go red.

The label is versioned in the string (`tokenize-key/v1`). Pre-release, a label change is a new hard
cut (the same refusal rule as D2); nothing records the label beside the vault.

### D4. Value-free frames on the vault failure path (F4)

Reuse `decoy_engine._opaque.OpaqueBox` and add one helper to the same module,
`closed_call(work, box) -> tuple[result | None, bool]`, the `_contained` shape of
`secret_providers/_preflight.py:165-175` without the failure rebuild (the tokenize error is already
a fixed coded `StrategyError`). `_preflight` is not touched in this plan.

`TokenizeStrategyHandler.run(self, df, column, plan, ctx)`:

1. First statement: `box = OpaqueBox(df, ctx)`; `del df, ctx`. The preflight refusals
   (`tokenize_requires_vault`, `_namespace`, `_string_dtype`, `_vault_scope_required`) are raised
   from this frame, which now holds `box`, `column`, `plan`, `self` only.
2. Normalization and the distinct list are computed inside `closed_call(_prepare, box)`; the
   result is an `OpaqueBox(normalized, distinct)` so no frame that returns holds a list of
   plaintexts.
3. Resolution runs as `closed_call(_resolve_tokens, box2)`; on failure `run` raises the coded
   error from its own frame. The inner frames (`_resolve_tokens`, `_vault_call`, the writer's)
   are on the discarded exception's traceback, which `closed_call` drops; nothing on the raised
   error's chain holds `normalized`, `distinct`, `minted`, `mask_key`, `plaintext` or `args`.
4. Publication (`df[column] = ...`) opens the box inside the call expression that assigns.

Canary (gate finding 8): `tests/fileaccess/locals_support.py:16-17` renders bytes through
`latin-1` and `engine_locals` (`:27-50`) yields `repr(value)` plus the nested strings, so a
hex-encoded key never appears in anything the helper inspects and rev 1's `key_hex` canary could
not fail. The key canary is the key's own ASCII bytes: `KEY = b"canary-key-0123456789abcdefghijk"`
(32 bytes, every byte printable ASCII, so both `repr(KEY)` and the latin-1 text contain
`canary-key-`); the plaintext canaries are `pt-canary-<n>`; the assertion is
`assert_no_engine_local_holds(exc, "canary-key-", "pt-canary-")`. Control: on B-6 before the
fix the same helper names `_resolve_tokens() local 'mask_key'` and `run() local 'normalized'`
(red first). Mutation: box everything except `mask_key` (keep it a plain local of
`_resolve_tokens`); the test must go red on the key canary alone, which proves the key canary is
the one the helper detects.

Documented guarantee: `docs/security/error-reporting-and-data-exposure.md` line 9 ("Other engine
code paths make no frame-level promise yet") gains a second bullet naming the tokenize vault failure
path (both seams, every exit) and the frames it covers. Hostile in-process inspection stays V2 and
stays out of the promise.

### D5. Validate inside the sanitized boundary (F5)

`_vault_call` materializes and type-checks inside its `try`:

- batch results: `type(result) is dict` after `dict(result.items())`; every key and value
  must satisfy `type(x) is str` (exact type: no `__eq__`, `__hash__`, `__bool__`, `__str__`
  hooks run after the boundary) and be non-empty;
- per-row results: `None` or exact `str`, non-empty.

`_checked_tokens` then runs over plain `dict[str, str]` and plain `set[str]` only (subset or
exact-set checks) and cannot raise anything but the coded error. The existing fail-closed rules
(`_adapter.py:131-137`, CHANGELOG B-6 entry) are unchanged in meaning; the guard moves.

### D6. Tests at the real boundary (F6, N1)

Engine (S5): `tests/property/test_tokenize_parity.py` with Hypothesis over lists of strings (with
duplicates, NFC-composed and decomposed forms, nulls, empty strings), a random preloaded subset
(hits) and both writer seams built over one shared in-memory store, asserting equal emitted tokens
per row, one lookup and one put per distinct miss on the batch seam, and vault-authoritative reuse.
Mutations that must go red: skip NFC on one seam; put a hit into `put_many`; reorder the batch.

Platform (S6b), every test with two `Session`s over the `pg_url` fixture
(`tests/test_vault_concurrency_pg.py:64`) and the `pg_semantic` marker so the `backend-postgres` leg
fails if they skip:

- `tests/test_vault_batch_pg.py::test_two_sessions_interleave_lookup_many_and_put_many`: both
  sessions miss on the same distinct set, A commits `put_many` first, B's `put_many` returns A's
  tokens for the overlap and B's own for the rest; the emitted frame under the real engine
  matches the stored rows.
- `::test_partial_put_many_after_a_concurrent_commit`: between B's `lookup_existing_many` and
  `put_many`, A commits a subset; B's result is authoritative for every key (conflict re-read).
- `::test_batch_commit_failure_publishes_nothing`: `db.commit` raises inside
  `_commit_token_vault_before_publish` with the batch writer; `write_v2_outputs` is never called;
  the second session sees no rows.
- N1, `tests/test_vault_concurrency_pg.py::test_write_lock_is_reacquired_after[commit, rollback,
  savepoint_rollback]`: session A `storeA.put(v, "ns", "a", t1)` (lock taken at `store.py:160-165`),
  then the parametrized boundary: `A.commit()`; `A.rollback()`; or the put ran inside
  `A.begin_nested()` and that savepoint is rolled back (PostgreSQL releases the row locks of a
  rolled-back subtransaction, so the cache is stale in all three). Then `storeA.put(v, "ns", "b",
  t2)`, which must take `FOR UPDATE` again. Proof that A holds the lock: session B runs
  `SET LOCAL lock_timeout = '300ms'` and `RotationManager(VaultStore(B)).complete(v)`, which locks
  first (`rotation.py:214`) and must raise `sqlalchemy.exc.OperationalError` with SQLSTATE `55P03`
  (`lock_not_available`). After `A.commit()`, B's `complete(v)` returns (no rotation pending, a
  no-op after the lock). Control, in the same test before the boundary: with A's first put
  uncommitted, B's `complete(v)` raises the same way, so the detector is live and the assertion
  is not vacuous. Red first on main: after the boundary A's second put skips the lock and B's
  `complete(v)` returns at once. Fix: `event.listen(session, "after_commit" | "after_rollback" |
  "after_soft_rollback", clear)` registered in `VaultStore.__init__` (`store.py:88`), each clearing
  `_write_locked` (and `_rotation_due` only on rollback). Mutation: remove one listener; the
  matching parameter goes red. `pg_locks` is not consulted.
- `tests/test_vault_durability.py::test_file_vault_commit_failure_publishes_nothing` (F1 platform):
  `VaultWriter.write` raising `vault_commit_failed` fails the job before `write_v2_outputs`.

### D7. PyCA HKDF (F7)

`determinism/_hkdf.py` keeps its three names with the same signatures and semantics:

- `hkdf_extract(salt, ikm)`: the empty-salt `ValueError` first (`:54-59`, QA-10 F12), then PyCA
  `HMAC(salt, SHA256())` over `ikm`.
- `hkdf_expand(prk, info, length)`: PyCA `HKDFExpand(SHA256(), length, info).derive(prk)`; the
  `255 * HashLen` bound is PyCA's own `ValueError`, re-raised with the current message.
- `hkdf_sha256(ikm, salt, info, length)`: the same empty-salt `ValueError` as its first statement,
  then PyCA `HKDF(SHA256(), length, salt, info).derive(ikm)`. The guard is repeated here, not
  inherited, because the one-shot no longer passes through `hkdf_extract`; PyCA accepts `b""`
  and derives under the all-zero salt (§1 probe), the exact degrade the refusal exists to stop.

The module docstring's anti-PyCA rationale (lines 8-13) is replaced by the citation line
(`Pattern: RFC 5869 HKDF-SHA256 (PyCA cryptography)`), and the module is added to
`docs/methodology-registry.yaml`. Compatibility: byte-for-byte on every existing vector (RFC
A.1-A.3) plus engine vectors captured on main before the swap:
`tests/fixtures/hkdf/engine-vectors.json` with the engine salt and a 32-byte and an 8-byte IKM,
the 32-zero-byte salt used by `context._hkdf_sha256` and `provenance.py`, the maximum length, and
one entry per entry point with `salt: ""` whose expected result is the refusal (`"raises":
"ValueError"`, message prefix `salt must be non-empty`), so the vector runner asserts the refusal
on both `hkdf_extract` and `hkdf_sha256`. Tests: `test_engine_vectors_are_unchanged_by_the_pyca_swap`
(the file), `test_oneshot_rejects_empty_salt` (explicit, red first on a naive swap seeded in the
worktree), the existing `test_empty_salt_rejected_by_extract` (`test_hkdf.py:117`) stays. Mutations
that must go red: drop the guard from `hkdf_sha256`; map an empty salt to 32 zero bytes before the
call; drop the guard from `hkdf_extract`. Vault-scoped derivation (D2) changes no existing output:
it adds a derivation, it does not alter `derive`, `SEED_PROTOCOL_VERSION` (stays 7), the HKDF
salt or the info format. Only tokenize tokens change, and the D2 hard cut covers the vaults that
hold old ones.

### D8. Handoff note (F8)

`docs/handoffs/inputs/b6-engine-api-note.md` is rewritten in S5: status line names platform #110
as merged with the per-row seam and S6b as the batch half; §4's "the engine will never relay it" is
restated as the D4 frame guarantee plus the D5 boundary; §5's "or the job seed pre-GA" is deleted
(keyed strategies need a resolved secret in every phase since F13); §2 step 4 gains the D2
derivation; §3 gains `derivation_scope`. A sentry test (`tests/sentry/test_b6_note_currency.py`)
asserts that every protocol signature quoted in the note equals `inspect.signature` of the shipped
protocol method and that the strings "job seed", "not started" and "never relay" do not occur.

## 4. Slices

Each slice: one repo, at most 10 production files, at most 800 changed lines (tests included in
the line count, excluded from the file count), red tests first, then the change, then the mutation
run recorded in the PR. LOC budgets (600 aim, 700 max) measured with `wc -l` from the lock:
`vault.py` 451 on main and 525 on A-4; `_tokenize.py` 276 on B-6; `_derive.py` 419 on A-4;
`_adapter.py` 431 on B-6; `_local.py` 378 and `_output.py` 389 on A-4. S4b keeps `vault.py` under
600 (525 plus about 60); S3 splits the key helper out of `_tokenize.py` rather than growing it
past 400.

| Slice | Repo, base | Resolves | Production files | Depends on |
|---|---|---|---|---|
| S1 | engine, main | F7 | `determinism/_hkdf.py`, `docs/methodology-registry.yaml`, `docs/security/key-derivation.md` | none |
| S2 | engine, main after B-6 merges | F2, F3, hard-cut codes | `execution/_adapter.py`, `execution/_strategies/_tokenize.py`, `execution/_strategies/_tokenize_key.py` (new), `reasons/runtime/_part3.py` (`tokenize_vault_scope_required`, `token_vault_derivation_unsupported`), `reasons/_registry.py` (minor bump), `docs/reasons.md` (line 21), `docs/compatibility-contract.md`, `docs/security/key-derivation.md`, `CHANGELOG.md` | S1 (vectors), B-6 branch merged |
| S3 | engine | F4, F5 | `_opaque.py`, `execution/_strategies/_tokenize.py`, `docs/security/error-reporting-and-data-exposure.md` | S2, A-4 merged (for `_opaque.py` and `locals_support`) |
| S4a | engine | F1 (fileaccess half), N3 | `fileaccess/_output.py`, `fileaccess/_local.py` (delete `vault_direct`, `publish(durable=)`, raising directory fsync), `fileaccess/adapters/serial.py` (`write_bytes(durable=)`), `fileaccess/_types.py` (the `publish(durable=)` protocol) | A-4 merged |
| S4b | engine | F1 (vault half) | `vault.py`, `reasons/runtime/_part3.py` (`vault_commit_failed`, `vault_exists`), `reasons/_registry.py` (minor bump), `docs/reasons.md`, `docs/security/token-vault.md`, `docs/compatibility-contract.md`, `CHANGELOG.md` | S4a |
| S5 | engine | F6 (engine), F8 | `docs/handoffs/inputs/b6-engine-api-note.md`, `CHANGELOG.md` (plus the property and sentry tests) | S2, S3 |
| S6a-i | platform, main | F2 (platform), hard cut, pin | `api/migrations/versions/vault_v5_derivation_scope.py` (new), `api/models.py`, `api/vaults/store.py` (scope at create), `api/jobs/v2_token_vault.py` (`derivation_scope`, `_resolve_vault` refusal), `api/engine_a8_guard.py`, `pyproject.toml` (repin to R-b), `.github/workflows/backend.yml` (`ENGINE_PIN_SHA`) | R-b released |
| S6a-ii | platform | F1 (platform, both route families), N5 | `api/jobs/v2_vault.py` (raise, rollback helper), `api/jobs/v2_full_frame.py`, `api/jobs/v2_sequential.py`, `api/jobs/route_dispatch_exec.py` | S6a-i |
| S6b | platform | F6 (platform), N1 | `api/vaults/store.py` (`lookup_existing_many`, `put_many`, lock-cache clearing), `api/jobs/v2_token_vault.py` (batch adapter) | S6a-i |
| S7a | CLI, main | F2 (CLI), N4, F1 (tokenize), N2 record | `src/decoy/cli/_token_vault.py`, `src/decoy/cli/tokenize.py`, `pyproject.toml` (repin to R-b) | R-b released |
| S7b | CLI | F1 (run, plain and chunked), Q4 | `src/decoy/cli/run.py` | S7a |

Changed-line budget per slice (estimate from the cited line ranges; tests included; limit 800;
the builder records the actual `git diff --stat` in the PR and splits again if a slice measures
over):

| Slice | Est. production lines | Est. test lines | Est. total | Note |
|---|---|---|---|---|
| S1 | 110 | 140 | 250 | vectors fixture counted as test lines |
| S2 | 180 | 260 | 440 | `FakeVault` fixtures in `test_tokenize.py` gain `derivation_scope` |
| S3 | 150 | 230 | 380 | |
| S4a | 130 | 180 | 310 | strace order test, fault-injection test, two N3 replacements, two pins deleted |
| S4b | 120 | 420 | 540 | kill-point child script and seven parameters, two writers, hardlink |
| S5 | 90 | 220 | 310 | |
| S6a-i | 110 | 220 | 330 | v5 migration test sibling of `test_vault_v1_migration.py` |
| S6a-ii | 90 | 300 | 390 | durability tests for both route families and the token rows |
| S6b | 140 | 320 | 460 | |
| S7a | 150 | 280 | 430 | N4 two-process test |
| S7b | 120 | 220 | 340 | chunked staging |

Why S4, S6a and S7 are split: rev 1's S4 measured at about 720 including the kill-point tests and
S6a at about 560 with N5 added; a split keeps each under the limit with margin and gives N3 its own
commit (the pin and the behavior move together in S4a) and N5 its own tests in S6a-ii.

`rule_codes.py` (CLI) is not in S7a's file list: its engine half is derived from the pinned
registry at first use (`rule_codes.py:9-15`), so the four registered codes appear after the repin
without an edit; `tests/unit/test_ci_rule_codes.py` re-derives the set and is the check.

The CLI B-1 writer does not share the derivation (the engine mints; `_token_vault.py:11-15`), but
S7a is still required: the protocol gains `derivation_scope` (F2), the schema goes to 2, `created`
replaces the `exists()` pre-check (N4), and the output/vault order is reversed (F1).

Call sites that change:

| Repo | File:line (read) | Change |
|---|---|---|
| engine | `vault.py:332-345` (A-4 numbering) | durable exclusive publish, `vault_commit_failed`, `vault_exists`, `vault_direct` route deleted |
| engine | `fileaccess/_output.py:71-77, 246-273`; `_local.py:178-193, 225-231, 260-300`; `adapters/serial.py:45-47`; `_types.py:204` | `durable=True` fsyncs, raising directory fsync, step order 1-7 |
| engine | `_tokenize.py:85-94, 154, 253-258` | scope check, subkey mint, boxed frames |
| engine | `_adapter.py:98-157` | `derivation_scope` on `TokenVaultWriter` |
| platform | `v2_token_vault.py:146-166, 177-195` | scope attribute, batch methods, old-vault refusal |
| platform | `v2_full_frame.py:165-172, 205-206` | vault write before publish, failure fatal, rollback on output failure |
| platform | `v2_sequential.py:449-456`, `route_dispatch_exec.py:246-247` | vault persisted before the per-table publish; the late persist deleted |
| platform | `v2_vault.py:65-100` | raise instead of an error node; rollback helper |
| platform | `store.py:88, 93-113, 255-319` | scope at create, lock-cache clearing, batch statements |
| platform | `engine_a8_guard.py:60-110` | assert `derivation_scope` and the batch signatures |
| CLI | `_token_vault.py:63, 95-130, 203-211, 215-251` | schema v2, scope, `O_EXCL` create and `created`, old-vault refusal, batch methods |
| CLI | `tokenize.py:326, 333-348, 377-383` | ownership from `created`, vault commit before outputs, rollback of a new vault on output failure |
| CLI | `run.py:840, 854, 1398-1470` | vault before outputs on the plain route; staged outputs on the chunked route; `vault_exists` to EXIT_USAGE |

## 5. Merge and release order, versions, where each is recorded

Order: S1, S2, S3 land on engine main in that order (each its own PR, Codex gate each), after the
B-6 batch branch merges (the batch writer preserves single-row behavior; S2 and S3 change the
behavior it preserves). A-4 merges, then S4a, S4b, then S5. Release R-b is cut from the engine main
that carries A-4 and S1-S5 as `v0.18.0`; `docs/records/release-rb-facts.md` is written the way
`release-ra-facts.md` is (every value with its command). Then S6a-i, S6a-ii, S6b on the platform
(repin `decoy-engine==0.18.0`, `ENGINE_PIN_SHA` in `backend.yml`), then S7a, S7b on the CLI (repin
`decoy-engine==0.18.0`). Nothing in S6/S7 can land against 0.17.0: the protocol attribute and the
reason codes do not exist there.

Reason registry (gate finding 3). A slice that adds identities sets `REGISTRY_VERSION`
(`reasons/_registry.py:24`) to the value on `origin/main` at that slice's merge-base plus one minor,
and every identity it adds carries `since=` that same literal (the form A-4 uses at
`runtime/_secret_provider.py:85`). The literal appears in `_registry.py`, in the `since=` keywords,
and in `docs/reasons.md` line 21, which `test_reasons_doc_states_the_live_registry_version` ties to
the live value; no test hardcodes it. For the record, not as an instruction: main is 1.1.0 today,
A-4 sets 1.2.0, so S2 (building after A-4) would write 1.3.0 and S4b 1.4.0; if anything else bumps
in between, the rule decides, not these numbers. `tests/fixtures/f29/reason-manifest-v1.json` is the
immutable append-only pin (`test_v1_manifest_is_append_only`) and is NOT regenerated.
`tests/fixtures/f29/reason-registry-release.json` is recaptured once, at the R-b cut, by
`scripts/release/reason_release_pin.py --write` (which `cut-release.sh` requires), never inside a
slice. Tests that prove the bump: `tests/unit/test_reason_release_pin.py::test_live_registry_honours_the_released_pin`
is red on the slice branch the moment an identity is added without the bump (`pin_violations`
reports "without a registry version bump", `test_reason_release_pin.py:85-87` shows the shape) and
green after it; `::test_identities_added_after_the_v1_pin_name_their_own_since` rejects a `since`
at or below the v1 floor; `::test_reasons_doc_states_the_live_registry_version` ties the doc. S2
adds `::test_no_identity_claims_a_since_newer_than_the_live_registry_version` (every `since` is at
most `REGISTRY_VERSION`; red first by seeding one identity one minor above the live value).
Mutation for each adding slice: revert `REGISTRY_VERSION` to main's value; the pin test goes red.
The task's reference to the test named in the CDF plan (T17) could not be resolved: no plan in
decoy-engine or decoy-platform `docs/` names a registry-bump test under that label (searched
`CDF`, `T17`); the tests above are the engine's own.

The code `token_vault_derivation_unsupported` is declared under
`_catalog.runtime("decoy_engine.execution._adapter")`, the module that defines the scope contract,
and raised by the platform and the CLI; no engine test requires a registered code to have an engine
producer site (`test_every_producer_resolves`, `test_reason_registry.py:177-199`, checks that
inventory sites resolve, not the converse).

Version and format bumps, exact:

| Surface | Today | After | Recorded in |
|---|---|---|---|
| Engine package | 0.17.0 | 0.18.0 (R-b) | `pyproject.toml` version, `CHANGELOG.md` `## [0.18.0]`, tag `v0.18.0` |
| `TokenVaultWriter` protocol | `lookup_existing`, `put` | plus `derivation_scope: bytes` (required) | `docs/compatibility-contract.md` §3.4 (the tokenize paragraph at lines 522-540) and CHANGELOG; a required attribute is not additive under §4.1, legal pre-GA only, stated as such |
| Token derivation | unlabeled `derive(mask_key, ns, pt)` | `tokenize-key/v1` subkey with scope | engine `_tokenize_key.py`; nothing stored beside the vault (D2 hard cut) |
| `SEED_PROTOCOL_VERSION` | 7 | 7 (unchanged; `derive` is untouched) | `_derive.py:136`, `test_derive_vectors.py::test_seed_protocol_version_is_seven` stays |
| File vault format | `decoy-vault/v2`, `vault-key/v1` | unchanged (bytes identical; only the publication path changes) | `vault.py:82-88`, `docs/security/token-vault.md` "File format" |
| Reason registry | main's value at build time (1.1.0 on main today; 1.2.0 once A-4 merges) | plus one minor per adding slice (S2: `tokenize_vault_scope_required`, `token_vault_derivation_unsupported`; S4b: `vault_commit_failed`, `vault_exists`), each `since` equal to its slice's literal | `reasons/runtime/_part3.py`, `reasons/_registry.py`, `docs/reasons.md`; `reason-registry-release.json` at the R-b cut only; `reason-manifest-v1.json` untouched |
| Platform DB | vault_v4 | `vault_v5_derivation_scope`: `derivation_scope BYTEA NULL` (NULL = refused, D2) | `api/migrations/versions/`, `test_vault_v1_migration.py` sibling `test_vault_v5_migration.py` |
| CLI vault schema | `meta.schema_version = 1` | 2 (a 1 is refused with `token_vault_derivation_unsupported`) | `_token_vault.py::_SCHEMA_VERSION`, CLI CHANGELOG |
| Platform engine pin | 0.16.0 | 0.18.0 | `pyproject.toml:36`, `backend.yml` `ENGINE_PIN_SHA` |
| CLI engine pin | 0.17.0 (since #76) | 0.18.0 | `pyproject.toml:103` |

## 6. Acceptance tests (red first)

Every test is written and shown red on the base named in "fails before" with the seeded violation,
then the change is made, then the named mutation is applied and the test shown red again, then
reverted. Mutations are recorded in the PR's evidence record under `docs/records/`. Baselines run
from the lock in the same environment as the branch, and the record says so.

| Test | Seeded violation (red first) | Fails before (file:line, base) | Mutation that must turn it red |
|---|---|---|---|
| `tests/unit/test_vault_publish.py::test_sigkill_at_each_publish_step_leaves_a_loadable_leaf_or_none[K1..K7]` | child interpreter, real `tmp_path`, `SIGKILL` after the numbered step; parent asserts the kill-point table and the recovery table | A-4 `_local.py:231` (ambient truncates the leaf in place: `K2` leaves a leaf that does not load; `K4`-`K6` unreachable); main `vault.py:305` | restore the in-place open; or unlink the stage before the link; or swallow the `link` error |
| `::test_publish_fsyncs_file_then_directory` | `strace -f -yy` over a child; `fsync` on the stage path before `linkat`, on the parent after `linkat` and after `unlinkat` | A-4 `serial.py:45-47` (no file fsync), `_output.py:254-269` (no directory fsync in the exclusive branch) | drop any one fsync; swap the file fsync after the link |
| `::test_a_failure_after_the_stage_exists_leaves_no_leaf_and_no_stage[write, file_fsync, link, dir_fsync]` | fault injection (not a crash test): the named call raises `OSError(EIO)`; expect `vault_commit_failed`, no leaf, no stage, no path or errno text | A-4 `_output.py:71-77`, `_local.py:178-193` (directory fsync error swallowed) | swallow the error; keep the leaf on a directory fsync failure |
| `::test_two_real_writers_one_publishes_one_fails_closed` | two threads, real `tmp_path`, `threading.Barrier` after both staged; exactly one leaf, loser raises `vault_exists`, no `_decoy_vault_stage_*` residue, `load_vault` reads the winner | main `vault.py:305`: both succeed, last writer wins | switch the label to `replace` |
| `::test_a_hardlinked_twin_is_not_rewritten` (replaces N3 pin 1) | a hardlinked twin keeps the old bytes; the leaf is a new inode | A-4 `test_char_vault_in_place.py:23-30` asserts the opposite | re-add `vault_direct` |
| `::test_an_existing_vault_in_a_read_only_parent_is_preserved_and_no_stage_remains` (replaces N3 pin 2) | writable vault in a read-only parent; `write` raises `PermissionError` naming the path (ambient); vault bytes and directory listing unchanged; skipped only as root | A-4 `test_char_vault_in_place.py:33-45` asserts the rewrite | re-add `vault_direct` (the in-place `O_TRUNC` open succeeds on a writable file in a read-only directory) |
| platform `tests/test_vault_durability.py::test_file_vault_commit_failure_publishes_nothing` | `VaultWriter.write` raises `vault_commit_failed`; job failed; `write_v2_outputs` never called; no `target.vault` node | `v2_full_frame.py:206` runs after `:168`; `v2_vault.py:89-100` returns an error node | move `_persist_job_vault` back after `write_v2_outputs`; restore the error node |
| `::test_output_failure_after_a_file_vault_commit_removes_the_vault` | `write_v2_outputs` raises after the vault leaf exists; leaf absent after, job failed, no stage | no rollback exists on any base | skip the unlink |
| `::test_relationship_routes_persist_the_vault_before_the_first_table_publishes[sequential_fk, out_of_core_fk]` (N5) | `_materialize_staged_sequential_outputs` spy records whether the leaf existed at its first call | `route_dispatch_exec.py:247` runs after `v2_sequential.py:450` | move the persist back after the loop |
| `::test_relationship_route_output_failure_removes_the_vault` (N5) | the per-table publish raises on table 2 of 2; leaf absent, table 1 published, job failed | no rollback exists | skip the unlink |
| `::test_output_failure_after_the_token_commit_keeps_rows_and_a_rerun_adds_none` | `write_v2_outputs` raises after `_commit_token_vault_before_publish`; the second session sees the rows; a rerun of the same config emits the same tokens and `count(*)` is unchanged | n/a (documents the retention rule; passes on main for the per-row seam, red for the batch seam until S6b) | delete rows on failure |
| CLI `tests/e2e/test_run_vault_gating.py::test_vault_commit_failure_writes_no_outputs` | vault parent unwritable | `run.py:854` after `:840` | reorder back |
| `::test_output_failure_after_the_vault_commit_removes_the_vault` | target parent made unwritable after the vault commit (monkeypatched `_write_mask_outputs` raising) | no rollback exists | skip the unlink |
| `::test_an_existing_vault_is_refused_before_any_output` (Q4) | a pre-existing vault file; expect EXIT_USAGE, `vault_exists` in the output, vault bytes unchanged, no output files | `run.py:854` overwrites; outputs already written at `:840` | pre-delete the leaf |
| `::test_chunked_vault_commit_failure_leaves_no_output_and_no_stage` | `--chunked --vault` with the vault parent unwritable; no target file, no `_decoy_stage_*` beside it | `_write_chunked_output` writes the target directly (`run.py:1455`) | publish the stages before the vault |
| CLI `tests/e2e/test_tokenize_cli.py::test_output_failure_after_commit_removes_a_vault_this_run_created` | `_write_mask_outputs` raises after `commit()`; vault absent | `tokenize.py:383` keeps a committed vault | restore the `not committed` condition |
| `::test_output_failure_after_commit_keeps_a_preexisting_vault_and_its_new_rows` | same, against a vault from a prior run; file kept, `count()` grew, a rerun reuses the tokens | n/a (passes on main; pins the retention rule) | delete this run's rows on failure |
| CLI `tests/unit/test_token_vault.py::test_created_is_false_for_a_path_that_already_exists` (N4) | an empty 0600 file pre-created; `LocalTokenVault(path, create=True).created is False` | `created` does not exist on `a5d36b4` | derive `created` from `exists()` before the open |
| `::test_exactly_one_of_two_processes_creates_one_path` (N4) | two `multiprocessing` children open one absent path with `create=True` behind a barrier; exactly one reports `created` | same | replace `O_EXCL` with the `exists()` pre-check |
| `tests/unit/execution/test_tokenize_domain_separation.py::test_two_vaults_same_key_namespace_plaintext_mint_different_proposals` | two `FakeVault`s with distinct `derivation_scope`, same key, ns, plaintext; `puts[0][2]` differ | B-6 `_tokenize.py:253-258` | drop the scope from the label |
| `::test_writer_without_derivation_scope_fails_closed` | `FakeVault` without the attribute; expect `tokenize_vault_scope_required` before any `lookup` call | no check exists (`_tokenize.py:85-94`) | default the scope to `b""` |
| `::test_token_payload_never_equals_hash_truncate_32` | Hypothesis corpus; `hash_array(..., truncate=32)` vs token payload | B-6 `_tokenize.py:255` vs `kernel/_scalar.py:88` (probe: equal) | mint with `mask_key` as the inner IKM |
| `::test_tokenize_subkey_vector` | pinned hex for `(key, ns, scope, pt)` captured at S2 | n/a (new) | any change to the label, the prefix or the order |
| platform `tests/test_token_vault_adapter.py::test_a_vault_without_a_derivation_scope_is_refused_before_the_engine_runs` | a vault row with `derivation_scope = NULL`; `build_job_token_vault_writer` raises `token_vault_derivation_unsupported`; `run_pipeline` never called | no column, no check on main | return a constant scope for `NULL` |
| platform `tests/test_vault_store.py::test_two_vaults_tokenize_apart_through_the_real_engine` | same plaintext, two vaults, real engine via `run_pipeline` | `v2_token_vault.py:158-166` (no scope) | return a constant scope |
| CLI `tests/unit/test_token_vault.py::test_v1_vault_is_refused_with_the_derivation_code` | a schema-1 file; expect `token_vault_derivation_unsupported`, not `vault_schema_mismatch` | `_SCHEMA_VERSION` is 1 (`_token_vault.py:63`) | accept version 1; or fall through to the generic mismatch |
| `::test_derivation_scope_is_32_random_bytes_and_stable_across_reopen` | two creates differ; reopen returns the same | no scope exists | regenerate on open |
| `tests/unit/execution/test_tokenize_frame_locals.py::test_vault_failure_traceback_holds_no_plaintext_or_key[per_row, batch, lazy_mapping]` | `assert_no_engine_local_holds(exc, "canary-key-", "pt-canary-")` with the ASCII key `b"canary-key-0123456789abcdefghijk"` (A-4 `tests/fileaccess/locals_support.py:53`) | B-6 frames listed in §1 (`_resolve_tokens` local `mask_key`, `run` local `normalized`) | keep only `mask_key` as a plain local (box everything else): red on the key canary alone |
| `tests/unit/execution/test_tokenize.py::test_lazy_mapping_whose_items_raises_is_sanitized[lookup_many, put_many]` | mapping subclass whose `items()` raises with the plaintext in the message; expect `tokenize_vault_error` with empty chain | B-6 `_tokenize.py:212` (probe: `RuntimeError` escapes) | materialize outside the `try` |
| `::test_hostile_key_and_token_objects_are_rejected_inside_the_boundary` | `str` subclasses whose `__eq__`, `__hash__`, `__bool__` raise, as keys and as values, both seams | `_tokenize.py:192, 214` | replace `type(x) is str` with `isinstance` |
| `tests/property/test_tokenize_parity.py::test_batch_and_per_row_emit_identical_tokens` | generated lists with duplicates, NFC pairs, nulls, preloaded hits | B-6 `test_tokenize.py:535-541` covers one example | skip NFC on the batch seam; include a hit in `put_many` |
| platform `tests/test_vault_batch_pg.py::test_two_sessions_interleave_lookup_many_and_put_many` | two sessions, barrier after both lookups miss, A commits | no batch seam on main | return the proposal instead of the re-read |
| `::test_partial_put_many_after_a_concurrent_commit` | A commits a subset between B's lookup and put | same | skip the re-read for conflict losers |
| `::test_batch_commit_failure_publishes_nothing` | `db.commit` raises | `test_vault_durability.py:167` covers the per-row seam only | call `write_v2_outputs` before the commit |
| platform `tests/test_vault_concurrency_pg.py::test_write_lock_is_reacquired_after[commit, rollback, savepoint_rollback]` (N1) | A put, boundary, A put again; B `complete()` under `lock_timeout` must raise `55P03`; control before the boundary | `store.py:88, 160-165` (N1): after the boundary B's `complete()` returns at once | remove the listener for that boundary |
| `tests/unit/determinism/test_hkdf.py::test_engine_vectors_are_unchanged_by_the_pyca_swap` | fixture captured on main `1132d29e` from the lock, including the empty-salt refusal entries | n/a (behavior-preserving) | change the salt, info order or length handling; map an empty salt to zeros |
| `::test_oneshot_rejects_empty_salt` | `hkdf_sha256(ikm, b"", info, 32)` raises `ValueError` matching `salt must be non-empty`; red first on a naive swap seeded in the worktree (PyCA accepts, §1) | n/a on main (passes through `hkdf_extract`); red on the seeded swap | drop the guard from `hkdf_sha256` |
| `tests/sentry/test_hkdf_is_pyca.py::test_hkdf_module_delegates_to_cryptography` | `_hkdf` imports `HKDF`, `HKDFExpand`, `HMAC` from `cryptography` and does not import `hashlib` | main `_hkdf.py:26-27` | re-add the stdlib loop |
| `tests/unit/test_reason_release_pin.py::test_no_identity_claims_a_since_newer_than_the_live_registry_version` | one identity seeded with `since` one minor above `REGISTRY_VERSION` | n/a (new) | remove the check |
| `tests/sentry/test_b6_note_currency.py::test_note_signatures_and_claims_match_code` | note quotes signatures; forbidden phrases | B-6 note lines 2, 93-96, 114-115 | re-add "job seed" |

No threshold, slice bound, allowlist or census entry is widened by this plan. `vault.py` on A-4 is
525 lines; S4b is budgeted to stay under 600 and takes no census entry. The two deleted pins
(`test_char_vault_in_place.py:23-45`) are each replaced by a named test in the same commit; no other
pinned test or golden is edited.

## 7. Owner decisions applied, and the one open question

Applied as written (§0a): D2/Q1 hard cut; Q3 the platform file-vault write is fatal (decided by
`test_file_vault_commit_failure_publishes_nothing`, D1); Q4 the CLI refuses an existing vault
(`vault_exists`). Not reopened.

- Q2 (goldens), still open. Does the tokenize and hash domain separation move any pinned golden?
  Engine: no golden fixture contains a tokenize output (`grep -rl "tok_" tests/fixtures tests/golden`
  on main returns nothing); `hash`, `fpe`, generation and the file vault bytes are unchanged (D7).
  Platform: `tests/test_vault_e2e.py` and `test_token_vault_adapter.py` assert shape and round trip,
  not token values. CLI: `tests/e2e/test_tokenize_cli.py` asserts the prefix and reversal. The plan
  therefore moves no golden; confirm, or name the pin to re-record.

No decision is needed for N4 or N5: each is fixed in its class with a test, and neither changes a
documented behavior beyond what D1 already changes.

## 8. Repeat-class register and stop conditions

- F4 is the second appearance of class 1 frame-local secret material: the first was A-4 round 4
  (`_derive.py:282-309`, `vault.py` `_fernet`/`assert_keyed_with`, owner decision 2026-11-01 §A-4
  round 4). The fix is the shared construction (D4), not a local `del`. If a third instance of this
  class appears in any gate on this plan's slices, the builder stops and reports instead of patching.
- Class 4 (route parity): F5 (engine guard placement) was the first instance; N5 (platform
  relationship routes persist the vault after the per-table publish) is the second, found in rev 2
  by reading every caller of `_persist_job_vault` rather than the one rev 1 cited. A third class-4
  instance in any slice stops that slice.
- Class 7 (check then use): N1 (platform lock cache) and N4 (CLI `exists()` before a non-exclusive
  create) are the two instances, in different repos. A further class-7 finding in S6 or S7 stops
  that slice.
- N3 is class 10 (a pin without its partner), resolved by moving both pins and the behavior in one
  commit (S4a); N2 was class 10 and is closed upstream. A further class-10 finding in S4 or S7
  stops that slice.
- Class 3 (state machine): the rev 1 publication design claimed an unreachable state (gate
  finding 1); D1 now states the reachable states. A class-3 finding against D1's table in the S4
  gate stops S4b.

## 9. Measurements and probes (reproducible from the lock)

```bash
uv sync --frozen --all-extras            # engine 1132d29e; cryptography 49.0.0; Python 3.11.18
git worktree add /tmp/wt-b6 origin/feat/b6-batch-vault-writer-engine
cd /tmp/wt-b6 && /path/to/engine/.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/unit/execution/test_tokenize.py tests/unit/determinism tests/unit/test_vault.py
# 132 passed
```

Probe (run with `PYTHONPATH` at the B-6 worktree `src`; results in §1):

```python
from decoy_engine.determinism import derive
from decoy_engine.determinism._hkdf import hkdf_sha256, hkdf_extract, hkdf_expand
from decoy_engine.kernel._scalar import hash_array
from decoy_engine.execution._strategies import _tokenize as T
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF, HKDFExpand
import pyarrow as pa, unicodedata
key, ns, pt = b"k" * 32, "pan_ns", unicodedata.normalize("NFC", "4111111111111111")
h = hash_array(pa.array([pt]), seed=key, namespace=ns, truncate=32).to_pylist()[0]
tok = T._TOKEN_PREFIX + derive(key, ns, pt.encode()).hex()[:T._TOKEN_HEX_LEN]
assert h == tok[4:]                                     # F3
salt = b"decoy-engine/determinism/v1"
assert hkdf_sha256(ikm=key, salt=salt, info=b"pan_ns", length=32) == \
    HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=b"pan_ns").derive(key)   # F7
prk = hkdf_extract(salt, key)
assert hkdf_expand(prk, b"i", 64) == HKDFExpand(algorithm=hashes.SHA256(), length=64, info=b"i").derive(prk)
# F4: raise from a writer's put; walk exc.__traceback__ frames; see §1 for the frames found.
# F5: a dict subclass whose items() raises, returned from lookup_existing_many: RuntimeError escapes.
```

Empty-salt probe (rev 2; system Python 3.13.18, `cryptography 50.0.1`, standalone copy of main
`a3fe49e8` `determinism/_hkdf.py` imported as a module; the lock environment was not built in the
rev 2 session, so the builder re-runs this from the lock in S1 and records the output):

```python
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import _hkdf_main as H                      # copy of src/decoy_engine/determinism/_hkdf.py
k = b"k" * 32
empty = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"", info=b"i").derive(k)
zeros = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"\x00" * 32, info=b"i").derive(k)
none_ = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"i").derive(k)
assert empty == zeros == none_              # PyCA: empty salt silently equals the all-zero salt
H.hkdf_sha256(ikm=k, salt=b"", info=b"i", length=32)   # ValueError: hkdf_extract: salt must be non-empty
assert H.hkdf_sha256(ikm=k, salt=b"\x00" * 32, info=b"i", length=32) == zeros
```

Output observed: `pyca empty==zeros: True empty==None: True`; `engine one-shot: ValueError:
hkdf_extract: salt must be non-empty`; `engine zeros == pyca zeros: True`.

LOC (`wc -l`, lock env, rev 1): `vault.py` main 451 / A-4 525; `_tokenize.py` B-6 276; `_derive.py`
A-4 419; `_adapter.py` B-6 431; `_hkdf.py` 95; `_local.py` A-4 378; `_output.py` A-4 389;
`_opaque.py` A-4 37.

## 10. Plan lint and done-when

Plan lint: no plan-lint tool exists in decoy-engine, decoy-platform or the CLI (searched
`plan-lint`, `plan_lint`, `planlint`, `lint_plan` over `*.py`, `*.toml`, `*.md`, `*.yml`, `*.sh`;
the only hits are two prose mentions in `decoy-platform/docs/ROADMAP.md`). The referenced
`/home/cam/dev-rules` directory is not present in this environment. This plan was checked by hand
against the required-content list: every finding, including the eight gate findings and N4, N5,
has a section, a slice, a named red test with its seeded violation, fail-before location and
mutation; the owner decisions are applied in §0a and nowhere reopened; the version bumps and their
recording places are in §5; every slice has a changed-line estimate under 800 in §4.

Done when: this file is on `plan/b6-vault-crypto-remediation`; the ROADMAP B-6 row in
decoy-platform links it (a one-line platform PR by the orchestrator, not this plan); the Codex plan
gate returns GO; S1-S7b then follow the order in §5.
