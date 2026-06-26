# Determinism and the seed protocol

Decoy is deterministic by design: the same inputs produce the same masked or
generated output. This is what makes a masked dataset reproducible, makes joins
stable across tables, and makes a masking run auditable.

## The guarantee

Every deterministic-mode column routes through one primitive:

```
derive(seed, namespace, source) -> 32 bytes (HMAC-SHA256)
```

The same `(seed, namespace, source)` produces byte-identical output across
processes, across days, and across engine versions while the seed-protocol
version is unchanged. The current `SEED_PROTOCOL_VERSION` is exposed as
`decoy_engine.SEED_PROTOCOL_VERSION`. A bump to that version is a deliberate,
release-noted change that re-keys output, so it is not done casually.

From this primitive the engine builds the higher-level mappings:
`derive_index(...)` for picking a pool position (faker, categorical), and
`derive_value(...)` for domain-typed values.

## Two levels of "same output"

There are two reproducibility scopes, and they have different requirements.

### Same seed, same machine: the `seed`

Set `global_settings.seed` in the config. With a fixed seed, the same config
plus the same input produces the same output on repeated runs. This covers
local reproducibility and the byte-equal-across-runs invariant the golden tests
pin.

### Same key, any machine: the master key

A bare seed is reproducible per input but not portable in the same way across
every keying path. For output that is bitwise-identical across machines, supply
a master key and a stable key label:

```
decoy run pipeline.yaml --master-key <64-char-hex> --key-label customers_q4
```

The master key can also come from the `DECOY_MASTER_KEY` environment variable,
and the key label from the YAML's top-level `key_label` field. The same master
key plus the same key label always yield bitwise-identical output across runs
and machines. Without either, masking falls back to the legacy seeded path
(per-input deterministic, but not portable in the same sense). Changing the key
label produces different masked output, so pick something durable.

See [security/key-derivation](security/key-derivation.md) for how the master
key is split into per-field subkeys.

## What is deterministic

- Keyed mask strategies in deterministic mode: `faker`, `hash`, `fpe`,
  `date_shift`, `categorical`, `shuffle`. Same source value plus same namespace
  yields the same masked value.
- Strategies that are deterministic by construction: `bucketize` (same value,
  same bucket), `redact` and `text_redact` (pure function of input and config),
  `truncate`, `formula` (deterministic by its expression).
- Foreign-key remapping: a masked join is byte-stable across runs (see
  [relationships](relationships.md)).
- Generation in seeded mode: same seed yields the same synthetic table.

## What is NOT deterministic

- Non-deterministic mode. `faker`, `categorical`, and `shuffle` can run in a
  non-deterministic mode that draws from an unseeded RNG; two runs differ. This
  is opt-in and is not the keyed path.
- Profiling without a seed. `profile_source` falls back to OS entropy for
  reservoir sampling if no seed is passed and none is set in
  `global_settings.seed`; on tables larger than the sample cap this makes the
  profile (and therefore plan compilation, which reads sampled distinct counts)
  non-deterministic. Always set a seed for reproducible profiles; the engine
  warns when one is missing.
- Anything outside the keyed primitives. Wall-clock timings, log ordering under
  threading, and other run metadata are not part of the output contract; only
  the masked or generated table values are.

## Namespaces and determinism

The `namespace` argument is what scopes determinism. Two columns in the same
namespace map identical source values to identical outputs (this is how
foreign keys stay joined). Two columns in different namespaces are independent.
Keyed strategies (`hash`, `fpe`, `date_shift`, and deterministic-mode
`shuffle`) require a namespace; a keyed column without one is a wiring error
that the engine rejects rather than silently mis-keying.

## FPE key and tweak model

FPE uses a single Feistel key per `(job_seed, namespace)`, derived as
`derive(job_seed, namespace, b"fpe-key/v1")`. This is the NIST SP 800-38G FF1
key model: one key, varying tweak per column. The tweak is the column name
encoded as UTF-8.

This means:

- Two columns in the same namespace with different names receive the same key
  but different tweaks, so their ciphertexts are independent.
- A given source value in a given column always produces the same ciphertext
  within one seed and namespace: `fpe("alice", key, tweak="email")` is
  byte-stable across runs.
- **Renaming a column changes its FPE tweak and therefore its ciphertext.**
  If you rename `email` to `contact_email` and re-run the mask, `unmask` with
  the original column name will not reverse the new ciphertexts. Re-run unmask
  against the new column name, or treat the rename as a re-keying event.

The implementation is in `src/decoy_engine/execution/_strategies/_fpe.py`
(key derivation) and `src/decoy_engine/transforms/fpe.py` (Feistel cipher).

## Deterministic shuffle and column names

The `shuffle` strategy binds the column name into its derivation source:
`derive(job_seed, namespace, column_name.encode("utf-8"))`. This ensures that
two shuffle columns in the same namespace draw distinct permutations. Without
the column-name binding, both columns would share one permutation and their
values would permute in lockstep, re-linking records across columns that masking
is meant to decouple.
