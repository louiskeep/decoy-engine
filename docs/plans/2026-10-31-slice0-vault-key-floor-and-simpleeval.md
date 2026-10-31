Status: plan only, revision 2 (Codex round 1 remediation). No implementation or release approval is granted by this document.

Summary: The token vault refuses any key shorter than the 32-byte mask root at construction, at write and at load, stores key material as immutable `bytes`, `vault_writer_for_config` never keys a vault on the public 8-byte `job_seed` (keyed or not), the committed compatibility-corpus vault is regenerated under a 32-byte synthetic key, and the `simpleeval` floor is raised to the advisory-fixed release with a sentry that proves the lock satisfies it.

Baselines: engine `origin/main` `6ce435ce` (2026-10-31, F13 #339 merged), planning branch `plan/slice0` rebased onto it. Every probe below was re-run on `6ce435ce` in the `uv.lock` venv that `scripts/dev/bootstrap.sh` builds (Python 3.10.20, pyarrow 24.0.0, pandas 2.3.3, duckdb 1.5.4, simpleeval 1.0.7, cryptography from the lock). Consumer citations: decoy-platform `main` `aad2de7`, decoy (CLI) `main` `d28fae2`.

Rules consulted: dev-rules `00-universal`, `risk-and-exceptions`, `development-loop` (R2 plan contract, Established solutions), `bugfix`, `security`, `config-secrets-and-supply-chain`, `data-and-migrations`, `api-and-compatibility`, `testing`; `DEFECT-CATALOG.md`, `OWNER-DECISIONS-2026-10-30.md` and `HOSTILE-PLAN-REVIEW.md` (decoy-platform `handoffs/queue`); the engine `CLAUDE.md`; `docs/compatibility-contract.md` sections 3.1, 3.2 and 3.4; `docs/security/token-vault.md`; `docs/security/key-derivation.md`; `release.py:30-45` (DE-02 pre-GA vault rule).

# Slice 0, item 1: vault key floor (11.1) and the simpleeval floor (12.3), PLAN (revision 2)

Risk: R2 for both slices (security and compatibility behavior change; no deletion, no credential rotation, no irreversible production mutation; consumers run pinned engine releases). The one R3 activity in this area, disposing of or regenerating a real seed-keyed vault that exists outside the repos, is NOT in this plan: it needs Cam's explicit authorization first (section 13, Q1) and would carry a Dennis gate if it is ever scheduled. Gate: Codex on both slices.

Threat model: V1 (self-hosted single-org, engine process trusted). In scope: an operator or a direct library caller who gets a re-identification database whose only protection is a 64-bit public seed, and an ordinary caller whose mutable key buffer drifts after admission. Out of scope: hostile in-process objects (a `KeyProvider` that returns 32 weak bytes is a V2 note).

## 1. Reproduction on main (yes, narrower after F13)

Command, in the locked venv on `6ce435ce` (`scratchpad/probe_vault.py`, not committed):

```
.venv/bin/python -c "
from decoy_engine.vault import VaultWriter, vault_writer_for_config, load_vault
w = vault_writer_for_config({'version': 1, 'global_settings': {'seed': 42}, 'tables': []})
print(len(w._mask_key)); print(type(VaultWriter(b'x')).__name__)
ba = bytearray(b'\x07' * 32); w2 = VaultWriter(ba); ba[0] = 0; print(w2._mask_key is ba, w2._mask_key[0])"
```

Output: `8`, `VaultWriter`, `True 0`. A seed-only `vault: true` config now raises `KeyedStrategyRequiresSecret` (`keyed_strategy_requires_secret`) from `vault_writer_for_config` because F13 admission classifies every `vault: true` column as keyed (`secret_providers/_requirements.py:189-193`) and `vault_writer_for_config` admits through `admit_mask_key` (`vault.py:345-353`). The residual seed-keyed surface on main is therefore: `vault_writer_for_config` on a config with no keyed column (`vault.py:353`, `else job_seed`); a direct `VaultWriter(bytes)` of any length (`vault.py:216-219`, which also stores the caller's object by reference); `load_vault(path, key)` with any key (`vault.py:357-383`, `_fernet` at `vault.py:133-149` derives from whatever it is given); `assert_keyed_with` and `assert_vault_writer_keyed` (`vault.py:221-239`, `:312-331`) compare keys and never measure them. An 8-byte-keyed vault written through `VaultWriter(b"\x01" * 8)` writes and opens (probed). The committed compatibility corpus vault `tests/integration/compat_corpus/decoy-vault-v2__synthetic.vault` is seed-keyed: `regenerate.py:81-83` builds `VaultWriter(_SYNTHETIC_JOB_SEED)` with an 8-byte seed and `manifest.json` carries `job_seed_hex`, which `test_compat_corpus.py:53` and `:115` read.

Consumers on their pinned engines: the platform builds every job vault from a seed alone (`decoy-platform/api/jobs/v2_vault.py:23-40`, `derive_mask_job_seed_int` from the master key, `api/keys/derive.py:77-86`) and passes no `key_provider` to `run_pipeline` (`api/jobs/v2_runner.py:225-232`); the platform pins `decoy-engine==0.16.0` (`decoy-platform/pyproject.toml:36`). The CLI builds the writer from the config only (`decoy/src/decoy/cli/run.py:831-835`) and the inspector opens every vault with the seed (`decoy/src/decoy/cli/report.py:2290-2305`). Against engine main both already fail closed at creation for `vault: true` configs (F13 admission); the inspector still opens seed-keyed files.

simpleeval: `pyproject.toml:108` says `"simpleeval>=0.9.13"`; `uv.lock:567`, `:667` resolve `1.0.7`. Advisory GHSA-44vg-5wv2-h2hg / CVE-2026-32640 (objects passed as names can leak modules or dangerous callables into the sandbox) is fixed in simpleeval 1.0.5, so any install that resolves the floor rather than the lock sits in the affected range. The engine's own scope hands simpleeval a non-module proxy and builtins (`expressions/_safe_eval.py:46-95`), and formulas come from the operator's config inside the V1 trust boundary, so the bump is housekeeping with an advisory-backed minimum, not a V1 blocker. Sources: the GitHub advisory above, upstream releases (github.com/danthedeckie/simpleeval/releases), Debian DSA-6220-1. The worker re-checks the fixed version before writing the sentry's comment; the review notes PyPI now has 1.0.8, which the floor contract below tolerates without an edit.

## 2. Established solutions

- Key floor: Fernet (`cryptography`) takes exactly 32 key bytes and the repo already derives it with HKDF-SHA256 (`determinism.derive`, `vault.py:148`); NIST SP 800-108 KDFs cannot add entropy to an 8-byte IKM, so the only sound floor is on the input. The floor value and the refusal pattern are copied from `SecretKeyProvider.__post_init__` (`keyprovider.py:135-143`, `MIN_SECRET_BYTES` at `keyprovider.py:54`) and from F13's `require_mask_key` (`keyprovider.py:209-227`, which already refuses a resolved key shorter than the root). No new KDF, cipher or constant.
- Immutable key material: normalize once to `bytes` at the boundary, the pattern `SecretKeyProvider.mask_key` uses when it calls `hkdf_sha256(ikm=bytes(self.secret), ...)` (`keyprovider.py:150-156`) and what `hmac.compare_digest` expects. Storing the caller's buffer by reference is the defect.
- Dependency floor: a PEP 440 lower bound at the first fixed release named by the advisory, with the lock pinned independently (pip-audit and the GitHub advisory database both express the fix as "patched in 1.0.5"); a sentry that the lock satisfies the floor is the `uv lock --check` idea written as a test, parsed with stdlib `tomllib` the way `tests/sentry/test_ci_config.py:1-30` parses CI config, compared with `packaging.version.Version` (in the lock).
- Fail closed instead of fallback: F13's "no job_seed fallback exists in any release phase" (`keyprovider.py:201-207`), reused verbatim as the refusal text.

## 3. Guarantee (invariants as properties)

- **P1 (floor).** No `VaultWriter` instance exists, and no vault file is written or read, under a key shorter than `MIN_SECRET_BYTES`. The refusal is `VaultError(code=weak_mask_secret)` and names no key bytes.
- **P2 (no seed key).** `vault_writer_for_config` never returns a writer keyed on `job_seed`, in any release phase and for any config, keyed or not. With no admitted provider it raises `KeyedStrategyRequiresSecret` before any entry is collected.
- **P3 (stable under a secret).** For a config that already resolves a `SecretKeyProvider`, the vault key, label (`vault.py:88`), file format (`vault.py:82`) and header are byte-identical to main. A vault written on main under a 32-byte key opens unchanged.
- **P4 (floor contract).** The `simpleeval` lower bound in `pyproject.toml` is at least the advisory-fixed release (1.0.5), and the version `uv.lock` resolves satisfies that bound. Both are sentry-enforced; a lock upgrade never moves the floor.
- **P5 (one definition).** The floor value is `keyprovider.MIN_SECRET_BYTES`; the vault module defines no second number.
- **P6 (immutable key).** After admission a writer's key is a `bytes` object that no caller-held buffer can change; the same holds for `SecretKeyProvider.secret`.

## 4. Contract decision and coordinated release

Chosen: fail closed, no derivation from the seed. A 64-bit public seed carries no secrecy for a KDF to amplify; the only key source is a `KeyProvider` whose `mask_key()` is at least 32 bytes, which today means `SecretKeyProvider` through `run(key_provider=...)` or `global_settings.mask_secret_ref`. Rejected alternatives: HKDF over the seed (same 64 bits), a vault-only minimum below 32 (violates P5), gating on `is_pre_ga()` (`release.py:48-55`; the vault is reversible plaintext PII whatever the phase).

Migration for existing vaults: pre-GA hard delete, as `release.py:35-45` already states for DE-02 and as `docs/compatibility-contract.md:106-122` (3.2) allows only while no vault exists in the wild. A seed-keyed vault cannot be opened by the new engine; `load_vault` refuses the 8-byte key with `weak_mask_secret` before reading the file. No dual-read path, no format bump, no header field.

Evidence on seed-keyed vaults: inside the repos the only one is the synthetic corpus artifact, regenerated in slice A. Outside the repos it is not provable: every platform `vault: true` job on the pinned 0.16.0 engine writes one to `<upload_dir>/vaults` (`v2_vault.py:40`, `:43-47`) and every CLI `decoy run --vault` on its pinned engine does the same. The plan therefore does not decide the fate of those files; it records the question for Cam (section 13, Q1) and states that "old-engine-only recovery" (unmask with the pinned engine before upgrading) is the default consequence of landing slice A without a migration.

Coordinated release prerequisites (each separately owned; order matters):

1. Engine slice A (this plan; owner: engine worker; gate: Codex). Safe to land alone: every consumer runs a pinned engine release, so nothing breaks on merge.
2. Platform F13 slice 4 (owner: platform; must land before the platform bumps its engine pin past slice A): `_build_job_vault_writer` passes `key_provider=` a `SecretKeyProvider` derived from the master key (the `api/keys/derive.py:77-86` HKDF pattern) and `v2_runner.py:225-232` passes the same provider to `run_pipeline`. Without it every vault job fails closed at creation (already the case against engine main through F13 admission).
3. CLI creation and inspection (owner: CLI; must land before the CLI bumps its pin): `run.py:831-835` passes the provider the run resolved from `--mask-secret` or `mask_secret_ref`; `report.py:2290-2305` resolves the secret the same way `decoy unmask` does through engine `unmask_pipeline` (`unmask.py:247-257`) instead of `job_seed_for_config`.

The consumer pin bump is the release gate for 2 and 3; neither is in this plan's slices.

## 5. Design (one structural fix per class)

- `vault.py`: one private `_require_vault_key(mask_key: object) -> bytes` that refuses a non-`bytes`/`bytearray` value and a value shorter than `MIN_SECRET_BYTES` with `VaultError(code=reasons.runtime.WEAK_MASK_SECRET, message=<fixed text>)`, and returns `bytes(mask_key)`. Called from `VaultWriter.__init__` (`vault.py:216-219`, which stores the returned `bytes`) and from `_fernet` (`vault.py:133`) after the `cryptography` import, so the absent-dependency ordering that `load_vault` pins (`vault.py:377-381`) is unchanged: crypto absent, then weak key, then unreadable file.
- `vault_writer_for_config` (`vault.py:334-355`): drop the `else job_seed` branch; when the admitted provider is `None` raise `KeyedStrategyRequiresSecret(KEY_REQUIRED_GUIDANCE)` (`keyprovider.py:201-207`, text reused verbatim). `_normalize_job_seed` stays only for the seed validation error parity the docstring promises.
- `SecretKeyProvider.__post_init__` (`keyprovider.py:135-143`): after the existing checks, `object.__setattr__(self, "secret", bytes(self.secret))` so a mutable secret cannot drift after admission (same class as the writer key, 1 LOC, F13 territory; noted for the F13 owner).
- Compatibility corpus: `regenerate.py:81-83` keys the synthetic vault with a fixed 32-byte synthetic value; `manifest.json` carries `mask_key_hex` instead of `job_seed_hex`; `test_compat_corpus.py:53` and `:115` read the new field; the regenerated artifact is committed. This artifact is also the P3 characterization fixture (one fixture, not two).
- Docstrings and docs corrected in the same slice: `vault.py:12-17`, `:30-38`, `:134-137`, `:217`, `:359-363`; `plan/_seed.py:1-12` (the vault no longer routes its key through the seed); `docs/security/token-vault.md:22-31` and `:63-68`.
- Sentry for P4: `tests/sentry/test_dependency_floors.py` parses the `simpleeval` specifier in `[project].dependencies` and the locked version (`[[package]] name = "simpleeval"`), asserts `Version(floor) >= Version("1.0.5")` (advisory id in the comment) and `Version(locked) >= Version(floor)`. `pyproject.toml:108` becomes `"simpleeval>=1.0.5"`; `uv lock` must produce no diff.
- F13 reconciliation (merged): admission covers the config-level entries; the floor covers direct constructors and `load_vault`. `tests/_keyed_secret.py` is imported, never copied; the autouse fixture `_default_throwaway_secret` (`tests/conftest.py:256-290`) is untouched; every test that asserts no-secret behavior carries `@pytest.mark.no_default_secret` (`pyproject.toml:410`).

## 6. Class table

| # | Class | Closing structure | Proving test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | `_require_vault_key` and `vault_writer_for_config` raise fixed text; no key bytes, no ref text, no path in the message (`exc.args`, `__cause__`, `__notes__` included) | T4 seeded-marker: a 7-byte key whose bytes are a marker, a 40-byte `bytearray` marker, and a `mask_secret_ref` whose name is a marker; assert the marker is absent from `str(exc)`, `exc.args`, `exc.message`, the cause chain | Format the key length together with the key bytes into the message: T4 fails |
| 2 | Cleanup missing on an exit path | `VaultWriter.__init__` raises before `_entries` exists; `write()` builds the Fernet (`vault.py:278`) before any chunk and commits with one `write_bytes` (`vault.py:305`), so a weak-key refusal leaves no partial file | T5 exit-path matrix: refusal at construction creates nothing; a valid 32-byte writer whose `_mask_key` is then set to 8 bytes by ordinary attribute assignment raises `weak_mask_secret` from `write()` with no file created, and with a pre-existing file at the path its sha256 is unchanged; `load_vault` with a weak key refuses before the read (missing path still reports `weak_mask_secret`, not `vault_unreadable`) | Move the `_fernet` call after `Path(path).write_bytes`: a partial or altered file appears and T5 fails |
| 3 | State machine and concurrency | N/A: the writer has one state (entries accumulate, then one write); no lock or epoch is added | | |
| 4 | Route parity and silent fallback | Every run entry that accepts a writer already calls `assert_vault_writer_keyed` (`_pipeline_context.py:325-327`; chunked entry per `vault.py:317-319`); the floor sits inside the writer, so no route can bypass it | T6 route matrix: `run_pipeline` on full_frame, sequential, forced out_of_core and `run_mask_pipeline_chunked`, each with an 8-byte-keyed writer, each refused with `weak_mask_secret` at construction, before any strategy handler runs | Remove the `__init__` call and keep only the `_fernet` one: the resident route fails at `write()` instead, and T6's "before any masking" assertion fails |
| 5 | Referential integrity | N/A: the vault pairs source and masked values positionally per column (`vault.py:176-203`); no FK shape is read | | |
| 6 | Untrusted producer | `_require_vault_key` is an exact-type check (`bytes`, `bytearray`) plus the length floor, then a copy to `bytes`; `SecretKeyProvider` freezes its secret the same way | T7 field walk over `VaultWriter(key)` and `load_vault(path, key)` with `None`, `str`, `int`, `memoryview`, 31 bytes, 32 bytes, `bytearray(32)`; then mutate the admitted `bytearray` and prove the writer's key, the written file and `assert_keyed_with` are unchanged; same mutation walk for `SecretKeyProvider(bytearray(...))` | Drop the `bytes(...)` copy: the mutation cell fails. Drop the isinstance check: a 32-character `str` passes `len` and T7 fails |
| 7 | TOCTOU on a path | N/A: the vault path handling is unchanged (`Path(path).write_bytes`, `read_bytes`); no check-then-use is added | | |
| 8 | Weak tests and overclaimed evidence | Red records for T1 to T11 recorded on `6ce435ce` before the fix, with `no_default_secret` on every no-secret test so the F13 autouse fixture cannot make it green; the writer sites in section 8 are migrated with their keyed assertions intact (no skip, xfail or weakened assertion); baseline from `scripts/dev/gates.sh --baseline` | T8 non-vacuity: each new test fails against main with the expected code, captured in `docs/plans/evidence/2026-10-31-slice0-vault-red-baseline.txt` | Revert `_require_vault_key` to a pass-through: T1, T2, T5, T6, T7 fail |
| 9 | Behavior-preserving refactor drift | P3 characterization first: the regenerated corpus vault (32-byte synthetic key) is committed before the floor lands and must open after it | T3 corpus round trip through `test_compat_corpus.py` | Change `VAULT_KEY_LABEL` or the HKDF info: T3 fails with `vault_key_mismatch` |
| 10 | Frozen contract drift | `decoy-vault/v2` and `vault-key/v1` untouched (section 9); the dependency sentry pins floor >= advisory and lock >= floor | T9 header snapshot (`format`, `seed_protocol_version`, `ambiguous_dropped`, `chunk_rows`, `chunk_count`, nothing else) and T10 floor sentry | Add a header key, or set the floor to `>=1.0.4`, or edit the lock to `1.0.3`: T9 or T10 fails |

## 7. Enumerations from the code

### 7.1 Exit paths

- `VaultWriter.__init__` (`vault.py:216-219`): new refusal before `_entries` is created.
- `VaultWriter.write` (`vault.py:245-306`): `_fernet` built before chunking (`vault.py:278`), so crypto-absent and (new) weak-key refusals leave no file; `write_bytes` (`vault.py:305`) is the one commit.
- `load_vault` (`vault.py:357-451`) in order: `vault_crypto_not_installed`, (new) `weak_mask_secret`, `vault_unreadable` (read, magic, header), `vault_format_unsupported`, `vault_protocol_version_mismatch`, `vault_key_mismatch`.
- `vault_writer_for_config` (`vault.py:334-355`): seed validation errors from `_normalize_job_seed`, admission errors from `admit_mask_key`, (new) `KeyedStrategyRequiresSecret` when no provider was admitted.
- `unmask_pipeline` wraps `VaultError` into `ExecutionError` with the same code (`unmask.py:280-285`).

### 7.2 Routes

- `run_pipeline` admits the provider once (`_pipeline_context.py:305-310`) and calls `assert_vault_writer_keyed` before any route runs (`_pipeline_context.py:325-327`); the route itself (full_frame, sequential, out_of_core, mixed per `_pipeline_routing.py:312-320`) never touches the key.
- `run_mask_pipeline_chunked` (`_chunked.py:369-381`) carries the same guard (named at `vault.py:317-319`; the worker cites the chunked call site).
- `unmask_pipeline` admits the same way (`unmask.py:247-257`), keeps its keyed-reversal gate (`unmask.py:268-276`) and calls `load_vault` with the admitted key (`unmask.py:280-285`); this plan does not touch unmask resolution.
- Consumers outside this repo: CLI `decoy run --vault` (`decoy/src/decoy/cli/run.py:831-835`), CLI vault inspector (`report.py:2290-2305`), platform `_build_job_vault_writer` (`decoy-platform/api/jobs/v2_vault.py:23-40`) and `v2_runner.py:225-232`; all listed in section 4 as release prerequisites.

### 7.3 FK shapes

- N/A: the vault never reads a relationship; entries are per column and positional (`vault.py:176-203`), so single, composite, self-referencing and cross-batch FK shapes do not reach this code.
- The out-of-core vault path (`tests/parity/test_phase_d_d3b_vault_ooc.py`, 6 writer sites) builds its writer through `vault_writer_for_config`, so it migrates with the rest.

### 7.4 Fields

- `VaultWriter._mask_key: bytes` (now always `bytes`), `VaultWriter._entries: set[tuple[str, str, str]]` (`vault.py:218-219`).
- `VaultError.code: str`, `VaultError.message: str` (`vault.py:124-130`); the header dict keys (`vault.py:284-290`).
- `KeyProvider.key_version: str`, `KeyProvider.mask_key() -> bytes` (`keyprovider.py:100-118`); `SecretKeyProvider.secret: bytes` (frozen to `bytes` at `__post_init__`); `MIN_SECRET_BYTES` (`keyprovider.py:54`).

### 7.5 Output surfaces

- `VaultError` and `KeyedStrategyRequiresSecret` message text (fixed strings, no bytes, no ref, no path).
- The vault file itself (data plane, unchanged bytes under a secret).
- `UnmaskResult` reports carry `ambiguous_dropped` and codes, never key material (`unmask.py:280-285`).
- CLI stderr for `vault_*` codes (`decoy/src/decoy/cli/report.py:2302-2310`); platform `JobNodeRun.error` JSON on a failed vault write (`decoy-platform/api/jobs/v2_vault.py:89-98`) carries `str(exc)`, which this plan keeps value-free by construction of the messages.

## 8. Reuse inventory

- `keyprovider.MIN_SECRET_BYTES`, `reasons.runtime.WEAK_MASK_SECRET` (`reasons/runtime/_part3.py:254`), `reasons.runtime.KEYED_STRATEGY_REQUIRES_SECRET` (`reasons/runtime/_part2.py:319`), `KEY_REQUIRED_GUIDANCE` (`keyprovider.py:201`): no new reason code, no new message.
- `hmac.compare_digest` in `assert_keyed_with` (unchanged), `determinism.derive` and `VAULT_KEY_LABEL` (unchanged).
- `tests/_keyed_secret.py` (`THROWAWAY_SECRET`, 38 bytes; `THROWAWAY_KEY_PROVIDER`), imported; the `no_default_secret` marker (`pyproject.toml:410`, `tests/conftest.py:288`).
- `tests/sentry/test_reason_registry.py` (codes only from the registry), `tests/sentry/test_module_size.py` (vault.py is 451 LOC, stays under 600), `tests/sentry/test_ci_config.py` (tomllib pattern), `packaging.version`.
- Test sites to migrate (count of `VaultWriter(` plus `vault_writer_for_config(` per file on `6ce435ce`): `tests/unit/test_vault.py` 11, `tests/parity/test_phase_d_d3b_vault_ooc.py` 6, `tests/unit/test_de02_keyprovider.py` 6, `tests/unit/execution/test_auto_chunk_dispatcher.py` 6, `tests/native/test_chunked_entry_side_channels.py` 5, `tests/unit/execution/test_auto_chunk_routing.py` 3, `tests/unit/execution/test_multi_table_gates.py` 2, `tests/unit/execution/test_b6a_publish.py` 2, `tests/native/test_chunked_entry_parity_matrix.py` 2, and one each in `tests/unit/execution/test_engine_transforms_admission.py`, `test_engine_transforms_routes.py`, `tests/native/_b8_support.py`, `tests/native/test_chunked_entry_rev8.py`, `tests/native/test_unconfigured_passthrough.py`, `tests/integration/compat_corpus/regenerate.py`. The 8-byte literals in `tests/unit/test_vault.py:222-334` (`VaultWriter(b"\x01" * 8)`, `load_vault(path, b"\x02" * 8)`) become 32-byte literals with the same wrong-key semantics; each config-driven site passes `key_provider=THROWAWAY_KEY_PROVIDER` to both the writer and the run.

## 9. Frozen contracts touched

- `decoy-vault/v2` file format and `vault-key/v1` label (`docs/compatibility-contract.md` 3.1, 3.2): not changed; readability of seed-keyed files ends (pre-GA hard delete, legal only while no vault exists in the wild per 3.2, which is the open question Q1).
- Public API (3.4): `VaultWriter(mask_key)`, `vault_writer_for_config(config, *, key_provider=None)`, `load_vault(path, mask_key)` keep their signatures; behavior narrows (breaking, pre-GA). `job_seed_for_config` stays exported with a corrected docstring.
- `pyproject.toml` dependency floor: additive tightening.

## 10. Acceptance tests, red first (hostile corpus)

Commands run from the repo root in the locked venv; every new test is recorded red on `6ce435ce` first. Every test that asserts no-secret behavior carries `@pytest.mark.no_default_secret`.

- T1 `tests/unit/test_vault.py::test_writer_refuses_short_key[len=0,1,8,31]`: `VaultWriter(key)` raises `VaultError` with `code == "weak_mask_secret"`; 32 and 64 bytes construct and store `bytes`.
- T2 `::test_vault_writer_for_config_refuses_seed_only_config` (`no_default_secret`): a config with no keyed column and one with a `vault: true` column both raise `KeyedStrategyRequiresSecret`; with `key_provider=THROWAWAY_KEY_PROVIDER` the writer key equals `THROWAWAY_KEY_PROVIDER.mask_key()`.
- T3 `tests/integration/compat_corpus/test_compat_corpus.py`: the regenerated corpus vault opens under `mask_key_hex` and yields `expected_mapping`; the tamper case still reports `vault_key_mismatch`.
- T4 `::test_refusal_text_carries_no_key_or_ref_marker`: marker bytes, a marker `bytearray` and a marker env name never appear in the exception, its args, or its cause chain.
- T5 `::test_exit_paths_leave_no_partial_file`: the four cells from the class table, including the `write()`-after-mutation cell with a sha256 check on a pre-existing file.
- T6 `tests/unit/test_de02_keyprovider.py::test_every_run_entry_refuses_weak_vault_writer_before_masking`: full_frame, sequential, forced out_of_core, chunked; the refusal arrives at writer construction, before any strategy handler runs (spy on the adapter).
- T7 `::test_vault_key_field_walk_and_mutation`: the type corpus and the two mutation-after-admission cells (writer and `SecretKeyProvider`).
- T8 red baseline file as named in the class table.
- T9 `::test_vault_header_keys_are_frozen`.
- T10 `tests/sentry/test_dependency_floors.py::test_simpleeval_floor_is_advisory_fixed_and_lock_satisfies_it`.
- T11 `tests/unit/test_de02_keyprovider.py::test_secret_key_provider_freezes_mutable_secret`: `SecretKeyProvider(bytearray(...))`, mutate the buffer, `mask_key()` unchanged.
- Command: `.venv/bin/python -m pytest tests/unit/test_vault.py tests/unit/test_de02_keyprovider.py tests/integration/compat_corpus tests/sentry/test_dependency_floors.py -q`; full gate: `scripts/dev/gates.sh`.

## 11. Slices

- **Slice A (engine, R2, about 70 LOC of code plus the test migration and the regenerated corpus artifact):** `_require_vault_key`, the two call sites, `vault_writer_for_config` fail-closed branch, the `SecretKeyProvider` freeze, docstring and docs corrections, corpus regeneration and manifest field rename, T1 to T9 and T11, migration of the sites in section 8. Ships alone against pinned consumers. Release prerequisites for the consumer pin bump are items 2 and 3 of section 4, owned outside this plan. Expected rounds: at most two (the known risk is a missed writer site in a parity or native test, which the full gate exposes in round one).
- **Slice B (engine, R2, 2 LOC plus one sentry):** `pyproject.toml:108` floor, T10, `uv lock` no-op check. Ships alone, independent of A.

## 12. Pre-mortem

- A reviewer finds a run entry that builds a writer internally from the seed: none exists on main (the only `VaultWriter(` constructor call under `src` is `vault.py:355`); T6 covers the public entries.
- The platform's vault jobs break on engine main: already true today through F13 admission; the fix is platform F13 slice 4 (section 4, item 2), not this plan.
- The CLI inspector opens no vault after the pin bump: yes, until section 4 item 3 lands; the inspector is the one consumer site that gets worse (today it opens seed-keyed files), so it is named as a prerequisite, not a follow-up.
- A real seed-keyed vault exists in a deployment: the engine cannot open it after the pin bump; the only recovery is the old engine. That is Q1 and is not decided here.
- The corpus test is skipped pre-GA and hides a broken fixture: `test_compat_corpus.py:94` skips only the real-artifact capture; the synthetic round trip (`:37`, `:53`) runs on every CI job, so a stale `job_seed_hex` fails loudly.
- A hostile `KeyProvider` returns 32 bytes of zeros: accepted by the floor (V2 note, out of the V1 model).
- Someone proposes a vault-only minimum of 16 bytes to keep tests green: refused by P5 and the worker rules.
- `uv lock --upgrade` moves simpleeval to 1.0.8: the sentry stays green (lock >= floor); the floor moves only when a new advisory names a higher fixed release.

## 13. Open owner questions

- Q1 (Cam, not decided by this plan): landing slice A makes every seed-keyed vault unreadable by the new engine, and the repos cannot prove none exists outside disposable fixtures (platform `<upload_dir>/vaults`, CLI `--vault` outputs). Options: approve old-engine-only recovery explicitly (unmask with the pinned engine before upgrading), or schedule a separate R3 re-mask migration with a Dennis gate. Recommendation: approve old-engine-only recovery for pre-GA deployments.
- Q2: owner and timing of platform F13 slice 4 (section 4, item 2) relative to the platform's engine pin bump.
- Q3: owner of the CLI inspector and creation change (section 4, item 3).

## 14. Review history

Codex round 1 on `c97bf0e9` (verdict NO-GO: 1 BLOCKER, 4 MEDIUM, 1 LOW), verified against `6ce435ce`:

- BLOCKER stale baseline and unsafe sequencing: fixed. Rewritten against merged F13 (sections 0, 1, 5); platform provider injection and CLI creation and inspection resolution are named coordinated release prerequisites with owners and order (section 4); seed-keyed vault existence settled as "not provable outside the repos" with the one committed instance regenerated in-slice and the recovery decision recorded as Q1 for Cam, not decided here.
- MEDIUM write-path recheck untested: fixed. T5 now mutates a valid writer's `_mask_key`, calls `write()`, and proves no file is created or altered (class 2 row).
- MEDIUM mutable `bytearray` admitted by reference: fixed as a class. Keys are normalized to `bytes` at admission in the writer and in `SecretKeyProvider` (same class, sibling instance found by self-attack: `keyprovider.py:135-156`), with mutation-after-admission tests (T7, T11).
- MEDIUM no-secret red test not red under the F13 autouse fixture: fixed. T2 and every no-secret test carry `no_default_secret`; `tests/_keyed_secret.py` is imported, not copied (section 5, class 8 row).
- MEDIUM "floor equals lock" is not the dependency contract: fixed. P4 is now floor >= advisory-fixed 1.0.5 and lock >= floor, both sentry-enforced; routine lock upgrades never move the floor.
- LOW re-tier: fixed. Both slices are R2 with a Codex gate; the only R3 activity (disposal or regeneration of a real seed-keyed vault) is outside this plan and needs Cam's authorization plus a Dennis gate if scheduled (Risk line, section 13).
- Missing canonical rulebooks and Established solutions: fixed (Rules consulted line, section 2).
- Self-attack of the same class elsewhere: the committed compatibility-corpus vault was seed-keyed and its test reads `job_seed_hex`; the planner's revision 1 listed `regenerate.py` as a migration site but not the committed artifact or the manifest field. Added to section 5 and T3.

## 15. Worker evidence rules

Name the class of every finding and fix the class, not the instance. Write the generated tests first and record them red (T8 file). Before pushing, attack your own diff: mutate each new mechanism once and show the test goes red. Run baselines with `uv.lock` in the same environment as the branch (`scripts/dev/bootstrap.sh`, `scripts/dev/gates.sh --baseline`) and state what you did not run. Never widen a threshold, skip a test or weaken a keyed assertion to make the migration pass. If a defect of a class already in the catalog appears a second time, stop and report it rather than patch again.
