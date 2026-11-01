Status: plan, rev 3 (Opus, 2026-11-01). Handoff. Risk class R2 (plan only, no code).

# PLAN: B-6 tokenization vault, crypto remediation (engine, platform, CLI)

Branch `plan/b6-vault-crypto-remediation` off engine `1132d29e` (R-a, 0.17.0). Rev 3 resolves the
Codex gate NO-GO on rev 2 (`f4a2a3b1`: 2 BLOCKER, 2 HIGH, 2 MEDIUM) by replacing one design choice,
"delete a NEW vault after an output failure", with the owner's never-delete rule (§0a), and shrinks
the plan around it. Rev 1's gate findings stay resolved where rev 2 put them (§0c). The plan builds
after A-4 (`claude/jolly-carson-3k5stf`) merges; nothing here is built; every slice is red first.

Bases re-read for rev 3 (2026-11-01):

| Repo | Ref | Sha | Role |
|---|---|---|---|
| decoy-engine | `origin/main` | `51548303` | current main. Every engine file this plan cites on main is byte-identical to rev 2's `a3fe49e8` (`git diff --stat a3fe49e8 51548303 -- src/decoy_engine/determinism/_hkdf.py src/decoy_engine/vault.py src/decoy_engine/reasons scripts/release/reason_release_pin.py tests/unit/test_reason_release_pin.py tests/unit/determinism/test_hkdf.py docs/reasons.md docs/security/token-vault.md` is empty) |
| decoy-engine | `origin/claude/jolly-carson-3k5stf` | `a19889a5` | A-4 (`fileaccess`, `_opaque`, registry 1.2.0); merge-base with main `a3fe49e8` |
| decoy-engine | `origin/feat/b6-batch-vault-writer-engine` | `e3c539d6` | B-6 batch writer under review; unchanged since rev 2 |
| decoy-platform | `origin/main` | `7fc4612d` | `git diff --stat 2d40c9a 7fc4612 -- api/vaults api/jobs api/unmask tests/test_vault_durability.py` is empty: every platform line cited by rev 2 is unchanged; the lines added in rev 3 were read at `7fc4612d` |
| louiskeep/decoy (CLI) | `origin/main` | `a5d36b4` (rev 2) | NOT re-read in rev 3: attaching the repository was refused in this session. CLI citations are rev 2's (`run.py:840, 854`, `tokenize.py:326, 341, 348, 383`, `_token_vault.py:109`) plus the gate's (`run.py:1572-1589`, `_write_mask_outputs`, the per-table write loop). Each CLI slice re-reads them first and records the shas; a moved line is a plan amendment, not a builder decision |

Measurements: rev 1's were taken from the engine lock (`uv sync --frozen --all-extras`, Python
3.11.18, `cryptography 49.0.0`) and stand as recorded in §9; rev 2's empty-salt probe ran on the
system interpreter and is labelled. Rev 3 made no new measurement; every rev 3 claim is
VERIFIED-BY-READ at the shas above, and every slice names the test that proves it in CI.

## 0a. Owner decisions applied (delegated, not reopened)

- Rev 3 (2026-11-01), the retention rule: a committed vault is NEVER deleted by a failure path.
  Only an unpublished stage file is cleaned. A vault that exists is the recovery key for any
  output that may have been published; a leftover vault with no output is harmless (D1 R4). This
  removes rev 2's NEW versus EXISTING cleanup table and its `O_EXCL` ownership-for-deletion race.
- D2 / Q1: HARD CUT. A vault minted under the old derivation is refused with
  `token_vault_derivation_unsupported` before the engine runs. No marker, no dual derivation, no
  migration of data.
- Q4: the CLI refuses to overwrite an existing vault (`vault_exists`, registered, EXIT_USAGE) on
  both `run --vault` routes. The refusal is the engine's exclusive publication (`link(2)` refusing
  an existing leaf); it is not an ownership claim and licenses no deletion.
- Q3: the platform's non-fatal file-vault write becomes fatal (D1 "Platform").

## 0b. Gate findings on rev 2 (`f4a2a3b1`) and where each is resolved

