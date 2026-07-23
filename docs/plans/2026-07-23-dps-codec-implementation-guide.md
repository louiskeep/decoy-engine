# DPS-CODEC implementation guide

Status: PLAN, revision 2 (remediated against Codex plan-review round 1, which
returned BLOCK — 2 BLOCKER, several HIGH, 2 MEDIUM). Author: Opus. Cycle:
DPS-CODEC, the next dev cycle after the marginal-DP mechanism landed on main
(`c9914b9`).

This guide expands ROADMAP §DPS item 5 into a build-ready plan and folds in both
the 2026-07-23 comprehensive Codex review of the current code and the round-1
plan-review of revision 1 of this guide.

## 1. Goal, stated honestly

Replace the scattered, value-level pandas type inference in the DP fit with **one
canonical typed-carrier layer converted through a single total, versioned codec**,
and pin the whole DataFrame→vector transformation to an exact, tested dependency
set.

**What this does and does NOT achieve (round-1 BLOCKER correction).** Typed
carriers prove `CarrierTable → OpenDP vector` is stability-1 by construction. They
do NOT by themselves prove `DataFrame → CarrierTable` is stability-1 — that adapter
still owns column access, per-cell fetch, null detection, warning suppression,
scalar decoding, and vector construction, which is where most of the current
defects live. So "take pandas out of the formal boundary" precisely means:

- The **core claim** is stated over the `CarrierTable` and is pandas-free. A caller
  that supplies a valid `CarrierTable` gets DP with no pandas in the theorem.
- The **end-to-end DataFrame claim** (what `fit_dp_snapshot` advertises today, over
  rows of the DataFrame) additionally depends on the pandas adapter being a
  **formally specified, total, boxing-invariant stability-1 transformation**. The
  win is that this adapter is now ONE certified codec — property-tested, in the
  crown-jewel invariant, and gated to an exact dependency set — instead of
  per-type inference defended example-by-example across an unbounded version range.

The adapter therefore lives **in the engine and in the end-to-end proof** (it is
outside the OpenDP mechanism core, not outside the theorem). This is the round-1
Q5 resolution.

Non-goal: changing any privacy mechanism. OpenDP mechanisms and `dp_accounting`
composition are unchanged and were confirmed sound (no under-accounting).

## 2. Why (the formal problem)

OpenDP proves (epsilon, delta) over its input vector with declared stability 1: one
changed vector element perturbs the output by at most one unit. Equivalence to a
row-level claim needs: add/remove one row => at most one changed vector element,
all others equal under OpenDP's atomic equality. pandas derives storage from ALL
rows, so one added row can rebox every value (int+None→float; Arrow list→object of
ndarrays; **bool→complex128 when a `1j` is appended**), turning "add one row" into
many changed elements while `map(1)` is unchanged. That is the bug class.

## 3. Design

### 3.1 CarrierTable (explicit, round-1 HIGH)

The carrier boundary needs a concrete representation that preserves row count and
null positions (a null-excluded numpy array loses both; the row-count release comes
from `len(frame)` today at `dp.py:444`):

```
CarrierTable:
    row_count: int                      # authoritative N for the row-count release
    columns: dict[name, Column]         # every column length == row_count
Column is one of:
    NumberColumn(values: float64 ndarray, validity: bool ndarray)
    FlagColumn(values: bool ndarray,      validity: bool ndarray)
    TextColumn(values: tuple[str, ...],   validity: bool ndarray)
```

`validity[i] == False` marks a null/non-conforming cell (excluded from the release
like today). `row_count` is released via the existing row-count mechanism; per
column, only valid cells enter the OpenDP vector.

### 3.2 Carrier codecs (closed set, executable totality)

Three carriers. Each codec is a **total function over a single pandas cell** whose
output is invariant under every reboxing pandas can apply to the same value. "Total"
is defined executably as the whole path: **fetch → container/temporal reject → null
detect → decode → validity → FFI-safety**, never raising or warning on any cell.

