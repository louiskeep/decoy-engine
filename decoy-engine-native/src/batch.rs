//! The pure row-loop: validate, canonicalize, derive, hex-encode every row of one Arrow array.
//!
//! Split out from `arrow_ffi` so it has no PyO3 dependency at all: the allocation-bound test
//! (`tests/allocation_bound.rs`) and any other Rust-only harness calls this directly, over a
//! real arrow-rs array, with no Python interpreter involved.

use arrow_array::builder::StringBuilder;
use arrow_array::{Array, StringArray};

use crate::canonicalize::{canonicalize_row, is_admitted_type, CanonError};
use crate::derive::{derive, hex_token_into, DeriveError, HEX_LEN};

/// Everything that can go wrong deriving a batch, independent of how the array arrived (Python
/// FFI import or a Rust-constructed array in a test).
///
/// Seed-length and namespace validation are NOT here: they live inside `derive::derive`, fired
/// per non-null row, because the reference (`_ReferenceKeyedDerivation.derive_batch`) only
/// validates them there too. An empty or all-null batch never calls `derive()` in the
/// reference, so it must never raise here either; a batch with at least one non-null row still
/// surfaces `BatchError::Derive` with the reference's own `seed_wrong_length` /
/// `namespace_empty` code the moment that row is reached.
#[derive(Debug)]
pub enum BatchError {
    Canon(CanonError),
    Derive(DeriveError),
    /// Missing or empty mask key (fail-before-output; no row has been touched yet). Mirrors
    /// `_require_mask_key`, which the reference calls once, unconditionally, before its loop
    /// -- unlike seed length, this check does not depend on there being a non-null row.
    MaskKeyRequired,
}

impl From<CanonError> for BatchError {
    fn from(e: CanonError) -> Self {
        BatchError::Canon(e)
    }
}

impl From<DeriveError> for BatchError {
    fn from(e: DeriveError) -> Self {
        BatchError::Derive(e)
    }
}

impl BatchError {
    pub fn code(&self) -> &str {
        match self {
            BatchError::Canon(e) => e.code,
            BatchError::Derive(e) => e.code,
            BatchError::MaskKeyRequired => "mask_key_required",
        }
    }

    pub fn detail(&self) -> String {
        match self {
            BatchError::Canon(e) => e.detail.clone(),
            BatchError::Derive(e) => e.detail.clone(),
            BatchError::MaskKeyRequired => {
                "mask_key is required and must be non-empty; refusing to emit unkeyed output"
                    .to_string()
            }
        }
    }
}

/// Validate the mask key and the array's admitted type, then canonicalize + derive + hex-encode
/// every row, returning one output string per input row (null in, null out; no derivation for a
/// null slot).
///
/// `truncate` is a signed Python-style slice stop (see `derive::hex_token_into`), not a
/// `usize`: the reference's `token[:truncate]` accepts a negative `truncate` and slices from
/// the end rather than erroring, so the native kernel must too.
pub fn derive_array(
    array: &dyn Array,
    mask_key: Option<&[u8]>,
    namespace: &str,
    truncate: Option<isize>,
) -> Result<StringArray, BatchError> {
    let mask_key = match mask_key {
        Some(k) if !k.is_empty() => k,
        _ => return Err(BatchError::MaskKeyRequired),
    };

    let data_type = array.data_type();
    if !is_admitted_type(data_type) {
        return Err(BatchError::Canon(CanonError::unsupported(format!(
            "Arrow type {data_type:?} is not in the native keyed-hash admitted set"
        ))));
    }

    // `StringBuilder` writes hex characters straight into its own growing output buffer; the
    // per-row `[u8; HEX_LEN]` stack buffer means no owned `String` is ever heap-allocated per
    // row on top of that (see `hex_token_into`'s doc comment for why this shape matters).
    let mut builder = StringBuilder::with_capacity(array.len(), array.len() * HEX_LEN);
    let mut hex_buf = [0u8; HEX_LEN];
    for i in 0..array.len() {
        match canonicalize_row(array, i)? {
            None => builder.append_null(),
            Some(canonical) => {
                // `derive()` validates seed length and namespace HERE, only when a non-null
                // row is actually reached -- an empty or all-null array never gets here, so it
                // never raises, matching the reference exactly.
                let digest = derive(mask_key, namespace, &canonical)?;
                builder.append_value(hex_token_into(&digest, truncate, &mut hex_buf));
            }
        }
    }
    Ok(builder.finish())
}

#[cfg(test)]
mod tests {
    use super::*;
    use arrow_array::StringArray;

    fn one_row_array() -> StringArray {
        StringArray::from(vec![Some("alice")])
    }

