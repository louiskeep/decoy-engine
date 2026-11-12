Breaking: a vault writer is refused with a sink or a chunked entry, a `TokenVaultWriter` needs `derivation_scope`, tokenize output changes, and an existing vault file is refused instead of overwritten; see the entry.

### Changed (vault crypto core, B-6, 2026-11-12)

- HKDF-SHA256 (`determinism/_hkdf.py`) delegates to PyCA `cryptography` (`HKDF`, `HKDFExpand`,
  `HMAC`) instead of a hand-written loop. Every derived key is byte-identical (RFC 5869 A.1 to
  A.3 vectors and a corpus captured from the stdlib implementation). The empty-salt refusal holds
  on both `hkdf_extract` and the one-shot `hkdf_sha256`.
- Breaking, pre-GA, hard cut: `tokenize` derives each token under a per-vault subkey,
  `derive(mask_key, namespace, b"tokenize-key/v1" || len(scope) || scope)`, where `scope` is the
  new required `TokenVaultWriter.derivation_scope` (16 to 64 non-secret bytes the vault owner
  mints once). The tokenize output changes: two vaults with the same key, namespace and plaintext
  mint different tokens, and a `hash` output (any `truncate`) is no longer a token payload. A
  writer without a scope (a vault from before this change) is refused with
  `runtime/token_vault_derivation_unsupported`, a malformed scope with
  `runtime/tokenize_vault_scope_required`. Both are raised before any source is read, with the
  registered summary as their only text. There is no dual derivation and no migration: tokens
  from an earlier vault cannot be reproduced.
- Breaking, pre-GA: boundary rule B0. A file-vault `vault_writer` is refused with
  `runtime/vault_requires_resident_output` wherever the engine cannot hold every output row until
  the vault is published: `run_pipeline` with both a `sink` and a `vault_writer`,
  `run_mask_chunked` and `run_mask_pipeline_chunked` with any `vault_writer`, and
  `run_fk_out_of_core` with both. With a sink the engine commits output inside the run, so the
  vault could only follow it. The refusal runs before any source or chunk is read.
  `run_pipeline` without a sink, including its auto-chunk lane, is unchanged. The `decoy` CLI's
  `run --chunked --vault` uses the refused combination and must refuse the flag pair before it
  repins to this release.
- Breaking, pre-GA: the file vault is published privately, durably and exclusively.
  `VaultWriter.write` (a path string under the ambient policy, or a policy `OutputDir`) creates a
  stage beside the vault through the private-file creator (mode `0600` whatever the umask), writes
  and fsyncs it, links it to the vault name, fsyncs the directory and removes the stage. An
  existing vault is refused with `runtime/vault_exists` (the ambient policy used to overwrite it
  in place, writing through hard links). A write, fsync or link failure is
  `runtime/vault_commit_failed`. Both texts are fixed and name no path. No failure path removes a
  published vault. The ambient in-place write (the `vault_direct` stage label) is deleted.
