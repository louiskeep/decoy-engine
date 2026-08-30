Cross-model PLAN gate for the Decoy engine-efficiency Part 1 Phase 2 implementation plan, BEFORE
it is handed to a builder. HARD CONSTRAINTS: do NOT use qmd, graphify, web search, or dispatch ANY
subagent. Read the plan file below and, only if needed, the existing plan and the Phase 0 crypto
contract it references. Return a terse GO or NO-GO in under 350 words. No code.

Plan under review: `docs/plans/2026-08-28-part1-phase2-detail-DRAFT.md` (cwd = the engine
native-phase1 worktree). Reference context in the same folder:
`2026-08-26-engine-efficiency-plan.md` (Part A roadmap, Decisions block, Part B Phase 0 tasks
0.3/0.4/0.6, Part C Phase 1 as the format template).

Binding scope (Cam Decision 4): Part 1 Phase 2 = native `passthrough`, `redact`, `truncate`, and
keyed `hash`, PLUS the narrow Rust `KeyedDerivationKernel` (HKDF-SHA256 over the Arrow C Data
Interface). Native FPE and every other crypto/provider are DEFERRED to Part 2 and OUT OF SCOPE.
Single-org, ~100M-row product; do not overbuild.

Judge the plan on whether a builder could execute it correctly and safely. Report findings
most-severe-first if any fail:
1. **Scope discipline:** the plan stays within the four strategies + the one Rust kernel; it does
   not smuggle in FPE, other providers, generation, FK/shuffle, or Part 2 work.
2. **Determinism:** keyed `hash` reproduces the EXACT shipped draw sequence and stays byte-parity
   with the pinned pandas oracle via the Phase 0 Task 0.6 harness; the `KeyedDerivationKernel`
   matches the Phase 0 Task 0.4 pure-Python reference + KATs exactly. No task bypasses the frozen
   determinism protocol.
3. **The frozen performance/correctness gate** is defined BEFORE implementation, is measurable, and
   the intended-route proof actually fails the gate on oracle-completion / reject / reroute /
   reference-kernel-in-path.
4. **Task sequencing + acceptance tests:** tasks are dependency-ordered (kernel contract/build
   first, then native masking, then route integration + gate); each task defines its own
   acceptance tests and failure modes up front; nothing is left to a later contributor to weaken.
5. **Rust build + fail-closed:** the maturin/PyO3 recommendation and the wheel/CI/runtime-gate story
   are sound; the fail-closed-when-extension-absent behavior (reroute to the pandas oracle at
   preflight, reference kernel stays test-only) is safe and consistent with "no mid-stream
   fallback".
6. **The 4 flagged judgment calls** (unmeasured proposed perf numbers; fail-closed = reroute;
   hatchling->maturin build-backend change touching OSS distribution; bounded native-hash input
   types) are each acceptable to proceed on, or name which needs a decision before build.

Return GO (plan is buildable as-is, note any non-blocking nits) or NO-GO (per-finding, root-cause,
what to fix before a builder starts). Terse. No tooling. No subagents.

---

## GATE RESULT — 2026-08-28 (Codex gpt-5, codex-cli 0.149.1, high effort)

**Verdict: NO-GO.** Phase 2 build must not start until these are addressed (plan-gate rule).

1. **BLOCKER — OSS packaging unresolved.** Plan alternates between replacing hatchling with maturin and a companion package; those differ materially in wheel/sdist/optional-extension/source-install behavior. "No-Rust source install still works" is incompatible with an unconditional maturin backend. **Cam decision: select the distribution model before Task 2.1**, then freeze module name + CI/platform matrix.
2. **HIGH — Perf gate not frozen before implementation.** Oracle baseline + Cam confirmation sit in Task 2.7, after 2.1-2.6 build the native path (risk of re-freezing targets to fit results). Move baseline capture + approval before Task 2.1; freeze the speed target, Phase 1 RSS ceiling, spill budget, results-log location.
3. **HIGH — Rust release gate incomplete.** Missing explicit ownership/threading tests, panic/FFI boundary checks, fuzzing/sanitizer evidence, and an independent crypto-aware review bound to the exact artifact. Add as blocking acceptance criteria (keyed-derivation portions of the crypto reference only).
4. **HIGH — Determinism/vector handoff underspecified.** Pure Rust cannot "load shared HASH_KAT" without a named language-neutral fixture. Define the committed vector file/schema; require every admitted type incl. bool + tz-aware timestamps; require Task 0.3 goldens + the Task 0.6 public parity harness explicitly (not a new similar one).
5. **MEDIUM — Task 2.1's "failing test" can already pass** ("valid object OR raises CryptoExtensionUnavailableError" accepts current absent-extension behavior). Split into distinct compiled-wheel and pure-install tests, one that initially fails.

**Judgment calls:** preflight oracle reroute — OK; bounded hash types — OK with coded preflight rerouting. **Need Cam decision before build:** the proposed performance numbers, and the build-backend change. Scope discipline and intended-route failure semantics otherwise sound.
