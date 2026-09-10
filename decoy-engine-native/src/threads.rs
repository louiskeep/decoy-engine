//! One native thread budget and the single long-lived Rayon pool built from it.
//!
//! This is the plumbing for Phase 1 Task 1.3 of the execution-consolidation plan
//! (`docs/plans/2026-09-09-execution-consolidation-and-native-throughput.md`). Its purpose is
//! to bound how many Rust worker threads a native masking job may spin up, so the kernel never
//! creates more threads than its approved budget and cannot contend unbounded with the rest of
//! the process (including later DuckDB work). Task 1.5 is what actually flips `derive_array`'s
//! per-row loop onto the pool built here; in Task 1.3 the budget is resolved and threaded
//! through the PyO3 boundary, and the pool is built and owned, but execution stays sequential
//! and byte-identical.
//!
//! Ownership + lifetime (Phase-0 Codex requirement): a `NativeThreadPool` owns exactly ONE
//! long-lived `rayon::ThreadPool`, constructed once from a `NativeThreadBudget` and reused for
//! the pool's whole lifetime. It is NEVER constructed per 50k batch. When several native jobs
//! run concurrently they MUST share one pool (or otherwise bound their aggregate thread count
//! against a single budget) rather than each building their own; a per-job pool would multiply
//! the approved thread count by the number of in-flight jobs and defeat the bound. This module
//! owns the type; wiring a single shared instance into the PyO3 entry point (and deciding where
//! that instance lives) is Task 1.5's job.
//!
//! Platform lane (OUT OF SCOPE here): precedence between the platform's per-worker thread
//! budget and this engine-level argument, and recording the resolved value in the job report,
//! are a separate platform-side lane. `resolve` deliberately takes only the caller's requested
//! value and the host's available parallelism; it does not read any platform budget.

use std::sync::OnceLock;

use crate::derive::DeriveError;

/// Absolute ceiling on a requested native thread count, independent of host topology.
///
/// An absolute ceiling (rather than a multiple of `host_available`) keeps the "excessive"
/// rejection deterministic across machines: a value is rejected identically whether the test
/// runs on a 4-core CI box or a 64-core host, so the coded error is reproducible. 1024 is far
/// above any single-org box's real core count (the product's own scope note caps at a
/// conservative single host), while still refusing a pathological value that would try to spawn
/// an effectively unbounded number of OS threads.
const MAX_NATIVE_THREADS: i64 = 1024;

/// A validated native thread budget: an approved worker-thread count that is always `>= 1`.
///
/// Construct with [`NativeThreadBudget::resolve`]; the invariant (`threads >= 1`, `threads <=
/// MAX_NATIVE_THREADS`) holds for every value that exists, so the one-thread deterministic mode
/// is always representable and downstream code never has to defend against zero.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NativeThreadBudget {
    threads: usize,
}

impl NativeThreadBudget {
    /// Resolve a caller-supplied request into a validated budget.
    ///
    /// `requested` is the raw engine argument (a Python `int` or `None` at the PyO3 boundary):
    /// - `None` resolves to the deterministic default of **1 thread**. One thread is chosen as
    ///   the default (rather than `host_available`) so the out-of-the-box native path stays
    ///   byte-identical to today's sequential execution and to the one-thread mode Task 1.5's
    ///   parity gate pins as ground truth; a caller opts into parallelism explicitly by passing
    ///   a count. `host_available` is accepted for future precedence/clamping use (and to let a
    ///   caller pass it straight through) but does not change the `None` default here.
    /// - `Some(v)` with `v >= 1` and `v <= MAX_NATIVE_THREADS` resolves to exactly `v` threads.
    ///
    /// Rejections carry a coded, value-free [`DeriveError`] (the redacted-error contract: the
    /// offending value is never echoed into the detail string):
    /// - `native_threads_zero` for `0`,
    /// - `native_threads_negative` for any negative value,
    /// - `native_threads_excessive` for a value above `MAX_NATIVE_THREADS`.
    pub fn resolve(
        requested: Option<i64>,
        host_available: usize,
    ) -> Result<NativeThreadBudget, DeriveError> {
        // `host_available` is intentionally not consulted for the default or the cap yet; it is
        // part of the signature so the platform-precedence lane and Task 1.5 can clamp against
        // the real host without another boundary change. Bind it to silence "unused" without
        // pretending to use it.
        let _ = host_available;
        let threads = match requested {
            None => 1,
            Some(0) => {
                return Err(DeriveError::new(
                    "native_threads_zero",
                    "native thread count must be at least 1",
                ));
            }
            Some(v) if v < 0 => {
                return Err(DeriveError::new(
                    "native_threads_negative",
                    "native thread count must be positive",
                ));
            }
            Some(v) if v > MAX_NATIVE_THREADS => {
                return Err(DeriveError::new(
                    "native_threads_excessive",
                    "native thread count exceeds the maximum allowed",
                ));
            }
            // `v` is now in `1..=MAX_NATIVE_THREADS`, which fits `usize` on every supported
            // target (usize is at least 32-bit; 1024 fits trivially).
            Some(v) => v as usize,
        };
        Ok(NativeThreadBudget { threads })
    }

