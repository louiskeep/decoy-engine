//! Batch-invariance property tests: a whole-column derivation must equal the concatenation of
//! the same values processed as separate partitions, for any partitioning (including empty and
//! prime-sized batches). `derive`/`canonicalize_row` are pure per-row functions with no shared
//! state across calls, so this property should hold by construction; these tests catch a
//! regression that accidentally introduces batch-boundary or row-order dependence.

use std::sync::Arc;

use _kernel::canonicalize::canonicalize_row;
use _kernel::derive::{derive, hex_token};
use arrow_array::{Array, ArrayRef, StringArray};
use proptest::prelude::*;

const MASK_KEY: [u8; 32] = {
    let mut k = [0u8; 32];
    let mut i = 0;
    while i < 32 {
        k[i] = i as u8;
        i += 1;
    }
    k
};
const NAMESPACE: &str = "proptest.batch_invariance";

/// Derive a whole utf8 column in one pass (no partitioning), the baseline every partitioned
/// run is compared against.
fn derive_whole(values: &[Option<String>]) -> Vec<Option<String>> {
    let array: ArrayRef = Arc::new(StringArray::from(values.to_vec()));
    (0..array.len())
        .map(|i| {
            canonicalize_row(array.as_ref(), i)
                .unwrap()
                .map(|canonical| {
                    hex_token(&derive(&MASK_KEY, NAMESPACE, &canonical).unwrap(), None)
                })
        })
        .collect()
}

/// Split `values` into batches per `cuts` (a sequence of batch lengths that may include zeros,
/// so empty batches interleave with nonempty ones), derive each batch separately, and
/// concatenate.
fn derive_partitioned(values: &[Option<String>], cuts: &[usize]) -> Vec<Option<String>> {
    let mut out = Vec::with_capacity(values.len());
    let mut offset = 0;
    for &len in cuts {
        let end = (offset + len).min(values.len());
        if offset >= values.len() {
            break;
        }
        out.extend(derive_whole(&values[offset..end]));
        offset = end;
    }
    if offset < values.len() {
        out.extend(derive_whole(&values[offset..]));
    }
    out
}

fn arb_values() -> impl Strategy<Value = Vec<Option<String>>> {
    proptest::collection::vec(proptest::option::of("[a-zA-Z0-9]{0,12}"), 0..64)
}

proptest! {
    /// Any partition of any input: whole-array output equals the concatenation of partitioned
    /// output. Covers uneven and prime-sized batches, and batches of size 1, via the shrinker's
    /// own exploration of `cuts`.
    #[test]
    fn whole_equals_concatenated_partitions(
        values in arb_values(),
        cuts in proptest::collection::vec(0usize..7, 0..20),
    ) {
        let whole = derive_whole(&values);
        let partitioned = derive_partitioned(&values, &cuts);
        prop_assert_eq!(whole, partitioned);
    }

    /// Prime-sized batches specifically (the plan's named case): chunk a longer input into
    /// batches of a fixed prime size and confirm the concatenation still matches.
    #[test]
    fn prime_sized_batches_match_whole(
        values in proptest::collection::vec(proptest::option::of("[a-zA-Z0-9]{0,8}"), 0..97),
        batch_size in prop_oneof![Just(2usize), Just(3), Just(5), Just(7), Just(11), Just(13)],
    ) {
        let whole = derive_whole(&values);
        let cuts: Vec<usize> = std::iter::repeat_n(batch_size, values.len() / batch_size + 1).collect();
        let partitioned = derive_partitioned(&values, &cuts);
        prop_assert_eq!(whole, partitioned);
    }
}

/// Empty batch, explicitly (not just as a degenerate case the property strategy might skip):
/// deriving zero rows must produce zero rows, and must not affect a subsequent batch.
#[test]
fn empty_batch_is_a_no_op_between_nonempty_batches() {
    let values = vec![
        Some("a".to_string()),
        Some("b".to_string()),
        Some("c".to_string()),
    ];
    let whole = derive_whole(&values);
    let partitioned = derive_partitioned(&values, &[1, 0, 0, 2]);
    assert_eq!(whole, partitioned);
}

/// Null and empty-string are logically distinct source values (the crypto-testing-reference's
/// "null-vs-empty distinction"): a null slot must derive to `None`, while an empty string is a
/// real, hashed source value, and the two must not collide with each other's output.
#[test]
fn null_and_empty_string_are_distinct() {
    let values = vec![None, Some(String::new())];
    let out = derive_whole(&values);
    assert_eq!(out[0], None);
    assert!(out[1].is_some());
    assert_ne!(out[0], out[1]);
}
