# DPS-CODEC implementation guide

Status: PLAN, revision 3 (remediated against Codex plan-review rounds 1-2, both
BLOCK; round 2 confirmed the direction and closed flag/v3-v4/matrix-seeds, and
returned 6 specification-completeness changes). Author: Opus. Cycle: DPS-CODEC.

Expands ROADMAP §DPS item 5; grounded in the current code it modifies.

## 1. Goal, stated honestly

Replace the scattered, value-level pandas type inference in the DP fit with **one
canonical typed-carrier layer converted through a single total, versioned codec**,
pinned to an exact, tested dependency set.

**What this achieves (round-1/2 BLOCKER correction).** Typed carriers make
`CarrierTable -> OpenDP vector` stability-1 by construction. They do NOT by
themselves make `DataFrame -> CarrierTable` stability-1. So:

- **Core claim** is over a *canonical* `CarrierTable` (invariants in §3.1) and is
  pandas-free. A caller supplying a canonical `CarrierTable` gets DP with no pandas
  in the theorem.
- **End-to-end DataFrame claim** additionally depends on the pandas adapter being a
  **total, boxing-invariant stability-1 transformation**. The adapter lives in the
  engine and INSIDE the end-to-end theorem (outside the OpenDP mechanism core, not
  outside the proof). The win: the adapter is ONE certified codec, property-tested,
  in the crown-jewel invariant, gated to an exact dependency set, replacing per-type
  inference defended example-by-example across an unbounded version range.

Both claims inherit the **documented residual exclusions** carried forward from
`what-we-cannot-prove.md` (see §3.7): cell dunders raising `KeyboardInterrupt`/
`SystemExit`, live/executable-object cells and `Series`-subclass containers
(fail-loud, out of domain), and the single-threaded warning-suppression
precondition. The claim is total over DATA VALUES, not over arbitrary embedded
executable objects. "Formally specified total" means total-over-data with these
exclusions, not unconditional.

Non-goal: changing any privacy MECHANISM. The releases stay exactly as landed:
numeric = binned histogram count (mean is `None`), categorical = grouped
thresholded count + non-null total. No mean mechanism is added (round-2 HIGH-4).

## 2. Why (the bug class)

OpenDP proves (epsilon, delta) over its vector with stability 1. Equivalence to a
row-level claim needs: add/remove one row => at most one changed vector element,
others equal under OpenDP's atomic equality. pandas derives storage from ALL rows,
so one added row can rebox every value (int+None->float; Arrow list->object of
ndarrays; **bool->complex128 when a `1j` is appended**), breaking distance-1 while
`map(1)` is unchanged.

## 3. Design

### 3.1 CarrierTable: physical shape AND canonical invariants (round-2 BLOCKER-1)

Owned by a new module `quality/carriers.py` (public import path
`decoy_engine.quality.carriers`). Physical shape:

```
CarrierTable:
    row_count: int                      # authoritative N for the row-count release
    columns: dict[name, Column]
Column := NumberColumn(values: float64 ndarray, validity: bool ndarray)
        | FlagColumn(values:   bool  ndarray, validity: bool ndarray)
        | TextColumn(values:  tuple[str,...], validity: bool ndarray)
```

**Canonical invariants** (a `CarrierTable` is "valid" only if ALL hold; enforced by
`sanitize_carrier_table()` which runs on EVERY input, including a directly-supplied
`CarrierTable` that bypasses the adapter — round-2 probe: OpenDP assumes domain
membership rather than enforcing it, so a valid-marked NaN reached `make_find_bin`,
a NUL text truncated, a surrogate raised):

- `row_count >= 0`; `columns` keys exactly equal the `column_schema` keys.
- every column is 1-D, exact dtype per its type, `len == row_count`, `validity` is
  1-D bool `len == row_count`.
- for `validity[i] == True`: NumberColumn value is finite-or-clamped, non-NaN,
  binary64-canonical, signed-zero-normalized (`-0.0 -> 0.0`); FlagColumn value is a
  real `numpy.bool_`; TextColumn value is `str`, UTF-8-encodable, no embedded NUL,
  no lone surrogate.
- `validity[i] == False` positions are dropped before the FFI call; only valid
  cells enter the OpenDP vector.
- **Adjacency** is defined over `(value, validity)` rows: add/remove one row changes
  at most one vector element (a valid row adds/removes one element; an invalid row
  changes none), and changes `row_count` by exactly one.

`sanitize_carrier_table()` re-applies the per-carrier FFI-safety checks (§3.2) to
direct carriers so the direct path cannot smuggle a NaN/NUL/surrogate past OpenDP.

### 3.2 Carrier codecs (closed set, executable totality)

Each codec is total over a single pandas cell, output invariant under every
reboxing. "Total" = the whole path **fetch -> container/temporal reject -> null
detect -> decode -> validity -> FFI-safety**, subject to the §3.7 exclusions.

