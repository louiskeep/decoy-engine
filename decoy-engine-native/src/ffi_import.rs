//! Arrow C Data Interface import and the error type it reports through, kept pyo3-free so a
//! standalone binary (the FFI-metadata fuzz target, an ASan/TSan build) can exercise the real
//! import path -- the one this crate's `force_validate` build already runs against untrusted
//! metadata -- with no Python interpreter involved. `arrow_ffi.rs` re-exports both items for the
//! PyO3 entry point; that module owns everything downstream of a successful import (the row
//! loop, the exception translation, and the Python-object-shaped parts of the boundary).

use std::panic::{catch_unwind, AssertUnwindSafe};

use arrow_array::ffi::{from_ffi, FFI_ArrowArray, FFI_ArrowSchema};
use arrow_array::{make_array, ArrayRef};

use crate::canonicalize::{map_arrow_error, CanonError};
use crate::derive::DeriveError;

/// A single error type for everything that can go wrong on the FFI-import boundary and the row
/// loop downstream of it, so the PyO3 entry point (`arrow_ffi.rs`) has one place to map to a
/// Python exception.
#[derive(Debug)]
pub enum KernelError {
    Canon(CanonError),
    /// Also carries `crate::batch::BatchError::Derive`'s wrong-length / empty-namespace codes
    /// (`seed_wrong_length`, `namespace_empty`): those live in `derive::derive`, fired per
    /// non-null row, not as a distinct variant here.
    Derive(DeriveError),
    /// Missing or empty mask key (fail-before-output; no row has been touched yet).
    MaskKeyRequired,
    /// The `__arrow_c_array__` protocol failed or returned an unexpected shape.
    ProtocolError(String),
}

impl KernelError {
    pub fn code(&self) -> &str {
        match self {
            KernelError::Canon(e) => e.code,
            KernelError::Derive(e) => e.code,
            KernelError::MaskKeyRequired => "mask_key_required",
            KernelError::ProtocolError(_) => "mixed_object_not_native",
        }
    }

    pub fn detail(&self) -> String {
        match self {
            KernelError::Canon(e) => e.detail.clone(),
            KernelError::Derive(e) => e.detail.clone(),
            KernelError::MaskKeyRequired => {
                "mask_key is required and must be non-empty; refusing to emit unkeyed output"
                    .to_string()
            }
            KernelError::ProtocolError(msg) => msg.clone(),
        }
    }
}

impl From<CanonError> for KernelError {
    fn from(e: CanonError) -> Self {
        KernelError::Canon(e)
    }
}

impl From<DeriveError> for KernelError {
    fn from(e: DeriveError) -> Self {
        KernelError::Derive(e)
    }
}

// The single choke point translating the PyO3-free `batch::BatchError` into this module's
// error type: both the real entry point and the sentinel-redaction test route through this
// `From` impl (rather than each re-matching the variant list by hand), so a future BatchError
// variant cannot silently fail to surface as a coded Python exception.
impl From<crate::batch::BatchError> for KernelError {
    fn from(e: crate::batch::BatchError) -> Self {
        match e {
            crate::batch::BatchError::Canon(c) => KernelError::Canon(c),
            crate::batch::BatchError::Derive(d) => KernelError::Derive(d),
            crate::batch::BatchError::MaskKeyRequired => KernelError::MaskKeyRequired,
        }
    }
}

