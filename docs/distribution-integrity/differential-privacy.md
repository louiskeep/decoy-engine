# Differential privacy internals

Status: current

[Back to the developer reference](index.md)

How `fit_dp_snapshot` produces a `dps-marginal/v3` release, how generation
verifies it, and why it only runs on a certified software stack.

The binding statement of what the guarantee covers is
[What Decoy does not prove](../what-we-cannot-prove.md). This page describes
the mechanism. If the two disagree, that page wins.

## Scope

- Approximate (epsilon, delta) differential privacy.
- Add-or-remove-one-row adjacency, over the rows of the frame handed to the
  fit.
- Single-column numeric and categorical marginals, plus the row count.
- Not covered: joints, correlation, `condition_on`, datetime, freetext, masked
  output, exact snapshots.

The artifact records the scope as the literal `"single-column-marginals"`, so a
later joint mechanism has a discriminator to branch on.

## Division of labour

| Party | Owns |
|---|---|
| OpenDP 0.15.1 | Every mechanism and its privacy map. Each query is one `Transformation >> Measurement`; its loss is read from `Measurement.map(1)` on the object actually invoked. |
| `dp_accounting` 0.6.0 | Composition. Each certified `(epsilon_i, delta_i)` becomes a dominating-pair privacy loss distribution; the composed total is read at the requested delta. A valid upper bound, not the tightest. |
| Decoy | The public query schedule, the typed carrier conversion, wiring certificates into the accountant, and asserting the total against the request. No epsilon, scale or threshold comes from a Decoy formula. |

`quality/dp_budget.py` is the only module that imports `opendp`. A sentry test
pins that (`tests/unit/quality/test_opendp_dependency.py`). It is what makes
"every release went through the mandated chain" checkable.

The `opendp[polars]` extra must never be installed. Its FFI layer pins one
exact Polars version, which conflicts with the engine's Polars range
(`docs/plans/2026-07-22-dps-scope-b-spike-result.md`).

## The fit, step by step

```python
fit_dp_snapshot(
    source,                 # pandas.DataFrame or CarrierTable
    column_schema,          # {name: {"carrier": "number"|"text"|"flag", "bounds": (lo, hi)}}
    *, epsilon, delta, numeric_bins=10,
)
```

1. **Environment gate.** `check_fit_environment()` refuses an uncertified
   platform or package set before any cell is read.
2. **Carrier conversion.** The frame is converted to a `CarrierTable`
   (`carrier_adapter.py`, `carriers.py`). The declared carrier decides what a
   cell releases, never the pandas storage dtype. The conversion is total: no
   cell value can make it raise.
3. **Schedule.** A fixed public schedule is built from the schema alone
   (`dp_schedule.py`):
   - one row-count query,
   - one query per `number` column,
   - two queries per `text` or `flag` column (grouped count, non-null total).

   So `query_count = 1 + numeric + 2 * categorical`.
4. **Allocation.** `OpenDpReleaseSession` searches for the largest uniform
   per-query epsilon whose composed total stays within the request. It is a
   bisection over certificates, a pure function of the schedule and the
   request, and touches no data. The scalar result is cached per
   `(schedule, epsilon, delta, library versions)`.
5. **Release.** Each scheduled query runs once. Unscheduled or duplicate
   releases are refused (`dp_unscheduled_release`, `dp_duplicate_release`).
6. **Compose and assert.** Certificates are composed and checked against the
   request.
7. **Serialize.** The artifact is written in snapshot shape with a `dp` block.

### Mechanism chains

| Release | Chain |
|---|---|
| Row count | `make_count >> then_laplace` |
| Numeric column | `make_find_bin >> then_count_by_categories >> then_laplace` over `numeric_bins` equal-width bins of the declared domain |
| Categorical grouped count | `make_count_by >> then_laplace_threshold` |
| Categorical non-null total | `make_count >> then_laplace` |

The thresholded categorical release only emits a label whose noisy count
clears a threshold, so the label set itself does not disclose rare values.
Labels that do not clear it are folded into `other_count`.

Numeric interior bin edges are derived from the declared `bounds` and
`numeric_bins`, never from data. Values outside the bounds are clamped.

### Fit parameters

| Parameter | Constraint |
|---|---|
| `epsilon` | Finite, greater than 0, at most 700 (`dp_epsilon_unsupported` above) |
| `delta` | Finite, in (0, 1). Rejected at 0. |
| `numeric_bins` | Integer, at least 2 |
| `bounds` | Required for `number`; finite, `lo < hi` |

