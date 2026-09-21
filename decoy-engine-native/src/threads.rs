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

/// Default native-mask thread knee: the measured throughput sweet spot on the 8-core class.
///
/// The 2026-09-21 thread sweep found t4 beats t8 on the native mask path (the keyed-hash kernel
/// measured t4=116.9s against t8=126s at 100M rows on an n2-standard-8), so a request above this
/// knee spends more threads for less throughput. `resolve` therefore clamps every per-call budget
/// to it. This is a static conservative cap calibrated to the 8-core class, not a per-host optimum;
/// larger hosts should raise it via [`NATIVE_MASK_THREAD_KNEE_ENV`].
const DEFAULT_NATIVE_MASK_THREAD_KNEE: usize = 4;

/// Env var that overrides [`DEFAULT_NATIVE_MASK_THREAD_KNEE`] for other host classes.
const NATIVE_MASK_THREAD_KNEE_ENV: &str = "DECOY_NATIVE_MASK_THREAD_KNEE";

/// The knee resolved from the environment, parsed at most once per process.
///
/// A `OnceLock` so the process-wide cap is read exactly once and cannot drift mid-run: every
/// `resolve` after the first reuses the cached outcome. An invalid override caches (and re-returns)
/// the coded error rather than falling back silently, mirroring how [`shared_native_pool`] caches a
/// build failure.
static NATIVE_MASK_THREAD_KNEE: OnceLock<Result<usize, DeriveError>> = OnceLock::new();

/// Parse a raw `DECOY_NATIVE_MASK_THREAD_KNEE` value into an effective knee.
///
/// Pure (no env read) so the semantics are exhaustively testable without a process-global race:
/// - `None` (var absent) -> [`DEFAULT_NATIVE_MASK_THREAD_KNEE`],
/// - a decimal integer in `1..=MAX_NATIVE_THREADS` -> that value,
/// - anything else (unparseable, `< 1`, `> MAX_NATIVE_THREADS`) -> a coded
///   `native_mask_thread_knee_invalid` [`DeriveError`], never a silent fallback.
fn parse_native_mask_thread_knee(raw: Option<&str>) -> Result<usize, DeriveError> {
    match raw {
        None => Ok(DEFAULT_NATIVE_MASK_THREAD_KNEE),
        Some(text) => match text.trim().parse::<i64>() {
            Ok(v) if (1..=MAX_NATIVE_THREADS).contains(&v) => Ok(v as usize),
            _ => Err(DeriveError::new(
                "native_mask_thread_knee_invalid",
                "DECOY_NATIVE_MASK_THREAD_KNEE must be an integer in 1..=1024",
            )),
        },
    }
}

