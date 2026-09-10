//! The pure row-loop: validate, canonicalize, derive, hex-encode every row of one Arrow array.
//!
//! Split out from `arrow_ffi` so it has no PyO3 dependency at all: the allocation-bound test
//! (`tests/allocation_bound.rs`) and any other Rust-only harness calls this directly, over a
//! real arrow-rs array, with no Python interpreter involved.

use arrow_array::{Array, StringArray};
use arrow_buffer::{Buffer, OffsetBuffer, ScalarBuffer};
use rayon::prelude::*;

use crate::canonicalize::{canonicalize_row, is_admitted_type, CanonError};
use crate::derive::{hex_token_into, DeriveContext, DeriveError, HEX_LEN};
use crate::threads::shared_native_pool;

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
    /// The total output size for the batch would exceed `i32::MAX`, which a `StringArray`'s i32
    /// offset buffer cannot represent. The pre-1.5 `StringBuilder` path had the same i32 ceiling
    /// (a >2GB batch is already outside this kernel's per-batch contract; the engine chunks well
    /// under it), but silent wrapping would corrupt offsets, so we fail closed instead.
    OffsetOverflow,
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
            BatchError::OffsetOverflow => "native_offset_overflow",
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
            BatchError::OffsetOverflow => {
                "batch output exceeds the i32 StringArray offset limit (~2GB); split the batch"
                    .to_string()
            }
        }
    }
}

/// One contiguous row range and the disjoint output-byte slice it fills. `split_at_mut` carves the
/// single values buffer into these non-overlapping `&mut [u8]` windows (one per range), so workers
/// never alias each other's memory and no `unsafe` or per-worker builder is needed (Task 1.5).
struct RangeTask<'a> {
    row_lo: usize,
    row_hi: usize,
    /// Exactly `offsets[row_hi] - offsets[row_lo]` bytes: the output for rows `row_lo..row_hi`.
    out: &'a mut [u8],
}

/// The constant per-row output width for this `truncate`: the keyed hash is always a 32-byte
/// digest -> 64 hex chars, and `token[:truncate]` slices to a length that depends only on
/// `truncate` and the constant 64, never on the digest bytes. So every non-null row has the SAME
/// width, which is what lets the offset layout be computed from the null mask alone, before any
/// derivation. A future variable-width kernel (e.g. redact) would have to revisit this.
fn constant_output_width(truncate: Option<isize>) -> usize {
    let mut buf = [0u8; HEX_LEN];
    hex_token_into(&[0u8; 32], truncate, &mut buf).len()
}

