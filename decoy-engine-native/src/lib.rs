//! Compiled Rust companion for `decoy-engine`'s native masking hot path.
//!
//! `_kernel` is the canonical compiled module this companion package ships (installed directly;
//! the core has no `native` extra yet, see `decoy-engine/pyproject.toml`) and the
//! `load_compiled_crypto_kernel` / `load_compiled_index_kernel` loaders target. It exports
//! `abi_version()` (the build-system stub from the companion scaffold), `derive_batch` (the
//! security-sensitive `KeyedDerivationKernel`, see `arrow_ffi::derive_batch`), and
//! `derive_index_batch` (the deterministic-faker pool-index kernel, see
//! `arrow_ffi::derive_index_batch`). Everything else the engine does stays pure Python.
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
// C6a: the NIST SP 800-38G FF1 format-preserving-encryption kernel. Its pure core (the deployable
// wrapper over the `fpe` crate's FF1 + AES-256) builds without PyO3 so cargo tests exercise it
// directly; the `#[pyfunction]` boundary lives in `arrow_ffi` behind the extension-module feature.
pub mod fpe;
// C6c-ii: the text_redact span kernel. Its pure detector/validator/predicate core builds without
// PyO3 (so cargo tests exercise it directly); only the `#[pyfunction]` wrappers and `register` are
// gated behind the PyO3 boundary feature, like `arrow_ffi`.
pub mod text_redact;
pub mod threads;

/// The ABI tag the core's loader checks on every load (`load_compiled_crypto_kernel`).
///
/// A mismatch or absence is treated as an incompatible extension: the core reroutes to the
/// pandas oracle rather than running against a stale binary.
#[cfg(feature = "extension-module")]
// abi-3 (was abi-2): C6a adds the `fpe_transform_batch` FF1 entry point. A pre-C6a abi-2
// binary lacks it, so the core's `load_compiled_fpe_kernel` must reject it at load by tag
// rather than crash on the first fpe call. abi-2 added `native_threads` on `derive_batch`.
const ABI_VERSION: &str = "decoy-native-abi-3";

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
    text_redact::register(m)?;
    Ok(())
}
