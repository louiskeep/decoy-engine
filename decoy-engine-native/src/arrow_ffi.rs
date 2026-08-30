//! Arrow C Data Interface boundary and the PyO3 entry point for `derive_batch`.
//!
//! Imports a Python `pa.Array` via the Arrow PyCapsule Interface
//! (<https://arrow.apache.org/docs/format/CDataInterface/PyCapsuleInterface.html>, the
//! `__arrow_c_array__` protocol pyarrow implements since version 14), using the `arrow` crate's
//! own C Data Interface bridge (`arrow_array::ffi`) rather than hand-rolled FFI: only the
//! PyCapsule plumbing around it is ours. Exports the derived column back the same way, via
//! `pa.Array._import_from_c_capsule`.
//!
//! Fail-before-output and panic safety: `derive_batch` validates the mask key, namespace, and
//! admitted input type before touching any row, and the whole entry point runs inside
//! `catch_unwind` so an unexpected panic anywhere on this path (including inside arrow-rs's own
//! FFI import, which asserts rather than returning `Result` on a few malformed-metadata shapes)
//! is converted to a typed Python exception instead of unwinding across the `extern "C"`
//! boundary into the interpreter.

use std::ffi::CStr;
use std::panic::{catch_unwind, AssertUnwindSafe};

use arrow_array::ffi::{to_ffi, FFI_ArrowArray, FFI_ArrowSchema};
use arrow_array::{Array, ArrayRef, StringArray};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyCapsule, PyCapsuleMethods, PyInt, PyTuple};

use crate::ffi_import::{import_ffi, KernelError};

const ARROW_SCHEMA_CAPSULE_NAME: &CStr = c"arrow_schema";
const ARROW_ARRAY_CAPSULE_NAME: &CStr = c"arrow_array";

fn to_py_err(err: KernelError) -> PyErr {
    // Redacted by construction: every KernelError variant's detail is built from type names,
    // byte lengths, or fixed strings, never from row content, mask_key bytes, or canonical
    // source bytes (see CanonError / DeriveError doc comments). This crate has no dependency on
    // the engine's Python package, so it raises a plain, coded ValueError here; the engine-side
    // loader maps these coded ValueErrors onto its own MaskKeyRequiredError /
    // CryptoExtensionUnavailableError classes.
    PyValueError::new_err(format!("{}: {}", err.code(), err.detail()))
}

/// Import a Python `pa.Array`-shaped object into an arrow-rs `ArrayRef`, over the Arrow
/// C Data Interface. Never panics: an internal panic from arrow-rs's own FFI import path is
/// caught and converted to a `KernelError`.
fn import_array(obj: &Bound<'_, PyAny>) -> Result<ArrayRef, KernelError> {
    let call = obj.call_method0("__arrow_c_array__").map_err(|e| {
        KernelError::ProtocolError(format!(
            "expected an object implementing __arrow_c_array__ (a pyarrow Array); {e}"
        ))
    })?;
    let tuple = call.cast::<PyTuple>().map_err(|_| {
        KernelError::ProtocolError("__arrow_c_array__ did not return a tuple".into())
    })?;
    if tuple.len() != 2 {
        return Err(KernelError::ProtocolError(
            "__arrow_c_array__ must return exactly (schema_capsule, array_capsule)".into(),
        ));
    }
    let schema_capsule = tuple
        .get_item(0)
        .and_then(|o| o.cast_into::<PyCapsule>().map_err(PyErr::from))
        .map_err(|e: PyErr| KernelError::ProtocolError(format!("invalid schema capsule: {e}")))?;
    let array_capsule = tuple
        .get_item(1)
        .and_then(|o| o.cast_into::<PyCapsule>().map_err(PyErr::from))
        .map_err(|e: PyErr| KernelError::ProtocolError(format!("invalid array capsule: {e}")))?;

    let schema_ptr = schema_capsule
        .pointer_checked(Some(ARROW_SCHEMA_CAPSULE_NAME))
        .map_err(|e| KernelError::ProtocolError(format!("bad arrow_schema capsule: {e}")))?
        .cast::<FFI_ArrowSchema>();
    let array_ptr = array_capsule
        .pointer_checked(Some(ARROW_ARRAY_CAPSULE_NAME))
        .map_err(|e| KernelError::ProtocolError(format!("bad arrow_array capsule: {e}")))?
        .cast::<FFI_ArrowArray>();

    // SAFETY: `pointer_checked` validated the capsule name matches the Arrow PyCapsule
    // Interface convention; `from_raw` takes ownership, moving the C struct out (leaving the
    // capsule's copy released), which is exactly the C Data Interface's documented move
    // semantics for a one-shot import.
    let ffi_schema = unsafe { FFI_ArrowSchema::from_raw(schema_ptr.as_ptr()) };
    let ffi_array = unsafe { FFI_ArrowArray::from_raw(array_ptr.as_ptr()) };

    import_ffi(ffi_array, ffi_schema)
}

