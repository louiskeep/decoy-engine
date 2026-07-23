# DPS-CODEC implementation guide

Status: PLAN (awaiting Codex plan-review). Author: Opus. Cycle: DPS-CODEC, the
next dev cycle after the marginal-DP mechanism landed on main (`c9914b9`).

This guide is the expansion of ROADMAP §DPS item 5 (`decoy-platform/docs/ROADMAP.md`)
into a build-ready plan. It also folds in the fresh comprehensive Codex review of
2026-07-23 (ROADMAP §DPS item 5, "Comprehensive fresh-Codex review").

## 1. Goal

Replace the value-level pandas type inference in the DP fit with **declared typed
carriers converted through versioned, total codecs**, so that pandas moves OUT of
the formal DP boundary. This closes, structurally, the entire boxing/totality bug
class that produced 9+ adversarial review rounds on `quality/dp_normalize.py`.

Non-goal: changing any privacy mechanism. OpenDP mechanisms and `dp_accounting`
composition are unchanged and were confirmed sound (no under-accounting) by the
comprehensive review.

## 2. Why this design (the formal problem)

OpenDP proves its (epsilon, delta) guarantee over **its own input vector**, with a
declared stability of 1: one changed vector element perturbs the output by at most
one unit. Our documented guarantee is stated over **rows of the caller's
DataFrame**. The two are the same statement only if the conversion satisfies:

> add or remove one row  =>  at most one element of the vector changes, and every
> other element is byte-identical.

pandas derives a column's storage type from ALL of its rows, so one added row can
silently re-box every existing value (int+None -> float; Arrow `list` -> object of
ndarrays; etc.). That turns "add one row" into "many changed vector elements",
which breaks distance-1 while OpenDP's `map(1)` certificate is unchanged. That is
the bug class. Defending it with a hand-built test matrix is load-bearing on the
matrix and volatile across pandas/numpy/pyarrow versions (comprehensive review,
High).

The fix is to define adjacency over **canonical typed carriers** that have exactly
one representation per value, so "add one row -> one new element, others unchanged"
holds by construction. pandas becomes a convenience adapter that lives OUTSIDE the
formal claim.

## 3. Design

### 3.1 Carrier types (closed set for v1)

Three carriers. Each has a TOTAL, boxing-invariant decode: for the reboxing
relation `x ~ y` (pandas can store the same value as either), `decode_T(x) ==
decode_T(y)`, and any non-conforming value maps to `null`. Nulls are total, cannot
create a fit-success channel, and match existing missing-value semantics.

- **`number`**: canonicalized through the binary64 image. Zero-imaginary complex
  maps to its real component; a nonzero imaginary part is non-conforming -> null.
  NaN -> null; +/-inf policy fixed (clamp to the declared bound, as today).
  Out-of-domain finite values clamp into `[lower, upper]`.
- **`flag`**: booleans plus the exact `0`/`1` reboxings (int, float, numpy). Any
  other value -> null. (A naive "accept only Python `bool`" codec is NOT enough --
  pandas reboxes booleans as `1`/`0`; the codec must accept the exact 0/1 image.)
- **`text`**: Unicode only, kept verbatim (never generic `str()`). A `text` column
  stored as `int64` goes to **null, not stringified**: an `int64` cell cannot
  distinguish source text `"7"` from `"07"` from a numeric `7`, and `str()`
  recreates the current instability. Non-UTF-8 / non-Unicode -> null.

Carrier for datetime/timedelta: **null (unsupported) for v1** (matches today).
Open question in section 8 on whether to add an epoch-int carrier later.

### 3.2 The `column_schema` API (the fit-API break)

Replace the parallel `categorical_columns` / `numeric_domains` / `delta` arguments
with one `column_schema` mapping:

```
column_schema = {
    "<column>": {
        "kind": "categorical" | "numeric",   # release kind
        "carrier": "text" | "flag" | "number",
        "bounds": (lower, upper),              # numeric only
    },
    ...
}
```

It records: release kind, categorical carrier, numeric bounds, the fixed
adjacency, and the codec version. It must NOT record: pandas/numpy dtype names,
per-column raise behaviour, caller-supplied stringifiers, inferred nullability or
categories, or exact invalid-value counts (each of those is a channel or a
dtype-coupling we are removing).

Since the fit-API break is already accepted, the unified schema is the cleaner
final shape rather than another additive argument.

### 3.3 Carrier-first conversion

- `number` carriers enter OpenDP as contiguous **float64 numpy arrays** (OpenDP
  0.15.1 accepts these directly). `flag`/`text` enter as validated UTF-8 string
  lists / boolean vectors per the mechanism.
- A **pandas adapter**, explicitly outside the formal DP claim, converts a
  DataFrame under a declared `column_schema` to carriers; non-conforming values
  become null. A caller that already holds carriers can skip pandas entirely.
- Note from the review's probe: OpenDP 0.15.1 does NOT accept `pyarrow.Array`
  directly (`UnknownTypeException`), so numpy float64 is the numeric carrier and
  there is still one final, total conversion from the canonical carrier into
  OpenDP's exact input -- but from a canonical typed source, not from pandas
  inference.

### 3.4 Dependency-version gate + artifact recording (review High)

The guarantee still touches numpy/pandas/pyarrow at the adapter edge, so:

- **Fail closed** outside the certified pandas/numpy/pyarrow versions -- the exact
  matrix the codecs are property-tested against. Outside it, refuse the fit with a
  clear message rather than silently running.
- **Record in the artifact**: codec ID + version, and the pandas/numpy/pyarrow +
  OpenDP + `dp_accounting` versions. Today only OpenDP/`dp_accounting` are
  recorded; the versions that actually determine the vector are not.
- **CI runs a dependency-version matrix** (min and max of the certified range),
  running the adjacency property test on each.

