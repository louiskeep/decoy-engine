Breaking: a token vault opens and writes only under the 32-byte mask root of a secret; a vault keyed on the job seed by an older engine no longer opens and must be regenerated with its masked output under a secret

### Changed (vault key floor and simpleeval floor, SLICE0-VAULT, 2026-11-12)

A token vault is a re-identification database, and the engine could still key one on the 8-byte
public `job_seed`.

- `VaultWriter`, `load_vault` and the run-entry key-match guard (`assert_vault_writer_keyed`, so
  `run_pipeline`, `run_mask_pipeline_chunked` and direct out-of-core callers) refuse a key that is
  not the 32-byte mask root as `bytes`/`bytearray` with `VaultError(code="weak_mask_secret")` and
  fixed text. A longer key is refused too: the key derivation accepts no other strong length, so
  such a key used to pass construction and fail at the first write or load. The check runs at construction and again at
  every write, load and key-match, before any file is created, changed or read.
- `vault_writer_for_config` never keys a vault on `job_seed`: with no admitted secret it raises
  `KeyedStrategyRequiresSecret` for every config, keyed or not.
- A writer stores an immutable `bytes` copy of its key, and `SecretKeyProvider` freezes its secret
  to `bytes`, so a caller-held `bytearray` cannot change a key after admission.
- Seed-keyed vaults written by older engines no longer open under any key. There is no migration
  and no recovery path: regenerate the vault together with its masked output by re-running the mask
  job under a secret of at least 32 bytes (its mask root is 32 bytes). The `decoy-vault/v2` format
  and the `vault-key/v1` label are unchanged, so a vault written under a 32-byte key opens
  unchanged.
- `simpleeval>=1.0.5` (was `>=0.9.13`): GHSA-44vg-5wv2-h2hg / CVE-2026-32640 is patched in 1.0.5.
  The lock already resolved 1.0.7; nothing installed changes.

The CLI already builds its vault writer and opens vaults from the run's resolved secret. The
platform's job vault writer (`api/jobs/v2_vault.py`) must pass a `SecretKeyProvider` before the
platform moves its `decoy-engine` pin past this change; until then every platform vault job fails
closed.