/// Export an arrow-rs `StringArray` back to Python as a `pa.Array`, over the same C Data
/// Interface boundary, via `pa.Array._import_from_c_capsule`.
fn export_string_array(py: Python<'_>, array: &StringArray) -> PyResult<Py<PyAny>> {
    let (ffi_array, ffi_schema) = to_ffi(&array.to_data())
        .map_err(|e| PyValueError::new_err(format!("failed to export the derived column: {e}")))?;
    let schema_capsule = PyCapsule::new_with_value(py, ffi_schema, ARROW_SCHEMA_CAPSULE_NAME)?;
    let array_capsule = PyCapsule::new_with_value(py, ffi_array, ARROW_ARRAY_CAPSULE_NAME)?;
    let pa = py.import("pyarrow")?;
    let out = pa
        .getattr("Array")?
        .call_method1("_import_from_c_capsule", (schema_capsule, array_capsule))?;
    Ok(out.unbind())
}

/// Convert the raw Python `truncate` argument to `Option<isize>`, tolerating an arbitrary-size
/// Python `int` (Python slice indices have no width limit; `isize` does) while REFUSING
/// anything that merely looks int-like.
///
/// `token[:truncate]` in the reference only ever cares about `truncate`'s value relative to
/// the 64-character token: any stop at or beyond 64 keeps everything, any stop at or below -64
/// empties it, and `python_slice_stop` (in `derive.rs`) already clamps `isize::MAX`/`isize::MIN`
/// to exactly those outcomes. So a magnitude beyond `isize` can be replaced by the extreme
/// `isize` value matching its SIGN with no loss of the only behavior that matters.
///
/// The `cast::<PyInt>()` gate comes FIRST and is load-bearing, not a style choice: the
/// declared contract is `int | None`, so anything else (a string, a float, an object with a
/// custom `__index__`) must raise `TypeError` here, exactly as the reference's own slicing
/// would refuse it.
///
/// `PyInt` alone is not enough to make a Python-level sign check (`.lt(0)`) safe, though: an
/// `int` SUBCLASS can override `__lt__` to lie (return `False` unconditionally regardless of
/// the wrapped value), which would make a "catch OverflowError, then call `.lt(0)`" clamp
/// treat any such object as `+isize::MAX` no matter its true value. `PyNumber_AsSsize_t` is the
/// fix: it is the exact primitive CPython's own slice-index conversion
/// (`_PyEval_SliceIndex`) uses, reading the integer's C-level value directly and, with a NULL
/// `exc` argument, clamping to `PY_SSIZE_T_MAX`/`PY_SSIZE_T_MIN` by that TRUE sign on overflow
/// instead of raising -- invoking no `__lt__`, no `__index__`, no other overridable method.
fn extract_truncate(value: Option<&Bound<'_, PyAny>>) -> PyResult<Option<isize>> {
    let Some(value) = value else {
        return Ok(None);
    };
    let value = value
        .cast::<PyInt>()
        .map_err(|_| PyTypeError::new_err("truncate must be an int or None"))?;
    let py = value.py();
    // SAFETY: `value.as_ptr()` is a valid, GIL-held `PyObject*` for a confirmed `PyInt` for the
    // duration of this call; passing `exc = NULL` selects CPython's own sign-correct-clamp
    // behavior (see the doc comment above) rather than the overflow-raising variant.
    let stop = unsafe { pyo3::ffi::PyNumber_AsSsize_t(value.as_ptr(), std::ptr::null_mut()) };
    // `PyNumber_AsSsize_t` signals a genuine failure (not the overflow case, which is
    // clamped rather than raised here) by leaving a Python exception set, not solely by its
    // return value (`-1` is also `stop`'s legitimate value when `truncate == -1`).
    if let Some(err) = PyErr::take(py) {
        return Err(err);
    }
    Ok(Some(stop))
}