    /// The process-capacity budget: `host_available` clamped to `1..=MAX_NATIVE_THREADS`.
    ///
    /// This sizes the single shared pool ([`shared_native_pool`]), and is deliberately distinct
    /// from [`resolve`](Self::resolve)'s `None` default of 1: the shared pool is the whole
    /// process's parallel capacity (so a large job can actually use the cores), while a per-job
    /// `native_threads` request stays opt-in. Lowering this to a platform-worker budget is the
    /// carried-forward platform-precedence lane.
    pub fn for_host(host_available: usize) -> NativeThreadBudget {
        let threads = host_available.clamp(1, MAX_NATIVE_THREADS as usize);
        NativeThreadBudget { threads }
    }

    /// The approved worker-thread count. Always `>= 1`.
    pub fn threads(&self) -> usize {
        self.threads
    }
}

/// Owns the single long-lived Rayon thread pool built from a [`NativeThreadBudget`].
///
/// Built ONCE (see the module doc on ownership + lifetime) and reused; never rebuilt per batch.
/// In Task 1.3 the pool is constructed and owned but not yet driving `derive_array` — Task 1.5
/// wires the row loop onto it. `install` is provided as the entry point Task 1.5 will use to
/// run a closure on the pool; it doubles as a smoke path here so the owned pool is exercised.
pub struct NativeThreadPool {
    pool: rayon::ThreadPool,
}

impl NativeThreadPool {
    /// Build the pool with exactly `budget.threads()` worker threads.
    ///
    /// `pub(crate)` on purpose: the ONLY production path to a pool is [`shared_native_pool`],
    /// which builds one shared instance. Keeping the constructor crate-internal makes it
    /// structurally impossible for a caller (or Task 1.5) to build a per-batch or per-job pool
    /// that would multiply the native-thread count across concurrent jobs.
    ///
    /// Returns a coded `native_thread_pool_build` [`DeriveError`] if Rayon cannot build the pool
    /// (e.g. the OS refuses the thread allocation), so a pool-construction failure surfaces on
    /// the same coded-error path as budget validation rather than panicking.
    pub(crate) fn new(budget: &NativeThreadBudget) -> Result<NativeThreadPool, DeriveError> {
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(budget.threads())
            .build()
            .map_err(|e| {
                DeriveError::new(
                    "native_thread_pool_build",
                    format!("failed to build the native thread pool: {e}"),
                )
            })?;
        Ok(NativeThreadPool { pool })
    }

    /// The number of worker threads the owned pool actually holds.
    pub fn current_num_threads(&self) -> usize {
        self.pool.current_num_threads()
    }

    /// Run `op` inside the owned pool. Task 1.5 uses this to drive the parallel row loop; in
    /// Task 1.3 it is the smoke path proving the owned pool executes work.
    pub fn install<Op, R>(&self, op: Op) -> R
    where
        Op: FnOnce() -> R + Send,
        R: Send,
    {
        self.pool.install(op)
    }
}

/// The one process-wide native thread pool, built once and shared by every native job.
static SHARED_POOL: OnceLock<Result<NativeThreadPool, DeriveError>> = OnceLock::new();