/// The effective native-mask thread knee for this process, parsed once from the environment.
///
/// Reads [`NATIVE_MASK_THREAD_KNEE_ENV`] on the first call and caches the outcome (value or coded
/// error) for the process lifetime. See [`parse_native_mask_thread_knee`] for the semantics.
fn native_mask_thread_knee() -> Result<usize, DeriveError> {
    NATIVE_MASK_THREAD_KNEE
        .get_or_init(|| {
            parse_native_mask_thread_knee(
                std::env::var(NATIVE_MASK_THREAD_KNEE_ENV).ok().as_deref(),
            )
        })
        .clone()
}

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
    /// Resolve a caller-supplied request into a validated, clamped budget.
    ///
    /// `requested` is the raw engine argument (a Python `int` or `None` at the PyO3 boundary):
    /// - `None` resolves to the deterministic default of **1 thread** (before the clamp below).
    ///   One thread is the default (rather than `host_available`) so the out-of-the-box native
    ///   path stays byte-identical to sequential execution and to the one-thread mode the parity
    ///   gate pins as ground truth; a caller opts into parallelism explicitly by passing a count.
    /// - `Some(v)` with `v >= 1` and `v <= MAX_NATIVE_THREADS` is the validated request.
    ///
    /// The validated request is then clamped down (never up) to the smaller of the real host
    /// parallelism and the measured [thread knee](DEFAULT_NATIVE_MASK_THREAD_KNEE):
    /// `effective = min(requested_or_default, host_available, knee)`. This is the ONE engine-side
    /// enforcement point so no caller can exceed the knee, and it is shared by `derive_batch`
    /// (hash) and `derive_index_batch` (faker/index) alike. Because a native masking job derives
    /// each row independently and arbitrates errors by lowest row index, thread count never
    /// affects output bytes, so this clamp is parity-neutral by construction: it changes speed,
    /// never results.
    ///
    /// Rejections carry a coded, value-free [`DeriveError`] (the redacted-error contract: the
    /// offending value is never echoed into the detail string):
    /// - `native_threads_zero` for `0`,
    /// - `native_threads_negative` for any negative value,
    /// - `native_threads_excessive` for a value above `MAX_NATIVE_THREADS`,
    /// - `native_mask_thread_knee_invalid` if the `DECOY_NATIVE_MASK_THREAD_KNEE` override is set
    ///   to an invalid value (see [`parse_native_mask_thread_knee`]).
    pub fn resolve(
        requested: Option<i64>,
        host_available: usize,
    ) -> Result<NativeThreadBudget, DeriveError> {
        Self::resolve_with_knee(requested, host_available, native_mask_thread_knee()?)
    }

    /// [`resolve`](Self::resolve) with the knee supplied explicitly.
    ///
    /// Split out so the clamp arithmetic is unit-testable against a chosen knee without the
    /// process-global `OnceLock`: `resolve` is just this called with the env-resolved knee.
    fn resolve_with_knee(
        requested: Option<i64>,
        host_available: usize,
        knee: usize,
    ) -> Result<NativeThreadBudget, DeriveError> {
        let requested_or_default: usize = match requested {
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
        // Clamp to the real host and the knee. `host_available.max(1)` keeps the `threads >= 1`
        // invariant even if a caller passes 0; `requested_or_default` and `knee` are already
        // `>= 1`, so the min can never drop below 1.
        let threads = requested_or_default.min(host_available.max(1)).min(knee);
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
    fn valid_values_below_the_clamps_resolve_unchanged() {
        // With the default knee (4) and a roomy host, a request at or below the knee is granted
        // as-is; only requests above a clamp get lowered (covered by the clamp tests below).
        for v in [1_i64, 2, 4] {
            let budget = NativeThreadBudget::resolve(Some(v), 8)
                .unwrap_or_else(|_| panic!("{v} should be valid"));
            assert_eq!(budget.threads(), v as usize);
        }
    }

    #[test]
    fn resolve_clamps_a_request_above_the_knee_down_to_the_knee() {
        // The measured knee is the default 4; a request of 8 on an 8-core host is granted 4.
        let budget = NativeThreadBudget::resolve(Some(8), 8).expect("8 is a valid request");
        assert_eq!(budget.threads(), DEFAULT_NATIVE_MASK_THREAD_KNEE);
        // The absolute-max request is likewise clamped to the knee, not granted verbatim.
        let budget = NativeThreadBudget::resolve(Some(MAX_NATIVE_THREADS), 64)
            .expect("max is a valid request");
        assert_eq!(budget.threads(), DEFAULT_NATIVE_MASK_THREAD_KNEE);
    }

    #[test]
    fn resolve_grants_a_request_below_both_clamps_unchanged() {
        // requested 2 <= host 8 and <= knee 4 -> 2.
        let budget = NativeThreadBudget::resolve(Some(2), 8).expect("2 is valid");
        assert_eq!(budget.threads(), 2);
    }

    #[test]
    fn resolve_clamps_to_host_available_when_it_is_the_lowest() {
        // host_available 3 is below both the request (8) and the knee (4), so it wins.
        let budget = NativeThreadBudget::resolve(Some(8), 3).expect("8 is valid");
        assert_eq!(budget.threads(), 3);
    }

    #[test]
    fn resolve_with_knee_lets_an_override_move_the_cap() {
        // A raised knee (8) grants a request of 8 that the default knee (4) would clamp to 4,
        // proving the env override changes the effective cap. Host must be >= 8 to see it.
        let raised = NativeThreadBudget::resolve_with_knee(Some(8), 8, 8).expect("valid");
        assert_eq!(raised.threads(), 8);
        let defaulted =
            NativeThreadBudget::resolve_with_knee(Some(8), 8, DEFAULT_NATIVE_MASK_THREAD_KNEE)
                .expect("valid");
        assert_eq!(defaulted.threads(), DEFAULT_NATIVE_MASK_THREAD_KNEE);
        // The knee only lowers: a raised knee never grants above the request or the host.
        assert_eq!(
            NativeThreadBudget::resolve_with_knee(Some(2), 8, 8)
                .unwrap()
                .threads(),
            2
        );
        assert_eq!(
            NativeThreadBudget::resolve_with_knee(Some(16), 6, 8)
                .unwrap()
                .threads(),
            6
        );
    }

    #[test]
    fn resolve_with_knee_still_validates_the_request_before_clamping() {
        // The request-validation errors are unchanged and fire regardless of the knee.
        assert_eq!(
            NativeThreadBudget::resolve_with_knee(Some(0), 8, 8)
                .unwrap_err()
                .code,
            "native_threads_zero"
        );
        assert_eq!(
            NativeThreadBudget::resolve_with_knee(Some(-1), 8, 8)
                .unwrap_err()
                .code,
            "native_threads_negative"
        );
        assert_eq!(
            NativeThreadBudget::resolve_with_knee(Some(MAX_NATIVE_THREADS + 1), 8, 8)
                .unwrap_err()
                .code,
            "native_threads_excessive"
        );
    }

    #[test]
    fn resolve_with_knee_holds_the_at_least_one_invariant() {
        // Even a degenerate host_available of 0 cannot drop the budget below 1.
        assert_eq!(
            NativeThreadBudget::resolve_with_knee(None, 0, 4)
                .unwrap()
                .threads(),
            1
        );
        assert_eq!(
            NativeThreadBudget::resolve_with_knee(Some(8), 0, 4)
                .unwrap()
                .threads(),
            1
        );
    }

    #[test]
    fn knee_env_parsing_has_defined_semantics() {
        // Absent -> the calibrated default.
        assert_eq!(
            parse_native_mask_thread_knee(None).unwrap(),
            DEFAULT_NATIVE_MASK_THREAD_KNEE
        );
        // A valid override in range -> that value (surrounding whitespace tolerated).
        assert_eq!(parse_native_mask_thread_knee(Some("8")).unwrap(), 8);
        assert_eq!(parse_native_mask_thread_knee(Some(" 2 ")).unwrap(), 2);
        assert_eq!(
            parse_native_mask_thread_knee(Some(&MAX_NATIVE_THREADS.to_string())).unwrap(),
            MAX_NATIVE_THREADS as usize
        );
        // Invalid / out-of-range -> a coded error, never a silent fallback to the default.
        for raw in ["0", "-1", "1025", "abc", "", "4.5", "0x4"] {
            let err = parse_native_mask_thread_knee(Some(raw))
                .expect_err("invalid override must be a coded error");
            assert_eq!(
                err.code, "native_mask_thread_knee_invalid",
                "override {raw:?} should be rejected"
            );
        }
    }

    #[test]
    fn knee_env_is_read_once_and_matches_the_default_when_unset() {
        // No test sets the real env var, so the cached process-wide knee is the default. This
        // exercises the `OnceLock` path itself (the clamp tests use `resolve_with_knee` to stay
        // race-free); the exhaustive env semantics are covered by the pure parser test above.
        assert_eq!(
            native_mask_thread_knee().expect("knee resolves"),
            DEFAULT_NATIVE_MASK_THREAD_KNEE
        );
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
        // Use a knee/host high enough that the clamp does not lower the request: this asserts the
        // pool honors the budget it is handed, independent of the resolve-time clamp.
        for v in [1_usize, 2, 4, 8] {
            let budget = NativeThreadBudget::resolve_with_knee(Some(v as i64), 16, 16).unwrap();
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
