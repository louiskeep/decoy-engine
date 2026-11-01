Status: plan, rev 1 (Opus, 2026-11-01). Handoff. Risk class R3 (crypto; plan only, no code).

# PLAN: B-6 tokenization vault, crypto remediation (engine, platform, CLI)

Branch `plan/b6-vault-crypto-remediation` off engine `origin/main` `1132d29e` (R-a, 0.17.0). This
plan resolves the Codex NO-GO on the retroactive crypto pass over B-6 (0 BLOCKER, 3 HIGH, 4 MEDIUM,
1 LOW) and the defects found while re-reading the cited code. Phase 0 closes only when this pass is
signed off. Nothing here is built; every slice is specified red first.

Bases read for this plan (all fetched 2026-11-01):

| Repo | Ref | Sha | Role |
|---|---|---|---|
| decoy-engine | `origin/main` | `1132d29e` | R-a release commit; base of this branch |
| decoy-engine | `origin/feat/b6-batch-vault-writer-engine` | `e3c539d6` | B-6 batch writer branch under review (merge-base with main `0090f6f9`) |
| decoy-engine | `origin/claude/jolly-carson-3k5stf` | `e60c93f9` | A-4 file access, merging soon (`fileaccess`, `_opaque`) |
| decoy-platform | `origin/main` | `19b5007` | stage 2 (#110) merged, pins `decoy-engine==0.16.0` |
| decoy-platform | `origin/claude/optimistic-mayer-sfrak5` | (stale) | the old batch adapter; diverged, deletes newer main work, NOT a base |
| louiskeep/decoy (CLI) | `origin/main` | `df4c069` | B-1 local vault (CLI #64), pins `decoy-engine==0.15.1` |

Every measurement below was taken from the engine lock (`uv.lock`, `cryptography 49.0.0`, Python
3.11.18, `uv sync --frozen --all-extras` on `1132d29e`, `.venv/bin/python`), run against a worktree
of the branch named in each row. The platform and CLI were read, not executed: no platform or CLI
environment was built in this session, so every platform and CLI claim is marked VERIFIED-BY-READ
and its slice names the test that proves it in CI.

## 0. Findings register

Classes: (1) value-bearing output; (2) cleanup missing on an exit path; (3) state machine and
concurrency; (4) route parity and silent fallback; (5) referential integrity; (6) untrusted producer;
(7) check then use; (8) weak tests and overclaimed evidence; (9) behavior-preserving refactor drift;
(10) frozen contract drift.

| Id | Sev | Class | Where (re-read) | Design | Slice | Red-first test (file::function) |
|---|---|---|---|---|---|---|
| F1 | HIGH | 3 (+2 crash window, +10 pinned partner) | engine `vault.py:305` on main and B-6; A-4 `fileaccess/_local.py:225-237`, `_output.py:271-273`, `adapters/serial.py:45-47`; platform `v2_full_frame.py:168,206`, `v2_vault.py:84-91`; CLI `run.py:840,854`, `tokenize.py:341,348` | D1 | S4, S6a, S7 | `tests/unit/test_vault_publish.py::test_crash_at_each_step_leaves_no_leaf_and_no_stage`, `::test_two_real_writers_one_publishes_one_fails_closed`, `::test_publish_fsyncs_file_then_directory`; platform `tests/test_vault_durability.py::test_file_vault_commit_failure_publishes_nothing`; CLI `tests/e2e/test_run_vault_gating.py::test_vault_commit_failure_writes_no_outputs` |
| F2 | HIGH | 1 | B-6 `_tokenize.py:253-258`; `_adapter.py:98-157` (no vault scope in either protocol); platform `v2_token_vault.py:158-166` supplies none; CLI `_token_vault.py:215-251` supplies none | D2 | S2, S6a, S7 | `tests/unit/execution/test_tokenize_domain_separation.py::test_two_vaults_same_key_namespace_plaintext_mint_different_proposals`, `::test_writer_without_derivation_scope_fails_closed` |
| F3 | HIGH | 1 | B-6 `_tokenize.py:255` vs `kernel/_scalar.py:88` and `generation/pool/_canonicalize.py:120` (NFC string branch) | D3 | S2 | `tests/unit/execution/test_tokenize_domain_separation.py::test_token_payload_never_equals_hash_truncate_32`, `::test_tokenize_subkey_vector` |
| F4 | MEDIUM | 1 (second appearance, see §8) | B-6 `_tokenize.py:147-155` (`df`, `col`, `normalized`, `distinct`, `ctx`), `:233-258` (`distinct`, `mask_key`, `plaintext`, `minted`), `:181` (`args`) | D4 | S3 | `tests/unit/execution/test_tokenize_frame_locals.py::test_vault_failure_traceback_holds_no_plaintext_or_key` |
| F5 | MEDIUM | 4 (guard placement) | B-6 `_tokenize.py:169-186` sanitizes `fn(*args)` only; `:212` consumes `result.items()` outside it; `:192` `bool(token)` on an unchecked type | D5 | S3 | `tests/unit/execution/test_tokenize.py::test_lazy_mapping_whose_items_raises_is_sanitized`, `::test_hostile_key_and_token_objects_are_rejected_inside_the_boundary` |
| F6 | MEDIUM | 8 | B-6 `tests/unit/execution/test_tokenize.py:535-541` (one fixed parity example), `:565-579` (fabricated winner); platform has no batch seam on main (`v2_token_vault.py:146-166`) | D6 | S5, S6b | engine `tests/property/test_tokenize_parity.py::test_batch_and_per_row_emit_identical_tokens`; platform `tests/test_vault_batch_pg.py::test_two_sessions_interleave_lookup_many_and_put_many`, `::test_partial_put_many_after_a_concurrent_commit`, `::test_batch_commit_failure_publishes_nothing` |
| F7 | MEDIUM | 9 (the swap must be byte-identical) | `determinism/_hkdf.py:1-95` (stdlib HKDF; docstring lines 8-13 say PyCA is not a dep, which is false since Task 5.2); callers `_derive.py:213,303`, `context.py:348-371` (zero salt), `profiling/model_pack/provenance.py:114` (zero salt) | D7 | S1 | `tests/unit/determinism/test_hkdf.py::test_engine_vectors_are_unchanged_by_the_pyca_swap`, `tests/sentry/test_hkdf_is_pyca.py::test_hkdf_module_delegates_to_cryptography` |
| F8 | LOW | 8 | B-6 `docs/handoffs/inputs/b6-engine-api-note.md:2` ("platform half not started"), `:93-96` ("the engine will never relay it"; false for F5), `:114-115` ("or the job seed pre-GA"; false since F13: `vault.py:349-350`, `test_tokenize.py::test_tokenize_requires_a_secret_key`) | D8 | S5 | `tests/sentry/test_b6_note_currency.py::test_note_signatures_and_claims_match_code` |
| N1 (new) | HIGH | 7 | platform `api/vaults/store.py:88,160-165`: `_write_locked` is never cleared on commit or rollback (`grep after_commit\|after_rollback` finds only the rotation launcher at `:391`), so a store reused after `session.commit()` skips `SELECT ... FOR UPDATE` and writes unlocked, racing `complete()` | D6 (N1) | S6b | platform `tests/test_vault_concurrency_pg.py::test_write_lock_is_reacquired_after_commit_and_rollback` |
| N2 (new) | HIGH | 10 | CLI `_token_vault.py:223` `put` returns `None`; engine 0.16+ `_tokenize.py:266-268` refuses a `None` return. The CLI pin is 0.15.1 today; the R-b repin turns every `decoy tokenize` run into `tokenize_vault_error` | D2 (CLI) | S7 | CLI `tests/unit/test_token_vault.py::test_put_returns_the_authoritative_token` |
| N3 (new) | MEDIUM | 10 | A-4 `tests/fileaccess/test_char_vault_in_place.py:23-45` pins the in-place truncate rewrite (hardlink written through, read-only directory rewritten) that F1 removes; the pin and the behavior must move in one commit | D1 | S4 | the two tests are deleted in the S4 commit that changes the behavior; `tests/unit/test_vault_publish.py::test_a_hardlinked_twin_is_not_rewritten` replaces the first |

Defects found in the existing code that are NOT folded into any move: N1, N2, N3 each have their
own row, test and slice step.

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
| F7: PyCA `HKDF`/`HKDFExpand` equal `_hkdf` byte for byte | True on the engine salt, the RFC A.2-shaped vector, the 255*32 maximum, the 32-zero-byte salt, and expand-only (probe) | n/a (behavior-preserving) |
| F1 engine: `Path.write_bytes` straight to the final path | main `vault.py:305`; A-4 ambient route opens the target with `O_TRUNC` in place (`_local.py:34,231`) and never fsyncs; A-4 strict route stages and renames but never fsyncs the file data (`serial.py:45-47`) and swallows the directory fsync error (`_output.py:71-77`) | by read |
| F1 callers publish outputs before the vault commits | platform `v2_full_frame.py:168` (`write_v2_outputs`) precedes `:206` (`_persist_job_vault`), and `v2_vault.py:78-91` documents that a vault write failure does not fail the job; CLI `run.py:840` precedes `:854`; CLI `tokenize.py:341` precedes `:348` | VERIFIED-BY-READ |
| Platform two-writer serialization | `store.py:138-158` takes `SELECT ... FOR UPDATE` on the vault row per transaction on Postgres (nothing on SQLite); real two-session races exist for the per-row seam only (`tests/test_vault_tokenize_race_pg.py::_RaceWriter`), gated on `DECOY_TEST_DB_BACKEND=postgres` in the `backend-postgres` CI leg ("PG-semantic tests must run (by marker, fail the leg if any skip)") | VERIFIED-BY-READ; N1 is the gap |
| Platform batch seam | absent on main; the old batch branch is not a usable base (its diff against main deletes `test_vault_concurrency_pg.py`, `test_vault_durability.py`, `rate_limit.py`) | VERIFIED-BY-READ |
| CLI local vault | SQLite, cleartext by design, `put -> None`, no scope, commit after outputs | VERIFIED-BY-READ |

Unverifiable from this checkout: PostgreSQL behavior and crash behavior of the platform store. Each
is covered by a named test in S6a/S6b that runs in the `backend-postgres` leg with a real
`postgres:15`, and by a crash-injection test that uses two sessions, not mocks.

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
  already derives fingerprint keys with PyCA `HKDF` (`api/crypto/vault_key.py:187`).
- Crash-atomic file publication: write to a same-directory stage, `fsync` the file, `rename`
  over the leaf, `fsync` the directory (POSIX; the pattern SQLite's atomic-commit notes and
  PostgreSQL's `durable_rename` implement). A-4 already stages and renames through
  `fileaccess` (cap-std `Dir`, Spark `FileOutputCommitter`). D1 adds the two missing `fsync`
  calls and uses the existing `exclusive` mode (`link(2)` refusing an existing leaf) as the
  compare-and-set between two writers. No lock file, no new mechanism.
- Secret-free frames: the A-4 boxed-entry construction (`decoy_engine/_opaque.py`,
  `secret_providers/_preflight.py:165-198`): value-bearing arguments move into an `OpaqueBox`,
  the plain names are deleted, the work runs in a contained call whose frames never reach the
  raised error.
- Idempotent concurrent insert: `INSERT ... ON CONFLICT DO NOTHING` plus re-read (already in
  `store.py:255-319`); the batch form is the same statement over many rows.

## 3. Designs

### D1. Durable, exclusive vault publication; callers gate on it (F1, N3)

Engine (`vault.py`, A-4 `fileaccess`), after the A-4 merge:

1. `VaultWriter.write(path | OutputDir)` always publishes through a stage beside the leaf and
   `exclusive` mode. The ambient path-string route stops using the `vault_direct` in-place label:
   `_local.py:225-231` and the label itself are deleted (pre-release hard delete). Under the
   ambient policy a path string stages in the target's parent exactly as the strict route does
   (`_CREATE` with `O_EXCL` on a `_decoy_vault_stage_<hex>` sibling, `_local.py:244-246`).
2. `serial.write_bytes(txn, component, data, *, durable=False)` gains `durable=True`: `flush()`
   then `os.fsync(fd)` before the handle closes. The vault is the only caller that passes it.
3. `OutputTxn.publish(*, durable=False)`: with `durable=True`, after the `link`/`replace` the
   parent directory is fsynced and a failure of that fsync raises (it is not swallowed as in
   `_output.py:71-77` and `_local.py:178-193`). Both implementations (`RootedOutputTxn`,
   `LocalOutputTxn`) take the flag; existing callers are unchanged (default `False`).
4. Two writers: `exclusive` publication is the compare-and-set. The first `link(2)` wins; the
   second gets `EEXIST`, its stage is removed, and `VaultWriter.write` raises
   `VaultError(code="vault_exists")` (new reason, §5). A vault leaf is never replaced in place.
   An operator who wants to rewrite a CLI vault deletes it first; the platform leaf is per job id
   (`job_<id>.dcyvault`) and is created once.
5. Every failure after the stage exists (write, fsync, link, directory fsync) aborts the
   transaction (stage unlinked, `_discard`) and raises `VaultError(code="vault_commit_failed")`
   with no path and no value in the message. `write` returns the entry count only after the
   directory fsync succeeded.
6. Hardlinks: a twin hardlink of an existing leaf is no longer rewritten through (it keeps the
   old inode); this is the behavior N3's pins were protecting and it goes with them.

Callers (publication gated on the vault commit):

- Platform `v2_full_frame.py`: `_persist_job_vault(vault_writer, job.id)` moves to before
  `write_v2_outputs` (after `_commit_token_vault_before_publish`), and a vault write failure
  raises `VaultCommitError` (new, `api/jobs/v2_vault.py`) that the terminal handler maps to a
  failed job with nothing published. The `status="error"` node path at `v2_vault.py:89-100` is
  deleted. The docstring claim "a write failure does NOT fail the job" is deleted with it.
- CLI `run.py`: `vault_writer.write(vault)` moves to before `_write_mask_outputs`; a
  `VaultError` exits `EXIT_RUNTIME` with no output files written.
- CLI `tokenize.py`: `token_vault.commit()` moves to before `_write_mask_outputs`; the
  empty-vault cleanup at `:377-383` keeps its meaning (a failed commit leaves no new vault).

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

- Platform: `tokenization_vaults.derivation_scope BYTEA NOT NULL` (migration
  `vault_v5_derivation_scope`), 32 bytes from `secrets.token_bytes(32)` at `create_vault`;
  `TokenVaultWriterAdapter.derivation_scope` returns it.
- CLI: `meta.derivation_scope` (hex) written at `LocalTokenVault(create=True)`, 32 bytes from
  `secrets.token_bytes`; `_SCHEMA_VERSION` 1 to 2; a v1 vault fails closed with the existing
  `vault_schema_mismatch` (`_token_vault.py:203-211`), which is the pre-release hard cutover.
- CLI N2: `LocalTokenVault.put` returns the stored token (its own row on a fresh insert, the
  existing row's token on the idempotent same-token path; the different-token conflict still
  raises). `put_many` and `lookup_existing_many` are added over one `executemany` and one
  `IN (...)` query; both namespace-scoped.

Compatibility: existing tokenization vaults (platform rows, CLI files) were minted without a scope.
Owner question Q1 (§7) decides between (a) hard delete: the migration refuses to run while
`token_mappings` has rows and operators drop their vaults, and (b) tombstone: existing vaults get
`token_derivation = 'tokenize/v0'`, every new tokenize job against them is refused with
`token_vault_derivation_unsupported`, detokenize of their stored rows keeps working. The plan's
default, pending the answer, is (b), because it needs no destructive migration and the standing rule
(no backward compatibility, no migration) is satisfied either way. Nothing in S2 depends on the
answer; S6a's migration has one branch per option and ships the chosen one.

### D3. Tokenize and hash separate PRF domains (F3)

D2 already separates them: `hash` is `HMAC(HKDF(mask_key, ns), ...)` and `tokenize` is
`HMAC(HKDF(K_tok, ns), ...)` with `K_tok = derive(mask_key, ns, label || scope)`. The two HMAC keys
are outputs of HKDF under different IKMs, so equality of a hash prefix and a token payload for the
same plaintext has probability `2^-128` per value. The negative-equality regression test holds a
generated corpus (Hypothesis `st.text()`, NFC-equivalent pairs included, both seams) and a pinned
vector so the mutation "drop the label" and the mutation "use `mask_key` as the IKM" both go red.

The label is versioned in the string (`tokenize-key/v1`) and recorded where the scope is recorded
(platform `tokenization_vaults.token_derivation TEXT NOT NULL`, CLI `meta.token_derivation`), so a
future label change is a detectable vault incompatibility rather than silent token drift. The engine
exports the constant as `decoy_engine.execution.TOKEN_DERIVATION = "tokenize/v1"` for the two
callers to store and compare.

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

Platform (S6b), every test with two `Session`s over `_postgres_url_or_skip()` and the
`pg_semantic` marker so the `backend-postgres` leg fails if they skip:

- `test_two_sessions_interleave_lookup_many_and_put_many`: both sessions miss on the same
  distinct set, A commits `put_many` first, B's `put_many` returns A's tokens for the overlap and
  B's own for the rest; the emitted frame under the real engine matches the stored rows.
- `test_partial_put_many_after_a_concurrent_commit`: between B's `lookup_existing_many` and
  `put_many`, A commits a subset; B's result is authoritative for every key (conflict re-read).
- `test_batch_commit_failure_publishes_nothing`: `db.commit` raises inside
  `_commit_token_vault_before_publish` with the batch writer; `write_v2_outputs` is never called;
  the second session sees no rows.
- `test_write_lock_is_reacquired_after_commit_and_rollback` (N1): a store that committed once
  must take `FOR UPDATE` again on the next put; proven with `pg_locks` on the vault row from the
  second session. Fix: `_write_locked.clear()` on `after_commit`, `after_rollback` and
  `after_soft_rollback` session events registered in `VaultStore.__init__`.
- `test_file_vault_commit_failure_publishes_nothing` (F1 platform): `VaultWriter.write`
  raising `vault_commit_failed` fails the job before `write_v2_outputs`.

### D7. PyCA HKDF (F7)

`determinism/_hkdf.py` keeps its three names with the same signatures and semantics:

- `hkdf_extract(salt, ikm)`: PyCA `HMAC(salt, SHA256())` over `ikm`; the empty-salt
  `ValueError` stays (PyCA would map `None` to `HashLen` zeros, which is the degrade QA-10 F12
  refused).
- `hkdf_expand(prk, info, length)`: PyCA `HKDFExpand(SHA256(), length, info).derive(prk)`; the
  `255 * HashLen` bound is PyCA's own `ValueError`, re-raised with the current message.
- `hkdf_sha256(ikm, salt, info, length)`: PyCA `HKDF(SHA256(), length, salt, info).derive(ikm)`.

The module docstring's anti-PyCA rationale (lines 8-13) is replaced by the citation line
(`Pattern: RFC 5869 HKDF-SHA256 (PyCA cryptography)`), and the module is added to
`docs/methodology-registry.yaml`. Compatibility: byte-for-byte on every existing vector (RFC
A.1-A.3) plus new engine vectors captured on main before the swap:
`tests/fixtures/hkdf/engine-vectors.json` with the engine salt and a 32-byte and an 8-byte IKM,
the 32-zero-byte salt used by `context._hkdf_sha256` and `provenance.py`, and the maximum length.
Vault-scoped derivation (D2) changes no existing output: it adds a derivation, it does not alter
`derive`, `SEED_PROTOCOL_VERSION` (stays 7), the HKDF salt or the info format. Only tokenize
tokens change, and Q1 covers the vaults that hold old ones.

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
`_adapter.py` 431 on B-6; `_local.py` 378 and `_output.py` 389 on A-4. S4 keeps `vault.py` under
600; S3 splits the key helper out of `_tokenize.py` rather than growing it past 400.

| Slice | Repo, base | Resolves | Production files | Depends on |
|---|---|---|---|---|
| S1 | engine, main | F7 | `determinism/_hkdf.py`, `docs/methodology-registry.yaml`, `docs/security/key-derivation.md` | none |
| S2 | engine, main after B-6 merges | F2, F3 | `execution/_adapter.py`, `execution/_strategies/_tokenize.py`, `execution/_strategies/_tokenize_key.py` (new), `execution/__init__.py` (export `TOKEN_DERIVATION`), `reasons/runtime/_part3.py`, `docs/compatibility-contract.md`, `docs/security/key-derivation.md`, `CHANGELOG.md` | S1 (vectors), B-6 branch merged |
| S3 | engine | F4, F5 | `_opaque.py`, `execution/_strategies/_tokenize.py`, `docs/security/error-reporting-and-data-exposure.md` | S2, A-4 merged (for `_opaque.py` and `locals_support`) |
| S4 | engine | F1, N3 | `vault.py`, `fileaccess/_output.py`, `fileaccess/_local.py`, `fileaccess/adapters/serial.py`, `fileaccess/_types.py` (the `publish(durable=)` protocol), `reasons/runtime/_part3.py`, `docs/security/token-vault.md`, `docs/compatibility-contract.md` | A-4 merged |
| S5 | engine | F6 (engine), F8 | `docs/handoffs/inputs/b6-engine-api-note.md`, `CHANGELOG.md` (plus the property and sentry tests) | S2, S3 |
| S6a | platform, main | F2, F1 (platform), N2 pin | `api/migrations/versions/vault_v5_derivation_scope.py` (new), `api/models.py`, `api/vaults/store.py` (scope at create, `token_derivation` check), `api/jobs/v2_token_vault.py` (`derivation_scope`), `api/jobs/v2_full_frame.py`, `api/jobs/v2_vault.py`, `api/engine_a8_guard.py`, `pyproject.toml` (repin to R-b) | R-b released |
| S6b | platform | F6 (platform), N1 | `api/vaults/store.py` (`lookup_existing_many`, `put_many`, lock-cache clearing), `api/jobs/v2_token_vault.py` (batch adapter) | S6a |
| S7 | CLI, main | F1 (CLI), F2 (CLI), N2 | `src/decoy/cli/_token_vault.py`, `src/decoy/cli/tokenize.py`, `src/decoy/cli/run.py`, `pyproject.toml` (repin to R-b), `src/decoy/ci/rule_codes.py` (new reason codes) | R-b released |

The CLI B-1 writer does not share the derivation (the engine mints; `_token_vault.py:11-15`), but
S7 is still required: the protocol gains `derivation_scope` (F2), `put` must return the token (N2),
and the output/vault order is reversed (F1).

Call sites that change:

| Repo | File:line (read) | Change |
|---|---|---|
| engine | `vault.py:263-345` (A-4 numbering) | durable exclusive publish, `vault_commit_failed`, `vault_exists` |
| engine | `_tokenize.py:85-94, 154, 253-258` | scope check, subkey mint, boxed frames |
| engine | `_adapter.py:98-157` | `derivation_scope` on `TokenVaultWriter` |
| platform | `v2_token_vault.py:146-166` | scope attribute, batch methods |
| platform | `v2_full_frame.py:165-172, 205-206` | vault write before publish, failure fatal |
| platform | `v2_vault.py:65-100` | raise instead of an error node |
| platform | `store.py:88, 93-113, 255-319` | scope at create, lock-cache clearing, batch statements |
| platform | `engine_a8_guard.py:60-110` | assert `derivation_scope` and the batch signatures |
| CLI | `_token_vault.py:100-130, 215-251` | schema v2, scope, `put -> str`, batch methods |
| CLI | `tokenize.py:333-348`, `run.py:840-854` | vault commit before outputs |

## 5. Merge and release order, versions, where each is recorded

Order: S1, S2, S3 land on engine main in that order (each its own PR, Codex gate each), after the
B-6 batch branch merges (the batch writer preserves single-row behavior; S2 and S3 change the
behavior it preserves). A-4 merges, then S4, then S5. Release R-b is cut from the engine main that
carries A-4 and S1-S5 as `v0.18.0`; `docs/records/release-rb-facts.md` is written the way
`release-ra-facts.md` is (every value with its command). Then S6a and S6b on the platform (repin
`decoy-engine==0.18.0`, `ENGINE_PIN_SHA` in `backend.yml`), then S7 on the CLI (repin
`decoy-engine==0.18.0`). Nothing in S6/S7 can land against 0.17.0: the protocol attribute and the
reason codes do not exist there.

Version and format bumps, exact:

| Surface | Today | After | Recorded in |
|---|---|---|---|
| Engine package | 0.17.0 | 0.18.0 (R-b) | `pyproject.toml` version, `CHANGELOG.md` `## [0.18.0]`, tag `v0.18.0` |
| `TokenVaultWriter` protocol | `lookup_existing`, `put` | plus `derivation_scope: bytes` (required) | `docs/compatibility-contract.md` §3.4 (the tokenize paragraph at lines 523-540) and CHANGELOG; a required attribute is not additive under §4.1, legal pre-GA only, stated as such |
| Token derivation | unlabeled `derive(mask_key, ns, pt)` | `tokenize-key/v1` subkey with scope; `TOKEN_DERIVATION = "tokenize/v1"` | engine `execution/__init__.py`; platform `tokenization_vaults.token_derivation`; CLI `meta.token_derivation` |
| `SEED_PROTOCOL_VERSION` | 7 | 7 (unchanged; `derive` is untouched) | `_derive.py:136`, `test_derive_vectors.py::test_seed_protocol_version_is_seven` stays |
| File vault format | `decoy-vault/v2`, `vault-key/v1` | unchanged (bytes identical; only the publication path changes) | `vault.py:82-88`, `docs/security/token-vault.md` "File format" |
| Reason registry | 1.1.0 (891 identities) | 1.2.0 with four additions, `since: "1.2.0"`: `tokenize_vault_scope_required`, `vault_commit_failed`, `vault_exists`, `token_vault_derivation_unsupported` (platform-raised, engine-registered so the CLI `rule_codes` and the registry agree) | `reasons/runtime/_part3.py`, `tests/fixtures/f29/reason-manifest-v1.json`, `tests/fixtures/f29/reason-registry-release.json` at the R-b cut (`scripts/release/reason_release_pin.py`), `docs/reasons.md` |
| Platform DB | vault_v4 | `vault_v5_derivation_scope`: `derivation_scope BYTEA NOT NULL`, `token_derivation TEXT NOT NULL` | `api/migrations/versions/`, `test_vault_v1_migration.py` sibling `test_vault_v5_migration.py` |
| CLI vault schema | `meta.schema_version = 1` | 2 | `_token_vault.py::_SCHEMA_VERSION`, CLI CHANGELOG |
| Platform engine pin | 0.16.0 | 0.18.0 | `pyproject.toml:36`, `backend.yml` `ENGINE_PIN_SHA` |
| CLI engine pin | 0.15.1 | 0.18.0 | `pyproject.toml:99` |

## 6. Acceptance tests (red first)

Every test is written and shown red on the base named in "fails before" with the seeded violation,
then the change is made, then the named mutation is applied and the test shown red again, then
reverted. Mutations are recorded in the PR's evidence record under `docs/records/`.

| Test | Seeded violation (red first) | Fails before (file:line, base) | Mutation that must turn it red |
|---|---|---|---|
| `tests/unit/test_vault_publish.py::test_crash_at_each_step_leaves_no_leaf_and_no_stage[stage_create, write, file_fsync, link, dir_fsync]` | `monkeypatch` the named `os` call through `fileaccess._output._call` / `_local` to raise `OSError(EIO)` | A-4 `_local.py:231` (ambient truncates the leaf in place: partial leaf remains); main `vault.py:305` | restore the in-place open, or swallow the `link` error |
| `::test_publish_fsyncs_file_then_directory` | record `os.fsync(fd)` with `os.fstat` identity; assert file inode synced before `link`, directory inode after | A-4 `serial.py:45-47` (no file fsync), `_output.py:71-77` (best effort) | drop either fsync |
| `::test_two_real_writers_one_publishes_one_fails_closed` | two threads, real `tmp_path`, `threading.Barrier` after both staged; exactly one leaf, loser raises `vault_exists`, no `_decoy_vault_stage_*` residue, `load_vault` reads the winner | main `vault.py:305`: both succeed, last writer wins | switch the label to `replace` |
| `::test_directory_fsync_failure_fails_the_commit` | `os.fsync` on a directory fd raises | A-4 `_output.py:71-77`, `_local.py:178-193` | swallow the error |
| `::test_a_hardlinked_twin_is_not_rewritten` (replaces N3 pin 1) | a hardlinked twin keeps the old bytes | A-4 `test_char_vault_in_place.py:23-30` asserts the opposite | re-add `vault_direct` |
| platform `tests/test_vault_durability.py::test_file_vault_commit_failure_publishes_nothing` | `VaultWriter.write` raises `vault_commit_failed` | `v2_full_frame.py:206` runs after `:168` | move `_persist_job_vault` back after `write_v2_outputs` |
| CLI `tests/e2e/test_run_vault_gating.py::test_vault_commit_failure_writes_no_outputs` | vault parent unwritable | `run.py:854` after `:840` | reorder back |
| CLI `tests/e2e/test_tokenize_cli.py::test_vault_commit_precedes_output_write` | `LocalTokenVault.commit` raises | `tokenize.py:348` after `:341` | reorder back |
| `tests/unit/execution/test_tokenize_domain_separation.py::test_two_vaults_same_key_namespace_plaintext_mint_different_proposals` | two `FakeVault`s with distinct `derivation_scope`, same key, ns, plaintext; `puts[0][2]` differ | B-6 `_tokenize.py:253-258` | drop the scope from the label |
| `::test_writer_without_derivation_scope_fails_closed` | `FakeVault` without the attribute; expect `tokenize_vault_scope_required` before any `lookup` call | no check exists (`_tokenize.py:85-94`) | default the scope to `b""` |
| `::test_token_payload_never_equals_hash_truncate_32` | Hypothesis corpus; `hash_array(..., truncate=32)` vs token payload | B-6 `_tokenize.py:255` vs `kernel/_scalar.py:88` (probe: equal) | mint with `mask_key` as the inner IKM |
| `::test_tokenize_subkey_vector` | pinned hex for `(key, ns, scope, pt)` captured at S2 | n/a (new) | any change to the label, the prefix or the order |
| platform `tests/test_vault_store.py::test_two_vaults_tokenize_apart_through_the_real_engine` | same plaintext, two vaults, real engine via `run_pipeline` | `v2_token_vault.py:158-166` (no scope) | return a constant scope |
| `tests/unit/execution/test_tokenize_frame_locals.py::test_vault_failure_traceback_holds_no_plaintext_or_key[per_row, batch, lazy_mapping]` | `assert_no_engine_local_holds(exc, plaintexts..., key_hex)` (A-4 `tests/fileaccess/locals_support.py:53`) | B-6 frames listed in §1 | keep `normalized` as a local in `run` |
| `tests/unit/execution/test_tokenize.py::test_lazy_mapping_whose_items_raises_is_sanitized[lookup_many, put_many]` | mapping subclass whose `items()` raises with the plaintext in the message; expect `tokenize_vault_error` with empty chain | B-6 `_tokenize.py:212` (probe: `RuntimeError` escapes) | materialize outside the `try` |
| `::test_hostile_key_and_token_objects_are_rejected_inside_the_boundary` | `str` subclasses whose `__eq__`, `__hash__`, `__bool__` raise, as keys and as values, both seams | `_tokenize.py:192, 214` | replace `type(x) is str` with `isinstance` |
| `tests/property/test_tokenize_parity.py::test_batch_and_per_row_emit_identical_tokens` | generated lists with duplicates, NFC pairs, nulls, preloaded hits | B-6 `test_tokenize.py:535-541` covers one example | skip NFC on the batch seam; include a hit in `put_many` |
| platform `tests/test_vault_batch_pg.py::test_two_sessions_interleave_lookup_many_and_put_many` | two sessions, barrier after both lookups miss, A commits | no batch seam on main | return the proposal instead of the re-read |
| `::test_partial_put_many_after_a_concurrent_commit` | A commits a subset between B's lookup and put | same | skip the re-read for conflict losers |
| `::test_batch_commit_failure_publishes_nothing` | `db.commit` raises | `test_vault_durability.py:167` covers the per-row seam only | call `write_v2_outputs` before the commit |
| platform `tests/test_vault_concurrency_pg.py::test_write_lock_is_reacquired_after_commit_and_rollback` | commit, then put, then inspect `pg_locks` from session 2 | `store.py:88, 160-165` (N1) | remove the event listeners |
| `tests/unit/determinism/test_hkdf.py::test_engine_vectors_are_unchanged_by_the_pyca_swap` | fixture captured on main `1132d29e` from the lock | n/a (behavior-preserving) | change the salt, info order or length handling |
| `tests/sentry/test_hkdf_is_pyca.py::test_hkdf_module_delegates_to_cryptography` | `_hkdf` imports `HKDF`, `HKDFExpand`, `HMAC` from `cryptography` and does not import `hashlib` | main `_hkdf.py:26-27` | re-add the stdlib loop |
| `tests/sentry/test_b6_note_currency.py::test_note_signatures_and_claims_match_code` | note quotes signatures; forbidden phrases | B-6 note lines 2, 93-96, 114-115 | re-add "job seed" |
| CLI `tests/unit/test_token_vault.py::test_put_returns_the_authoritative_token` | fresh insert and idempotent path both return `str` | CLI `_token_vault.py:223` returns `None` | return `None` on the idempotent path |
| CLI `::test_v1_vault_fails_closed_under_schema_v2` | a schema-1 file | passes today only because `_SCHEMA_VERSION` is 1 | accept version 1 |
| CLI `::test_derivation_scope_is_32_random_bytes_and_stable_across_reopen` | two creates differ; reopen returns the same | no scope exists | regenerate on open |

No threshold, slice bound, allowlist or census entry is widened by this plan. `vault.py` on A-4 is
525 lines; S4 is budgeted to stay under 600 and takes no census entry.

## 7. Owner questions

- Q1 (compatibility with existing tokenization vaults). The standing rule is pre-release, no
  backward compatibility, no migration. Confirm it applies to tokenization vaults minted before
  R-b (platform rows and CLI SQLite files), and choose: (a) hard delete (the v5 migration refuses
  while `token_mappings` has rows), or (b) tombstone (`token_derivation = 'tokenize/v0'`, new
  jobs refused, detokenize of old rows still works). Plan default pending the answer: (b).
- Q2 (goldens). Does the tokenize and hash domain separation move any pinned golden? Engine: no
  golden fixture contains a tokenize output (`grep -rl "tok_" tests/fixtures tests/golden` on main
  returns nothing); `hash`, `fpe`, generation and the file vault bytes are unchanged (D7). Platform:
  `tests/test_vault_e2e.py` and `test_token_vault_adapter.py` assert shape and round trip, not token
  values. CLI: `tests/e2e/test_tokenize_cli.py` asserts the prefix and reversal. The plan therefore
  moves no golden; confirm, or name the pin to re-record.
- Q3 (platform vault write failure). `v2_vault.py:78-80` documents that a file vault write failure
  does not fail the job. D1 reverses that (publication gated on the vault commit, per the finding).
  Confirm the reversal, since it changes a documented platform behavior.
- Q4 (CLI overwrite). D1 makes `decoy run --vault` refuse an existing vault file (`vault_exists`)
  instead of replacing it. Confirm, or ask for an explicit `--vault-overwrite` flag (not planned).

## 8. Repeat-class register and stop conditions

- F4 is the second appearance of class 1 frame-local secret material: the first was A-4 round 4
  (`_derive.py:282-309`, `vault.py` `_fernet`/`assert_keyed_with`, owner decision 2026-11-01 §A-4
  round 4). The fix is the shared construction (D4), not a local `del`. If a third instance of this
  class appears in any gate on this plan's slices, the builder stops and reports instead of patching.
- N1 is class 7 (check then use) on the platform store lock cache. No earlier gate on B-6 found
  class 7; it is the first appearance. A second appearance in S6 stops the slice.
- N2 and N3 are class 10 (a pin without its partner). Both are resolved by moving the pin and the
  behavior in one commit; a further class 10 finding in S4 or S7 stops that slice.

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

LOC (`wc -l`, same env): `vault.py` main 451 / A-4 525; `_tokenize.py` B-6 276; `_derive.py` A-4
419; `_adapter.py` B-6 431; `_hkdf.py` 95; `_local.py` A-4 378; `_output.py` A-4 389; `_opaque.py`
A-4 37.

## 10. Plan lint and done-when

Plan lint: no plan-lint tool exists in decoy-engine, decoy-platform or the CLI (searched
`plan-lint`, `plan_lint`, `planlint`, `lint_plan` over `*.py`, `*.toml`, `*.md`, `*.yml`, `*.sh`;
the only hits are two prose mentions in `decoy-platform/docs/ROADMAP.md`). The referenced
`/home/cam/dev-rules` directory is not present in this environment. This plan was checked by hand
against the required-content list in the task: every finding has a section, a slice, a named red
test with its seeded violation, fail-before location and mutation; the compatibility question is a
question (Q1); the version bumps and their recording places are in §5.

Done when: this file is on `plan/b6-vault-crypto-remediation`; the ROADMAP B-6 row in
decoy-platform links it (a one-line platform PR by the orchestrator, not this plan); the Codex plan
gate returns GO; S1-S7 then follow the order in §5.
