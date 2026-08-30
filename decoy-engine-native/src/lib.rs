//! Compiled Rust companion for `decoy-engine`'s native masking hot path.
//!
//! `_kernel` is the canonical compiled module the engine's `native` extra ships and the
//! `load_compiled_crypto_kernel` loader targets. It exports `abi_version()` (the build-system
//! stub from the companion scaffold) and `derive_batch` (the security-sensitive
//! `KeyedDerivationKernel`, see `arrow_ffi::derive_batch`). Everything else the engine does
//! stays pure Python.
//!
//! `batch`/`canonicalize`/`derive`/`ffi_import` have no PyO3 dependency and stay `pub`
//! unconditionally, so a standalone binary (a fuzz target, an ASan/TSan test build) can link
//! this crate with `--no-default-features` and exercise the real derivation and FFI-import
//! paths with no Python interpreter involved. `arrow_ffi` and the two items below it are the
//! PyO3 boundary and only build under the default `extension-module` feature (see Cargo.toml
//! for why that feature exists).

#[cfg(feature = "extension-module")]
use pyo3::prelude::*;

#[cfg(feature = "extension-module")]
mod arrow_ffi;
pub mod batch;
pub mod canonicalize;
pub mod derive;
pub mod ffi_import;

/// The ABI tag the core's loader checks on every load (`load_compiled_crypto_kernel`).
///
/// A mismatch or absence is treated as an incompatible extension: the core reroutes to the
/// pandas oracle rather than running against a stale binary.
#[cfg(feature = "extension-module")]
const ABI_VERSION: &str = "decoy-native-abi-1";

#[cfg(feature = "extension-module")]
#[pyfunction]
fn abi_version() -> &'static str {
    ABI_VERSION
}

#[cfg(feature = "extension-module")]
#[pymodule]
fn _kernel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(abi_version, m)?)?;
    arrow_ffi::register(m)?;
    Ok(())
}
