# decoy-engine-native

Compiled Rust companion to [decoy-engine](https://github.com/louiskeep/decoy-engine). It ships the
`KeyedDerivationKernel`, the security-sensitive native masking kernel (HKDF-SHA256 then
HMAC-SHA256 over the Arrow C Data Interface, reproducing the engine's shipped `hash` strategy byte
for byte), and the deterministic-faker index kernel (one pool-index derivation per row, reproducing
the engine's shipped `derive_index` byte for byte). Everything else the engine does stays pure
Python.

## Why a separate package

`decoy-engine` installs and runs with no Rust toolchain, no compiler, and no platform wheel beyond
`py3-none-any`. This package is optional: install it directly (a prebuilt wheel from
`native-companion.yml`, or `uv pip install -e ./decoy-engine-native` / `maturin develop` for local
dev) to get the compiled kernel, or skip it and the engine reroutes keyed-hash columns to its
pandas oracle at preflight. There is no `decoy-engine[native]` extra yet; see the negative note in
`decoy-engine/pyproject.toml` and `docs/native/supported-matrix.md` for why and what installing
looks like today. Neither path changes a masked value; the compiled kernel exists for throughput,
not for correctness the pure-Python path lacks.

## Build

Requires a stable Rust toolchain (pinned in `rust-toolchain.toml`) and
[maturin](https://www.maturin.rs/):

```
maturin develop          # build + install into the active virtualenv
maturin build --release  # produce a wheel under target/wheels/
```

## What's here today

`decoy_engine_native._kernel.abi_version()` returns a pinned ABI tag, checked by the core's loader
(`decoy_engine.execution.native._crypto_ext.load_compiled_crypto_kernel`) before it returns a
working kernel; a mismatch or an absent companion raises `CryptoExtensionUnavailableError` before
any output.

`decoy_engine_native._kernel.derive_batch(values, *, mask_key, namespace, truncate=None)` is the
compiled `KeyedDerivationKernel`: HKDF-SHA256 then HMAC-SHA256 over a typed `pa.Array` (utf8,
large_utf8, signed/unsigned integer widths, bool, or timestamp-with-timezone), reproducing the
engine's shipped `reference_keyed_derivation()` byte for byte. Any other Arrow type is rejected
with the coded error `mixed_object_not_native`; a missing or empty `mask_key` fails before any row
is processed. See `src/derive.rs`, `src/canonicalize.rs`, and `src/arrow_ffi.rs` for the
implementation, and `vectors/keyed_derivation_kat.json` for the shared known-answer-test corpus
(generated from the live Python reference by `vectors/generate_kat.py`).

`decoy_engine_native._kernel.derive_index_batch(values, *, mask_key, namespace, pool_size,
native_threads=None)` is the compiled deterministic-faker pool-index kernel: returns one uint64
pool index per row (null in, null out), reproducing the engine's shipped `derive_index` byte for
byte. It backs the native masking route's per-chunk faker selection (`_sample_faker_chunk`); the
pure-Python `PoolSampler` remains the oracle's own selection mechanism. See `src/batch.rs` and
`src/arrow_ffi.rs` for the implementation, and `vectors/derive_index_kat.json` for the shared
known-answer-test corpus.

Both kernels are wired into the core's real loaders
(`decoy_engine.execution.native._crypto_ext.load_compiled_crypto_kernel` and
`decoy_engine.execution.native._index_ext.load_compiled_index_kernel`): an absent or
ABI-incompatible companion reroutes the affected table to the pandas oracle at preflight rather
than raising mid-run.

## Fuzzing and sanitizers

`fuzz/fuzz_targets/derive_array.rs` is a libFuzzer target over the PyO3-free `batch::derive_array`
path: it builds an array of one admitted type from structured `arbitrary` input and calls the real
derivation loop, letting libFuzzer's crash detector catch a panic or memory fault directly.
`fuzz/fuzz_targets/derive_index.rs` mirrors it over `batch::derive_index_array`, additionally
fuzzing `pool_size` across its full valid range. Both targets link the crate with
`default-features = false`, since pyo3's own `extension-module` feature omits linking libpython,
which a standalone fuzz binary does not provide.

```
cargo install cargo-fuzz
cargo +nightly fuzz run derive_array -- -max_total_time=120
cargo +nightly fuzz run derive_index -- -max_total_time=120
```

Running the crate's own test suite under AddressSanitizer or ThreadSanitizer needs a
sanitizer-instrumented standard library (`-Zbuild-std`), or the linker rejects the ABI mismatch
between instrumented and plain `std`:

```
RUSTFLAGS="-Zsanitizer=address" cargo +nightly test -Zbuild-std --target x86_64-unknown-linux-gnu
RUSTFLAGS="-Zsanitizer=thread"  cargo +nightly test -Zbuild-std --target x86_64-unknown-linux-gnu \
  derive_is_send_sync_and_concurrent_calls_agree
```

## Wheel mapping

The engine's core `pyproject.toml` does not yet declare a `native` extra (see the negative note
there): no version of this package has been released to PyPI for a locker to resolve against. Until
that first paired release, install this package directly rather than through an extra. The `native`
extra is planned to land with that release, pinning the compatible companion version; once it
exists, cutting a companion-only security release (a new wheel, same ABI tag) will not require
rebuilding the core.
