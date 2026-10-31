Status: plan only, revision 1. No implementation or release approval is granted by this document.

Summary: The token vault refuses any key shorter than the 32-byte mask root at construction and at load, `vault_writer_for_config` never keys a vault on the public 8-byte `job_seed`, and the `simpleeval` floor is raised to the locked version with a sentry that keeps floor and lock equal.

Baselines: engine `origin/main` `a6e5c252` (2026-10-31), planning branch `plan/slice0`. Every probe below ran in the `uv.lock` venv that `scripts/dev/bootstrap.sh` builds (Python 3.10.20, pyarrow 24.0.0, pandas 2.3.3, duckdb 1.5.4, cryptography from the lock). F13 citations were read at `origin/feat/f13-secret-provider` `dbb098f` (engine PR #339, r5, not merged).

Rules consulted: `DEFECT-CATALOG.md` and `OWNER-DECISIONS-2026-10-30.md` (decoy-platform `handoffs/queue`), the engine `CLAUDE.md`, `docs/compatibility-contract.md` sections 3.1, 3.2 and 3.4, `docs/security/token-vault.md`, `docs/security/de-02-keyprovider-design.md` (title only; the design is summarized in `keyprovider.py:1-33`).

# Slice 0, item 1: vault key floor (11.1) and the simpleeval floor (12.3), PLAN (revision 1)

Risk: R3 for slice A (a vault written under a seed-only config on main can no longer be opened by any engine that carries this change; the file format and the key label do not change). R2 for slice B (dependency floor). Gate: Codex on both; Dennis on slice A because it changes which persisted vaults open.

Threat model: V1 (self-hosted single-org, engine process trusted). In scope: an operator who follows the documented no-secret path and gets a re-identification database whose only protection is a 64-bit public seed. Out of scope: hostile in-process objects (a `KeyProvider` that lies about `mask_key()` length is still rejected by the floor, but one that returns 32 weak bytes is a V2 note).

## 1. Reproduction on main (yes)

Command, in the locked venv:

```
.venv/bin/python -c "
from decoy_engine.vault import VaultWriter, vault_writer_for_config
w = vault_writer_for_config({'version': 1, 'global_settings': {'seed': 42}, 'tables': []})
print(len(w._mask_key)); print(type(VaultWriter(b'x')).__name__)"
```

Output: `8` then `VaultWriter`. Code: `vault.py:349-355` resolves `mask_key = provider.mask_key() if provider is not None else job_seed`; `vault.py:216-219` stores any bytes; `vault.py:221-239` (`assert_keyed_with`) and `vault.py:312-331` (`assert_vault_writer_keyed`) compare keys, they never measure them; `vault.py:133-149` (`_fernet`) derives the Fernet key from whatever it is given; `load_vault` (`vault.py:358-452`) accepts the same 8-byte key. The masking side is gated by `require_mask_key` (`keyprovider.py:369-397`), which returns `job_seed` for a keyed plan while `is_pre_ga()` (`release.py:48-55`); the vault has no gate of its own.

simpleeval: `pyproject.toml:108` says `"simpleeval>=0.9.13"`; `uv.lock:2408-2413` resolves `1.0.7`. Advisory: CVE-2026-32640 (objects passed as names can leak modules or dangerous callables into the sandbox) is fixed in simpleeval 1.0.5, so any install that resolves the floor rather than the lock sits in the affected range. The engine's own scope hands simpleeval a non-module proxy and builtins (`expressions/_safe_eval.py:46-95`), and formulas come from the operator's config inside the V1 trust boundary, so the bump is housekeeping with an advisory-backed minimum, not a V1 blocker. Sources: Debian DSA-6220-1 (linuxsecurity.com/advisories/debian/debian-dsa-6220-1-simpleeval), GitLab advisory CVE-2026-32640 (advisories.gitlab.com/pkg/pypi/simpleeval/CVE-2026-32640/). The NVD record was not read; the worker re-checks the affected range before writing the sentry's comment.

## 2. Guarantee (invariants as properties)

- **P1 (floor).** No `VaultWriter` instance exists, and no vault file is written or read, under a key shorter than `MIN_SECRET_BYTES` (`keyprovider.py:58`). The refusal is `VaultError(code=weak_mask_secret)` and names no key bytes.
- **P2 (no seed key).** `vault_writer_for_config` never returns a writer keyed on `job_seed`, in any release phase. With no provider and no `mask_secret_ref` it raises `KeyedStrategyRequiresSecret` (`keyprovider.py:82-86`) before any entry is collected.
- **P3 (stable under a secret).** For a config that already resolves a `SecretKeyProvider`, the vault key, label (`vault.py:88`), file format (`vault.py:82`) and header are byte-identical to main. A vault written on main under a secret opens unchanged.
- **P4 (floor equals lock).** The `simpleeval` lower bound in `pyproject.toml` equals the version `uv.lock` resolves, enforced by a sentry.
- **P5 (one definition).** The floor value is `keyprovider.MIN_SECRET_BYTES`; the vault module defines no second number.

## 3. Contract decision

Chosen: fail closed, no derivation from the seed. A 64-bit public seed carries no secrecy for a KDF to amplify, so neither the vault nor anyone else may derive a vault key from it; the only key source is a `KeyProvider` whose `mask_key()` is at least 32 bytes, which today means `SecretKeyProvider` (`keyprovider.py:124-160`) through `run(key_provider=...)` or `global_settings.mask_secret_ref`. Rejected alternatives: HKDF over the seed (same 64 bits, false assurance), a vault-only minimum below 32 (a second number, violates P5), gating on `is_pre_ga()` (the vault is reversible plaintext PII whatever the phase; `keyprovider.py:307-323` already classifies every vault column as keyed).

Migration for existing vaults: pre-GA hard delete, as `release.py:35-45` already states for DE-02. A seed-keyed vault cannot be opened by the new engine (no 32-byte key decrypts it); `load_vault` refuses the 8-byte key with `weak_mask_secret` before reading the file, and the message says to re-mask under a secret. No dual-read path, no format bump, no header field.

## 4. Design (one structural fix per class)

- `vault.py`: one private `_require_vault_key(mask_key: bytes) -> None` raising `VaultError(code=reasons.runtime.WEAK_MASK_SECRET, message=...)` when `len(mask_key) < MIN_SECRET_BYTES`; called from `VaultWriter.__init__` (`vault.py:216`) and from `_fernet` (`vault.py:133`) after the `cryptography` import, so the absent-dependency ordering that `load_vault` pins (`vault.py:378-381`) is unchanged: crypto absent, then weak key, then unreadable file.
- `vault_writer_for_config` (`vault.py:334-355`): drop the `else job_seed` branch; when `provider is None` after ref resolution raise `KeyedStrategyRequiresSecret(_GA_MASK_KEY_REQUIRED_MSG)` (`keyprovider.py:359-367`, message reused verbatim). `_normalize_job_seed` stays only to keep the seed validation error parity the docstring promises.
- Docstrings and docs that state the seed fallback are corrected in the same slice: `vault.py:12-17`, `vault.py:30-38`, `vault.py:134-137`, `vault.py:217`, `vault.py:359-363`, `plan/_seed.py:112-121` (`job_seed_for_config` no longer describes what the vault keys on), `docs/security/token-vault.md:22-31` and `:63-68`, `docs/security/key-derivation.md` where it names the vault.
- Sentry for P4: `tests/sentry/test_dependency_floors.py`, stdlib `tomllib` (the pattern of `tests/sentry/test_ci_config.py:1-30`), parses the `simpleeval` specifier in `[project].dependencies` and the `[[package]] name = "simpleeval"` version in `uv.lock`, asserts the `>=` bound equals the locked version. `pyproject.toml:108` becomes `"simpleeval>=1.0.7"`; `uv lock` must produce no diff.
- F13 reconciliation (engine PR #339 owns the provider side; nothing here duplicates it). F13 replaces the ref resolution in `vault_writer_for_config` with `admit_mask_key` and removes the keyed `job_seed` fallback everywhere. This plan lands first and is smaller; on rebase F13 keeps `_require_vault_key` (admission does not cover a direct `VaultWriter(...)` or `load_vault(...)` call) and replaces the resolution lines. The test secret is the same file and bytes F13 introduces, `tests/_keyed_secret.py` with `THROWAWAY_KEY_PROVIDER`, so the rebase is a no-op there; this plan adds no autouse fixture (F13 owns that) and no config classifier.

## 5. Class table

| # | Class | Closing structure | Proving test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | `_require_vault_key` and `vault_writer_for_config` raise with fixed text; no key bytes, no ref text, no path in the message (the existing `_redact_ref` rule, `keyprovider.py:207-216`) | T4 seeded-marker: a 7-byte key whose bytes are a marker, and a `mask_secret_ref` whose name is a marker; assert the marker is absent from `str(exc)`, `exc.args`, `exc.message` | Format the key length together with the key bytes into the message: T4 fails |
| 2 | Cleanup missing on an exit path | `VaultWriter.__init__` raises before `_entries` exists; `write()` builds the Fernet before any chunk (`vault.py:276-278`) so a weak-key refusal leaves no partial file | T5 exit-path matrix: refusal at construction, at `write()` with a mutated key, at `load_vault` before and after the file read | Move the floor check after `Path(path).write_bytes`: a partial file appears and T5 fails |
| 3 | State machine and concurrency | N/A: the writer has one state (entries accumulate, then one write); no lock or epoch is added | | |
| 4 | Route parity and silent fallback | Every run entry that accepts a writer already calls `assert_vault_writer_keyed` (`_pipeline_context.py:311-315`, chunked entry per `vault.py:317-319`); the floor sits inside the writer, so no route can bypass it | T6 route matrix: `run_pipeline` on full_frame, sequential, out_of_core (forced) and `run_mask_pipeline_chunked`, each with a seed-keyed writer, each refused with the same code | Remove the `__init__` call and keep only the `_fernet` one: the resident route still fails but at `write()`, and T6's "before any masking" assertion fails |
| 5 | Referential integrity | N/A: the vault pairs source and masked values positionally per column (`vault.py:176-203`); no FK shape is read | | |
| 6 | Untrusted producer | The floor measures `len(bytes(mask_key))` after an `isinstance(mask_key, (bytes, bytearray))` check (the `SecretKeyProvider` rule, `keyprovider.py:139-143`); a provider returning a non-bytes value is refused with the same code | T7 field walk over `VaultWriter(mask_key)` and `load_vault(path, mask_key)` with `None`, `str`, `int`, 31 bytes, 32 bytes, `bytearray(32)` | Drop the isinstance check: a 32-character `str` passes `len` and T7 fails |
| 7 | TOCTOU on a path | N/A: the vault path handling is unchanged (`Path(path).write_bytes`, `read_bytes`); no check-then-use is added | | |
| 8 | Weak tests and overclaimed evidence | Red records for T1 to T8 recorded on `a6e5c252` before the fix; the 30 writer sites in 8 test files (section 7) are migrated to the shared secret with their keyed assertions intact (no skip, xfail or weakened assertion); baseline from `scripts/dev/gates.sh --baseline` | T8 non-vacuity: each new test fails against main with the expected code, captured in `docs/plans/evidence/2026-10-31-slice0-vault-red-baseline.txt` | Revert `_require_vault_key` to a no-op: T1, T2, T3, T6 fail |
| 9 | Behavior-preserving refactor drift | P3 characterization first: a vault written on main under `THROWAWAY_KEY_PROVIDER` is committed as a fixture and must open after the change | T3 fixture round trip | Change `VAULT_KEY_LABEL` or the HKDF info: T3 fails with `vault_key_mismatch` |
| 10 | Frozen contract drift | `decoy-vault/v2` and `vault-key/v1` untouched (section 8); the sentry pins floor equals lock | T9 header snapshot (`format`, `seed_protocol_version`, `ambiguous_dropped`, `chunk_rows`, `chunk_count`, nothing else) and T10 floor sentry | Add a header key, or set the floor to `>=1.0.6`: T9 or T10 fails |

## 6. Enumerations from the code

### 6.1 Exit paths

- `VaultWriter.__init__` (`vault.py:216-219`): new refusal before `_entries` is created.
- `VaultWriter.write` (`vault.py:245-306`): `_fernet` built before chunking (`vault.py:276-278`), so crypto-absent and (new) weak-key refusals leave no file; `write_bytes` is the one commit.
- `load_vault` (`vault.py:358-452`) in order: `vault_crypto_not_installed`, (new) `weak_mask_secret`, `vault_unreadable` (read, magic, header), `vault_format_unsupported`, `vault_protocol_version_mismatch`, `vault_key_mismatch`.
- `vault_writer_for_config` (`vault.py:334-355`): seed validation errors from `_normalize_job_seed`, `MaskSecretError` from `key_provider_from_ref` (`keyprovider.py:274-276`), (new) `KeyedStrategyRequiresSecret`.
- `unmask_pipeline` wraps `VaultError` into `ExecutionError` with the same code (`unmask.py:282-287`).

### 6.2 Routes

- `run_pipeline` resolves the provider once and calls `assert_vault_writer_keyed` before any route runs (`_pipeline_context.py:299-315`); the route itself (full_frame, sequential, out_of_core, mixed per `_pipeline_routing.py:312-320`) never touches the key.
- `run_mask_pipeline_chunked` carries the same guard (named at `vault.py:317-319`; the worker cites the chunked call site).
- `unmask_pipeline` keys `load_vault` from the same resolution rule (`unmask.py:245-257`); its GA gate (`unmask.py:272-278`) stays as is, this plan does not touch unmask resolution (F13 does).
- Consumers outside this repo: CLI `decoy run --vault` builds the writer with no provider argument (`decoy/src/decoy/cli/run.py:833-835`, resolves `mask_secret_ref` from the config); CLI vault inspector opens with the seed (`decoy/src/decoy/cli/report.py:2290-2305`); platform `_build_job_vault_writer` seeds the config and passes no provider (`decoy-platform/api/jobs/v2_vault.py:23-40`) and its runner passes no `key_provider` to `run_pipeline` (`decoy-platform/api/jobs/v2_runner.py:226-240`).

### 6.3 FK shapes

- N/A: the vault never reads a relationship; entries are per column and positional (`vault.py:176-203`), so single, composite, self-referencing and cross-batch FK shapes do not reach this code.
- The out-of-core vault path (`tests/parity/test_phase_d_d3b_vault_ooc.py:188`, `:271`) builds its writer through `vault_writer_for_config`, so it migrates with the rest.

### 6.4 Fields

- `VaultWriter._mask_key: bytes`, `VaultWriter._entries: set[tuple[str, str, str]]` (`vault.py:218-219`).
- `VaultError.code: str`, `VaultError.message: str` (`vault.py:124-130`); the header dict keys (`vault.py:284-290`).
- `KeyProvider.key_version: str`, `KeyProvider.mask_key() -> bytes` (`keyprovider.py:103-121`); `MIN_SECRET_BYTES` (`keyprovider.py:58`).

### 6.5 Output surfaces

- `VaultError` and `KeyedStrategyRequiresSecret` message text (fixed strings, no bytes, no ref, no path).
- The vault file itself (data plane, unchanged bytes under a secret).
- `UnmaskResult` reports carry `ambiguous_dropped` and codes, never key material (`unmask.py:282-287`).
- CLI stderr for `vault_*` codes (`decoy/src/decoy/cli/report.py:2302-2310`); platform `JobNodeRun.error` JSON on a failed vault write (`decoy-platform/api/jobs/v2_vault.py:89-98`) carries `str(exc)`, which this plan keeps value-free by construction of the messages.

## 7. Reuse inventory

- `keyprovider.MIN_SECRET_BYTES`, `reasons.runtime.WEAK_MASK_SECRET` (`reasons/runtime/_part3.py:254`), `reasons.runtime.KEYED_STRATEGY_REQUIRES_SECRET` (`reasons/runtime/_part2.py:319`), `_GA_MASK_KEY_REQUIRED_MSG`: no new reason code, no new message.
- `hmac.compare_digest` in `assert_keyed_with` (unchanged), `determinism.derive` and `VAULT_KEY_LABEL` (unchanged).
- F13's `tests/_keyed_secret.py` (`THROWAWAY_SECRET`, 38 bytes; `THROWAWAY_KEY_PROVIDER`): copied verbatim so both branches carry the same file.
- `tests/sentry/test_reason_registry.py` (codes only from the registry) and `tests/sentry/test_module_size.py` (vault.py is 452 LOC, stays under 600).
- Test sites to migrate (count of `VaultWriter(b...)` or `vault_writer_for_config(` on main): `tests/unit/test_vault.py` 11, `tests/unit/execution/test_auto_chunk_dispatcher.py` 5, `tests/parity/test_phase_d_d3b_vault_ooc.py` 4, `tests/unit/test_de02_keyprovider.py` 3, `tests/unit/execution/test_auto_chunk_routing.py` 3, `tests/native/test_chunked_entry_side_channels.py` 2, `tests/unit/execution/test_multi_table_gates.py` 1, `tests/unit/execution/test_b6a_publish.py` 1; plus `tests/native/_b8_support.py`, `tests/integration/compat_corpus/regenerate.py` and the route tests that import them. Each migrated test passes `key_provider=THROWAWAY_KEY_PROVIDER` to both the writer and the run; `test_pre_ga_keyed_no_secret_falls_back_to_seed` (`test_de02_keyprovider.py:583-585`) does not involve the vault and stays unchanged.

## 8. Frozen contracts touched

- `decoy-vault/v2` file format and `vault-key/v1` label (`docs/compatibility-contract.md` 3.1, 3.2): not changed; readability of seed-keyed files ends (pre-GA hard delete, no vault in the wild per 3.2).
- Public API (3.4): `VaultWriter(mask_key)`, `vault_writer_for_config(config, *, key_provider=None)`, `load_vault(path, mask_key)` keep their signatures; behavior narrows (breaking, pre-GA). `job_seed_for_config` stays exported with a corrected docstring.
- `pyproject.toml` dependency floor: additive tightening.

## 9. Acceptance tests, red first (hostile corpus)

Commands run from the repo root in the locked venv; every new test is recorded red on `a6e5c252` first.

- T1 `tests/unit/test_vault.py::test_writer_refuses_short_key[len=0,1,8,31]`: `VaultWriter(key)` raises `VaultError` with `code == "weak_mask_secret"`; 32 and 64 bytes construct.
- T2 `::test_vault_writer_for_config_refuses_seed_only_config`: seed-only config raises `KeyedStrategyRequiresSecret`; with `key_provider=THROWAWAY_KEY_PROVIDER` the writer key equals `THROWAWAY_KEY_PROVIDER.mask_key()`.
- T3 `::test_secret_keyed_vault_from_main_still_opens`: fixture vault written on main under the throwaway secret (committed under `tests/fixtures/vault/`), `load_vault` returns the expected map.
- T4 `::test_refusal_text_carries_no_key_or_ref_marker`: marker bytes and a marker env name never appear in the exception.
- T5 `::test_exit_paths_leave_no_partial_file`: refusal at `write()` after `object.__setattr__`-free mutation (a writer built with 32 bytes, then `load_vault` with 8 bytes on that file) leaves the file untouched; a weak key refused at construction creates nothing.
- T6 `tests/unit/test_de02_keyprovider.py::test_every_run_entry_refuses_weak_vault_writer_before_masking`: full_frame, sequential, forced out_of_core, chunked; the refusal arrives before any strategy handler runs (spy on the adapter).
- T7 `::test_vault_key_field_walk`: the type corpus from the class table.
- T8 red baseline file as named in the class table.
- T9 `::test_vault_header_keys_are_frozen`.
- T10 `tests/sentry/test_dependency_floors.py::test_simpleeval_floor_equals_lock`.
- Command: `.venv/bin/python -m pytest tests/unit/test_vault.py tests/unit/test_de02_keyprovider.py tests/sentry/test_dependency_floors.py -q`; full gate: `scripts/dev/gates.sh`.

## 10. Slices

- **Slice A (engine, R3, about 60 LOC of code plus the test migration):** `_require_vault_key`, the two call sites, `vault_writer_for_config` fail-closed branch, docstring and docs corrections, `tests/_keyed_secret.py`, T1 to T9, migration of the sites in section 7. Ships alone. Expected rounds: at most two (the only known risk is a missed writer site in a parity or native test, which the full gate exposes in round one).
- **Slice B (engine, R2, 2 LOC plus one sentry):** `pyproject.toml:108` floor, T10, `uv lock` no-op check. Ships alone, independent of A.

## 11. Pre-mortem

- A reviewer finds a run entry that builds a writer internally from the seed: none exists on main (the only `VaultWriter(` constructor call under `src` is `vault.py:355`; `_adapter.py:87` is the unrelated `TokenVaultWriter` protocol); T6 covers the public entries.
- The platform's vault jobs break: yes, by design, until the platform passes a `key_provider` (it already has a master key, `decoy-platform/api/keys/derive.py:77-86`). This is owner question Q1, and it is why F13 slice 4 (platform) sequencing matters.
- The CLI vault inspector breaks on every vault: yes, `decoy/src/decoy/cli/report.py:2305` keys on the seed; owner question Q2.
- A hostile `KeyProvider` returns 32 bytes of zeros: accepted by the floor (V2 note, out of the V1 model).
- Someone proposes a vault-only minimum of 16 bytes to keep tests green: refused by P5 and the worker rules.
- The simpleeval sentry blocks a routine `uv lock --upgrade`: intended; the bump is a one-line pyproject change in the same commit.

## 12. Open owner questions

- Q1: Does the platform pass a `SecretKeyProvider` derived from its master key before this lands (platform change, F13 slice 4), or does slice A wait for it? Recommendation: land slice A now; platform vault jobs fail closed with a clear code until F13 slice 4.
- Q2: CLI `decoy report --vault` inspector (`decoy/src/decoy/cli/report.py:2290-2305`) must take the secret ref; CLI follow-up, not in this plan.
- Q3: R3 classification confirmed, or downgraded to R2 because no vault exists in the wild?

## 13. Worker evidence rules

Name the class of every finding and fix the class, not the instance. Write the generated tests first and record them red (T8 file). Before pushing, attack your own diff: mutate each new mechanism once and show the test goes red. Run baselines with `uv.lock` in the same environment as the branch (`scripts/dev/bootstrap.sh`, `scripts/dev/gates.sh --baseline`) and state what you did not run. Never widen a threshold, skip a test or weaken a keyed assertion to make the migration pass. If a defect of a class already in the catalog appears a second time, stop and report it rather than patch again.