- **`number`**: reject containers and temporal values BEFORE unboxing (round-1 HIGH:
  `datetime64[ns].item()` / `timedelta64[ns].item()` return plain ints, so a
  post-unbox check is too late — see current `dp_normalize.py:109`). Zero-imaginary
  complex → real component; nonzero imaginary → invalid. NaN → invalid; ±inf clamps
  to the declared bound; finite out-of-domain clamps into `[lower, upper]`.
  Canonicalize through the binary64 image and **normalize signed zero** (`-0.0 →
  0.0`) — equality is OpenDP's atomic f64 equality, NOT byte-identity (round-1 HIGH:
  `-0.0 == 0.0` but their bytes differ).
- **`flag`**: booleans, the exact int/float `0`/`1` reboxings, AND **zero-imaginary
  complex whose real part is exactly 0 or 1** (round-1 BLOCKER: appending one `1j`
  reboxes every bool as complex128; a bool/int/float-only codec gives distance 4).
  Nonzero imaginary, any other value → invalid. Container/temporal reject before the
  equality check.
- **`text`**: Unicode only, kept verbatim (never generic `str()`). **Reject embedded
  NUL** (round-1 HIGH: OpenDP truncates a string at NUL — current code rejects it at
  `dp_normalize.py:574`) and **reject lone surrogates** (probe: `UnicodeEncodeError`).
  A `text` column stored as `int64` → all-invalid, NOT stringified (`int64` cannot
  distinguish `"7"` from `"07"` from numeric `7`).

Datetime/timedelta: null-only (invalid) for v1, rejected before unboxing.

### 3.3 Allowed release-kind × carrier pairs (closed table, round-1 HIGH)

`kind × carrier` is NOT a free product. OpenDP 0.15.1 `make_count_by` supports
unknown-key counting for `str` and `bool` but NOT `float` (probe: float constructor
fails; numeric vs categorical domains already differ at `dp_budget.py:227` / `:251`).
So:

| kind        | allowed carrier(s) | mechanism                          |
|-------------|--------------------|------------------------------------|
| numeric     | `number`           | binned count / clamped-mean chain  |
| categorical | `text`, `flag`     | `make_count_by` (str / bool keys)  |

`categorical + number` is REJECTED at schema validation for v1 (it would need a
canonical float→token string encoding + post-release decode; deferred). Schema
validation fails closed on any unlisted pair.

### 3.4 The fit API (round-1 HIGH: state where every arg lives)

```
fit_dp_snapshot(
    source,                       # DataFrame (uses the adapter) OR a CarrierTable
    column_schema: {name: {kind, carrier, bounds?, numeric_bins?}},
    epsilon: float,               # requested ceiling, strictly > 0
    delta: float,                 # requested ceiling, strictly in (0, 1)
    ...
)
```

`epsilon`/`delta`/`numeric_bins` are fit-level (or per-column for bins); the schema
holds per-column `kind`/`carrier`/`bounds`. The schema does NOT carry pandas/numpy
dtype names, caller stringifiers, inferred nullability/categories, per-column raise
behaviour, or exact invalid counts. This replaces the parallel
`categorical_columns`/`numeric_domains`/`delta` arguments.

### 3.5 The pandas adapter (in-engine, in the end-to-end claim)

One shared adapter `DataFrame + column_schema → CarrierTable`, in the engine so CLI,
platform, and tests use the same implementation, and **inside the end-to-end
stability claim**. It owns the guarded per-cell fetch (keep the positional-read
guard pattern from `_cells`, `dp_normalize.py:295`, for Arrow-backed cells that raise
on fetch), null detection, and validity construction, then applies the per-carrier
codec. It is the transformation the crown-jewel property test certifies.

### 3.6 Dependency gate: exact-tuple allowlist (round-1 HIGH)

Testing only min/max of a version range does not certify intermediate releases and
boxing is not monotonic under semver. For v1:

- Gate on **exact certified tuples** of `(python_minor, pandas, numpy, pyarrow)` —
  not ranges. Python 3.10–3.12 are supported and scalar unboxing is part of the path,
  so Python minor is in the tuple.
- The dependency-matrix CI job runs the adjacency property on **every** certified
  tuple, not just endpoints.
- The gate runs **before any private value is touched**, and fails closed (clear
  refusal) on an uncertified stack.
