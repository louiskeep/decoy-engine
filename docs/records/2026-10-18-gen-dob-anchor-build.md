Status: record

# Generation: seed-stable date of birth (build record, 2026-10-18)

Plan: `docs/plans/2026-10-18-gen-dob-anchor.md` (Codex plan-gate GO).

## What changed

- `generation/_dob_anchor.py`: `DOB_ANCHOR = 2026-10-16` and `anchored_date_of_birth(fake,
  tzinfo=None, minimum_age=0, maximum_age=115)`. It is Faker's `date_of_birth` with the anchor in
  place of `datetime.now()`, drawn through Faker's own `date_time_ad` and `change_year`. The
  signature is positional-or-keyword so formulas can call `date_of_birth(None, 18, 65)`.
- Two seams call it: `FakerAdapter._faker_call` (per-row, pooled, composite pools) and the
  formula scope in `generators/_formula.py`.
- Import-time symbol guard raises `DecoyError` if Faker drops `change_year` or `date_time_ad`.

## Why the oracle did not move

`DOB_ANCHOR` is the date of `ORACLE_CLOCK`, and the F0 oracle was recorded under that frozen
clock. The anchored draw has the same endpoints and the same RNG draw. Checked by running the
F0 modules with `F0_DOB_ANCHOR_CHECK=poison` (Faker's own `date_of_birth` raises) and
`F0_DOB_ANCHOR_CHECK=noclock` with `-k composite_person` (no freeze). No digest moved.

## Known gaps

- A generation or masking column with `faker_type: date_of_birth` resolves through the reflected
  Faker provider table (`internal/faker_setup.py::get_faker_providers` and
  `resolve_pool_provider`), not the two seams above. It still reads the clock.
- Mimesis `person_dob` reads its own clock. Out of scope for this slice.
- Faker date helpers other than dob (`date_this_year`, ...) are unchanged.

## Upgrade tripwire

`tests/unit/generation/test_dob_anchor.py` compares the helper with upstream
`date_of_birth` (value and RNG state) with the clock frozen to the anchor. A Faker change to that
formula fails there.
