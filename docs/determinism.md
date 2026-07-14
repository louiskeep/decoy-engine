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
`derive(job_seed, namespace, b"fpe-key/v1")`. This is a single-key/varying-tweak
key model (one key, varying tweak per column), but the underlying primitive is
the engine's home-rolled 8-round HMAC-SHA256 Feistel, which is NOT NIST SP
800-38G FF1 (an audited FF1 is a documented fast-follow). The default tweak is
the column name encoded as UTF-8.

This means:

- Two columns in the same namespace with different names receive the same key
  but different tweaks, so their ciphertexts are independent (the F3
  domain-separation invariant).
- A given source value in a given column always produces the same ciphertext
  within one seed and namespace: `fpe("alice", key, tweak="email")` is
  byte-stable across runs.
- **Renaming a column changes its FPE tweak and therefore its ciphertext.**
  If you rename `email` to `contact_email` and re-run the mask, `unmask` with
  the original column name will not reverse the new ciphertexts. Re-run unmask
  against the new column name, or treat the rename as a re-keying event.

The implementation is in `src/decoy_engine/execution/_strategies/_fpe.py`
(key derivation) and `src/decoy_engine/transforms/fpe.py` (Feistel cipher).

### FPE join groups (opt-in, SP-46)

By default, two FPE columns in the same namespace encrypt the same value
to different ciphertexts (different tweaks). This is the correct behaviour
for independent columns.

In telco and similar schemas, two columns from different tables
(e.g. `subscribers.msisdn` and `cdr.called_msisdn`) contain the same domain
values and must join after masking. To make that join work, both columns need
identical ciphertext for the same plaintext -- which requires a shared tweak.

Set `fpe_join_group: "<name>"` in `provider_config` on every column that must
join. Members of the same group share the tweak (the group name replaces the
column name), so the same plaintext encrypts to the same ciphertext and the
join survives masking.

```yaml
columns:
  - name: msisdn
    strategy: fpe
    namespace: phone_ns
    provider_config:
      charset: digits
      fpe_join_group: phone_e164

  - name: called_msisdn
    strategy: fpe
    namespace: phone_ns
    provider_config:
      charset: digits
      fpe_join_group: phone_e164
```

Requirements enforced at compile time:

- A group must have at least two members (a singleton group raises
  `fpe_join_group_singleton`).
- All members must use the `fpe` strategy (`fpe_join_group_non_fpe_member`).
- All members must declare the same `charset`, `preserve_separators`,
  `validate_luhn`, and `checksum` (`fpe_join_group_config_mismatch`).
- All members must belong to the same namespace (`fpe_join_group_namespace_mismatch`).

**Security note:** activating a join group intentionally waives the
per-column domain-separation guarantee (F3). The plan manifest records this
decision explicitly as a compile-time warning. Two columns in the same group
encrypt the same value identically, so an analyst who can observe both columns
can link records across them by matching ciphertext (cross-table
re-identification) and pool their frequency distributions - exactly the linkage
the join enables, but also the attack surface it opens. This is the correct trade-off
for a schema that requires cross-table joins; do not use join groups when
columns should be cryptographically independent.

Key derivation is NOT affected by the join group. The tweak is not part of
the `derive()` envelope, so no `SEED_PROTOCOL_VERSION` bump is required.
Default behaviour (no `fpe_join_group`) is byte-identical to pre-SP-46
output.

## Deterministic shuffle and column names

The `shuffle` strategy binds the column name into its derivation source:
`derive(job_seed, namespace, column_name.encode("utf-8"))`. This ensures that
two shuffle columns in the same namespace draw distinct permutations. Without
the column-name binding, both columns would share one permutation and their
values would permute in lockstep, re-linking records across columns that masking
is meant to decouple.

## Generation determinism (v6 rewrite, F2/F3)

Prior to `SEED_PROTOCOL_VERSION` v6, the synthetic-generation path used a
per-column integer seed derived by truncating the HKDF key material to 4 bytes
(`generators/derivation.py`, the old `synthetic_column_seed`), then adding a
row index for per-row variation: `column_seed + i`. That design had two
correctness defects (F2 and F3 in `docs/remediation-source.md`):

- **F2.** A 32-bit keyspace makes birthday collisions between columns likely
  at realistic column counts, and allows brute-force recovery of a generated
  column's seed.
- **F3.** With `column_seed + i`, column A (base `S`) and column B (base
  `S+1`) produced seeds that were row-shift-identical, so adjacent generated
  columns were statistically correlated across rows.

The v6 rewrite replaces `synthetic_column_seed` with
`GenDeriveContext` (`generators/derivation.py`). One full-32-byte column
root is resolved per column (keyed path: full `derive_key("gen:" +
fingerprint)`, no truncation). Per-RNG-family and per-row sub-keys are
derived from that root via `_gen_hmac`, which mixes `SEED_PROTOCOL_VERSION`
into its HMAC input (mirroring the mask-path envelope in
`determinism/_derive.py`). The three RNG families (`py`, `np`, `faker`)
each receive a distinct family key, so they no longer share one integer.
Per-row derivation uses `row_int(family, i)` rather than `base_int + i`,
eliminating the cross-column row-shift correlation.

Generation now mixes the protocol version byte the same way the mask
envelope does. This makes `SEED_PROTOCOL_VERSION` the single compatibility
knob across both determinism roots: a bump re-keys BOTH masked output and
synthetic-generation output. A v5 vault over a synthetic column cannot be
unmasked under v6 (the seeds diverge). The explicit cross-version vault
protocol guard (detect the mismatch and surface a clear error rather than
returning wrong values) is deferred to the vault-hardening work (F13 in
`docs/remediation-source.md`).

The V2 generation engine (`generation/synthesize.py`) null-injection path
was unified to V1's numpy-vectorized mask in the same rewrite: both engines
now call `numpy.random.default_rng(base_int("np")).random(n) < null_prob`
with the same seed derivation, so null-probability columns are byte-identical
across V1 and V2. Before v6, V2 used a per-row Python `random.Random` reseed
loop, which was fraction-convergent but not byte-identical.

The v6 bump is a pre-GA hard cutover: no manifests exist in the wild, so
there is no migration path needed for existing vaults at this point.
`SEED_PROTOCOL_VERSION` is stamped into every compiled plan by the plan
compiler (`plan/_compile.py`); the current value is always readable from
`decoy_engine.SEED_PROTOCOL_VERSION`.