### Carriers

| Carrier | Kind | Releases |
|---|---|---|
| `number` | numeric | A float64 value clamped to bounds. NaN and unconvertible cells are null. |
| `text` | categorical | Genuine `str` cells verbatim. Every non-string cell is null. |
| `flag` | categorical | Canonical `true` / `false` for cells that are exactly 0 or 1 in a non-text type. Text such as `"1"` is null. |

Deriving a label from pandas storage would make it a function of the whole
column, so one added row could change every label. The carrier rule exists to
prevent that.

## The artifact

A DP artifact reuses the snapshot shape so the sampler needs no second code
path, and adds a `dp` block:

| `dp` field | Meaning |
|---|---|
| `schema` | `dps-marginal/v3` |
| `release_id` | Unique per fit. The unit of budget accounting. |
| `scope`, `adjacency` | `single-column-marginals`, `add-remove-one-row` |
| `epsilon_total`, `delta_total` | The composed loss actually certified |
| `query_count`, `numeric_bins` | Schedule shape |
| `column_schema`, `numeric_domains`, `categorical_columns` | What was declared |
| `normalization_policy` | Plain-text statement of the carrier rules |
| `accountant`, `opendp_version`, `dp_accounting_version`, `codec` | What produced it |
| `provenance` | `platform`, `cpython`, `fingerprint` of the fitting stack |

Numeric columns carry `bin_edges`, `bin_counts`, and the declared `min` and
`max`. `mean`, `std` and `quantiles` are empty: no exact moment is released.
`row_count`, `null_count` and `non_null_count` are noisy and need not be
consistent with each other.

## Generation-side verification (`plan/_checks_dp.py`)

`global_settings.dp: {epsilon, delta}` declares that the pipeline's generate
output must be a DP release. Its presence turns on these compile-time checks:

| Check | Failure |
|---|---|
| Every statistical column's snapshot is a reproduced `dps-marginal/v3` release, not merely one with a truthy `dp` key | Rejected |
| `allow_real_categories`, `high_cardinality`, `condition_on` on a statistical column | Rejected |
| The artifact's recorded provenance is a certified row (`validate_recorded_provenance`) | `dp_stack_uncertified`, `dp_platform_uncertified`, `dp_provenance_record_malformed` |
| Declared columns and carriers reproduce the artifact's `query_count` | Rejected |
| Composed spend across distinct `release_id`s is within the declared ceiling | `dp_budget_exceeded` |
| One `release_id` with two different byte contents | Rejected as a conflicting artifact |

Budget rules: distinct release IDs compose by summing their already-composed
totals (`dp_ledger.py`); one release ID referenced by many columns is charged
once.

Generation checks the provenance recorded in the artifact. It does not
recompute a fingerprint from the generating host, which may differ from the
fitting host.

A verified DP column is exempt from the `allow_real_categories` gate, because
its label set is itself a DP release. The exemption is passed down by the
compiler as `dp_verified=True`.

## The certified stack (`quality/dp_provenance.py`)

A guarantee is only honest on the software it was tested against. The gate
certifies rows of `(platform, cpython_full_version, lock_fingerprint)` in
`_CERTIFIED_STACKS`.

- **Fingerprint.** SHA-256 over the sorted, PEP 503-canonical `name==version`
  list of every installed distribution. Complete by construction. An earlier
  hand-curated list kept missing transitive dependencies.
- **Platform.** Linux, x86-64, CPython, glibc only.
- **Strictness.** Any extra, missing or re-versioned package changes the
  fingerprint and fails the gate. This includes the engine's own version.
- **Not gated in v1.** Native binaries below the distribution level: OpenDP's
  Rust core, OpenBLAS, the glibc patch, the exact CPython build. The record is
  audit evidence, not a MAC.

### Certified profiles

| Profile | Used by | How it is built |
|---|---|---|
| Engine dev and CI | `dp-certified-gate` job, local dev | `uv sync --frozen --extra dev --extra lint --extra vault --extra cloud` on CPython 3.10.20 |
| CLI runtime | End users running `decoy fit --epsilon` | `decoy-engine` and `decoy-cli` plus `requirements-certified.txt` from the `decoy` repo (63 third-party pins) on CPython 3.10.20 |

Prior releases' rows are kept from engine 0.18.0 on, so their artifacts still
compile.

### Re-pinning

Any change to `uv.lock` that moves the certified profile, and any engine
version bump, needs a new row. In one change:

