Status: reference
Purpose: Frozen contract for deterministic Faker pool selection (`derive_index`), so a compiled `derive_index_batch` reproduces Python pool choice exactly.
Last reviewed: 2026-09-10

# derive_index contract (Phase 2 Task 2.1)

Deterministic Faker masking picks a value from a fixed pool by deriving a stable index
per source row: `derive_index(seed, namespace, canonical_source, pool_size)`. Phase 2 adds
a compiled `derive_index_batch` (Task 2.2). This document freezes the exact selection rule
and points at the known-answer vectors that pin it, so the compiled port is judged against
a fixed contract rather than a moving reference.

The rule below is the shipped Python behavior in `decoy_engine.determinism.derive_index`
(`src/decoy_engine/determinism/_derive.py`). It is recorded here, not redesigned.

## 1. Selection rule

```
index = int.from_bytes(derive(seed, namespace, canonical_source)[:8], "big") % pool_size
```

`derive(...)` is the frozen keyed-derivation envelope the keyed-hash kernel already uses
(the two share one implementation and one canonicalization). It returns a 32-byte
HMAC-SHA256 digest:

```
hmac_key   = HKDF-SHA256(ikm=seed, salt=b"decoy-engine/determinism/v1", info=namespace_utf8, length=32)
hmac_input = byte(SEED_PROTOCOL_VERSION) ++ u32be(len(namespace_utf8)) ++ namespace_utf8
                                         ++ u32be(len(canonical_source)) ++ canonical_source
digest     = HMAC-SHA256(hmac_key, hmac_input)      # 32 bytes
```

`SEED_PROTOCOL_VERSION` is `6`. The index takes the first 8 bytes of the digest as a
big-endian unsigned 64-bit integer and reduces modulo `pool_size`. The length-prefixed
framing is load-bearing: moving a byte between namespace and source changes the index even
when the raw concatenation is identical (`"ab"+"c"` vs `"a"+"bc"` select different indices;
see the `framing_boundary_*` vectors).

## 2. Canonicalization

`canonical_source` is the output of the shared `_canonicalize_source` (re-exported as
`decoy_engine.kernel._canonicalize.canonicalize_derive_source`; the two names are the same
function object). Pool selection and keyed hashing therefore canonicalize identically, and
the Rust `canonicalize.rs` already mirrors it. The admitted arrow types and their canonical
bytes:

- utf8 / large_utf8: NFC-normalized UTF-8, no length prefix (the HMAC frame supplies it).
- bool: `b"\x01"` / `b"\x00"` (checked before integer).
- integer (any width, signed or unsigned): `u32be(len(body)) ++ body`, where
  `body = n.to_bytes((n.bit_length() + 8) // 8, "big", signed=True)`. This is NOT strictly
  minimal: the `+ 8` always reserves a full sign byte, so a negative power of two is one byte
  wider than a DER-minimal encoding would be. Example: `-128` (bit length 8) encodes as
  `00000002 ff80` (two body bytes), not `00000001 80`. A port must use this exact sizing, not
  a minimal encoder.
- timestamp with timezone: the UTC instant as an ISO-8601 string with a `+00:00` offset,
  UTF-8. Timezone-naive timestamps are rejected (`timezone_naive_datetime`).

Float is rejected (`float_canonicalization_unsupported`). Date and Decimal have Python
canonical forms but are not in the native admitted set; the compiled batch covers only the
arrow types above, the same set the keyed-hash kernel admits.

## 3. Null policy

A null source row is never canonicalized and never reaches `derive_index`. The pool sampler
short-circuits null in to null out and counts only non-null positions when sizing draws
(`generation/pool/_sampler.py`). The vectors record a null index for every null row. A
compiled batch must produce a null (not index 0, not an error) for a null input.

## 4. Pool-size and framing errors

`derive_index` raises `DeterminismError` with these codes, which a compiled port must
reproduce rather than accept or remap:

