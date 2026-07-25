# TQ findings log

Real code observations surfaced by the Test-Quality Program's oracle authoring
(distinct from mutation survivors, which are tracked in per-module ledgers).
These are potential source issues found while writing invariant tests. None were
fixed autonomously; each is pinned as observed behavior in the test suite and
listed here for a decision. Ordered by stakes.

## TQ-1 crown jewels (2026-07-25, branch `tq/crown-jewels`)

### 1. FK key route divergence: float vs Decimal of equal value (RI, worth root-cause)
`execution/_fk_keys.py`. `fk_key_value(12.5) == fk_key_value(Decimal("12.5"))` is
`True` (Python numeric tower), so the full-frame / sequential route (a plain-dict
`parent_map`) treats an equal-valued float parent key and Decimal child FK as ONE
key. But `fk_join_key` type-tags them apart (`\x00FLOAT:` vs `\x00DEC:`), so the
out-of-core route (string-token map) treats the SAME pair as a NON-match. Two
routes can disagree on whether a child row is an orphan for mixed
float64/decimal128 FK columns of equal value. RI is worst-blast-radius; confirm
whether a real schema can produce that column pairing, and if so, unify the two
routes' key identity. Pinned:
`test_fk_keys_invariants.py::test_fractional_float_and_decimal_of_equal_value_do_not_share_a_join_token`.

### 2. `hkdf_expand` does not validate negative length (crypto, minor)
`determinism/_hkdf.py`. A negative `length` is not rejected: `n = (length+31)//32`
is <= 0, the block loop never runs, and `b"".join([])[:length]` returns `b""`.
Silent empty output rather than the `ValueError` the over-max case raises. No
docstring contract for it and likely unreachable from real callers, but the
asymmetry (over-max raises, under-zero silently empties) deserves an explicit
guard. Not currently asserted either way in the suite.

### 3. `_compose([])` raises bare `IndexError` (DP, unreachable today)
`quality/dp_budget.py`. Composing zero certificates raises a bare `IndexError`
rather than a coded `DpBudgetError` or a zero-loss base case. Unreachable via the
public API today (`Schedule.row_count_name` is mandatory, so `query_count >= 1`).
Worth a base case if `Schedule` ever gains an optional row-count query. Pinned as
documented-unreachable in `test_dp_budget_invariants.py`.

### 4. `compute_lock_fingerprint` serialization has no escaping (DP, latent)
`quality/dp_provenance.py`. The fingerprint input is `"\n".join(f"{name}==
{version}")` with no escaping; a name/version containing `==` or a newline could
make two different distribution sets serialize identically. Not reachable via
`installed_distribution_set()` (PEP-503-canonical names only), but a latent
ambiguity in the function's contract for arbitrary callers.

## Notes for grading (Phase B)
- `quality/dp.py` and parts of `quality/dp_provenance.py` have `dp_certified`-gated
  tests that SKIP off the certified 77-dist profile, so mutmut in an uncertified
  shell can only grade the non-cert-gated logic. Grade the cert-gated paths on the
  certified profile (the CI cert-gate job) or note the coverage gap.
- `dp_budget` additivity/single-cert tolerances are empirically derived over PLD
  discretization noise; confirm via mutmut they still kill a real composition-logic
  mutant rather than only noise.

## Codex verdict on finding 1 (FK float/Decimal route divergence), 2026-07-25

Cam asked Codex. Verdict: **REAL correctness bug** -- the RI outcome must not
depend on execution route. Equal-valued numeric PK/FK values should map to the
SAME FK key when the source system considers them equal, else valid children
become false orphans. **Fix direction:** make the out-of-core route (`fk_join_key`,
the string-token map) use the SAME canonical numeric equivalence as
`fk_key_value()` -- normalize float/Decimal to a shared, collision-safe numeric
token rather than type-tagging them apart. Source change to `_fk_keys.py`,
high-stakes (RI); Cam-gated, NOT done autonomously.