/// Validate the mask key and the array's admitted type, then canonicalize + derive + hex-encode
/// every row across up to `threads` Rayon workers, returning one output string per input row
/// (null in, null out; no derivation for a null slot). Output is byte-identical to a single-thread
/// run at every thread count: each row's hash depends only on its own canonical bytes and the
/// shared per-batch key, so the row partition cannot change any byte.
///
/// `truncate` is a signed Python-style slice stop (see `derive::hex_token_into`), not a
/// `usize`: the reference's `token[:truncate]` accepts a negative `truncate` and slices from
/// the end rather than erroring, so the native kernel must too.
///
/// `threads` is the resolved per-job budget (`NativeThreadBudget::threads`): it bounds the number
/// of row ranges, and therefore the parallelism, WITHIN the one shared host-sized pool. `1` means a
/// single range (sequential work on a pool worker).
pub fn derive_array(
    array: &dyn Array,
    mask_key: Option<&[u8]>,
    namespace: &str,
    truncate: Option<isize>,
    threads: usize,
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

    let len = array.len();
    let non_null = len - array.null_count();
    let width = constant_output_width(truncate);

    // Serial pre-pass: the full i32 offset layout from the null mask alone (no derivation yet).
    // `offsets[i+1] = offsets[i] + (valid ? width : 0)`; a null row keeps the previous offset
    // (0-width) and is marked null in the output validity below. Fail closed on i32 overflow
    // rather than wrap: the pre-1.5 `StringBuilder` had the same i32 ceiling, and a >2GB batch is
    // outside this kernel's per-batch contract (the engine chunks far under it).
    let mut offsets: Vec<i32> = Vec::with_capacity(len + 1);
    offsets.push(0);
    let mut acc: i32 = 0;
    for i in 0..len {
        if array.is_valid(i) {
            acc = acc
                .checked_add(width as i32)
                .ok_or(BatchError::OffsetOverflow)?;
        }
        offsets.push(acc);
    }
    let total_bytes = acc as usize;

    // Output validity == input validity (null in <-> null out). Cloning the input `NullBuffer` is
    // an Arc bump over a Rust-owned buffer (the array was moved in over the C Data interface), so
    // it neither copies nor retains the whole input array across the boundary.
    let nulls = array.nulls().cloned();

    // The ONLY derivation-free short-circuit is "no non-null row": then the reference never calls
    // `derive()`, so seed/namespace are never validated and an empty or all-null batch must NOT
    // raise even under a wrong-length key. (A zero-`width` truncate is NOT this case: its non-null
    // rows are empty strings that still go through `derive()`, so a bad seed there must surface.)
    if non_null == 0 {
        let offsets_buf = OffsetBuffer::new(ScalarBuffer::from(offsets));
        return StringArray::try_new(offsets_buf, Buffer::from_vec(Vec::<u8>::new()), nulls)
            .map_err(assembly_error);
    }

    // Build the per-batch key ONCE, up front (Task 1.2). This fires seed-length / namespace
    // validation with the reference's own coded errors before any row is filled -- the same errors
    // the scalar path raised on its first non-null row, since the key is constant across the batch.
    let ctx = DeriveContext::new(mask_key, namespace)?;

    // One initialized values buffer, carved into disjoint per-range windows. `vec![0u8; ..]` (not
    // just capacity) so the slices have real length to split; each worker overwrites its window.
    let mut values = vec![0u8; total_bytes];
    let tasks = partition_into_tasks(array, &offsets, &mut values, threads, non_null);

    // Run on the one shared, host-sized pool (never a per-batch pool). `install` is synchronous, so
    // workers may borrow `array`/`ctx`/`values` from this stack frame; a worker panic propagates
    // out of `install` to the `catch_unwind` in `derive_batch`. `into_par_iter` moves each task
    // (its owned `&mut [u8]`) to a worker; `par_iter` would only hand out shared refs.
    let pool = shared_native_pool()?;
    let results: Vec<Result<(), (usize, BatchError)>> = pool.install(|| {
        tasks
            .into_par_iter()
            .map(|task| fill_range(&ctx, array, truncate, task))
            .collect()
    });

    // Multi-error arbitration: report the failure at the MINIMUM global row index, never
    // task-completion order, so the error matches the sequential first-error-wins path exactly.
    if let Some(err) = first_error_by_row_index(results) {
        return Err(err);
    }

    let offsets_buf = OffsetBuffer::new(ScalarBuffer::from(offsets));
    StringArray::try_new(offsets_buf, Buffer::from_vec(values), nulls).map_err(assembly_error)
}

/// Reduce per-range results to the single error at the MINIMUM global row index (the reference's
/// first-error-wins order), independent of which range or worker finished first. Extracted so the
/// arbitration is unit-testable with distinguishable synthetic errors: for this kernel the only
/// real per-row error (a timestamp tick out of range) is content-identical across rows, so the
/// integration path alone cannot observe WHICH failing row was chosen.
fn first_error_by_row_index(results: Vec<Result<(), (usize, BatchError)>>) -> Option<BatchError> {
    results
        .into_iter()
        .filter_map(Result::err)
        .min_by_key(|(row, _)| *row)
        .map(|(_, err)| err)
}