- `pool_size_overflow`: `pool_size > 2**56`. The guard is strictly greater-than, so
  `pool_size == 2**56` is ACCEPTED. This inclusive boundary is pinned by
  `utf8_pool_size_at_max_boundary`; a port that shifts the check to `>=` fails it.
- `pool_size_invalid`: `pool_size < 1` (zero or negative). Distinct from overflow, so a
  caller inspecting `e.code` can tell underflow from overflow.
- `seed_wrong_length`: the seed is neither 8 bytes (job seed) nor 32 bytes (mask key).
- `namespace_empty`: an empty namespace.

Guard order is part of the contract: `derive_index` checks the pool-size guards BEFORE it
derives, so a bad `pool_size` reports its own code even when the seed or namespace is also
invalid. A port that derives first and guards `pool_size` afterward would pass every
single-fault case yet diverge on a combined fault; the `pool_invalid_beats_seed` and
`pool_overflow_beats_namespace` error vectors pin the ordering.

The `2**56` ceiling bounds modulo bias: for `pool_size <= 2**56` the most-favored index is
at most `2**-8` more likely than the least-favored; for typical pool sizes (1k to 100k) the
bias is below `2**-44`.

## 5. Partition and thread invariance

`derive_index` is a pure function of `(seed, namespace, canonical_source, pool_size)`. An
index depends only on its own row, never on surrounding rows or call order, so the per-row
expectations hold under any partitioning or thread count a batched or parallel port uses.
The vectors are per-row and order-independent by construction; the pinning test also checks
forward-vs-reversed order explicitly.

## 6. Vectors and evidence

- Fixture: `decoy-engine-native/vectors/derive_index_kat.json` (24 value cases + 7 error
  cases), the cross-language known-answer set the compiled `derive_index_batch` must
  reproduce index-for-index. Its header records the `seed_protocol_version` (6) the indices
  were generated under, so a post-bump fixture is self-identifying, not silently stale.
- Generator: `decoy-engine-native/vectors/generate_derive_index_kat.py`. Every expected
  index comes from running the shipped `derive_index` over the shipped canonicalization, so
  the fixture is correct by construction against Python behavior. Re-run it ONLY on a
  `SEED_PROTOCOL_VERSION` bump; a bump invalidates every expected index, exactly like the
  keyed-hash KAT.
- Python pinning test: `tests/native/test_derive_index_kat.py` proves the live
  `derive_index` and canonicalization still reproduce the fixture (indices, canonical bytes,
  error codes, and order invariance). It is pure Python and needs no compiled companion.

Coverage: every admitted arrow type (utf8, large_utf8, the eight integer widths, bool, and
timestamp at s/ms/us/ns including a non-UTC timezone and a pre-epoch instant), null rows, an
all-null column and an empty column, both seed lengths (8-byte job seed and 32-byte mask
key), NFC/NFD normalization equivalence, the namespace/source framing boundary, the
degenerate `pool_size == 1` (all indices zero), the inclusive `pool_size == 2**56` boundary,
all four error codes, and two combined-fault vectors pinning the guard order.

## 7. What the compiled port owes (Task 2.2)

`derive_index_batch` reuses the existing `canonicalize_row` and the frozen `derive` envelope,
adds only the `digest[:8]` big-endian reduction and the pool-size guards, preserves null
positions, and returns an Arrow `uint64` array of indices. The reduction reads `digest[:8]` as
an UNSIGNED 64-bit integer (a signed reading diverges for any digest with the high bit set; the
`utf8_unicode_namespace_high_bit_digest` vector pins this), and the pool-size guards run BEFORE
the derive so a bad `pool_size` outranks a bad seed or namespace. Arbitrary pool values
stay outside Rust: the caller uses Arrow `take` to select the pool entries. The exit gate is
this fixture reproduced index-for-index, plus the property, mutation, fuzz, sanitizer, and
allocation gates the keyed-hash kernel already carries.
