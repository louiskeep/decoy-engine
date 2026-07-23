# Decoy DPS Scope B implementation guide

## 1. Context and non-goals

This rebuild replaces the parked Option A Differential Privacy Synthesis implementation at `feat/dps-option-a` commit `fafbb7500fa97dd8b5fa5a5b1fee01324a1dc713`.

**Revised 2026-07-22.** The first revision routed every release through OpenDP's Polars-integrated `Context` compositor. That design is dead: the compositor FFI-locks to a Polars version Decoy does not run. Sections 3, 4.3, 4.4, 4.5, 4.6, 5 (steps 1 to 3 and 6), 6, 7, 8, and 9 now specify per-column OpenDP measurements composed by Google `dp_accounting`. Section 3.4 records the closed spike, and section 4.3.5 states plainly which guarantee that costs. Do not reconstruct the compositor design from an older copy of this document.

The two prior implementations were blocked because they leaked private predicates during fitting, implemented categorical thresholding incorrectly, allowed generation to bypass compilation, did not pin verified snapshot contents, and undercharged independent releases whose serialized contents happened to collide. This implementation must remove those failure modes structurally and pin each one with an assertion test before production code changes.

Verified Option A source anchors:

All line numbers below were re-verified against the worktree at `fafbb75` on 2026-07-22. Re-verify before editing if the branch has moved.

| Concern | Verified source |
|---|---|
| Home-grown threshold and premature rounding | `src/decoy_engine/quality/dp.py:105-110`, `:194-197` |
| Suppressed-count summation into `other_count` | `src/decoy_engine/quality/dp.py:232-251` (`suppressed += noised` at `:245`, `other_count` at `:248`) |
| Exact-rank categorical ordering (the rank is minted here and preserved unchanged by `dp.py`) | `src/decoy_engine/quality/snapshot.py:577-598` |
| All-null `kind: empty` bypass | `src/decoy_engine/quality/snapshot.py:260-300` (`kind, support_origin = "empty", "data"` at `:279`) |
| All-inf numeric special shape | `src/decoy_engine/quality/snapshot.py:514-575` (the `finite.size == 0` branch at `:524-535`) |
| Exact per-column scalars that must not survive into a DP artifact | `src/decoy_engine/quality/snapshot.py:271-273` (`null_count`, `non_null_count`, `distinct_count`) |
| Falsy `dp: {}` fail-open | `src/decoy_engine/plan/_checks_dp.py:62-71` |
| DP categorical rejection | `src/decoy_engine/plan/_checks_dp.py:73-93` (shared raise), `:147-221` (`check_dp_categorical_unsupported`), `:438-441` (duplicate raise inside provenance) |
| Content-hash budget identity | `src/decoy_engine/plan/_checks_dp.py:384` (`seen_artifacts` keyed by digest), `:483-525`, `:527-553` |
| Snapshot path cache and parsing | `src/decoy_engine/generation/statistical/_spec.py:74-92` (content-keyed LRU), `:97-141` (`_load_snapshot`) |
| `allow_real_categories` requirement | `src/decoy_engine/generation/statistical/_spec.py:219-227` |
| Raw-config generation entrypoint | `src/decoy_engine/generation/synthesize.py:76-176` |
| Runtime snapshot reload | `src/decoy_engine/generation/synthesize.py:292-325` (`load_spec(col)` at `:308`) |
| Fidelity-gate snapshot reload | `src/decoy_engine/generation/_fidelity_gate.py:39` (module-level import), `:60-130` (`score_generated_fidelity`, `_load_snapshot` call at `:107`) |
| Existing Plan shape | `src/decoy_engine/plan/_types.py:281-304` |
| Compile checks and Plan construction | `src/decoy_engine/plan/_compile.py:189-198`, `:444-462` |
| Plan serialization | `src/decoy_engine/plan/_serialize.py:53-65`, `:179-194` |
| Pipeline passes raw config after compilation | `src/decoy_engine/execution/_pipeline.py:467-476` |
| Manual accountant to be replaced | `src/decoy_engine/quality/dp_budget.py:38-96` |
| Non-DP fit entrypoint carrying `dp_mode`/`numeric_domains` | `src/decoy_engine/quality/snapshot.py:152-162` |

The file referred to elsewhere as `plan/_spec.py` is actually `src/decoy_engine/generation/statistical/_spec.py` in the parked worktree. Do not create a second spec module under `plan/`.

Current module sizes at `fafbb75`, which the ~600 LOC orchestration cap in `CLAUDE.md` constrains: `plan/_compile.py` 690, `execution/_pipeline.py` 645, `generation/synthesize.py` 600, `plan/_checks_dp.py` 553. Three of the four modules this build touches are already at or over the cap, so every step below that adds orchestration must extract into a new module rather than grow the existing one. Net LOC for each of those four files must not increase.

This build covers one fit-wide release containing single-column marginals for:

- Numeric columns with caller-declared public domains.
- Categorical columns with caller-declared public kinds and privately discovered labels released through an OpenDP stable-key mechanism.

The following are out of scope:

- Joint or conditional synthesis.
- Cross-column correlation guarantees.
- PrivBayes, MST, AIM, DPS-4, or another graphical model.
- `condition_on`, joint snapshots, and joint fidelity claims under DP.
- Datetime-specific DP mechanisms.
- Free-text synthesis.
- Authentication or signatures for serialized Plan files.
- Protection against hostile Python code executing in the same process.
- Caller-specific orchestration inside `decoy-engine`.

Leave one seam for future joint mechanisms by making the release scope an explicit enum or literal, currently accepting only `single-column-marginals`. Do not add dormant joint parameters, accountant branches, or schema fields beyond that discriminator.

## 2. Binding decisions

The implementer may not renegotiate these decisions:

1. Implement Scope B for both numeric and categorical single-column marginals.
2. Wrap OpenDP. Do not vendor OpenDP code or retain Decoy’s mechanism math.
3. Replace `apply_dp_noise(snapshot, ...)` with a one-shot DP fit API. The DP API receives raw tabular values plus public column declarations and never consumes an inferred exact snapshot.
4. Require explicit `categorical_columns`, `numeric_domains`, `epsilon`, and `delta`. These declarations must cover every fitted column exactly once.
5. Never infer a DP column kind from values, dtype cardinality, nullness, or successful conversion.
6. Compute categorical `other_count` only from the independently noised non-null total and the kept released counts.
7. Let OpenDP perform threshold comparison on the mechanism’s exact noisy value. Decoy may round only after OpenDP has completed selection.
8. Generation accepts a compiled `Plan`, not a raw configuration mapping.
9. Compilation embeds the verified snapshot bytes and parsed statistical specification in the Plan. Generation and fidelity checking do not reopen snapshot paths.
10. Mint a data-independent release ID for every independent fit. Byte-for-byte copies retain it. Independent fits compose even when their released bytes are identical.
11. Keep DP production randomness unseeded. No public seed, RNG, or deterministic-noise parameter may exist.
12. Reject unsupported DP joint behavior explicitly. Do not silently fall back to non-DP sampling.
13. Do not import `opendp.extras.polars`, install `opendp[polars]`, or use any OpenDP `Context`, `LazyFrame`, or table-domain API. The 2026-07-22 spike closed that route permanently; section 3.4 records the outcome so it is not re-litigated.
14. Every privacy quantity that reaches the artifact is read back from an OpenDP `Measurement.map()` or from a `dp_accounting` privacy-loss-distribution composition. Decoy computes no epsilon, no delta, no noise scale, and no threshold from a formula of its own.
15. Rows become counts inside OpenDP. Each column's measurement is a chained OpenDP `Transformation >> Measurement` whose input metric is `symmetric_distance()` and whose input is the column's normalized value vector. Decoy must not pre-aggregate counts in Python and hand a count vector to a bare mechanism, because that moves the sensitivity derivation out of the library and into Decoy.

## 3. Dependency decision

### 3.1 Package selection

Add these two core runtime dependencies:

```toml
opendp==0.15.1
dp-accounting==0.6.0
```

`opendp` supplies every mechanism and every per-measurement privacy map. Its published license is MIT, compatible with Decoy's Apache-2.0. OpenDP 0.15.1 declares Python 3.10 or newer and publishes an `abi3` wheel covering the Python 3.10 through 3.12 range declared at `pyproject.toml:13` (`requires-python`) and `:21-39` (classifiers). See [OpenDP 0.15.1 on PyPI](https://pypi.org/project/opendp/0.15.1/).

`dp-accounting` is Google's standalone, pure-Python privacy accountant from `google/differential-privacy`. Its license is Apache-2.0, identical to Decoy's own, so there is no compatibility question. It has no native build step. It is the composition accountant for this build, in the accounting-only role the DPS established-library survey already blessed (`docs/plans/2026-07-22-dps-established-library-survey.md`, section 2 "Google differential-privacy / dp-accounting / PipelineDP" and the composition row of the section 3 mapping table). It supplies no mechanism and no noise.

Use exact pins for both. The implementation depends on privacy-critical OpenDP APIs marked `contrib`, and on `dp_accounting.pld`'s dominating-pair construction. Upgrade either package only through a separate dependency review with mechanism and composition regression tests.

**STOP condition, absolute.** Do not install the `opendp[polars]` extra. Do not import `opendp.extras.polars`. Do not narrow, pin, or otherwise change Decoy's existing `polars>=1,<2` range for any reason arising from this build. `opendp[polars]` pins `polars==1.36.1` exactly and its compiled core embeds a Polars DSL schema hash for that release; Decoy resolves `polars==1.42.0`. That conflict is what killed the previous design (section 3.4). This build touches Polars not at all: neither mechanism path in section 4 involves a `LazyFrame`, a Polars expression, or an OpenDP table domain. If a step appears to require one, you have deviated from the design; stop and escalate rather than reintroducing the extra.

Use only dependencies licensed under Apache-2.0 or MIT for this feature.

### 3.2 Mandatory resolution spike

Complete this spike before editing `quality/dp.py`. It is a fresh spike for the new dependency shape; the compositor spike it replaces is closed and recorded in section 3.4.

1. For Python 3.10, 3.11, and 3.12, ask pip to resolve binary distributions without allowing an sdist:

   ```bash
   dps_wheel_dir="$(mktemp -d)"
   for py in python3.10 python3.11 python3.12; do
     "$py" -m pip download --only-binary=:all: --no-deps \
       --dest "$dps_wheel_dir/$py" "opendp==0.15.1" "dp-accounting==0.6.0"
   done
   ```

2. In clean virtual environments for every available supported interpreter, install Decoy with its development dependencies plus both pins. Record the resolved `polars` version and assert it is unchanged from the pre-spike resolution. A spike that moves Polars has already failed.

3. Run a smoke program that:

   - Imports `opendp.prelude` and `dp_accounting`.
   - Enables only the OpenDP `contrib` feature. Do not enable `honest-but-curious`; every constructor this build uses is reachable under `contrib` alone, and that has been verified.
   - Builds the numeric chain of section 4.4 and invokes it on a vector, including an empty vector.
   - Builds the categorical chain of section 4.5 and invokes it on a vector, including an empty vector.
   - Reads `Measurement.map(1)` from each chain and confirms the numeric chain reports a scalar epsilon and the categorical chain reports an `(epsilon, delta)` pair.
   - Composes those reported losses through `dp_accounting.pld` per section 3.3 and reads back a composed epsilon at the fit-wide delta.
   - Asserts that nothing in the program imports `polars` or `opendp.extras`.

4. Add the same import and construction smoke to the supported CI matrix. A successful local wheel download is not proof that every CI platform resolves.

