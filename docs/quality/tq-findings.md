# TQ findings log

Real code observations surfaced by the Test-Quality Program's oracle authoring
(distinct from mutation survivors, which are tracked in per-module ledgers).
These are potential source issues found while writing invariant tests. None were
fixed autonomously; each is pinned as observed behavior in the test suite and
listed here for a decision. Ordered by stakes.

## TQ-1 crown jewels (2026-07-25, branch `tq/crown-jewels`)

### 1. FK key route divergence: float vs Decimal of equal value (RI) -- RESOLVED (7e7be68)
`execution/_fk_keys.py`. `fk_key_value(12.5) == fk_key_value(Decimal("12.5"))` is
`True` (Python numeric tower), so the full-frame / sequential route (a plain-dict
`parent_map`) treats an equal-valued float parent key and Decimal child FK as ONE
key. But `fk_join_key` type-tags them apart (`\x00FLOAT:` vs `\x00DEC:`), so the
out-of-core route (string-token map) treats the SAME pair as a NON-match. Two
routes can disagree on whether a child row is an orphan for mixed
float64/decimal128 FK columns of equal value. RI is worst-blast-radius; confirm
whether a real schema can produce that column pairing, and if so, unify the two
routes' key identity. **FIXED in `7e7be68`** (fk_join_key encodes a fractional
float through its exact `Decimal(value)` expansion so float/Decimal fold to one
`\x00DEC:` token IFF Python `==` holds; dennis-SOUND). Regression sentries:
`test_fk_keys_invariants.py::test_fractional_float_and_equal_decimal_share_a_join_token`
and `::test_float_and_decimal_that_are_unequal_keep_distinct_join_tokens`, plus
an end-to-end route-parity case (fractional float parent vs Decimal child).

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

## Step-4 sweep observations

### 5. `_shuffle` narrows `timestamp[ns] -> timestamp[us]` on datetime columns (latent)
`execution/_strategies/_shuffle.py`. The `to_numpy(dtype=object)` boxing (the Q13
object-dtype fix) turns datetime values into `datetime.datetime`, so the adapter
re-infers `timestamp[us]` for a `timestamp[ns]` source. Surfaced by the batch-2
dennis gate while grading `_shuffle` (the `dtype=None` mutant keeps `ns`, which is
why the mutant is killable, not equivalent). Lossy for sub-microsecond timestamps.
Pinned as current behavior by
`test_datetime_column_output_type_and_permutation_pinned`; not fixed here (a
TESTS-only batch). Worth a decision: preserve source resolution, or accept us.

### 6. `derived_aggregate` forward-reference silently yields 0 instead of failing closed (real, out-of-scope)
`transforms/derived_aggregate.py` + `config/_tables.py` + `plan/_checks_derived_aggregate.py`.
A generate-mode `derived_aggregate` column whose `column` names a sibling NOT yet
generated (forward reference, or an absent `column` key) is not caught: there is no
`derived_aggregate` branch in `_type_params_present`, and `check_derived_aggregate_refs`
short-circuits (`if source_col and ...`) when the ref is absent. It reaches
`generate_derived_aggregate_column`, where `generated.get(config.column, [])` returns
`[]` and the aggregate silently produces `0` for every row rather than a coded error.
Surfaced by the batch-6 dennis gate while grading the mask/mask-fallback defaults.
Silent-wrong-output, not fail-closed. Fix belongs in the plan-compile refs check /
`_type_params_present` (add the derived_aggregate branch), NOT in this tests-only
sweep. File as a separate product issue.

### 7. dtype=object output survivors are killable on an all-null non-object column (ledger-tightening, not a bug)
`transforms/text_mask.py`, `execution/_strategies/_nested.py` (and originally
`_text_redact.py`, now fixed). Several ledgers classified `pd.Series(col_values,
dtype=object) -> dtype=None`/dropped as EQUIVALENT with a "col_values is uniformly
str-or-null so object is inferred anyway" rationale. Batch-7 dennis showed that is
false for an ALL-NULL non-object column (e.g. all-null float64): without the
explicit `dtype=object` pandas infers `float64`, an observable output-dtype change
(the explicit object is a deliberate Arrow/concat-boundary contract). `_text_redact`
was tightened (mutmut_110/113 reclassified LOGIC, killed by
`test_all_null_float_column_output_is_object_dtype`). The equivalent
`_text_mask`(mutmut_125/128) and `_nested`(mutmut_163/166) survivors are the same
pattern -- redacted/masked VALUES are identical (all null), only the dtype differs;
gated as immaterial at the time. Tightening opportunity: add the same all-null test
to those two modules and reclassify. Not a product bug.

### 8. mutmut in-process runner misreports surviving mutants as timeout on substrate suites (harness limitation, not a product bug)
`execution/_planner.py` (and the Batch-A execution substrates generally). Grading
`_planner` with the standard focused-selection harness produced "459 mutants, 313
killed, 146 timeout, 0 survived" -- but sampling the ⏰ mutants showed pure
message-prose mutations (e.g. `reason = None`, a case-changed error string) that
cannot hang. Running one such mutant standalone
(`MUTANT_UNDER_TEST=...x__table_column_entries__mutmut_2 pytest test_execution_planner.py`)
completes in ~3s and the mutant SURVIVES (exit 0) -- so the 146 "timeouts" are 146
real survivors the harness is hiding, making the score false. The cause is mutmut's
per-mutant limit `(estimated_time_of_tests + timeout_constant) * timeout_multiplier`:
for these suites the per-test call-duration estimate is ~0, so the limit collapses,
and mutmut's in-process runner marks the survivor a timeout. Raising
`timeout_constant` to 15.0 (a ~225s wall / ~450s CPU per-mutant limit) did NOT change
the verdict -- the same 5 mutants still reported ⏰ -- so it is not a limit-tuning
issue but a runner/suite interaction. The substrate tier (biggest P0 section, Batch A)
therefore needs a different grading path (a runner that shells out to standalone
pytest per mutant, or an alternative mutation tool), NOT the in-process focused
harness that grades the strategy/transform tier cleanly. Logged, not worked around;
`_planner` and siblings are NOT falsely scored. The fast strategy/transform modules
are unaffected (their surviving runs finish well under the default limit).

### 9. mutmut does not mutate module-level constants -- constant-driven logic needs explicit per-value tests (methodology)
Surfaced by the `transforms/_fpe_checksum.py` batch-gate (2026-07-26). mutmut only
generates mutants INSIDE functions; a module-level constant like
`_EXACT_LENGTHS["gtin"] = frozenset({8, 12, 13, 14})` is copied verbatim into the
mutants tree and never mutated. So a suite can reach "LOGIC-100%" on mutmut's own
mutants while leaving a real blind spot: here GTIN was tested only at length 14,
so nothing pinned that 8/12/13 are legal or that the exact-length guard fires --
a dropped length would silently reject a valid GTIN with no failing test, and
mutmut would never flag it. Lesson for the sweep: for any module whose behavior is
driven by a module-level table/set/dict of constants (length sets, scheme maps,
threshold tables), add explicit tests that exercise EACH value, independent of the
mutation score. The mutation score is necessary, not sufficient, for
constant-driven code. (Remediated for `_fpe_checksum` by covering all four GTIN
lengths + an illegal-length fail-closed case.)

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
