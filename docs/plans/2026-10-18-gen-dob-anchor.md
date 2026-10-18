Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, feature-dev, architecture, testing, verification, use-established-methodology

# Generation: deterministic seed-stable dob via an anchored reimplementation

Foundational re-baseline slice (Cam 2026-10-18: "anchor dob deterministically", "we can
shift the baseline, we're prerelease", "reimplement dob approach"). Rev-3 REPLACES the
rev-2 contextvar-gated clock-patch approach entirely. Instead of patching Faker's module
clock, we reimplement `date_of_birth` using Faker's OWN public `date_time_ad` with
explicit anchor-relative endpoints. This reads NO wall clock, reuses Faker's exact date
distribution, needs no clock patch / contextvar / generation-vs-masking gating, and (see
§4) yields ZERO oracle churn because the anchor equals ORACLE_CLOCK's date.

## 1. Problem

`person_dob` -> Faker `date_of_birth` (providers_v2/_faker_adapter.py:54 maps person_dob
-> date_of_birth). Faker's `date_of_birth` computes `now = datetime.now(tzinfo).date()`,
then `start = change_year(now, -(max_age+1))`, `end = change_year(now, -min_age)`, then
`date_time_ad(start_datetime=start, end_datetime=end).date()` (with a one-day boundary
nudge). The ONLY wall-clock input is `now`. Nothing in src/ anchors it; production dob for
the same seed shifts by calendar day. The dob POOL is built via PoolBuilder ->
FakerAdapter -> faker.date_of_birth per draw (composite _person.py `_pools`, dob_pool),
then indexed deterministically. So the non-determinism lives entirely in the per-draw
clock read at POOL-BUILD time.

Key fact confirmed from Faker source: `date_time_ad(start_datetime, end_datetime)` does
NOT read the clock when both endpoints are given (`_rand_seconds(start, end)` only); it is
a pure function of the Faker seed + the endpoints. `change_year` is a pure helper. Both
are importable from `faker.providers.date_time`.

## 2. Goal

`date_of_birth` / `person_dob` generation is a pure function of (Faker seed + a fixed
anchor), with NO wall-clock read, reusing Faker's own uniform-date algorithm (no
distribution reimplementation). Masking's composite path is unaffected in VALUE (§4).

## 3. Design: reimplement date_of_birth against a fixed anchor

