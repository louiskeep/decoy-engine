Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, architecture, testing, verification, use-established-methodology

# Generation: anchor Faker date providers to a fixed reference (seed-stable dob)

Foundational re-baseline slice (Cam 2026-10-18: "anchor dob deterministically").
Prereq-friendly for F1/F3 re-baseline: establishes a HERMETIC, seed-stable
generation baseline for Faker date providers (dob) in PRODUCTION, not just tests.

## 1. Problem

`person_dob` -> Faker `date_of_birth` (providers_v2/_faker_adapter.py:54) computes the
date from an age window relative to `datetime.now()` INSIDE Faker
(faker.providers.date_time). Today nothing in src/ anchors this; only the TEST-side
`frozen_faker_clock()` (tests/parity/_f0_fixtures.py) pins it (to ORACLE_CLOCK =
2026-10-16). So in production, dob for the same seed+config SHIFTS with the calendar
day -> generation is NOT reproducible-by-seed across days, and any re-frozen baseline
would be date-fragile. (Confirmed: no date_time/dtdate/reference_date override in
src/decoy_engine/. Formula date helpers are already injectable via
ColumnGenerator._reference_date; this slice is the SEPARATE faker-provider seam.)

## 2. Goal

Make Faker date providers deterministic in production: a fixed-reference clock pinned
around every Faker provider call in the generation path, so `date_of_birth` (and
date_between / date_this_* / date_time*) is a pure function of the Faker instance seed
and the fixed anchor, independent of the wall clock. Reproducible-by-seed across days.

## 3. Design (reuse the established frozen-clock technique)

- Add a PRODUCTION context manager (new module, e.g. generation/_faker_clock.py)
  mirroring tests' `frozen_faker_clock`: patch `faker.providers.date_time.datetime`
  and `.dtdate` to a fixed anchor via the same `_SameInstances` metaclass approach
  (keeps isinstance valid; the test proved a naive subclass raises ParseError).
  Established-methodology note: this is the same technique the F0 test harness uses;
  cite it in the module docstring.
- ANCHOR DATE: a fixed module constant `GENERATION_FAKER_ANCHOR = date(2026,10,16)`
  (== the current ORACLE_CLOCK date) so the re-recorded oracle is UNCHANGED (zero
  digest churn) while behavior moves from test-side to production-side. (Decision to
  confirm at gate: fixed constant now; seed-derived per-job anchor is an explicit
  deferred option - fixed is simplest + hermetic + Cam asked for deterministic.)
- APPLY at the generation Faker-call boundaries the survey identified: the per-row
  `_faker` loop (synthesize.py), `_faker_pool.build_pool_values`, the v2
  FakerAdapter.generate / generate_batch, and the composite `_person._pools` dob pool
  (routes through PoolBuilder -> FakerAdapter). Prefer wrapping generation ENTRY
  (generate_tables / _generate_tables_from_config and generate_tables_streamed when
  it lands) in the context manager so every downstream faker call inherits it, rather
  than sprinkling at each call site - single choke point, fewer missed seams. Verify
  no generation faker-date path escapes the wrapper.
- Masking is untouched (this is generation-only). Non-date faker providers are
  unaffected (the clock patch only changes now()/today()).

## 4. Oracle / baseline

- Because the anchor == ORACLE_CLOCK (2026-10-16), the F0 oracle digests
  (composite_person_deterministic / composite_person_seeded_pool, the only dob-bearing
  fixtures) are UNCHANGED. Confirm by running the F0 oracle suite with the production
  anchor active AND the test conftest `frozen_faker_clock` REMOVED for those modules
  (prove production anchoring alone reproduces the oracle). If any digest moves,
  re-record via `python -m tests.parity._f0_capture` under pinned faker 40.23.0 (both
  venvs) and record it as a DELIBERATE re-baseline in the _f0_expected.py header.
- Decide the test conftest: once production anchors faker dates, the autouse
  `frozen_faker_clock` for test_phase_f_f0_* is redundant for faker dates; KEEP it
  (harmless, also covers any non-anchored provider + formula helpers) OR retire it -
  recommend KEEP for safety this slice, note retirement as follow-up.

## 5. Acceptance

- dob is seed-stable ACROSS DAYS: a test that generates a dob-bearing config twice
  under two DIFFERENT simulated system dates and asserts byte-identical output (mirror
  the hermeticity-fix proof: simulate the ambient clock, show the production anchor
  makes it invariant). This is the core new guarantee.
- F0 oracle suite green with production anchor (digests unchanged, or re-recorded +
  documented).
- Generation parity otherwise intact (no non-date generation output changes): run the
  broader generation/determinism suites (tests/native/test_determinism_goldens.py,
  tests/parity/test_phase_f_f0_*, composite tests) - any change MUST be only
  date-provider values anchoring, nothing else.
- No masking change. Companion-present + absent both green.
- ci-mirror clean (ruff+format+mypy src/decoy_engine); pytest-one on the affected
  suites both venvs.

## 6. Risk

The clock patch is a module-global on faker.providers.date_time, so the production
context manager must wrap generation and restore on exit incl. exceptions (try/finally
like frozen_faker_clock). Thread-safety: generation is single-threaded per job; note
if any concurrent generation exists (it should not). The metaclass isinstance subtlety
is already proven. Over-broad wrapping risk: the patch only affects now()/today(), so
non-date generation is unaffected; the parity suites guard this.

## 7. Gate chain

Codex plan-gate (this doc) -> build (Sonnet) -> ci-mirror + F0 oracle + determinism
goldens + the cross-date seed-stability test, both venvs -> dennis -> Codex final ->
merge (Codex + dennis green + local CI; GitHub CI ignored). DOCUMENT: CHANGELOG +
a records note; this unblocks the F1/F3 re-baseline (hermetic generation baseline).
