# decoy-engine-native

Compiled Rust companion to [decoy-engine](https://github.com/louiskeep/decoy-engine). It ships the
`KeyedDerivationKernel`, the one security-sensitive native masking kernel (HKDF-SHA256 then
HMAC-SHA256 over the Arrow C Data Interface, reproducing the engine's shipped `hash` strategy byte
for byte). Everything else the engine does stays pure Python.

## Why a separate package

`decoy-engine` installs and runs with no Rust toolchain, no compiler, and no platform wheel beyond
`py3-none-any`. This package is optional: install it (the engine's `native` extra) to get the
compiled kernel, or skip it and the engine reroutes keyed-hash columns to its pandas oracle at
preflight. Neither path changes a masked value; the compiled kernel exists for throughput, not for
correctness the pure-Python path lacks.

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

Wiring this kernel into the core's real loader (replacing the always-raising Phase 0 stub) is a
separate slice; today, `load_compiled_crypto_kernel` still always raises
`CryptoExtensionUnavailableError` regardless of whether this companion is installed.

## Fuzzing and sanitizers

`fuzz/fuzz_targets/derive_array.rs` is a libFuzzer target over the PyO3-free `batch::derive_array`
path: it builds an array of one admitted type from structured `arbitrary` input and calls the real
derivation loop, letting libFuzzer's crash detector catch a panic or memory fault directly. The
target links the crate with `default-features = false`, since pyo3's own `extension-module`
feature omits linking libpython, which a standalone fuzz binary does not provide.

```
cargo install cargo-fuzz
cargo +nightly fuzz run derive_array -- -max_total_time=120
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

The engine's core `pyproject.toml` declares an optional `native` extra that names the compatible
version of this package, released alongside it. Cutting a companion-only security release (a new
wheel, same ABI tag) does not require rebuilding the core.