This converts a silent, CI-passing guarantee break into a loud refusal, and is the
durable form of the fail-closed version gate.

### 3.5 Artifact schema -> `dps-marginal/v3`

Add `column_schema`, the codec version, and the dependency versions. Bump the
schema string to `dps-marginal/v3`. Pre-GA: hard break, no back-compat shim
(RELEASE_PHASE is pre-GA; `is_pre_ga()` gates).

## 4. Fold in the comprehensive-review findings (modules the codec KEEPS)

These live in `dp_budget` / `dp_ledger` / `dp_policy` / `dp.py`, all retained:

- **Shared-mutable policy dict (Medium).** `_DP_NORMALIZATION_POLICY` ships by
  reference; mutating one artifact's policy retroactively changes others and a
  later fit inherits it. Emit a fresh copy per artifact; keep the module value
  immutable (freeze / return a deep copy).
- **Budget calibration rejects valid permissive budgets (Medium).** OpenDP
  `binary_search` raises on a constant predicate, including when the lower endpoint
  already satisfies the target; `_certify_schedule` reads every such raise as
  "infeasible" and `_allocate_epsilon` assumes monotone feasibility. Check BOTH
  search endpoints explicitly; treat lower-endpoint-satisfies as feasible; document
  and enforce a numerically-supported epsilon range; handle the PLD `OverflowError`
  on very large epsilon explicitly instead of surfacing it raw.
- **Zero-epsilon rejected downstream (Medium).** `dp_accounting` legitimately
  returns `epsilon_total = 0` at permissive delta; the fit accepts and serializes
  it, but `ReleaseLedger.charge` and snapshot verification reject nonpositive
  epsilon. Pick one: accept `epsilon >= 0` downstream, or reject such requested
  budgets before fitting. (Recommend: accept `>= 0` downstream -- eps=0 with delta>0
  is a valid approximate-DP release.)
- **Allocation cost (Medium).** Session build runs ~80 full schedule
  certifications, each rebuilding/recalibrating every OpenDP chain (~27s for 5
  numeric + 2 categorical before any rows). Cache certification by public schedule
  shape + budget, or certify one representative per identical mechanism shape and
  replicate its certificate; release-time measurements are still individually
  constructed.
- **Private-seam wording (Low).** `_fit_dp_snapshot_with_backend` is described as
  unreachable; Python privates are importable. Reword as an unsupported internal
  seam, not a security boundary.

## 5. What is replaced vs kept

- **Replaced:** `quality/dp_normalize.py` (the pandas value-level codec and its
  hand-built adjacency matrix) and the fit-API surface.
- **Kept (adapted where noted):** `dp_budget` (composition), `dp_ledger`,
  `dp_schedule`, `dp.py` orchestration (adapted to the carrier API), the OpenDP
  chains, and the release-ID / artifact machinery.

## 6. Test strategy (TQ discipline, invariant-first)

Land the invariant test FIRST, before the codecs exist:

- **DP adjacency invariant (the crown jewel for this module):** for every carrier
  and every add/remove-one-row neighbour, multiset distance <= 1, no raise, no
  warning -- run across the certified dependency matrix. This is a hard merge gate.
- **Property-based codec tests (Hypothesis):** generate values and their reboxing
  relations; assert `decode` totality and boxing invariance
  (`decode_T(x) == decode_T(y)`).
- **Guard-structure mutation tests:** keep the mutation-checked totality-guard
  pattern for whatever residual guards the codecs need.
- **Dependency-matrix CI job:** the adjacency property across min/max pinned
  pandas/numpy/pyarrow.

The property tests over carriers replace the enumerated pandas boxing matrix in
`test_dp.py`; the point of the codec is that correctness is provable over the
carrier, not defended example-by-example.

## 7. Build order (phasing)

1. **Invariant + carriers.** Land the adjacency invariant test; implement the three
   total codecs (`number`/`flag`/`text`) with property-based adjacency tests.
2. **`column_schema` API + pandas adapter** (adapter outside the formal claim).
3. **Orchestration + artifact.** Wire `dp.py` to carriers; bump artifact to
   `dps-marginal/v3`; add the dependency-version gate + version recording.
4. **Fold in the review findings** (policy-dict copy, budget calibration + cache,
   ledger zero-epsilon, seam wording).
5. **Remove `dp_normalize.py`** and its matrix (superseded).
6. **CLI + platform wiring** against the `column_schema` API (this is the separate
   ROADMAP item 2, sequenced right after this cycle -- wire once).

Each phase lands its tests with it. Gates: dennis (Opus) then Codex per the loop
protocol; Opus may take the hardest codec/orchestration build directly given the
difficulty (the DPS Opus-builds exception).

## 8. Open questions for the plan-review

1. **Datetime/timedelta carrier:** null-only for v1 (matches today), or add an
   explicit epoch-int carrier now? (Lean: null-only for v1.)
2. **Certified dependency matrix bounds:** which pandas/numpy/pyarrow min+max to
   certify and gate on (pyproject currently allows pandas 1.5-2.x, pyarrow
   unbounded). Proposal: certify the current resolved versions as the floor+ceiling
   for v1 and widen deliberately later.
3. **Carrier set completeness:** are `number`/`flag`/`text` sufficient for v1, or
   add an explicit fixed-vocabulary categorical carrier (OpenDP `Enum`-style, public
   categories) now rather than later?
4. **Zero-epsilon disposition:** accept `epsilon >= 0` downstream vs reject
   pre-fit. (Lean: accept `>= 0`.)
5. **Adapter surface:** does the pandas adapter live in the engine (convenience,
   outside the claim) or only in the CLI/platform callers? (Lean: engine, clearly
   fenced as non-DP, so tests and callers share one adapter.)
