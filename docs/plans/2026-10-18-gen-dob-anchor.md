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
  - `anchored_date_of_birth(fake, *, minimum_age=0, maximum_age=115) -> date` that
    REPRODUCES faker.date_of_birth's body with DOB_ANCHOR substituted for `now`:
    `start = change_year(DOB_ANCHOR, -(maximum_age+1)); end = change_year(DOB_ANCHOR,
    -minimum_age); dob = fake.date_time_ad(start_datetime=start, end_datetime=end).date();
    return dob if dob != start else dob + timedelta(days=1)`. Use Faker's OWN
    `change_year` + the Faker instance's `date_time_ad` (seeded) -- do NOT hand-roll the
    date draw (established-methodology: reuse Faker's algorithm, cite it in the docstring).
  - SYMBOL GUARD at import: assert `faker.providers.date_time` exposes `change_year` and a
    `date_time_ad` on the Faker instance; raise a clear engine error if a Faker version
    moves/renames them (so a bump fails LOUDLY, never silently falling back to the clock).
- Wire-in: at the single point the adapter dispatches the `date_of_birth` faker method
  (resolved from provider `person_dob` and any direct `date_of_birth` provider), call
  `anchored_date_of_birth(fake, minimum_age=..., maximum_age=...)` INSTEAD of the bound
  `fake.date_of_birth(...)`. Thread minimum_age/maximum_age through if the provider config
  supplies them (the pool build currently passes none -> defaults 0/115, matching Faker).
  Enumerate every call path that reaches date_of_birth (per-row `_faker`, pooled
  `_faker_pool.build_pool_values`, v2 adapter generate/generate_batch, composite `_pools`)
  and confirm all route through the one adapter dispatch seam (or wire each that does not).
- NO clock patch, NO contextvar, NO generation-vs-masking gate. The reimplementation is a
  pure function engaged wherever date_of_birth is generated.
- SCOPE: this slice anchors `date_of_birth` / `person_dob` ONLY. Other Faker date
  providers (date_this_year, etc.) still read the clock and are OUT of scope (not dob);
  note this explicitly. Seed-derived anchoring is explicitly DEFERRED (fixed constant now).

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
- Masking: the dob pool build via the adapter is shared by masking's composite_person
  strategy. Because the anchored VALUES equal the old values, masking output is
  byte-identical too (no masking re-baseline). The latent improvement (masked dob is now
  reproducibly anchored instead of wall-clock-dependent) comes for free. Prove it with an
  explicit masking-unchanged test.
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
  not the wall clock); assert cross-date invariance (above). The SYMBOL GUARD (§3) is the
  faker-bump tripwire: a moved/renamed change_year/date_time_ad fails loudly.
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
is far less fragile than rev-2. Two bump axes, both GUARDED: (1) dob VALUES change if Faker
alters date_time_ad / change_year / the date_of_birth formula we mirror -> caught loudly by
the F0 oracle + determinism goldens (a deliberate, expected re-baseline). (2) the anchor
MECHANISM breaks if Faker moves/renames the symbols -> caught by the SYMBOL GUARD +
anchor-engaged test (loud fail, never a silent clock fallback). Faker is PINNED (40.23.0),
so this is infrequent. Residual risk: a date_of_birth call path that bypasses the adapter
dispatch seam -> guarded by the production-only F0 oracle check + the caller enumeration.

## 7. Gate chain

Codex plan-gate (this doc) -> build (Sonnet) -> ci-mirror + F0 oracle (production anchor,
freeze bypassed) + determinism goldens + cross-date seed-stability + anchor-engaged +
masking-unchanged, both venvs -> dennis -> Codex final -> merge (Codex + dennis green +
local CI; GitHub CI ignored). DOCUMENT: CHANGELOG + records note; unblocks the F1/F3
generation re-baseline.
