Status: plan, rev 4 (Opus, 2026-11-01). Handoff. Risk class R2 (plan only, no code).

# PLAN: B-6 tokenization vault, crypto remediation (engine, platform, CLI)

Branch `plan/b6-vault-crypto-remediation` off engine `1132d29e` (R-a, 0.17.0). Rev 4 resolves the
Codex gate NO-GO on rev 3 (`77b90136`: 1 BLOCKER carrying a BLOCKER-class ordering issue, 1 HIGH,
5 MEDIUM) and takes the owner's four cuts (§0a). The one design change: the engine REFUSES a vault
writer wherever it cannot hold every output row until the vault is published (a `sink`, or a
chunked stream), instead of ordering a publication it does not own. The plan builds after A-4
merges (§5); nothing here is built; every slice is red first.

Bases re-read for rev 4 (2026-11-01):

| Repo | Ref | Sha | Role |
|---|---|---|---|
| decoy-engine | `origin/main` | `51548303` | current main; every engine line below was re-read here |
| decoy-engine | `origin/claude/jolly-carson-3k5stf` | `16d72eca` | A-4 head (moved from rev 3's `a19889a5`; it merged main `51548303` in `d8f19393`, so its merge-base with main is now `51548303`). `_output.py`, `_local.py`, `vault.py:269-351`, `serial.py` and `test_char_vault_in_place.py` are identical between the two heads; every A-4 line below was re-read at `16d72eca` |
| decoy-engine | `origin/feat/b6-batch-vault-writer-engine` | `e3c539d6` | B-6 batch writer under review; unchanged since rev 2 |
| decoy-platform | `origin/main` | `8a581fe2` | `git diff --stat 7fc4612d 8a581fe2 -- api/vaults api/jobs api/unmask api/crypto tests/test_vault_durability.py tests/test_vault_concurrency_pg.py` is empty; every platform line below was re-read at `8a581fe2` |
| louiskeep/decoy (CLI) | `origin/main` | `a5d36b4` | re-read in this session (read-only shallow clone); every CLI line below is at this sha |

Measurements: rev 1's were taken from the engine lock (`uv sync --frozen --all-extras`, Python
3.11.18, `cryptography 49.0.0`) and stand in §9; rev 2's empty-salt probe ran on the system
interpreter and is labelled. Rev 4 made no new measurement; every rev 4 claim is VERIFIED-BY-READ
at the shas above, and every slice names the test that proves it in CI.

## 0a. Owner decisions applied (delegated, not reopened)

- Rev 3, the retention rule: a committed vault is NEVER deleted by a failure path. Only an
  unpublished stage is cleaned. A vault that exists is the recovery key for any output that may
  have been published; a leftover vault with no output is harmless.
