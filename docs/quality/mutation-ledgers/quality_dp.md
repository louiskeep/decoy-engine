# Mutation grading: `quality/dp.py` -- pure layer LOGIC-100%, mechanism on CI cert-gate

TQ crown-jewels pass, graded 2026-07-25. `dp.py` has two layers with different
gradeability:

- The **pure fail-closed request layer** (`DpError`, `_dp_versions`,
  `_validate_fit_params`, `_interior_edges`, `_check_derived_edges`,
  `_flag_token`, `_serialize_count`) reads only public request parameters and is
  gradeable in any shell. **Graded to LOGIC-100% here.**
- The **OpenDP mechanism** (`fit_dp_snapshot`, `_fit_dp_snapshot_with_backend`)
  only runs on the certified proof-stack: 47 of the 88 covering tests carry the
  `dp_certified` marker and SKIP off the certified 77-dist / CPython 3.10.20
  profile (`check_fit_environment` fails closed first). Its mutants are therefore
  UNCOVERED in an uncertified shell -- an environment limitation, not a test gap.
  **Deferred to the CI cert-gate**, where the gated tests execute.

## Numbers (uncertified shell)

441 mutants: **124 killed, 311 survived, 6 skipped.** The 311 survivors split by
layer:

- **18 pure-layer survivors, all EQUIVALENT** (message-prose-only + one
  `str(exc)` arg). After adding 3 direct tests this pass, 0 LOGIC mutants survive
  in the pure layer.
- **293 mechanism survivors, UNCOVERED** (`_fit_dp_snapshot_with_backend` 291 +
  `fit_dp_snapshot` 2): the `dp_certified` tests that exercise them skip here.
  Not classified as equivalent -- they are simply not reachable off the certified
  profile, and grade on the CI cert-gate.

## LOGIC killed this pass (pure layer)

3 new direct tests in `tests/unit/quality/test_dp.py` (call `_validate_fit_params`
/ `DpError` directly, no fit, no cert gate):

| Mutants | Mutation | Killed by |
|---|---|---|
| `_validate_fit_params` delta-except: `code=None` / `code="XX...XX"` / `code="DP_DELTA_INVALID"` / `message=` kwarg dropped (-> `TypeError`, `DpError` has no message default) | the `float(delta)` `TypeError`/`ValueError` branch -- the config tests only pass numeric bad values, so a non-numeric delta never reached it | `test_validate_fit_params_rejects_non_numeric_delta` (`delta="abc"`, asserts code `dp_delta_invalid`) |
| `_validate_fit_params` bins: `numeric_bins < 2` -> `<= 2` and `< 3` | both reject a legitimate two-bin fit | `test_validate_fit_params_accepts_the_minimum_two_bins` (asserts `_validate_fit_params(..., numeric_bins=2) is None`) |
| `DpError.__init__` `self.message = None` | nulls the surfaced `.message` attribute | `test_dp_error_exposes_code_and_message_attributes` (asserts `.code` and `.message`) |

## EQUIVALENT (18, pure layer)

### WORDING (17): error-message prose only
`_validate_fit_params` and `_check_derived_edges` raise coded `DpError`s whose
message literal is consumed only as the human `.message`; tests assert `.code`,
so `message=None`, `XX...XX` wrapping, and upper/lowercasing survive. (`message=
None` is a no-op because `message` accepts any value; the config tests assert the
code, not the prose.) Spread across `_validate_fit_params` (epsilon/delta/bins
messages) and `_check_derived_edges` (the derived-edge-degenerate message).

### STR-ARG (1): the `str(exc)` argument only
`DpError.__init__` `super().__init__(f"[{code}] {message}")` -> `super().__init__(
None)`. Changes only `str(exc)` / `exc.args[0]`; callers branch on `.code` and
read `.message` (both separately set and asserted), and no test inspects
`str(exc)`. Nothing observable in the tested surface changes. (Same shape as the
`ProvenanceError` str-arg survivor; see `quality_dp_provenance.md`.)

### CHAINING-METADATA (if emitted): `from exc` drop
A mutant that drops `from exc` on a raise (e.g. the delta-except branch) only
changes the exception's `__cause__` / `__suppress_context__`; no test reads
`__cause__` or `__context__`, so it is unobservable and equivalent. It is NOT
killed by `test_validate_fit_params_rejects_non_numeric_delta`, which asserts
`.code` (unchanged by dropping `from exc`).

## Mechanism grading (deferred to CI cert-gate)

To grade `fit_dp_snapshot` / `_fit_dp_snapshot_with_backend`, run on the certified
profile so the `dp_certified` tests execute:

```
uv run --frozen --extra dev --extra lint --extra vault python -m mutmut run
```

with `only_mutate=["src/decoy_engine/quality/dp.py"]` and test selection
`tests/unit/quality/test_dp.py` + `tests/property/test_dp_mechanisms_invariants.py`,
on the CI cert-gate job (where the pinned `895b9a20...` fingerprint reproduces).
The local `dev+lint+vault` sync no longer reproduces that fingerprint (the lock
has grown past the 77-dist profile the row was pinned from), and the
`dp_certified` gate has no test-side override by design, so the mechanism cannot
be graded locally without the CI-frozen lock state. Then classify + kill logic
survivors + extend this ledger.

## Regenerate (pure layer, any shell)
```
# pyproject [tool.mutmut]: only_mutate=["src/decoy_engine/quality/dp.py"],
# test selection = tests/unit/quality/test_dp.py + the mechanisms property file
rm -rf mutants && python -m mutmut run
```
