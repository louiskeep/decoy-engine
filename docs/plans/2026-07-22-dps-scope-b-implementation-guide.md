# Decoy DPS Scope B implementation guide

## 1. Context and non-goals

This rebuild replaces the parked Option A Differential Privacy Synthesis implementation at `feat/dps-option-a` commit `fafbb7500fa97dd8b5fa5a5b1fee01324a1dc713`.

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

## 3. Dependency decision

### 3.1 Package selection

Add this core runtime dependency:

```toml
opendp==0.15.1
```

Use an exact pin for this rebuild because the implementation depends on privacy-critical APIs currently marked `contrib`, including stable-key thresholded grouping. Upgrade only through a separate dependency review with mechanism regression tests.

Do not request the `opendp[polars]` extra initially. The repository already declares `polars>=1,<2`, while the OpenDP extra may constrain Polars more narrowly. Import OpenDP’s Polars integration against Decoy’s resolved dependency set during the dependency spike.

OpenDP 0.15.1 declares Python 3.10 or newer and publishes wheels covering the Python 3.10 through 3.12 range declared at `pyproject.toml:13` (`requires-python`) and `:21-39` (classifiers). Its published license is MIT, which is compatible with Decoy’s Apache-2.0 license. See [OpenDP 0.15.1 on PyPI](https://pypi.org/project/opendp/0.15.1/).

Use only dependencies licensed under Apache-2.0 or MIT for this feature.

### 3.2 Mandatory resolution spike

Complete this spike before editing `quality/dp.py`:

1. For Python 3.10, 3.11, and 3.12, ask pip to resolve a binary distribution without allowing an sdist:

   ```bash
   dps_wheel_dir="$(mktemp -d)"
   python3.10 -m pip download --only-binary=:all: --no-deps \
     --dest "$dps_wheel_dir/py310" "opendp==0.15.1"
   python3.11 -m pip download --only-binary=:all: --no-deps \
     --dest "$dps_wheel_dir/py311" "opendp==0.15.1"
   python3.12 -m pip download --only-binary=:all: --no-deps \
     --dest "$dps_wheel_dir/py312" "opendp==0.15.1"
   ```

2. In clean virtual environments for every available supported interpreter, install Decoy with its development dependencies plus `opendp==0.15.1`.

3. Run a smoke program that:

   - Imports `opendp.prelude`.
   - Enables only the OpenDP `contrib` feature.
   - Builds and invokes a bounded numeric histogram query.
   - Builds and invokes an unknown-key categorical count query.
   - Uses one OpenDP Context compositor for multiple releases.
   - Reads the compositor’s accumulated privacy loss.
   - Demonstrates that an additional unscheduled query cannot exceed the declared budget.

4. Add the same import and construction smoke to the supported CI matrix. A successful local wheel download is not proof that every CI platform resolves.

5. Record the exact Polars version resolved in the spike output. Do not tighten Decoy’s Polars constraint unless the current range fails the smoke test.

The relevant OpenDP patterns are its [bounded histogram example](https://docs.opendp.org/en/stable/api/user-guide/transformations/aggregation-quantile.html), [thresholded noise mechanism](https://docs.opendp.org/en/stable/api/user-guide/measurements/thresholded-noise-mechanisms.html), and [grouping compositor](https://docs.opendp.org/en/stable/getting-started/tabular-data/grouping.html). Cite these patterns in the implementing module’s docstring as required by `CLAUDE.md`.

### 3.3 Accountant choice and fallback

Use the OpenDP Context compositor as both the mechanism host and fit-time privacy accountant. Build the complete query schedule from public declarations before reading values. Give the compositor the fit-wide `(epsilon, delta)` limit and a fixed number of scheduled queries. Every mechanism release must go through that compositor.

Do not invoke a resolved OpenDP `Measurement` directly after creating the Context. That would bypass the accountant.

Do not retain `PrivacyBudget.charge()` arithmetic as the mechanism accountant. Compile-time summation of already certified release losses remains a policy ceiling check, not a replacement mechanism accountant.

Google `dp-accounting` is not an active fallback for this implementation. Stable `dp-accounting==0.6.0` does not provide the verified generic approximate-DP event support needed to replace OpenDP’s mixed thresholded-mechanism composition. It may become an accounting-only fallback after a stable release provides that support and passes Decoy’s Python, license, and composition tests. It cannot replace OpenDP’s mechanisms.

Stop before adapter work if:

- A supported Decoy platform cannot resolve an OpenDP binary wheel.
- OpenDP cannot construct the mixed numeric and categorical schedule under one compositor.
- OpenDP’s required Polars integration conflicts with Decoy’s supported dependency range.
- The accountant cannot report a composed loss bounded by the requested `(epsilon, delta)`.

Do not respond to those failures with manual Laplace noise, manual threshold calibration, or manual floating-point composition.

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
        | OpenDP Context compositor
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
dtype   public schema metadata, taken from the frame's declared dtype label
        (internal.pandas_compat.canonical_dtype_label), never from values
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
bin_edges   from the declared public domain and numeric_bins; never from data
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

### 4.3 Fixed query schedule

Construct the schedule before examining column values:

```text
1 table row-count query
1 bounded histogram query per numeric column
2 queries per categorical column:
  - thresholded unknown-key grouped count
  - noised non-null total
```

Thus:

```text
query_count = 1 + numeric_column_count + 2 * categorical_column_count
```

Use a single OpenDP Context compositor with a public, deterministic query order:

1. Table row count.
2. Numeric columns sorted by column name.
3. Categorical columns sorted by column name, with grouped count before non-null total.

The Context receives the fit-wide `(epsilon, delta)` and `split_evenly_over=query_count`, unless the dependency spike proves that a different public fixed weight vector is required by the stable grouping API. Any alternative weighting must be written as a constant policy, tested against column order, and documented before implementation. It may not depend on values, nullness, cardinality, or mechanism output.

After all scheduled queries, obtain the actual composed privacy loss from the OpenDP compositor. Assert that it does not exceed the requested `(epsilon, delta)` before serializing the artifact.

### 4.4 Numeric marginal

For every declared numeric column:

1. Normalize values recordwise.
2. Clamp finite and infinite numeric values to the declared public domain.
3. Exclude normalized nulls.
4. Create fixed public bin edges from the domain and `numeric_bins`.
5. Use OpenDP’s bounded histogram/count construction.
6. Include every public bin in the output even when its released count rounds to zero.
7. Convert released counts for serialization with:

   ```python
   max(0, int(round(noisy_count)))
   ```

8. Derive any displayed total, distinct-bin count, or null count only from released quantities.

All-null and all-inf columns therefore have the same kind, bin edges, output shape, query count, and budget schedule as any other values under the same declarations.

### 4.5 Categorical marginal

For every declared categorical column:

1. Normalize values recordwise without inspecting cardinality.
2. Exclude nulls from label grouping.
3. Submit unknown-key grouped counts through OpenDP’s stable thresholded grouping mechanism.
4. Submit a separate noised non-null total through the same compositor.
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
    "epsilon_total": 1.0,
    "delta_total": 0.000001,
    "accountant": "OpenDP Context compositor",
    "opendp_version": "0.15.1",
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

The artifact must not contain exact row counts, exact distinct counts, suppressed label names, suppressed noisy counts, inferred kinds, or an RNG seed. It must not carry a per-release `charges` breakdown of the kind `quality/dp.py:284` emits today; the fit-wide `(epsilon_total, delta_total)` reported by the compositor is the whole receipt, and a per-query breakdown invites a consumer to re-derive a per-column claim the scope does not support.

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

### Step 1: Add and prove the OpenDP dependency

Files:

- `pyproject.toml`
- Dependency lock or generated metadata used by this repository
- New `tests/unit/quality/test_opendp_dependency.py`

Assertion first:

- `test_opendp_supported_python_and_core_mechanisms_import`

Before:

- No OpenDP dependency.
- Decoy owns mechanism and accounting calculations.

After:

- `opendp==0.15.1` is a core dependency.
- The test imports the required API and constructs numeric, categorical, and composed measurements.
- No production adapter work starts until the wheel and compositor spike passes.

### Step 2: Replace manual budget accounting

Files:

- `src/decoy_engine/quality/dp_budget.py`
- `tests/unit/quality/test_dp_budget.py`

Assertions first:

- `test_release_session_reports_opendp_composed_privacy_loss`
- `test_release_session_refuses_unscheduled_query`
- `test_release_session_query_schedule_is_column_order_independent`

Before:

- `_Charge` and `PrivacyBudget` manually accumulate epsilon and delta.

After:

- Replace them with a thin `OpenDpReleaseSession` around one OpenDP Context compositor.
- The session receives a frozen public query schedule at construction.
- The session releases each resolved `Measurement` through the Context.
- The session exposes the composed privacy loss reported by OpenDP.
- The session refuses query names or counts outside the schedule.
- No mechanism formulas or manual fit-time composition remain in this module.

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
- `test_dp_artifact_emits_no_exact_column_scalars`
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
- Add a private adapter seam, such as `_OpenDpBackend`, for mechanism-level test doubles.
- Production construction always uses unseeded OpenDP randomness.
- Test doubles return already released measurements. They do not accept a random seed.
- Add the OpenDP source-pattern citations to the module docstring.

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
- Release metadata records the loss reported by the compositor.

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
5. Assert **only** that this upper bound does not exceed the mechanism's configured per-query delta times a documented slack factor.
6. Report `N`, releases, observed rate, the bound, and the configured per-query delta on failure.
7. Carry a `statistical` marker if its runtime is unsuitable for every unit-test invocation, and still run in the required CI job.

Step 5 is one-sided on purpose. A correctly calibrated threshold mechanism may release a count-one label far less often than delta, including never in `N` trials. Do not assert a two-sided band, a lower bound, or "close to delta"; that assertion fails on a correct implementation and the natural fix is to weaken the threshold, which is the exact defect 1c describes. Compare against the **per-query** delta the compositor allocated to the thresholded grouping query, not the fit-wide delta; using the fit-wide delta makes the bound loose enough to miss a real regression when several queries are scheduled. Choose `N` so that the bound at `alpha = 1e-6` is tight enough to catch a mechanism whose effective release probability is an order of magnitude above the per-query delta, and state that sensitivity target in the test docstring.

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

Use `math.fsum` or `Decimal` for compile-time ceiling summation and define one conservative comparison tolerance. This arithmetic checks a policy ceiling over certified releases; it does not replace OpenDP’s fit-time accountant.

### 7.6 Full verification

Before review, run the repository’s required formatting, linting, type-checking, and complete test suite from `CONTRIBUTING.md`. Then run:

1. The unseeded statistical suite multiple times.
2. The supported Python matrix.
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
- A DP artifact’s recorded `epsilon_total` and `delta_total` are self-declared. Decoy verifies that the artifact is internally consistent and that its release ID is unique, but a hand-edited artifact can understate what it actually spent. The compile-time ceiling check is a policy control over artifacts Decoy itself produced, not a defense against a caller who edits their own artifacts.

### 8.2 Exact claim sentences

These are the claim sentences the shipped documentation must carry. They are the technically reviewed wording; write them as-is when the code lands.

> For a DP fit whose declared fit-wide privacy loss is `(epsilon, delta)`, Decoy’s released single-column numeric and categorical marginals, and synthetic columns generated solely as post-processing of a DP-verified pinned Plan, are covered by that fit’s approximate `(epsilon, delta)` differential privacy guarantee under add-or-remove-one-row adjacency.

> This guarantee is marginal only. It does not cover joint distributions, cross-column correlations, conditional sampling, masked outputs, non-DP snapshots, or forged artifacts.

> When several independent release IDs are consumed, their privacy losses compose; repeated references to the same release ID are charged once, and conflicting artifacts carrying one release ID are rejected.

Product review of this wording happens in the pre-release docs pass, not here, and does not gate this merge. If that review changes the wording, rerun the technical review against the replacement. Under no revision may the copy omit “approximate,” “marginal,” or the joint exclusion; `test_dp_claim_copy_is_marginal_and_names_joint_exclusion` (section 8.4) enforces that mechanically and lands with the code.

### 8.3 CHANGELOG

Record:

- OpenDP 0.15.1 as the mechanism and accountant dependency.
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

Enable only `contrib`. Do not enable `honest-but-curious`, construct user-defined measurements, or bypass OpenDP checks. Record the exact OpenDP constructors used in `quality/dp.py` and submit those calls to dennis for adversarial review.

Stop if the implementation requires copying an OpenDP formula into Decoy.

### 9.2 Mixed-mechanism composition

The spike must demonstrate that numeric histograms, categorical stable grouping, and categorical non-null totals run under one fit-wide compositor and that OpenDP reports the composed loss.

Stop if:

- Any query must execute outside that compositor.
- The adapter cannot prove every planned query was charged.
- Query allocation depends on observed values.
- The reported loss exceeds the requested fit budget.
- A manual threshold or manual mechanism composition appears necessary.

### 9.3 Polars compatibility

The exact OpenDP Polars compatibility range was not verified from repository source during authorship beyond the published 0.15.1 package metadata and official examples. The implementer must verify it in the mandatory spike.

Stop and escalate before changing Decoy’s existing `polars>=1,<2` constraint or adding `opendp[polars]`.

### 9.4 Value normalization

The exact canonical categorical string policy is not yet specified in the parked implementation. Define it in one helper and test it before mechanism code. It must be deterministic, recordwise, locale-independent, and valid for every supported scalar dtype.

Stop if preprocessing can fail based on a private value after public configuration validation. Add a total normalization rule or narrow the public supported-type contract without inspecting cardinality.

### 9.5 Empty categorical releases

A DP categorical release may retain no labels. Do not recover labels from the input, lower the threshold, or retry until a label appears.

Support the already defined other token when configuration permits it. Otherwise return a typed compile error based only on the DP release.

### 9.6 Plan size and sensitivity

Pinning full artifacts increases Plan size and may embed sensitive exact snapshots in non-DP workflows. This is required for the generation capability but must be documented.

Stop if existing Plan transport imposes a hard size limit that cannot hold realistic snapshots. Escalate a bounded embedded-artifact format rather than reverting to runtime path reads.

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
4. The supported Python dependency matrix passes.
5. Documentation is synchronized to the shipped behavior in the same changeset: `docs/what-we-cannot-prove.md` and the CHANGELOG carry the section 8 wording, every Option A overclaim listed in section 8.1 is gone, and `test_dp_claim_copy_is_marginal_and_names_joint_exclusion` passes. This is barry's normal post-ship pass over shipped behavior, not a review gate. Decoy is pre-release and docs are kept best-effort current, with the full docs review happening before release, so no doc sign-off blocks this merge. What does block it is F7: the code must not land while the docs still claim more than the code delivers, and the assertion test is what proves that.
6. The final cross-model gate confirms that generation has no public raw-config path and no runtime snapshot-path read.

### 9.10 Decisions still owed by a human

These are not implementer choices. Escalate rather than picking one.

1. `numeric_bins` is a public parameter with a default of 10 that lands in the artifact and shapes every bin edge. Under DP its value is a utility knob with no privacy cost, but the default silently determines resolution for every caller. Confirm 10 stays the default, or set a different one, before the artifact schema is written; changing it later invalidates artifacts.
2. Whether `delta` must be strictly positive at the fit API. Scope B always schedules a thresholded categorical query when any categorical column is declared, which requires `delta > 0`; a numeric-only fit does not. Decide whether a numeric-only fit may pass `delta = 0` (pure epsilon-DP, a stronger and honest claim) or whether the API always demands a positive delta for uniformity. The guide currently assumes a positive delta everywhere.
3. Plan size. Section 9.6 flags that embedding artifacts grows Plan files without bound. There is no stated limit today. If the platform's Plan transport has one, it must be surfaced before Step 7, not discovered during it.
