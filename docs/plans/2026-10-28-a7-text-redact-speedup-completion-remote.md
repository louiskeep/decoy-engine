# A-7 text-redact detection speedup: completion plan for a remote worker

Status: plan
Feature: F08 (free-text masking) + F34 (residual/canary detection), perf axis only.
Tier: R2 (detection-correctness-sensitive perf refactor). Plan gate chain: Dennis -> Codex.
Branch to continue: `claude/text-redact-detection-speedup` (engine, tip `0f14a21a`).
Author: orchestrator (local). Executor: remote claude.ai/code worker.
Created: 2026-10-28.

Rules consulted: `~/dev-rules/00-universal.md`, `~/dev-rules/development-loop.md`,
`~/dev-rules/risk-and-exceptions.md`, `~/dev-rules/performance.md`,
`~/dev-rules/testing.md`, `~/dev-rules/refactoring.md`,
`~/dev-rules/api-and-compatibility.md`,
`~/dev-rules/observability-and-resilience.md`; engine
`CLAUDE.md` "use established methodology".

---

## 1. Problem and definition of done

The text-redact span kernel scanned 8 lookaround-free detectors in Rust but fell
back to a per-cell Python `re.finditer` oracle for the 3 lookaround detectors
(`ssn`, `us_zip`, `street_address`), and that per-cell Python scan dominated cost.
The branch ports those 3 into the Rust kernel as hand-written ASCII byte scanners
and records 1M-row acceptance. The work is built and self-validated but has **not
had a cross-model review**, is **89 commits behind main**, and its perf evidence is
**medians only** (no tail/variance). This plan scopes the remaining work to
merge-ready.

**Definition of done.**
1. Detection results are byte-identical before vs after (the hard gate), proven by
   the existing oracle differential + golden tests after rebase.
2. The branch is rebased onto current `main` with the one expected conflict
   resolved and the physical-seam sentry allowlist preserved (§5).
3. Perf evidence meets `performance.md`: workload, baseline, target, correctness
   guard, resource budget, AND tail/variance (the current gap, §4).
4. The Rust companion rebuilds and the full native/parity/sentry suites pass on a
   rebased tree.

## 2. Current state (built on the branch, do NOT rebuild)