    /// The two lengths the reference accepts (8-byte job_seed, 32-byte mask_key) must still
    /// succeed: this guard must reject only what the reference rejects, never narrow the
    /// admitted set further.
    #[test]
    fn accepts_the_two_reference_seed_lengths() {
        let array = one_row_array();
        for len in [8usize, 32] {
            let key = vec![0u8; len];
            let result = derive_array(&array, Some(&key), "ns", None);
            assert!(
                result.is_ok(),
                "length {len} must be accepted, got {result:?}"
            );
        }
    }

    /// Every OTHER length -- including ones that are merely "close" to 8 or 32 -- must fail
    /// closed with the reference's own `seed_wrong_length` code once a non-null row is reached.
    #[test]
    fn rejects_every_other_seed_length_with_seed_wrong_length() {
        let array = one_row_array();
        for len in [1usize, 7, 9, 16, 20, 31, 33, 64] {
            let key = vec![0u8; len];
            let err = derive_array(&array, Some(&key), "ns", None)
                .expect_err(&format!("length {len} must be rejected"));
            assert_eq!(err.code(), "seed_wrong_length");
            assert!(
                err.detail().contains(&len.to_string()),
                "detail should report the offending length {len}: {}",
                err.detail()
            );
        }
    }

    /// A present-but-empty mask key (`Some(&[])`, distinct from a wholly missing `None`) must
    /// fail closed the same way a missing key does: the guard checks non-emptiness, not just
    /// presence, matching `_require_mask_key`'s own check on the reference side.
    #[test]
    fn present_but_empty_mask_key_fails_closed_with_mask_key_required() {
        let array = one_row_array();
        let empty_key: &[u8] = &[];
        let err = derive_array(&array, Some(empty_key), "ns", None).unwrap_err();
        assert!(matches!(err, BatchError::MaskKeyRequired));
        assert_eq!(err.code(), "mask_key_required");
        assert!(err.detail().contains("mask_key"));
    }

    /// A batch with at least one non-null row must produce no output at all for a wrong-length
    /// key (Rust's `Result` already guarantees this structurally: an `Err` return carries no
    /// `StringArray`), and must report the reference's own code.
    #[test]
    fn wrong_length_key_with_a_non_null_row_produces_no_output_at_all() {
        let array = StringArray::from(vec![Some("a"), Some("b"), Some("c")]);
        let err = derive_array(&array, Some(&[0u8; 20]), "ns", None).unwrap_err();
        assert_eq!(err.code(), "seed_wrong_length");
        assert!(err.detail().contains("20"));
    }

    /// The reference's `_ReferenceKeyedDerivation.derive_batch` never calls `derive()` for an
    /// empty batch (nothing to iterate), so a wrong-length key or an empty namespace must not
    /// raise: the native kernel must match, not "fail extra safely" ahead of the reference.
    #[test]
    fn empty_batch_with_wrong_length_key_or_empty_namespace_succeeds() {
        let empty = StringArray::from(Vec::<Option<&str>>::new());
        let wrong_len_key = [0u8; 20];
        let valid_key = [0u8; 32];

        let out = derive_array(&empty, Some(&wrong_len_key), "ns", None).unwrap();
        assert_eq!(out.len(), 0);

        let out = derive_array(&empty, Some(&valid_key), "", None).unwrap();
        assert_eq!(out.len(), 0);
    }

    /// Same reasoning as the empty-batch case, but with rows present and all of them null:
    /// `derive()` is still never called (every row takes the `None` branch), so this must
    /// still succeed, returning an all-null column.
    #[test]
    fn all_null_batch_with_wrong_length_key_or_empty_namespace_succeeds() {
        let all_null = StringArray::from(vec![None::<&str>, None, None]);
        let wrong_len_key = [0u8; 20];
        let valid_key = [0u8; 32];

        let out = derive_array(&all_null, Some(&wrong_len_key), "ns", None).unwrap();
        assert_eq!(out.to_data().null_count(), 3);

        let out = derive_array(&all_null, Some(&valid_key), "", None).unwrap();
        assert_eq!(out.to_data().null_count(), 3);
    }

    /// The mirror case: as soon as ONE row is non-null, both bad-key-length and empty-namespace
    /// must fail closed, even alongside nulls in the same batch.
    #[test]
    fn one_non_null_row_among_nulls_still_fails_closed() {
        let mixed = StringArray::from(vec![None, Some("alice"), None]);
        let wrong_len_key = [0u8; 20];
        let valid_key = [0u8; 32];

        let err = derive_array(&mixed, Some(&wrong_len_key), "ns", None).unwrap_err();
        assert_eq!(err.code(), "seed_wrong_length");

        let err = derive_array(&mixed, Some(&valid_key), "", None).unwrap_err();
        assert_eq!(err.code(), "namespace_empty");
    }
}