- Rev 4, the boundary rule (gate #1): the engine accepts a vault writer only where it holds every
  output row until the caller can publish the vault first. `run_pipeline` with a `sink` and a
  `vault_writer`, and the chunked entries with any `vault_writer`, are refused before execution
  with one registered code. No V1 caller needs either combination (§1), so nothing is integrated
  into the sink transaction.
- Rev 4, the cuts (gate #8): S6b keeps only N1 (no platform batch tokenization seam); the CLI
  refuses `--chunked --vault` instead of staging chunked outputs (a flag combination stops working,
  flagged in §7); the second directory fsync (rev 3 step 7) and the note-currency sentry are
  dropped. No finding in §0b needs either (§10).
- D2 / Q1: HARD CUT. A vault minted under the old derivation is refused with
  `token_vault_derivation_unsupported` before the engine runs, for tokenize AND detokenize
  (gate #5). No marker, no dual derivation, no migration of data.
- Q4: the CLI refuses to overwrite an existing vault (`vault_exists`, registered, EXIT_USAGE), by
  the engine's exclusive publication; not an ownership claim, licenses no deletion. Q3: the
  platform's non-fatal file-vault write becomes fatal (D1 "Platform").

## 0b. Gate findings on rev 3 (`77b90136`) and where each is resolved

| # | Sev | Class (fixed as a class) | Resolved in | Slice | Red-first test; seeded violation; fails before |
|---|---|---|---|---|---|
| 1 | BLOCKER | 5 (authoritative output whose vault does not exist yet) and 8 (no test at either public boundary). Class fix: one boundary rule B0 at every public entry that takes a vault writer, plus a control that pins the engine's own lane | D1 B0 | S2b, S7b | `tests/unit/execution/test_vault_boundary.py::test_run_pipeline_refuses_a_sink_with_a_vault_writer_before_reading_a_source`; seeded: none needed, main accepts the pair; fails before at `_pipeline_full_frame.py:245` (`publish.commit()` inside the engine) with the vault written only afterwards by the caller, the order `test_b6a_publish.py:436-443` observes. `::test_chunked_entries_refuse_a_vault_writer_before_the_first_chunk[run_mask_chunked, run_mask_pipeline_chunked]`; fails before at `_chunked.py:405-407` ("the caller writes the artifact after the stream drains") and `native/_chunked_entry.py:401-405` (accumulate, then `yield`). Control `::test_the_auto_chunk_lane_still_collects_vault_entries_resident`. CLI `tests/e2e/test_run_chunked.py::test_chunked_with_vault_is_refused_before_any_output`; fails before at `run.py:793-794` |
| 2 | BLOCKER-class | 10 (two branches claiming one registry version) and 3 (an order that cannot be executed) | §5: A-4 lands first; every identity-adding slice takes the next minor over main at its build time; no version literal in this plan | S2, S2b, S4b | `tests/unit/test_reason_registry_bump.py::test_registry_is_merge_base_plus_one_minor_and_new_identities_carry_it` (S2); seeded: a new identity whose `since` equals the base version with `REGISTRY_VERSION` unbumped; fails before at `scripts/release/reason_release_pin.py:48-65` (a `since` equal to an unreleased live version passes) |
| 3 | HIGH | 1 (vault bytes readable by another local user; under the no-secret fallback the Fernet key derives from the public job seed, `vault.py:143-160`, so a readable file is a decrypted vault) and 7 (a mode fixed after creation is a window) | D1 "Mode" | S4a | `tests/unit/test_vault_publish.py::test_vault_is_0600_under_a_permissive_umask[ambient, strict]`; seeded: `os.umask(0)`; fails before at A-4 `_output.py:222` (`0o666` unless `quarantine`) and `_local.py:231, 246` (`0o666`) |
| 4 | MEDIUM | 8 (a test and a message that contradict the design) | D1 "CLI tokenize" | S7a | `tests/e2e/test_tokenize_cli.py::test_unresolvable_secret_gives_a_coded_error_and_keeps_an_empty_vault`, `::test_failed_run_keeps_the_vault_file_and_adds_no_rows`, `::test_output_failure_after_commit_keeps_the_vault_and_its_rows`; seeded: the output write raises after `commit()`; fails before at `tokenize.py:368-372` (message claims "holds no mappings") and `:383-384` (unlink) |
| 5 | MEDIUM | 4 (a guard on one branch of a shared function: `_ensure_schema` serves create and read, `_token_vault.py:148-211`) | D2 "Hard cut, CLI" | S7a | `tests/unit/test_token_vault.py::test_v1_vault_is_refused_with_the_derivation_code[tokenize, detokenize]`; seeded: a schema-1 file; fails before at `_token_vault.py:63` (version 1 accepted) and, had rev 3 been built, at `:167-170` (`INSERT OR IGNORE` leaves an old file at version 1 and the create path mints into it) |
| 6 | MEDIUM | 8 (a claimed class fix that one repo and one helper call defeat) | D1 "Retention sentries" | S4b, S6a-ii, S7a | engine `tests/sentry/test_vault_retention_sentry.py`, CLI `tests/unit/test_vault_retention_sentry.py` (red on CLI main: `tokenize.py:384` is the seed), platform `tests/test_vault_retention_sentry.py` over fixture trees including `helper_indirection.py` |
| 7 | MEDIUM | 2 (exit paths, the directory open and the descriptor closes, missing from the fault table) | D1 steps and fault table | S4a | `::test_an_injected_failure_at_each_step[dir_open, write, file_fsync, stage_close, link_eio, link_eexist, dir_fsync, unlink, dir_close]`; fails before at A-4 `_local.py:178-193` (`_sync_dir` opens the parent after the rename and swallows every error) |
| 8 | MEDIUM | owner delegation, taken in full | §0a, §4, §10 | S6b, S7b, S4a, S5 | the deleted mechanisms have no tests; the refusal replacing chunked staging has the gate #1 CLI test |

## 0c. Earlier gate findings: still resolved

Rev 2 (`f4a2a3b1`): #1 inverse partial commit and destructive cleanup (D1 R1-R4, callers table);
#2 unreachable crash states (D1 harness); #3 registry bump (§5); #4 N3 pins; #5 route parity (D1
derived list); #6 ownership used to delete (D1 Q4, N4). Rev 1 (`27032a1b`): 1-2 (D1), 3 (§5), 4
(D6 N1), 5 (D1 N3), 6 (D7), 7 (§4), 8 (D4). The platform /unmask correction (the `target.vault`
node committed with the vault before the first publish, else `v2_orchestrator.py:269` rolls it back
and `router.py:79-96` refuses) was verified by the rev 3 gate and stands.

## 0. Findings register

Classes: (1) value-bearing output; (2) cleanup missing on an exit path; (3) state machine and
concurrency; (4) route parity and silent fallback; (5) referential integrity; (6) untrusted
producer; (7) check then use; (8) weak tests and overclaimed evidence; (9) behavior-preserving
refactor drift; (10) frozen contract drift.

| Id | Sev | Class | Where (re-read) | Design | Slice |
|---|---|---|---|---|---|
| F1 | HIGH | 3, 5 (+2 crash window, +10 pinned partner) | main `vault.py:305`; A-4 `vault.py:334-350`, `_local.py:225-231, 246`, `_output.py:71-77, 222, 246-275`, `serial.py:45-47`; engine boundary `_pipeline_full_frame.py:245`, `_chunked.py:405-407`; platform `v2_full_frame.py:165-206`, `v2_vault.py:65-100`, `route_dispatch_exec.py:244-247`, `v2_sequential.py:320-350, 449-458`; CLI `run.py:840, 854, 793-794, 1443-1448`, `tokenize.py:341, 348, 383-384` | D1 | S2b, S4a, S4b, S6a-ii, S7a, S7b |
| F2 | HIGH | 1 | B-6 `_tokenize.py:253-258`; `_adapter.py:98-157`; platform `v2_token_vault.py:158-166`; CLI `_token_vault.py:215-254` | D2 | S2, S6a-i, S7a |
| F3 | HIGH | 1 | B-6 `_tokenize.py:255` vs `kernel/_scalar.py:88` | D3 | S2 |
| F4 | MEDIUM | 1 (second appearance, §8) | B-6 `_tokenize.py:147-155, 181, 233-258` | D4 | S3 |
| F5 | MEDIUM | 4 (guard placement) | B-6 `_tokenize.py:169-186, 192, 212` | D5 | S3 |
| F6 | MEDIUM | 8 | B-6 `test_tokenize.py:535-541, 565-579` | D6 | S5 |
| F7 | MEDIUM | 9 | `determinism/_hkdf.py:1-95` (`:54-59` empty-salt refusal) | D7 | S1 |
| F8 | LOW | 8 | B-6 `docs/handoffs/inputs/b6-engine-api-note.md:2, 93-96, 114-115` | D8 | S5 |
| N1 | HIGH | 7 | platform `api/vaults/store.py:88, 160-165` (`_write_locked` never cleared) | D6 | S6b |
| N2 | closed upstream | 10 | CLI `57e8b81` (`put -> str`, pin 0.17.0) | D2 | S7a (repin only) |
| N3 | MEDIUM | 10 | A-4 `tests/fileaccess/test_char_vault_in_place.py:23-30, 33-45` pin the in-place rewrite F1 removes | D1 N3 | S4a |
| N4 | MEDIUM | 7 | CLI `tokenize.py:326` (`exists()`) then `:383-384` (unlink); the unlink is deleted, so the check has no use and is deleted | D1 Q4, N4 | S7a |
| N5 | HIGH | 4 | platform relationship routes publish per table (`v2_sequential.py:320-350`) before the vault persist (`route_dispatch_exec.py:246-247`) | D1 route parity | S6a-ii |

## 1. What was verified, and how

| Claim | Result | Where it fails before the fix |
|---|---|---|
| F3: `hash` `truncate: 32` equals the token payload for one `(mask_key, namespace, NFC(pt))` | True (probe, lock env, §9) | `_tokenize.py:255`, `kernel/_scalar.py:88` |
| F2: the proposal is a pure function of `(mask_key, namespace, plaintext)` | True (probe and by construction) | `_adapter.py:98-157` has no vault input |
| F4: frames on the `StrategyError` traceback holding plaintext or key | `run`: `df, ctx, col, normalized, distinct`; `_resolve_tokens`: `distinct, mask_key, plaintext, minted`; `_vault_call`: `args` (probe) | listed lines |
| F5: a batch result whose `items()` raises | `RuntimeError` escapes `_vault_call`, quoting the plaintext (probe) | `_tokenize.py:212` |
| F7: PyCA `HKDF`/`HKDFExpand` equal `_hkdf` byte for byte; PyCA `salt=b""` equals the all-zero salt; main's one-shot refuses `b""` | True; True; raises (probe; the empty-salt part on system Python 3.13.18, `cryptography 50.0.1`) | a naive swap accepts `b""` |
| F1 engine write paths | main `vault.py:305` writes the leaf directly; A-4 ambient opens the leaf `O_TRUNC` in place (`_local.py:34, 225-231`); A-4 strict stages and links (`_output.py:252-268`) through a buffered handle with no `fsync` (`serial.py:45-47`), swallows the directory fsync (`_output.py:71-77`, `_local.py:178-193`) and the stage unlink (`_output.py:266-269`); stages are created `0o666` under the umask (`_output.py:222`, `_local.py:231, 246`) | by read |
| F1 engine boundary (gate #1) | `run_pipeline(sink=...)` commits the sink inside the engine (`_pipeline_full_frame.py:114-115, 245`; `OutputPublish.commit`, `_chunked_output_sink.py:157-161`) while `vault_writer` only accumulates (`_pipeline_context.py:325-327` keys it, nothing writes it); `test_b6a_publish.py:423-445` passes both and writes the vault after the commit. The chunked entries (`_chunked.py:369-452`, `native/_chunked_entry.py:586-662`) yield chunks after `vault_writer.add` (`_chunked_entry.py:401-405`) | by read |
| No V1 caller passes `sink` with `vault_writer` to `run_pipeline` | platform `v2_runner.py:226-248` (no `sink`); CLI `run.py:821-830` (no `sink`); CLI library `api.py:505-512` (neither). The platform's `sink=ParquetTransactionalSink(stage_dir)` calls go to `run_sequential` (`v2_sequential.py:201-209`) and `run_fk_out_of_core` (`v2_out_of_core.py:327-332`), which take no vault writer; the platform collects vault entries from its own stage afterwards (`v2_sequential.py:408-414`) | by read |
| The CLI `--chunked --vault` route is the only V1 caller of a chunked entry with a vault writer | CLI `run.py:787-794` builds the writer and passes it to `_run_chunked_mask` (`:1398-1403`), which passes it to `run_mask_pipeline_chunked` (`:1443-1448`) and writes each table directly (`:1455-1467`). The platform streaming route passes no writer: `_run_v2_pipeline_streaming` defaults it to `None` (`v2_runner.py:251-258`), its one caller (`v2_stream_coordinator.py:184`) does not set it, and streaming eligibility refuses vault columns (`_phase1_eligibility.py:181-183`). Engine tests that pass a writer to a chunked entry (ten sites): `tests/native/_b8_support.py:65`, `_chunked_date_shift_support.py:146`, `test_chunked_entry_parity_matrix.py:418`, `test_chunked_entry_side_channels.py:132, 360-367, 430-432, 453`, `test_unconfigured_passthrough.py:149`, `tests/unit/execution/_auto_chunk_support.py:177`, `test_auto_chunk_units.py:442`, `tests/unit/test_de02_keyprovider.py:897, 921`, `tests/unit/test_vault.py:203`; the engine's own lane does too (`_pipeline_auto_chunk.py:237-246, 274-282, 329-337`) | by read |
| F1 callers publish before the vault commits | platform full frame `v2_full_frame.py:168` before `:206`; relationship `v2_sequential.py:336` (inside the `:320-350` loop) before `route_dispatch_exec.py:247`; CLI `run.py:840` before `:854`; CLI tokenize `:341` before `:348` | by read |
| A failed platform job loses an uncommitted `target.vault` node | `v2_orchestrator.py:269` `db.rollback()`; `/unmask` requires a `status=="ok"` node (`router.py:79-96`) and the file (`:97-108`) | by read |
| Platform route set is closed | `dispatch_route.py:55` `ALL_ROUTES`; `:121` refuses any other planned route; dispatch arms `v2_orchestrator.py:253-258` | by read |
| No platform code removes a vault file today | `git grep "job_vault_path\|vault_dir()\|dcyvault" 8a581fe2 -- api` finds the writer (`v2_vault.py:43-100`), the reader (`unmask/router.py:97-99`), quota accounting (`disk_quota_matrix.py:139`) and no unlink | by read |
| Registry | main and A-4 differ by exactly one minor (`_registry.py:24` on each); identity key `(namespace, code)` (`_registry.py:28`) | by read |
| Engine CI | `ci.yml:102` `regression-gate` has `fetch-depth: 0`; `engine-v2-substrate-matrix.yml:51` runs `pytest tests` on a depth-1 checkout; CI runs `ubuntu-latest` without a container (`ci.yml:29, 51, 90`) | by read |

Unverifiable here: PostgreSQL behavior (S6 tests run in the `backend-postgres` leg on `postgres:15`,
marker `pg_semantic`, which fails the leg on skip) and power-loss durability of `fsync` (a kernel
property; the tests prove call order with strace and crash consistency with SIGKILL).

## 2. Established methodology (surveyed; nothing is invented)

- Purpose separation of derived keys: RFC 5869 §3.2 (`info` binds a key to its context), NIST SP
  800-108r1 §4 (`Label`, `Context`). The engine already does this (`vault-key/v1`, `vault.py:88`;
  `ff1-key/v1`, `_derive.py:126`); D2 adds `tokenize-key/v1` through the same `derive` envelope.
- KDF implementation: PyCA `HKDF`, `HKDFExpand`, `HMAC` (RFC 5869); the platform already uses PyCA
  `HKDF` (`api/crypto/vault_key.py:187`). The engine's empty-salt refusal is an application rule
  over the primitive (QA-10 F12), kept on both entry points (D7).
- Crash-atomic publication: stage in the same directory, `fsync` the file, `link` into the leaf,
  `fsync` the directory (POSIX; SQLite's atomic-commit notes, PostgreSQL `durable_rename`). The
  leaf is the only authoritative name; exclusive `link(2)` is the compare-and-set (A-4's
  `exclusive` mode). Private artifacts take their final mode as the `open(2)` mode argument,
  never a later chmod (`mkstemp` creates 0600); a hardlink shares the inode's mode.
- Retention: a write-ahead artifact is kept until what depends on it is gone, never removed on the
  failure of a later step (PostgreSQL keeps WAL a replica may need). Fail-closed boundary: a
  library that cannot order two publications it does not own refuses the combination at
  admission (SQLite refuses `BEGIN` inside a transaction rather than guessing).
- Crash tests: a child interpreter killed with `SIGKILL` at a numbered point, the state asserted
  by the parent (SQLite `crashtest`); `SIGKILL` runs no `finally`, `__del__` or
  `weakref.finalize`, which a monkeypatched `raise` cannot model.
- Lock proofs: the blocked session runs under `lock_timeout` and expects SQLSTATE `55P03`.
- Secret-free frames: A-4's `OpaqueBox` (`_opaque.py`, `secret_providers/_preflight.py:165-198`).

## 3. Designs

### D1. Durable, exclusive, private vault publication; vault before output; never delete (F1, N3, N4, N5)

Boundary rule B0 (gate #1), one rule at every public entry that takes a vault writer:

- `run_pipeline(sink=S, vault_writer=W)` with both non-`None` raises
  `ExecutionError(code="vault_requires_resident_output")` from `build_run_context`, beside the
  knob checks (`_pipeline_context.py:246-250`), before any source is profiled or read: with a
  sink the engine commits authoritative output inside the run (`_pipeline_full_frame.py:245`) and
  the caller can only write the vault afterwards, with no engine seam to fix the order.
- `run_mask_chunked` (`native/_chunked_entry.py:586`) and `run_mask_pipeline_chunked`
  (`_chunked.py:369`) raise the same code as their first statement when `vault_writer` is not
  `None`: a generator cannot withhold the chunks it yields. Their bodies move unchanged to
  `_chunked_entry._run_mask_chunked` (`:643-662`, progress counting included) and
  `_chunked._mask_pipeline_chunked` (`:423-452`), which the engine's auto-chunk lane calls instead
  (`_pipeline_auto_chunk.py:237, 274, 329`); that lane returns resident output or streams into a
  sink B0 has already refused, so it needs no second check. The ten engine test sites in §1
  switch their import to the internal name, assertions untouched. `_chunked.py:405-407` is
  deleted; the three docstrings state B0.
- Retention rule R1-R4 (rev 3, unchanged): R1 once `link(stage, leaf)` has returned, no failure
  path in engine, platform or CLI removes, truncates or replaces the leaf. R2 a failure path
  removes only names it created and never published (the engine's `_decoy_vault_stage_*`). R3
  every caller commits the vault before its first output byte is published; a commit failure
  publishes nothing; on the platform "commits" includes the `target.vault` node. R4 a vault left
  by a failed job is kept; `docs/security/token-vault.md` "Operational rules" (`:78-99`) gains the
  operator rule: delete a vault only with its outputs; a stage file beside a vault with no running
  job may be deleted, it is never a vault.

Tests for B0, `tests/unit/execution/test_vault_boundary.py` (S2b): the sink test uses a
`RecordingSink` (`_b6a_support.py:45`), a `VaultWriter` and a `LazySource` whose load raises; the
call raises the code, the sink recorded no call, the source was never loaded, `tmp_path` is empty.
The chunked test passes a `chunks` iterable that raises if advanced; the call raises before
returning an iterator. The control, `run_pipeline(auto_chunk=True, auto_chunk_threshold_rows=1,
vault_writer=W)` on a vault column, gets resident output and one entry per row in `W`. Pinned tests edited:
`test_b6a_publish.py::test_streamed_vault_entries_and_file_equal_the_resident_run` (`:422-445`)
is deleted, it pins the inverse order; `::test_a_vault_writer_keyed_differently_is_rejected_before_anything_is_staged`
(`:448-465`) drops its sink. Mutations: drop either check; make the lane call a public name.

Engine publication (`vault.py`, A-4 `fileaccess`, after the A-4 merge). `VaultWriter.write(path
| OutputDir)` always publishes through a stage beside the leaf in `exclusive` mode. The ambient
path-string route stops using `vault_direct`: `_local.py:225-231` and the label (`_types.py:57`)
are deleted (hard delete). The steps, in both `RootedOutputTxn` (`_output.py:246`) and
`LocalOutputTxn` (`_local.py:260`):

1. Open the parent directory (`O_RDONLY | O_DIRECTORY | O_CLOEXEC`; `RootedOutputTxn` already
   holds it as `state.parent_fd`, `LocalOutputTxn` opens it here), then
   `os.open(_decoy_vault_stage_<hex>, O_CREAT | O_EXCL | O_WRONLY, 0o600, dir_fd=parent)`. A
   failure here created nothing and keeps the A-4 shape (`FileNotFoundError`/`PermissionError`
   naming the path under the ambient policy, as `test_char_vault_in_place.py:48-54` pins;
   `FileAccessUnavailable` under strict). Opening the directory first means step 5 can never fail
   for want of a descriptor after the leaf exists.
2. `os.write` in a loop until every byte is written (`serial.write_bytes(..., durable=True)`, a
   raw-descriptor loop replacing the buffered `fh.write` of `serial.py:45-47` for this mode only).
3. `os.fsync(stage fd)`, then `os.close(stage fd)`.
4. `os.link(stage, leaf, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)`: the
   compare-and-set. `EEXIST`: the stage is unlinked (R2) and `write` raises
   `VaultError(code="vault_exists")`.
5. `os.fsync(parent fd)`. This is the commit point. A failure raises `vault_commit_failed`; the
   stage is unlinked (R2); the leaf is kept (R1).
6. `os.unlink(stage, dir_fd=parent)`, then `os.close(parent fd)` (`LocalOutputTxn` only; the
   rooted transaction closes its own as today, `_output.py:271-274`).

`write` returns the entry count after step 6. Every `OSError` from steps 2-5 (`write`,
`fsync(stage)`, `close(stage)`, `link`, `fsync(parent)`) becomes `vault_commit_failed` with no
path and no errno text, except `EEXIST` from `link`, which is `vault_exists`. Step 6 tidies a name
after the commit: an error there (the unlink or the parent close) is not raised and records
nothing; it leaves the K5 or K6 residue, which R4's operator rule covers. Existing `publish()` and
`write_bytes()` callers are unchanged (`durable` defaults to `False`).

Mode (gate #3). The stage is created with mode `0o600` as the `os.open` creation argument, never
by a later chmod: `STAGE_MODE: dict[str, int] = {"quarantine": 0o600, "vault": 0o600}` with
default `0o666` replaces the conditional at `_output.py:222` and is used at `_local.py:246`
(`:241`'s `mkstemp` already creates 0600). The leaf is a hardlink of the stage, so it carries the
stage's mode whatever the umask; a K1-K6 residue is 0600 too. Under the no-secret fallback the
Fernet key derives from the public job seed (`vault.py:143-160`), so a 0644 vault under `umask
022` is a decrypted vault for any local user. The platform's vault directory is created `0o700`
(`v2_vault.py:86` gains `mode=0o700`). `test_vault_is_0600_under_a_permissive_umask[ambient,
strict]`: `os.umask(0)` restored in a `finally`; after `write`, `stat.S_IMODE(leaf.stat().st_mode)
== 0o600`; the K1 row asserts the same on the stage residue. Red first on A-4. Mutation: `0o666`.

Crash harness (gate rev 2 #2). `tests/unit/vault_publish_child.py <route> <K> <dir>` replaces
`os.open`, `os.write`, `os.fsync`, `os.link` and `os.unlink` on the `os` module before importing
the engine (the `fileaccess` modules call them as `os.<name>` at call time: `_local.py:231, 284,
296`; `_output.py:256` passes `os.link` through `_call`); each wrapper calls the real function and,
when its predicate matches, sends itself `SIGKILL` after the call returned. No production seam is
added. `route` is `ambient` (path string) or `strict` (`OutputDir`).

| K | Step | Wrapped call, killed after it returns | Predicate | Leaf after | Stage after | Child rc | Parent's second `write` |
|---|---|---|---|---|---|---|---|
| K1 | 1 | `os.open` | basename starts `_decoy_vault_stage_` | absent | present, 0 bytes, mode 0600 | -9 | publishes |
| K2 | 2 | `os.write` | fd is the K1 descriptor; first call | absent | present, exactly the byte count that call returned | -9 | publishes |
| K3 | 3 | `os.fsync` | fd is the K1 descriptor | absent | present, every byte | -9 | publishes |
| K4 | 4 | `os.link` | destination basename is the leaf | present, loads | present, same inode, `st_nlink == 2` | -9 | `vault_exists` |
| K5 | 5 | `os.fsync` | `S_ISDIR(fstat(fd))`, first call after K4's | present, loads | present, same inode | -9 | `vault_exists` |
| K6 | 6 | `os.unlink` | basename starts `_decoy_vault_stage_` | present, loads, `st_nlink == 1` | absent | -9 | `vault_exists` |
| K7 | none: step 6 completes and the child exits | none | none | present, loads | absent | 0 | `vault_exists` |

`test_sigkill_after_each_publish_step[ambient|strict × K1..K7]` asserts the row exactly: the
return code first (a K1-K6 child that exits 0 fails with "kill point K<n> not reached"; a K7
child that does not exit 0 fails), then the leaf (`load_vault` returns the expected entries when
present), the stage (size, mode, inode, link count), then the parent's second `write` to the
same leaf, after which any residue the kill left is byte-identical and the parent left no stage
of its own. Red first on A-4 `16d72eca`: ambient K1-K6 are not reached (no stage,
`_local.py:231`); strict K2, K3 (no `os.write`, no file `fsync`, `serial.py:45-47`) and K5
(`_output.py:252-268`) are not reached. Main `vault.py:305` behaves as ambient.

Fault injection (gate #7; not a crash test, labelled so): `test_an_injected_failure_at_each_step[...]`
patches the named call to raise and asserts one row each, on both routes; no message holds the
path or errno text (asserted on every raising row).

| Case | Patched call raises | Error | Leaf | Stage |
|---|---|---|---|---|
| `dir_open` | `os.open` of the parent (step 1, ambient only; the rooted transaction holds its fd) | A-4 shape: `PermissionError` naming the path | unchanged | none created |
| `write` | `os.write` | `vault_commit_failed` | absent | none |
| `file_fsync` | `os.fsync(stage)` | `vault_commit_failed` | absent | none |
| `stage_close` | `os.close(stage)` | `vault_commit_failed` | absent | none |
| `link_eio` | `os.link` with `EIO` | `vault_commit_failed` | absent | none |
| `link_eexist` | `os.link` with `EEXIST` | `vault_exists` | pre-existing bytes, same inode | none |
| `dir_fsync` | `os.fsync(parent)` | `vault_commit_failed` | kept, loads (R1) | none |
| `unlink` | `os.unlink(stage)` | none; `write` returns the count | loads | the K5-shaped twin remains |
| `dir_close` | `os.close(parent)` | none; `write` returns the count | loads | none |

Red first on A-4: `dir_fsync` returns success (`_output.py:71-77`, `_local.py:178-193`);
`dir_open` is swallowed (`_local.py:181-183`). Order: `test_publish_fsyncs_file_then_directory`
runs `tests/fileaccess/strace_support.py` (`-f -yy`) over a child and asserts `fsync(stage)` before
`linkat` and `fsync(parent)` between `linkat` and `unlinkat`; the helper fails, never skips, without
`strace`. Two writers: `test_two_real_writers_one_publishes_one_fails_closed` (two threads, a
`Barrier` after both staged; one leaf, the loser `vault_exists`, no stage residue).

N3 (gate rev 2 #4). Both pins of `test_char_vault_in_place.py` assert the in-place rewrite and are
replaced in S4a, the commit that deletes `vault_direct`: pin 1 (`:23-30`) becomes
`test_an_existing_hardlinked_vault_is_refused_and_both_names_are_unchanged` (leaf and twin share
one inode with old bytes; `write(leaf)` raises `vault_exists`; both names keep `st_ino`,
`st_nlink == 2` and the bytes; no stage remains); pin 2 (`:33-45`) becomes
`test_an_existing_vault_in_a_read_only_parent_is_preserved_and_no_stage_remains` (step 1 fails
with `PermissionError` naming the path; bytes and listing unchanged; skipped only as root).

Callers (one retention rule; no per-caller split):

| Caller (read) | Vault | Order after this plan | Output failure after the vault commit | Test |
|---|---|---|---|---|
| platform full frame, `v2_full_frame.py:165-172, 205-206` | file vault `job_<id>.dcyvault`, token rows | after the last cancel poll: `vault_writer.write(job_vault_path)`; add the `status="ok"` `target.vault` node; one `db.commit()` that also commits the token rows (`_commit_vaults_before_publish` in `v2_vault.py` replaces `_commit_token_vault_before_publish`, `v2_full_frame.py:47-67`); then `write_v2_outputs` | job failed; vault, node and rows kept; `/unmask` works for every published table | `tests/test_vault_durability.py::test_output_failure_keeps_a_reachable_vault[full_frame]` |
| platform relationship routes (N5), `v2_sequential.py:320-350, 449-458`; `route_dispatch_exec.py:244-247` | file vault | `_commit_vaults_before_publish` runs in `_run_relationship_job_to_targets` immediately before `_materialize_staged_sequential_outputs` (`:450`); the late persist at `route_dispatch_exec.py:246-247` is deleted | tables already published stay; vault and node kept; job failed | `::test_table_b_failure_keeps_a_vault_that_reidentifies_table_a[sequential, out_of_core]` (POST `/unmask` on table A returns the source values) |
| platform token rows | Postgres | committed in the same `db.commit()` | rows kept; a rerun emits the same tokens and adds no row | `::test_output_failure_after_the_token_commit_keeps_rows_and_a_rerun_adds_none` |
| CLI `run.py` plain route, `:840, :854`, loop `:1572-1589` | file vault | vault (exclusive: an existing leaf is `vault_exists`, EXIT_USAGE, nothing written), then `_write_mask_outputs` | tables already written stay; the vault stays; EXIT_RUNTIME; the message says the vault was kept and that a rerun needs a new `--vault` path or the old vault removed with its outputs | `tests/e2e/test_run_vault_gating.py::test_table_b_failure_keeps_a_vault_that_reidentifies_table_a`, `::test_vault_commit_failure_writes_no_outputs`, `::test_an_existing_vault_is_refused_before_any_output` |
| CLI `run.py` chunked route, `:787-794`, `_run_chunked_mask:1398-1403, 1443-1448` | none: refused | `--chunked` with `--vault` raises `_ChunkedVaultError` (EXIT_USAGE, the `_ChunkedReportFlagError` shape at `:135-143, 710, 952-955`) in the usage block at `:739-749`, before key resolution (`:787-791`) and before any output; `_run_chunked_mask` loses its `vault_writer` parameter | n/a | `tests/e2e/test_run_chunked.py::test_chunked_with_vault_is_refused_before_any_output` (no output file, no vault, the message names both flags); `tests/e2e/test_vault_cli.py::test_chunked_run_writes_equivalent_vault` (`:93-119`) is deleted, it pins the inverse order on this route |
| CLI `tokenize.py`, `:324-349, :364-384` | SQLite token vault | `commit()`, then `_write_mask_outputs` | the file is never removed by a failure path; rows kept; a rerun reuses them | `tests/e2e/test_tokenize_cli.py::test_output_failure_after_commit_keeps_the_vault_and_its_rows` |

CLI tokenize (gate #4). `vault_preexisted` (`:324-326`), the `finally` unlink (`:377-384`) and the
"leaves no partial vault" comment (`:344-347`) are deleted. The catch-all (`:364-373`) reports by
`committed`: not committed, "tokenize failed with an unexpected <Type>; the vault file was kept
and holds no mappings from this run (its schema only); rerun with the same --vault"; committed,
"tokenize failed with an unexpected <Type> after the vault was committed; the vault holds this
run's mappings, so any output this run wrote stays reversible; rerun with the same --vault to
reuse them and write the outputs again". Neither names a value. Tests:
`test_unresolvable_secret_gives_a_coded_error` (`:319-338`) becomes
`..._and_keeps_an_empty_vault` (EXIT_USAGE, the file exists with zero rows);
`test_failed_run_leaves_no_empty_vault` (`:356-371`) becomes
`test_failed_run_keeps_the_vault_file_and_adds_no_rows` (EXIT_RUNTIME, file kept, zero rows,
"holds no mappings from this run"); new `test_output_failure_after_commit_keeps_the_vault_and_its_rows`
makes `_write_mask_outputs` raise after `commit()` (EXIT_RUNTIME, "after the vault was committed",
row count equals the distinct plaintext count, a rerun exits 0 and adds no row);
`test_failed_run_keeps_a_preexisting_vault` (`:373-395`) stays unchanged.

Platform: fatal. `_persist_job_vault` (`v2_vault.py:65-100`) stops catching: its error node, the
`str(exc)` in it and the "does NOT fail the job" docstring are deleted (hard delete).
`_commit_vaults_before_publish` raises `VaultCommitError` (new, `v2_vault.py`, carrying the engine
code `vault_commit_failed` or `vault_exists`), which the runner's terminal handler
(`runner.py:128-159`) lands as a failed job whose manifest names the code and no path.
`test_file_vault_commit_failure_publishes_nothing`: `write` raises `vault_commit_failed`; the job
fails; `write_v2_outputs` is never called. If the engine `write` succeeded and the `db.commit()`
then fails, no output is published and the leaf without a node is an R4 leftover that `/unmask`
refuses; `::test_node_commit_failure_publishes_nothing_and_keeps_the_leaf` pins it.

Route parity (N5, gate rev 2 #5). The vault-capable route list is derived, not maintained:
`tests/_route_vault_derivation.py::derive()` returns `{route: "commits" | "refuses"}` from (1) the
closed set `api.jobs.dispatch_route.ALL_ROUTES` (`dispatch_route.py:55`; `:121` refuses any other
planned route); (2) AST of `route_dispatch_exec.py`: each route has `execute_<r>` (missing: fail)
and every `execute_*`/`dispatch_*` call in the orchestrator's dispatch block
(`v2_orchestrator.py:253-258`, located by AST) resolves to that module (unmapped: fail); (3)
`commits` when the executor reads `ctx.vault_writer`, `refuses` when the route's eligibility module
emits a reason literal starting `vault_not_supported_` (`_phase1_eligibility.py:181-183`), neither:
fail. `test_every_route_commits_the_vault_before_publish_or_refuses_vault_jobs` requires every
`commits` route to have a driver in `tests/test_vault_durability.py`'s `DRIVERS`; seeded red on
fixture trees (`tests/fixtures/route_sentry/`: an executor with no driver, a route with no
executor, a route neither committing nor refusing). The runtime property
`test_vault_is_committed_before_any_output_exists[<each commits route>]` spies
`_commit_vaults_before_publish`: at its return the leaf loads, a second `Session` sees the `ok`
node, and the target directory holds no output file; after the job every target exists.

Retention sentries (gate #6; the class fix for rev 2 #1, per repo, claim stated exactly):

- Platform `tests/test_vault_retention_sentry.py::test_no_code_path_removes_a_vault_leaf` walks
  the AST of `api/**/*.py`. A "remover" is a function that calls `unlink`, `remove`, `rmtree`,
  `truncate`, `os.replace` or `os.rename` on a parameter or local. The sentry fails when (a) a
  function referencing `job_vault_path` or `vault_dir` calls a removal primitive, or (b) a call to
  a remover of the tree receives an argument containing `job_vault_path(` or `vault_dir(`, or a
  name assigned from one in the same function: one level of helper indirection, resolved by callee
  name. A path stored on an object and removed elsewhere is outside the sentry and is caught by the
  runtime retention tests. Fixture trees under `tests/fixtures/vault_retention/`: `direct.py`
  (rev 2's unlink-on-failure wrapper) and `helper_indirection.py` (`def _tidy(p): p.unlink()`
  called with `job_vault_path(job.id)`) fail, `clean.py` passes. Empty allowlist (§1).
- Engine `tests/sentry/test_vault_retention_sentry.py::test_no_engine_code_path_removes_a_vault_leaf`:
  `src/decoy_engine/vault.py` contains no call named `unlink`, `remove`, `rmtree`, `replace`,
  `rename`, `truncate` or `write_bytes` (none today, at `51548303` and A-4); in
  `fileaccess/_output.py` and `_local.py` every `os.unlink`, `os.remove` and `Path.unlink` call
  takes an argument whose source text contains `stage` and never `leaf` or `target`. The
  replace-mode `os.replace(stage, leaf)` (`_output.py:264`, `_local.py:275`) is reachable only
  for non-vault labels, which N3 pin 1 pins at runtime (the leaf's inode is unchanged). Seeded:
  `Path(path).unlink()` in an `except` of `write`; `self._target.unlink()` on the `EEXIST` path.
- CLI `tests/unit/test_vault_retention_sentry.py::test_no_cli_code_path_removes_a_vault`: in
  `src/decoy/cli/*.py`, no function that references a parameter or local named `vault` calls one
  of those primitives. Red first on CLI main: `tokenize.py:384` is the seed.

Q4 and N4 (gate rev 2 #6). The CLI `run --vault` refusal is the engine's exclusive publication
(step 4); the CLI maps `vault_exists` to EXIT_USAGE and adds no `exists()` pre-check;
`LocalTokenVault`'s open (`_token_vault.py:109`, `O_RDWR | O_CREAT`, 0600) is unchanged.
`test_a_concurrent_second_run_is_refused_and_never_deletes_the_first_runs_vault`: (a) two
`decoy run --vault V` processes released by a `multiprocessing.Barrier`, 20 trials: exactly one
exits 0, the other EXIT_USAGE with `vault_exists`, `V` loads with the winner's entries and
`st_ino`, the loser wrote no output; (b) run 1 fails on table B, run 2 on `V` is refused, `V` is
byte-identical and re-identifies run 1's table A. Mutation: unlink the leaf on either path.

### D2. Vault-scoped token derivation (F2, N2)

Protocol (`execution/_adapter.py`): `TokenVaultWriter` gains one required attribute,
`derivation_scope: bytes`, an opaque, non-secret, vault-unique byte string of 16 to 64 bytes that the
vault owner mints once at vault creation and stores with the vault; the engine never logs it, writes
it into output, or derives anything from it alone. `BatchTokenVaultWriter` inherits it. A writer
without it, or with one outside the length bound or not `bytes`, fails closed with
`StrategyError(code="tokenize_vault_scope_required")` before any plaintext is normalized.

Derivation (`execution/_strategies/_tokenize.py`, new helper module
`execution/_strategies/_tokenize_key.py`):

```
TOKENIZE_KEY_LABEL = b"tokenize-key/v1"
K_tok   = derive(mask_key, namespace, TOKENIZE_KEY_LABEL + len(scope).to_bytes(4, "big") + scope)
token   = "tok_" + DeriveContext.for_column(K_tok, namespace).derive_source(namespace, NFC(pt).encode())[:16].hex()
```

`K_tok` is 32 bytes, a valid `derive` IKM (`_derive.py:144`). The label-and-scope bytes are the
framed SOURCE argument of the engine's existing `derive`, not a literal HKDF `info`: `derive`
wraps them in its own envelope (`SEED_PROTOCOL_VERSION`, the HKDF salt, the length prefixes,
`_derive.py:126-144`), the same envelope `vault-key/v1` and `ff1-key/v1` already go through, so
the purpose separation of RFC 5869 §3.2 is achieved by the framed source with no new KDF call
shape. `hash`, `fpe`, generation and the file vault key produce the same bytes as today. The
subkey is minted once per column and the per-value HMAC runs through the existing
`derive_sources` fast path.

Scope sources:

- Platform: `tokenization_vaults.derivation_scope BYTEA NULL` (migration
  `vault_v5_derivation_scope`), 32 bytes from `secrets.token_bytes(32)` set by `create_vault`
  (`store.py:93-113`); `TokenVaultWriterAdapter.derivation_scope` returns it. Nullable only
  because a `NOT NULL` column cannot be added to a populated table without a data migration,
  which the pre-release rule forbids; `NULL` is the hard cut's refusal signal, not a compatibility state.
- CLI: `meta.derivation_scope` (hex) written at `LocalTokenVault(create=True)`, 32 bytes from
  `secrets.token_bytes`; `_SCHEMA_VERSION` 1 to 2 (`_token_vault.py:63`). The scope is written
  inside the schema-initialising transaction (`BEGIN IMMEDIATE`, `INSERT OR IGNORE`, re-read), so
  two first runs on one path agree on one scope without an ownership flag;
  `test_derivation_scope_is_32_random_bytes_and_stable_across_reopen` gains a two-process case.
- CLI N2: closed on CLI main (`57e8b81`); `put` returns the stored token on both paths
  (`_token_vault.py:223-254`).

Hard cut (owner decision D2/Q1), one registered code `token_vault_derivation_unsupported`, raised
before the engine runs:

- Platform: `_resolve_vault` (`v2_token_vault.py:177-195`, where `revoked_at` is already refused)
  raises `TokenVaultConfigError(code="token_vault_derivation_unsupported")` when
  `vault.derivation_scope is None`; the job fails at vault resolution, no `run_pipeline` call.
- CLI (gate #5): `_ensure_schema` (`_token_vault.py:148-211`) serves both opens, `create=True`
  from tokenize (`tokenize.py:333`) and `create=False` from detokenize (`:631`), so the refusal
  runs on BOTH branches: on create, the stored `schema_version` is re-read after the `INSERT OR
  IGNORE` (`:167-170`) and `"1"` raises the code (rev 3 checked only the read branch, so a
  tokenize run on an old vault would have minted new-derivation tokens into it); on read
  (`:200-211`) `"1"` raises the same code ahead of the generic `vault_schema_mismatch`. Message
  (the CLI's vault messages already name the path, `:187, :196, :207`): "token vault <path> was
  written under schema version 1, before vault-scoped derivation; this CLI can neither mint into
  it nor reverse from it. Keep it with the outputs it reverses, under the CLI that wrote it, or
  re-tokenize the source into a new vault." Decision: refused for both commands; reversing what
  the CLI refuses to mint would carry two vault formats in one binary, which the hard cut exists
  to avoid. `test_v1_vault_is_refused_with_the_derivation_code[tokenize, detokenize]`: a schema-1
  file built by the test; both commands exit EXIT_USAGE with the code and the file is
  byte-identical afterwards. Mutation: accept version 1 on either branch.
- Engine: `tokenize_vault_scope_required` stays as the defense in depth for a writer that reaches
  the engine without a valid scope; it is not the old-vault code.

No marker column, no second derivation path, no rows are migrated. The `TOKEN_DERIVATION` export
and the `token_derivation` columns of rev 1 do not exist in this plan.

### D3. Tokenize and hash separate PRF domains (F3)

D2 already separates them: `hash` is `HMAC(HKDF(mask_key, ns), ...)` and `tokenize` is
`HMAC(HKDF(K_tok, ns), ...)` with `K_tok = derive(mask_key, ns, label || scope)`; the two HMAC keys
are HKDF outputs under different IKMs, so a hash prefix equals a token payload with probability
`2^-128` per value. The negative-equality test holds a generated corpus (Hypothesis `st.text()`,
NFC-equivalent pairs, both seams) and a pinned vector so "drop the label" and "use `mask_key` as
the IKM" both go red. The label is versioned in the string; a label change is a new hard cut.

### D4. Value-free frames on the vault failure path (F4)

Reuse `decoy_engine._opaque.OpaqueBox` and add one helper to the same module,
`closed_call(work, box) -> tuple[result | None, bool]`, the `_contained` shape of
`secret_providers/_preflight.py:165-175` without the failure rebuild (the tokenize error is already
a fixed coded `StrategyError`). `_preflight` is not touched. `TokenizeStrategyHandler.run`: (1)
first statement `box = OpaqueBox(df, ctx)`; `del df, ctx`; the preflight refusals are raised from
this frame, which holds `box`, `column`, `plan`, `self` only; (2) normalization and the distinct
list are computed inside `closed_call(_prepare, box)` and returned as an `OpaqueBox`; (3)
resolution runs as `closed_call(_resolve_tokens, box2)`; on failure `run` raises the coded error
from its own frame and the inner frames stay on the discarded traceback, so nothing on the raised
chain holds `normalized`, `distinct`, `minted`, `mask_key`, `plaintext` or `args`; (4) publication
opens the box inside the assigning call expression.

Canary (gate rev 1 #8): `tests/fileaccess/locals_support.py:16-17` renders bytes through
`latin-1` and `engine_locals` (`:27-50`) yields `repr(value)` plus nested strings, so the key
canary is the key's own ASCII bytes, `KEY = b"canary-key-0123456789abcdefghijk"`, the plaintext
canaries `pt-canary-<n>`, the assertion `assert_no_engine_local_holds(exc, "canary-key-",
"pt-canary-")`. Control: on B-6 the helper names `_resolve_tokens() local 'mask_key'` and `run()
local 'normalized'` (red first). Mutation: box everything except `mask_key`; the test must go red
on the key canary alone. `docs/security/error-reporting-and-data-exposure.md` line 9 gains a
bullet naming the tokenize vault failure path and the frames it covers; hostile in-process
inspection stays V2.

### D5. Validate inside the sanitized boundary (F5)

`_vault_call` materializes and type-checks inside its `try`: batch results are `type(result) is
dict` after `dict(result.items())`, every key and value `type(x) is str` (exact type: no
`__eq__`, `__hash__`, `__bool__`, `__str__` hooks run after the boundary) and non-empty; per-row
results are `None` or exact non-empty `str`. `_checked_tokens` then runs over plain `dict[str,
str]` and `set[str]` only and cannot raise anything but the coded error; the fail-closed rules
(`_adapter.py:131-137`, CHANGELOG B-6 entry) keep their meaning, the guard moves.

### D6. Tests at the real boundary (F6, N1)

Engine (S5): `tests/property/test_tokenize_parity.py` with Hypothesis over lists of strings (with
duplicates, NFC-composed and decomposed forms, nulls, empty strings), a random preloaded subset
(hits) and both writer seams over one shared in-memory store, asserting equal emitted tokens per
row, one lookup and one put per distinct miss on the batch seam, and vault-authoritative reuse.
Mutations: skip NFC on one seam; put a hit into `put_many`; reorder the batch.

Platform (S6b), N1 only (the batch seam is cut; the platform stays on the per-row seam).
`tests/test_vault_concurrency_pg.py::test_write_lock_is_reacquired_after[commit, rollback,
savepoint_rollback]`, two `Session`s over the `pg_url` fixture (`:64`), `pg_semantic` marker:
session A `storeA.put(v, "ns", "a", t1)` (lock taken at `store.py:160-165`), then the boundary
(`A.commit()`; `A.rollback()`; or a rolled-back `A.begin_nested()` savepoint, which releases the
row locks), then `storeA.put(v, "ns", "b", t2)`, which must take `FOR UPDATE` again. Proof: session
B runs `SET LOCAL lock_timeout = '300ms'` and `RotationManager(VaultStore(B)).complete(v)`, which
locks first (`rotation.py:214`) and must raise `OperationalError` with SQLSTATE `55P03`; the same
call before the boundary is the live control. Red first on main: after the boundary A's second put
skips the lock and B returns at once. Fix: `event.listen(session, "after_commit" |
"after_rollback" | "after_soft_rollback", clear)` in `VaultStore.__init__` (`store.py:88`), each
clearing `_write_locked` (and `_rotation_due` only on rollback). Mutation: remove one listener.

### D7. PyCA HKDF (F7)

`determinism/_hkdf.py` keeps its three names with the same signatures and semantics:
`hkdf_extract(salt, ikm)` runs the empty-salt `ValueError` first (`:54-59`, QA-10 F12), then PyCA
`HMAC(salt, SHA256())` over `ikm`; `hkdf_expand(prk, info, length)` is PyCA `HKDFExpand(SHA256(),
length, info).derive(prk)`, the `255 * HashLen` bound re-raised with the current message;
`hkdf_sha256(ikm, salt, info, length)` repeats the empty-salt `ValueError` as its first statement,
then PyCA `HKDF(SHA256(), length, salt, info).derive(ikm)`. The guard is repeated, not inherited,
because the one-shot no longer passes through `hkdf_extract`; PyCA accepts `b""` and derives under
the all-zero salt (§1 probe), the exact degrade the refusal exists to stop.

The module docstring's anti-PyCA rationale (lines 8-13) is replaced by the citation line
(`Pattern: RFC 5869 HKDF-SHA256 (PyCA cryptography)`), and the module is added to
`docs/methodology-registry.yaml`. Compatibility: byte-for-byte on the RFC A.1-A.3 vectors plus
engine vectors captured on main before the swap, `tests/fixtures/hkdf/engine-vectors.json` (the
engine salt with a 32-byte and an 8-byte IKM, the 32-zero-byte salt of `context._hkdf_sha256` and
`provenance.py`, the maximum length, and one `salt: ""` entry per entry point whose expected
result is the refusal, `"raises": "ValueError"`, prefix `salt must be non-empty`). Tests:
`test_engine_vectors_are_unchanged_by_the_pyca_swap`, `test_oneshot_rejects_empty_salt` (red
first on a naive swap seeded in the worktree); `test_empty_salt_rejected_by_extract`
(`test_hkdf.py:117`) stays. Mutations: drop either guard; map an empty salt to 32 zero bytes. D2
alters neither `derive`, `SEED_PROTOCOL_VERSION` (stays 7), the HKDF salt nor the info format.

### D8. Handoff note (F8)

`docs/handoffs/inputs/b6-engine-api-note.md` is rewritten in S5: the status line names platform
#110 as merged with the per-row seam (no batch half, §0a); §4's "the engine will never relay it"
becomes the D4 frame guarantee plus the D5 boundary; §5's "or the job seed pre-GA" is deleted
(keyed strategies need a resolved secret since F13); §2 step 4 gains the D2 derivation; §3 gains
`derivation_scope`. No sentry (cut, §0a): the rewrite closes F8.

## 4. Slices

Each slice: one repo, at most 10 production files, at most 800 changed lines (tests counted in
lines, not files), red tests first, then the change, then the mutation run recorded in the PR.
Module LOC (aim 600, max 700; `wc -l`): `vault.py` 451 on main and 531 on A-4; `_tokenize.py` 276
and `_adapter.py` 431 on B-6; `_local.py` 378, `_output.py` 389, `serial.py` 47 on A-4;
`_chunked.py` 640 and `native/_chunked_entry.py` 666 on main (S2b adds about 12 lines to each and
updates their census entries to the exact LOC). S4b deletes the ambient branch from `vault.py`
(`:336-343`) and adds about 30 lines; S3 splits the key helper out of `_tokenize.py`.

| Slice | Repo, base | Resolves | Production files | Depends on |
|---|---|---|---|---|
| S1 | engine, main | F7 | `determinism/_hkdf.py`, `docs/methodology-registry.yaml`, `docs/security/key-derivation.md` | A-4 merged |
| S2 | engine | F2, F3, hard-cut codes, registry bump test | `execution/_adapter.py`, `execution/_strategies/_tokenize.py`, `execution/_strategies/_tokenize_key.py` (new), `reasons/runtime/_part3.py`, `reasons/_registry.py`, `docs/reasons.md`, `docs/compatibility-contract.md`, `docs/security/key-derivation.md`, `CHANGELOG.md`, `.github/workflows/engine-v2-substrate-matrix.yml` (`fetch-depth: 0`, §5) | S1, B-6 merged |
| S2b | engine | F1 (public boundary, gate #1) | `execution/_pipeline_context.py`, `execution/_chunked.py`, `execution/native/_chunked_entry.py`, `execution/_pipeline_auto_chunk.py`, `reasons/runtime/_part1.py` (`vault_requires_resident_output` beside `invalid_execution_knob`, `:207`), `reasons/_registry.py`, `docs/reasons.md`, `CHANGELOG.md` | S2 (the bump test) |
| S3 | engine | F4, F5 | `_opaque.py`, `execution/_strategies/_tokenize.py`, `docs/security/error-reporting-and-data-exposure.md` | S2 |
| S4a | engine | F1 (fileaccess half), N3, gate #3, gate #7 | `fileaccess/_output.py`, `fileaccess/_local.py` (delete `vault_direct`, `STAGE_MODE`, `publish(durable=)`, steps 1-6), `fileaccess/adapters/serial.py` (`write_bytes(durable=)`), `fileaccess/_types.py` | S2b |
| S4b | engine | F1 (vault half), gate rev 2 #2, engine retention sentry | `vault.py`, `reasons/runtime/_part3.py` (`vault_commit_failed`, `vault_exists`), `reasons/_registry.py`, `docs/reasons.md`, `docs/security/token-vault.md` (R1-R4, operator rule), `docs/compatibility-contract.md`, `CHANGELOG.md` | S4a |
| S5 | engine | F6 (engine), F8 | `docs/handoffs/inputs/b6-engine-api-note.md`, `CHANGELOG.md` | S3 |
| S6a-i | platform, main | F2 (platform), hard cut, pin | `api/migrations/versions/vault_v5_derivation_scope.py` (new), `api/models.py`, `api/vaults/store.py`, `api/jobs/v2_token_vault.py`, `api/engine_a8_guard.py`, `pyproject.toml`, `.github/workflows/backend.yml` | R-b released |
| S6a-ii | platform | F1 (platform), N5, gate rev 2 #1 and #5, platform retention sentry | `api/jobs/v2_vault.py` (`_commit_vaults_before_publish`, `VaultCommitError`, error node deleted, `mode=0o700`), `api/jobs/v2_full_frame.py`, `api/jobs/v2_sequential.py`, `api/jobs/route_dispatch_exec.py` | S6a-i |
| S6b | platform | N1 | `api/vaults/store.py` | S6a-i |
| S7a | CLI, main | F2 (CLI), F1 (tokenize), N4, N2 record, gate #4, gate #5, CLI retention sentry | `src/decoy/cli/_token_vault.py`, `src/decoy/cli/tokenize.py`, `pyproject.toml` | R-b released |
| S7b | CLI | F1 (run), Q4, gate rev 2 #6, gate #1 (CLI) | `src/decoy/cli/run.py` | S7a |

Changed-line budget (estimate; the builder records `git diff --stat` and splits if over 800):

| Slice | Prod | Test | Total | Rev 3 total | Why it moved |
|---|---|---|---|---|---|
| S1 | 110 | 140 | 250 | 250 | |
| S2 | 185 | 345 | 530 | 530 | |
| S2b | 60 | 130 | 190 | new | B0: two checks, two internal names, one identity, three tests, ten import swaps, one deletion |
| S3 | 150 | 230 | 380 | 380 | |
| S4a | 130 | 230 | 360 | 320 | `STAGE_MODE`, the parent opened first, the umask test, two fault rows |
| S4b | 90 | 300 | 390 | 360 | engine retention sentry |
| S5 | 90 | 110 | 200 | 310 | no note-currency sentry |
| S6a-i | 110 | 220 | 330 | 330 | |
| S6a-ii | 80 | 340 | 420 | 380 | helper-indirection resolution and fixture trees |
| S6b | 30 | 100 | 130 | 460 | N1 only |
| S7a | 110 | 240 | 350 | 310 | both-branch schema check, the three tokenize tests, CLI retention sentry |
| S7b | 60 | 170 | 230 | 360 | `--chunked --vault` refused instead of staged |
| Total | 1205 | 2555 | 3760 | 3990 | |

`rule_codes.py` (CLI) needs no edit: its engine half is derived from the pinned registry
(`src/decoy/ci/rule_codes.py:10-14`); `tests/unit/test_ci_rule_codes.py` re-derives the set.

## 5. Merge and release order, versions, where each is recorded

Order (gate #2): (1) A-4 (`claude/jolly-carson-3k5stf`) merges first, through its own gate; its
registry bump is then main's value. (2) The B-6 batch writer (`feat/b6-batch-vault-writer-engine`)
rebases onto that main and merges. (3) S1, S2, S2b, S3, S4a, S4b, S5 on engine main, in that
order, each rebased onto main at build time, each its own PR and Codex gate. (4) R-b is cut as the
next minor engine release from the main carrying A-4 and S1-S5, with
`docs/records/release-rb-facts.md` written like `release-ra-facts.md`. (5) S6a-i, S6a-ii, S6b on
the platform (repin the engine to R-b, `ENGINE_PIN_SHA`), then S7a, S7b on the CLI (repin).
Nothing in S6/S7 can land against R-a: the protocol attribute and the codes do not exist there.

Reason registry rule: a slice that adds identities (S2, S2b, S4b) sets `REGISTRY_VERSION` to
main's value at its build time plus exactly one minor (`X.Y.Z` to `X.(Y+1).0`), and every identity
it adds carries `since=` that value. No version literal appears in this plan or in the tests; the
value is read from git at test time. `tests/unit/test_reason_registry_bump.py::
test_registry_is_merge_base_plus_one_minor_and_new_identities_carry_it` (lands in S2): (1)
outside a git checkout `pytest.skip` ("not a git checkout: the merge-base registry bump rule is
UNCHECKED", the only skip); (2) `base = git merge-base HEAD origin/main`; an unresolvable
`origin/main` FAILS ("fetch origin/main (CI checks out with fetch-depth: 0)"); `ci.yml:102`
already fetches full history, S2 adds `fetch-depth: 0` to `engine-v2-substrate-matrix.yml` (`:51`,
the only other workflow running the unit tree); (3) base version from `git show
<base>:src/decoy_engine/reasons/_registry.py` parsed with `ast`, base identities from `git archive
<base> src/decoy_engine` imported first on `sys.path` in a subprocess that prints
`reasons.manifest()`, key `(namespace, code)` (`_registry.py:28`); (4) `new = live_keys -
base_keys`; if `new` is empty and no base `since` changed, `live == base`; otherwise `live ==
bump_minor(base)`, every identity in `new` has `since == live`, and no base `since` changed. On
main after a merge `HEAD` is its own merge-base. Seeded red: one identity added at the base
`since` with `REGISTRY_VERSION` unbumped (the gate's case, green on both existing tests).
Mutations: bump two minors; bump the patch; leave one new identity at the base `since`. The three
existing registry tests stay; `reason-manifest-v1.json` is immutable; `reason-registry-release.json`
is recaptured only at the R-b cut by `reason_release_pin.py --write`.
`token_vault_derivation_unsupported` is declared under `_catalog.runtime("decoy_engine.execution._adapter")`
and raised by the platform and the CLI; no engine producer site is required (`test_reason_registry.py:177-199`).

| Surface | Today | After | Recorded in |
|---|---|---|---|
| Engine package | R-a | R-b, the next minor | `pyproject.toml`, `CHANGELOG.md`, tag |
| `run_pipeline`, `run_mask_chunked`, `run_mask_pipeline_chunked` | accept a vault writer with a sink or a chunk stream | refuse it (`vault_requires_resident_output`) | docstrings, `docs/compatibility-contract.md` §3.4, CHANGELOG |
| `TokenVaultWriter` | `lookup_existing`, `put` | plus `derivation_scope: bytes` (required; legal pre-GA only, stated) | `docs/compatibility-contract.md` §3.4, CHANGELOG |
| Token derivation | `derive(mask_key, ns, pt)` | `tokenize-key/v1` subkey with scope | `_tokenize_key.py` |
| `SEED_PROTOCOL_VERSION` | 7 | 7 | `_derive.py:136` |
| File vault format | `decoy-vault/v2`, `vault-key/v1` | unchanged bytes; publication path, mode and retention change | `vault.py:82-88`, `docs/security/token-vault.md` |
| Reason registry | main's value | plus one minor per adding slice (S2: `tokenize_vault_scope_required`, `token_vault_derivation_unsupported`; S2b: `vault_requires_resident_output`; S4b: `vault_commit_failed`, `vault_exists`) | `_part1.py`, `_part3.py`, `_registry.py`, `docs/reasons.md` |
| Platform DB | vault_v4 | `vault_v5_derivation_scope` (`BYTEA NULL`, NULL refused) | `api/migrations/versions/`, `test_vault_v5_migration.py` |
| CLI vault schema | 1 | 2 (1 refused with `token_vault_derivation_unsupported` on tokenize and detokenize) | `_token_vault.py::_SCHEMA_VERSION` |
| CLI `run --chunked --vault` | accepted | refused (EXIT_USAGE) | `run.py`, CLI CHANGELOG, `decoy explain vault` |
| Platform / CLI engine pin | R-a or older | R-b | `pyproject.toml`, `backend.yml` |

## 6. Acceptance tests (red first)

Each test is shown red on its "fails before" base with the seeded violation, then the change is
made, then the mutation is applied, shown red, and reverted (never committed with a mutation
applied). Baselines run from the lock in the branch's environment, and the record says so.

| Test | Seeded violation | Fails before | Mutation |
|---|---|---|---|
| `tests/unit/execution/test_vault_boundary.py::test_run_pipeline_refuses_a_sink_with_a_vault_writer_before_reading_a_source` (gate #1) | none: main accepts the pair | `_pipeline_full_frame.py:245` commits the sink; the vault is written after (`test_b6a_publish.py:436-443`) | drop the `build_run_context` check |
| `::test_chunked_entries_refuse_a_vault_writer_before_the_first_chunk[run_mask_chunked, run_mask_pipeline_chunked]` | none | `_chunked_entry.py:401-405`, `_chunked.py:405-407` | drop the chunked check |
| `::test_the_auto_chunk_lane_still_collects_vault_entries_resident` (control) | none | n/a (green on main; pins the lane) | the lane calls a public name |
| `tests/unit/test_vault_publish.py::test_sigkill_after_each_publish_step[ambient\|strict × K1..K7]` | none: the A-4 code; SIGKILL after the wrapped call of each step | A-4 `_local.py:225-231`, `serial.py:45-47`, `_output.py:252-268` (kill points not reached); main `vault.py:305` | restore the in-place open; buffered write; drop the file fsync |
| `::test_an_injected_failure_at_each_step[dir_open, write, file_fsync, stage_close, link_eio, link_eexist, dir_fsync, unlink, dir_close]` (gate #7) | the named call raises | A-4 `_output.py:71-77`, `_local.py:178-193` | swallow the step 5 error; unlink the leaf on step 5 failure; open the parent after the link |
| `::test_vault_is_0600_under_a_permissive_umask[ambient, strict]` (gate #3) | `os.umask(0)` | A-4 `_output.py:222`, `_local.py:246` | `0o666` for the vault label |
| `::test_publish_fsyncs_file_then_directory` | strace order | A-4 `serial.py:45-47`, `_output.py:252-268` | drop any fsync; fsync after the link |
| `::test_two_real_writers_one_publishes_one_fails_closed` | two threads, barrier | main `vault.py:305` | publish with `replace` |
| `::test_an_existing_hardlinked_vault_is_refused_and_both_names_are_unchanged` (N3 pin 1) | leaf and twin share one inode with old bytes | A-4 `test_char_vault_in_place.py:23-30` | re-add `vault_direct` |
| `::test_an_existing_vault_in_a_read_only_parent_is_preserved_and_no_stage_remains` (N3 pin 2) | writable vault, read-only parent | A-4 `test_char_vault_in_place.py:33-45` | re-add `vault_direct` |
| `tests/sentry/test_vault_retention_sentry.py::test_no_engine_code_path_removes_a_vault_leaf` (gate #6) | `Path(path).unlink()` in `write`'s `except`; `self._target.unlink()` on `EEXIST` | n/a (main removes nothing) | allow `target` in an unlink argument |
| platform `tests/test_vault_durability.py::test_file_vault_commit_failure_publishes_nothing` | `write` raises `vault_commit_failed` | `v2_full_frame.py:206` after `:168`; `v2_vault.py:89-98` error node | restore the error node |
| `::test_node_commit_failure_publishes_nothing_and_keeps_the_leaf` | `db.commit` raises after `write` | `v2_vault.py:65-100` has no such commit | publish before the commit |
| `::test_output_failure_keeps_a_reachable_vault[full_frame]` | `write_v2_outputs` raises | `v2_full_frame.py:168` before `:206`; rollback `v2_orchestrator.py:269` | add the node after publication |
| `::test_table_b_failure_keeps_a_vault_that_reidentifies_table_a[sequential, out_of_core]` (gate rev 2 #1) | table 2 of 2 raises; POST `/unmask` on table A | `v2_sequential.py:320-350` before `route_dispatch_exec.py:246-247`; `router.py:79-96` | unlink the leaf on failure; skip the node commit |
| `::test_vault_is_committed_before_any_output_exists[<derived>]` (N5) | spy at the vault commit | `route_dispatch_exec.py:246-247` | move the relationship commit after the loop |
| `::test_output_failure_after_the_token_commit_keeps_rows_and_a_rerun_adds_none` | publish raises after the commit | n/a (pins retention) | delete rows on failure |
| `tests/test_vault_route_parity_sentry.py::test_every_route_commits_the_vault_before_publish_or_refuses_vault_jobs` (gate rev 2 #5) | fixture trees: unmapped route, executor without driver, route neither committing nor refusing | n/a on main (new); red on each fixture | drop the driver for `out_of_core` |
| `tests/test_vault_retention_sentry.py::test_no_code_path_removes_a_vault_leaf[direct, helper_indirection, clean]` (gate #6) | the fixture trees | n/a (main removes nothing); red on `direct` and `helper_indirection` | skip callee resolution (`helper_indirection` passes) |
| CLI `tests/e2e/test_run_vault_gating.py::test_table_b_failure_keeps_a_vault_that_reidentifies_table_a` (gate rev 2 #1) | table B write raises in `_write_mask_outputs` | `run.py:840` before `:854` | unlink the vault on failure |
| `::test_vault_commit_failure_writes_no_outputs` | vault parent unwritable | `run.py:854` after `:840` | reorder back |
| `::test_an_existing_vault_is_refused_before_any_output` (Q4) | pre-existing vault | `run.py:854` overwrites | pre-delete the leaf |
| `::test_a_concurrent_second_run_is_refused_and_never_deletes_the_first_runs_vault` (gate rev 2 #6) | two processes released together, 20 trials; and run 1 failing on table B then run 2 | `run.py:854` (overwrite) | unlink the leaf on the refused or failed path |
| `tests/e2e/test_run_chunked.py::test_chunked_with_vault_is_refused_before_any_output` (gate #1, CLI) | `--chunked --vault` | `run.py:793-794` runs it; `:1443-1448` streams with the writer | pass the writer through again |
| `tests/unit/test_vault_retention_sentry.py::test_no_cli_code_path_removes_a_vault` (gate #6) | CLI main itself (`tokenize.py:384`) | `tokenize.py:384` | re-add the unlink |
| CLI `tests/e2e/test_tokenize_cli.py::test_output_failure_after_commit_keeps_the_vault_and_its_rows` (gate #4) | `_write_mask_outputs` raises after `commit()` | `tokenize.py:341` before `:348`; `:368-372` message; `:384` unlink | re-add the unlink; restore the old message |
| `::test_failed_run_keeps_the_vault_file_and_adds_no_rows` (replaces `test_failed_run_leaves_no_empty_vault`, gate #4) | failure before `commit()` | `tokenize.py:384` | re-add the unlink |
| `::test_unresolvable_secret_gives_a_coded_error_and_keeps_an_empty_vault` (replaces `test_unresolvable_secret_gives_a_coded_error`, gate #4) | unset env var | `tokenize.py:384` | re-add the unlink |
| `tests/unit/test_reason_registry_bump.py::test_registry_is_merge_base_plus_one_minor_and_new_identities_carry_it` (gate #2) | new identity at the base `since`, version unbumped | `reason_release_pin.py:48-65` passes it | bump two minors; bump the patch |
| `tests/unit/execution/test_tokenize_domain_separation.py::test_two_vaults_same_key_namespace_plaintext_mint_different_proposals` | two scopes | B-6 `_tokenize.py:253-258` | drop the scope from the label |
| `::test_writer_without_derivation_scope_fails_closed` | no attribute | `_tokenize.py:85-94` | default the scope to `b""` |
| `::test_token_payload_never_equals_hash_truncate_32` | Hypothesis corpus | `_tokenize.py:255` vs `kernel/_scalar.py:88` | `mask_key` as inner IKM |
| `::test_tokenize_subkey_vector` | pinned hex captured at S2 | n/a | change label, prefix or order |
| platform `tests/test_token_vault_adapter.py::test_a_vault_without_a_derivation_scope_is_refused_before_the_engine_runs` | `derivation_scope = NULL` | no column on main | constant scope for NULL |
| platform `tests/test_vault_store.py::test_two_vaults_tokenize_apart_through_the_real_engine` | same plaintext, two vaults | `v2_token_vault.py:158-166` | constant scope |
| CLI `tests/unit/test_token_vault.py::test_v1_vault_is_refused_with_the_derivation_code[tokenize, detokenize]` (gate #5) | schema-1 file | `_token_vault.py:63, 167-170, 200-211` | accept version 1 on either branch |
| `::test_derivation_scope_is_32_random_bytes_and_stable_across_reopen` | two creates; reopen; two processes | no scope | regenerate on open |
| `tests/unit/execution/test_tokenize_frame_locals.py::test_vault_failure_traceback_holds_no_plaintext_or_key[per_row, batch, lazy_mapping]` | ASCII key canary | B-6 frames (§1) | keep only `mask_key` unboxed |
| `tests/unit/execution/test_tokenize.py::test_lazy_mapping_whose_items_raises_is_sanitized[lookup_many, put_many]` | `items()` raises with plaintext | `_tokenize.py:212` | materialize outside the `try` |
| `::test_hostile_key_and_token_objects_are_rejected_inside_the_boundary` | `str` subclasses with raising hooks | `_tokenize.py:192, 214` | `isinstance` for `type(x) is str` |
| `tests/property/test_tokenize_parity.py::test_batch_and_per_row_emit_identical_tokens` | generated lists | `test_tokenize.py:535-541` | skip NFC on one seam; a hit in `put_many` |
| platform `tests/test_vault_concurrency_pg.py::test_write_lock_is_reacquired_after[commit, rollback, savepoint_rollback]` (N1) | B `complete()` under `lock_timeout` | `store.py:88, 160-165` | remove one listener |
| `tests/unit/determinism/test_hkdf.py::test_engine_vectors_are_unchanged_by_the_pyca_swap`, `::test_oneshot_rejects_empty_salt` | captured vectors incl. refusals; naive swap seeded | n/a; red on the seeded swap | map empty salt to zeros; drop a guard |
| `tests/sentry/test_hkdf_is_pyca.py::test_hkdf_module_delegates_to_cryptography` | stdlib import | main `_hkdf.py:26-27` | re-add the stdlib loop |

No threshold, slice bound, allowlist or census entry is widened. Pinned tests edited or deleted,
the complete list: the two N3 pins (`test_char_vault_in_place.py:23-45`, replaced in S4a); engine
`test_b6a_publish.py::test_streamed_vault_entries_and_file_equal_the_resident_run` (deleted in
S2b) and `::test_a_vault_writer_keyed_differently_is_rejected_before_anything_is_staged` (sink
dropped in S2b); the ten chunked-entry test sites of §1 (import swapped in S2b, assertions
untouched); CLI `test_tokenize_cli.py::test_failed_run_leaves_no_empty_vault` and
`::test_unresolvable_secret_gives_a_coded_error` (replaced in S7a); CLI
`test_vault_cli.py::test_chunked_run_writes_equivalent_vault` (deleted in S7b). No other pin or golden moves.

## 7. Owner decisions applied, and what is open

Applied as written (§0a): never-delete; the boundary rule; the four cuts; D2/Q1 hard cut on
both commands; Q3 fatal; Q4 refusal. Not reopened.

- Q2 (goldens), still open: no engine golden holds a tokenize output (`grep -rl "tok_"
  tests/fixtures tests/golden` on main is empty); `hash`, `fpe`, generation and vault bytes are
  unchanged; platform and CLI tests assert shape and round trip, not token values. The plan
  moves no golden; confirm, or name the pin to re-record.
- Flagged, not a new decision: `decoy run --chunked --vault` stops working (EXIT_USAGE, a message
  naming both flags). A run that needs a vault runs without `--chunked`; the auto-chunk lane inside
  `run_pipeline` still bounds memory one table at a time and returns resident output.
- Consequence to acknowledge: under R1 a CLI `run --vault V` that fails after the vault commit
  leaves `V`, so a rerun to `V` is refused (Q4) until the operator chooses a new path or removes
  `V` with its outputs. The message says so (D1 callers table).

## 8. Repeat-class register and stop conditions

- Class 1: F4 (second frame instance after A-4 round 4; D4 is the shared fix) and gate #3 (a
  readable vault, closed by the creation mode). A third frame-local instance stops the builder.
- Class 4, route parity and one-branch guards: F5, N5, gate rev 2 #5, gate #5. The route list is
  derived (D1); the schema check runs on both branches (D2). A class-4 slice finding stops it.
- Class 5 and 2: gate rev 2 #1 (deletion) and gate #1 (output before its vault). R1 plus a
  retention sentry per repo, and B0 at every public entry, are the class fixes. A further instance
  in a slice gate stops the slice.
- Class 7: N1 and N4 (closed by deleting the use). A further instance in S6 or S7 stops that slice.
- Class 8: gate rev 2 #2 and #4; gate #1 (no boundary test), #4, #6. Every claim here names its
  test and the test's seeded violation. A further instance in a slice gate stops that slice.
- Class 10: N3 (pins), gate rev 2 #3 and gate #2 (registry). A further instance in S2, S2b, S4
  or S7 stops that slice.

## 9. Measurements and probes (reproducible from the lock)

```bash
uv sync --frozen --all-extras            # engine 1132d29e; cryptography 49.0.0; Python 3.11.18
git worktree add /tmp/wt-b6 origin/feat/b6-batch-vault-writer-engine
cd /tmp/wt-b6 && /path/to/engine/.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/unit/execution/test_tokenize.py tests/unit/determinism tests/unit/test_vault.py
# 132 passed (rev 1)
```

The F3/F7 probe script is unchanged from rev 2 (`git show f4a2a3b1:docs/plans/2026-11-01-b6-vault-crypto-remediation.md`,
§9; results in §1). Empty-salt probe (rev 2; system Python 3.13.18, `cryptography 50.0.1`,
standalone copy of main `_hkdf.py`; S1 re-runs it from the lock): PyCA `salt=b""`, `salt=None` and
`salt=b"\x00" * 32` derive equal bytes; main's `hkdf_sha256(salt=b"")` raises `ValueError:
hkdf_extract: salt must be non-empty`. Not run in rev 4: no test, probe or build. Rev 4 is a
read-only revision; every repo, including the CLI, was re-read at the shas in the bases table.

## 10. Is this the smallest design that closes the defect

The defect is F1 (a published output can exist without its vault, and a vault can be torn,
overwritten or read by another local user) plus the crypto findings F2-F7. With the owner's cuts
applied, this is the smallest design that closes it: every remaining mechanism is reached by a
finding in §0b or §0c, and removing any one reopens a defect. B0 (gate #1): the engine owns
neither publication a sink or a chunk stream makes, so a refusal is the only enforcement. Steps
1-6 with the stage created `0o600` (gate #3, #7, rev 2 #2): stage, fsync, exclusive link,
directory fsync is the minimum crash-atomic, exclusive, private publication, and each step has a
kill row and a fault row. Vault before output on every route with the list derived (rev 2 #5,
N5): a hand-kept list was the class-4 miss. R1-R4 with one sentry per repo and one level of helper
resolution (gate #6, rev 2 #1). D2's scope (F2), D3 (F3), D4 and D5 (F4, F5), D7's refusal (F7), N1.

Taken from rev 3 by the owner: the platform batch seam (about 330 lines), chunked output staging
(about 130 lines; replaced by a 40-line refusal), the second directory fsync and its strace
clause, and the note-currency sentry (about 110 lines). No finding in §0b needed any of them. The
estimate fell from 3990 to 3760 changed lines, and the plan from 766 lines to the length of this
file. What would still be cut if asked, with its cost: (1) the strace order test
`test_publish_fsyncs_file_then_directory` (about 60 lines): the kill table infers the same order
from on-disk state, but only strace proves the `fsync` calls were issued at all; (2) D4's
`closed_call` helper (about 25 lines): inlining it into `run` closes F4 the same way but leaves
`_preflight`'s `_contained` as a second copy of the shape. Neither is a defect; both are evidence
or reuse.

## 11. Plan lint and done-when

No plan-lint tool exists in decoy-engine, decoy-platform or the CLI (rev 2 searched); the
`/home/cam/dev-rules` directory is not present in this environment. Checked by hand: each gate
rev 3 finding maps to a section, a slice and a red-first test with seeded violation and
fail-before location (§0b); owner decisions and cuts are applied, not reopened (§0a); the merge
order is executable and version-free (§5); every slice is under 800 (§4); every pinned test this
plan edits or deletes is named once (§6).

Done when: this file is on `plan/b6-vault-crypto-remediation`; the platform ROADMAP B-6 row links
it (orchestrator); the Codex plan gate returns GO; A-4 merges; S1-S7b follow §5.