The relevant OpenDP patterns are its [transformation user guide](https://docs.opendp.org/en/stable/api/user-guide/transformations/index.html), [thresholded noise mechanisms](https://docs.opendp.org/en/stable/api/user-guide/measurements/thresholded-noise-mechanisms.html), and [parameter search utilities](https://docs.opendp.org/en/stable/api/user-guide/utilities/parameter-search.html). The relevant `dp_accounting` pattern is `dp_accounting.pld.privacy_loss_distribution.from_privacy_parameters`, the dominating-pair construction for a mechanism known only by its `(epsilon, delta)`. Cite these patterns in the implementing module's docstring as required by `CLAUDE.md`.

### 3.3 Accountant choice

There is no single object that both hosts every mechanism and accounts for the whole fit. The architecture splits that responsibility across two libraries, and the split is the part of this design most in need of care.

**OpenDP certifies each column.** Every column's release is one chained `Transformation >> Measurement`. `Measurement.map(d_in)` is what reports that release's privacy loss, with `d_in = 1` under `symmetric_distance()`, meaning one added or removed row. The adapter must read the loss back from `map()` on the exact measurement object it invoked. It must never assume the loss it calibrated for; calibration proposes, `map()` certifies.

**`dp_accounting` composes those certificates.** For each certified `(epsilon_i, delta_i)`, build the dominating-pair privacy loss distribution:

```python
from dp_accounting.pld import common
from dp_accounting.pld import privacy_loss_distribution as pldist

pld = pldist.from_privacy_parameters(
    common.DifferentialPrivacyParameters(epsilon_i, delta_i),
    value_discretization_interval=_PLD_DISCRETIZATION,  # module constant, 1e-4
)
```

Compose them with `PrivacyLossDistribution.compose` (and `self_compose(k)` for `k` identical certificates, which is the same result and much faster). Read the fit-wide loss with `composed.get_epsilon_for_delta(delta)` at the caller's requested `delta`.

`from_privacy_parameters` is the correct constructor precisely because Decoy knows each OpenDP measurement only by its certified `(epsilon_i, delta_i)`. The resulting PLD dominates any mechanism satisfying those parameters, so the composed result is a valid upper bound. It is looser than a mechanism-specific PLD would be. Accept that looseness; do not attempt to hand-build a tighter mechanism-specific PLD for OpenDP's thresholded Laplace.

`dp_accounting` 0.6.0 has no generic approximate-DP `DpEvent`, so the `DpEventBuilder` / `PrivacyAccountant` path cannot carry the thresholded categorical release. Verified by enumerating `dp_accounting.dp_event`: there is no `EpsilonDeltaDpEvent`. Use the `pld` module directly, as above. Do not route pure-epsilon columns through `LaplaceDpEvent` and thresholded columns through something else; one uniform representation for every column keeps the composition argument reviewable.

Do not retain `PrivacyBudget.charge()` arithmetic as the mechanism accountant. Compile-time summation of already certified release totals across artifacts remains a policy ceiling check, not a replacement mechanism accountant.

**What Decoy owns, and it is exactly this much.** Naming it precisely is the point; anything beyond this list is a defect:

1. Recordwise value normalization, and the claim that it is recordwise, so one input row contributes at most one element to each column vector. This is the one stability claim that is not OpenDP's. It is structural rather than numeric, and section 7.1 pins it with a test.
2. The public budget-allocation policy of section 4.3, which decides how the fit-wide budget is divided across queries. Allocation is a utility decision over library-computed quantities, not a privacy derivation.
3. Wiring OpenDP's certified losses into `dp_accounting`'s composition and asserting the result against the request.

Nothing else. No noise is sampled by Decoy, no scale or threshold is computed by a Decoy formula, no epsilon or delta is added, multiplied, or converted by Decoy arithmetic.

Stop before adapter work if:

- A supported Decoy platform cannot resolve an `opendp` or `dp-accounting` binary wheel.
- Installing either package moves Decoy's resolved `polars` version.
- Any constructor in section 4.4 or 4.5 requires the `honest-but-curious` feature, an `opendp.extras` import, or a user-defined measurement.
- `Measurement.map(1)` cannot be read back from a constructed chain.
- The `dp_accounting` composition of the certified losses exceeds the requested `(epsilon, delta)` for a schedule the allocation policy claims is feasible.

Do not respond to those failures with manual Laplace noise, manual threshold calibration, or manual floating-point composition.

### 3.4 Resolved spike outcome: the Polars Context is closed

This is settled. Do not re-open it, do not re-run the compositor spike, and do not propose a variant of it.

`docs/plans/2026-07-22-dps-scope-b-spike-result.md` ran the previous revision's section 3.2 spike and returned STOP. Findings that remain true and that this revision is built on:

- `opendp==0.15.1` resolves one `abi3` wheel covering Python 3.10, 3.11, and 3.12, and installs into Decoy's dependency set with one new transitive dependency (`deprecated`) and no resolver conflict.
- `make_laplace_threshold`, `make_gaussian_threshold`, and `make_count_by` all exist under the assumed names.
- The only OpenDP API that composes heterogeneous per-column queries against one shared table object is `opendp.extras.polars`'s `Context.compositor`, and it FFI-locks to `polars==1.36.1` through an embedded DSL schema hash. Against Decoy's resolved `polars==1.42.0` it fails at `Context.compositor()` construction, before any query runs. The same failure reproduces on `polars==1.43.0`. No current OpenDP release, including pre-releases, widens that pin.

The product owner's decision on that result: drop the Polars Context, keep OpenDP core mechanisms per column, and use `dp_accounting` as the composition accountant. That is spike-result option 4, and it is the design this document now specifies. Options 1 (pin Polars to 1.36.1) and 2 (wait for upstream) are rejected.

Facts verified in the build venv after that decision, which the implementer may rely on without re-deriving:

- `opendp 0.15.1`, `dp-accounting 0.6.0`, and `polars 1.42.0` install and coexist.
- Both mechanism chains build and run under `contrib` alone, with zero Polars involvement.
- `make_laplace_threshold` takes `metrics.l01inf_distance(dp.absolute_distance(T=int))` as its input metric, not `l1_distance`. Passing `l1_distance` is a hard FFI cast error, not a graceful rejection. When the mechanism is reached through `make_count_by` as section 4.5 requires, `make_count_by`'s own output metric is already `L01InfDistance(AbsoluteDistance(i32))`, so `then_laplace_threshold` picks it up and the error cannot occur. Constructing the measurement standalone is where the mistake happens.
- `make_laplace_threshold`'s `threshold` argument is the count type, `i32`. Values above `2**31 - 1` raise `ValueError: ... is not representable by i32`. Section 4.3 bounds the threshold search accordingly.

## 4. Target architecture

### 4.1 End-to-end flow

```text
public declarations
        |
        v
fit_dp_snapshot(dataframe, categorical_columns, numeric_domains,
                epsilon, delta)
        |
        | fixed public query schedule
        | one OpenDP Transformation >> Measurement per query
        | each certified by its own Measurement.map(1)
        | dp_accounting PLD composition over those certificates
        v
DP snapshot artifact
  - released numeric marginals
  - released categorical labels/counts
  - release_id
  - certified privacy loss
        |
        v
compile_plan(config)
  - reads each snapshot exactly once
  - verifies schema and DP provenance
  - deduplicates budget by release_id
  - embeds exact bytes and frozen parsed specs
        |
        v
Plan with GenerationPlan
  - pinned snapshot bytes and digests
  - immutable statistical specs
  - DP verification receipt
        |
        v
generate_tables(plan)
  - accepts Plan only
  - never reads snapshot paths
  - uses pinned specs for sampling and fidelity
```

### 4.2 DP fit contract

Replace the old fit path with:

```python
def fit_dp_snapshot(
    frame: pandas.DataFrame,
    *,
    categorical_columns: Collection[str],
    numeric_domains: Mapping[str, tuple[float, float]],
    epsilon: float,
    delta: float,
    numeric_bins: int = 10,
) -> dict[str, Any]:
    ...
```

Contract requirements:

- `categorical_columns` and `numeric_domains` are mandatory, even when one is empty.
- `epsilon` and `delta` are mandatory keyword-only arguments.
- `epsilon` must be finite and strictly positive. `delta` must be finite and strictly positive; `delta = 0` is rejected with a typed error even for a numeric-only fit, and `delta >= 1` is rejected as well.
- `numeric_bins` must be an integer of at least 2. Its default is 10 and its actual value is recorded in the artifact.
- The union of categorical names and numeric-domain keys must equal `frame.columns`.
- The two sets must be disjoint.
- Every numeric lower and upper bound must be finite and satisfy `lower < upper`.
- Configuration validation occurs before inspecting values.
- No `rng`, `seed`, mechanism-scale, or threshold parameter is public.
- The function returns only released quantities and public metadata.
- It never constructs or persists an exact distribution snapshot.
- It mints `uuid.uuid4().hex` once per call before value processing. The ID does not depend on data, noise, or released contents.
- Unexpected row values are normalized through total, recordwise preprocessing. Numeric conversion failures become null; NaN becomes null; infinities clamp to the declared bounds. Categorical nulls remain null; supported scalar values receive one documented canonical string representation. Content must not cause a kind-selection error.
- The output kind for each column comes exclusively from the declaration.

The return value is a complete snapshot artifact, not a fragment. It keeps the existing outer `schema_version` value `distribution-snapshot/v1` so `generation/statistical/_spec.py::_load_snapshot` (`:133-141`, which rejects any other value) and every non-DP consumer keep working unchanged. The `dp` block is additive and carries its own `dp.schema` discriminator. Do not bump the outer snapshot schema version in this build; doing so forces a coordinated change across every snapshot consumer and is out of scope.

Delete `apply_dp_noise` rather than retaining a compatibility overload. The pre-GA API break is accepted.

### 4.2.1 Exactly what a DP column entry may contain

`compute_distribution_snapshot`'s per-column entry carries exact `null_count`, `non_null_count`, and `distinct_count` (`quality/snapshot.py:271-273`) and, for numeric columns, exact `min`, `max`, `mean`, `std`, and `quantiles` (`:566-575`). Every one of those is an exact private statistic. Copying that builder, or any part of it, into the DP path is a direct disclosure. `fit_dp_snapshot` constructs its column entries from scratch. The following lists are exhaustive; emitting any key not listed is a defect.

Every DP column entry contains exactly:

```text
dtype   a fixed label per declared KIND (_DP_NUMERIC_DTYPE_LABEL /
        _DP_CATEGORICAL_DTYPE_LABEL), never read off the frame. This said
        "canonical_dtype_label(frame[col].dtype)" until Codex round 3
        showed the frame's own dtype IS content-dependent: pandas upcasts
        an integer column to float64 the moment a null enters it, so [1]
        and [1, None] emitted different dtypes under identical public
        declarations. Kind is the only public dtype signal a DP fit has.
kind    "numeric" or "categorical", taken only from the caller's declaration
stats   the per-kind block below
```

`null_count`, `non_null_count`, and `distinct_count` are derived from released quantities only, or omitted. Use:

```text
non_null_count  numeric:     sum of the released bin counts
                categorical: the released noised non-null total
null_count      max(0, released row_count - non_null_count)
distinct_count  numeric:     count of released bins with a nonzero count
                categorical: len(released top_values), plus 1 when other_count > 0
```

No other derivation is permitted, and no exact fallback is permitted when a derived value looks implausible (for example a negative null count clamps to zero, it does not fall back to the true value).

A numeric `stats` block contains exactly:

```text
bin_edges   the full numeric_bins + 1 boundary list spanning [lower, upper],
            derived from the declared public domain and numeric_bins; never
            from data. Note the OpenDP chain in section 4.4 is constructed
            from the numeric_bins - 1 interior cut points of this same list.
bin_counts  released counts, serialized with max(0, int(round(v)))
min         the declared domain lower bound
max         the declared domain upper bound
mean        null
std         null
quantiles   {}
```

A categorical `stats` block contains exactly:

```text
top_values   released, retained (label, count) pairs, ordered per section 4.5
other_count  derived per section 4.5 step 8
```

Do not emit the `high_cardinality` provenance marker (`quality/snapshot.py:510`) on a DP column. High-cardinality retention is rejected under DP.

`support_origin` is not emitted by `fit_dp_snapshot`. It existed to prove a non-DP fit had used caller domains; under Scope B every column in the artifact came from a declaration by construction, and the `dp` block is the provenance. Compile-time checks must key off the `dp` block and the declared `categorical_columns`/`numeric_domains` recorded in it, not off `support_origin`.

### 4.3 Fixed query schedule, budget allocation, and composition

#### 4.3.1 The schedule

Construct the schedule before examining column values:

```text
1 table row-count query
1 binned-count query per numeric column
2 queries per categorical column:
  - thresholded unknown-key grouped count
  - noised non-null total
```

Thus:

```text
query_count = 1 + numeric_column_count + 2 * categorical_column_count
```

The query order is public and deterministic:

1. Table row count.
2. Numeric columns sorted by column name.
3. Categorical columns sorted by column name, with grouped count before non-null total.

Every query is an independent OpenDP measurement over its own column vector. There is no shared compositor object, because OpenDP 0.15.1 offers no non-Polars way to build one across heterogeneous per-column domains (section 3.4). Composition happens after the fact, over the certificates each measurement issues.

Sequential composition over per-column measurements is sound here for the ordinary reason: each measurement is applied to a projection of the same dataset, one added or removed row perturbs each projection by at most one element, and every measurement's map is evaluated at that same `d_in = 1`. What Decoy must guarantee, and what section 3.3 item 1 names, is that the projection really is recordwise.

#### 4.3.2 Budget allocation

Allocation is a fixed public policy. It depends only on `epsilon`, `delta`, `numeric_column_count`, and `categorical_column_count`. It may not depend on values, nullness, cardinality, dtype, or any mechanism output.

Delta is consumed only by thresholded categorical grouping queries. Allocate:

```text
delta_threshold_per_categorical = (delta / 2) / categorical_column_count
```

The unallocated half of `delta` is composition headroom, spent by `get_epsilon_for_delta` when the certificates are composed. When `categorical_column_count` is zero, no query consumes delta and the whole of it is headroom. `delta = 0` is rejected at the API boundary regardless (section 4.2 and section 9.10 item 2).

Epsilon is allocated as a single per-query value `eps_q`, identical for every query in the schedule. Do not compute `eps_q` as `epsilon / query_count`. Instead select the largest `eps_q` whose resulting schedule composes within the request, by monotone search over the accountant:

```python
def _composed_epsilon(eps_q: float) -> float:
    """Fit-wide loss for a schedule built at per-query epsilon eps_q."""
    certificates = _certify_schedule(eps_q, delta_threshold_per_categorical)
    return _compose(certificates).get_epsilon_for_delta(delta)


eps_q = _search_largest(lambda e: _composed_epsilon(e) <= epsilon,
                        lower=_EPS_Q_FLOOR, upper=epsilon)
```

`_certify_schedule` builds the measurements and reads their maps; it does not touch data. `_search_largest` is a fixed-iteration bisection over a monotone predicate, `_PLD_SEARCH_ITERATIONS = 40`, with `_EPS_Q_FLOOR = 1e-9`. Fix both as module constants and comment the derivation.

Two reasons this is a search and not a division. It is exact rather than approximately safe: `epsilon / query_count` overshoots the request by roughly the PLD discretization interval on some schedules, which would make the section 4.3.4 assertion fail on a correct implementation and invite someone to weaken the assertion. And it recovers the composition benefit the accountant provides. For a 20-numeric, 10-categorical fit at `epsilon = 1.0`, even division yields `eps_q = 0.0244` and a composed loss of `0.64`, leaving a third of the budget unspent; the search yields `eps_q = 0.0372` and a composed loss of `0.998`.

The search is data-independent, so its result is a pure function of the four public inputs. Caching it is permitted. Making it depend on anything else is not.

If the predicate is false even at `_EPS_Q_FLOOR`, the requested budget cannot fund the schedule. Raise a typed `dp_budget_infeasible` error naming `query_count`, `epsilon`, and `delta`. Do not silently widen the request.

#### 4.3.3 Calibrating one measurement to its allocation

Calibration searches OpenDP's own privacy map. It never inverts a mechanism formula.

Numeric and count queries, which report a scalar epsilon:

```python
scale = dp.binary_search(
    lambda s: (transformation >> meas.then_laplace(scale=s)).map(1) <= eps_q,
    bounds=(1e-12, 1e12),
)
```

The thresholded categorical query reports an `(epsilon, delta)` pair, so calibrate the two parameters in two separate scalar searches. Do not pass a tuple `d_out` to `binary_search_param`: the pair carries only a partial order, and OpenDP raises `FailedFunction("unknown ordering between ...")` when the candidates are incomparable.

```python
_I32_MAX = 2**31 - 1


def _chain(scale: float, threshold: int) -> dp.Measurement:
    return count_by >> meas.then_laplace_threshold(scale=scale, threshold=threshold)


scale = dp.binary_search(
    lambda s: _chain(s, _I32_MAX).map(1)[0] <= eps_q,
    bounds=(1e-12, 1e12),
)
threshold = dp.binary_search(
    lambda t: _chain(scale, t).map(1)[1] <= delta_threshold_per_categorical,
    bounds=(1, _I32_MAX),
    T=int,
)
```

`threshold` is the count type `i32`; a bound above `_I32_MAX` raises `ValueError: ... is not representable by i32`. If no threshold within that range reaches the allocated delta, raise `dp_budget_infeasible` rather than accepting a larger delta.

After construction, read the certificate back from the constructed object:

```python
certified_loss = measurement.map(1)
```

The certificate, not the allocation target, is what enters composition and what the artifact's totals derive from. A calibration search that lands slightly under its target must show up as a slightly smaller certified loss, not as the target.

#### 4.3.4 Composition and the fit-wide assertion

Compose every certificate in the schedule through `dp_accounting.pld` exactly as section 3.3 specifies, then:

```python
epsilon_total = composed.get_epsilon_for_delta(delta)
delta_total = delta
```

Assert `epsilon_total <= epsilon` and that `epsilon_total` is finite before serializing the artifact. A non-finite `epsilon_total` means the composed mechanism's delta floor exceeds the requested delta; that is a real failure, so raise `dp_budget_infeasible` rather than reporting infinity.

The number of certificates composed must equal `query_count`. Assert that too. This assertion is what replaces the OpenDP Context's runtime refusal of an unscheduled query, and section 4.3.5 explains why the replacement is weaker.

#### 4.3.5 What dropping the Context costs, stated plainly

One guarantee genuinely weakened, and it must not be papered over.

Under the previous design, OpenDP's `Context` was the only route to a release: an unscheduled query was refused by the library at runtime with an exhausted-allowance error, and the accumulated loss was maintained by the same library that owned the mechanisms. Enforcement of the schedule was external to Decoy.

Under this design, nothing outside Decoy prevents a Decoy code path from constructing an OpenDP measurement and invoking it without registering a certificate. The schedule is enforced by `OpenDpReleaseSession` (section 5, step 2), which is Decoy code. A bug in that class is a budget bug that no library will catch.

What did not weaken, and must stay that way:

- Per-measurement privacy loss is still computed entirely by OpenDP's own privacy maps.
- Sensitivity and stability are still derived by OpenDP, because the row-to-counts aggregation runs inside chained OpenDP transformations under `symmetric_distance()` (binding decision 15). Feeding pre-aggregated Python counts to a bare mechanism would move that derivation into Decoy and would be a second, larger weakening. Do not do it.
- Composition is still computed by a library, `dp_accounting`, over a dominating-pair representation that upper-bounds each certified mechanism.
- No epsilon, delta, scale, or threshold is produced by a Decoy formula.

The mitigations that make the remaining exposure reviewable, all mandatory:

1. `OpenDpReleaseSession` is the sole construction and invocation site for OpenDP measurements in the codebase. `quality/dp.py` calls the session; it does not call `opendp` directly. Pin this with an import-shape test so a later contributor cannot quietly add a second call site.
2. The session refuses any release whose query name is not in the frozen schedule, and refuses a second release under a name already used.
3. The session refuses to report a fit-wide loss until every scheduled query has released exactly once.
4. The certificate count is asserted against `query_count` before serialization (section 4.3.4).

Record this shift in `docs/what-we-cannot-prove.md` per section 8.1. A reader is entitled to know that the schedule boundary is Decoy-enforced.

### 4.4 Numeric marginal

For every declared numeric column:

1. Normalize values recordwise.
2. Clamp finite and infinite numeric values to the declared public domain.
3. Exclude normalized nulls.
4. Create fixed public bin edges from the domain and `numeric_bins`.
5. Build the OpenDP chain below and invoke it on the normalized `list[float]`.
6. Include every public bin in the output even when its released count rounds to zero.
7. Convert released counts for serialization with:

   ```python
   max(0, int(round(noisy_count)))
   ```

8. Derive any displayed total, distinct-bin count, or null count only from released quantities.

The chain, verified end to end in the build venv:

```python
domain = dp.vector_domain(dp.atom_domain(T=float, nan=False))
metric = dp.symmetric_distance()

# interior cut points only: B bins over [lower, upper] means B - 1 edges,
# which makes find_bin's category range exactly 0..B-1 with no overflow bin.
transformation = (
    tf.make_find_bin(domain, metric, edges=interior_edges)
    >> tf.then_count_by_categories(categories=list(range(numeric_bins)),
                                   null_category=False)
)
measurement = transformation >> meas.then_laplace(scale=scale)
```

Three properties of this chain the implementer must not reorganize away:

- `make_find_bin` maps a value below the first edge to bin 0 and a value at or above the last edge to bin `numeric_bins - 1`. Combined with the clamp in step 2, that is exactly the declared public domain's semantics, and it handles `+inf` and `-inf` without a special case.
- `null_category=False` is required. Leaving it at its default appends a null bucket the artifact schema has no slot for.
- `atom_domain(T=float, nan=False)` forbids NaN, so step 3 must have removed every NaN. Passing a NaN through is a domain violation, not silent behavior.

Invoking the chain on an empty list returns a full-length count vector of noise, so all-null and all-inf columns have the same kind, bin edges, output shape, query count, and budget schedule as any other values under the same declarations. Do not add a short-circuit for the empty case; the whole point of F2's closure is that there is no branch to take.

### 4.5 Categorical marginal

For every declared categorical column:

1. Normalize values recordwise without inspecting cardinality.
2. Exclude nulls from label grouping.
3. Submit unknown-key grouped counts through OpenDP's stable thresholded grouping chain below.
4. Submit a separate noised non-null total through the count chain below, as its own scheduled query with its own certificate.
5. Receive only the labels retained by OpenDP. Decoy must never receive a list of suppressed noisy label counts for use in output construction.
6. Serialize retained counts only after OpenDP has completed threshold selection.
7. Sort output pairs only after release, by:

   ```text
   descending serialized noised count, then canonical label ascending
   ```

   That key is a total order because released labels are distinct, so the mechanism's emission order cannot survive into the output. Materialize the retained pairs into a list built by `sorted(pairs, key=lambda p: (-p.count, p.label))` and serialize that list. Do not sort in place over a container whose iteration order came from the mechanism, do not rely on a stable sort to break ties, and do not carry the retained set as a dict whose insertion order is the mechanism's. The order must be reconstructible from the released pairs alone.

8. Derive:

   ```python
   other_count = max(
       0,
       serialize_count(noised_non_null_total)
       - sum(serialize_count(count) for count in retained_noisy_counts),
   )
   ```

9. Never calculate `other_count` from suppressed labels, exact cardinality, exact counts, or exact totals.

The two chains, verified end to end in the build venv:

```python
domain = dp.vector_domain(dp.atom_domain(T=str))
metric = dp.symmetric_distance()

count_by = tf.make_count_by(domain, metric)
grouped = count_by >> meas.then_laplace_threshold(scale=scale, threshold=threshold)

counter = tf.make_count(domain, metric, TO=int)
non_null_total = counter >> meas.then_laplace(scale=total_scale)
```

`make_count_by`'s output metric is already `L01InfDistance(AbsoluteDistance(i32))`, which is what `make_laplace_threshold` requires. Reaching the measurement through the chain is therefore also what keeps you out of the `l1_distance` cast error described in section 3.4. If you find yourself writing `dp.map_domain(...)` and `metrics.l01inf_distance(...)` by hand to construct the measurement standalone, you have left the design.

`grouped` returns a plain dict of retained labels to noisy counts. On an empty input it returns `{}`, which is the no-retained-labels case section 4.5's closing paragraph and section 9.5 already cover. It has no separate empty branch and must not acquire one.

OpenDP owns the continuous threshold comparison. Decoy must not calculate `tau`, compare to `tau`, or round a value before OpenDP’s selection. The adapter may only serialize an already selected release.

If no labels survive, the released marginal may contain only `other_count`. `other_mode: emit` can sample the existing other token. A configuration requiring redistribution with no kept labels fails during compilation based on the already-DP-released artifact. That failure is post-processing and spends no additional privacy budget.

### 4.6 Release artifact schema

Use a versioned block:

```json
{
  "dp": {
    "schema": "dps-marginal/v2",
    "release_id": "32-lowercase-hex-characters",
    "scope": "single-column-marginals",
    "adjacency": "add-remove-one-row",
    "epsilon_total": 0.999995,
    "delta_total": 0.000001,
    "accountant": "dp_accounting PLD composition over OpenDP privacy maps",
    "opendp_version": "0.15.1",
    "dp_accounting_version": "0.6.0",
    "query_count": 4,
    "numeric_bins": 10,
    "categorical_columns": ["country"],
    "numeric_domains": {
      "age": [0.0, 120.0]
    }
  }
}
```

`query_count` in this example is `1 + 1 numeric + 2 * 1 categorical = 4`. It is recorded so a compile-time check can recompute it from `categorical_columns` and `numeric_domains` and reject an artifact whose declared schedule does not match its declared columns. `numeric_bins` is recorded for the same reason: bin edges must be reproducible from public metadata alone.

`epsilon_total` is the accountant's composed result, `composed.get_epsilon_for_delta(delta)`, not the requested epsilon. It is normally slightly below the request, as in the example above. Do not round it up to the request, and do not write the request in its place; the receipt must state what was actually certified.

`delta_total` is the caller's requested delta, because that is the delta at which the composed epsilon was evaluated. The pair is only meaningful together.

`accountant` and `dp_accounting_version` are recorded so a consumer can tell which composition produced the totals. Both versions are checked at compile time against the running environment, and a mismatch is a rejection, not a warning: a receipt composed by a different accountant version is a receipt this build cannot reproduce.

The artifact must not contain exact row counts, exact distinct counts, suppressed label names, suppressed noisy counts, inferred kinds, or an RNG seed. It must not carry a per-release `charges` breakdown of the kind `quality/dp.py:284` emits today, and it must not carry the per-query certificates that fed the accountant. The fit-wide `(epsilon_total, delta_total)` is the whole receipt; a per-query breakdown invites a consumer to re-derive a per-column claim the scope does not support. Per-query certificates live inside `OpenDpReleaseSession` for the duration of the fit and are not serialized.

It must not record the calibrated noise scales or thresholds either. Those are public in the sense that they follow from the allocation policy, but writing them into the artifact creates a second apparent source of truth for the privacy claim, and the section 4.3.4 receipt is the only one.

### 4.7 Pinned Plan types

Extend `src/decoy_engine/plan/_types.py` with frozen types equivalent to:

```python
@dataclass(frozen=True)
class PinnedSnapshot:
    source_path: str
    sha256: str
    payload_b64: str
    release_id: str | None


@dataclass(frozen=True)
class PinnedStatisticalSpec:
    table_name: str
    column_name: str
    snapshot_index: int
    spec: FrozenJsonValue


@dataclass(frozen=True)
class DpVerification:
    scope: Literal["single-column-marginals"]
    release_ids: tuple[str, ...]
    epsilon_total: float
    delta_total: float


@dataclass(frozen=True)
class GenerationPlan:
    config_json: str
    snapshots: tuple[PinnedSnapshot, ...]
    statistical_specs: tuple[PinnedStatisticalSpec, ...]
    dp_verification: DpVerification | None
```

`FrozenJsonValue` must be recursively immutable. Do not place mutable dictionaries or lists inside the frozen dataclasses.

`Plan` gains `generation: GenerationPlan | None`.

Compilation reads each snapshot file once as bytes, computes its SHA-256, parses those exact bytes, validates them, and embeds the bytes as base64. Paths remain diagnostic metadata only.

"Once" means once per distinct path per `compile_plan` call, not once per distinct content. The parked code reaches `_load_snapshot(path)` from three separate compile-time callers (`plan/_checks.check_statistical_columns`, `plan/_checks_dp.check_dp_snapshot_provenance`, and `generation/statistical.load_spec`), each of which re-opens the file; the process-global content-keyed LRU at `generation/statistical/_spec.py:74-92` deduplicates by content, so a file swapped between two of those reads yields two different byte strings inside one compilation and nothing detects it. Pinning the last-read bytes would then pin bytes that a different check validated. Restructure as:

1. A single read pass runs first, before any check. It walks the configuration, collects every distinct `snapshot_file` path, opens each exactly once, and builds an immutable `Mapping[str, PinnedSnapshot]` keyed by the configured path string.
2. Every subsequent compile-time check and every `load_spec` call receives bytes or parsed data from that mapping. No check takes a path and opens it. This is the same argument that makes generation safe, applied inside the compiler.
3. The content-keyed LRU stops being a correctness mechanism. Either delete `_SNAPSHOT_CACHE` and `_load_snapshot`'s caching, or keep `_load_snapshot` only as the read-pass primitive and make it the sole `open()` site in the package. Do not leave a second path-taking reader behind.

Pin `test_compile_plan_reads_each_snapshot_path_once` on this by counting `open()` calls per path across one `compile_plan`, not by asserting a cache hit.

`plan_from_yaml` must:

- Decode every embedded payload.
- Recompute and verify every digest.
- Reparse and refreeze every spec.
- Re-run DP artifact consistency checks against embedded bytes.
- Reject a DP verification receipt that cannot be reproduced from the embedded configuration and artifacts.

Serialized Plans are not authenticated against a hostile writer. They are validated capability objects for preventing accidental bypass and file-swap races inside Decoy’s supported execution path.

### 4.8 Generation contract

Change the public signature to:

```python
def generate_tables(
    plan: Plan,
    derive_key: Any = None,
    instance_default_locale: str | None = None,
) -> dict[str, pa.Table]:
    ...
```

Three corrections against the parked source, all of which a literal reading of an earlier draft would get wrong:

- The return type is `dict[str, pa.Table]` (`generation/synthesize.py:80`), not pandas. Generation is Arrow-native and `execution/_pipeline.py:470-476` types the result as `dict[str, pa.Table]`. Changing it to pandas is out of scope and would break the write path.
- `derive_key` and `instance_default_locale` stay. `execution/_pipeline.py:472-476` passes both positionally-by-keyword today, and `derive_key` carries the pipeline-bound key resolver that ties generation into the shared determinism envelope. Dropping either breaks the pipeline.
- Do not add a public `seed` parameter. The job seed is already resolved from the configuration by `_normalize_job_seed_int(config)` (`generation/synthesize.py:98-100`) and pinned by the Plan's `seed_envelope`. A second public seed input creates two sources of truth for the determinism envelope and invites a caller to pass DP-relevant material. Read the seed from `GenerationPlan.config_json` exactly as the parked code reads it from `config`.

The generation seed controls only post-DP synthetic sampling. It must never reach the OpenDP fitting adapter, and `fit_dp_snapshot` must not accept it.

Requirements:

- Reject a raw mapping with `TypeError`.
- Reject a Plan without `generation`.
- If the embedded configuration declares DP, require a reproduced `dp_verification` receipt.
- Consume only `GenerationPlan.config_json`, `PinnedStatisticalSpec`, and embedded snapshot payloads.
- Never call `_load_snapshot()` or open `source_path`.
- Pass the pinned artifact to the fidelity gate.
- Keep lower-level generation helpers private.

Retain the top-level `generate_tables` export, but make it Plan-only. This keeps the engine independent of CLI and platform callers. Those callers are responsible for calling `compile_plan` first.

## 5. Module-by-module work plan

### Step 1: Add and prove the OpenDP and dp-accounting dependencies

Files:

- `pyproject.toml`
- Dependency lock or generated metadata used by this repository
- New `tests/unit/quality/test_opendp_dependency.py`

Assertions first:

- `test_opendp_supported_python_and_core_mechanisms_import`
- `test_dp_accounting_composes_mixed_epsilon_and_epsilon_delta_certificates`
- `test_dp_stack_does_not_import_polars_or_opendp_extras`

Before:

- No OpenDP or dp-accounting dependency.
- Decoy owns mechanism and accounting calculations.

After:

- `opendp==0.15.1` and `dp-accounting==0.6.0` are core dependencies.
- The first test builds both section 4.4 and 4.5 chains under `contrib` alone and reads `map(1)` from each.
- The second test composes one scalar-epsilon certificate and one `(epsilon, delta)` certificate through `dp_accounting.pld` and reads back an epsilon at a fixed delta.
- The third test asserts that importing `decoy_engine.quality.dp` pulls in neither `polars` nor `opendp.extras`, and that `opendp[polars]` is absent from the resolved environment. This is the mechanical form of the section 3.1 STOP condition and it must land in this step, before any adapter code exists to violate it.
- No production adapter work starts until all three pass on the supported matrix.

### Step 2: Replace manual budget accounting

Files:

- `src/decoy_engine/quality/dp_budget.py`
- `tests/unit/quality/test_dp_budget.py`

Assertions first:

- `test_release_session_reports_dp_accounting_composed_privacy_loss`
- `test_certificates_come_from_measurement_maps_not_the_calibration_target`
- `test_release_session_refuses_unscheduled_query`
- `test_release_session_refuses_duplicate_release_of_one_query`
- `test_release_session_refuses_loss_report_before_schedule_is_complete`
- `test_release_session_query_schedule_is_column_order_independent`
- `test_release_session_budget_allocation_is_data_independent`
- `test_release_session_raises_dp_budget_infeasible_when_schedule_cannot_be_funded`

Before:

- `_Charge` and `PrivacyBudget` manually accumulate epsilon and delta.

After:

- Replace them with `OpenDpReleaseSession`, the single owner of the fit's privacy bookkeeping.
- The session receives a frozen public query schedule and the fit-wide `(epsilon, delta)` at construction, and runs the section 4.3.2 allocation search there. Construction touches no data.
- The session is the only place in the codebase that constructs or invokes an OpenDP `Measurement` (section 4.3.5 mitigation 1). It exposes a release method per scheduled query name; callers hand it a normalized value vector and get back the mechanism's output.
- For each release the session records the certificate read from `measurement.map(1)`, keyed by query name.
- The session composes those certificates through `dp_accounting.pld` and exposes `(epsilon_total, delta_total)` per section 4.3.4.
- The session refuses an unscheduled query name, a second release under a used name, and a loss report requested before every scheduled query has released.
- No mechanism formulas, no epsilon or delta arithmetic, and no manual composition remain in this module. The only numeric expressions permitted are the allocation policy of section 4.3.2, which operates on the request and the query counts, never on a mechanism output.

`test_certificates_come_from_measurement_maps_not_the_calibration_target` is the load-bearing one: it must prove that a certificate the session records equals `measurement.map(1)` for the object actually invoked, not the allocation target the session calibrated toward. Substitute a measurement whose map returns a value below the target and assert the recorded certificate follows the map.

If `PrivacyBudget` remains for compile-time policy ceilings, rename it to `ReleaseLedger` and make its role explicit. It may sum already certified release totals by release ID, but it must not claim to be the mechanism accountant.

### Step 3: Implement the OpenDP adapter and new fit API

Files:

- `src/decoy_engine/quality/dp.py`
- `src/decoy_engine/quality/__init__.py`
- `tests/unit/quality/test_dp.py`
- New `tests/unit/quality/test_dp_fit_contract.py`

Assertions first:

- `test_production_dp_fit_exposes_no_seed_or_rng_parameter`
- `test_independent_fits_mint_distinct_release_ids`
- `test_copied_snapshot_preserves_release_id`
- `test_dp_fit_rejects_missing_public_column_declarations`
- `test_dp_fit_rejects_overlapping_public_column_declarations`
- `test_dp_fit_kind_and_success_are_identical_across_30_31_distinct_neighbors`
- `test_dp_fit_all_null_declared_categorical_runs_measurement_and_emits_categorical_shape`
- `test_dp_fit_numeric_shape_and_charge_schedule_are_data_independent_for_all_null_and_all_inf`
- `test_categorical_release_order_uses_noised_counts_not_true_rank`
- `test_categorical_other_count_is_derived_only_from_noised_total_and_kept_counts`
- `test_adapter_never_rounds_or_compares_before_release`
- `test_dp_artifact_emits_no_exact_moments_or_quantiles`
- `test_null_count_is_derived_from_the_released_row_count_not_the_true_one`
- `test_dp_artifact_totals_are_the_accountant_result_not_the_request`
- `test_dp_artifact_records_opendp_and_dp_accounting_versions`
- `test_dp_fit_certificate_count_equals_query_count`
- `test_count_one_release_probability_upper_bound` (statistical, section 7.2)
- `test_independent_dp_fits_are_not_deterministic` (statistical, section 7.2)

Before:

- `apply_dp_noise` receives an exact snapshot.
- `_stable_histogram_threshold` computes Decoy’s threshold.
- `_noisy` rounds before categorical thresholding.
- Categorical order and `other_count` depend on exact private statistics.

After:

- Delete `_stable_histogram_threshold`.
- Delete manual Laplace sampling and manual threshold tests.
- Delete `apply_dp_noise`.
- Add `fit_dp_snapshot` with the binding contract from section 4.
- `quality/dp.py` normalizes values, drives `OpenDpReleaseSession` through the schedule, and serializes the result. It does not import `opendp` or `dp_accounting`. Both imports live in `quality/dp_budget.py` alongside the session.
- Add a private adapter seam, such as `_OpenDpBackend`, inside the session module for mechanism-level test doubles. A double supplies released values and a certificate; it never supplies an epsilon or delta that did not come from a map-shaped object.
- Production construction always uses unseeded OpenDP randomness.
- Test doubles return already released measurements. They do not accept a random seed.
- Add the OpenDP and `dp_accounting` source-pattern citations from section 3.2 to the module docstrings, in both `quality/dp.py` and `quality/dp_budget.py`.

Do not route the DP fit through `compute_distribution_snapshot`. That function materializes exact statistics and has value-dependent kind behavior unsuitable for DP.

### Step 4: Separate non-DP snapshot inference from DP fitting

Files:

- `src/decoy_engine/quality/snapshot.py`
- `tests/unit/quality/test_snapshot_dp_support.py`
- Existing callers of `compute_distribution_snapshot(..., dp_mode=...)`

Assertions first:

- `test_non_dp_snapshot_behavior_is_unchanged_after_dp_fit_split`
- `test_dp_fit_does_not_call_compute_distribution_snapshot`
- `test_private_values_cannot_change_declared_dp_kind`

Before:

- `compute_distribution_snapshot` contains partial DP branches.
- `_column_snapshot` emits `kind: empty`.
- `_stats_for` uses value and dtype behavior to select or reject kinds.

After:

- `compute_distribution_snapshot` is explicitly non-DP.
- Remove `dp_mode` and `numeric_domains` from it if no non-DP caller requires them.
- DP callers use `fit_dp_snapshot`.
- Exact snapshot behavior may retain `kind: empty` because it no longer participates in a DP guarantee.
- Update engine-owned tests and callers without adding caller-specific knowledge to the library.

### Step 5: Resolve the categorical configuration contradiction

Files:

- `src/decoy_engine/generation/statistical/_spec.py`
- `src/decoy_engine/plan/_checks_dp.py`
- `tests/unit/generation/statistical/test_spec.py`
- `tests/unit/generation/test_generate_dp_contract.py`

Assertions first:

- `test_dp_categorical_snapshot_compiles_without_allow_real_categories`
- `test_dp_configuration_rejects_allow_real_categories_true`
- `test_exact_categorical_snapshot_still_requires_allow_real_categories`
- `test_dp_exemption_ignores_unverified_dp_key_in_artifact`
- `test_dp_configuration_rejects_joint_or_condition_on`

Before:

- `_spec.py` requires `allow_real_categories`.
- `_checks_dp.py` rejects it and rejects categorical DP entirely.

After:

- Exact, non-DP categorical snapshots continue to require explicit real-category consent.
- A verified DP categorical label release does not require that consent.
- Under DP, `allow_real_categories: true` is rejected because raw real-category extraction is forbidden.
- The exemption is based on compiler-provided DP verification state, not merely an untrusted `dp` key inside JSON.
- Categorical DP itself is accepted.
- `high_cardinality`, joint snapshots, and `condition_on` remain explicitly rejected under DP.

`load_spec` no longer takes a path and no longer reads the artifact. Its new signature is:

```python
def load_spec(
    col_cfg: dict[str, Any],
    *,
    snapshot: Mapping[str, Any],
    dp_verified: bool,
) -> StatisticalSpec:
    ...
```

`snapshot` is the already-parsed, already-pinned artifact. `dp_verified` is computed by the compiler's DP verification pass and passed in; `load_spec` must never read `snapshot["dp"]` to decide the `allow_real_categories` exemption for itself. That is the whole content of "compiler-provided verification state": the artifact's own JSON claims nothing, the compiler's verdict does. This keeps the rule out of the library's guesswork without teaching the library about its callers, because `dp_verified` is a plain contract argument, not caller identity.

This forces a compile-order change. At `plan/_compile.py:189-198` the current order is `check_dp_categorical_unsupported`, `check_dp_generate_contract`, `check_statistical_columns` (which calls `load_spec`), then `check_dp_snapshot_provenance`. Under the new contract `load_spec` needs `dp_verified` before it runs, so DP verification must come first. The required order inside `compile_plan` is:

1. The single snapshot read-and-pin pass from section 4.7.
2. DP declaration parsing and fail-closed presence check (Step 6).
3. DP artifact verification and release-ID budget accounting over the pinned bytes, producing the per-path `dp_verified` verdict and the `DpVerification` receipt.
4. `check_statistical_columns` and every other snapshot-consuming check, each fed pinned bytes plus the verdict from step 3.

Preserve the existing error precedence within that reordering: a config that is both DP-declared and malformed in a non-DP way must still surface the same code it surfaces today. Add a test for each currently pinned precedence pair you move.

### Step 6: Make DP configuration fail closed and use release IDs

Files:

- `src/decoy_engine/plan/_checks_dp.py`
- `src/decoy_engine/plan/_errors.py`, if typed error codes live there
- `tests/unit/plan/test_checks_dp.py`
- `tests/unit/generation/test_generate_dp_contract.py`

Assertions first:

- `test_empty_dp_block_fails_closed_with_dp_budget_declaration_malformed`
- `test_non_mapping_dp_block_fails_closed`
- `test_distinct_release_ids_with_identical_released_values_compose_budget`
- `test_same_release_id_referenced_twice_is_charged_once`
- `test_same_release_id_with_different_digest_is_rejected`
- `test_dp_snapshot_without_release_id_is_rejected`
- `test_composed_release_budget_above_declared_ceiling_is_rejected`
- `test_dp_artifact_query_count_inconsistent_with_declared_columns_is_rejected`
- `test_dp_artifact_from_a_different_library_version_is_rejected`

Before:

- `_dp_settings` returns `None` for `dp: {}`.
- Content hashes serve as release identity.
- Validation reloads snapshot files.

After:

- Test key membership, not truthiness, and keep the non-mapping `global_settings` guard the parked code already has at `_checks_dp.py:69`:

  ```python
  global_settings = config.get("global_settings")
  if not isinstance(global_settings, dict) or "dp" not in global_settings:
      return None
  ```

  A missing or non-mapping `global_settings` means DP was never declared and is not an error. A present-but-malformed `dp` under a valid `global_settings` is always an error. Do not collapse those two cases.

- If `dp` is present, require a nonempty, valid mapping.
- Emit `dp_budget_declaration_malformed` for `{}`, null, lists, and incomplete mappings.
- Use `release_id` as the deduplication key.
- Track the artifact digest associated with each release ID.
- The same ID and same digest is charged once.
- The same ID and a different digest is `dp_release_id_conflict`.
- Different IDs always compose, regardless of equal bytes or equal released values.
- Compare the release-ID ledger total to the configuration ceiling.
- Recompute `query_count` from the artifact's own `dp.categorical_columns` and `dp.numeric_domains` and reject a mismatch.
- Reject an artifact whose `dp.opendp_version` or `dp.dp_accounting_version` differs from the running environment's. A receipt this build cannot reproduce is not a receipt it may accept.
- Accept already-read artifact bytes from the compiler. Do not open paths here.
- Delete the `support_origin == "caller"` eligibility rule at `_checks_dp.py:442-459` and the `dp_snapshot_numeric_support_data_dependent` code with it. `fit_dp_snapshot` does not emit `support_origin` (section 4.2.1), so leaving that rule in place rejects every artifact this build produces. Replace it with a check that the column appears in the artifact's own `dp.categorical_columns` or `dp.numeric_domains` under the right kind, which is the same guarantee sourced from the new provenance block.
- Replace the blanket categorical rejection at `_checks_dp.py:438-441` and `check_dp_categorical_unsupported` (`:147-221`). Categorical DP is now supported. Keep a rejection for every kind that is neither declared-numeric nor declared-categorical, so the allow-list stays default-reject.

Validation functions return verified data to the compiler or a new immutable value. They must not mutate configuration or parsed artifact mappings.

### Step 7: Pin configuration and snapshots into Plan

Files:

- `src/decoy_engine/plan/_types.py`
- `src/decoy_engine/plan/_compile.py`
- `src/decoy_engine/plan/_serialize.py`
- New `src/decoy_engine/plan/_generation.py`, mandatory. `plan/_compile.py` is already 690 lines at `fafbb75`, over the ~600 cap, so the read-and-pin pass, the `GenerationPlan` builder, and the recursive freeze all live in the new module. `_compile.py` gains call sites only and must come out of this step no larger than it went in.
- New `src/decoy_engine/plan/_checks_dp_budget.py` if the release-ID ledger work would push `plan/_checks_dp.py` (553 lines) past the cap
- `tests/unit/plan/test_generation_plan.py`
- `tests/unit/plan/test_serialize.py`

Assertions first:

- `test_compile_plan_reads_each_snapshot_path_once`
- `test_compile_plan_embeds_snapshot_bytes_and_digest`
- `test_compiled_generation_plan_is_recursively_immutable`
- `test_plan_yaml_round_trip_preserves_pinned_snapshot`
- `test_plan_from_yaml_rejects_pinned_snapshot_digest_mismatch`
- `test_plan_from_yaml_recomputes_dp_verification_receipt`

Before:

- `Plan` contains hashes and report data but not a generation capability.
- Snapshot paths remain runtime authority.
- Serialization cannot preserve compile-time snapshot identity.

After:

- Add `GenerationPlan`, `PinnedSnapshot`, `PinnedStatisticalSpec`, and `DpVerification`.
- Read snapshot bytes once during `compile_plan`.
- Parse, validate, digest, and pin the same bytes.
- Embed exact bytes, not a path-based promise.
- Recursively freeze parsed generation data.
- Bump the serialized Plan schema version.
- Permit older Plan versions to load for unaffected operations, but reject generation when no `GenerationPlan` exists.
- Revalidate embedded digests and DP receipts during deserialization.

Do not add this orchestration to `generation/synthesize.py`, which is exactly 600 lines in the parked branch.

Bumping the serialized Plan schema version is a breaking change for any Plan file already on disk. Because the engine is pre-GA (`decoy_engine.RELEASE_PHASE`), delete the old shape rather than translating it: a Plan at the older version loads for reporting and diagnostics but raises a typed error the moment generation is requested. Do not write a migration.

### Step 8: Make generation Plan-only

Files:

- `src/decoy_engine/generation/synthesize.py`
- `src/decoy_engine/generation/statistical/_spec.py`
- `src/decoy_engine/generation/statistical/_sample.py`
- `src/decoy_engine/generation/_fidelity_gate.py`
- `src/decoy_engine/__init__.py`
- `tests/unit/generation/test_generate.py`
- `tests/unit/generation/test_generate_dp_contract.py`
- `tests/unit/generation/test_fidelity_gate.py`

Assertions first:

- `test_generate_tables_rejects_raw_config_and_requires_compiled_plan`
- `test_generate_tables_rejects_plan_without_generation_payload`
- `test_generate_tables_uses_pinned_snapshot_after_file_swap`
- `test_generation_fidelity_gate_uses_pinned_snapshot_without_file_io`
- `test_dp_generation_requires_reproduced_verification_receipt`

Before:

- `generate_tables(config)` accepts unchecked mappings.
- Statistical sampling calls `load_spec`, which rereads a path.
- The fidelity gate rereads the path independently.

After:

- `generate_tables(plan)` accepts only `Plan`.
- A runtime type check rejects mappings.
- Statistical sampling receives `PinnedStatisticalSpec`.
- Fidelity checking parses the embedded payload or consumes an immutable compiled representation.
- No generation path opens `source_path`.
- The top-level export remains Plan-only.
- Private helpers may receive frozen table and column plans, not raw external configuration.

The TOCTOU test must compile a benign snapshot, replace the source path with a snapshot containing recognizable secrets, then prove that generated output and fidelity input still come from the pinned artifact. Monkeypatch path opening after compilation to fail so the test also pins the absence of runtime file I/O.

### Step 9: Update the internal pipeline and Decoy-owned callers

Files:

- `src/decoy_engine/execution/_pipeline.py`
- Engine examples and tests

`execution/_pipeline.py` is 645 lines at `fafbb75`. This step swaps the argument passed at `:472-476` and must not grow the module; if it would, extract into the existing `_psrc`-style helper module rather than inlining.

CLI and platform changes are out of scope for this branch. They are separate changesets in their own repositories, tracked as follow-ups, and no test in them gates this merge. Do not edit them from here.

Assertions first (engine only):

- `test_run_pipeline_passes_compiled_plan_to_generation`
- `test_pipeline_cannot_generate_after_config_only_checks`

Before:

- The pipeline compiles a Plan and later passes raw configuration to generation.

After:

- The pipeline passes the compiled Plan.
- `run_config_only_checks` does not produce a generation-capable object.
- Only `compile_plan` or validated Plan deserialization produces a generation-capable Plan.
- Engine modules do not inspect CLI flags or identify caller applications.

Follow-up work in the caller repositories, filed as issues from this branch and not built here: CLI and platform wiring collect the public declarations and pass them into `fit_dp_snapshot`, with mandatory `--categorical-columns`, `--numeric-domains`, and `--delta`, and callers compile a Plan before calling `generate_tables`. Because the engine break is real and pre-GA, file those issues before merging so the callers do not silently break.

## 6. Defect closure matrix

Every row names an observable the test reads. A row whose test would still pass with the defect reintroduced is not closed. The "what reintroducing the defect breaks" column exists so the implementer can check that property before writing the test, rather than after.

| Defect | Design closure | Required proving test | What the test observes, and what breaks if the defect returns |
|---|---|---|---|
| 1a, rank leakage | OpenDP releases retained pairs first; Decoy sorts only by released noised count and public label tie-breaker | `test_categorical_release_order_uses_noised_counts_not_true_rank` | The seam returns retained pairs whose released counts invert the true frequency order. The test asserts serialized `top_values` order matches the released counts. Sorting by true count, or preserving the mechanism's emission order, flips the assertion. |
| 1b, `other_count` leak | Compute `other_count` only as noised non-null total minus kept released counts | `test_categorical_other_count_is_derived_only_from_noised_total_and_kept_counts` | Two seam scenarios with identical released total and identical kept pairs but different suppressed inputs. The test asserts both produce byte-identical artifacts. Any dependence on suppressed values makes them differ. A single-scenario test that only checks the arithmetic would not catch this and is not sufficient. |
| 1c, rounded threshold | OpenDP owns exact noisy-value thresholding; serialization occurs afterward | `test_adapter_never_rounds_or_compares_before_release` and `test_count_one_release_probability_upper_bound` | Structural: the seam double records every value it receives and asserts each is the unrounded normalized input, and that `quality.dp` exposes no threshold helper (`not hasattr(dp, "_stable_histogram_threshold")` plus no module-level `tau`/`math.ceil` threshold arithmetic). Statistical: section 7.2. Restoring round-then-compare either changes the recorded inputs or lifts the release rate above the bound. |
| 1d, data-dependent fit success | Public declarations cover every column; DP fit never calls kind inference | `test_dp_fit_kind_and_success_are_identical_across_30_31_distinct_neighbors` | Emitted `kind`, key set, bin count, query-schedule names, and success-or-exception class across the 30 and 31 distinct-value neighbors. Any cardinality cliff changes one of them. |
| Finding 2, contradictory categorical consent | Verified DP categorical releases bypass exact-real consent; `allow_real_categories: true` remains forbidden under DP | `test_dp_categorical_snapshot_compiles_without_allow_real_categories`, `test_exact_categorical_snapshot_still_requires_allow_real_categories`, and `test_dp_exemption_ignores_unverified_dp_key_in_artifact` | The third test hand-writes a `dp` block into an otherwise exact snapshot and asserts compilation still demands consent. Without it, an implementation that reads `snapshot["dp"]` inside `load_spec` passes the first two tests while reopening the hole. |
| F1, all-null categorical bypass | Declared categorical kind and fixed query schedule apply even with zero non-null values | `test_dp_fit_all_null_declared_categorical_runs_measurement_and_emits_categorical_shape` | The measurement is invoked (recorded at the seam), the emitted `kind` is `categorical`, and the query schedule matches the non-empty case. A short-circuit that skips the mechanism changes the recorded call count. |
| F2, all-null/all-inf numeric disclosure | Public domain fixes kind, edges, shape, and query charge; total preprocessing prevents content-dependent failure | `test_dp_fit_numeric_shape_and_charge_schedule_are_data_independent_for_all_null_and_all_inf` | Bin edges, bin count, key set, and query schedule are identical across all-null, all-inf, and ordinary inputs under one declaration. The parked `finite.size == 0` branch at `snapshot.py:524-535` emits a different key set and would fail. |
| F3, generation bypass | Public generation entrypoint accepts a compiled Plan only | `test_generate_tables_rejects_raw_config_and_requires_compiled_plan` | `generate_tables(raw_config_mapping)` raises `TypeError` and produces no output. Reintroducing a mapping branch returns tables instead. |
| F4, snapshot TOCTOU | Plan embeds verified bytes and compiled spec; runtime never reopens the path | `test_generate_tables_uses_pinned_snapshot_after_file_swap` and `test_compile_plan_reads_each_snapshot_path_once` | Post-compile file swap plus a patched `open` that raises. Generated values match the original artifact and no read occurs. The second test counts opens per path so an in-compile swap window cannot reopen. |
| F5, content-hash undercharge | Deduplicate by fit-time release ID; distinct IDs always compose | `test_distinct_release_ids_with_identical_released_values_compose_budget` and `test_same_release_id_referenced_twice_is_charged_once` | Two artifacts whose released counts, epsilon, and delta are all equal but whose `release_id` differs. Charged total is 2x. Note that distinct release IDs can never produce identical bytes, since the ID is inside the artifact; the observable is identical released values, not identical bytes. A digest-keyed ledger charges 1x and fails. |
| F6, falsy `dp: {}` fail-open | Presence is detected by key membership; malformed present values fail closed | `test_empty_dp_block_fails_closed_with_dp_budget_declaration_malformed` and `test_non_mapping_dp_block_fails_closed` | Compilation raises `dp_budget_declaration_malformed` for `{}`, `null`, and a list. A truthiness check returns `None` and compiles clean. |
| A1, unscheduled or uncertified release (new with this revision, see section 4.3.5) | `OpenDpReleaseSession` is the sole OpenDP call site, refuses unscheduled and duplicate query names, refuses to report a loss before the schedule completes, and the fit asserts certificate count equals `query_count` | `test_release_session_refuses_unscheduled_query`, `test_release_session_refuses_duplicate_release_of_one_query`, `test_release_session_refuses_loss_report_before_schedule_is_complete`, `test_certificate_count_mismatch_guard_raises_dp_schedule_mismatch`, and the import-shape half of `test_dp_stack_does_not_import_polars_or_opendp_extras` | The session raises on an out-of-schedule name, on a repeated name, and on an early loss request; the fit raises when certificates and `query_count` disagree (`test_certificate_count_mismatch_guard_raises_dp_schedule_mismatch` forces that disagreement directly via monkeypatch, since the ordinary formula check `test_dp_fit_certificate_count_equals_query_count` alone never exercises the runtime guard); and no module outside `quality/dp_budget.py` imports `opendp`. The OpenDP Context used to refuse an extra query itself. It no longer exists, so a release path that skips the session is charged nothing and nothing outside Decoy notices. Deleting any one of these checks reopens that hole. |
| A2, hand-derived privacy parameter (new with this revision) | Every epsilon, delta, scale, and threshold is read from an OpenDP privacy map or a `dp_accounting` PLD; allocation policy operates only on the request and public query counts | `test_certificates_come_from_measurement_maps_not_the_calibration_target` and `test_dp_artifact_totals_are_the_accountant_result_not_the_request` | A substituted measurement whose map returns less than the calibration target: the recorded certificate must follow the map. And an artifact whose `epsilon_total` equals the accountant's composed result rather than the requested epsilon. An implementation that records the target, or that sums per-query epsilons itself, fails both. |
| C-B2b, complex64 silently keeps its real part (new with round 4) | Complex detection is by dtype kind, not `isinstance(raw, complex)` alone, so every numpy complex width is unconvertible | `test_every_complex_width_is_unconvertible_not_silently_real` and `test_real_numeric_types_survive_the_complex_guard` | `numpy.complex128` subclasses Python's `complex` and `numpy.complex64` does not, so the round-3 `isinstance` guard dropped one width and let the other through `float()`, which silently returns the real part. The test is parametrized over all three widths precisely because the single-value version passed on the two that subclass `complex`. The second test pins the negative case, so a guard widened until it also rejects ordinary numpy reals fails rather than silently emptying real columns. |
| C-B4, fit success as a disclosure channel (new with round 4) | Normalization is total against exceptions, not only against warnings: any failure deciding nullness, converting, or rendering a row's value drops that row, and categorical labels must be UTF-8 encodable at the release boundary | `test_normalization_is_total_over_scalars_that_raise_on_conversion`, `test_null_check_that_raises_on_content_does_not_take_the_fit_down`, `test_categorical_labels_that_cannot_be_encoded_are_dropped_not_raised`, `test_fit_succeeds_identically_on_neighbours_differing_by_a_hostile_row`, and `test_per_row_null_exclusion_matches_dropna_semantics` | Fit success is itself an observable, so a neighbour that raises where its partner emits an artifact has an observable with probability 0 on one and 1 on the other, which breaks `(epsilon, delta)` for any `delta < 1` before any released number is considered. The hostile scalars each raise a DIFFERENT exception type from `float()` and `str()`, so narrowing either handler back to an enumerated tuple fails rather than passing on the one type it names. The three guards were each mutated back individually and the corresponding test failed. The last row pins that making null exclusion per row did not change which values count as null. |
| C-B5, categorical labels unstable under adjacency (round 6, reopened and closed wider in round 7; the only defect in this program that broke the GUARANTEE, not a test or an error contract) | A label is a function of the value's FLOAT64 IMAGE, not of the column's storage width, and only `str`/`bool`/reals are labelled at all -- every other type drops, which is coercion-invariant where labelling is not | `test_categorical_normalization_is_multiset_recordwise` (the dtype x added-row matrix), `test_categorical_labels_are_stable_when_a_null_upcasts_the_column` (parametrized over magnitude, including beyond 2**53), `test_unlabellable_types_drop_rather_than_raise`, and `test_canonical_label_merges_integral_reals_but_preserves_everything_else` | pandas upcasts an integer column to float64 the moment a null enters it, so under plain `str()` adding ONE row replaced EVERY label (`ints 0..7` -> `["0".."7"]`, plus a null -> `["0.0".."7.0"]`). Round 6 rendered integral reals as integer strings, which closed ONLY that magnitude range: above 2**53 the upcast has already destroyed the value, so the `Integral` path rendered the exact int on one side and the `Real` path the rounded image on the other. dennis round 7 reproduced it end-to-end -- 1200 rows of three 19-digit IDs released three labels at 400 each, and one added null row released ONE label at 1200, multiset distance 2400 against `map(1)`. Codex round 7 independently reproduced the same class on `timedelta64`/`datetime64` columns, where one incompatible row forces `object` and every shared label changes spelling. Both are now closed: reals canonicalize through the float64 image (so both sides ask the same question of the same lossy image), and unlabellable types drop. Dropping rather than raising is deliberate -- raising would reopen the C-B4 fit-success channel. The tests assert a MULTISET bound, not a length bound: the round-6 tests compared cardinality, so neighbours of equal length with every element differing passed them, which is the structural reason this class recurred twice. |
| C-B6, `dp: null` fail-open through a non-dict mapping (new with round 6) | The before-validator matches `collections.abc.Mapping`, not `dict` | `test_explicit_dp_null_is_refused_through_any_mapping_type` | Pydantic accepts any mapping, so a `UserDict` carrying an explicit `dp: None` validated, and `exclude_none`/`exclude_defaults` then erased the key so `_dp_declared` returned False. A built-in dict CANNOT falsify this, so the test is parametrized over the mapping type; the unparametrized version passed while the hole was live. |
| C-H2b, leaked cardinality predicate (new with round 6) | Closure tests compare the artifact's public part (released/noised keys stripped), not its key set | `test_dp_fit_kind_and_success_are_identical_across_30_31_distinct_neighbors` | A leak of the form `over_30_distinct = len(set(values)) > 30` adds the key UNCONDITIONALLY, so both neighbours carry it and only the VALUE differs; comparing key sets cannot see it. These two fixtures are the only ones in the suite that straddle such a threshold, so this is where it must be caught. |
| C-M2, schema and validation disagreed about `dp` (new with round 6) | The advertised JSON schema says what validation accepts: the model, or the key absent | `test_advertised_dp_schema_matches_what_validation_accepts` | Both schema modes advertised `DpGenerateSettings | null` with default `null` while validation refuses an explicit null, so a schema-driven client could emit a schema-valid config pydantic rejects. The earlier tests compared property NAMES only and passed while the two disagreed. |
| D-MA, serialization schema collapsed by the `dp` serializer (new with round 5) | `__get_pydantic_json_schema__` re-asks the handler with `serialization` stripped, restoring the field-enumerating schema | `test_serialization_mode_schema_still_enumerates_fields` and `test_validation_and_serialization_modes_agree_on_field_names` | A `model_serializer(mode="wrap")` makes pydantic discard the model's serialization schema, so `GlobalSettings` became `{}` and `DpGenerateSettings` vanished from `$defs`. Latent (no consumer reads that schema today) but a real core-schema regression introduced by the `dp` omission fix, and nothing covered it. |
| D-MB, derived bin edges could degenerate (new with round 5) | The interior edges are derived from the PUBLIC declaration in `_validate_config` and required to be finite and strictly increasing | `test_domains_whose_derived_bin_edges_degenerate_raise_a_coded_error` and `test_ordinary_domains_still_fit` | Finite `lower < upper` passed, but the derived edges could overflow or collapse, and OpenDP then rejected them at the FFI with a raw `OpenDPException`, AFTER `release_row_count` had already charged the session. Not a privacy channel (the outcome is a function of the declaration alone). The second test exists because the first cut of the guard used `zip(full, full[1:], strict=True)`, whose operands differ in length by one, and raised on EVERY fit. |
| D-LA, non-string column labels and declarations (new with round 5, completed in round 6) | Both non-string frame labels and non-string declaration keys are rejected with `dp_column_label_not_a_string` | `test_non_string_frame_column_labels_are_rejected` and `test_non_string_column_declarations_are_rejected` | Validation compared stringified label sets while the fit indexed with the stringified name. Round 5 closed the FRAME side only: a non-string `numeric_domains` key still died on a bare `KeyError` inside the fit, and a non-string `categorical_columns` member SUCCEEDED SILENTLY, since that path only needs the name. The second test is parametrized over all three shapes. |
| D-L1, declarations read more than once (new with round 6) | The caller's declarations are snapshotted once at entry; validation and the fit both read only that snapshot | `test_declarations_are_read_once_so_a_drifting_mapping_cannot_slip_past` | They were read three times (validation, schedule construction, fit loop), so a `Mapping` whose reads differ passed every check and then handed OpenDP different bounds, landing a raw `OpenDPException` after the row-count release had charged the session. Validate what you use. |
| C-M1b, singleton containers dropped as null (new with round 5) | Only a genuine scalar verdict (`ndim == 0`) may exclude a row | `test_container_cells_are_present_whatever_their_length` | `pd.isna` on a container returns an ARRAY of per-element verdicts, not a verdict about the cell. `bool()` raises for a multi-element array, so those cells stayed present by accident, but for a SINGLETON it returned that one element's verdict and dropped `[None]` and `numpy.array([numpy.nan])`, which `dropna()` keeps. Parametrized across lengths because the `[1, 2]` fixture took the raising path and passed. |
| F7, documentation overclaim | Claims state approximate marginal DP, pinned-Plan boundary, composition, and joint exclusion | `test_dp_claim_copy_is_marginal_and_names_joint_exclusion` | The canonical limitations page contains the required semantic phrases and none of the removed Option A claims. The test lands with the code, not after it. It is a repo test, not a sign-off gate; see section 9.9. |

## 7. Test plan

### 7.1 Deterministic unit tests at the adapter seam

Use a private OpenDP backend protocol or narrow factory seam to return predetermined released values. These tests may control released values, but they must not seed or replace production randomness.

Use this seam to prove:

- Output order follows released noised counts when it contradicts the exact rank.
- `other_count` ignores suppressed noisy counts.
- Serialization rounds only values already retained by OpenDP.
- The public fit signature has no seed or RNG.
- Query schedule names and counts derive only from declarations.
- Release metadata records the accountant's composed loss, and each certificate records its measurement's map result.
- Budget allocation is a pure function of `(epsilon, delta, numeric_column_count, categorical_column_count)`. Run the allocation twice over two datasets that differ in every value under one declaration and assert identical scales, thresholds, and certificates.
- Value normalization is recordwise. Feed a fixture through the normalizer and assert `len(output) == len(input)` for the row-count vector, and that removing one input row removes at most one element from every derived column vector. This is the one stability claim Decoy makes on its own (section 3.3 item 1), so it needs a test rather than an argument.

Do not mock private data into a hand-written threshold implementation. Decoy must have no such implementation to test.

### 7.2 Unseeded statistical mechanism tests

Add:

- `test_count_one_release_probability_upper_bound`
- `test_independent_dp_fits_are_not_deterministic`

Both run against production entropy. Neither may be seeded, so both must be designed so that a correct implementation fails less often than the stated false-failure budget. Fix that budget once, as a module constant, at `1e-6` per test per run, and derive every other constant from it. Record the derivation in a comment so a later reader can recheck the arithmetic instead of tuning the numbers until the test goes green.

The count-one test must:

1. Fix a public configuration and an input in which exactly one label has count one.
2. Fix the trial count `N` and the one-sided confidence level `1 - alpha` with `alpha = 1e-6` as test constants.
3. Run the real OpenDP threshold mechanism `N` times and count releases of the count-one label.
4. Compute a one-sided upper Clopper-Pearson bound on the release probability at level `1 - alpha`.
5. Assert **only** that this upper bound does not exceed the measurement's certified delta times a documented slack factor.
6. Report `N`, releases, observed rate, the bound, and the certified delta on failure.
7. Carry a `statistical` marker if its runtime is unsuitable for every unit-test invocation, and still run in the required CI job.

Step 5 is one-sided on purpose. A correctly calibrated threshold mechanism may release a count-one label far less often than delta, including never in `N` trials. Do not assert a two-sided band, a lower bound, or "close to delta"; that assertion fails on a correct implementation and the natural fix is to weaken the threshold, which is the exact defect 1c describes. Compare against the **certified** delta of the thresholded grouping measurement itself, the second element of its `map(1)`, not the fit-wide delta and not the allocation target from section 4.3.2. The certificate is normally below the allocation target because the threshold search lands on an integer. Using the fit-wide delta makes the bound loose enough to miss a real regression when several queries are scheduled; using the allocation target makes it loose by the rounding gap. using the fit-wide delta makes the bound loose enough to miss a real regression when several queries are scheduled. Choose `N` so that the bound at `alpha = 1e-6` is tight enough to catch a mechanism whose effective release probability is an order of magnitude above the per-query delta, and state that sensitivity target in the test docstring.

`test_independent_dp_fits_are_not_deterministic` must not compare two fits for inequality on a single scalar; independently noised discrete counts collide often enough to make that flaky. Run `k = 5` independent fits over a fixture with at least 20 released numeric bin counts, and assert that the `k` released-count vectors are not all identical. Justify in a comment why the collision probability under the configured noise scale is below `1e-6`; if the fixture cannot meet that, widen the fixture rather than lowering `k`.

Do not seed either test. Do not assert an exact release count. These tests detect gross calibration regressions such as the previous 0.5 threshold shift and a fully broken noise source; neither is a proof of DP.

If a statistical test fails, the response is investigation, not a retry decorator. Do not add `flaky`, retries, or a widened tolerance without first showing that the calibration and the confidence arithmetic are both correct.

### 7.3 Disclosure-channel regressions

Add paired-input tests in which private values change but public declarations remain fixed:

- 30 versus 31 distinct categorical values.
- All-null versus one non-null categorical value.
- All-null versus all-inf versus ordinary numeric values.
- Same input fitted twice independently.
- Two copied references to one artifact.
- Two independent releases whose **released values** are forced equal at the adapter seam. Their bytes still differ, because each carries its own `release_id`; that is the point of the test.

Compare observable schema, success or failure class, query schedule, and charged release IDs. Do not require independently noised released values to match.

Add one more pair that the parked defects make necessary and that the list above does not cover: a declared-categorical column whose values are all distinct (every label has count one) versus one with a single repeated label. Both must emit the same schema and the same query schedule, and neither may fail. The first case is where a naive implementation retains nothing and then divides by zero, or falls back to exact values.

### 7.4 Entrypoint and pinning PoCs

The entrypoint-bypass test must attempt the exact prior attack:

1. Construct a raw config referencing a snapshot containing recognizable real values.
2. Call the public `generate_tables`.
3. Assert the call fails before any output is produced.

The TOCTOU test must:

1. Compile a config with a known snapshot.
2. Retain the returned Plan.
3. Replace the file contents at the same path.
4. Call `generate_tables(plan)`.
5. Assert generation uses only the original embedded snapshot.
6. Assert no path read occurs after compilation.

Add the equivalent no-file-I/O assertion for the fidelity gate.

### 7.5 Compile-time budget tests

Cover:

- Same ID and same digest referenced repeatedly charges once.
- Same ID and different digest fails.
- Different IDs and identical released counts, epsilon, and delta compose. Two artifacts cannot share a digest while carrying different release IDs, since the ID is inside the artifact; construct this case by fixing the released values at the seam and letting the IDs differ.
- Missing ID fails under the v2 DP schema.
- Composed epsilon above the declared ceiling fails.
- Composed delta above the declared ceiling fails.
- `dp: {}`, `dp: null`, incomplete DP settings, NaN budget values, and negative budget values fail closed.
- An artifact whose `query_count` disagrees with its own declared columns fails.
- An artifact whose recorded `opendp_version` or `dp_accounting_version` differs from the running environment fails.

Use `math.fsum` or `Decimal` for compile-time ceiling summation and define one conservative comparison tolerance. This arithmetic checks a policy ceiling over certified releases; it does not replace the fit-time accountant of section 4.3.4, and it must never recompute or adjust an artifact's recorded totals.

### 7.6 Full verification

Before review, run the repository’s required formatting, linting, type-checking, and complete test suite from `CONTRIBUTING.md`. Then run:

1. The unseeded statistical suite multiple times.
2. The supported Python matrix, confirming the resolved `polars` version is unchanged and that neither `polars` nor `opendp.extras` is imported by the DP path.
3. Plan serialization round trips.
4. The exact entrypoint-bypass and file-swap PoCs.
5. Tests with DP categorical-only, numeric-only, and mixed fits.
6. Tests with no retained categorical labels.
7. Tests with multiple columns sharing one release artifact.
8. Tests with multiple independent release artifacts.

Do not weaken a statistical test because one run fails. Investigate whether the confidence calculation, mechanism calibration, or trial count is wrong.

## 8. Documentation and claim wording

### 8.1 `docs/what-we-cannot-prove.md`

Replace the Option A numeric-only and categorical-unsupported language. Remove any statement that:

- Generation may safely reread the same path.
- A content hash identifies a privacy release.
- Numeric support implies joint protection.
- Categorical support reveals only values already safe to disclose.
- Successful compilation alone protects callers that bypass the compiled Plan.

Document these limits:

- The mechanism provides approximate `(epsilon, delta)` DP.
- The protected release scope is single-column marginals.
- Numeric domains and column kinds are public metadata.
- Categorical label discovery consumes privacy budget.
- Cross-column correlation and conditional synthesis are not protected.
- DP generation requires a DP-verified pinned Plan.
- Independent release IDs compose.
- Repeated references to the same exact release ID are charged once.
- Serialized Plan files are integrity-checked internally but not authenticated against hostile replacement.
- A Plan embedding an exact non-DP snapshot inherits that snapshot’s sensitivity.
- DP artifacts reveal the labels and counts deliberately released by OpenDP.
- Each column's privacy loss is computed by OpenDP's own privacy map, and the fit-wide loss is composed by Google's `dp_accounting` from those per-column certificates. The composition uses a dominating-pair representation of each certified `(epsilon, delta)`, so the reported total is a valid upper bound rather than the tightest achievable one.
- The fixed query schedule is enforced by Decoy, not by the DP library. Earlier drafts of this feature planned to route every release through an OpenDP compositor that would itself refuse an unscheduled query; that route is unavailable (it requires a Polars version Decoy does not run). A defect in Decoy's release session could therefore under-count a release, and no library would catch it. The mitigations are a single release call site, refusal of unscheduled and repeated queries, and an assertion that certificates match the declared schedule length.
- A DP artifact’s recorded `epsilon_total` and `delta_total` are self-declared. Decoy verifies that the artifact is internally consistent and that its release ID is unique, but a hand-edited artifact can understate what it actually spent. The compile-time ceiling check is a policy control over artifacts Decoy itself produced, not a defense against a caller who edits their own artifacts.

### 8.2 Exact claim sentences

These are the claim sentences the shipped documentation must carry. They are the technically reviewed wording; write them as-is when the code lands.

> For a DP fit whose declared fit-wide privacy loss is `(epsilon, delta)`, Decoy’s released single-column numeric and categorical marginals, and synthetic columns generated solely as post-processing of a DP-verified pinned Plan, are covered by that fit’s approximate `(epsilon, delta)` differential privacy guarantee under add-or-remove-one-row adjacency.

> This guarantee is marginal only. It does not cover joint distributions, cross-column correlations, conditional sampling, masked outputs, non-DP snapshots, or forged artifacts.

> When several independent release IDs are consumed, their privacy losses compose; repeated references to the same release ID are charged once, and conflicting artifacts carrying one release ID are rejected.

Product review of this wording happens in the pre-release docs pass, not here, and does not gate this merge. If that review changes the wording, rerun the technical review against the replacement. Under no revision may the copy omit “approximate,” “marginal,” or the joint exclusion; `test_dp_claim_copy_is_marginal_and_names_joint_exclusion` (section 8.4) enforces that mechanically and lands with the code.

### 8.3 CHANGELOG

Record:

- OpenDP 0.15.1 as the mechanism dependency and Google `dp-accounting` 0.6.0 as the composition accountant.
- Numeric and categorical marginal support.
- The accepted breaking `fit_dp_snapshot` contract.
- Required public categorical and numeric declarations.
- Required explicit delta.
- Removal of `apply_dp_noise`.
- Plan-only `generate_tables`.
- Snapshot pinning and Plan schema version change.
- Release-ID budget identity.
- Joint and conditional DP remaining unsupported.

### 8.4 Documentation assertion

Add `test_dp_claim_copy_is_marginal_and_names_joint_exclusion`. It must assert that the canonical DP limitations page contains:

- `approximate (epsilon, delta)` or the project’s rendered equivalent.
- `single-column marginal`.
- An explicit denial of joint and cross-column guarantees.
- The pinned-Plan boundary.
- Release-ID composition language.

The test should protect required semantic phrases, not the entire prose verbatim.

## 9. Risks, open engineering questions, and STOP conditions

### 9.1 OpenDP `contrib` status

The required stable-key categorical machinery is exposed through OpenDP’s `contrib` feature. This is an accepted consequence of the locked OpenDP choice, but it raises review requirements.

Enable only `contrib`. Every constructor in sections 4.4 and 4.5 has been verified to build and run under `contrib` alone. Do not enable `honest-but-curious`, construct user-defined measurements, or bypass OpenDP checks. Record the exact OpenDP constructors used in `quality/dp_budget.py` and submit those calls to dennis for adversarial review.

Stop if the implementation requires copying an OpenDP formula into Decoy, or if any required constructor turns out to need `honest-but-curious`.

### 9.2 Cross-library composition

The spike must demonstrate that each numeric chain, each categorical grouping chain, and each count chain reports its own loss through `Measurement.map(1)`, and that `dp_accounting`'s PLD composition over those reported losses returns a fit-wide epsilon at the requested delta.

Stop if:

- Any measurement's loss cannot be read back from `map(1)` on the invoked object.
- A release path exists that does not go through `OpenDpReleaseSession`.
- The adapter cannot prove every scheduled query released exactly once.
- Budget allocation depends on observed values.
- The composed loss exceeds the requested fit budget for a schedule the allocation policy declared feasible.
- A manual threshold, a manual scale formula, or manual floating-point composition appears necessary.

Composition is the seam this revision introduces. Section 4.3.5 states what it costs and section 6 rows A1 and A2 pin it. Treat any change to how certificates are produced, recorded, or composed as a privacy change requiring the same review as a mechanism change.

### 9.3 Polars must not move

Resolved and closed; see section 3.4. `opendp[polars]` pins `polars==1.36.1` exactly, Decoy resolves 1.42.0, and the mismatch is a hard FFI failure at Context construction. This build has no Polars dependency of its own.

Stop and escalate before changing Decoy’s existing `polars>=1,<2` constraint, adding `opendp[polars]`, or importing `opendp.extras` for any reason. Adding a Polars-integrated OpenDP path is a redesign of section 4.3, not an implementation detail, and the implementer may not make that call. `test_dp_stack_does_not_import_polars_or_opendp_extras` (step 1) is the mechanical guard; do not skip or weaken it.

### 9.4 Value normalization

The exact canonical categorical string policy is not yet specified in the parked implementation. Define it in one helper and test it before mechanism code. It must be deterministic, recordwise, locale-independent, and valid for every supported scalar dtype.

Stop if preprocessing can fail based on a private value after public configuration validation. Add a total normalization rule or narrow the public supported-type contract without inspecting cardinality.

### 9.5 Empty categorical releases

A DP categorical release may retain no labels. Do not recover labels from the input, lower the threshold, or retry until a label appears.

Support the already defined other token when configuration permits it. Otherwise return a typed compile error based only on the DP release.

### 9.6 Plan size and sensitivity

Pinning full artifacts increases Plan size and may embed sensitive exact snapshots in non-DP workflows. This is required for the generation capability but must be documented.

The embedded-artifact cap is 16 MiB per snapshot payload, following the HC-5 precedent for a fail-closed size fence. Exceeding it is a typed compile error, not a truncation and not a fallback to a runtime path read.

Stop if existing Plan transport imposes a limit below that cap. Escalate a bounded embedded-artifact format rather than reverting to runtime path reads.

### 9.7 Release-ID handling

Release IDs are privacy-ledger identities, not authenticity tokens. Do not derive them from contents, timestamps, paths, row counts, or random noise. Do not regenerate an ID when copying an artifact.

Stop if an existing caller rewrites snapshot JSON in a way that claims to preserve one release while changing its digest. Treat that as a conflicting artifact and require a new independent fit.

### 9.8 Unsupported joint configuration

Reject `condition_on`, joint snapshots, joint columns, and future joint-mechanism names with a typed `dp_joint_unsupported` error.

Do not accept and ignore these fields. Do not synthesize columns independently while claiming the joint configuration was honored.

### 9.9 Completion gate

The change is not complete until:

1. Every named assertion test lands before its corresponding production change.
2. All BLOCKER and HIGH findings from dennis are resolved.
3. The exact bypass, TOCTOU, collision, all-null, all-inf, falsy-DP, rank-order, and count-one calibration regressions pass.
4. The supported Python dependency matrix passes with both `opendp==0.15.1` and `dp-accounting==0.6.0`, and Decoy's resolved `polars` version is unchanged from before this branch.
5. Documentation is synchronized to the shipped behavior in the same changeset: `docs/what-we-cannot-prove.md` and the CHANGELOG carry the section 8 wording, every Option A overclaim listed in section 8.1 is gone, and `test_dp_claim_copy_is_marginal_and_names_joint_exclusion` passes. This is barry's normal post-ship pass over shipped behavior, not a review gate. Decoy is pre-release and docs are kept best-effort current, with the full docs review happening before release, so no doc sign-off blocks this merge. What does block it is F7: the code must not land while the docs still claim more than the code delivers, and the assertion test is what proves that.
6. The final cross-model gate confirms that generation has no public raw-config path and no runtime snapshot-path read, that `OpenDpReleaseSession` is the only OpenDP call site, and that no module imports `polars` or `opendp.extras` on the DP path.

### 9.10 Decisions settled by a human, closed

Nothing in this guide is owed a human decision. These three were open in the previous revision and are now answered. Implement them as written; do not reopen them.

1. **`numeric_bins` default stays 10**, and the value used is recorded in the artifact's `dp` block so bin edges are reproducible from public metadata alone.
2. **`delta = 0` is rejected** at the fit API with a typed error, for numeric-only fits too. Uniformity wins over the marginally stronger pure-epsilon claim a numeric-only fit could have carried. Section 4.2 states this as a contract requirement.
3. **Embedded artifacts are capped at 16 MiB** per snapshot payload, following the HC-5 precedent, enforced as a typed compile error. Section 9.6 states it.

The dependency-shape question that the previous revision's spike opened is also closed; see section 3.4.