- **`number`**: reject containers and temporal BEFORE unboxing (`datetime64[ns].item()`
  / `timedelta64[ns].item()` return ints; see `dp_normalize.py:109`). Zero-imaginary
  complex -> real; nonzero imaginary -> invalid. NaN -> invalid; +/-inf clamps to the
  bound; finite out-of-domain clamps into `[lower, upper]`; binary64 image;
  signed-zero-normalized. Equality is OpenDP atomic f64 equality, not byte-identity.
- **`flag`**: `bool`, exact int/float `0`/`1` reboxings, AND zero-imaginary complex
  whose real is exactly 0 or 1 (round-1 BLOCKER, probe-confirmed the codec collapses
  the bool->complex128 rebox). Nonzero imaginary / any other -> invalid.
  Container/temporal reject before the equality check.
- **`text`**: Unicode only, verbatim (never `str()`). Reject embedded NUL (OpenDP
  truncates; `dp_normalize.py:574`) and lone surrogates (`UnicodeEncodeError`). A
  `text` column stored as `int64` -> all-invalid, NOT stringified.

Datetime/timedelta: invalid (null) for v1, rejected before unboxing.

### 3.3 Allowed release-kind x carrier pairs (closed table)

| kind        | allowed carrier(s) | grouped mechanism         | total mechanism            |
|-------------|--------------------|---------------------------|----------------------------|
| numeric     | `number`           | binned histogram count    | (n/a; row-count only)      |
| categorical | `text`, `flag`     | `make_count_by(T)`        | `make_count(T)` non-null   |

`categorical + number` REJECTED for v1 (OpenDP has no float `make_count_by`; probe
confirmed). Schema validation fails closed on any unlisted pair. `T` is `str` for
`text`, `bool` for `flag`.

### 3.4 The categorical `flag` path, end-to-end (round-2 BLOCKER-2)

