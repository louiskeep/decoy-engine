# Key Derivation Chain

This document describes how Decoy derives per-field subkeys from a single
master key, why the mask and generate resolvers are kept separate, and
what the error behavior is when a key is missing or malformed.

## Chain overview

```
DECOY_MASTER_KEY (32 bytes)
        │
        ▼  HKDF-SHA256(info="pipeline:{pipeline_label}")
  pipeline_key (32 bytes)        ← generate resolver uses this
        │
        ▼  HKDF-SHA256(info="{column_info}")
  field_subkey (32 bytes)        ← passed to transform strategy
```

For **mask** operations the pipeline label is omitted and `DECOY_MASTER_KEY`
flows directly into the second HKDF step:

```
DECOY_MASTER_KEY (32 bytes)
        │
        ▼  HKDF-SHA256(info="mask")
  mask_key (32 bytes)            ← same value, every pipeline, every column
```

This is the FK-stability design decision: mask output is an instance-level
constant for a given plaintext value, so a value that appears in two tables
(e.g. `customers.email` and `orders.contact_email`) maps to the same masked
value across both tables without any cross-table configuration.

Generate uses the pipeline-scoped key so the synthetic data produced for one
pipeline does not accidentally collide with synthetic data from another.

## Implementation

The derivation is implemented in `src/decoy_engine/context.py` using only the
Python standard library (`hmac`, `hashlib`) so the engine has no dependency on
the `cryptography` package:

```python
def _hkdf_sha256(master: bytes, info: str, length: int = 32) -> bytes:
    # Empty-salt HKDF: PRK = HMAC(zero_salt, master)
    salt = b"\x00" * 32
    prk = hmac.new(salt, master, hashlib.sha256).digest()
    # Single expansion round (sufficient while length <= 32)
    okm = hmac.new(prk, info.encode("utf-8") + b"\x01", hashlib.sha256).digest()
    return okm[:length]

def make_key_resolver(master: bytes, pipeline_label: str) -> Callable[[str], bytes]:
    if not isinstance(master, (bytes, bytearray)) or len(master) != 32:
        raise ValueError("master key must be 32 bytes")
    pipeline_key = _hkdf_sha256(master, f"pipeline:{pipeline_label}")
    def resolver(info: str) -> bytes:
        return _hkdf_sha256(pipeline_key, info)
    return resolver
```

The `ExecutionContext` carries two resolvers:

| Attribute | Caller binds | Used by |
|---|---|---|
| `derive_key` | master only | mask transforms (hash, faker, date_shift, reference) |
| `pipeline_derive_key` | master + pipeline label | generate transforms |

## Error behavior

`make_key_resolver` raises `ValueError("master key must be 32 bytes")` when
given any of the following:

- `None`
- An empty `bytes` object (`b""`)
- A `bytes` object shorter or longer than 32 bytes
- Any non-bytes type

This ensures that a misconfigured deployment (missing env var, empty secret
file, wrong-length KMS response) produces a loud, immediate failure rather
than silently falling back to determinism-breaking behavior.

The engine itself does not enforce a non-`None` master key — that is a policy
decision made at the platform boundary (`api/keys/make_resolver.py`). The
platform raises a `PipelineValidationError` before execution if a pipeline's
policy requires deterministic masking but no key is configured.

## Tests

`tests/unit/test_determinism.py` covers:

- `TestMakeKeyResolver` — 32-byte output, stability, cross-pipeline isolation,
  rejection of `None`, empty bytes, and wrong-length masters.
- `TestKeyedHash`, `TestKeyedFaker`, `TestKeyedDateShift`, `TestKeyedReference` —
  per-strategy determinism and FK-stability (same value, different column names,
  same output).
- `TestForeignKeyIntegrity` — cross-strategy confirmation of master-only keying.

## Relationship to the evidence manifest

The evidence manifest (`api/evidence/assembly.py`) records the `key_label`
field on each job. The label is the `pipeline_label` passed to
`make_key_resolver` — it identifies which derivation path was active without
exposing the master key or any subkey material. Auditors can confirm that two
jobs used the same derivation path by comparing `key_label` values; they
cannot reconstruct any key from the label alone.

See [sql-surfaces.md](sql-surfaces.md) for the companion security doc covering
SQL injection surfaces.
