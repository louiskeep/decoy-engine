//! Transient-scratch allocation bound (engine-efficiency-plan Part 1 Phase 2, target 5(c)).
//!
//! A peak-tracking `#[global_allocator]` wraps the system allocator with an atomic
//! current/high-water-mark counter. Around one `derive_array` call over a frozen 4,096-row
//! batch for each admitted input type, this asserts:
//!
//!     (peak_outstanding_bytes - returned_output_buffer_bytes) <= 2x the input's Arrow buffer
//!     byte size
//!
//! `peak_outstanding_bytes` is measured as a DELTA: the high-water mark is reset to the
//! allocator's current live-byte level immediately before the call (so bytes already live going
//! in, such as the input array itself, do not count against the budget -- the input's own size
//! is what the "2x" bound is measured against, not something the call must additionally
//! reproduce), then read again immediately after. This isolates what the call itself allocates
//! (transient scratch plus the returned output buffer) from whatever the harness or the input
//! construction already held, which is what "excludes the required output buffer" means in
//! practice: subtracting the output's own buffer size from that delta leaves only the
//! transient-scratch cost the plan bounds. One 4,096-row-per-type fixture is fixed here (not
//! generated per run) so the byte counts are exactly reproducible between runs.

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use arrow_array::types::TimestampNanosecondType;
use arrow_array::{Array, ArrayRef, BooleanArray, Int64Array, PrimitiveArray, StringArray};

struct PeakTrackingAllocator;

static CURRENT: AtomicUsize = AtomicUsize::new(0);
static PEAK: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for PeakTrackingAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let ptr = unsafe { System.alloc(layout) };
        if !ptr.is_null() {
            record_alloc(layout.size());
        }
        ptr
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) };
        record_dealloc(layout.size());
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        let new_ptr = unsafe { System.realloc(ptr, layout, new_size) };
        if !new_ptr.is_null() {
            record_dealloc(layout.size());
            record_alloc(new_size);
        }
        new_ptr
    }
}

fn record_alloc(size: usize) {
    let now = CURRENT.fetch_add(size, Ordering::SeqCst) + size;
    // CAS loop rather than a plain store: two threads racing to raise PEAK past the current
    // value must not clobber each other's higher observation. `cargo test` runs test binaries
    // single-threaded per-process by default for this file (no threads spawned within these
    // tests), but the loop costs nothing and keeps the allocator honest if that ever changes.
    let mut observed = PEAK.load(Ordering::SeqCst);
    while now > observed {
        match PEAK.compare_exchange_weak(observed, now, Ordering::SeqCst, Ordering::SeqCst) {
            Ok(_) => break,
            Err(actual) => observed = actual,
        }
    }
}

fn record_dealloc(size: usize) {
    CURRENT.fetch_sub(size, Ordering::SeqCst);
}

#[global_allocator]
static ALLOCATOR: PeakTrackingAllocator = PeakTrackingAllocator;

/// Reset the high-water mark to the current live-byte level and return that baseline.
fn mark_baseline() -> usize {
    let baseline = CURRENT.load(Ordering::SeqCst);
    PEAK.store(baseline, Ordering::SeqCst);
    baseline
}

/// Peak bytes reached above `baseline` since `mark_baseline()` was called.
fn peak_since(baseline: usize) -> usize {
    PEAK.load(Ordering::SeqCst).saturating_sub(baseline)
}

const ROWS: usize = 4096;
const MASK_KEY: [u8; 32] = {
    let mut k = [0u8; 32];
    let mut i = 0;
    while i < 32 {
        k[i] = i as u8;
        i += 1;
    }
    k
};
const NAMESPACE: &str = "allocation_bound.fixture";

fn utf8_fixture() -> ArrayRef {
    // Fixed-width synthetic strings (not randomly generated) so the fixture's byte size, and
    // therefore this test's numbers, are exactly reproducible from run to run.
    let values: Vec<Option<String>> = (0..ROWS)
        .map(|i| Some(format!("row-{i:06}-fixed-width-value")))
        .collect();
    Arc::new(StringArray::from(values))
}

fn int64_fixture() -> ArrayRef {
    let values: Vec<Option<i64>> = (0..ROWS).map(|i| Some(i as i64)).collect();
    Arc::new(Int64Array::from(values))
}

fn bool_fixture() -> ArrayRef {
    let values: Vec<Option<bool>> = (0..ROWS).map(|i| Some(i % 2 == 0)).collect();
    Arc::new(BooleanArray::from(values))
}

fn timestamp_tz_fixture() -> ArrayRef {
    let values: Vec<Option<i64>> = (0..ROWS)
        .map(|i| Some(1_577_881_845_000_000_000 + i as i64 * 1_000_000_000))
        .collect();
    Arc::new(
        PrimitiveArray::<TimestampNanosecondType>::from(values)
            .with_timezone(Arc::<str>::from("UTC")),
    )
}

/// Run one `derive_array` call under the peak tracker and assert the transient-scratch bound.
fn assert_scratch_bound(label: &str, array: ArrayRef) {
    let input_bytes = array.get_buffer_memory_size();

    let baseline = mark_baseline();
    let output = _kernel::batch::derive_array(array.as_ref(), Some(&MASK_KEY), NAMESPACE, None)
        .unwrap_or_else(|e| panic!("{label}: derive_array failed: {}: {}", e.code(), e.detail()));
    let peak_outstanding = peak_since(baseline);

    let output_bytes = output.get_buffer_memory_size();
    let scratch = peak_outstanding.saturating_sub(output_bytes);
    let bound = 2 * input_bytes;

    assert_eq!(
        output.len(),
        ROWS,
        "{label}: output row count must match input"
    );
    assert!(
        scratch <= bound,
        "{label}: transient scratch {scratch} bytes exceeds 2x input bound {bound} bytes \
         (input={input_bytes}, peak_outstanding={peak_outstanding}, output={output_bytes})"
    );

    eprintln!(
        "{label}: input={input_bytes}B output={output_bytes}B peak_outstanding={peak_outstanding}B \
         scratch={scratch}B bound={bound}B"
    );
}

#[test]
fn scratch_allocation_stays_within_2x_input_for_every_admitted_type() {
    assert_scratch_bound("utf8", utf8_fixture());
    assert_scratch_bound("int64", int64_fixture());
    assert_scratch_bound("bool", bool_fixture());
    assert_scratch_bound("timestamp_tz", timestamp_tz_fixture());
}