- Record the fit stack (codec id + version, python/pandas/numpy/pyarrow/OpenDP/
  `dp_accounting` versions) in the artifact. Verification checks the recorded tuple +
  codec are recognized-certified. Do NOT require the generation machine's pandas/
  pyarrow to match when those libraries are not used in generation.
- **Recording versions is audit evidence, not authentication** (round-1 HIGH). Until
  the artifact-auth MAC lands (ROADMAP item 4, separate cycle), a forged artifact can
  also forge its codec/version fields. The gate closes accidental drift, not forgery.

### 3.7 Artifact schema versioning (round-1 HIGH: v3 collision)

The MAC work already claims `dps-marginal/v3` (ROADMAP item 4). To avoid a double
allocation: **codec metadata = `dps-marginal/v3`, artifact-auth MAC = `dps-marginal/v4`**
(sequential, they are separate cycles). Update the ROADMAP MAC entry to say v4. Add
`column_schema`, codec version, and the recorded dependency tuple to v3. Pre-GA hard
break, no back-compat shim.

## 4. Fold in the comprehensive-review findings (modules the codec KEEPS)

In `dp_budget` / `dp_ledger` / `dp_policy` / `dp.py`, all retained:

- **Shared-mutable policy dict (Medium).** Emit a fresh deep copy per artifact; keep
  the module value immutable. (Fix confirmed correctly specified by plan-review.)
- **Budget calibration (Medium).** Check BOTH `binary_search` endpoints; treat
  lower-endpoint-satisfies as feasible (current searches raise here,
  `dp_budget.py:215`). Define a **concrete numerically-supported epsilon range with a
  coded error** (not a raw `OverflowError`); pick the numbers during build against
  the installed PLD.
- **Zero-epsilon (Medium).** Accept composed `epsilon_total >= 0` downstream in
  `ReleaseLedger.charge` and snapshot verification; keep REQUESTED fit/generate
  budget ceilings strictly positive. (Probe confirmed `eps=1, delta=0.9` yields a
  legitimate `epsilon_total=0` the ledger currently rejects.)
- **Allocation cost (Medium) — with a safety spec (round-1 MEDIUM).** Cache
  calibration/allocation RESULTS, do NOT substitute a representative certificate for
  the exact measurement object (the invariant is that the certificate is read from
  the object actually invoked). Cache key MUST include: exact OpenDP + `dp_accounting`
  versions, backend identity (so fake test backends never share production entries),
  the full public schedule signature + budget, and any mechanism-shape field the
  certified map depends on.
- **Private-seam wording (Low).** Reword `_fit_dp_snapshot_with_backend` as an
  unsupported internal seam. (Confirmed correctly specified.)

## 5. Affected surface (round-1 HIGH: the complete list)

Beyond `dp_normalize`/`dp_budget`/`dp_ledger`/`dp_policy`/`dp.py`:

- `quality/snapshot.py` (schema owner, `:78`)
- `plan/_checks_dp.py` (artifact verification + query-count reconstruction, `:305`)
- `generation/statistical/_spec.py` (statistical-spec DP exemption wording, `:184`)
- `config/_global_settings.py` (generate-side config docs, `:34`)
- Plan serialization / compatibility fixtures; `test_dp_claim_copy.py`; CHANGELOG;
  and `docs/what-we-cannot-prove.md` (the guarantee wording — this GATES).

DPS stays **explicitly unshipped** in the docs/claim until the CLI + platform callers
and the revised claim are complete.

## 6. What is replaced vs kept

- **Replaced:** `quality/dp_normalize.py` (scattered value-level inference) and the
  fit-API surface.
- **Kept (adapted):** `dp_budget`, `dp_ledger`, `dp_schedule`, `dp.py` orchestration,
  the OpenDP chains, and the release-ID / artifact machinery.

## 7. Test strategy (TQ discipline, invariant-first)

- **Crown-jewel invariant, landed FIRST:** for the full `DataFrame → CarrierTable →
  OpenDP vector` path (the end-to-end adapter, NOT only already-canonical carriers),
  every add/remove-one-row neighbour has multiset distance <= 1, no raise, no warning
  — run across EVERY certified dependency tuple. Hard merge gate.
