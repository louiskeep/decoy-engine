//! Bounded FFI-metadata fuzz target: starts from a real, over-allocated Arrow array and mutates
//! ONLY its declared C Data Interface metadata (length, offset, a buffer pointer, the paired
//! schema's type) -- never the byte content -- within the bounds of that real allocation, then
//! calls `ffi_import::import_ffi` directly (no PyO3 involved).
//!
//! The safety contract this target exists to prove, per the plan: an OWNED array alone is not
//! enough to fuzz FFI metadata safely, because a declared length/offset LARGER than what is
//! really allocated makes `from_ffi` read past the real allocation, and `catch_unwind` (already
//! inside `import_ffi`) does not catch an out-of-bounds memory access -- only a language-level
//! panic. So every mutated field here is computed by construction to stay inside the real,
//! `ROWS`-element backing store:
//!
//! - `length`/`offset` are reduced modulo `ROWS + 1` and clamped so `offset + length <= ROWS`,
//!   which is always true of a real, `ROWS`-long, non-sliced array regardless of the two raw
//!   fuzz bytes that produced them.
//! - `null_count` is always the TRUE count for the chosen `(offset, length)` slice, computed
//!   from the donor's own known null pattern, not a fuzzed field. Two earlier runs of this
//!   target (one overriding `n_buffers`, one leaving `null_count` a raw arbitrary `i64`)
//!   independently found the same underlying arrow-rs behavior: `from_ffi` -> `consume` ->
//!   `ArrayData::new_unchecked` runs `validate_data()` internally and `.unwrap()`s the result
//!   rather than returning the clean `Result::Err` `force_validate` otherwise gives, so ANY
//!   metadata that fails validation panics instead of erroring. Both panics happened INSIDE
//!   `import_ffi`'s own `catch_unwind`, so the real, `panic = "unwind"`, maturin-built extension
//!   catches them cleanly and reports `KernelError::ProtocolError`; only `cargo-fuzz`'s
//!   hardcoded `panic = "abort"` build convention (which this crate's Cargo profile cannot
//!   override) turns each one into a process-ending "crash" here, since abort never unwinds
//!   regardless of where `catch_unwind` sits in the call stack. Once found and adjudicated (see
//!   the T1 record), fuzzing either dimension further would only rediscover the same panic on
//!   nearly every run rather than exploring anything new, so both are pinned to always-valid
//!   values, leaving the campaign free to explore length/offset/buffer-nulling/type instead.
//! - `null_out_buffer` nulls one real buffer pointer (0 = validity, 1 = data) in place: this is
//!   the metadata-mismatch case that genuinely returns cleanly rather than panicking (proven by
//!   `import_ffi_rejects_null_data_buffer_cleanly` in ffi_import.rs), so fuzzing it across many
//!   length/offset combinations is safe and adds real coverage beyond that one example.
//! - the "type" dimension is a full schema swap: the array's own int64 schema, or a real,
//!   freshly exported boolean schema with the SAME buffer count (validity + one data buffer),
//!   paired with the int64 array's real (and possibly length/offset/buffer-nulled) buffers.
//!   Reusing a genuinely different but buffer-count-compatible schema keeps the "wrong type"
//!   dimension inside the same safety bound as everything else.
//!
//! Run under ASan (`cargo +nightly fuzz run ffi_metadata`): a returned `Err` from `import_ffi` is
//! a normal, expected outcome (malformed-but-safely-rejected metadata), not a finding. A crash --
//! a panic that escapes `catch_unwind`, or a sanitizer-reported invalid access -- is the only
//! signal this campaign looks for.

#![no_main]

use arbitrary::Arbitrary;
use arrow_array::ffi::to_ffi;
use arrow_array::{Array, BooleanArray, Int64Array};
use libfuzzer_sys::fuzz_target;

use _kernel::ffi_import::import_ffi;

/// Real element count backing the fuzzed array: every declared `length`/`offset` this target
/// produces is bounded to stay within this many elements, so it never asks `from_ffi` to read
/// past what is genuinely allocated.
const ROWS: usize = 4096;

#[derive(Debug, Arbitrary)]
struct FuzzInput {
    length_raw: u32,
    offset_raw: u32,
    /// Which real buffer pointer (0 = validity, 1 = data) to null out, if any.
    null_out_buffer: Option<u8>,
    /// Pair the mutated int64 array with a real boolean schema instead of its own.
    use_bool_schema: bool,
}

/// A real, over-allocated Int64 array: every one of its `ROWS` elements is genuinely allocated
/// and initialized (a mix of null and non-null), so any declared length/offset within `ROWS`
/// addresses real, initialized memory.
fn int64_donor() -> Int64Array {
    let values: Vec<Option<i64>> = (0..ROWS)
        .map(|i| if i % 7 == 0 { None } else { Some(i as i64) })
        .collect();
    Int64Array::from(values)
}

/// The donor's own null pattern (`i % 7 == 0`), restated here so the true null count for any
/// `(offset, length)` slice can be computed rather than fuzzed -- see the module doc comment for
/// why an incorrect `null_count` just rediscovers an already-adjudicated panic path.
fn true_null_count(offset: usize, length: usize) -> i64 {
    (offset..offset + length).filter(|i| i % 7 == 0).count() as i64
}

/// A small, real boolean array, used only for its exported SCHEMA: a boolean array's buffer
/// layout (validity + one packed-bit data buffer) has the same buffer COUNT as the nullable
/// Int64 donor above, so swapping in this schema while keeping the int64 array's own buffers
/// stays inside the same "never more buffers than really allocated" safety bound.
fn bool_schema_donor() -> BooleanArray {
    BooleanArray::from(vec![Some(true), Some(false), None])
}

fuzz_target!(|input: FuzzInput| {
    let donor = int64_donor();
    let (mut ffi_array, ffi_schema_int) = to_ffi(&donor.to_data()).expect("donor export succeeds");
    assert_eq!(ffi_array.n_buffers, 2, "donor buffer-count assumption drifted");

    let length = (input.length_raw as usize) % (ROWS + 1);
    let offset = (input.offset_raw as usize) % (ROWS - length + 1);
    ffi_array.length = length as i64;
    ffi_array.offset = offset as i64;
    ffi_array.null_count = true_null_count(offset, length);

    if let Some(raw) = input.null_out_buffer {
        let idx = (raw % 2) as usize;
        // SAFETY: `idx` is 0 or 1, both within the real, 2-pointer buffers array `to_ffi`
        // allocated for this donor; writing a null through an in-bounds pointer is always sound.
        unsafe {
            *ffi_array.buffers.add(idx) = std::ptr::null();
        }
    }

    let ffi_schema = if input.use_bool_schema {
        let bool_donor = bool_schema_donor();
        let (bool_array, bool_schema) =
            to_ffi(&bool_donor.to_data()).expect("bool schema donor export succeeds");
        assert_eq!(
            bool_array.n_buffers, 2,
            "bool schema donor buffer-count assumption drifted"
        );
        bool_schema
    } else {
        ffi_schema_int
    };

    // Ignored on purpose: every `Err` (rejected by `validate_data`, or a caught internal panic)
    // is a normal outcome for metadata this adversarial. Only a crash under libFuzzer/ASan is a
    // finding.
    let _ = import_ffi(ffi_array, ffi_schema);
});