/// Return the single shared native thread pool, building it on first call.
///
/// This is the ONLY production path to a `NativeThreadPool` (its constructor is crate-internal),
/// so the aggregate native-thread count is bounded across concurrent jobs by construction: there
/// is exactly one pool, sized once to the host's available parallelism (the process capacity).
/// Task 1.5 drives the parallel row loop by calling [`NativeThreadPool::install`] on this
/// instance; a per-job `native_threads` budget caps the work WITHIN the shared pool, it never
/// sizes a separate pool.
///
/// Sizing note: rayon pools are fixed-size at build, so the first call fixes the size for the
/// process lifetime. Sizing to host cores here (not the first job's per-job argument) avoids a
/// small first job pinning the whole process to one thread. Lowering this to a platform-worker
/// budget is the carried-forward platform-precedence lane. A build failure is cached and
/// returned as the coded `native_thread_pool_build` error on every call (rather than retried),
/// so the failure is deterministic.
pub fn shared_native_pool() -> Result<&'static NativeThreadPool, DeriveError> {
    SHARED_POOL
        .get_or_init(|| {
            let host = std::thread::available_parallelism()
                .map(|n| n.get())
                .unwrap_or(1);
            NativeThreadPool::new(&NativeThreadBudget::for_host(host))
        })
        .as_ref()
        .map_err(|e| e.clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn none_resolves_to_deterministic_default_of_one() {
        let budget = NativeThreadBudget::resolve(None, 8).expect("None is valid");
        assert_eq!(budget.threads(), 1);
    }

    #[test]
    fn one_thread_mode_is_supported() {
        let budget = NativeThreadBudget::resolve(Some(1), 8).expect("1 is valid");
        assert_eq!(budget.threads(), 1);
    }

    #[test]
    fn valid_values_resolve_unchanged() {
        for v in [1_i64, 2, 4, 8, MAX_NATIVE_THREADS] {
            let budget = NativeThreadBudget::resolve(Some(v), 8)
                .unwrap_or_else(|_| panic!("{v} should be valid"));
            assert_eq!(budget.threads(), v as usize);
        }
    }

    #[test]
    fn threads_is_always_at_least_one() {
        assert!(NativeThreadBudget::resolve(None, 1).unwrap().threads() >= 1);
        assert!(NativeThreadBudget::resolve(Some(1), 1).unwrap().threads() >= 1);
    }

    #[test]
    fn zero_is_rejected_with_code() {
        let err = NativeThreadBudget::resolve(Some(0), 8).unwrap_err();
        assert_eq!(err.code, "native_threads_zero");
        // Redacted-error contract: the detail never echoes the offending value.
        assert!(!err.detail.contains('0'));
    }

    #[test]
    fn negative_is_rejected_with_code() {
        for v in [-1_i64, -8, i64::MIN] {
            let err = NativeThreadBudget::resolve(Some(v), 8).unwrap_err();
            assert_eq!(err.code, "native_threads_negative");
        }
    }

    #[test]
    fn excessive_is_rejected_with_code() {
        for v in [MAX_NATIVE_THREADS + 1, i64::MAX] {
            let err = NativeThreadBudget::resolve(Some(v), 8).unwrap_err();
            assert_eq!(err.code, "native_threads_excessive");
        }
    }

    #[test]
    fn pool_builds_with_the_requested_thread_count() {
        for v in [1_usize, 2, 4, 8] {
            let budget = NativeThreadBudget::resolve(Some(v as i64), 8).unwrap();
            let pool = NativeThreadPool::new(&budget).expect("pool builds");
            assert_eq!(pool.current_num_threads(), v);
        }
    }

    #[test]
    fn pool_install_runs_work_on_the_owned_pool() {
        let budget = NativeThreadBudget::resolve(Some(2), 8).unwrap();
        let pool = NativeThreadPool::new(&budget).unwrap();
        // Smoke: the owned pool executes a closure and reports its own thread count from inside.
        let inside = pool.install(rayon::current_num_threads);
        assert_eq!(inside, 2);
    }

    #[test]
    fn for_host_clamps_to_the_budget_range() {
        assert_eq!(NativeThreadBudget::for_host(0).threads(), 1);
        assert_eq!(NativeThreadBudget::for_host(4).threads(), 4);
        assert_eq!(
            NativeThreadBudget::for_host(usize::MAX).threads(),
            MAX_NATIVE_THREADS as usize
        );
    }

    #[test]
    fn shared_native_pool_is_one_instance_across_calls() {
        // The enforced single-owner: every call returns the same pool, so concurrent jobs share
        // one pool and the aggregate native-thread count is bounded by construction.
        let a = shared_native_pool().expect("shared pool builds");
        let b = shared_native_pool().expect("shared pool builds");
        assert!(
            std::ptr::eq(a, b),
            "shared_native_pool must return the same instance"
        );
        assert!(a.current_num_threads() >= 1);
    }
}