- New module `generation/_dob_anchor.py`:
  - `DOB_ANCHOR = datetime.date(2026, 10, 16)` -- EXACTLY ORACLE_CLOCK's date (see §4).
  - `anchored_date_of_birth(fake, tzinfo=None, minimum_age=0, maximum_age=115) -> date`
    that REPRODUCES faker.date_of_birth's body with DOB_ANCHOR substituted for `now`.
    SIGNATURE (fold P1-new, rev-5): the params are POSITIONAL-OR-KEYWORD, EXACTLY matching
    Faker's `date_of_birth(tzinfo=None, minimum_age=0, maximum_age=115)` -- NOT keyword-only.
    Faker callers pass positionally (e.g. a formula's `date_of_birth(None, 18, 65)`); a
    keyword-only helper would raise TypeError there, which the formula path SWALLOWS and
    emits NULL dob (silent data loss, generators/_formula.py:239). Body:
    `start = change_year(DOB_ANCHOR, -(maximum_age+1)); end = change_year(DOB_ANCHOR,
    -minimum_age); dob = fake.date_time_ad(tzinfo=tzinfo, start_datetime=start,
    end_datetime=end).date(); return dob if dob != start else dob + timedelta(days=1)`. Use
    Faker's OWN `change_year` + the Faker instance's `date_time_ad` (seeded) -- do NOT
    hand-roll the date draw (established-methodology: reuse Faker's algorithm, cite it).
  - INPUT VALIDATION (fold P2-3): preserve faker.date_of_birth's exact validation BEFORE
    the draw -- minimum_age/maximum_age must be int (TypeError otherwise), both >= 0
    (ValueError), minimum_age <= maximum_age (ValueError) -- matching Faker's messages so a
    bad age config fails the same way. tzinfo defaults None (the anchor is a naive date;
    date_of_birth itself returns a tz-naive date); pass tzinfo through to date_time_ad.
  - SYMBOL GUARD at import: assert `faker.providers.date_time` exposes `change_year` and a
    `date_time_ad` on the Faker instance; raise a clear engine error if a Faker version
    moves/renames them (so a bump fails LOUDLY, never silently falling back to the clock).
- Wire-in (fold P1-1, rev-4): there are EXACTLY TWO seams that reach faker.date_of_birth,
  both must be wired to `anchored_date_of_birth`:
  1. `providers_v2/_faker_adapter.py::FakerAdapter._faker_call` (~L149): it does
     `method = getattr(fake, method_name); method(**kwargs)`. When `method_name ==
     "date_of_birth"`, call `anchored_date_of_birth(fake, **kwargs)` instead of the bound
     method. This single seam covers generate() + generate_batch(), hence ALL of: per-row
     `_faker`, pooled `_faker_pool.build_pool_values`, v2 adapter, and composite `_pools`
     (all route through _faker_call).
  2. `generators/_formula.py:312`: the formula generator maps `"date_of_birth":
     self.faker.date_of_birth` and calls it DIRECTLY, bypassing the adapter (this is the
     bypass the plan-gate found). Replace that entry with a wrapper that forwards BOTH
     positional and keyword args to the helper, e.g. `lambda *a, **k:
     anchored_date_of_birth(self.faker, *a, **k)` -- it MUST accept `date_of_birth(None, 18,
     65)` positionally (see signature note above). Regression tests: a formula calling
     date_of_birth positionally AND one calling it by keyword, both producing non-null
     anchored dates.
  COVERAGE TEST (required, fold P2-new rev-5): patch the PROVIDER method that actually
  backs it -- `faker.providers.date_time.Provider.date_of_birth` (NOT `faker.Faker`, which
  has no class-level date_of_birth attribute, so monkeypatch.setattr would fail at setup;
  use raising=False if patching the proxy). Install a SPY that records calls (do not just
  raise -- the formula path swallows raises and returns null, which would falsely satisfy
  "none raises"). Run every person_dob generation path (per-row, pooled, composite,
  formula) and assert: ZERO calls to the unanchored provider method AND all dob outputs are
  non-null + equal the anchored values. The test MUST fail against the current unanchored
  implementation (demonstrate it).
  Thread minimum_age/maximum_age through where the provider config supplies them (pool
  build passes none -> defaults 0/115, matching Faker).
- NO clock patch, NO contextvar, NO generation-vs-masking gate. The reimplementation is a
  pure function engaged at the two seams above.
- SCOPE: this slice anchors the FAKER `date_of_birth` / `person_dob` path ONLY. The MIMESIS
  person_dob backend (providers_v2/mimesis/_adapter.py:60 -> datetime.date) is a DIFFERENT
  library with its own clock read and is OUT of scope this slice (the default backend +
  the F0 oracle are Faker; a mimesis anchor is a separate follow-up -- note it, do not
  silently leave it looking covered). Other Faker date providers (date_this_year, etc.)
  are also out of scope (not dob). Seed-derived anchoring is DEFERRED (fixed constant now).

## 4. Oracle / baseline: ZERO churn (the central claim, must be proven)

Because DOB_ANCHOR == ORACLE_CLOCK.date() (ORACLE_CLOCK = datetime(2026,10,16,12,0,0),
tests/parity/_f0_fixtures.py) AND the F0 oracle was recorded under `frozen_faker_clock()`
(which makes datetime.now() return ORACLE_CLOCK), the anchored computation produces the
IDENTICAL endpoints and the IDENTICAL `date_time_ad` draw as the frozen-clock
faker.date_of_birth. Through the pool build + deterministic pool indexing this means the
composite dob values, pool indices, and final digests are BYTE-IDENTICAL to the existing
oracle. Therefore:
- ACCEPTANCE: run the F0 oracle suite with the production anchor active AND the test
  `frozen_faker_clock` REMOVED/bypassed for those modules; digests MUST match the frozen
  digests byte-for-byte. A mismatch is a FINDING to investigate (a missed seam, a wrong
  anchor, or a pool-path that bypasses the adapter), NOT license to re-record. Do NOT use
  `_f0_capture` to "fix" a mismatch (it runs under the test freeze and would hide a missing
  production anchor).
- Masking (fold P1-2): the dob pool build via the adapter is shared by masking's
  composite_person strategy. The masking-unchanged proof needs a CORRECT baseline, which is
  NOT the anchored output (circular). Capture it properly: BEFORE wiring the change, run the
  composite_person masking strategy on a fixed dob-bearing config UNDER `frozen_faker_clock`
  (so the pre-change wall-clock dependence does not make the baseline itself unstable), and
  commit those output bytes as a fixture. AFTER the change, run the SAME masking WITHOUT the
  freeze and assert byte-equality to the committed fixture. This proves the anchored values
  equal the frozen pre-change values AND that masking no longer depends on the wall clock.
  The latent improvement (masked dob is now reproducibly anchored) comes for free.
- The baseline-shift authority Cam granted is held in reserve: if any digest DOES move, it
  is a real finding first; re-baseline only after root-causing it (not a reflex).

## 5. Acceptance

- SEED-STABLE ACROSS DAYS (non-vacuous): generate a dob-bearing config under TWO distinct
  simulated system dates, FRESH composite instances/pools each run (no cached pool reuse),
  with the F0 test freeze DISABLED; assert byte-identical output across the two dates. MUST
  FAIL against the current unanchored implementation (prove it bites). Cover table per-row,
  table pooled, AND independent composite paths.
- ANCHOR-ENGAGED check (Cam's requirement, reframed for the reimplement): assert
  DOB_ANCHOR == date(2026,10,16); assert a dob generated through the engine EQUALS the
  value from `anchored_date_of_birth` under the same seed (the reference date IS the anchor,
  not the wall clock); assert cross-date invariance (above).
- DIFFERENTIAL vs upstream (fold P2-4): the real independent reference + faker-bump
  tripwire. With the clock FROZEN to DOB_ANCHOR, assert `anchored_date_of_birth(fake, ...)`
  equals `faker.date_of_birth(...)` for the SAME seed, comparing BOTH the returned date AND
  the Faker RNG state after the call, across multiple seeds and age ranges (incl. non-default
  min/max). This fails loudly if a future Faker changes date_of_birth's formula (which the
  copied impl would otherwise silently diverge from). Use explicit runtime raises, NOT bare
  `assert` statements (which `python -O` strips), for both this and the §3 symbol guard.
- F0 oracle green under production anchoring with the test freeze bypassed, digests
  UNCHANGED (§4).
- Generation parity otherwise intact: tests/native/test_determinism_goldens.py +
  tests/parity/test_phase_f_f0_* + composite tests green; the ONLY path changed is the
  date_of_birth draw source, and its values are unchanged vs the frozen oracle.
- MASKING unchanged: composite masking strategy dob output byte-equal to today (explicit
  test), since the anchored values equal the old values.
- ci-mirror clean; pytest-one affected suites, companion-present + absent.

## 6. Risk + Faker-version robustness

This reuses Faker's PUBLIC `date_time_ad` + `change_year` (not a clock monkeypatch), so it
is far less fragile than rev-2. Faker-bump coverage, stated precisely (fold P2-4): (1) a
change to `date_time_ad` or `change_year` (the primitives we call) shifts our VALUES ->
caught by the F0 oracle + determinism goldens. (2) a change to the symbols' NAMES/location
-> caught by the §3 SYMBOL GUARD (detects removal). (3) a change to `date_of_birth`'s own
formula does NOT touch our copied impl or the goldens, so it is caught ONLY by the §5
DIFFERENTIAL test (anchored vs upstream date_of_birth at the frozen anchor). These three
together are the tripwire; the symbol guard alone is NOT sufficient (it detects removal, not
behavioral change). Faker is PINNED (40.23.0), so bumps are infrequent + deliberate.
Residual risk: a date_of_birth call path missed by the two §3 seams -> guarded by the §3
coverage test (monkeypatch date_of_birth to raise) + the production-only F0 oracle check.

## 7. Gate chain

Codex plan-gate (this doc) -> build (Sonnet) -> ci-mirror + F0 oracle (production anchor,
freeze bypassed) + determinism goldens + cross-date seed-stability + anchor-engaged +
masking-unchanged, both venvs -> dennis -> Codex final -> merge (Codex + dennis green +
local CI; GitHub CI ignored). DOCUMENT: CHANGELOG + records note; unblocks the F1/F3
generation re-baseline.
