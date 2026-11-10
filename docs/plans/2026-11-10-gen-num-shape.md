Status: plan

# GEN-NUM-SHAPE: fitted number columns survive outliers, skew and few distinct values (roadmap row 24u)

Owner: Cam. Author: Opus plan agent. Date: 2026-11-10. Scope: engine only, V1, blocks V1.

Rules consulted: `dev-rules/README.md` (routing), `00-universal.md`, `risk-and-exceptions.md`,
`development-loop.md` (Proportion; PLAN: guarantee, design notes, choke point, Placement,
Established solutions), `testing.md` (Test depth, full-suite triggers), `api-and-compatibility.md`
(snapshot format consumers), `workspace-and-vcs.md` (Commit messages); engine `CLAUDE.md` (cite the
method; modules aim for 600 lines, hard max 700, census above 600); engine
`docs/compatibility-contract.md:124-166` (a `distribution-snapshot/v1` artifact is a contract; new
fields are additive and optional, old readers ignore them) and `:204` (same input plus same seed
gives byte-identical output within a major); CODEMAP "Where Things Go" (`CODEMAP.md:216-252`);
the V1 bar (only a function break, data loss, a non-admin value leak or cascading design blocks).

Base: engine `main` `c15f1c37` (FID-RANGE #454 merged; #445 numeric typing merged as `1a5b36e5`).
Source: DI models QA 2026-11-10 items C2 and H3 (CLI repo
`docs/records/2026-11-10-cli-qa-generation-distribution-models.md`, lines 127-136 and 179-185;
engine `docs/distribution-integrity/extending.md:113-114`). Re-test on engine `5c24a75e`: one value
of 1,000,000 moved the generated mean from 150 to 50,084; a lognormal median went from 20.5 to
92.1; four prices became 3,585 distinct values with 0.08% of rows on a real price.

Terms. A column's **finite cells** are its non-null cells with `nan` and `+-inf` dropped
(`quality/snapshot.py::finite_numeric_cells`), count `n`. The **release floor** `K` is the fewest
source rows that must stand behind any value this plan writes into a snapshot. A **quantile grid**
is a list of probabilities and the matching empirical quantiles. A **low-distinct** column has at
most 30 distinct finite cells.

## 1. Goal

A number column fitted by `decoy fit` (or `compute_distribution_snapshot`) and generated with
`type: statistical` keeps its shape:

- One extreme value does not move the bulk of the output. The output never goes past the `K`-th
  smallest or largest source value, so a lone extreme row is neither emitted nor needed.
- A skewed column keeps its median and quartiles (within sampling error).
- A column with few distinct values emits exactly those values, at their source shares, when the
  operator consents with the same flag categorical columns use.

Non-goals: the categorical, datetime and freetext samplers; joints and `condition_on` for numbers;
the DP fit and DP sampler path; the operator-authored `strategy: distribution` generator
(`generators/_distribution.py`, owner question Q5); coarsening the existing raw `min`, `max`,
`bin_edges` and `quantiles` of a plain snapshot (row 24f); the CLI (no CLI change needed,
section 9); the platform (parked).

## 2. Current behavior (engine `c15f1c37`)

- Fit. `quality/snapshot.py:277-279` sends every numeric column to `numeric_stats`
  (`:407-461`): `min`, `max`, `mean`, `std`, five quantiles `p05..p95` (`:428-433`) and
  `np.histogram` with `DEFAULT_NUMERIC_BINS = 10` equal-width bins over min to max (`:106`,
  `:444-451`). Nothing else describes the shape. Module size 596 lines.
- Sample. `generation/statistical/_sample.py:305-330` reads only `bin_edges` and `bin_counts`;
  `_numeric_row` (`:195-205`) picks a bin by cumulative count, then a uniform value inside it (two
  `rng.random()` calls). Typing per #445: `emits_int` (`_cell_format.py:331-339`) and
  `format_numeric` (`:322-328`) round and clamp to `[edges[0], edges[-1]]`.
- Why it breaks. With one 1,000,000 among values near 100, bin one spans about 0 to 100,000 and
  holds 99.99% of rows, so the uniform draw inside it lands anywhere in that span. Skew does the
  same at a smaller scale. A four-price column has its mass in two or three bins and is drawn
  uniformly between prices.
- Low-distinct numbers. A numeric dtype never becomes `categorical`: the 30-distinct cap
  (`_CATEGORICAL_DISTINCT_CAP`, `snapshot.py:118`) applies to object columns only (`:283-287`).
- Disclosure gate. `load_spec` (`_spec.py:299-581`) asks for `allow_real_categories: true` only on
  categorical columns (`:466-479`); numbers need no consent. A redacted fit
  (`profiling/_snapshot_values.py:131-146`, rules in `profiling/_value_redaction.py:111-127`)
  rewrites a sensitive number column to one coarse bin and nulls `mean`, `std`, `quantiles`; any
  stats key the registry does not classify is nulled (default deny, `_value_redaction.py:360-370`).
  `_refuse_withheld_values` (`_spec.py:243-296`) refuses only categorical or conditioned columns.
- Format. Plan compile accepts only `schema_version == "distribution-snapshot/v1"`
  (`plan/_generation.py:120-127`). The CLI fit calls `compute_distribution_snapshot(df,
  joint_columns=...)` with defaults (CLI `src/decoy/cli/fit.py:422`).
- Other readers of `bin_edges`/`bin_counts`: `quality/shape_fidelity.py:162`,
  `quality/_distribution_gof.py:116-124`, `plan/_checks_dp_carrier.py:168-298`,
  `generation/_window_admission.py:244-260`, `generators/_distribution.py:125-143`.

Measured today on the acceptance inputs of section 8 (prototype run on `c15f1c37`, 20,000 rows,
`col_seed=7`; value similarity is FID-RANGE's IQR-scaled score):

| Case | Source | Generated today | Value similarity today |
|---|---|---|---|
| Outlier (AT-1) | mean 150.0, median 99.9, 0.005% above 200 | mean 49,850, median 49,468, 99.87% above 200 | 0.0 |
| Lognormal (AT-2) | median 20.05, mean 41.6 | median 78.0, mean 85.1 | 0.0 |
| Four prices (AT-3) | 4 distinct | 1,798 distinct (at 2 decimals), 0.18% on a real price | 0.885 |
| Zero-inflated (AT-6) | 60% exact zeros, median 0 | 0% zeros, median 23.3 | 0.697 |
| Normal (AT-7) | | | 0.965 |

## 3. Established solutions

Search run: library documentation and papers for numeric marginals in tabular synthesis, plus
the engine (`quality/`, `generation/statistical/`, `profiling/`).

- Inverse-CDF sampling from a piecewise-linear empirical quantile function. Hyndman and Fan,
  "Sample Quantiles in Statistical Packages", The American Statistician 50(4), 1996, type 7
  (numpy `np.quantile(method="linear")`, the method the snapshot already uses). scikit-learn
  `QuantileTransformer` stores the same grid (`quantiles_` at `n_quantiles` points) and inverts
  with `np.interp`. Inversion itself is Devroye, Non-Uniform Random Variate Generation (1986),
  ch. II.2, already cited by the sampler.
- Exact values for a number with few levels. synthpop `syn(minnumlevels=)` turns a numeric
  variable with fewer levels into a factor, and `syn.sample` draws observed values. DataSynthesizer
  (Ping, Stoyanovich and Howe, SSDBM 2017) treats any attribute whose domain is below
  `categorical_threshold` as categorical, numbers included; its numeric path is the equal-width
  histogram this row replaces. synthpop's `smoothing` option exists to avoid releasing exact
  values, which is why exact values here sit behind consent.
- Disclosure floor. Statistical disclosure control top and bottom coding and the minimum-frequency
  threshold rule (Hundepool et al., Statistical Disclosure Control, Wiley 2012, sections 3.4.3
  and 4.2; R sdcMicro `topBotCoding`), already cited in `profiling/_value_redaction.py`. Agencies
  use thresholds from 3 to 11 (the CMS cell-size suppression policy suppresses cells under 11).
- Rejected: parametric marginals (SDV `GaussianCopulaSynthesizer` `numerical_distributions`, the
  `copulas` univariate fits). They mis-fit point masses and mixtures (the zero-inflated and
  four-price cases), and need an optimizer whose result can vary by platform.

Custom code: only the grid's probability ladder and its thinning rule (section 4.1), which follow
from the release floor.

## 4. Chosen design

### 4.1 (a) Outlier and skew: quantile-grid inverse CDF

Chosen over the two other options the row names:

- Quantile (equal-frequency) bins written into `bin_edges`/`bin_counts`. The sampler would be the
  same as a grid, but five other readers depend on those keys meaning equal-width bins
  (section 2): `shape_fidelity` sorts `bin_counts` (all equal under quantile bins, so it would
  score nothing), the DP check pins canonical edges, admission reads the edges. Changing their
  meaning under the same tag breaks the v1 contract.
- Winsorized range plus tail mass. It removes the outlier, but keeps equal-width bins inside the
  clipped range, so skew still breaks. Measured on the AT-2 source clipped at p0.5 to p99.5: the
  first of ten bins holds 76% of rows and the generated median is 31.4 against 20.05.

The grid is additive: a new optional `stats.quantile_grid` beside the unchanged histogram.

Fit (`add_numeric_shape`, section 6). For a numeric column with `n >= 100` finite cells and more
than 30 distinct finite cells:

1. `t = max(0.001, K / n)` with `K = MIN_RELEASE_COUNT = 10`.
2. Candidate probabilities, ascending: `t`, then every point of the ladder
   `{0.001, 0.005, 0.01, 0.02, ..., 0.99, 0.995, 0.999}` strictly inside `(t, 1 - t)`, then `1 - t`.
3. Thinning, left to right: keep a candidate `p` only when `(p - last_kept) * n >= K - 1e-9` and
   `(1 - t - p) * n >= K - 1e-9`. The ends are always kept. Every interval then holds at least `K`
   rows, and so does each tail beyond the ends.
4. `values = np.quantile(finite, probs, method="linear")`. Both lists are rounded to 12
   significant digits (`float(f"{v:.12g}")`, a new local helper; the existing `_round` rounds to 12
   decimal places and would flatten a column of values below 1e-12).

Stored: `"quantile_grid": {"probs": [...], "values": [...]}`, at most 103 points (measured 2.4 KB
of JSON per column). With `n = 20,000` the ladder survives whole; with `n = 100` it thins to 8
points from 0.1 to 0.9.

Sample. When a numeric spec carries a grid (and `carrier is None`):

- Treat the first probability as 0 and the last as 1, so the tail mass beyond the ends is drawn
  inside the outer intervals. Interior quantiles stay exact; no value past the ends is made.
- Per row: `rng.seed(col_seed + start + i)`; `u = rng.random()`;
  `j = min(bisect_right(P, u) - 1, len(P) - 2)`;
  `x = v[j] + (u - P[j]) / (P[j+1] - P[j]) * (v[j+1] - v[j])`. One random draw per value (the
  histogram path uses two). The null draw follows as today.
- Typing per #445, unchanged rules: with a `cell_format`, `format_numeric(x, fmt, v[0], v[-1],
  as_int=emits_int(spec))`; without one, `int(min(max(round(x), v[0]), v[-1]))` when
  `emits_int`, else `x`. The clamp bounds are the grid ends, not `bin_edges`. `emits_int` and
  `cell_format_arrow_type` are not touched.
- No grid (old snapshot, fewer than 100 rows, redacted fit, DP carrier): the histogram path, byte
  for byte.

Why the outlier case works: `1 - t` with `t >= K/n` sits between the `(K+1)`-th and `K`-th largest
cell, so the top grid point is below every value held by fewer than `K` rows above it. The one
1,000,000 row never enters the grid. A cluster of `K` or more far rows does enter (AT-5): it is a
real part of the distribution.

### 4.2 (b) Low-distinct numbers: exact values

Fit, for a numeric column with at most 30 distinct finite cells (`_CATEGORICAL_DISTINCT_CAP`, the
same cap that makes an object column categorical):

- Values with at least `K` rows are kept with their counts; values with fewer than `K` rows are
  dropped, and their row total is written as `dropped_count` (a count, value-free).
- Exact mode applies when the dropped rows are at most 1% of finite cells. Stored:
  `"exact_values": {"values": [...], "counts": [...], "dropped_count": int}`, values ascending.
  Values are written as the source cells: Python `int` for an integer dtype (no float detour, so
  int64 above 2**53 stays exact), `float` otherwise (JSON `repr` round-trips exactly). No
  rounding.
- Otherwise (more than 1% of rows on rare values): a grid when `n >= 100`, else nothing new.

Exact mode and a grid are never both written: a grid of a discrete column is itself a list of its
real values, so writing one beside `exact_values` would bypass the consent gate below.

Sample: `rng.choices(values, weights=counts)`, the categorical draw; dropped mass is redistributed
the way categorical `other_mode: redistribute` does. Output is `int(v)` when `emits_int(spec)`,
else `float(v)`; no `format_numeric` (the values are source cells already).

Disclosure gate (the categorical gate, reused):

| Situation | Result |
|---|---|
| Plain snapshot, column has `exact_values`, `allow_real_categories: true` | Exact values emitted |
| Plain snapshot, column has `exact_values`, no consent | Refused at `load_spec` (so at `decoy validate` and plan compile) with the existing code `plan.statistical_real_categories_not_allowed`; the message names the column, says the snapshot holds the column's exact number values, and offers consent or another generator. No value in the message. |
| Redacted fit (`distribution_snapshot_document`, `include_values=False`), sensitive column | `exact_values` and `quantile_grid` are nulled (registry rule `NULL`); generation takes today's one-coarse-bin path |
| `include_values=True` | Both kept; the `raw` marker already says so |
| Sensitive column listed in `redacted_columns` but a hand edit restored `exact_values` or `quantile_grid` | Refused, `plan.statistical_snapshot_values_redacted` (`_refuse_withheld_values` counts these keys as sampling values) |
| DP-verified column (`carrier` set) carrying either key | Keys ignored; the DP path is unchanged and `dp_verified` never exempts exact values from consent |

Stored without consent: a plain snapshot always carried raw number stats without consent (today's
`min`, `max`, `quantiles`, `bin_edges`); this plan adds the grid and `exact_values` there too, but
each value it adds stands for at least `K` rows. Consent governs emitting exact values, as for
categories. Coarsening the existing raw stats is row 24f.

### 4.3 Snapshot format and old snapshots

No version bump. `schema_version` stays `distribution-snapshot/v1`; both keys are optional
additions under `stats` of a `numeric` column, the pattern `cell_format`, `fanout` and the
`high_cardinality` marker already use. A version bump would make plan compile
(`plan/_generation.py:120-127`) refuse every snapshot users already hold.

- Old snapshot, new engine: no keys, histogram path, byte-identical output (AT-9).
- New snapshot, old engine: the keys are ignored (the old sampler reads only the histogram), so
  it generates as before; no refusal and no extra disclosure in its output.
- `kind` stays `numeric`, so fidelity, shape fidelity, the diagnostic kind-drift check and the
  #445 typing all see the same kind as today.

The keys are written by default from `compute_distribution_snapshot`, so the CLI fit (default
arguments) and `distribution_snapshot_document` both carry them with no new keyword. Snapshot
bytes change for every fitted number column that qualifies.

Consumers inventoried (api-and-compatibility): CLI `fit` and `report fidelity` (keys ignored by
fidelity), plan compile pinning (hashes the file, unchanged), the platform (persists the dict,
parked), the compat corpus (old snapshot, histogram path), the five histogram readers in section 2
(unchanged keys).

### 4.4 Relation to row 24f

24f does not need to land first. This row folds the part of 24f that concerns its own keys: the
release floor `K` (no grid end and no exact value comes from fewer than `K` rows) and the
redaction classification of both keys. It leaves to 24f: coarsening the existing raw
`min`/`max`/`bin_edges`/`quantiles`, the categorical count-of-one floor, short free text classed
as categorical, and moving the CLI fit onto `distribution_snapshot_document`.

`MIN_RELEASE_COUNT` is defined once, in `quality/snapshot.py` next to `_CATEGORICAL_DISTINCT_CAP`,
so 24f's count floor imports the same constant. If 24f later widens `min`/`max`/`bin_edges`
outward, the grid stays valid (its values are inside the source range, checked against
`bin_edges` at decode). Both rows edit `quality/snapshot.py`, which is at 596 lines: 24f should
put its rules in `profiling/_snapshot_values.py` (the document path) rather than in
`snapshot.py`.

## 5. Failure modes

- F1 Small columns. With `n` near 100, `t = 0.1`: the tails beyond p10 and p90 are drawn inside
  the outer intervals, so the output range is about p10 to p90. Accepted: it is what the floor
  costs. Under 100 rows nothing changes from today.
- F2 Trimmed mean. A lone extreme row is not reproduced, so the output mean is the mean without
  it (AT-1: about 100 against a source mean of 150). Intended; owner question Q4.
- F3 A far cluster of `K` or more rows is smeared across its grid interval (AT-5: 0.46% of rows
  above 200 against 0.25%). Bounded by the interval width; the ladder is finer in the tails to
  keep it small.
- F4 Exact mode needs consent. A re-fit of a pipeline whose number column has 30 or fewer distinct
  values now fails validation until `allow_real_categories: true` is set. Clear message, marked
  Breaking. Owner question Q1.
- F5 Rare values in an exact column are never emitted (AT-4). That is the floor, as for
  categories.
- F6 A hand-written or corrupted block (non-monotone probabilities, values outside `bin_edges`,
  both keys present, bad counts) is refused with `plan.statistical_stats_degenerate` at load,
  never sampled silently.
- F7 Snapshot size grows by about 2.4 KB per qualifying number column. Accepted; a 200-column
  table grows by under 0.5 MB.
- F8 A builder loosens a bound or re-pins a golden to pass. Every bound in section 8 is fixed;
  section 10 lists the only goldens that may move.
- F9 Old snapshots keep the C2 defect until re-fit. Changelog says so; owner question Q6.

## 6. Choke points and design notes

- One fit choke point: `add_numeric_shape(columns, df, distinct_cap=...)`, called once from
  `compute_distribution_snapshot` after the per-column pass (beside `add_cell_format`), lazily
  imported like `add_fanout` and `add_cell_format` (`generation` imports `quality`). Both fit
  paths (CLI default call and `distribution_snapshot_document`) go through it. It skips
  `high_cardinality` columns (they are categorical) and non-numeric kinds. Guard: AT-12 and the
  `numeric_only` golden fail if a fit path bypasses it; mutation: drop the call, both go red.
- One read choke point: `decode_numeric_shape(column, stats, carrier) -> NumericShape | None`,
  called by `load_spec` (so compile and validate see the errors) and by `sample_column` (to build
  the tables). The spec dataclass and `spec_to_dict` do not change: the keys travel inside
  `stats`, so the serialized Plan format is unchanged.
- Design notes: one module owns both halves, as `_cell_format.py` does
  (`generation/statistical/_numeric_shape.py`); a frozen `NumericShape` dataclass is the typed
  context the sampler receives instead of loose lists; no flag argument (the sampler dispatches on
  which table the decoder returned). No other new structure.
- The class this fixes: "a sampler reads a summary that one row can stretch". The construction
  fix is the floor in the grid; the guard is the property test AT-17. No new sentry.

## 6a. Enumerations (from the code at engine `c15f1c37`)

### Routes that produce statistical number values

- In-memory generation: `sample_column` numeric branch, `generation/statistical/_sample.py:305`;
  grid, exact or histogram chosen by the decoder.
- Pinned-snapshot route: plan compile reads the pinned bytes once (`plan/_generation.py:120`) and
  calls `load_spec`, which runs the same decoder (`generation/statistical/_spec.py:466`).
- Windowed route: window admission checks `bin_edges` span (`generation/_window_admission.py:244`);
  grid values sit inside `bin_edges`, so admission is unchanged, and each window reseeds per row.
- DP carrier route: `carrier` set means the histogram path; both keys are ignored (AT-15a/b).

### Exit paths

- `load_spec` refusal for a malformed block: `plan.statistical_stats_degenerate` (AT-14a/b).
- `load_spec` refusal for exact values without consent: `plan.statistical_real_categories_not_allowed`
  (`_spec.py:466`, AT-3b).
- `_refuse_withheld_values` refusal for a restored key on a redacted column:
  `plan.statistical_snapshot_values_redacted` (`_spec.py:243`, AT-13a/b).
- Normal return: values drawn; no new exception type, no partial output (pure functions).

### FK shapes

N/A: number columns take no `condition_on` or FK fan-out in the statistical generator; the grid and
exact draws are per column only, and the FK paths do not read `stats`.

### Fields (every key this plan adds)

- `stats.quantile_grid.probs`: ascending floats in `(0, 1)`, value-free (counts of rows).
- `stats.quantile_grid.values`: ascending floats, value-bearing, registry `NULL` when redacted.
- `stats.exact_values.values`: ascending source cells, value-bearing, registry `NULL` when redacted.
- `stats.exact_values.counts` and `stats.exact_values.dropped_count`: ints, value-free, nulled
  with their container.

### Output surfaces

- Snapshot JSON (`quality/snapshot.py:407` numeric stats, plus the new keys): section 7 rules.
- Generated column values: floor `K` keeps every emitted grid value inside the K-th extremes.
- Error messages from the decoder and consent check: column, member and counts only.
- Fidelity and shape-fidelity reports: unchanged keys, unchanged shape.
- Logs: no new log line.

## 7. Value-free rules

- Snapshot: both keys hold values and are registered in `SNAPSHOT_VALUE_STATS` as
  `("numeric", "quantile_grid"): Rule.NULL` and `("numeric", "exact_values"): Rule.NULL`, so a
  redacted fit drops them. Their nested members (`probs`, `dropped_count`) are not separately
  classified; the whole container is nulled.
- Errors: decoder and consent messages name the column, the member and counts, never a grid value
  or an exact value. AT-14 and AT-3b assert a source value does not appear.
- No new log line. No new report field. Fidelity output shape unchanged.
- Docs, changelog and test names use synthetic numbers only.

## 7a. Class table

| # | Class | Where it applies | Structure that closes it | Proof | Mutation that must turn it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | the two new keys in a redacted fit; decoder and consent messages | registry rule `NULL` for both keys under default deny; messages name column and member only | AT-12, AT-3b, AT-14a/b | remove the `quantile_grid` registry entry and drop default deny for it (AT-12 red) |
| 2 | Cleanup missing on an exit path | none | N/A: pure functions, nothing opened or staged | n/a | n/a |
| 3 | State machine and concurrency | none | N/A: per-row reseed over an immutable spec, no cache | AT-10 | n/a |
| 4 | Route parity and silent fallback | in-memory, pinned, plan-load and windowed routes; DP carrier | one decoder used by `load_spec` and `sample_column`; carrier set means the histogram path | AT-10, AT-15a/b | decode only in `sample_column` (AT-14a at `load_spec` red) |
| 5 | Referential integrity | none | N/A: no joint or `condition_on` for numbers | n/a | n/a |
| 6 | Untrusted producer | a hand-edited snapshot block; a restored block on a redacted column | strict decoder (types, order, bounds inside `bin_edges`, no unknown member, not both keys); withheld-values refusal | AT-13a/b, AT-14a/b | accept a `bool` count (AT-14b red) |
| 7 | Check then use (TOCTOU) | the snapshot path | unchanged: specs come from the read-once pinned bytes | existing pinned tests | n/a, no new read |
| 8 | Weak tests | band tests that a broken sampler could pass | each band test bounds both sides and checks the source has the property (outlier present, zeros at 60%) | AT-1, AT-5, AT-6 | replace the grid draw with the histogram draw (AT-1 red) |
| 9 | Refactor drift | old snapshots | absent keys mean the histogram path, byte for byte | AT-9 | always build an empty grid (AT-9 red) |
| 10 | Frozen contract drift | `distribution-snapshot/v1`, determinism, Plan serialization, draw-site catalogue | additive optional keys; `StatisticalSpec` unchanged; catalogue re-pin | AT-9, `test_draw_sites_catalogue_pinned.py`, compat corpus | write `quantile_grid: null` for every column (`numeric_only` and compat checks red) |

## 8. Acceptance tests (red first)

Shared harness, unless stated: `src` built as named;
`snap = compute_distribution_snapshot(pd.DataFrame({"x": src}))`;
`spec = load_spec(cfg, snapshot=snap)` with `cfg = {"name": "x"}`;
`out = sample_column(spec, 20_000, col_seed=7)`. Value similarity is
`compute_fidelity(snap, compute_distribution_snapshot(pd.DataFrame({"x": out})))["marginal"]["columns"][0]["value_similarity"]`
(the FID-RANGE IQR scale). Each `default_rng(seed)` is fresh with exactly the calls named.
"Proto" is a prototype of this design run on `c15f1c37` with the same seeds; bounds have margin
over it.

E1 (grid), new file `tests/unit/generation/statistical/test_numeric_grid.py` unless noted:

| # | Test | Input | Bound | Today / proto |
|---|---|---|---|---|
| AT-1 | `test_one_outlier_does_not_move_the_bulk` | `np.append(default_rng(501).normal(100.0, 10.0, 19_999), 1_000_000.0)` | `95 <= mean(out) <= 105`; `abs(median(out) - median(src)) <= 1.0`; no value above 200; `max(out) <= sorted(src)[-10]`; value similarity `>= 0.95` | mean 49,850, sim 0.0 / mean 99.74, median 99.78, max 131.6, sim 0.975 |
| AT-2 | `test_lognormal_keeps_its_median` | `default_rng(502).lognormal(3.0, 1.2, 20_000)` | median within 5% of source; mean within 8%; p95 within 10%; similarity `>= 0.85` | median 78.0, sim 0.0 / median 19.66, mean 40.33, p95 139.9, sim 0.920 |
| AT-5 | `test_a_far_cluster_of_k_rows_is_kept` | `np.append(default_rng(505).normal(100.0, 10.0, 19_950), np.full(50, 1_000_000.0))` | `0.8 <= mean(out)/mean(src) <= 1.2`; share above 200 in `[0.001, 0.01]` | mean 52,006 / mean 2,462 (source 2,600), share 0.46% |
| AT-6 | `test_zero_inflated_keeps_its_zeros` | `np.append(np.zeros(12_000), default_rng(506).gamma(2.0, 30.0, 8_000))` | share of exact `0.0` within 0.01 of 0.60; median 0; similarity `>= 0.90` | 0% zeros, sim 0.697 / 59.8%, sim 0.959 |
| AT-7 | `test_a_normal_column_stays_good` | `default_rng(504).normal(50.0, 5.0, 20_000)` | similarity `>= 0.95` | 0.965 / 0.976 |
| AT-8 | `test_grid_needs_one_hundred_rows` | `default_rng(507).normal(100, 10, 100)` and its first 99 | 100 rows: grid present, `min(out) >= sorted(src)[9]`, `max(out) <= sorted(src)[-10]`; 99 rows: no `quantile_grid` key | proto: 8 points, probs 0.1 to 0.9 |
| AT-9 | `test_old_snapshot_is_byte_identical` | three snapshots (AT-7 source; an int64 column with a `cell_format`; AT-1 source) with `quantile_grid` deleted | `sample_column` output digest equals the digest recorded on `main` before the change (record it in the red commit) | must be equal |
| AT-10 | `test_grid_windows_equal_a_full_run` | AT-2 snapshot | `sample_column(..., start=k)` for `k` in 0, 1, 4,999 equals the full run's slice; two runs are equal | |
| AT-11a | `test_grid_typing_follows_445` | int64 source `default_rng(508).integers(0, 1_000, 5_000)`; float source as AT-7 | int64: every value is `int` and `cell_format_arrow_type` is `int64`; float: every value is `float` | |
| AT-12 | `tests/unit/profiling/test_snapshot_values_numeric_shape.py::test_sensitive_number_column_drops_the_grid` | the existing `force_sensitive` fixture on an AT-7-shaped column | `quantile_grid is None` in the document; generation stays inside the coarse bounds; `include_values=True` keeps the grid | |
| AT-13a | `test_restored_grid_on_a_redacted_column_is_refused` (same new profiling file) | redacted document, grid copied back from the raw snapshot | `plan.statistical_snapshot_values_redacted` | |
| AT-14a | `test_malformed_grid_is_refused` (parametrized) | probs not increasing; a prob outside `[0, 1]`; lengths differ; fewer than 2 points; a `bool` or string member; a value outside `bin_edges`; an unknown member; an `exact_values` block before E2 lands | `plan.statistical_stats_degenerate` at `load_spec`; message contains no grid value | |
| AT-15a | `test_dp_carrier_ignores_a_grid` | a DP-verified fixture with a grid added | output equals the same fixture without it | |
| AT-16 | `test_grid_is_small` | AT-1 snapshot | `len(json.dumps(stats["quantile_grid"])) <= 3_000`; `len(probs) <= 103` | 2,422 bytes |
| AT-17 | `test_grid_floor_properties` (hypothesis, `max_examples=200`, derandomized) | float arrays, `n` 100 to 2,000, with up to 5 values at `+-1e12` | probs strictly increasing; every `(p[i+1] - p[i]) * n >= K - 1e-9`; `v[0] >= sorted(src)[K-1]`; `v[-1] <= sorted(src)[-K]`; 1,000 drawn values inside `[v[0], v[-1]]` | |
| AT-19 | `test_grid_value_math` | `probs [0.1, 0.5, 0.9]`, `values [0, 10, 20]` through the pure draw function | `u=0.0 -> 0`; `u=0.25 -> 5`; `u=0.5 -> 10`; `u=0.75 -> 15` (ends treated as 0 and 1) | |

E2 (exact values), new file `tests/unit/generation/statistical/test_numeric_exact.py`:

| # | Test | Input | Bound | Today / proto |
|---|---|---|---|---|
| AT-3 | `test_four_prices_stay_four_prices` | `default_rng(503).choice([4.99, 9.99, 19.99, 49.99], p=[0.4, 0.3, 0.2, 0.1], size=20_000)`, `cfg` with `allow_real_categories: True`; run on the plain snapshot and on `distribution_snapshot_document(..., include_values=True)` (with `cell_format`) | every value is one of the four prices; each share within 0.015 of the source share; similarity `>= 0.99` | 0.18% on a price, sim 0.885 / 100%, largest share gap 0.0066, sim 1.0 |
| AT-3b | `test_exact_values_need_consent` | AT-3 source, `cfg = {"name": "x"}` | `StatisticalSpecError`, code `plan.statistical_real_categories_not_allowed`; `"4.99"` not in the message; `dp_verified=True` does not lift it | |
| AT-4 | `test_a_rare_value_is_never_emitted` | `np.append(default_rng(503).choice(<same four prices and p>, size=19_999), 999.99)` | `exact_values.values` are the four prices, `dropped_count == 1`; `999.99` not in the output | proto: confirmed |
| AT-11b | `test_exact_typing_follows_445` | int64 `[1, 2, 3]` repeated to 3,000 rows; float `[0.5, 1.5]` repeated to 2,000 | int64: every value `int`; float: every value `float` | |
| AT-13b | `test_restored_exact_values_on_a_redacted_column_are_refused` | as AT-13a with `exact_values` | `plan.statistical_snapshot_values_redacted` | |
| AT-14b | `test_malformed_exact_values_are_refused` (parametrized) | values not strictly increasing; a count of `0` or a `bool`; `dropped_count < 0`; lengths differ; empty; both keys present | `plan.statistical_stats_degenerate`; no value in the message | |
| AT-15b | `test_dp_carrier_ignores_exact_values` | DP-verified fixture with `exact_values` added | output equals the fixture without it | |
| AT-18 | `test_exact_mode_threshold` | 30 distinct values x 20 rows; 31 distinct x 20 rows; 25 values x 40 rows plus 30 singleton values (3% rare) | 30: exact; 31: grid; 3% rare: grid, no `exact_values` | |
| AT-20 | golden fixture `numeric_low_distinct` in `tests/snapshots/test_distribution_snapshot_baseline.py` | `[10, 20, 30] * 40` int64 | new golden file | |

Mutation per mechanism (each must turn a named test red; record it in the PR): tail floor `t`
replaced by 0 (AT-1, AT-17); thinning removed (AT-17); ends kept at `t`/`1-t` instead of 0/1
(AT-19); grid written for a low-distinct column (AT-18); rare values kept (AT-4); consent check
removed (AT-3b); registry rule removed (AT-12); carrier check removed (AT-15a/b);
`add_numeric_shape` call dropped (AT-12, `numeric_only` golden).

Must pass unchanged: `tests/unit/generation/test_statistical.py` (including
`test_distribution_shape_matches_source`), `test_statistical_redacted.py`,
`tests/unit/generation/statistical/test_cell_format_sampling.py`, `tests/unit/quality/`,
`tests/unit/profiling/test_snapshot_values.py` except the re-baseline in section 10,
`tests/integration/compat_corpus/`. A failure there is a stop and a report.

## 9. Typing, CLI and native

- #445 typing is kept: `emits_int` and `cell_format_arrow_type` are untouched; both new draw paths
  end in the same `int`/`float` decision (AT-11a, AT-11b).
- CLI: no change. `decoy fit` gets the keys through the default call (`cli/fit.py:422`). At the
  next engine pin bump, any CLI test that pins fit bytes re-pins with this row cited, and the QA
  record's C2 and H3 rows take re-test numbers.
- Native: no native statistical sampler exists. The draw-site entry `gen.statistical_per_row`
  (`execution/native/_draw_sites_gen.py:121-145`) changes its `call_shape` to name "histogram,
  quantile-grid (one draw) or exact-value draw"; re-pin the digests in
  `tests/native/test_draw_sites_catalogue_pinned.py` in the same commit, after showing the old
  catalogue reproduces the old digest.
- Window admission (`generation/_window_admission.py:244-260`) keeps checking `bin_edges`; grid
  values are accepted only when inside `[bin_edges[0], bin_edges[-1]]`, so its int64 span check
  still bounds every output. No admission change.

## 10. Goldens and pins that move

Each move is intentional and written in the diff:

- E1: `tests/snapshots/golden/distribution_snapshot/numeric_only.sha256` (100 distinct normal
  values, so a grid is written). `mixed_with_joint` does not move (salary: 6 distinct values of 5
  rows each, 30 rows, so neither key is written); the other fixtures are not numeric.
- E1: `tests/native/test_draw_sites_catalogue_pinned.py` digests (call-shape text).
- E1: a test that compares a redacted numeric entry against an exact key set
  (`test_snapshot_values.py::test_a_numeric_canary_column_keeps_a_coarse_unit_range_only`, if it
  lists keys) gains `quantile_grid: None`.
- E1: any generation test that fits in process a number column with at least 100 rows and more
  than 30 distinct values and pins output values. The builder lists them from the red-commit T1
  run; each must be explained by "this column now has a grid".
- E2: new golden `numeric_low_distinct.sha256`; a test that pins output from an in-process fit of
  a low-distinct number column without consent now hits the refusal and gains
  `allow_real_categories: True`.
- Not moving: `quality_fidelity`, `quality_report`, `quality_shape_fidelity` goldens (they hash
  reports, which read only the unchanged stats), the compat corpus (old snapshot, histogram path).

Regenerate with `UPDATE_SNAPSHOTS=1`. Any other moved digest or pinned number is a stop and a
report.

## 11. Changelog fragments

`changelog.d/gen-num-grid.md` (E1, not Breaking: same snapshot plus same seed still gives the
same output):

```
### Changed (fitted number columns keep their shape, GEN-NUM-SHAPE E1, 2026-11-10)

A fit now stores a quantile grid (`stats.quantile_grid`) for each number column with at least
100 values and more than 30 distinct values, and statistical generation samples from it instead
of ten equal-width bins. One extreme value no longer spreads generated rows across the whole
range, and skewed columns keep their median. The grid leaves out the 10 smallest and 10 largest
values, so generated numbers stay between the 10th smallest and 10th largest source value.
Snapshots fitted by earlier versions have no grid and generate exactly as before; re-fit to get
the new shape. A redacted fit drops the grid for sensitive columns.
```

`changelog.d/gen-num-exact.md` (E2):

```
Breaking: a re-fitted number column with 30 or fewer distinct values needs allow_real_categories: true.

### Changed (number columns with few values keep them, GEN-NUM-SHAPE E2, 2026-11-10)

A fit stores the exact values of a number column with 30 or fewer distinct values
(`stats.exact_values`), leaving out values held by fewer than 10 rows. Statistical generation
emits those values at their source shares, as it does for categories, and needs the same
consent: `allow_real_categories: true` on the column, or validation refuses it with
`statistical_real_categories_not_allowed`. Older snapshots are unaffected.
```

E2's commit subject carries `!` and a `BREAKING CHANGE:` line (workspace-and-vcs).

## 12. Placement (checked against `CODEMAP.md`)

| File | Change | Slice | CODEMAP row | Size |
|---|---|---|---|---|
| `src/decoy_engine/quality/snapshot.py` | `MIN_RELEASE_COUNT = 10` beside `_CATEGORICAL_DISTINCT_CAP`; lazy `add_numeric_shape(...)` call after the column pass; docstring numeric row | E1 | quality (`CODEMAP.md:44`) | 596, must end at or under 600: if the added lines push it over, condense the docstring's "What this module does NOT do" list (`:35-40`) to two lines. No census entry. |
| `src/decoy_engine/generation/statistical/_numeric_shape.py` | new: fit half (`add_numeric_shape`, grid, exact values) and read half (`NumericShape`, `decode_numeric_shape`, `grid_value`, consent message helper); sits beside `_cell_format.py`, which has the same two halves | E1 grid, E2 exact | `CODEMAP.md:58` (statistical generation) and the "Generation" row of "Where Things Go" | about 230 after E1, about 320 after E2 |
| `src/decoy_engine/generation/statistical/_sample.py` | numeric branch dispatches grid, exact or histogram; docstring methodology | E1, E2 | `:58` | 439 to about 470 |
| `src/decoy_engine/generation/statistical/_spec.py` | `load_spec` calls the decoder (carrier None only) and the exact-values consent check; `_refuse_withheld_values` counts the two keys | E1, E2 | `:58` | 581, must end at or under 600 (about +8) |
| `src/decoy_engine/generation/statistical/__init__.py` | export `add_numeric_shape` | E1 | `:58` | +2 |
| `src/decoy_engine/profiling/_value_redaction.py` | two `SNAPSHOT_VALUE_STATS` entries (both keys, so E2 adds no registry change) | E1 | `:106` | 469 to 471 |
| `src/decoy_engine/execution/native/_draw_sites_gen.py` | `call_shape` text | E1 | native bridge row | same |
| `tests/unit/generation/statistical/test_numeric_grid.py`, `test_numeric_exact.py` | new | E1, E2 | "Unit tests" row | folder 6 to 8 files |
| `tests/unit/profiling/test_snapshot_values_numeric_shape.py` | new (AT-12, AT-13) | E1, E2 | "Unit tests" row | |
| `tests/snapshots/test_distribution_snapshot_baseline.py` and goldens | `numeric_only` re-pin; `numeric_low_distinct` fixture | E1, E2 | snapshot harness | |
| `docs/distribution-integrity/snapshot-and-fidelity.md:65`, `statistical-generation.md:70,129,138`, `extending.md:113-114`, `index.md:86` | numeric stats row, sampler text, limits, C2 and H3 rows marked fixed, gate owner | E1, E2 | doc index | |
| `CODEMAP.md:58` | add `_numeric_shape.py` and the allowed lazy edge "`quality/snapshot.py` calls `add_numeric_shape`" | E1 | | |
| `changelog.d/gen-num-grid.md`, `gen-num-exact.md` | fragments | E1, E2 | "Changelog fragments" row | |

One new source module, in `generation/statistical/` (6 files to 7); none in `quality/` (25 files,
at the flat-folder limit). No census entry, no new reason code (both refusals reuse existing
codes), no new sentry.

## 12a. Reuse inventory

| Need | Reused | Not built |
|---|---|---|
| Quantile computation | `numpy.quantile(method="linear")` (Hyndman-Fan type 7) | a hand-rolled quantile |
| Inverse-CDF draw | the existing per-row `random.Random` reseed in `_sample.py` | a new RNG or stream |
| Exact draw | `rng.choices(values, weights=counts)`, the categorical draw | a new weighted sampler |
| Distinct cap | `_CATEGORICAL_DISTINCT_CAP` (30) | a second threshold |
| Consent | `allow_real_categories` and `plan.statistical_real_categories_not_allowed` | a new flag or code |
| Redaction | `SNAPSHOT_VALUE_STATS` registry and its default deny | a per-key redaction branch |
| Typing | `emits_int`, `format_numeric`, `cell_format_arrow_type` (#445) | new typing rules |
| Module shape | the `_cell_format.py` pattern (fit half plus read half, lazy import from `quality`) | a new package |

## 12b. Frozen contracts touched

| Contract | Change | Evidence |
|---|---|---|
| `distribution-snapshot/v1` | additive optional keys under numeric `stats`; no version bump | AT-9, compat corpus, `numeric_only` re-pin |
| Determinism (same snapshot plus seed gives same output) | holds per snapshot; old snapshots byte-identical | AT-9, AT-10 |
| Plan serialization (`spec_to_dict`) | unchanged: the keys travel inside `stats` | existing plan round-trip tests |
| Draw-site catalogue | `gen.statistical_per_row` call-shape text changes | `test_draw_sites_catalogue_pinned.py` re-pin in the same commit |
| Reason-code registry | no new code | existing reason-code sentry |

## 13. Slices, gates and risk

| Slice | Branch | Size | Content | Tests and gate |
|---|---|---|---|---|
| E1 GEN-NUM-GRID | `feat/gen-num-grid` | M | Prep folded in: `MIN_RELEASE_COUNT`, registry entries for both keys, a decoder that refuses an `exact_values` block until E2 (fail closed). Grid fit, grid sampler, `_refuse_withheld_values` extension, draw-site text and re-pin, `numeric_only` golden, docs, CODEMAP, fragment. AT-1, 2, 5 to 10, 11a, 12, 13a, 14a, 15a, 16, 17, 19. | T0 per edit; T1 `tests/unit/generation`, `tests/unit/quality`, `tests/unit/profiling`, `tests/unit/plan`, `tests/snapshots`, `tests/native/test_draw_sites_catalogue_pinned.py`, `tests/integration/compat_corpus`. Full suite on the merge head (trigger: serialization of a frozen artifact), Mac first, diffed against the saved main baseline. Review: Codex. |
| E2 GEN-NUM-EXACT | `feat/gen-num-exact` | S | Exact-values fit, decoder half, consent check in `load_spec`, sampler branch, `numeric_low_distinct` golden, docs, Breaking fragment. AT-3, 3b, 4, 11b, 13b, 14b, 15b, 18, 20. | Same T1 set; full suite on the merge head (frozen artifact plus a consent gate). Review: Codex, then Dennis (disclosure logic). |

Order: E1 then E2. Main stays correct between them: after E1 a low-distinct number column gets
no grid (the histogram path, as today), so no real values are emitted without consent in the gap,
and a hand-made `exact_values` block is refused rather than ignored.

Risk: R2 for both (artifact format and generated output change; E2 adds disclosure logic). E2 is
separate because its review needs a second, disclosure-focused reviewer and its changelog is
Breaking; E1 is not Breaking.

Builder rules: Conventional Commits subject, 72 characters max, no body unless it explains why,
final line `cam`; T0/T1 per `testing.md` "Test depth"; never widen a bound, skip a test or re-pin
an unlisted golden to pass; mutate each mechanism once (section 8) and show the named test goes
red.

## 14. Owner questions (recommended answer first)

1. Q1 Consent for exact values. Recommend: refuse without `allow_real_categories: true`, reusing
   the categorical flag and code (the row says "the same disclosure gate"; a bool or code column
   already needs it as a category). Alternatives: fall back to the histogram silently (keeps the
   defect for anyone who has not opted in), or a new `allow_real_values` flag (a second flag for
   the same consent).
2. Q2 Release floor `K`. Recommend 10, one constant shared with row 24f's count floor. Lower (5)
   keeps more of a small column's tails; 11 (the CMS cell rule) changes little.
3. Q3 Exact-mode threshold. Recommend 30 distinct values, the same cap that makes an object column
   categorical (DataSynthesizer uses 20). One number for both kinds.
4. Q4 A lone extreme value is not reproduced, so the generated mean in the outlier case is about
   100, not the source's 150. Recommend accept: reproducing it means emitting one person's value,
   which 24f forbids anyway.
5. Q5 The operator-authored `strategy: distribution` generator (`generators/_distribution.py`)
   keeps equal-width bins. Recommend V2-LOG: its snapshot is typed by hand, not fitted.
6. Q6 Old snapshots keep the defect until re-fit. Recommend the changelog note only, no runtime
   warning (pre-release, few old snapshots in use).

## 15. Pre-mortem

- Band tests flake: all draws are seeded per row and the bounds sit well outside the proto
  numbers; a red band means a determinism or method change, not noise.
- `snapshot.py` crosses 600 lines: the placement table names the trim; a census entry is not an
  allowed fix.
- A re-pinned golden hides a regression: section 10 is the whole list.
- 24f lands first and changes `numeric_stats` or the registry: rebase, keep both registry
  entries, re-run AT-12 and AT-13.
