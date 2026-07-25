# Test-Quality Program (TQ-0 .. TQ-3)

**Written:** 2026-07-21. **Purpose:** move test quality from reflective (audit
the existing 368-file suite after the fact) to proactive (make weakly-tested
code unable to enter, and let the audit happen incrementally at the point of
change). Two goals, in Cam's words: (1) build **correct** tests for each module
so we find errors more reliably, and (2) effective golden suites/runs and
effective CI.

This is a planning/tracking doc. TQ-0 gets its own results block here as it
lands; the fan-out sprints get implementation detail once TQ-0 validates the
playbook. Related: `docs/plans/2026-07-10-testflight-golden-gate-hardening.md`
(the golden acceptance gate this program layers on), `CLAUDE.md`
("snapshot before extraction", "land the assertion test first").

---

## The core problem this program solves

The naive version of "have agents write tests for each module" produces
**characterization tests**: the agent runs the code, observes the current
output, and asserts on it. Those pin current behavior. They are the right net
for regression (and are exactly what the V2.0-A snapshot-before-extraction rule
already mandates), but they **codify existing bugs** instead of finding them.
A suite that is green because the code does what it does is not evidence the
code does what it should.

To find errors, a test needs an **oracle independent of the implementation**.
Three that fit engine, in order of bug-finding power:

- **Property / invariant tests** (Hypothesis, already a dev dependency; already
  a `tests/property/` dir). Encode what must hold regardless of implementation,
  then let Hypothesis search for a counterexample. Engine's crown-jewel
  invariants are already articulable: masking is deterministic given a seed;
  referential integrity holds across FK chains; row counts are preserved;
  format/type is preserved; no source PII survives in output; idempotence where
  claimed. Hypothesis hunts the input that breaks these.
- **Metamorphic tests** for outputs with no ground-truth oracle (there is no
  single "correct" masked value). Assert relations instead: mask-twice-same-seed
  ⇒ identical; same entity ⇒ same surrogate; row-shuffle does not change the
  per-row mapping.
- **Differential / oracle tests** against a slow-but-obviously-correct reference
  or a frozen corpus. Engine already does this (the 346k-row byte-identical
  oracle behind `code_set` selection); the pattern generalizes.

The bar that makes "adequate" objective rather than a vibe is **mutation
testing** (`mutmut`): inject deliberate bugs into a module, check whether the
tests catch them. A module is adequately tested when its mutation score clears a
bar, not when line coverage hits a number. That is the definition of "find
errors more reliably", operationalized.

## What already exists (do not rebuild)

- **Hypothesis** is a `[dev]` dependency (`>=6.0`); `tests/property/` exists.
- **Golden / acceptance harness**: `testflight/` suite + `scripts/test_flight.py`
  + `scripts/build_golden_fixtures.py`, CI-gated by `testflight.yml`
  (matrix py3.10/3.11). This is goal (2)'s golden run, already wired. Program
  treats it as extend-and-layer, per the golden-gate hardening plan.
- **Snapshot-before-extraction** (`CLAUDE.md`) already gives the
  characterization/regression net for the V2 extraction work.
- **Coverage config** exists and is waiting: `[tool.coverage.run]`
  `source=["src/decoy_engine"]`, `branch=true`, with a comment stating it was
  put there "so `pytest --cov` produces useful output for V2.0-A's per-module
  unit-test work" and that a floor "lands once V2.0-C closes and the test
  surface stabilizes".

## The gaps (what this program adds)

- **Coverage is never measured.** `pytest-cov`/`coverage` are not installed and
  no workflow passes `--cov`; `fail_under=0`. We are blind on where tests are
  thin.
- **No mutation grading.** Nothing distinguishes a test that executes a line
  from a test that would catch a bug on that line.
- **Thin oracle layer.** The snapshot rule guards regressions we introduce;
  there is no systematic property/metamorphic layer hunting bugs already in the
  code. It slots into the existing `tests/property/` dir.
- **Diff-coverage not wireable yet.** `ci.yml`'s `regression-gate` runs the full
  suite on every PR but checks out shallow (`fetch-depth: 1`), so no merge-base
  is available for a diff computation. A ratchet needs `fetch-depth: 0`.

## Established tools only (per the engine core rule)

