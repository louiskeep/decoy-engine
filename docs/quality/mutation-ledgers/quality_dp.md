# Mutation grading: `quality/dp.py` -- DEFERRED to the certified DP profile

TQ crown-jewels pass, 2026-07-25. dp.py could NOT be mutation-graded in the
default (uncertified) shell: 47 of its 88 covering tests are `dp_certified`-gated
and SKIP off the certified 77-dist / Python 3.10.20 profile (`41 passed, 47
skipped`). The skipped tests are exactly the ones that exercise the real OpenDP
mechanism (`fit_dp_snapshot`, shape/domain preservation, categorical/numeric
release), so a mutmut run here leaves ~320/321 mutants uncovered -- an ENVIRONMENT
limitation, not a test gap.

The oracle suite `tests/property/test_dp_mechanisms_invariants.py` IS committed
(Phase A) with its cert-gated tests correctly marked; the non-cert-gated
fail-closed pre-gate + serialization-helper tests run and pass here.

ACTION for a follow-up run on the certified profile (the CI cert-gate env):
`uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run`
with `only_mutate=["src/decoy_engine/quality/dp.py"]` and test selection
`tests/unit/quality/test_dp.py` + the property file, on the certified stack so
the gated tests execute. Then classify + kill logic survivors + ledger as usual.
