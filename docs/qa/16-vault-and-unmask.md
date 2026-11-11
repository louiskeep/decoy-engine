# 16. Token vault and unmask

## What it covers

Reversing a mask: `unmask_pipeline` for `fpe` columns, and the encrypted token vault
(`vault: true` on a column plus `decoy run --vault PATH`) that makes one-way strategies
reversible.

## Expected behaviour

- A vault is written only when both are true: a column declares `vault: true` and the
  operator passes a vault path. Otherwise a mask run writes no vault.
- `vault: true` needs a namespace, and is refused on `fpe` (already reversible) and on
  `passthrough` (never masked): `vault_requires_namespace`, `vault_strategy_reversible`,
  `vault_strategy_unmasked`.
- The vault is a file of Fernet-encrypted chunks. Its key derives from the run's mask
  key, so the vault plus the mask secret recovers every vaulted source value.
- A vault written under a different seed-protocol version is refused before decryption
  (`vault_protocol_version_mismatch`). A wrong key gives `vault_key_mismatch`.
- `unmask_pipeline` takes the same config the mask run used. It reports a status per
  column: `reversed_unverified` for `fpe`, `vault_reversed` or `vault_miss` for vaulted
  columns, `irreversible` for one-way columns with no vault, `untouched` for unmasked
  columns.
- Where two source values masked to the same output, that entry is dropped and counted
  in `ambiguous_dropped`. Exact round trips are guaranteed only for collision-free
  maskings such as `hash` under a namespace.
- The vault file is not byte-reproducible (random IV and timestamp). Its contents are a
  pure function of config and sources.

## Checks

| Id | Run this | What should happen |
|---|---|---|
| VAULT-01 | Mask with `fpe` on an SSN column, then `unmask_pipeline` with the same config and secret. | Every value round-trips exactly. The column reports `reversed_unverified`. |
| VAULT-02 | Unmask VAULT-01's output with a different secret. | Values do not match the source. Record what the report says; it cannot verify. |
| VAULT-03 | `hash` column with `vault: true` and a vault path, then unmask with the vault. | Every value recovered. Status `vault_reversed`, zero misses. |
| VAULT-04 | Same column with `vault: true` but no vault path; and a vault path with no `vault: true` column. | No vault file is written in either case. |
| VAULT-05 | `vault: true` with no namespace; on `fpe`; on `passthrough`. | `vault_requires_namespace`; `vault_strategy_reversible`; `vault_strategy_unmasked`, all at compile. |
| VAULT-06 | Unmask with a vault from a different run. | `vault_miss` with zero hits. No wrong values are written as if recovered. |
| VAULT-07 | Open a vault with a different mask secret. | `vault_key_mismatch`. |
| VAULT-08 | Truncate the vault file; corrupt its magic bytes. | `vault_unreadable` for both. |
| VAULT-09 | `faker` column with `allow_collisions: true` and `vault: true` where two sources share a masked value. | Those entries are dropped. `ambiguous_dropped` is non-zero in the header and in the unmask report. No value is recovered wrongly. |
| VAULT-10 | Search the vault file's bytes for a known source value. | Absent. Only the JSON header is readable, and it holds no values. |
| VAULT-11 | `fpe` with `validate_luhn: true` on a column where one source value fails Luhn, then unmask. | Luhn-valid sources round-trip exactly. The invalid one returns with its body exact and last digit normalised, and the report carries the caveat. |

## Open points

- The `unmask.py` docstring says the config "carries the job seed (the secret)" and that
  anyone holding the config can reverse FPE. With the mask secret now required and held
  outside the config, that statement looks stale. Confirm what unmask needs today.
- `tokenize` uses a platform-supplied vault writer, which is a different mechanism from
  this file vault. The two share a word and little else; QA and docs should keep them
  apart.
- The compatibility contract calls the vault "the catastrophic one": a format change
  strands old vaults forever. There is no check here that an older vault file still
  opens. Add one with a stored fixture.
- FPE reversal is always reported `reversed_unverified`. A user cannot tell a correct
  reversal from a wrong-key one (VAULT-02). Decide whether that is acceptable for V1.
