Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, architecture, testing, verification, use-established-methodology

# Generation: anchor Faker date providers to a fixed reference (seed-stable dob)

Foundational re-baseline slice (Cam 2026-10-18: "anchor dob deterministically").
Rev-2 folds the Codex plan-gate NO-GO (3 P1 + 2 P2): the naive global clock-patch is
wrong (hits masking, races under concurrency, misses the composite path). Rev-2 uses a
CONTEXTVAR-GATED clock that generation opts into and masking never does.

## 1. Problem

`person_dob` -> Faker `date_of_birth` computes a date from an age window relative to
`datetime.now()` inside `faker.providers.date_time`. Nothing in src/ anchors it; only
the TEST `frozen_faker_clock()` pins it to ORACLE_CLOCK = 2026-10-16 12:00:00. So
production dob for the same seed+config shifts by calendar day. CRUCIAL CONSTRAINTS the
gate surfaced:
- The dob-bearing composite fixtures call `CompositePerson.generate_bundle()` DIRECTLY
  (_pools -> PoolBuilder.build -> FakerAdapter.generate_batch), NOT via generate_tables.
- That SAME `generate_bundle` is called by MASKING (execution/_strategies/_composite.py).
  So anchoring the shared composite/adapter unconditionally would change MASKING.
- `synthesize.py` promises concurrent-generation isolation (tested), so a module-global
  save/restore clock patch races across overlapping jobs.

## 2. Goal

Faker date providers are deterministic for GENERATION (pure function of the Faker seed +
a fixed anchor), concurrency-safe, with MASKING and caller-owned Faker UNCHANGED.

## 3. Design: contextvar-gated faker clock (generation opts in; masking does not)

- Patch `faker.providers.date_time.datetime` and `.dtdate` ONCE at engine import (reuse
  the proven `_SameInstances` metaclass so isinstance stays valid). The patched
  `now()/today()` return the ANCHOR **iff** a `contextvars.ContextVar` (e.g.
  `_GENERATION_FAKER_ANCHOR`) is set; otherwise they DELEGATE to the real stdlib
  now()/today(). ContextVars are per-thread/per-task, so this is concurrency-safe and
  needs no save/restore race. Masking never sets the var -> sees the real clock. (Est.
  methodology: same clock-pin technique as the F0 test harness; cite it.)
- A context manager `generation_faker_anchor()` sets/resets the ContextVar via
  set()/reset(token) (exception-safe).
- GENERATION opts in: wrap the generation paths that drive Faker dates:
  (a) `_generate_tables_from_config` (covers the per-row `_faker` loop + pooled
  `_faker_pool.build_pool_values`), and (b) the COMPOSITE GENERATION entry
  (CompositePerson generate / generate_bundle AS DRIVEN BY GENERATION, incl. the
  `_pools` dob pool) - the distinct piece that bypasses generate_tables. MASKING's
  `_strategies/_composite.py` call of generate_bundle does NOT enter the context -> its
  behavior is byte-unchanged.
  - Resolve the generation-vs-masking opt-in cleanly: the context manager is entered by
    generation callers only; masking keeps the real clock. Enumerate every generation
    caller of generate_bundle / FakerAdapter and confirm masking's is excluded (the
    build must prove no masking path sets the var).
- ANCHOR = the exact datetime `2026-10-16 12:00:00` with the SAME tz behavior as the
  F0 fixture's ORACLE_CLOCK (not just the date; midnight-vs-noon changes date_time*).
  Match the fixture exactly so dob/date digests are unchanged. Fixed constant now;
  seed-derived anchoring explicitly DEFERRED (adds unneeded semantics).

## 4. Oracle / baseline (unchanged is MANDATORY)

- The anchor equals ORACLE_CLOCK, so the F0 oracle digests MUST be UNCHANGED under
  PRODUCTION-only anchoring. Acceptance: run the F0 oracle suite with the production
  contextvar anchor active AND the test autouse `frozen_faker_clock` REMOVED/bypassed
  for those modules; digests must match byte-for-byte. A mismatch is a FINDING to
  investigate (a missed seam or wrong anchor), NOT license to re-record. Do NOT use
  `_f0_capture` to "fix" a mismatch (it runs under the test freeze, so it would
  reproduce old digests and HIDE a missing production anchor).
- Keep a PERMANENT production-only oracle check (a test that exercises the composite
  generation path through the production anchor, without the test freeze, asserting the
  oracle) so the production guarantee stays pinned.
- Test-conftest `frozen_faker_clock`: with production anchoring, it is redundant for
  generation faker-dates. KEEP it this slice (harmless belt-and-suspenders) but it is
  NOT the production evidence; the production-only check above is. (Correction vs rev-1:
  frozen_faker_clock does NOT patch formula helpers - those use the separate injectable
  ColumnGenerator._reference_date; no claim is made on them here.)

## 5. Acceptance

- CONCURRENCY-SAFE + MASKING-UNCHANGED (new, load-bearing):
  - A forced-overlap test: two generations on different threads/tasks with different
    (simulated) ambient clocks run concurrently; each gets the anchor, neither leaks to
    the other; after both exit, the real clock is restored. Must FAIL against a naive
    global-patch implementation.
  - A generation||masking overlap test: masking running concurrently with generation
    sees the REAL clock (its composite/date output is byte-identical to today's,
    unaffected by the generation anchor).
- SEED-STABLE ACROSS DAYS (non-vacuous): generate a dob-bearing config under TWO
  distinct simulated system dates, FRESH composite instances/pools each run (no cached
  pool reuse), the F0 test freeze DISABLED, simulating at the real clock dependency
  Faker reads; assert byte-identical output across the two dates. The test MUST FAIL
  against the current unanchored implementation (prove it bites). Cover table per-row,
  table pooled, AND independent composite paths.
- F0 oracle green under production-only anchoring (digests unchanged, §4).
- Generation parity otherwise intact: tests/native/test_determinism_goldens.py +
  tests/parity/test_phase_f_f0_* + composite tests green; the ONLY changed outputs are
  date-provider values becoming anchored (nothing else).
- MASKING untouched: the composite masking strategy + any masking date path byte-equal
  to today (explicit test).
- ci-mirror clean; pytest-one affected suites, companion-present + absent.

## 6. Risk

The contextvar-gated patch is global-but-inert-by-default (only active inside the
generation context), avoiding the rev-1 race + masking leak. Remaining risks: a
generation faker-date seam not wrapped (guarded by the production-only oracle check +
enumerating callers); a masking path accidentally entering the context (guarded by the
generation||masking test). The import-time patch must be a no-op when the var is unset
(delegates to real clock) so nothing changes globally.

## 7. Gate chain

Codex plan-gate (this doc) -> build (Sonnet) -> ci-mirror + F0 oracle (production-only
anchor) + determinism goldens + concurrency/overlap tests + cross-date seed-stability
+ masking-unchanged, both venvs -> dennis -> Codex final -> merge (Codex + dennis green
+ local CI; GitHub CI ignored). DOCUMENT: CHANGELOG + records note; unblocks the F1/F3
re-baseline.
