# Mimesis adoption evaluation, 2026-06-12

First full run of the S7 parity suite (`providers_v2/mimesis/_parity.py`)
against all 11 candidates. Environment: mimesis 19.1.0, Faker baseline from
the locked venv, locale en_US, n=10,000 samples per provider, single host.
Gate: checks 1-6 pass AND benchmark ratio (mimesis time / Faker time) < 0.20.
Check 7 (distribution) is advisory per S7 spec section 3.

## Adopted (5)

| provider | ratio | speedup | checks 1-6 | check 7 (advisory) |
|---|---|---|---|---|
| person_last_name | 0.018 | 56x | pass | pass (1000 vs 986 distinct) |
| person_name | 0.032 | 31x | pass | pass (9979 vs 9317) |
| person_full_name | 0.032 | 31x | pass | pass (9982 vs 9396) |
| person_email | 0.040 | 25x | pass | pass (9997 vs 9815) |
| person_first_name | 0.060 | 17x | pass | FAIL: 3103 vs 656 distinct |

person_first_name's check-7 failure is in the safe direction: mimesis draws
from a pool roughly 5x RICHER than Faker's en_US first-name list. More
distinct synthetic values means lower re-identification pressure per value,
not higher. Adopted per the `is_adoptable` predicate (check 7 does not gate);
recorded here as the manual review the spec requires.

## Rejected (6)

| provider | failing gate | evidence |
|---|---|---|
| person_dob | speed | ratio 0.20-0.25 across 4 runs (stable fail, just over threshold) |
| address_state | speed | ratio 0.37 |
| address_zip | speed | ratio 0.82 |
| address_city | length + distribution | mean 9.2 vs 12.0; 1749 vs 7735 distinct |
| address_street | length + distribution | mean 7.3 vs 22.4 (no street numbers); 2307 vs 10000 distinct |
| person_phone | length | mean 13.5 vs 16.2, max 15 vs 22 (no extension formats) |

The address rejections are behavioral, not speed: mimesis address parts are
structurally narrower than Faker's en_US providers and would visibly change
pool character. Re-evaluation requires a fresh full-gate pass; do not re-add
on speed evidence alone.

## Standing guards

- `tests/unit/providers_v2/mimesis/test_mimesis_adapter.py::TestAdoptionDriftTripwire`
  re-runs gating checks 1-6 with SEEDED samples for every adopted provider on
  every CI run; a mimesis or Faker upgrade that breaks parity fails CI.
  Benchmark ratios are not asserted in CI (timing noise); re-measure manually
  on dependency bumps with `python scripts/reevaluate_mimesis.py` (see the
  procedure below).
- The `mimesis` extra is pinned `>=19.0,<20`; widening past 19.x requires
  re-running this evaluation.
- Registry shape is unchanged: 34 providers either way; adoption rebinds the
  5 adopted names to MimesisAdapter when the extra is installed and leaves
  everything Faker/native when it is not
  (`test_optional_dep.py::test_mimesis_binding_matches_install_state`).

## Re-evaluation procedure

Trigger: any change that widens or replaces the `mimesis>=19.0,<20` pin
(deferred follow-up 9, 2026-06-12). Steps:

1. Install the candidate version into the working venv
   (`uv pip install 'mimesis==<candidate>'`).
2. Run `python scripts/reevaluate_mimesis.py` (options: `--locale`, `--n`,
   `--json`). The script runs `run_parity_suite` for all 11 candidates and
   diffs the verdicts against `ADOPTED_MIMESIS_PROVIDERS`. Exit 0 means no
   drift; exit 1 prints which providers should be added or removed.
   Benchmark ratios are timing-sensitive: run on an idle machine and
   re-run any provider within ~0.05 of the 0.20 gate (person_dob has
   historically sat at 0.20-0.25).
3. On drift: update `ADOPTED_MIMESIS_PROVIDERS` in `_adoption_matrix.py`,
   append a new dated results table to THIS document (keep the old one),
   and confirm `TestAdoptionDriftTripwire` stays green. Behavioral
   rejections (the address providers' narrower pools) need upstream data
   changes, not just speed, to flip; do not re-add on speed evidence alone.