/// Map an arrow-rs `StringArray::try_new` assembly failure to a coded batch error. This should be
/// unreachable in practice -- the offsets are monotonic by construction and hex bytes are valid
/// UTF-8 -- but assembling from raw buffers must still fail closed rather than panic if arrow's
/// `force_validate` scan ever rejects the buffers.
fn assembly_error(e: arrow_schema::ArrowError) -> BatchError {
    BatchError::Canon(CanonError::unsupported(format!(
        "failed to assemble the derived StringArray from raw buffers: {e}"
    )))
}

/// Split the values buffer into `min(threads, non_null)` contiguous row ranges balanced by
/// NON-NULL count (the actual per-row work), not raw row count: nulls can cluster, and an
/// equal-row-count split would then starve some workers. Boundaries are deterministic (a forward
/// scan closing each range once it holds its target non-null quota), so the partition -- and thus
/// nothing about the output -- depends on the thread count.
fn partition_into_tasks<'a>(
    array: &dyn Array,
    offsets: &[i32],
    values: &'a mut [u8],
    threads: usize,
    non_null: usize,
) -> Vec<RangeTask<'a>> {
    let len = array.len();
    let nranges = threads.clamp(1, non_null);
    // Ceil-divide the non-null quota so earlier ranges take the remainder; the last range absorbs
    // whatever is left (including any trailing null rows).
    let per_range = non_null.div_ceil(nranges);

    let mut tasks: Vec<RangeTask<'a>> = Vec::with_capacity(nranges);
    let mut remaining: &'a mut [u8] = values;
    let mut row_lo = 0usize;
    let mut seen_non_null = 0usize;
    let mut quota = per_range;
    for i in 0..len {
        if array.is_valid(i) {
            seen_non_null += 1;
        }
        // Close a range at row i+1 once it has met its non-null quota, unless this is the final
        // range (let the last one run to the end so no rows are dropped).
        let is_last_range = tasks.len() == nranges - 1;
        if !is_last_range && seen_non_null >= quota {
            let row_hi = i + 1;
            let byte_len = (offsets[row_hi] - offsets[row_lo]) as usize;
            let (head, tail) = remaining.split_at_mut(byte_len);
            tasks.push(RangeTask {
                row_lo,
                row_hi,
                out: head,
            });
            remaining = tail;
            row_lo = row_hi;
            quota += per_range;
        }
    }
    // The final range covers row_lo..len and the rest of the buffer.
    tasks.push(RangeTask {
        row_lo,
        row_hi: len,
        out: remaining,
    });
    tasks
}

