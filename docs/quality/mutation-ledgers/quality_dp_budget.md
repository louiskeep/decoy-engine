# Equivalent-mutant ledger: `dp_budget.py`

TQ crown-jewels pass, 2026-07-25. A mutmut run against `dp_budget.py` (the DP
privacy-budget composition/accounting module -- the sole `dp_accounting` call
site, privacy-critical) left **37 survivors**. Every survivor was classified
LOGIC or EQUIVALENT per `docs/quality/module-test-quality-playbook.md`
("Scope the score to LOGIC, not error-message wording") and, for the DP
composition/calibration mutants specifically, per the SPECIAL DP CASE rule:
a composition-LOGIC mutant can survive because the test tolerance is too
loose to notice the drift, so each such survivor was checked against
OpenDP/`dp_accounting` directly before being called equivalent, with the
observed numbers recorded below rather than asserted.

**19 LOGIC survivors** were killed with new tests appended to
`tests/property/test_dp_budget_invariants.py` (a new section headed "TQ
crown-jewels mutation-kill pass"). **18 survive and are equivalent**: 7 are
message-prose-only (WORDING, no downstream code reads `.message` or
`str(exc)` content -- verified by grep across `src/decoy_engine/quality/`),
and 11 are true no-ops verified empirically against the installed
opendp/dp_accounting versions (numbers below).

Covering tests: `tests/unit/quality/test_dp_budget.py` (schedule/session
example-based tests, including several real-`_RealOpenDpBackend` paths) +
`tests/property/test_dp_budget_invariants.py` (composition invariants +
this pass's 10 new targeted tests, 23 tests total in that file).

Bugs found in `dp_budget.py`: **none**. Every LOGIC mutant traces to a real
test gap (mostly: no existing test pinned one measurement's own calibration
precision, only the fit-wide composed total), not a defect in the module
itself.

## WORDING (7): error-message prose only

The literal is consumed only inside a raised `DpBudgetError`'s (or a bare
`AttributeError`'s) human message; `grep -rn "\.message" src/decoy_engine/quality/dp*.py`
confirms no code anywhere in this repo reads `.message` or a caught
`DpBudgetError`'s `str()` for comparison, branching, or serialization -- only
`.code` is ever asserted (in both the unit and property suites). Matches the
`keyprovider.py` ledger's identical precedent (`message=message -> None` in a
passthrough, classified WORDING there too).

| Mutant | Mutation |
|---|---|
| `x_check_epsilon_supported__mutmut_4` | the whole three-line `message=(...)` f-string -> `message=None` |
| `x_check_epsilon_supported__mutmut_9` | same message wrapped in `XX...XX` |
| `x_check_epsilon_supported__mutmut_10` | same message lowercased |
| `x_check_epsilon_supported__mutmut_11` | same message uppercased |
| `xǁDpBudgetErrorǁ__init____mutmut_2` | `self.message = message` -> `self.message = None` |
| `xǁDpBudgetErrorǁ__init____mutmut_3` | `super().__init__(f"[{code}] {message}")` -> `super().__init__(None)` |
| `x___getattr____mutmut_5` | `raise AttributeError(f"module {__name__!r} has no attribute {name!r}")` -> `raise AttributeError(None)` |

## NO-OP (11): verified equivalent against the installed OpenDP/dp_accounting

Each row's "why equivalent" was checked by direct construction against the
installed opendp/dp_accounting in this environment (not asserted from
memory), per the SPECIAL DP CASE instruction. `_SCALE_SEARCH_BOUNDS =
(1e-12, 1e12)`; the eps_q domain probed is the module's own realistic range
(`_EPS_Q_FLOOR = 1e-9` to `_DP_EPSILON_CEILING = 700.0`).

| Mutant | Mutation | Why equivalent (numbers) |
|---|---|---|
| `x__certificate_to_pld__mutmut_6` | drops `value_discretization_interval=_PLD_DISCRETIZATION` kwarg | `dp_accounting.pld.privacy_loss_distribution.from_privacy_parameters`'s own default is `value_discretization_interval: float = 0.0001` (`inspect.signature`) -- identical to `_PLD_DISCRETIZATION = 1e-4`. Dropping the kwarg falls back to the exact same value. |
| `xǁ_RealOpenDpBackendǁ_count_over_domain__mutmut_7` | `tf.make_count(domain, _dp.symmetric_distance(), TO=int)` drops `TO=int` | Default `TO='int'` (string) resolves to the identical `AtomDomain(T=i32)` output domain and identical `.invoke(['a','b'])` result (`2` both ways) -- OpenDP normalizes the Python `int` type and the `'int'` string to the same `RuntimeType`. |
| `xǁ_RealOpenDpBackendǁ_count_over_domain__mutmut_10` | scale search `bounds=_SCALE_SEARCH_BOUNDS` -> `bounds=None` | `_dp.binary_search` falls back to its own exponential search when `bounds=None`; probed at eps_q in {1e-9, 1e-6, 0.01, 5.0, 700.0} (the module's floor-to-ceiling range), the found scale is BIT-IDENTICAL (`diff == 0.0` exactly) to the bounded search at every point. |
| `xǁ_RealOpenDpBackendǁ_count_over_domain__mutmut_18` | scale search `<= eps_q` -> `< eps_q` | Continuous-float bisection converges to the same boundary regardless of the open/closed comparison. At eps_q=0.25: `<=` gives scale=4.0 (map(1)==0.25 exactly); `<` gives scale=4.000000000000001 (map(1)==0.24999999999999997). Diff ~8.88e-16 -- about 4 ULPs of float64 (machine epsilon ~2.22e-16), pure numerical noise, not a step change (contrast with the discrete threshold-search case below, which IS a real step and IS killed). |
| `xǁ_RealOpenDpBackendǁnumeric_measurement__mutmut_22` | `then_count_by_categories(..., null_category=False)` -> `null_category=None` | OpenDP's bool-typed `null_category` parameter treats Python `None` identically to `False` here: verified identical `Transformation` repr (input/output domain, metric) and identical `.invoke([0.5,1.5,2.5,10.0])` result (`[1,1,1,1]` both) for `null_category=False` vs `null_category=None`. (Contrast with `null_category` dropped entirely, which defaults to the library's OWN `True` -- a real 5th-element overflow bin -- killed as LOGIC below.) |
| `xǁ_RealOpenDpBackendǁnumeric_measurement__mutmut_30` | scale search `bounds=_SCALE_SEARCH_BOUNDS` -> `bounds=None` | Same exponential-search equivalence as the `_count_over_domain` case above, re-probed against the `make_find_bin >> then_count_by_categories` chain at the same eps_q range: `diff == 0.0` at every point. |
| `xǁ_RealOpenDpBackendǁ_grouped_over_domain__mutmut_17` | scale search `bounds=_SCALE_SEARCH_BOUNDS` -> `bounds=None` | Same exponential-search equivalence, re-probed against the `make_count_by >> then_laplace_threshold` chain (scale-only stage, threshold fixed at `_I32_MAX`) at the same eps_q range: `diff == 0.0` at every point. |
| `xǁ_RealOpenDpBackendǁ_grouped_over_domain__mutmut_28` | scale search `<= eps_q` -> `< eps_q` | Same continuous-boundary float-noise equivalence as `_count_over_domain__mutmut_18`, re-probed on this chain. |
| `xǁ_RealOpenDpBackendǁ_grouped_over_domain__mutmut_32` | threshold search `T=int` -> `T=None` | `_dp.binary_search` auto-infers the `int` type from the literal `(1, _I32_MAX)` integer bounds tuple identically to passing `T=int` explicitly: probed with 20 random integer targets spanning the full i32 range, zero mismatches in either value or `type()`. |
| `xǁ_RealOpenDpBackendǁ_grouped_over_domain__mutmut_35` | threshold search drops the `T=int` kwarg entirely | Same auto-inference equivalence as mutmut_32 (dropping the kwarg falls back to the identical `T=None` inference path). |

## LOGIC (19): killed by new tests in this pass

All in `tests/property/test_dp_budget_invariants.py`, appended after
`test_pure_epsilon_composition_matches_basic_sequential_composition` under
the "TQ crown-jewels mutation-kill pass" heading. `max_examples` restored to
300 (was temporarily 25) as part of this pass.

| Mutant | Mutation | Killed by |
|---|---|---|
| `x__search_largest__mutmut_1` | `if not predicate(lower): return lower` -> `if predicate(lower): return lower` (inverted) | `test_search_largest_returns_the_lower_bound_when_predicate_fails_everywhere` + `test_search_largest_bisects_to_the_predicates_crossing_point` |
| `x__search_largest__mutmut_8` | `mid = (lo + hi) / 2.0` -> `/ 3.0` | `test_search_largest_bisects_to_the_predicates_crossing_point` (known-answer: predicate `x <= 0.37` over `[0,1]` converges to ~0.37 correctly, ~0.222 under the mutant -- diff ~0.15, not noise) |
| `x__binary_search_endpoint_aware__mutmut_1` | `if T is not None:` -> `if T is None:` (branch inversion) | `TestBinarySearchEndpointAwareForwarding::test_forwards_bounds_and_t_unchanged_when_t_is_given` |
| `x__binary_search_endpoint_aware__mutmut_4` | `T=T` -> `T=None` in the forwarding call | same |
| `x__binary_search_endpoint_aware__mutmut_7` | drops the `T=T` kwarg entirely (same effect as mutmut_4) | same |
| `x__binary_search_endpoint_aware__mutmut_9` | `bounds=bounds` -> `bounds=None` in the `T is None` branch | `TestBinarySearchEndpointAwareForwarding::test_omits_t_entirely_and_forwards_bounds_unchanged_when_t_is_not_given` |
| `x__binary_search_endpoint_aware__mutmut_11` | drops the `bounds=bounds` kwarg entirely (same effect as mutmut_9) | same |
| `xǁ_RealOpenDpBackendǁ_count_over_domain__mutmut_17` | scale search `.map(1) <= eps_q` -> `.map(2) <= eps_q` | `TestRealOpenDpBackendCalibrationPrecision::test_count_measurement_certificate_equals_the_calibration_target_epsilon` |
| `xǁ_RealOpenDpBackendǁnumeric_measurement__mutmut_4` | `atom_domain(T=float, nan=False)` -> `nan=None` | `test_numeric_measurement_domain_rejects_nan_but_accepts_ordinary_floats` |
| `xǁ_RealOpenDpBackendǁnumeric_measurement__mutmut_6` | drops the `nan=False` kwarg (defaults to the same `nan=None` as mutmut_4) | same |
| `xǁ_RealOpenDpBackendǁnumeric_measurement__mutmut_7` | `nan=False` -> `nan=True` | same |
| `xǁ_RealOpenDpBackendǁnumeric_measurement__mutmut_10` | `numeric_bins = len(interior_edges) + 1` -> `- 1` | `test_numeric_measurement_output_length_matches_edges_plus_one_with_no_overflow_bin` |
| `xǁ_RealOpenDpBackendǁnumeric_measurement__mutmut_11` | same line -> `+ 2` | same |
| `xǁ_RealOpenDpBackendǁnumeric_measurement__mutmut_24` | drops the `null_category=False` kwarg (defaults to OpenDP's own `True`, a real overflow bin) | same |
| `xǁ_RealOpenDpBackendǁnumeric_measurement__mutmut_27` | `null_category=False` -> `True` | same |
| `xǁ_RealOpenDpBackendǁnumeric_measurement__mutmut_37` | scale search `.map(1) <= eps_q` -> `.map(2) <= eps_q` | `TestRealOpenDpBackendCalibrationPrecision::test_numeric_measurement_certificate_equals_the_calibration_target_epsilon` |
| `xǁ_RealOpenDpBackendǁ_grouped_over_domain__mutmut_26` | scale search `.map(1)[0] <= eps_q` -> `.map(2)[0] <= eps_q` | `TestRealOpenDpBackendCalibrationPrecision::test_grouped_measurement_epsilon_component_equals_the_calibration_target` |
| `xǁ_RealOpenDpBackendǁ_grouped_over_domain__mutmut_44` | threshold search `<= delta_alloc` -> `< delta_alloc` | `test_grouped_over_domain_threshold_search_boundary_is_inclusive_of_the_lower_bound` |
| `xǁ_RealOpenDpBackendǁ_grouped_over_domain__mutmut_45` | `bounds=(1, _I32_MAX)` -> `bounds=(2, _I32_MAX)` | same |

## Regenerate

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut results
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut show <mutant-id>
```