/// The actual `derive_batch` work: import, then delegate the validate/canonicalize/derive loop
/// to `batch::derive_array` (the pure, PyO3-free core also used directly by Rust-only tests),
/// then export. Split from the `#[pyfunction]` wrapper so `catch_unwind` covers the whole thing
/// uniformly.
fn derive_batch_checked(
    py: Python<'_>,
    values: &Bound<'_, PyAny>,
    mask_key: Option<&[u8]>,
    namespace: &str,
    truncate: Option<isize>,
) -> Result<Py<PyAny>, KernelError> {
    // Fail before any FFI import when the key is missing: the fail-before-output contract
    // applies before we even touch the caller's array.
    if mask_key.map(|k| k.is_empty()).unwrap_or(true) {
        return Err(KernelError::MaskKeyRequired);
    }

    let array = import_array(values)?;
    let result_array = crate::batch::derive_array(array.as_ref(), mask_key, namespace, truncate)
        .map_err(KernelError::from)?;

    export_string_array(py, &result_array).map_err(|e| {
        KernelError::ProtocolError(format!("failed to export the derived string array: {e}"))
    })
}

/// `derive_batch(values, *, mask_key, namespace, truncate=None) -> pa.Array`
///
/// Matches the `KeyedDerivationKernel` Protocol in `decoy_engine.execution.native._crypto_ext`
/// exactly (`mask_key` named, not `seed`). Accepts only a typed `pa.Array` over an admitted
/// Arrow type; rejects everything else (including the mixed-object Python list form the
/// pure-Python reference also accepts) with `mixed_object_not_native`.
///
/// `truncate` is a Python-style slice stop matching the reference's `token[:truncate]` on the
/// 64-character hex string exactly: `None` keeps everything, and any genuine `int` (`bool`
/// included) works no matter how large its magnitude (Python ints are arbitrary precision;
/// `extract_truncate` clamps by sign rather than raising `OverflowError` for a value the
/// reference accepts fine). Anything that is not `None` or a real `int` -- a string, a float,
/// or an object that merely looks int-like via a custom `__index__` -- raises `TypeError`
/// before any row is touched, the same way the reference's own slicing refuses it. See
/// `derive::hex_token_into` for the slice-semantics implementation this delegates to.
#[pyfunction]
#[pyo3(signature = (values, *, mask_key, namespace, truncate=None))]
fn derive_batch(
    py: Python<'_>,
    values: &Bound<'_, PyAny>,
    mask_key: Option<Vec<u8>>,
    namespace: String,
    truncate: Option<Bound<'_, PyAny>>,
) -> PyResult<Py<PyAny>> {
    let truncate = extract_truncate(truncate.as_ref())?;
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        derive_batch_checked(py, values, mask_key.as_deref(), &namespace, truncate)
    }));
    match outcome {
        Ok(Ok(result)) => Ok(result),
        Ok(Err(kernel_err)) => Err(to_py_err(kernel_err)),
        Err(_panic) => Err(PyValueError::new_err(
            "internal_panic: the native kernel hit an unexpected internal error and stopped \
             before producing output"
                .to_string(),
        )),
    }
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(derive_batch, m)?)?;
    Ok(())
}
