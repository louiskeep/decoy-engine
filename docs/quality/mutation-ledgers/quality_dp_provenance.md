# Mutation grading: `quality/dp_provenance.py` -- DEFERRED

TQ crown-jewels pass, 2026-07-25. dp_provenance could not be meaningfully
mutation-graded in this run: a baseline mutmut left 87/87 mutants surviving even
though the covering tests pass (56 passed / 2 skipped). Root cause: the suite is
monkeypatch-heavy BY DESIGN (57 monkeypatch/setattr sites across
`test_dp_provenance.py` + the property file) -- it validates the fail-closed GATE
LOGIC (`check_fit_environment`, `assert_lock_matches_installed`) by replacing
`compute_lock_fingerprint` / `installed_distribution_set` / `current_platform`
with synthetic certified/near-miss stand-ins, so mutations INSIDE those real
functions cannot be killed by those tests. Grading the implementations needs
direct (non-monkeypatched) tests of `compute_lock_fingerprint`,
`installed_distribution_set`, and `ProvenanceError`, and the real certified
fingerprint path (certified profile) for the membership checks.

The oracle suite `tests/property/test_dp_provenance_invariants.py` IS committed
(Phase A, 21 tests) and DOES validate the load-bearing fail-closed behavior
(near-miss / tampered / single-dist-off stacks all rejected). What's deferred is
mutation-grading the fingerprint/distribution-set IMPLEMENTATION functions.

ACTION for a follow-up: add direct impl tests (determinism + sensitivity of
compute_lock_fingerprint over real inputs; installed_distribution_set metadata
handling; ProvenanceError code/message formatting) so their mutants are killable,
then grade on the certified profile.
