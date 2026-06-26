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
