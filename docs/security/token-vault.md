# Token vault: handling and threat model

The token vault (`decoy_engine.vault`, `decoy run --vault`,
`decoy unmask --vault`) makes one-way strategies reversible by
recording each vaulted column's source-to-masked map at mask time. It
is the most sensitive artifact the engine can produce, and it changes
what an operator must protect.

## What the vault is

An encrypted single file containing `(namespace, masked_value) ->
source_value` triples for every column declared `vault: true`. The
payload is a parquet table encrypted with Fernet (AES-128-CBC +
HMAC-SHA256, encrypt-then-MAC, per the Fernet spec) from the
`cryptography` package, installed via the optional `vault` extra.

The key derives from the job seed: `derive(job_seed, "vault",
b"vault-key/v1")`. That is the same HMAC-SHA256 envelope every other
engine derivation uses, with its own label, so the vault adds a
derivation domain without touching the seed protocol.

## Threat model

- **Vault + config = re-identification.** The pipeline config's
  `global_settings.seed` derives the decryption key. Anyone holding
  both files recovers every vaulted source value. This extends the
  config-as-key property `decoy unmask` introduced for fpe columns:
  with a vault in play, the config now unlocks one-way columns too.
- **The vault grows with the data.** Unlike the seed (a constant-size
  secret), the vault contains actual source values. Treat it with the
  handling the source data itself requires.
- **Never ship the vault with the masked output.** The masked output
  is the artifact that is safe to share; the vault is the thing that
  un-shares it.

## Operational rules

1. Vault creation is opt-in twice: a column must declare `vault: true`
   AND the operator must pass `--vault PATH`. A mask run never writes
   a vault otherwise.
2. Store the vault and the config separately, each access-controlled.
3. `vault: true` requires a `namespace` and is rejected on
   `strategy: fpe` (already reversible from the config alone); the
   plan compiler enforces both (`vault_requires_namespace`,
   `vault_strategy_reversible`).
4. Pooled strategies can map two sources to one masked value; those
   keys are dropped at write time (`ambiguous_dropped` in the unmask
   report). Exact round trips are guaranteed only for collision-free
   maskings such as `hash` under a namespace.

## What the vault is not

- Not part of the determinism contract: the file embeds a random IV
  and timestamp, so it is not byte-reproducible. Vault CONTENTS are a
  pure function of (config, sources).
- Not a cross-run consistency store: it maps one run's outputs. The
  managed seed/namespace store remains a separate follow-up.
