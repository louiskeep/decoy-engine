//! Fuzz target over `_kernel::batch::derive_index_array`, the PyO3-free deterministic-Faker
//! pool-index path (validate, canonicalize the first non-null row for precedence, then derive one
//! `u64 % pool_size` per non-null row across the disjoint-slot parallel fill). Same shape as the
//! `derive_array` target: build an arrow-rs array of one admitted type per run from structured
//! `arbitrary` input, call `derive_index_array`, and let libFuzzer's crash detector do the
//! asserting -- a panic, an out-of-bounds access, or a sanitizer-flagged fault IS the finding. A
//! returned `Err` (empty/missing mask key, wrong seed length, empty namespace, `pool_size < 1`,
//! `pool_size > 2**56`, an admitted-but-unlucky value) is a normal fail-closed outcome, not a bug.
//!
//! `pool_size` is generated as the full `i64` the kernel takes (boundary-biased by `arbitrary`),
//! so the campaign reliably drives both pool guards (the `< 1` underflow and the `> 2**56`
//! overflow, INCLUSIVE at the boundary) and the modulo arithmetic in between, not just the happy
//! path. The `PoolSize::NotAnInteger` state lives one layer up at the PyO3 boundary
//! (`extract_pool_size` in arrow_ffi.rs) which this PyO3-free target does not cross; the loader
//! parity tests cover that layer.
//!
//! `shape` forces a fraction of runs to an empty or all-null array: those two shapes validate
//! NOTHING (no pool guard, no seed/namespace check, no derive), so pairing them deliberately with
//! wrong-length keys, empty namespaces, and out-of-range pool sizes exercises the "an empty or
//! all-null batch tolerates every otherwise-fatal argument" precedence branch that plain random
//! `Vec`s would only hit by luck.

#![no_main]

use std::sync::Arc;

use arbitrary::Arbitrary;
use arrow_array::types::TimestampNanosecondType;
use arrow_array::{ArrayRef, BooleanArray, GenericStringArray, Int64Array, PrimitiveArray};
use libfuzzer_sys::fuzz_target;

use _kernel::batch::derive_index_array;

/// One admitted-type column, generated as its own logical values so most iterations reach real
/// canonicalization + derivation work instead of failing an upfront type check.
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
    /// Raw nanosecond epoch ticks, unbounded to a "reasonable" calendar range: the
    /// timestamp-to-ISO8601 canonicalization arithmetic near the domain edges is worth fuzzing.
    TimestampNs(Vec<Option<i64>>),
}

/// Forces the generated array to an empty or all-null shape on a fraction of runs; see the module
/// doc for why these two shapes get deliberate over-representation rather than being left to chance.
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
    /// The full `i64` `derive_index_array` takes, generated directly so `arbitrary`'s
    /// boundary bias reliably drives `pool_size < 1`, `pool_size > 2**56`, and the `2**56`
    /// inclusive edge, not just small positive values.
    pool_size: i64,
    /// A bounded native-thread budget so the fuzzer drives the parallel disjoint-slot fill, not
    /// only the 1-thread case. Clamped to 1..=8 at the call site.
    threads: u8,
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
    // Clamp the fuzz-chosen budget into the kernel's supported 1..=8 range; 0 would be rejected by
    // the PyO3-boundary budget resolver one layer up, which this PyO3-free target does not cross.
    let threads = (input.threads % 8 + 1) as usize;
    // Return value intentionally ignored: every `Err` (missing key, wrong seed length, empty
    // namespace, pool_size out of range, an admitted type this corner case rejects) is a normal
    // fail-closed outcome. Only a panic or a sanitizer-flagged fault is a finding here.
    let _ = derive_index_array(
        array.as_ref(),
        mask_key,
        &input.namespace,
        input.pool_size,
        threads,
    );
});