| # | Sev | Class (fixed as a class) | Resolved in | Slice | Red-first test; seeded violation; fails before |
|---|---|---|---|---|---|
| 1 | BLOCKER | 5 referential integrity (published output whose vault is gone) and 2 (a destructive cleanup on an exit path). Class fix: one retention rule R1-R4 for every caller, plus a no-removal sentry per repo | D1 R1-R4, callers table | S4b, S6a-ii, S7a, S7b | platform `tests/test_vault_durability.py::test_table_b_failure_keeps_a_vault_that_reidentifies_table_a[sequential, out_of_core]`; seeded: `write_v2_outputs` raises on table 2 of 2; fails before at main `v2_sequential.py:336` publishing inside the loop `:319-350` while the vault is persisted only at `route_dispatch_exec.py:246-247`, after the loop, and the uncommitted node is lost to `v2_orchestrator.py:269` `db.rollback()`; `/unmask` answers `unmask_vault_not_found` (`unmask/router.py:80-95`). CLI `tests/e2e/test_run_vault_gating.py::test_table_b_failure_keeps_a_vault_that_reidentifies_table_a`; seeded: the table-B write raises inside `_write_mask_outputs` (`run.py:1572-1589`); fails before at `run.py:840` (outputs) preceding `:854` (vault). Sentries: platform `tests/test_vault_retention_sentry.py::test_no_code_path_removes_a_vault_leaf` (seeded: rev 2's unlink-on-failure wrapper) |
| 2 | BLOCKER | 8 (a harness that cannot reach the states it asserts) and 3 | D1 "Crash harness": one wrapped call per step, kill after it returns, K7 is the no-kill control with rc 0, table = harness | S4a, S4b | `tests/unit/test_vault_publish.py::test_sigkill_after_each_publish_step[ambient\|strict × K1..K7]`; seeded: none needed, the A-4 code is the violation; fails before at A-4 `_local.py:225-231` (ambient opens the leaf in place, K1 never fires), `serial.py:45-47` (buffered `fh.write`, no `os.write` and no file `fsync`: K2, K3 never fire), `_output.py:252-268` (no directory fsync in the exclusive branch: K5 never fires); the child exits 0 and the parent fails "kill point K<n> not reached" |
| 3 | HIGH | 10 (the registry bump is a frozen contract; a pin moved without its partner) and 8 | §5 "Reason registry": the base is derived from git, not hardcoded | S2 (lands the test), S4b (uses it) | `tests/unit/test_reason_registry_bump.py::test_registry_is_merge_base_plus_one_minor_and_new_identities_carry_it`; seeded: one new identity with `since="1.2.0"` and `REGISTRY_VERSION` left at `"1.2.0"` on a branch whose merge-base value is 1.2.0; fails before at `scripts/release/reason_release_pin.py:48-65` (released pin 1.1.0 < live 1.2.0, so `pin_violations` checks only `since > current` and `since <= pinned`; a `since` equal to an unreleased live version passes) |
| 4 | MEDIUM | 8 (a test that contradicts the design it tests) | D1 N3 | S4a | `tests/unit/test_vault_publish.py::test_an_existing_hardlinked_vault_is_refused_and_both_names_are_unchanged`; seeded: leaf and twin are two names of one inode holding old bytes; fails before at A-4 `test_char_vault_in_place.py:23-30` (pin asserts the write goes through the twin) and `_local.py:225-231` |
| 5 | MEDIUM | 4 (route parity; hand-maintained route list). Second class-4 miss on this plan; a third stops the slice (§8) | D1 "Route parity" | S6a-ii | `tests/test_vault_route_parity_sentry.py::test_every_route_commits_the_vault_before_publish_or_refuses_vault_jobs` (derived from `ALL_ROUTES` plus AST) and `tests/test_vault_durability.py::test_vault_is_committed_before_any_output_exists[<derived route>]`; seeded: a synthetic `execute_<route>` reading `ctx.vault_writer` with no driver, and a route in `ALL_ROUTES` with no executor; fails before at `route_dispatch_exec.py:246-247` (persist after the relationship publish) |
| 6 | MEDIUM | 3 and 7 (an ownership claim used to delete). Fixed by removing the use: nothing deletes a leaf, so no ownership is needed | D1 "Q4 and N4" | S7a, S7b | CLI `tests/e2e/test_run_vault_gating.py::test_a_concurrent_second_run_is_refused_and_never_deletes_the_first_runs_vault`; seeded: two `decoy run --vault <same>` processes released together, and a sequential variant where run 1 fails on table B first; fails before at `run.py:854` (the vault write overwrites) and `tokenize.py:383` (unlinks a vault it judged its own from `exists()` at `:326`) |

Finding 1's platform half needs one thing rev 2 did not have: the `target.vault` node must be
committed with the vault, before the first publish, or a failed job's rollback
(`v2_orchestrator.py:269`) removes the only proof `/unmask` accepts (`router.py:80-95` requires a
`status=="ok"` node). This is part of finding 1's test ("re-identifies table A"), not a new
finding; it is reported here because it changes the platform design.

## 0c. Rev 1 gate findings (`27032a1b`): still resolved

1 (unreachable states, mocks as crashes) and 2 (inverse partial commit): D1, rewritten in rev 3.
3 (registry): §5, rewritten in rev 3. 4 (N1 lock proof): D6, S6b. 5 (N3 pins): D1 N3, pin 1
corrected in rev 3. 6 (empty salt): D7, S1. 7 (slice sizes): §4. 8 (F4 canary): D4, S3.

## 0. Findings register

Classes: (1) value-bearing output; (2) cleanup missing on an exit path; (3) state machine and
concurrency; (4) route parity and silent fallback; (5) referential integrity; (6) untrusted
producer; (7) check then use; (8) weak tests and overclaimed evidence; (9) behavior-preserving
refactor drift; (10) frozen contract drift.

| Id | Sev | Class | Where (re-read) | Design | Slice |
|---|---|---|---|---|---|
| F1 | HIGH | 3, 5 (+2 crash window, +10 pinned partner) | main `vault.py:305` (`Path.write_bytes`); A-4 `vault.py:336-350`, `fileaccess/_local.py:225-231`, `_output.py:71-77, 246-275`, `adapters/serial.py:45-47`; platform `v2_full_frame.py:166-206`, `v2_vault.py:65-100`, `route_dispatch_exec.py:236-247`, `v2_sequential.py:319-350, 450`; CLI `run.py:840, 854, 1572-1589`, `tokenize.py:341, 348, 383` | D1 | S4a, S4b, S6a-ii, S7a, S7b |
| F2 | HIGH | 1 | B-6 `_tokenize.py:253-258`; `_adapter.py:98-157`; platform `v2_token_vault.py:158-166`; CLI `_token_vault.py:215-251` | D2 | S2, S6a-i, S7a |
| F3 | HIGH | 1 | B-6 `_tokenize.py:255` vs `kernel/_scalar.py:88` | D3 | S2 |
| F4 | MEDIUM | 1 (second appearance, §8) | B-6 `_tokenize.py:147-155, 181, 233-258` | D4 | S3 |
| F5 | MEDIUM | 4 (guard placement) | B-6 `_tokenize.py:169-186, 192, 212` | D5 | S3 |
| F6 | MEDIUM | 8 | B-6 `test_tokenize.py:535-541, 565-579`; platform `v2_token_vault.py:146-166` | D6 | S5, S6b |
| F7 | MEDIUM | 9 | `determinism/_hkdf.py:1-95` (`:54-59` empty-salt refusal) | D7 | S1 |
| F8 | LOW | 8 | B-6 `docs/handoffs/inputs/b6-engine-api-note.md:2, 93-96, 114-115` | D8 | S5 |
| N1 | HIGH | 7 | platform `api/vaults/store.py:88, 160-165` (`_write_locked` never cleared) | D6 | S6b |
| N2 | closed upstream | 10 | CLI `57e8b81` (`put -> str`, pin 0.17.0) | D2 | S7a (repin only) |
| N3 | MEDIUM | 10 | A-4 `tests/fileaccess/test_char_vault_in_place.py:23-30, 33-45` pin the in-place rewrite F1 removes | D1 N3 | S4a |
| N4 | MEDIUM | 7 | CLI `tokenize.py:326` (`exists()`) then `:383` (unlink) | D1 Q4 and N4: the unlink is deleted, so the check has no use and is deleted | S7a |
| N5 | HIGH | 4 | platform relationship routes publish per table (`v2_sequential.py:319-350`) before the vault persist (`route_dispatch_exec.py:246-247`) | D1 route parity | S6a-ii |

## 1. What was verified, and how

| Claim | Result | Where it fails before the fix |
|---|---|---|
| F3: `hash` `truncate: 32` equals the token payload for one `(mask_key, namespace, NFC(pt))` | True (probe, lock env, §9) | `_tokenize.py:255`, `kernel/_scalar.py:88` |
| F2: the proposal is a pure function of `(mask_key, namespace, plaintext)` | True (probe and by construction) | `_adapter.py:98-157` has no vault input |
| F4: frames on the `StrategyError` traceback holding plaintext or key | `run`: `df, ctx, col, normalized, distinct`; `_resolve_tokens`: `distinct, mask_key, plaintext, minted`; `_vault_call`: `args` (probe) | listed lines |
| F5: a batch result whose `items()` raises | `RuntimeError` escapes `_vault_call`, quoting the plaintext (probe) | `_tokenize.py:212` |
| F7: PyCA `HKDF`/`HKDFExpand` equal `_hkdf` byte for byte; PyCA `salt=b""` equals the all-zero salt; main's one-shot refuses `b""` | True; True; raises (probe; the empty-salt part on system Python 3.13.18, `cryptography 50.0.1`) | a naive swap accepts `b""` |
| F1 engine write paths | main `vault.py:305` writes the leaf directly; A-4 ambient opens the leaf `O_TRUNC` in place (`_local.py:34, 225-231`); A-4 strict stages and links (`_output.py:252-268`) but writes through a buffered handle with no `fsync` (`serial.py:45-47`), swallows the directory fsync (`_output.py:71-77`, `_local.py:178-193`) and swallows the stage unlink (`_output.py:266-269`) | by read |
| F1 callers publish before the vault commits | platform full frame `v2_full_frame.py:168` before `:206`; relationship `v2_sequential.py:336` (inside the `:319-350` loop) before `route_dispatch_exec.py:247`; CLI `run.py:840` before `:854`; CLI tokenize `:341` before `:348` | by read |
| A failed platform job loses an uncommitted `target.vault` node | `v2_orchestrator.py:269` `db.rollback()` on any execution error; `/unmask` requires a `status=="ok"` node (`unmask/router.py:80-95`) and the file (`:97-108`) | by read |
| Platform route set is closed | `dispatch_route.py:55` `ALL_ROUTES`; `:121` refuses any other planned route; dispatch arms `v2_orchestrator.py:254-258`; streaming refuses vault columns (`_phase1_eligibility.py:181-183`) | by read |
| No platform code removes a vault file today | `git grep "job_vault_path\|vault_dir()\|dcyvault" 7fc4612d -- api` finds the writer (`v2_vault.py:43-100`), the reader (`unmask/router.py:97-99`), quota and inventory accounting (`disk_quota_matrix.py:139`, `admin/storage_inventory.py:423`) and no unlink | by read |
| Registry | main `REGISTRY_VERSION = "1.1.0"` (`_registry.py:24`), released pin 1.1.0; A-4 `"1.2.0"`; identity key `(namespace, code)` (`_registry.py:28, 150-178`) | by read |
| Engine CI depth | `ci.yml:102` `regression-gate` has `fetch-depth: 0`; `engine-v2-substrate-matrix.yml:51` runs `pytest tests` on a depth-1 checkout | by read |

Unverifiable here: PostgreSQL behavior (S6 tests run in the `backend-postgres` leg on
`postgres:15`, marker `pg_semantic`, which fails the leg on skip) and power-loss durability of
`fsync` (a kernel property; the engine tests prove the call order with strace and name/content
crash consistency with SIGKILL).

## 2. Established methodology (surveyed; nothing is invented)

- Purpose separation of derived keys: RFC 5869 §3.2 (`info` binds a key to its context), NIST SP
  800-108r1 §4 (`Label` names the purpose, `Context` the parties). The engine already does this
  (`vault-key/v1`, `vault.py:88`; `ff1-key/v1`, `_derive.py:126`); D2 adds `tokenize-key/v1`.
- KDF implementation: PyCA `HKDF`, `HKDFExpand`, `HMAC` (RFC 5869); the platform already uses PyCA
  `HKDF` (`api/crypto/vault_key.py:187`). The engine's empty-salt refusal is an application rule
  over the primitive (QA-10 F12), kept on both entry points (D7).
- Crash-atomic publication: stage in the same directory, `fsync` the file, `link` into the leaf,
  `fsync` the directory (POSIX; SQLite's atomic-commit notes, PostgreSQL `durable_rename`). The
  leaf is the only authoritative name; a stage is never read as data. Exclusive `link(2)` is the
  compare-and-set between two writers (A-4's `exclusive` mode).
- Retention: a write-ahead artifact is kept until the thing that depends on it is gone, never
  removed on the failure of a later step (PostgreSQL keeps WAL a replica may need; SQLite never
  deletes a database file on a failed transaction, it rolls the transaction back inside it). Here
  the vault is the dependency of any published output, so no failure path removes it.
- Crash tests: a child interpreter killed with `SIGKILL` at a numbered point, the state asserted
  by the parent (SQLite `crashtest`). `SIGKILL` runs no `finally`, `__del__` or
  `weakref.finalize`, which a monkeypatched `raise` cannot model.
- Lock proofs: the blocked session runs under `lock_timeout` and expects SQLSTATE `55P03`.
- Secret-free frames: A-4's `OpaqueBox` (`_opaque.py`, `secret_providers/_preflight.py:165-198`).
- Idempotent concurrent insert: `INSERT ... ON CONFLICT DO NOTHING` plus re-read (`store.py:255-319`).

## 3. Designs

### D1. Durable, exclusive vault publication; vault before output; never delete (F1, N3, N4, N5)

Retention rule (one rule; every caller, every repo):

- R1. Once `link(stage, leaf)` has returned, no failure path in engine, platform or CLI removes,
  truncates or replaces the leaf.
- R2. A failure path removes only names it created and never published: the engine's own
  `_decoy_vault_stage_*`, and the CLI chunked route's own output stages. Nothing else.
- R3. Every caller commits the vault before its first output byte is published. A vault commit
  failure publishes nothing. On the platform, "commits" includes the `target.vault` node.
- R4. A vault left by a failed job is kept. It re-identifies whatever of that job's output was
  published; with no output it holds ciphertext of mappings the same job config would recreate,
  under the same 0600 file and directory rules as any vault. `docs/security/token-vault.md`
  "Operational rules" states this and the operator rule: delete a vault only together with the
  outputs it re-identifies; a `_decoy_vault_stage_*` file beside a vault with no running job may
  be deleted, it is never a vault.

Engine (`vault.py`, A-4 `fileaccess`, after the A-4 merge). `VaultWriter.write(path | OutputDir)`
always publishes through a stage beside the leaf in `exclusive` mode. The ambient path-string
route stops using `vault_direct`: `_local.py:225-231` and the label are deleted (hard delete);
an ambient path stages in its parent exactly as the strict route does (`_CREATE` with `O_EXCL`,
`_local.py:33, 244-246`; prefix table `_output.py:32-41`). The steps, in both `RootedOutputTxn`
(`_output.py:246`) and `LocalOutputTxn` (`_local.py:260`):

1. `os.open(parent/_decoy_vault_stage_<hex>, O_CREAT | O_EXCL | O_WRONLY)`.
2. `os.write` in a loop until every byte is written (`serial.write_bytes(..., durable=True)`; the
   raw-descriptor loop replaces the buffered `fh.write` of `serial.py:45-47` for this mode only,
   because it handles short writes and keeps the descriptor for step 3).
3. `os.fsync(stage fd)`, then close.
4. `os.link(stage, leaf, follow_symlinks=False)`: the compare-and-set. `EEXIST`: the stage is
   unlinked (R2) and `write` raises `VaultError(code="vault_exists")`.
5. `os.fsync(parent directory fd)`. This is the commit point. A failure raises
   `vault_commit_failed`; the stage name is unlinked (R2); the leaf is kept (R1).
6. `os.unlink(stage)`.
7. `os.fsync(parent directory fd)`.

`write` returns the entry count after step 7. Steps 6 and 7 tidy a name after the commit: an
error there is not raised and nothing value-bearing is recorded; it
leaves the K5 or K6 residue below, which R4's operator rule covers. Every `OSError` from steps 2-5
becomes `vault_commit_failed` with no path and no errno text; a step 1 failure keeps the A-4 shape
(`FileNotFoundError`/`PermissionError` naming the path under the ambient policy, as
`test_char_vault_in_place.py:48-54` pins; `FileAccessUnavailable` under a strict policy), because
nothing was created. Existing `publish()` and `write_bytes()` callers are unchanged (`durable`
defaults to `False`).

Crash harness (gate rev 2 #2). `tests/unit/vault_publish_child.py <route> <K> <dir>` replaces
`os.open`, `os.write`, `os.fsync`, `os.link` and `os.unlink` on the `os` module before importing
the engine (the `fileaccess` modules call them as `os.<name>` at call time: `_local.py:231, 284,
296`; `_output.py:256` passes `os.link` through `_call`). Each wrapper calls the real function
and, when its step's predicate matches, sends itself `SIGKILL` after the call has returned. No
production seam is added. `route` is `ambient` (path string) or `strict` (`OutputDir`).

| K | Step | Wrapped call, killed after it returns | Predicate | Leaf after | Stage after | Child rc | Parent's second `write` |
|---|---|---|---|---|---|---|---|
| K1 | 1 | `os.open` | basename starts `_decoy_vault_stage_` | absent | present, 0 bytes | -9 | publishes |
| K2 | 2 | `os.write` | fd is the K1 descriptor; first call | absent | present, exactly the byte count that call returned | -9 | publishes |
| K3 | 3 | `os.fsync` | fd is the K1 descriptor | absent | present, every byte | -9 | publishes |
| K4 | 4 | `os.link` | destination basename is the leaf | present, loads | present, same inode, `st_nlink == 2` | -9 | `vault_exists` |
| K5 | 5 | `os.fsync` | `S_ISDIR(fstat(fd))`, first call after K4's | present, loads | present, same inode | -9 | `vault_exists` |
| K6 | 6 | `os.unlink` | basename starts `_decoy_vault_stage_` | present, loads, `st_nlink == 1` | absent | -9 | `vault_exists` |
| K7 | none: step 7 runs and the child exits | none | none | present, loads | absent | 0 | `vault_exists` |

`test_sigkill_after_each_publish_step[ambient|strict × K1..K7]` asserts the row exactly: the
return code first (a K1-K6 child that exits 0 fails with "kill point K<n> not reached"; a K7
child that does not exit 0 fails), then the leaf (`load_vault` returns the expected entries when
present), the stage (size, inode, link count), then the parent's second `write` to the same leaf,
after which any residue the kill left is byte-identical and the parent left no stage of its own.
Red first on A-4 `a19889a5`: ambient K1-K6 are not reached (no stage, `_local.py:231`); strict
K2, K3 (no `os.write`, no file `fsync`, `serial.py:45-47`) and K5 (`_output.py:252-268`) are not
reached. Main `vault.py:305` behaves as ambient.

Fault injection (not a crash test, labelled so):
`test_an_injected_failure_at_each_step[write, file_fsync, link_eio, link_eexist, dir_fsync, unlink,
dir_fsync_after_unlink]` patches the named call to raise and asserts one row each: before the link
(`write`, `file_fsync`, `link_eio`) `vault_commit_failed`, no leaf, no stage; `link_eexist`
`vault_exists`, the pre-existing leaf byte-identical, no stage; `dir_fsync` `vault_commit_failed`,
the leaf kept and loads (R1), no stage; `unlink` and `dir_fsync_after_unlink` `write` returns the
count, the leaf loads, at most the K5-shaped stage remains. No message holds the path or errno
text. Red first on A-4: `dir_fsync` returns success (`_output.py:71-77`).

Order: `test_publish_fsyncs_file_then_directory` runs `tests/fileaccess/strace_support.py`
(`-f -yy`) over a child and asserts `fsync(stage)` before `linkat`, `fsync(parent)` after `linkat`
and before `unlinkat`, and `fsync(parent)` after `unlinkat`; the helper fails, never skips, when
`strace` is absent. Two writers: `test_two_real_writers_one_publishes_one_fails_closed` (two
threads, a `Barrier` after both staged; one leaf, the loser `vault_exists`, no stage residue).

N3 (gate rev 2 #4). Both pins of `test_char_vault_in_place.py` assert the in-place rewrite and are
replaced in S4a, the commit that deletes `vault_direct`:

- pin 1 (`:23-30`) becomes `test_an_existing_hardlinked_vault_is_refused_and_both_names_are_unchanged`:
  leaf and twin are two names of one inode holding old bytes; `write(leaf)` raises `vault_exists`;
  afterwards both names have the original `st_ino`, `st_nlink == 2` and the old bytes, and no
  stage remains. The leaf cannot become a new inode: exclusive publication refuses it.
- pin 2 (`:33-45`) becomes `test_an_existing_vault_in_a_read_only_parent_is_preserved_and_no_stage_remains`:
  step 1 fails with `PermissionError` naming the path (ambient); the vault bytes and the listing
  are unchanged. Skipped only when `os.geteuid() == 0`, as the deleted pin was; engine CI runs
  `ubuntu-latest` without a container (`ci.yml`), where it runs.

Callers (one retention rule; no per-caller split):

| Caller (read) | Vault | Order after this plan | Output failure after the vault commit | Test |
|---|---|---|---|---|
| platform full frame, `v2_full_frame.py:160-172, 205-206` | file vault `job_<id>.dcyvault`, token rows | after the last cancel poll: `vault_writer.write(job_vault_path)`; add the `status="ok"` `target.vault` node; one `db.commit()` that also commits the token rows (`_commit_vaults_before_publish` in `v2_vault.py` replaces `_commit_token_vault_before_publish`, `v2_full_frame.py:47-62`); then `write_v2_outputs` | job failed; vault, node and rows kept; `/unmask` works for every published table | `tests/test_vault_durability.py::test_output_failure_keeps_a_reachable_vault[full_frame]` |
| platform relationship routes (N5), `v2_sequential.py:319-350, 450`; `route_dispatch_exec.py:246-247` | file vault | `_commit_vaults_before_publish` runs in `_run_relationship_job_to_targets` immediately before `_materialize_staged_sequential_outputs` (`:450`); the late persist at `route_dispatch_exec.py:246-247` is deleted | tables already published stay; vault and node kept; job failed | `::test_table_b_failure_keeps_a_vault_that_reidentifies_table_a[sequential, out_of_core]` (POST `/unmask` on table A returns the source values) |
| platform token rows | Postgres | committed in the same `db.commit()` | rows kept; a rerun emits the same tokens and adds no row | `::test_output_failure_after_the_token_commit_keeps_rows_and_a_rerun_adds_none` |
| CLI `run.py` plain route, `:840, :854`, loop `:1572-1589` | file vault | vault (exclusive: an existing leaf is `vault_exists`, EXIT_USAGE, nothing written), then `_write_mask_outputs` | tables already written stay; the vault stays; EXIT_RUNTIME; the message says the vault was kept and that a rerun needs a new `--vault` path or the old vault removed with its outputs | `tests/e2e/test_run_vault_gating.py::test_table_b_failure_keeps_a_vault_that_reidentifies_table_a`, `::test_vault_commit_failure_writes_no_outputs`, `::test_an_existing_vault_is_refused_before_any_output` |
| CLI `run.py` chunked route, `_run_chunked_mask:1398`, `_write_chunked_output:1455` | file vault | each table streams to its own `_decoy_stage_<hex><suffix>` beside its target; then the vault; then each stage is `os.replace`d onto its target | a vault failure unlinks every output stage (R2), no target written; a failure inside the replace loop keeps the vault and the renamed tables, unlinks the remaining stages (R2), and names how many of N tables published | `::test_chunked_vault_commit_failure_leaves_no_output_and_no_stage`, `::test_chunked_replace_failure_keeps_the_vault`; existing `tests/e2e/test_vault_cli.py::test_chunked_run_writes_equivalent_vault` stays green |
| CLI `tokenize.py`, `:326, :341-348, :377-383` | SQLite token vault | `commit()`, then `_write_mask_outputs` | the file is never removed by a failure path; rows kept; a rerun reuses them | `tests/e2e/test_tokenize_cli.py::test_output_failure_after_commit_keeps_the_vault_and_its_rows` |

Platform: fatal. `_persist_job_vault` (`v2_vault.py:65-100`) stops catching: its error node, the
`str(exc)` in it and the "does NOT fail the job" docstring are deleted (hard delete).
`_commit_vaults_before_publish` raises `VaultCommitError` (new, `v2_vault.py`, carrying the engine
code `vault_commit_failed` or `vault_exists`), which the runner's terminal handler
(`runner.py:124-156`) lands as a failed job whose manifest names the code and no path.
`test_file_vault_commit_failure_publishes_nothing`: `write` raises `vault_commit_failed`; the job
fails; `write_v2_outputs` is never called. If the engine `write` succeeded and the `db.commit()`
then fails, no output is published and the leaf without a node is an R4 leftover that `/unmask`
refuses; `::test_node_commit_failure_publishes_nothing_and_keeps_the_leaf` pins it.

Route parity (N5, gate rev 2 #5). The vault-capable route list is derived, not maintained:
`tests/_route_vault_derivation.py::derive()` returns `{route: "commits" | "refuses"}` from

1. the closed route set `api.jobs.dispatch_route.ALL_ROUTES` (`dispatch_route.py:55`; `:121`
   refuses any other planned route, so no route exists outside it);
2. AST of `route_dispatch_exec.py`: each route `r` has a function `execute_<r>` (missing: fail),
   and every `execute_*` or `dispatch_*` call in the orchestrator's dispatch block
   (`v2_orchestrator.py:254-258`, located by AST, not by line) resolves to a function of that
   module (unmapped: fail);
3. classification: `commits` when the executor reads `ctx.vault_writer` (AST attribute access),
   `refuses` when the route's eligibility module emits a reason literal starting
   `vault_not_supported_` (streaming: `_phase1_eligibility.py:181-183`), neither: fail (a route
   that drops a vault silently, class 4).

`test_every_route_commits_the_vault_before_publish_or_refuses_vault_jobs` runs `derive()` on the
tree and requires every `commits` route to have a job driver in `tests/test_vault_durability.py`'s
`DRIVERS` mapping; a derived route with no driver fails at collection. Seeded red (fixture source
trees under `tests/fixtures/route_sentry/`): a new `execute_x` reading `ctx.vault_writer` with no
driver; a route in `ALL_ROUTES` with no executor; a route neither reading the writer nor refusing.
The runtime property `test_vault_is_committed_before_any_output_exists[<each commits route>]`,
parametrized from `derive()`, spies `_commit_vaults_before_publish`: at its return the leaf loads,
a second `Session` sees the `ok` node, and the job's target directory holds no output file; after
the job every declared target exists. That ordering holds whichever function publishes, so a new
publisher inside an existing route is also caught. Mutation: move the relationship commit back
after the loop; `[sequential, out_of_core]` go red.

Retention sentry (class fix for finding 1, platform): `tests/test_vault_retention_sentry.py::
test_no_code_path_removes_a_vault_leaf` walks the AST of `api/**/*.py` and fails when a function
that references `job_vault_path` or `vault_dir` also calls `unlink`, `remove`, `rmtree`,
`truncate`, `os.replace` or `os.rename`. Empty allowlist (no such function exists at
`7fc4612d`, §1). Seeded red: rev 2's wrapper that unlinked `job_vault_path(job.id)` on an output
failure.

Q4 and N4 (gate rev 2 #6). The CLI `run --vault` refusal is the engine's exclusive publication
(step 4); the CLI maps `vault_exists` to EXIT_USAGE and adds no `exists()` pre-check. Tokenize:
with R1 nothing unlinks the SQLite vault, so `vault_preexisted = vault.exists()`
(`tokenize.py:326`) and the unlink at `:383` are both deleted; `LocalTokenVault`'s open
(`_token_vault.py:109`, `O_RDWR | O_CREAT`, 0600) is unchanged. No ownership flag exists.
`test_a_concurrent_second_run_is_refused_and_never_deletes_the_first_runs_vault`: (a) two
`decoy run --vault V` processes released by a `multiprocessing.Barrier` in a launcher, 20 trials:
exactly one exits 0, the other exits EXIT_USAGE with `vault_exists`, `V` loads with the winner's
entries and has the winner's `st_ino`, the loser wrote no output; (b) sequential: run 1 fails on
table B (finding 1's seed), run 2 on the same `V` is refused, `V` is byte-identical to run 1's and
re-identifies run 1's table A. Mutation: an unlink of the leaf on the refused path or on output
failure; (a) or (b) goes red.

One existing CLI test inverts under R1 and is named here as the only pinned test this plan edits
outside N3: `tests/e2e/test_tokenize_cli.py::test_failed_run_leaves_no_empty_vault` becomes
`::test_failed_run_keeps_the_vault_file_and_adds_no_rows` (a run that fails before `commit()`
leaves the file, with zero rows from that run). Deleting the file needed the ownership claim the
owner removed (the N4 race: another run may have committed rows into it). `::test_failed_run_keeps_a_preexisting_vault`
stays unchanged.

Architecture view (the whole lifecycle):

```
caller (platform route / CLI)                engine VaultWriter.write(leaf)
  run engine; vault_writer filled
  last cancel poll
  -- vault commit --------------------------> 1 open stage O_EXCL   2 os.write loop   3 fsync(stage)
                                              4 link(stage, leaf)   EEXIST -> vault_exists (stage unlinked)
                                              5 fsync(dir)          COMMIT POINT; error -> vault_commit_failed, leaf kept
                                              6 unlink(stage)       7 fsync(dir)   (errors leave junk, never raise)
  platform: add target.vault ok node; db.commit() (token rows too)
     any failure up to here: job fails, nothing published
  -- publish table 1 .. N -------------------  failure at table k: tables < k stay, vault stays (R1), job fails
  success
removal of a vault leaf: never by code; an operator deletes a vault with its outputs (R4)
```

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
  `secrets.token_bytes`; `_SCHEMA_VERSION` 1 to 2 (`_token_vault.py:63`). The scope is written
  inside the schema-initialising transaction (`BEGIN IMMEDIATE`, `INSERT OR IGNORE`, then re-read),
  so two first runs on one path agree on one scope without any ownership flag (rev 3, D1 N4);
  `test_derivation_scope_is_32_random_bytes_and_stable_across_reopen` gains a two-process case.
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

Each slice: one repo, at most 10 production files, at most 800 changed lines (tests counted in
lines, not files), red tests first, then the change, then the mutation run recorded in the PR.
Module LOC (aim 600, max 700; `wc -l`): `vault.py` 451 on main and 531 on A-4 `a19889a5` (rev 2
said 525, read at an earlier A-4 head); `_tokenize.py` 276 on B-6; `_adapter.py` 431 on B-6;
`_local.py` 378, `_output.py` 389, `serial.py` 47 on A-4. S4b deletes the ambient branch from
`vault.py` (`:336-343`) and adds about 30 lines; S3 splits the key helper out of `_tokenize.py`.

| Slice | Repo, base | Resolves | Production files | Depends on |
|---|---|---|---|---|
| S1 | engine, main | F7 | `determinism/_hkdf.py`, `docs/methodology-registry.yaml`, `docs/security/key-derivation.md` | none |
| S2 | engine, main after B-6 | F2, F3, hard-cut codes, registry bump test (gate rev 2 #3) | `execution/_adapter.py`, `execution/_strategies/_tokenize.py`, `execution/_strategies/_tokenize_key.py` (new), `reasons/runtime/_part3.py`, `reasons/_registry.py`, `docs/reasons.md`, `docs/compatibility-contract.md`, `docs/security/key-derivation.md`, `CHANGELOG.md`, `.github/workflows/engine-v2-substrate-matrix.yml` (`fetch-depth: 0`, §5) | S1, B-6 merged |
| S3 | engine | F4, F5 | `_opaque.py`, `execution/_strategies/_tokenize.py`, `docs/security/error-reporting-and-data-exposure.md` | S2, A-4 merged |
| S4a | engine | F1 (fileaccess half), N3 | `fileaccess/_output.py`, `fileaccess/_local.py` (delete `vault_direct`, `publish(durable=)`, steps 4-7), `fileaccess/adapters/serial.py` (`write_bytes(durable=)`: `os.write` loop, `fsync`), `fileaccess/_types.py` (`publish(durable=)`) | A-4 merged |
| S4b | engine | F1 (vault half), gate rev 2 #2 | `vault.py`, `reasons/runtime/_part3.py` (`vault_commit_failed`, `vault_exists`), `reasons/_registry.py`, `docs/reasons.md`, `docs/security/token-vault.md` (R1-R4, operator rule), `docs/compatibility-contract.md`, `CHANGELOG.md` | S4a |
| S5 | engine | F6 (engine), F8 | `docs/handoffs/inputs/b6-engine-api-note.md`, `CHANGELOG.md` | S2, S3 |
| S6a-i | platform, main | F2 (platform), hard cut, pin | `api/migrations/versions/vault_v5_derivation_scope.py` (new), `api/models.py`, `api/vaults/store.py`, `api/jobs/v2_token_vault.py`, `api/engine_a8_guard.py`, `pyproject.toml`, `.github/workflows/backend.yml` | R-b released |
| S6a-ii | platform | F1 (platform), N5, gate rev 2 #1 and #5 | `api/jobs/v2_vault.py` (`_commit_vaults_before_publish`, `VaultCommitError`, error node deleted), `api/jobs/v2_full_frame.py`, `api/jobs/v2_sequential.py`, `api/jobs/route_dispatch_exec.py` | S6a-i |
| S6b | platform | F6 (platform), N1 | `api/vaults/store.py`, `api/jobs/v2_token_vault.py` | S6a-i |
| S7a | CLI, main | F2 (CLI), F1 (tokenize), N4, N2 record | `src/decoy/cli/_token_vault.py`, `src/decoy/cli/tokenize.py`, `pyproject.toml` | R-b released |
| S7b | CLI | F1 (run, plain and chunked), Q4, gate rev 2 #6 | `src/decoy/cli/run.py` | S7a |

Changed-line budget (estimate; the builder records `git diff --stat` and splits if over 800):

| Slice | Prod | Test | Total | Rev 2 total | Why it moved |
|---|---|---|---|---|---|
| S1 | 110 | 140 | 250 | 250 | |
| S2 | 185 | 345 | 530 | 440 | merge-base registry test (about 90 lines), one workflow line |
| S3 | 150 | 230 | 380 | 380 | |
| S4a | 120 | 200 | 320 | 310 | the seven-row fault table replaces two tests |
| S4b | 90 | 270 | 360 | 540 | no stage reclaim, no recovery table: one kill table, one child |
| S5 | 90 | 220 | 310 | 310 | |
| S6a-i | 110 | 220 | 330 | 330 | |
| S6a-ii | 80 | 300 | 380 | 390 | no rollback helper; the derivation and retention sentries are added |
| S6b | 140 | 320 | 460 | 460 | |
| S7a | 110 | 200 | 310 | 430 | no `O_EXCL` ownership, no two-process ownership test, one unlink deleted |
| S7b | 120 | 240 | 360 | 340 | concurrent-refusal test |
| Total | 1305 | 2685 | 3790 | 4180 | |

`rule_codes.py` (CLI) needs no edit: its engine half is derived from the pinned registry
(`rule_codes.py:9-15`, rev 2 read); `tests/unit/test_ci_rule_codes.py` re-derives the set.

## 5. Merge and release order, versions, where each is recorded

Order: S1, S2, S3 on engine main (each its own PR and Codex gate) after B-6 merges; A-4 merges;
then S4a, S4b, S5. R-b is cut as `v0.18.0` from the main carrying A-4 and S1-S5, with
`docs/records/release-rb-facts.md` written like `release-ra-facts.md`. Then S6a-i, S6a-ii, S6b on
the platform (repin `decoy-engine==0.18.0`, `ENGINE_PIN_SHA`), then S7a, S7b on the CLI (repin).
Nothing in S6/S7 can land against 0.17.0: the protocol attribute and the codes do not exist there.

Reason registry (gate rev 2 #3). Rule: a slice that adds identities sets `REGISTRY_VERSION` to its
merge-base's value plus exactly one minor (`X.Y.Z` to `X.(Y+1).0`), and every identity it adds
carries `since=` that value. Neither the tests nor this plan hardcode a version; for the record
only, main is 1.1.0 and A-4 sets 1.2.0.

`tests/unit/test_reason_registry_bump.py::test_registry_is_merge_base_plus_one_minor_and_new_identities_carry_it`
(lands in S2, red first; S4b is its second user):

1. `git rev-parse --is-inside-work-tree`; outside a git checkout `pytest.skip` with the reason
   "not a git checkout: the merge-base registry bump rule is UNCHECKED" (the only skip).
2. `base = git merge-base HEAD origin/main`. Inside a checkout where `origin/main` does not
   resolve the test FAILS with "fetch origin/main (CI checks out with fetch-depth: 0)"; it does
   not skip. `ci.yml:102` already fetches full history for `regression-gate`; S2 adds
   `fetch-depth: 0` to `engine-v2-substrate-matrix.yml` (the only other workflow running the
   unit tree, on a depth-1 checkout, `:51`). No other workflow runs `tests/unit` whole.
3. Base version: `git show <base>:src/decoy_engine/reasons/_registry.py`, parsed with `ast` for the
   `REGISTRY_VERSION` assignment. Base identities: `git archive <base> src/decoy_engine` into
   `tmp_path`, then a subprocess imports that tree first on `sys.path` and prints
   `reasons.manifest()`; identity key `(namespace, code)` (`_registry.py:28`).
4. Asserts: `new = live_keys - base_keys`. If `new` is empty and no base identity changed its
   `since`, `live == base`; otherwise `live == bump_minor(base)` and every identity in `new` has
   `since == live`; no identity of the base changed its `since`.

On main after a merge, `HEAD` is its own merge-base and the test asserts `live == base`. Seeded
red: one identity added with `since="1.2.0"` while `REGISTRY_VERSION` stays `"1.2.0"` on a branch
off A-4 (exactly the gate's case, green on both existing tests). Mutations: bump by two minors;
bump the patch instead; leave one new identity at the base `since`; each red.
`test_live_registry_honours_the_released_pin`, `test_identities_added_after_the_v1_pin_name_their_own_since`
and `test_reasons_doc_states_the_live_registry_version` stay. Rev 2's
`test_no_identity_claims_a_since_newer_than_the_live_registry_version` is dropped: `pin_violations`
already reports it (`reason_release_pin.py:58-60`). `reason-manifest-v1.json` is immutable and not
regenerated; `reason-registry-release.json` is recaptured only at the R-b cut by
`reason_release_pin.py --write`.

`token_vault_derivation_unsupported` is declared under
`_catalog.runtime("decoy_engine.execution._adapter")` and raised by the platform and the CLI; no
engine test requires an engine producer site for a registered code (`test_reason_registry.py:177-199`).

| Surface | Today | After | Recorded in |
|---|---|---|---|
| Engine package | 0.17.0 | 0.18.0 (R-b) | `pyproject.toml`, `CHANGELOG.md`, tag |
| `TokenVaultWriter` | `lookup_existing`, `put` | plus `derivation_scope: bytes` (required; legal pre-GA only, stated) | `docs/compatibility-contract.md` §3.4, CHANGELOG |
| Token derivation | `derive(mask_key, ns, pt)` | `tokenize-key/v1` subkey with scope | `_tokenize_key.py` |
| `SEED_PROTOCOL_VERSION` | 7 | 7 | `_derive.py:136` |
| File vault format | `decoy-vault/v2`, `vault-key/v1` | unchanged bytes; publication path and retention change | `vault.py:82-88`, `docs/security/token-vault.md` |
| Reason registry | merge-base value | plus one minor per adding slice (S2: `tokenize_vault_scope_required`, `token_vault_derivation_unsupported`; S4b: `vault_commit_failed`, `vault_exists`) | `_part3.py`, `_registry.py`, `docs/reasons.md` |
| Platform DB | vault_v4 | `vault_v5_derivation_scope` (`BYTEA NULL`, NULL refused) | `api/migrations/versions/`, `test_vault_v5_migration.py` |
| CLI vault schema | 1 | 2 (1 refused with `token_vault_derivation_unsupported`) | `_token_vault.py::_SCHEMA_VERSION` |
| Platform / CLI engine pin | 0.16.0 / 0.17.0 | 0.18.0 | `pyproject.toml`, `backend.yml` |

## 6. Acceptance tests (red first)

Each test is shown red on its "fails before" base with the seeded violation, then the change is
made, then the mutation is applied and the test shown red, then reverted (never committed with a
mutation applied). Baselines run from the lock in the branch's environment, and the record says so.

| Test | Seeded violation | Fails before | Mutation |
|---|---|---|---|
| `tests/unit/test_vault_publish.py::test_sigkill_after_each_publish_step[ambient\|strict × K1..K7]` | none: the A-4 code; SIGKILL after the wrapped call of each step | A-4 `_local.py:225-231`, `serial.py:45-47`, `_output.py:252-268` (kill points not reached); main `vault.py:305` | restore the in-place open; buffered write; drop the file fsync |
| `::test_an_injected_failure_at_each_step[write, file_fsync, link_eio, link_eexist, dir_fsync, unlink, dir_fsync_after_unlink]` | the named call raises | A-4 `_output.py:71-77`, `_local.py:178-193` (directory fsync swallowed) | swallow the step 5 error; unlink the leaf on step 5 failure |
| `::test_publish_fsyncs_file_then_directory` | strace order | A-4 `serial.py:45-47`, `_output.py:252-268` | drop any fsync; fsync after the link |
| `::test_two_real_writers_one_publishes_one_fails_closed` | two threads, barrier | main `vault.py:305` | publish with `replace` |
| `::test_an_existing_hardlinked_vault_is_refused_and_both_names_are_unchanged` (N3 pin 1) | leaf and twin share one inode with old bytes | A-4 `test_char_vault_in_place.py:23-30` | re-add `vault_direct` |
| `::test_an_existing_vault_in_a_read_only_parent_is_preserved_and_no_stage_remains` (N3 pin 2) | writable vault, read-only parent | A-4 `test_char_vault_in_place.py:33-45` | re-add `vault_direct` |
| platform `tests/test_vault_durability.py::test_file_vault_commit_failure_publishes_nothing` | `write` raises `vault_commit_failed` | `v2_full_frame.py:206` after `:168`; `v2_vault.py:89-100` error node | restore the error node |
| `::test_node_commit_failure_publishes_nothing_and_keeps_the_leaf` | `db.commit` raises after `write` | `v2_vault.py:65-100` has no such commit | publish before the commit |
| `::test_output_failure_keeps_a_reachable_vault[full_frame]` | `write_v2_outputs` raises | `v2_full_frame.py:168` before `:206`; rollback `v2_orchestrator.py:269` | add the node after publication |
| `::test_table_b_failure_keeps_a_vault_that_reidentifies_table_a[sequential, out_of_core]` (gate #1) | table 2 of 2 raises; POST `/unmask` on table A | `v2_sequential.py:319-350` before `route_dispatch_exec.py:246-247`; `router.py:80-95` | unlink the leaf on failure; skip the node commit |
| `::test_vault_is_committed_before_any_output_exists[<derived>]` (N5) | spy at the vault commit | `route_dispatch_exec.py:246-247` | move the relationship commit after the loop |
| `::test_output_failure_after_the_token_commit_keeps_rows_and_a_rerun_adds_none` | publish raises after the commit | n/a (pins retention; red for batch until S6b) | delete rows on failure |
| `tests/test_vault_route_parity_sentry.py::test_every_route_commits_the_vault_before_publish_or_refuses_vault_jobs` (gate #5) | fixture trees: unmapped route, executor without driver, route neither committing nor refusing | n/a on main (new); red on each fixture | drop the driver for `out_of_core` |
| `tests/test_vault_retention_sentry.py::test_no_code_path_removes_a_vault_leaf` | rev 2's unlink-on-failure wrapper | n/a (main has no removal) | add `job_vault_path(job.id).unlink()` in an `except` |
| CLI `tests/e2e/test_run_vault_gating.py::test_table_b_failure_keeps_a_vault_that_reidentifies_table_a` (gate #1) | table B write raises in `_write_mask_outputs` | `run.py:840` before `:854` | unlink the vault on failure |
| `::test_vault_commit_failure_writes_no_outputs` | vault parent unwritable | `run.py:854` after `:840` | reorder back |
| `::test_an_existing_vault_is_refused_before_any_output` (Q4) | pre-existing vault | `run.py:854` overwrites | pre-delete the leaf |
| `::test_a_concurrent_second_run_is_refused_and_never_deletes_the_first_runs_vault` (gate #6) | two processes released together, 20 trials; and run 1 failing on table B then run 2 | `run.py:854` (overwrite) | unlink the leaf on the refused or failed path |
| `::test_chunked_vault_commit_failure_leaves_no_output_and_no_stage` | `--chunked --vault`, vault parent unwritable | `run.py:1455` writes the target directly | publish stages before the vault |
| `::test_chunked_replace_failure_keeps_the_vault` | the second `os.replace` raises | no staging exists | unlink the vault |
| CLI `tests/e2e/test_tokenize_cli.py::test_output_failure_after_commit_keeps_the_vault_and_its_rows` | `_write_mask_outputs` raises after `commit()` | `tokenize.py:383` unlinks a vault judged new at `:326` | re-add the unlink |
| `::test_failed_run_keeps_the_vault_file_and_adds_no_rows` (replaces `test_failed_run_leaves_no_empty_vault`, §D1) | failure before `commit()` | `tokenize.py:383` | re-add the unlink |
| `tests/unit/test_reason_registry_bump.py::test_registry_is_merge_base_plus_one_minor_and_new_identities_carry_it` (gate #3) | new identity at the base `since`, version unbumped | `reason_release_pin.py:48-65` passes it | bump two minors; bump the patch |
| `tests/unit/execution/test_tokenize_domain_separation.py::test_two_vaults_same_key_namespace_plaintext_mint_different_proposals` | two scopes | B-6 `_tokenize.py:253-258` | drop the scope from the label |
| `::test_writer_without_derivation_scope_fails_closed` | no attribute | `_tokenize.py:85-94` | default the scope to `b""` |
| `::test_token_payload_never_equals_hash_truncate_32` | Hypothesis corpus | `_tokenize.py:255` vs `kernel/_scalar.py:88` | `mask_key` as inner IKM |
| `::test_tokenize_subkey_vector` | pinned hex captured at S2 | n/a | change label, prefix or order |
| platform `tests/test_token_vault_adapter.py::test_a_vault_without_a_derivation_scope_is_refused_before_the_engine_runs` | `derivation_scope = NULL` | no column on main | constant scope for NULL |
| platform `tests/test_vault_store.py::test_two_vaults_tokenize_apart_through_the_real_engine` | same plaintext, two vaults | `v2_token_vault.py:158-166` | constant scope |
| CLI `tests/unit/test_token_vault.py::test_v1_vault_is_refused_with_the_derivation_code` | schema-1 file | `_token_vault.py:63` | accept version 1 |
| `::test_derivation_scope_is_32_random_bytes_and_stable_across_reopen` | two creates; reopen | no scope | regenerate on open |
| `tests/unit/execution/test_tokenize_frame_locals.py::test_vault_failure_traceback_holds_no_plaintext_or_key[per_row, batch, lazy_mapping]` | ASCII key canary | B-6 frames (§1) | keep only `mask_key` unboxed |
| `tests/unit/execution/test_tokenize.py::test_lazy_mapping_whose_items_raises_is_sanitized[lookup_many, put_many]` | `items()` raises with plaintext | `_tokenize.py:212` | materialize outside the `try` |
| `::test_hostile_key_and_token_objects_are_rejected_inside_the_boundary` | `str` subclasses with raising hooks | `_tokenize.py:192, 214` | `isinstance` for `type(x) is str` |
| `tests/property/test_tokenize_parity.py::test_batch_and_per_row_emit_identical_tokens` | generated lists | `test_tokenize.py:535-541` | skip NFC on one seam; a hit in `put_many` |
| platform `tests/test_vault_batch_pg.py::test_two_sessions_interleave_lookup_many_and_put_many`, `::test_partial_put_many_after_a_concurrent_commit`, `::test_batch_commit_failure_publishes_nothing` | two sessions | no batch seam on main | return the proposal; skip the re-read; publish before the commit |
| platform `tests/test_vault_concurrency_pg.py::test_write_lock_is_reacquired_after[commit, rollback, savepoint_rollback]` (N1) | B `complete()` under `lock_timeout` | `store.py:88, 160-165` | remove one listener |
| `tests/unit/determinism/test_hkdf.py::test_engine_vectors_are_unchanged_by_the_pyca_swap`, `::test_oneshot_rejects_empty_salt` | captured vectors incl. refusals; naive swap seeded | n/a; red on the seeded swap | map empty salt to zeros; drop a guard |
| `tests/sentry/test_hkdf_is_pyca.py::test_hkdf_module_delegates_to_cryptography` | stdlib import | main `_hkdf.py:26-27` | re-add the stdlib loop |
| `tests/sentry/test_b6_note_currency.py::test_note_signatures_and_claims_match_code` | forbidden phrases | B-6 note `:2, 93-96, 114-115` | re-add "job seed" |

No threshold, slice bound, allowlist or census entry is widened. Pinned tests edited: the two N3
pins (`test_char_vault_in_place.py:23-45`, replaced in S4a) and
`test_tokenize_cli.py::test_failed_run_leaves_no_empty_vault` (replaced in S7a, R1); no other pin
or golden moves.

## 7. Owner decisions applied, and the one open question

Applied as written (§0a): never-delete; D2/Q1 hard cut; Q3 fatal; Q4 refusal. Not reopened.

- Q2 (goldens), still open: no engine golden holds a tokenize output (`grep -rl "tok_"
  tests/fixtures tests/golden` on main is empty); `hash`, `fpe`, generation and vault bytes are
  unchanged; platform and CLI tests assert shape and round trip, not token values. The plan moves
  no golden; confirm, or name the pin to re-record.
- Consequence to acknowledge, not a new decision: under R1 a CLI `run --vault V` that fails after
  the vault commit leaves `V`, so a rerun to `V` is refused (Q4) until the operator chooses a new
  path or removes `V` with its outputs. The message says so (D1 callers table).

## 8. Repeat-class register and stop conditions

- Class 1, frame-local secrets: F4 is the second instance (A-4 round 4 the first); D4 is the shared
  fix. A third instance in any slice gate stops the builder.
- Class 4, route parity: F5 first, N5 second; gate rev 2 #5 (a hand-maintained route list) is the
  second class-4 miss on this plan's design. The route list is now derived (D1). A third class-4
  finding in any slice stops that slice.
- Class 2 and 5, destructive cleanup: gate rev 2 #1 is the first; R1 plus one retention sentry
  per repo is the class fix. A second instance stops the slice.
- Class 7: N1 and N4. N4 is closed by deleting the use (no unlink), not by a stronger check. A
  further class-7 finding in S6 or S7 stops that slice.
- Class 8, harness that cannot reach its asserted states: gate rev 2 #2 and #4. Every crash row
  now names its wrapped call and its return code. A further instance in S4 stops S4b.
- Class 10: N3 (pins), gate rev 2 #3 (registry). A further class-10 finding in S2, S4 or S7 stops
  that slice.

## 9. Measurements and probes (reproducible from the lock)

```bash
uv sync --frozen --all-extras            # engine 1132d29e; cryptography 49.0.0; Python 3.11.18
git worktree add /tmp/wt-b6 origin/feat/b6-batch-vault-writer-engine
cd /tmp/wt-b6 && /path/to/engine/.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/unit/execution/test_tokenize.py tests/unit/determinism tests/unit/test_vault.py
# 132 passed (rev 1)
```

The F3/F7 probe script is unchanged from rev 2 and is not repeated: `git show
f4a2a3b1:docs/plans/2026-11-01-b6-vault-crypto-remediation.md`, §9 (results in §1 above).

Empty-salt probe (rev 2; system Python 3.13.18, `cryptography 50.0.1`, standalone copy of main
`_hkdf.py`; S1 re-runs it from the lock and records the output): PyCA `salt=b""`, `salt=None` and
`salt=b"\x00" * 32` derive equal bytes; main's `hkdf_sha256(salt=b"")` raises `ValueError:
hkdf_extract: salt must be non-empty`.

Not run in rev 3: no test, probe or build. Rev 3 is a read-only revision; the CLI was not re-read
(see the bases table).

## 10. Is this the smallest design that closes the defect, and what I would cut

The defect is F1 (a published output can lose its vault, and a vault can be torn or overwritten)
plus the crypto findings F2-F7. For F1 the design is now close to minimal: one engine publication
sequence that every caller already reaches through `VaultWriter.write`, one ordering rule (vault
before output) and one retention rule that needs no state. Rev 3 removed rev 2's per-caller
NEW/EXISTING split, the rollback helper, the stage reclaim and recovery table, the `O_EXCL`
ownership flag and its race test, and one redundant registry test; the estimate fell from 4180 to
3790 changed lines and the plan from 790 lines to the length of this file.

It is not the smallest possible plan. What I would cut, and what each cut costs:

1. S6b's platform batch seam (`lookup_existing_many`, `put_many`, three PG tests, about 330 of
   its 460 lines). The per-row seam is correct today; the batch seam is a performance feature
   carried here because F6 asks for tests at its boundary. Keep N1 (a real lock bug) as a 130-line
   slice. Cost: the platform stays on the per-row seam until a later item.
2. The CLI chunked staging (half of S7b). Refusing `--chunked --vault` with a registered code
   closes F1 on that route in about 40 lines. Cost: a supported flag combination stops working
   pre-GA; owner call.
3. D1 step 7 (the second directory fsync) and its kill row. Without it a power loss can bring a
   stage name back as a hardlink twin of the leaf, which R4 already declares junk. Kept only
   because the gate's harness spec names seven steps.
4. The D8 note-currency sentry (S5): a doc rewrite without a sentry closes F8 at the cost of
   future drift detection.

Not cuttable without reopening the defect: the stage-fsync-link-fsync sequence, vault before
output on every vault-capable route with the derived route list, the never-delete rule and its
sentries, D2's scope (F2), D4 and D5 (F4, F5), and D7's preserved refusal.

## 11. Plan lint and done-when

No plan-lint tool exists in decoy-engine, decoy-platform or the CLI (rev 2 searched); the
`/home/cam/dev-rules` directory is not present in this environment. Checked by hand: each gate
rev 2 finding maps to a section, a slice and a red-first test with seeded violation and fail-before
location (§0b); owner decisions are applied and not reopened (§0a); version bumps are in §5;
every slice estimate is under 800 (§4).

Done when: this file is on `plan/b6-vault-crypto-remediation`; the platform ROADMAP B-6 row links
it (orchestrator); the Codex plan gate returns GO; S1-S7b follow §5.