/// Fill one range's output window: canonicalize + derive + hex-encode each non-null row straight
/// into `task.out`. On the first failing row, return its GLOBAL index with the coded error and
/// stop (the caller reduces to the minimum index across ranges). Null rows write nothing (their
/// 0-width slot is already laid out).
fn fill_range(
    ctx: &DeriveContext,
    array: &dyn Array,
    truncate: Option<isize>,
    task: RangeTask<'_>,
) -> Result<(), (usize, BatchError)> {
    // Test-only fault injection: force a panic INSIDE a Rayon worker (a non-empty range on a pool
    // thread) so the worker-panic path is exercised end to end -- the panic must propagate out of
    // `pool.install` and become the coded `internal_panic` error, with no partial array returned.
    // Never set in production; the single `var_os` is per range (a handful per batch), negligible.
    if task.row_hi > task.row_lo
        && std::env::var_os("DECOY_ENGINE_NATIVE_FORCE_PANIC_IN_WORKER").is_some()
        && rayon::current_thread_index().is_some()
    {
        // The `current_thread_index().is_some()` guard means this fires ONLY on a Rayon pool
        // worker: a test that observes the coded `internal_panic` has thereby proven the range
        // ran on the pool, not inline on the calling thread.
        panic!("test-only forced panic inside a Rayon worker");
    }

    let mut hex_buf = [0u8; HEX_LEN];
    let mut cursor = 0usize;
    for i in task.row_lo..task.row_hi {
        match canonicalize_row(array, i).map_err(|e| (i, BatchError::from(e)))? {
            None => {}
            Some(canonical) => {
                let digest = ctx
                    .derive_row(&canonical)
                    .map_err(|e| (i, BatchError::from(e)))?;
                let token = hex_token_into(&digest, truncate, &mut hex_buf);
                let bytes = token.as_bytes();
                task.out[cursor..cursor + bytes.len()].copy_from_slice(bytes);
                cursor += bytes.len();
            }
        }
    }
    Ok(())
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
            let result = derive_array(&array, Some(&key), "ns", None, 1);
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
            let err = derive_array(&array, Some(&key), "ns", None, 1)
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
        let err = derive_array(&array, Some(empty_key), "ns", None, 1).unwrap_err();
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
        let err = derive_array(&array, Some(&[0u8; 20]), "ns", None, 1).unwrap_err();
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

        let out = derive_array(&empty, Some(&wrong_len_key), "ns", None, 1).unwrap();
        assert_eq!(out.len(), 0);

        let out = derive_array(&empty, Some(&valid_key), "", None, 1).unwrap();
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

        let out = derive_array(&all_null, Some(&wrong_len_key), "ns", None, 1).unwrap();
        assert_eq!(out.to_data().null_count(), 3);

        let out = derive_array(&all_null, Some(&valid_key), "", None, 1).unwrap();
        assert_eq!(out.to_data().null_count(), 3);
    }

    /// The mirror case: as soon as ONE row is non-null, both bad-key-length and empty-namespace
    /// must fail closed, even alongside nulls in the same batch.
    #[test]
    fn one_non_null_row_among_nulls_still_fails_closed() {
        let mixed = StringArray::from(vec![None, Some("alice"), None]);
        let wrong_len_key = [0u8; 20];
        let valid_key = [0u8; 32];

        let err = derive_array(&mixed, Some(&wrong_len_key), "ns", None, 1).unwrap_err();
        assert_eq!(err.code(), "seed_wrong_length");

        let err = derive_array(&mixed, Some(&valid_key), "", None, 1).unwrap_err();
        assert_eq!(err.code(), "namespace_empty");
    }

    /// A mixed string array with nulls, long enough to split into several ranges. Content varies
    /// per row so partition boundaries could expose an off-by-one in the offset math.
    fn mixed_string_fixture(rows: usize) -> StringArray {
        let values: Vec<Option<String>> = (0..rows)
            .map(|i| match i % 5 {
                0 => None,
                1 => Some(String::new()),
                n => Some(format!("row-{i}-{}", "x".repeat(n))),
            })
            .collect();
        StringArray::from(values)
    }

    /// EXIT GATE: output must be byte-identical at every thread count. Each row's hash depends only
    /// on its own bytes and the shared key, so no row partition can change any output byte. Covers
    /// several `truncate` values, including `0` (every non-null row -> empty string, still derived)
    /// and out-of-range slices.
    #[test]
    fn thread_count_never_changes_output() {
        let array = mixed_string_fixture(97); // prime-ish, forces uneven range boundaries
        let key = [7u8; 32];
        for truncate in [
            None,
            Some(0isize),
            Some(1),
            Some(10),
            Some(63),
            Some(100),
            Some(-5),
        ] {
            let baseline = derive_array(&array, Some(&key), "ns", truncate, 1).unwrap();
            for threads in [2usize, 3, 4, 8, 16] {
                let out = derive_array(&array, Some(&key), "ns", truncate, threads).unwrap();
                assert_eq!(
                    out, baseline,
                    "output diverged at threads={threads}, truncate={truncate:?}"
                );
            }
        }
    }

    /// A tz-aware second-resolution timestamp holding `i64::MAX` is admitted by type but fails
    /// conversion per row (Codex's non-vacuous per-row error). It must surface the coded overflow
    /// error at every thread count, whichever range the bad row lands in.
    fn timestamp_with_bad_rows() -> arrow_array::TimestampSecondArray {
        let mut v: Vec<Option<i64>> = (0..80).map(|i| Some(i as i64)).collect();
        v[3] = Some(i64::MAX); // low-index failure
        v[61] = Some(i64::MIN); // high-index failure, a different range under threads>=2
        arrow_array::TimestampSecondArray::from(v).with_timezone("UTC")
    }

    #[test]
    fn multi_error_surfaces_the_overflow_code_under_parallelism() {
        let array = timestamp_with_bad_rows();
        let key = [7u8; 32];
        for threads in [1usize, 2, 4, 8] {
            let err = derive_array(&array, Some(&key), "ns", None, threads).unwrap_err();
            assert_eq!(
                err.code(),
                "mixed_object_not_native",
                "threads={threads}: a bad timestamp row must fail closed with its coded error"
            );
        }
    }

    /// The arbitration reducer picks the MINIMUM global row index regardless of collection order,
    /// which is what matches the reference's first-error-wins semantics. Tested directly with
    /// DISTINGUISHABLE errors because the kernel's only real per-row error is content-identical
    /// across rows (so the integration path cannot observe which row was chosen).
    #[test]
    fn first_error_by_row_index_selects_the_lowest_row() {
        // Row 2's error must win over row 5's, in either collection order.
        let low = || (2usize, BatchError::MaskKeyRequired);
        let high = || {
            (
                5usize,
                BatchError::Canon(CanonError::unsupported("higher row")),
            )
        };
        let forward = vec![Ok(()), Err(low()), Ok(()), Err(high())];
        let reversed = vec![Err(high()), Ok(()), Err(low()), Ok(())];
        assert_eq!(
            first_error_by_row_index(forward).unwrap().code(),
            "mask_key_required"
        );
        assert_eq!(
            first_error_by_row_index(reversed).unwrap().code(),
            "mask_key_required"
        );
        // All-Ok reduces to no error.
        assert!(first_error_by_row_index(vec![Ok(()), Ok(())]).is_none());
    }

    /// TSan target (name contains "concurrent" so the sanitizer job's filter matches it): a single
    /// multi-range `derive_array` call must be race-free and byte-identical to the 1-thread run.
    /// The disjoint `split_at_mut` windows are the thing under test -- any aliasing write would be
    /// a TSan finding here.
    #[test]
    fn parallel_multi_range_concurrent_fill_agrees_with_single_thread() {
        let array = mixed_string_fixture(50);
        let key = [9u8; 32];
        let baseline = derive_array(&array, Some(&key), "ns", None, 1).unwrap();
        let parallel = derive_array(&array, Some(&key), "ns", None, 8).unwrap();
        assert_eq!(parallel, baseline);
    }

    /// A `truncate` of 0 makes every non-null row an EMPTY string, but the reference still calls
    /// `derive()` per non-null row, so a bad seed must still fail closed -- the zero-width case must
    /// NOT take the derivation-free "no non-null row" short-circuit.
    #[test]
    fn zero_width_truncate_still_validates_the_seed() {
        let array = StringArray::from(vec![Some("a"), Some("b")]);
        let err = derive_array(&array, Some(&[0u8; 20]), "ns", Some(0), 4).unwrap_err();
        assert_eq!(err.code(), "seed_wrong_length");

        // With a valid key, zero-width yields two non-null EMPTY strings (not nulls).
        let out = derive_array(&array, Some(&[0u8; 32]), "ns", Some(0), 4).unwrap();
        assert_eq!(out.len(), 2);
        assert_eq!(out.null_count(), 0);
        assert_eq!(out.value(0), "");
        assert_eq!(out.value(1), "");
    }
}
