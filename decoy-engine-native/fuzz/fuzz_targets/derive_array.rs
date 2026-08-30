//! Fuzz target over `_kernel::batch::derive_array`, the PyO3-free row-loop (validate,
//! canonicalize, derive, hex-encode). Builds an arrow-rs array of one admitted type per run
//! from structured `arbitrary` input (not raw bytes fed straight to the canonicalizer, since
//! most byte strings would just fail the outer type check before reaching the interesting
//! code paths -- the canonicalizer's own per-type branches, encode_int's arithmetic, and the
//! timestamp fraction formatting are what this campaign wants to exercise), then calls
//! `derive_array` and lets libFuzzer's own crash detector do the asserting: a panic, an
//! out-of-bounds access, or another sanitizer-detected fault IS the failure signal. A returned
//! `Err` (empty namespace, missing mask key, an admitted-but-otherwise-unlucky value) is a
//! normal, expected outcome, not a finding.
//!
//! `shape` deliberately forces a fraction of runs to an empty or all-null array regardless of
//! what `array` itself generated: relying on `arbitrary`'s natural tendency to sometimes emit
//! a short or all-`None` `Vec` would eventually cover this, but this campaign is specifically
//! about the boundary where seed-length and namespace validation move INSIDE `derive::derive`
//! (fired per non-null row, never for an empty or all-null batch), so those two shapes are
//! forced often enough to pair reliably with the equally arbitrary `mask_key`/`namespace`
//! (including wrong-length keys and empty namespaces) and `truncate` (including negative
//! values) generated alongside them.

#![no_main]

use std::sync::Arc;

use arbitrary::Arbitrary;
use arrow_array::types::TimestampNanosecondType;
use arrow_array::{ArrayRef, BooleanArray, GenericStringArray, Int64Array, PrimitiveArray};
use libfuzzer_sys::fuzz_target;

use _kernel::batch::derive_array;

/// One admitted-type column, generated directly as its own logical values rather than as raw
/// bytes, so most fuzz iterations reach real canonicalization work instead of failing an
/// upfront type check.
#[derive(Debug, Arbitrary)]
enum FuzzArray {
    Utf8(Vec<Option<String>>),
    LargeUtf8(Vec<Option<String>>),
    Bool(Vec<Option<bool>>),
    Int8(Vec<Option<i8>>),
    Int16(Vec<Option<i16>>),
    Int32(Vec<Option<i32>>),
    Int64(Vec<Option<i64>>),
    UInt8(Vec<Option<u8>>),
    UInt16(Vec<Option<u16>>),
    UInt32(Vec<Option<u32>>),
    UInt64(Vec<Option<u64>>),
    /// Raw nanosecond epoch ticks, not bounded to a "reasonable" calendar range: the
    /// timestamp-to-ISO8601 formatting path is exactly the kind of arithmetic (day/month/year
    /// decomposition near the domain's edges) worth fuzzing with extreme values.
    TimestampNs(Vec<Option<i64>>),
}

/// Forces the generated array to an empty or all-null shape on a fraction of runs; see the
/// module doc comment for why these two shapes specifically get deliberate over-representation
/// rather than being left to chance.
#[derive(Debug, Arbitrary)]
enum ArrayShapeBias {
    AsGenerated,
    ForceEmpty,
    ForceAllNull,
}

#[derive(Debug, Arbitrary)]
struct FuzzInput {
    array: FuzzArray,
    shape: ArrayShapeBias,
    mask_key: Vec<u8>,
    namespace: String,
    /// The same width `batch::derive_array` actually takes, generated directly (not narrowed
    /// through a smaller int type first): `arbitrary`'s integer generation is boundary-biased,
    /// so this reliably exercises `python_slice_stop`'s extreme ends (`isize::MIN`/`MAX`), not
    /// just values near zero. The Python-int-overflow clamp in `extract_truncate` (arrow_ffi.rs)
    /// lives one layer up, at the PyO3 boundary this PyO3-free target does not cross; parity
    /// tests against the live reference cover that layer instead (see
    /// tests/native/test_keyed_derivation_kernel_parity.py).
    truncate: Option<isize>,
}

fn apply_shape_bias<T>(mut values: Vec<Option<T>>, shape: &ArrayShapeBias) -> Vec<Option<T>> {
    match shape {
        ArrayShapeBias::AsGenerated => values,
        ArrayShapeBias::ForceEmpty => Vec::new(),
        ArrayShapeBias::ForceAllNull => {
            for slot in &mut values {
                *slot = None;
            }
            values
        }
    }
}

fn build_array(array: FuzzArray, shape: &ArrayShapeBias) -> ArrayRef {
    match array {
        FuzzArray::Utf8(v) => Arc::new(GenericStringArray::<i32>::from(apply_shape_bias(v, shape))),
        FuzzArray::LargeUtf8(v) => {
            Arc::new(GenericStringArray::<i64>::from(apply_shape_bias(v, shape)))
        }
        FuzzArray::Bool(v) => Arc::new(BooleanArray::from(apply_shape_bias(v, shape))),
        FuzzArray::Int8(v) => Arc::new(PrimitiveArray::<arrow_array::types::Int8Type>::from(
            apply_shape_bias(v, shape),
        )),
        FuzzArray::Int16(v) => Arc::new(PrimitiveArray::<arrow_array::types::Int16Type>::from(
            apply_shape_bias(v, shape),
        )),
        FuzzArray::Int32(v) => Arc::new(PrimitiveArray::<arrow_array::types::Int32Type>::from(
            apply_shape_bias(v, shape),
        )),
        FuzzArray::Int64(v) => Arc::new(Int64Array::from(apply_shape_bias(v, shape))),
        FuzzArray::UInt8(v) => Arc::new(PrimitiveArray::<arrow_array::types::UInt8Type>::from(
            apply_shape_bias(v, shape),
        )),
        FuzzArray::UInt16(v) => Arc::new(PrimitiveArray::<arrow_array::types::UInt16Type>::from(
            apply_shape_bias(v, shape),
        )),
        FuzzArray::UInt32(v) => Arc::new(PrimitiveArray::<arrow_array::types::UInt32Type>::from(
            apply_shape_bias(v, shape),
        )),
        FuzzArray::UInt64(v) => Arc::new(PrimitiveArray::<arrow_array::types::UInt64Type>::from(
            apply_shape_bias(v, shape),
        )),
        FuzzArray::TimestampNs(v) => Arc::new(
            PrimitiveArray::<TimestampNanosecondType>::from(apply_shape_bias(v, shape))
                .with_timezone(Arc::<str>::from("UTC")),
        ),
    }
}

fuzz_target!(|input: FuzzInput| {
    let array = build_array(input.array, &input.shape);
    let mask_key = if input.mask_key.is_empty() {
        None
    } else {
        Some(input.mask_key.as_slice())
    };
    let truncate = input.truncate;
    // The return value is intentionally ignored: every `Err` variant (missing key, wrong seed
    // length, empty namespace, an admitted type this particular corner case still rejects) is
    // a normal fail-closed outcome. Only a panic or a sanitizer-flagged fault is a finding here.
    let _ = derive_array(array.as_ref(), mask_key, &input.namespace, truncate);
});