A categorical release is TWO measurements (`dp_budget.release_categorical`): grouped
count and non-null total. The current non-null-total `count_measurement` uses
`atom_domain(T=str)` (`dp_budget.py:204`), which fails on bools ("inferred bool,
expected String"). So for `flag`:

- Add a `carrier` field to `CategoricalQuerySpec` (`dp_schedule.py:42`, built at
  `dp.py:436`); it enters the schedule and the cache signature (§4).
- Both measurements get a **bool-domain** variant: grouped `make_count_by(bool)`
  (probe-confirmed) and a bool-domain non-null `make_count(bool)`. `str` carrier keeps
  the existing str-domain pair. Selected by the column's carrier.
- **Artifact encoding:** flag `top_values` keys serialize deterministically as the
  canonical tokens `"true"`/`"false"` (not Python `str(bool)` `"True"/"False"`, not
  `"0"/"1"`); record `carrier: "flag"` on the column so verification and generation
  know the domain.
- **Generation decoding (`_sample._categorical_tables`, `:156`):** today it does
  `str(value)` and appends `OTHER_TOKEN` when `other_mode == "emit"`. For a `flag`
  column: decode the `"true"/"false"` tokens back to the flag domain (emit booleans or
  the canonical token per the column's output dtype). `other_mode == "emit"` is
  FORBIDDEN for `flag` columns (the `__other__` token is not a flag value); flag
  columns use `other_mode == "drop"` only. Enforced at schema validation.
- Tests: empty and all-invalid flag vectors; a flag column whose grouped+total both
  release; the `other_mode` rejection.

### 3.5 The fit API

```
fit_dp_snapshot(
    source,                       # DataFrame (uses the adapter) OR a CarrierTable
    column_schema: {name: {kind, carrier, bounds?}},   # per-column only
    epsilon: float,               # requested ceiling, strictly > 0
    delta: float,                 # requested ceiling, strictly in (0, 1)
    numeric_bins: int,            # ONE fit-level value for v1 (round-2 HIGH-4)
    ...
)
```

`epsilon`/`delta`/`numeric_bins` are fit-level; the schema holds per-column
`kind`/`carrier`/`bounds` only. Replaces `categorical_columns`/`numeric_domains`/
`delta`. `numeric_bins` stays a single fit-level value (matches the current
artifact/verifier); no per-column bins in v1.

### 3.6 The pandas adapter (in-engine, in the end-to-end claim)

One shared adapter `DataFrame + column_schema -> CarrierTable` in `quality/carriers.py`,
inside the end-to-end claim. Owns the guarded per-cell fetch (keep the positional-read
guard from `_cells`, `dp_normalize.py:295`, for Arrow cells that raise on fetch), null
detection, validity construction, then the per-carrier codec. Its output goes through
`sanitize_carrier_table()` like any carrier. It is what the crown-jewel property test
certifies.

### 3.7 Totality exclusions carried forward (round-2 BLOCKER-1)

The claim is total over DATA VALUES with exactly the residual already documented in
`what-we-cannot-prove.md` (do not silently strengthen it):

- Cells whose `__str__`/`__float__` raise `KeyboardInterrupt`/`SystemExit` are
  re-raised (an operator must be able to Ctrl-C a fit); such a cell can terminate a
  fit where its neighbour succeeded. Out of the adjacency domain.
- Live/executable-object cells and `Series`-subclass containers whose `array`/length
  depend on their rows: fail loud (raise), not silently release a near-null column.
  Out of domain.
- Single-threaded precondition: warning suppression uses process-global
  `catch_warnings()`+`simplefilter("ignore")`; a concurrent `simplefilter("error")`
  reopens the fit-success channel. Documented, not defended.

§§1/3.5/10 and the revised `what-we-cannot-prove.md` state these explicitly.

### 3.8 Dependency gate: exact certified manifest (round-2 HIGH-3)

A STATIC certified manifest of exact tuples `(python_minor, pandas, numpy, pyarrow)`.
**v1 certified rows** (adopt from the current lock; confirm exact patch versions
against `uv.lock` at build phase 3):

```
(3.10, pandas 2.3.3, numpy 2.2.6, pyarrow 24.0.0)
(3.11, pandas 2.3.3, numpy 2.4.6, pyarrow 24.0.0)
(3.12, pandas 2.3.3, numpy 2.5.0, pyarrow 24.0.0)
```

- **Fit time:** check the local tuple against the manifest BEFORE reading private
  data; fail closed on an uncertified stack.
- **Generation time:** check the artifact's RECORDED fit tuple + codec id/version
  against the static manifest; do NOT require equality to the generation machine's
  installed pandas/numpy/pyarrow (generation does not invoke them) NOR to
  OpenDP/`dp_accounting` (generation invokes neither) — this fixes the current
  over-strict compare at `_checks_dp.py:277`.
- The manifest distinguishes **direct-carrier certification** from **pandas-adapter
  certification**; the artifact records which boundary produced the vector (a
  direct-carrier fit does not need a certified pandas/pyarrow, only a certified
  Python+numpy for the float64 carrier).
- CI dependency-matrix workflow runs the crown-jewel adjacency property on EVERY
  manifest row.
- Recording versions is audit evidence, NOT authentication (the MAC is ROADMAP item 4
  / schema v4).

### 3.9 Artifact schema (round-1 HIGH: v3/v4)

Codec metadata = `dps-marginal/v3`; artifact-auth MAC = `dps-marginal/v4` (update the
ROADMAP MAC entry to v4 during build). v3 adds `column_schema`, per-column `carrier`,
codec id/version, the recorded fit tuple, and the source-boundary flag. Pre-GA hard
break, no shim.

## 4. Fold in the comprehensive-review findings (modules KEPT)

- **Shared-mutable policy dict (Medium).** Fresh deep copy per artifact; module value
  immutable. (Confirmed correctly specified.)
- **Budget calibration (Medium).** Check BOTH `binary_search` endpoints; treat
  lower-endpoint-satisfies as feasible (`dp_budget.py:215`). Supported epsilon range:
  at build, run the acceptance probe — binary-search the epsilon at which the
  installed PLD's exponential overflows, set the supported ceiling below it, and
  surface a coded `dp_epsilon_unsupported` error above it (not a raw `OverflowError`).
- **Zero-epsilon (Medium).** Accept composed `epsilon_total >= 0` in
  `ReleaseLedger.charge` and snapshot verification; requested ceilings stay strictly
  positive. (Probe-confirmed `eps=1, delta=0.9` -> legitimate `epsilon_total=0`.)
- **Allocation cost (Medium) — frozen cache key (round-2 MEDIUM-6).** Cache ONLY
  scalar calibration/allocation results (never a measurement or certificate; the
  certificate must be read from the object actually invoked). Frozen key =
  `(carrier-bearing schedule signature, edges/bins, epsilon, delta, exact OpenDP
  version, exact dp_accounting version, backend cache namespace)`. Production backend
  uses a stable namespace; fake/stateful test backends bypass the cache or use an
  instance-unique token.
- **Private-seam wording (Low).** Reword `_fit_dp_snapshot_with_backend` as an
  unsupported internal seam.

## 5. Affected surface (round-2 HIGH-5, completed)

`quality/dp_normalize.py` (removed), `dp_budget.py`, `dp_ledger.py`, `dp_policy.py`,
`dp.py`, and:

- `quality/carriers.py` (NEW — `CarrierTable`, columns, codecs, adapter,
  `sanitize_carrier_table`; the public import path).
- `quality/dp_schedule.py` (add `carrier` to `CategoricalQuerySpec`; carrier enters
  the schedule + cache signature).
- `quality/snapshot.py` (schema owner, `:78`).
- `plan/_checks_dp.py` (verification + query-count reconstruction `:305`; fix the
  generation-time version compare `:277`).
- `generation/statistical/_sample.py` (flag artifact decoding + `other_mode`).
- `generation/statistical/_spec.py` (DP-exemption wording `:184`).
- `config/_global_settings.py` (generate-side config docs `:34`).
- The dependency-matrix CI workflow + the static certified manifest file.
- Plan serialization / compat fixtures; `test_dp_claim_copy.py`; CHANGELOG; and
  `docs/what-we-cannot-prove.md` (guarantee wording — this GATES).

DPS stays explicitly unshipped in the claim until the CLI + platform callers and the
revised claim are complete.

## 6. Replaced vs kept

Replaced: `dp_normalize.py` + the fit-API surface. Kept (adapted): `dp_budget`,
`dp_ledger`, `dp_schedule`, `dp.py`, the OpenDP chains, release-ID/artifact machinery.

## 7. Test strategy (TQ discipline, invariant-first)

- **Crown-jewel invariant, landed FIRST:** for the full `DataFrame -> CarrierTable ->
  OpenDP vector` path AND the direct-`CarrierTable` path, every add/remove-one-row
  neighbour has multiset distance <= 1 **including the row-count projection**, no
  raise, no warning (subject to §3.7 exclusions), across EVERY certified manifest
  tuple. Hard merge gate.
- **Property tests (Hypothesis)** with a defined strategy generating values + their
  reboxings (bool/int/float/complex widening; list/ndarray; nullable Boolean;
  NUL/surrogate text; temporal). Assert decode totality + boxing invariance.
- **Preserve the current matrix's known examples as regression SEEDS** (do not delete
  `dp_normalize`'s matrix until the property suite proves equal strength): complex
  widening, Arrow temporal fetch errors, list->ndarray, nullable Boolean,
  NUL/surrogate text, hostile dunders, and both list-reconstruction and `pd.concat`
  paths.
- **Non-vacuous coverage guard** (current suite `test_dp.py:688`): every carrier/
  reboxing produces a real dtype-differing comparison.
- Flag-path tests (§3.4). Sanitizer tests: direct carrier with NaN/NUL/surrogate is
  rejected before FFI.

## 8. Build order

1. `quality/carriers.py`: `CarrierTable` + invariants + `sanitize_carrier_table`;
   crown-jewel adjacency invariant (both paths, row-count projection) landed FIRST;
   the three codecs with the property strategy + regression seeds.
2. Adapter + `column_schema` API + closed kind x carrier validation + `other_mode`
   flag rule.
3. Certified manifest + fit/generation gates + CI matrix.
4. `dp_schedule` carrier field; `dp_budget` bool-domain measurements + endpoints +
   eps-range probe + frozen cache key; `dp_ledger` `>= 0`.
5. Orchestration + artifact `dps-marginal/v3` (carrier metadata + recorded tuple +
   source boundary); `dp.py`, `snapshot.py`, `_checks_dp.py`.
6. Flag artifact/generation semantics in `_sample.py`.
7. Remove `dp_normalize.py` once the property suite subsumes its matrix.
8. Docs: `_spec.py`/`_global_settings.py`, claim-copy tests, CHANGELOG,
   `what-we-cannot-prove.md` (carrier + certified-adapter claim + §3.7 exclusions).
9. CLI + platform wiring against `column_schema` (ROADMAP item 2, next cycle).

Gates per phase: dennis (Opus) then Codex; Opus may take the hardest carrier/adapter/
orchestration build given DPS difficulty.

## 9. Resolved open questions

1. Datetime/timedelta null-only, reject before unboxing. 2. Exact dependency tuples
incl Python minor, every tuple tested (§3.8). 3. `number`/`flag`/`text` sufficient for
v1 after the flag fix + closed pair table + full flag path (§3.4); no fixed-vocabulary
categorical now. 4. Accept `epsilon_total >= 0`; requested ceilings strictly positive.
5. Adapter in-engine, in the end-to-end claim.

## 10. One decision to surface to Cam (customer-facing, non-blocking)

What we advertise:

- **(default, built here)** Keep the end-to-end DataFrame-row DP claim with the
  adapter certified as a stability-1 transformation under the exact-version gate,
  carrying the §3.7 residual exclusions. Most customer-friendly, strictly stronger
  than today, pandas mitigated to one gated tested component.
- **(stricter)** State DP only over a canonical `CarrierTable`; DataFrame fits are
  labeled convenience and cannot advertise row-level DP unless the caller supplies a
  canonical carrier.

Building the default; the carrier-level claim is the clean core, so we can tighten
later without a rebuild. Flag if you want the stricter public claim from v1.