- **Property-based codec tests (Hypothesis)** with a DEFINED strategy that generates
  values and their reboxings (bool↔int↔float↔complex widening; list/ndarray; nullable
  Boolean; NUL/surrogate text; temporal). Assert decode totality + boxing invariance.
- **Preserve the current matrix's known examples as regression SEEDS** (round-1
  MEDIUM: do not delete until the property suite proves equal strength) — complex
  widening, Arrow temporal fetch errors, list→ndarray, nullable Boolean, NUL/surrogate
  text, hostile dunders, and BOTH the list-reconstruction and `pd.concat` construction
  paths.
- **Carry the non-vacuous-comparison coverage guard** (current suite has it at
  `test_dp.py:688`): every declared carrier/reboxing must produce a real
  dtype-differing comparison, so no test is silently vacuous.
- Keep the mutation-checked totality-guard pattern for residual guards.

## 8. Build order (phasing)

1. **Invariant + CarrierTable + codecs.** Land the crown-jewel adjacency invariant
   over the adapter path; define `CarrierTable`; implement the three total codecs
   with the property strategy + preserved regression seeds.
2. **Adapter + `column_schema` API** with the closed kind×carrier table and the
   fail-closed schema validation.
3. **Dependency gate + exact-tuple allowlist + CI matrix**, gate before any value.
4. **Orchestration + artifact `dps-marginal/v3`** (carrier metadata + recorded stack);
   wire `dp.py`, `snapshot.py`, `_checks_dp.py`.
5. **Fold in the review findings** (policy-dict copy, budget endpoints + eps range +
   result-cache with the safe key, ledger `>= 0`, seam wording).
6. **Remove `dp_normalize.py`** once the property suite subsumes its matrix (seeds
   preserved).
7. Update `_spec.py`/`_global_settings.py` docs, claim-copy tests, CHANGELOG, and
   `what-we-cannot-prove.md` to the carrier + certified-adapter claim.
8. **CLI + platform wiring** against `column_schema` (ROADMAP item 2, next cycle).

Gates per phase: dennis (Opus) then Codex; Opus may take the hardest codec/adapter/
orchestration build given DPS difficulty.

## 9. Resolved open questions (from round-1)

1. Datetime/timedelta: null-only for v1, rejected before unboxing. RESOLVED.
2. Dependency certification: exact tuples incl Python minor, every tuple tested, not a
   min/max interval. RESOLVED (§3.6).
3. Carrier set: `number`/`flag`/`text` sufficient for v1 after the flag zero-imaginary
   fix (§3.2) and the closed kind×carrier table (§3.3). No fixed-vocabulary categorical
   now (different support mechanism + accounting shape). RESOLVED.
4. Zero-epsilon: accept composed `epsilon_total >= 0` downstream; requested ceilings
   strictly positive. RESOLVED (§4).
5. Adapter placement/claim: in-engine, shared, and INSIDE the end-to-end stability
   claim (outside the OpenDP core, not outside the proof). RESOLVED (§1, §3.5).

## 10. One decision to surface to Cam (customer-facing)

The round-1 BLOCKER forces an honest choice about what we advertise:

- **(default in this plan)** Keep the end-to-end DataFrame-row DP claim, with the
  adapter certified as a stability-1 transformation under the exact-version gate. Most
  customer-friendly ("declare a schema, hand us a DataFrame, get DP over your rows"),
  and strictly stronger than today because the adapter is one certified codec, but
  pandas remains in the end-to-end theorem (mitigated to a single gated, tested
  component).
- **(stricter)** State DP only over the `CarrierTable`; DataFrame fits via the adapter
  are labeled convenience and cannot advertise row-level DP unless the caller supplies
  carriers. Purest ("pandas fully out of the theorem"), but pushes carrier
  materialization onto callers and narrows the customer claim.

This plan builds the default and keeps the carrier-level claim available as the clean
core, so we can tighten to the stricter statement later without a rebuild. Flagging in
case Cam wants the stricter public claim from v1.