1. Sync the profile fresh on CPython 3.10.20 and compute the fingerprint with
   `compute_lock_fingerprint(installed_distribution_set())`.
2. Add the fingerprint to `_CERTIFIED_STACKS`, keeping older rows.
3. Regenerate NOTICE with `scripts/dev/licenses_write.sh`.
4. For the CLI runtime row, re-derive `requirements-certified.txt` in the
   `decoy` repo, confirm its fingerprint, and add that row too.

`tests/sentry/test_dp_lock_row.py` recomputes the fingerprint from `uv.lock`
alone and fails when it is not a member. It never skips, so a forgotten re-pin
is caught on every host.

Keep fingerprint-moving tools out of the certified extras. `mutmut` and
`pytest-cov` live in a separate `mutation` extra for this reason.

## Testing

Tests that need a completed fit are marked `dp_certified`. Off the certified
profile they are skipped by `tests/conftest.py`.

| Job or test | Purpose |
|---|---|
| `dp-certified-gate` (ci.yml) | Asserts the environment is certified, runs the marked suites, and fails unless tests passed with none skipped. Unconditional, not path-filtered. |
| `dps-dependency-matrix.yml` | Establishes the fingerprint per exact CPython patch. Marked draft. |
| `tests/unit/quality/test_dp*.py`, `test_carriers.py` | Fit, budget, carriers, codec goldens, provenance |
| `tests/unit/generation/test_generate_dp_contract.py` | The generate-side contract |
| `testflight/test_dp_acceptance.py` | Acceptance |

The backend is injectable for tests that observe the session's own
bookkeeping. Production never passes one.

## Operational boundaries

- **Single thread.** Totality relies on a process-global warning filter that
  is not thread-safe. Run a fit on one thread.
- **Parsing is the caller's job.** Adjacency is over frame rows. If CSV type
  inference could re-type a column on one record, fix dtypes before the fit.
- **Live objects in cells are out of scope.** Only `KeyboardInterrupt` and
  `SystemExit` escape the conversion.
- **Utility is not guaranteed.** See the sizing table below.

## Measured utility and cost

From `docs/records/2026-11-10-cli-qa-generation-distribution-models.md` in the
`decoy` repo. Labels surviving for a 5-value and a 20-value column, delta 1e-6,
one draw per cell.

| Columns (queries) | Rows | epsilon 0.1 | epsilon 1 | epsilon 5 |
|---|---|---|---|---|
| 2 (4) | 500 | 0/5 | 3/5 | 5/5 |
| 2 (4) | 5,000 | 3/5 | 5/5 | 5/5 |
| 8 (13) | 500 | 0/5, 0/20 | 0/5, 0/20 | 4/5, 3/20 |
| 8 (13) | 5,000 | 1/5, 0/20 | 5/5, 6/20 | 5/5, 20/20 |
| 8 (13) | 50,000 | 5/5, 6/20 | 5/5, 20/20 | 5/5, 20/20 |
| 20 (31) | 5,000 | 0/5, 0/20 | 4/5, 3/20 | 5/5, 16/20 |
| 20 (31) | 50,000 | 4/5, 4/20 | 5/5, 20/20 | 5/5, 20/20 |

With a uniform per-query split, each added column lowers every column's share.

Allocation search cost, first fit per `(schedule, epsilon, delta)` in a
process: 11 s for 4 queries, 36 s for 13, 85 s for 31. Later fits with the same
key reuse the cached scalar (0.3 to 2.4 s at 5,000 rows). The CLI starts cold
every time. Release cost then scales with rows: about 14 s, 55 s and 140 s at
500,000 rows.

A release can be empty. Twenty rows at epsilon 0.02 released no labels and a
row count of 318. It compiles and then fails at generation with
`statistical_stats_degenerate`.

Forged artifacts were tested and are accepted, as the scope statement says: an
added label, a lowered `epsilon_total`, and an exact snapshot reshaped to the
DP schema all passed `verify_dp_snapshots`.

## Design history

`docs/plans/2026-07-22-dps-established-library-survey.md`,
`2026-07-22-dps-scope-b-implementation-guide.md`,
`2026-07-22-dps-scope-b-spike-result.md`,
`2026-07-23-dps-codec-implementation-guide.md`,
`2026-07-24-dp-test-certification-awareness.md`.

An earlier design (Option A, `apply_dp_noise`) noised an already-computed exact
snapshot. It never met the bound it claimed and was deleted. Do not route a DP
fit through `compute_distribution_snapshot`.