Hypothesis (property search), `mutmut` (mutation grading), `pytest-cov`/
`coverage.py` (measurement, config already present), `diff-cover` (diff-scoped
coverage in CI). No hand-rolled test framework. Each new property/metamorphic
suite cites the invariant's source (spec section or established-tool pattern) in
the test module docstring, matching the "cite the source pattern" rule.

---

## Sprints

| Sprint | What | Status |
|--------|------|--------|
| **TQ-0** | **Pilot on one crown-jewel module.** Install `pytest-cov`; measure that module's current coverage and baseline its `mutmut` score. Write property + metamorphic tests until the mutation score clears the agreed bar. The module + its test files become the **template + playbook** every later agent copies. Report the baseline, the lift, and any real bug surfaced. | **NOT STARTED** - awaits GATE-TQ0 (pilot module + mutation-bar policy). |
| **TQ-1** | **Fan out across the crown jewels** (RI/FK, masking-correctness, crypto, DP paths) using the TQ-0 template: one agent per module, each following the fixed playbook, mutation-graded independently. Runnable as a Workflow (one agent per module). | BLOCKED on TQ-0. |
| **TQ-2** | **Mutation-graded sweep of the remaining surface**, module by module, prioritized by (blast radius x churn). Everything not swept is covered incrementally by the TQ-3 ratchet as it is touched. Log what is left unswept (no silent "covered everything"). | BLOCKED on TQ-1. |
| **TQ-3** | **CI tiering + diff-coverage ratchet.** Set `fetch-depth: 0` on the coverage job; add `pytest-cov` + `diff-cover` so changed lines must clear a patch threshold and total coverage may not decrease. Tier by speed: fast unit+property on every push; full testflight + deeper Hypothesis budget + diff-scoped `mutmut` on merge/nightly. | BLOCKED on TQ-0 (needs the measured baseline). |

**The proactive backbone is TQ-3** (ratchet + diff-scoped review): once wired,
weakly-tested new code cannot enter, and TQ-2 becomes a finite catch-up rather
than an open-ended audit. TQ-0/1/2 build the oracle layer that makes the ratchet
mean something (a ratchet on characterization-only tests just locks in bugs
faster).

**Pilot-first, not fan-out-first, on purpose.** Prove the loop on one module
(cheap, ~1 hour of agent time) and see a real mutation-score lift before
spending a large token run on a playbook that is not yet validated.

## Definition of done (per module)

Mutation score clears its bar with the property/metamorphic layer present, not
just coverage. **Policy is measure-first**: baseline every module, set bars from
where they actually land, with **100% mandatory on crypto and RI/FK** regardless
of baseline. An abstract "80% everywhere" set before measurement is a guess.

## Relationship to the V2 extraction program

- The hard **coverage floor** (`fail_under`) stays deferred until **V2.0-C**
  closes and the surface stabilizes, per the existing pyproject note. TQ-3's
  ratchet is a **diff-scoped** gate (changed lines only), which is compatible
  with a moving surface and does not need a global floor to be useful now.
- TQ tests are the "assertion test first" and "snapshot before extraction"
  rules made systematic and graded, not a competing scheme.

## Gates and open decisions

- **GATE-TQ0 (Cam, blocks TQ-0):**
  1. **Pilot module** - recommend **RI/FK preservation** (worst blast radius,
     crisp invariants). Alternatives: masking-correctness, crypto.
  2. **Mutation-bar policy** - recommend **measure-first** (baseline, then set
     bars), 100% on crypto/RI.
- **GATE-TQ1 (Cam, blocks fan-out):** review TQ-0's baseline + template + any
  bug found before authorizing the multi-agent Workflow (token spend).
- **GATE-TQ3:** soft ratchet (warn, do not block) vs hard ratchet (block merge
  under threshold). Recommend **soft-with-visibility on engine first**, harden
  once the baseline stabilizes, given the automated sprint cadence.
- Per-branch review is the standard **dennis -> Codex** gate; resolve every
  BLOCKER/HIGH before merge.

## Resume pointer

Start at GATE-TQ0: pick the pilot module and the mutation-bar policy, then run
TQ-0 (install `pytest-cov`, baseline coverage + `mutmut`, write property/
metamorphic tests to the bar, ship the template).