/// The actual FFI-to-`ArrayRef` conversion. Split out so both the PyO3 entry point
/// (`arrow_ffi::import_array`) and the Rust-only malformed-metadata tests -- including the fuzz
/// target -- exercise this exact function, not independently-written paths that could drift
/// apart.
pub fn import_ffi(
    ffi_array: FFI_ArrowArray,
    ffi_schema: FFI_ArrowSchema,
) -> Result<ArrayRef, KernelError> {
    // `force_validate` is enabled unconditionally (see Cargo.toml), so `from_ffi` runs
    // `ArrayData::validate_data()` rather than skipping straight to `ArrayData::new_unchecked`
    // -- malformed metadata that CAN be caught without dereferencing untrusted buffer content (a
    // null buffer pointer at a nonzero declared length, an inconsistent null_count, ...) comes
    // back as a clean `Result::Err`, not silent garbage.
    //
    // `catch_unwind` is defense in depth on top of that: arrow-rs's own `FFI_ArrowArray::buffer`
    // uses an internal `assert!` on an inconsistent buffer count, which is a real panic path we
    // do not control. Wrapping it here guarantees no panic from this call ever reaches a caller
    // as an unwind, regardless of which specific malformed shape triggered it. It does NOT
    // guarantee no out-of-bounds memory access for a shape `force_validate` fails to catch --
    // that is what the FFI-metadata fuzz target is for, run under ASan rather than trusted to
    // `catch_unwind`.
    let result = catch_unwind(AssertUnwindSafe(|| unsafe {
        from_ffi(ffi_array, &ffi_schema)
    }));
    let data = match result {
        Ok(Ok(data)) => data,
        Ok(Err(arrow_err)) => return Err(KernelError::Canon(map_arrow_error(arrow_err))),
        Err(_panic) => {
            return Err(KernelError::ProtocolError(
                "the Arrow C Data Interface import path failed on malformed input metadata"
                    .to_string(),
            ))
        }
    };
    Ok(make_array(data))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::canonicalize::is_admitted_type;
    use arrow_array::ffi::to_ffi;
    use arrow_array::{Array, Float64Array, Int64Array, StringArray};

    /// A well-formed FFI array of an out-of-admitted-set type (float64) must be rejected with
    /// `mixed_object_not_native`, never panic, never partially succeed. This exercises the
    /// "wrong type tag" case from the Rust release gate: the schema/array pair is entirely
    /// valid Arrow, just a type this kernel does not admit.
    #[test]
    fn import_ffi_rejects_unadmitted_type_cleanly() {
        let array = Float64Array::from(vec![1.0, 2.0, 3.0]);
        let (ffi_array, ffi_schema) = to_ffi(&array.to_data()).unwrap();
        let imported = import_ffi(ffi_array, ffi_schema).expect("FFI import itself succeeds");
        assert!(!is_admitted_type(imported.data_type()));
    }

    /// A null data-buffer pointer at a nonzero declared length is explicit, safely-detected
    /// malformed metadata (arrow-rs's own `buffers()` returns a clean `ArrowError` for this,
    /// never dereferencing the null pointer): the "null buffer" case from the Rust release
    /// gate.
    #[test]
    fn import_ffi_rejects_null_data_buffer_cleanly() {
        let array = Int64Array::from(vec![Some(1), Some(2), None, Some(4), Some(5)]);
        let (ffi_array, ffi_schema) = to_ffi(&array.to_data()).unwrap();

        // Buffer 0 is the validity bitmap, buffer 1 is the Int64 data buffer (per the Arrow
        // C Data Interface layout for a primitive type with nulls). Null it out while leaving
        // `length` unchanged, so the computed byte length arrow-rs expects for that buffer
        // stays nonzero: this is exactly the "external buffer at position N is null" path
        // `arrow_array::ffi`'s `buffers()` returns as a clean `Err`, not a panic.
        assert_eq!(ffi_array.n_buffers, 2);
        unsafe {
            let buffers_ptr = ffi_array.buffers;
            *buffers_ptr.add(1) = std::ptr::null();
        }

        let result = import_ffi(ffi_array, ffi_schema);
        assert!(
            result.is_err(),
            "a null required data buffer must not silently import"
        );
    }

    /// An arithmetic-overflow panic inside arrow-rs's own buffer-length computation (triggered
    /// by an implausible declared `length`, i.e. a "bad length" metadata shape) must be caught
    /// by `catch_unwind` and surfaced as a clean `KernelError`, never let unwind further. This
    /// is the "bad length" case from the Rust release gate: it proves the *safety net* holds
    /// for malformed-length metadata that arrow-rs cannot detect without arithmetic that
    /// overflows on nonsensical input, since the C Data Interface has no independent buffer
    /// byte-length field to check against.
    #[test]
    fn import_ffi_never_panics_on_implausible_length() {
        let array = StringArray::from(vec![Some("a"), Some("bb"), Some("ccc")]);
        let (mut ffi_array, ffi_schema) = to_ffi(&array.to_data()).unwrap();

        // A negative length casts (via `FFI_ArrowArray::len`, `self.length as usize`) to a huge
        // usize, which arrow-rs's offset-buffer length computation `(length + 1) * width`
        // overflows computing -- an internal panic path, not a `Result::Err` path, on this
        // build profile. `import_ffi` must convert that panic to an `Err`, never let it escape.
        ffi_array.length = -1;

        let result = import_ffi(ffi_array, ffi_schema);
        assert!(
            result.is_err(),
            "implausible length must fail closed, not panic through"
        );
    }

    /// `KernelError::code`/`detail` are plain match arms with no PyO3 dependency (only
    /// `arrow_ffi::to_py_err`, which formats them into a `PyValueError`, needs a live
    /// interpreter), so every variant is checked directly here rather than left to the
    /// PyO3-only path.
    #[test]
    fn kernel_error_code_and_detail_are_correct_per_variant() {
        let canon_err = CanonError::unsupported("some-type");
        let canon = KernelError::Canon(canon_err.clone());
        assert_eq!(canon.code(), canon_err.code);
        assert_eq!(canon.detail(), canon_err.detail);

        // A real DeriveError, obtained through the public `derive()` entry point rather than
        // hand-built, since `DeriveError::new` is private to the `derive` module.
        let derive_err = crate::derive::derive(&[0u8; 5], "ns", b"x").unwrap_err();
        let expected_code = derive_err.code;
        let expected_detail = derive_err.detail.clone();
        let derive_kernel_err = KernelError::Derive(derive_err);
        assert_eq!(derive_kernel_err.code(), expected_code);
        assert_eq!(derive_kernel_err.detail(), expected_detail);

        assert_eq!(KernelError::MaskKeyRequired.code(), "mask_key_required");
        assert!(KernelError::MaskKeyRequired.detail().contains("mask_key"));

        let proto = KernelError::ProtocolError("boom".to_string());
        assert_eq!(proto.code(), "mixed_object_not_native");
        assert_eq!(proto.detail(), "boom");
    }

    /// Drives every failure path through the real `batch::derive_array` code with
    /// sentinel-laden inputs and checks the exact `(code, detail)` text a Python exception
    /// would carry, without needing a live interpreter to construct a `PyErr`. Catches a future
    /// edit that starts interpolating raw input into a message, not just today's code.
    #[test]
    fn kernel_error_never_embeds_sentinel_bytes() {
        const SENTINEL_KEY_MARKER: &str = "SENTINEL_KEY_MUST_NOT_APPEAR_IN_ANY_ERROR";
        const SENTINEL_SOURCE_MARKER: &str = "SENTINEL_SOURCE_MUST_NOT_APPEAR_IN_ANY_ERROR";
        const SENTINEL_NAMESPACE_MARKER: &str = "SENTINEL_NAMESPACE_MUST_NOT_APPEAR_IN_ANY_ERROR";

        let sentinel_key = SENTINEL_KEY_MARKER.as_bytes();

        let mut messages = Vec::new();

        // Missing mask key: nothing but the fixed message text is possible here.
        messages.push(format!(
            "{}: {}",
            KernelError::MaskKeyRequired.code(),
            KernelError::MaskKeyRequired.detail()
        ));

        // Unsupported type on an array built from sentinel-content data.
        let floats = arrow_array::Float64Array::from(vec![1.0]);
        let err = crate::batch::derive_array(
            &floats,
            Some(sentinel_key),
            SENTINEL_NAMESPACE_MARKER,
            None,
        )
        .unwrap_err();
        messages.push(format!("{}: {}", err.code(), err.detail()));

        // Empty namespace over a sentinel-content string array.
        let strings =
            arrow_array::StringArray::from(vec![Some(SENTINEL_SOURCE_MARKER.to_string())]);
        let err = crate::batch::derive_array(&strings, Some(sentinel_key), "", None).unwrap_err();
        messages.push(format!("{}: {}", err.code(), err.detail()));

        // Wrong-length key (16 bytes, neither 8 nor 32): the error must report the LENGTH,
        // never the sentinel key bytes themselves.
        let wrong_len_key = &sentinel_key[..16];
        let err = crate::batch::derive_array(
            &strings,
            Some(wrong_len_key),
            SENTINEL_NAMESPACE_MARKER,
            None,
        )
        .unwrap_err();
        messages.push(format!("{}: {}", err.code(), err.detail()));

        for msg in messages {
            assert!(
                !msg.contains(SENTINEL_KEY_MARKER),
                "error message embedded the key sentinel: {msg}"
            );
            assert!(
                !msg.contains(SENTINEL_SOURCE_MARKER),
                "error message embedded the source sentinel: {msg}"
            );
            assert!(
                !msg.contains(SENTINEL_NAMESPACE_MARKER),
                "error message embedded the namespace sentinel: {msg}"
            );
        }
    }

    /// Documents the Send/Sync claim `CRYPTO_EXT_ABI` makes for the compiled kernel: the pure
    /// derivation path holds no shared mutable state, so calling it concurrently from many
    /// threads over the same inputs is safe and deterministic. Exercised at the pure-Rust
    /// `derive`/`canonicalize` layer (no GIL, no Python involved) so it runs under `cargo test`
    /// without needing an embedded interpreter.
    #[test]
    fn derive_is_send_sync_and_concurrent_calls_agree() {
        use crate::canonicalize::encode_utf8;
        use crate::derive::derive as raw_derive;
        use std::sync::Arc;
        use std::thread;

        let mask_key: Arc<[u8]> = Arc::from(&[7u8; 32][..]);
        let namespace = "concurrency.check";
        let values: Vec<String> = (0..64).map(|i| format!("row-{i}")).collect();

        let expected: Vec<[u8; 32]> = values
            .iter()
            .map(|v| raw_derive(&mask_key, namespace, &encode_utf8(v)).unwrap())
            .collect();

        let handles: Vec<_> = (0..8)
            .map(|_| {
                let mask_key = Arc::clone(&mask_key);
                let values = values.clone();
                thread::spawn(move || {
                    values
                        .iter()
                        .map(|v| raw_derive(&mask_key, namespace, &encode_utf8(v)).unwrap())
                        .collect::<Vec<_>>()
                })
            })
            .collect();

        for handle in handles {
            let got = handle.join().expect("worker thread must not panic");
            assert_eq!(
                got, expected,
                "concurrent derivation must match the single-threaded result"
            );
        }
    }
}