- `decoy-engine-native/src/text_redact.rs`: `scan_ssn`/`scan_us_zip`/`scan_street`
  + helpers, hand-written ASCII byte scanners reproducing Python `re.finditer`
  backtracking semantics. `SUPPORTED_IDS` 8->11 (appended, indices stable),
  `CATALOG_VERSION` 1->2, `CATALOG_LEN`/`LOOKAROUND_FREE_COUNT`. `fancy-regex` was
  tried and rejected on measured perf (1.6-2.6x slower than Python's C `re`); the
  shipped path adds no Cargo dependency.
- `decoy-engine-native/src/text_mask.rs`: per-detector `verbatim` array widened to
  `CATALOG_LEN` (shared kernel; keyed text_mask benefits too).
- `src/decoy_engine/execution/native/_text_redact_kernel.py`: `SUPPORTED_IDS`=11,
  `CATALOG_VERSION`=2; merge/loader unchanged.
- Build record `docs/records/2026-10-22-c6c-ii-text-redact-lookaround-port-build.md`
  with the perf matrix.
- Tests: `tests/native/test_c6c_ii_text_redact_rust.py` (oracle differential on
  4,000 + 3,000 adversarial cells + golden clinical notes), updated
  `tests/parity/test_ooc_text_redact_native_real.py`,
  `tests/parity/test_phase_d_text_redact_seq_real.py`. Rust: 109 lib tests; Python
  differential 181 tests.

## 3. Established solutions (surveyed; design follows them)

Per engine `CLAUDE.md` "use established methodology": no new crypto, no new
primitive. The port reuses the detection semantics of Python's C `re` (the oracle),
reproducing its backtracking by hand in Rust. The named refactoring step (Fowler)
is **Substitute Algorithm**: the hand-written byte scanner replaces the Python
`finditer` call on the native path while preserving the detected spans exactly.
fancy-regex (a lookaround regex engine) was the obvious library choice and was
rejected on a measured latency floor, recorded in the build record. That is the
right order: survey, measure, justify the custom code.

## 4. Remaining work to merge-ready

1. **Add tail/variance perf evidence (the gap).** `performance.md` requires
   variance and tail, not mean/median alone. The recorded matrix has medians over
   3 reps at 1M (5 at 200k) with a RSS budget (<=1.20x baseline) and a latency
   floor (<=1.05x baseline) enforced per row, but no p95/p99 or stdev. Re-run
   `scripts/bench_text_redact.py` and report, per row, RUN-TO-RUN variance across
   reps (stdev) and a tail figure (p95, or max/median). A stdev or p99 over the
   current 3 reps is statistically meaningless, so pin the rep count: either
   >=10 reps at 1M rows, OR drop to 200k rows where 5+ reps already exist and
   extrapolate per the pandas-path-minimal convention. State explicitly that the
   figure is run-to-run (not per-cell) variance, and prefer an observed max/median
   over calling a p95 from a handful of reps reliable.
   **Fix the baseline (important):** `scripts/bench_text_redact.py` currently forces
   `load_text_redact_kernel = lambda: None`, so its baseline is FULL PYTHON, not the
   shipped 8-detector kernel, and the recorded 3.69x is a full-Python comparison. A
   repeat can look green while REGRESSING against what actually ships. Change the
   harness to measure the shipped 8-detector kernel vs the new 11-detector kernel on
   identical fixtures, host, and build, and apply the latency floor + RSS budget to
   THAT paired comparison. Report the full-Python speedup separately, not as the
   acceptance guard. The speedup is hit-rate-dependent (scales with ASCII-cell
   fraction; the 95%-ASCII representative fixture is the headline case).
2. **Rebase onto current `main` (89 commits).** Low risk: 13 of 14 changed files
   have zero churn on main since the merge-base. The one file with churn is
   `tests/sentry/test_physical_seam_disconnection.py` (14 commits on main). Its hunks
   do not overlap the branch's 2-line comment edit, so it likely 3-way merges with
   NO conflict: a clean rebase of that file is the expected good outcome, not a sign
   something was missed. The detection oracle (`storm/detectors.py`, `storm/_patterns.py`)
   has zero churn on main, so there is no catalog drift to reconcile.
3. **Rebuild the Rust companion** (`maturin develop --release`, Rust 1.98.0 per
   `rust-toolchain.toml`) and re-run `cargo test --no-default-features` + `cargo fmt`
   + `cargo clippy`, then the Python native/parity/sentry suites + `ruff` + `mypy`.
4. **Re-run the byte-parity oracle differential + golden tests** on the rebased
   tree and confirm green (the hard gate).

Deferred / out of scope (not blockers): the STRETCH to widen the Rust path beyond
ASCII-safe cells to full Unicode (cannot be proven byte-identical cheaply); the
cross-repo decoy-platform ROADMAP row; the STORM->detection/profiling/verification
rename reconciliation (not a conflict on today's main; whichever branch merges
second eats it).

## 5. Physical-seam sentry (do not break)

`tests/sentry/test_physical_seam_disconnection.py` asserts production execution
modules are byte-identical to `origin/main` against an allowlist. The branch edits
its allowlist comments for `_kernels_scalar.py` / `_text_redact_kernel.py`. After
the rebase, the allowlist entries for the changed native modules MUST survive the
reconciliation so the gate stays green. A changed engine `execution/` module that
is not allowlisted fails CI by design.

## 6. Observable behavior, failure modes, acceptance tests

Per `testing.md`: use the existing oracle differential as the behavior proof;
measure test strength on the changed Rust/Python units; name honestly what each
test covers.

**Correctness guard (the pinned hard gate).** For every eligible cell, the Rust
kernel's detected spans equal the Python `iter_spans` + `_splice` oracle,
span-for-span, before and after merge, labeled and unlabeled. Proven by the 4,000 +
3,000 adversarial-cell differentials and the golden clinical-notes corpus. No
config, routing, admission, evidence, or golden-snapshot change.

**Perf acceptance (`performance.md`), on the rebased tree.** Representative workload
= clinical-notes free text ~5% non-ASCII (95% ASCII-cell hit rate); baseline = the
shipped 8-detector kernel; 1M rows. Report median AND stdev AND a tail figure per
row. Guards enforced per row: latency floor `median_new <= 1.05 * median_baseline`
(no regression even at 0% hit rate), RSS budget `peak_rss_new <= 1.20 *
peak_rss_baseline`. Recorded headline (to reproduce): R-representative ~3.69x,
R-python-only ~2.97x.

**Failure modes.** If any divergence from the oracle is found in review or testing,
the immediate fail-closed recovery is to **revert the port** (or force the full
Python path for all three ported detectors), not a surgical single-detector drop.
`scan_lookaround` dispatches FIXED indices (8 -> ssn, 9 -> us_zip, 10 -> street), so
dropping `ssn` or `us_zip` from `SUPPORTED_IDS` SHIFTS the later indices without
changing that dispatch, and a remaining detector then invokes the wrong scanner while
the catalog-agreement test still passes. If a selective fallback is nonetheless kept,
it must in one change: update the index dispatch, update BOTH `SUPPORTED_IDS` arrays
(Rust + Python), bump `CATALOG_VERSION`, and re-verify the remaining detectors AND
`text_mask` against their oracles. No silent divergence.

## 7. Pinned guarantee and non-goals

**Guarantee.** An internal throughput change only: the same spans are detected and
spliced as the Python oracle, byte-identical, with no regression (latency floor) and
bounded memory (RSS budget). FPE output is unaffected (keys off `_FPE_CONFIG`, not
the detection source).

**In scope: a vocabulary agreement test (prevents silent future drift).** The Rust
detection vocabularies (the 47-entry `STREET_SUFFIXES`, `STREET_UNITS`, and the
SSN/ZIP/street regex bodies) are hand-copied from the Python oracle. They agree
today, but no existing test fails when they diverge: the catalog-version/
supported-set checks and the ASCII-class test do not cover the vocabularies, so a
future one-sided edit to `_USPS_STREET_SUFFIXES` (add "pike"/"run") would silently
diverge on the native path. Expose the Rust suffix + unit vocabularies from the
companion and assert ordered list-equality to the Python oracle. But list-equality
alone is NOT enough: it does not pin the SSN/ZIP/street regex BODIES or FLAGS (a
boundary, quantifier, or lookahead change in the Python pattern leaves the lists and
`CATALOG_VERSION` unchanged), and the unit vocabulary is embedded directly in
`_STREET_ADDRESS_RE`, so comparing against a separately copied Python list is also
insufficient. So also: pin the actual oracle patterns + flags into the scanner
contract (a snapshot the test compares against), require an explicit scanner review +
differential update + coordinated `CATALOG_VERSION` bump whenever that contract
changes, and add targeted differential cases for suffix/unit alternatives, the
optional-dot fallback, and street-name repetition backtracking (the current random
corpus draws mostly from fixed examples). Required acceptance items, not follow-ups.

**Non-goals.** No detection-behavior or semantics change; no new detectors (the
catalog supported-set grew but detected spans are identical); no Unicode/non-ASCII
widening; no FPE change; no cross-repo roadmap edit. A review finding outside this
guarantee is a recorded follow-up, not a blocker.

## 8. Design notes

No new structure or abstraction: a Substitute Algorithm on one kernel path plus a
catalog-version bump. `CATALOG_VERSION` 1->2 and `SUPPORTED_IDS` 8->11 are an
internal versioned surface (`api-and-compatibility.md`): indices are appended and
stable, the merge/loader handles a supported-set change, and no external consumer
contract changes. House style applies to touched code (comments explain why, single
purpose). The highest-risk unit for the reviewer is `scan_street`
(`scan_street`/`street_rest`/`street_unit`), the only ported detector with genuine
backtracking (greedy street-name repetition x 47-entry USPS suffix alternation x
optional dot/unit); the empirical gate is the adversarial differentials + golden
notes, and the plan asks for a cold read against `_STREET_ADDRESS_RE`.

## 9. Remote-worker scope (phase 0) and hand-back

Phase 0 = items §4.1-§4.4 (tail/variance evidence, rebase + conflict, rebuild
companion, re-run the byte-parity gate) plus the §7 vocabulary agreement test, on
`claude/text-redact-detection-speedup`. Engine-only, no cross-repo coupling. **The
worker's image MUST have a Rust toolchain (Rust 1.98.0, maturin)**; this is not a
pure-Python slice.

**Fetch `origin/main` first (do not skip).** The rebase needs it, and so does the
physical-seam sentry: `test_physical_seam_disconnection.py` is
`skipif(not origin/main reachable)`, so on a checkout without that ref it SILENTLY
SKIPS rather than fails, which is false confidence on the single most important
governance gate. The hand-back MUST confirm that
`test_production_execution_modules_are_byte_identical_to_origin_main` actually RAN
and PASSED (quote the pytest line showing it passed, not skipped).

**Prove the correctness gate RAN, not skipped (same trap, different place).** The
differential/golden tests guard on `NEEDS_TR_KERNEL` in
`tests/native/_c6c_ii_support.py`, which SKIPS whenever `load_text_redact_kernel()`
returns `None` (including on catalog skew). So the raw differential, merged
differential, golden, routing, and vocabulary-agreement tests can all silently SKIP
while ordinary fallback tests pass. Add a companion-present preflight that asserts the
kernel loads with the expected `CATALOG_VERSION` + ordered `SUPPORTED_IDS`, and the
hand-back MUST quote explicit PASSED (not skipped) lines for: raw differential, merged
differential, golden clinical-notes, routing, and vocabulary agreement. An unexpected
skip FAILS this acceptance run (keep the companion-absent fallback validation as a
separate run).

Hand back as a pushed branch + PR against engine `main`; do not merge. The
orchestrator then runs the CODE-level Dennis -> Codex gate on the pushed branch
(Dennis's primary target: the `scan_street` backtracking vs `_STREET_ADDRESS_RE`;
this plan gate did not clear the code) and merges on green.
