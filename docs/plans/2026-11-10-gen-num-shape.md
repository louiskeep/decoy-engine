Status: plan

# GEN-NUM-SHAPE: fitted number columns survive outliers, skew and few distinct values (roadmap row 24u)

Owner: Cam. Author: Opus plan agent. Date: 2026-11-10. Revision 3. Scope: engine only, V1, blocks V1.

Rules consulted: `dev-rules/README.md` (routing), `00-universal.md`, `risk-and-exceptions.md`,
`development-loop.md` (Proportion; PLAN: guarantee, design notes, choke point, Placement,
Established solutions), `code-review.md`, `testing.md` (Test depth, full-suite triggers),
`architecture.md`, `security.md`, `documentation.md`, `catalogs/recurring-defects.md`,
`api-and-compatibility.md` (snapshot format consumers), `workspace-and-vcs.md` (Commit messages);
engine `CLAUDE.md` (cite the
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
(`quality/snapshot.py::finite_numeric_cells`), count `n`. The **release floor** `K` is the minimum
source-row mass. In grid mode it applies to every interval and tail, not to each interior knot. In
exact mode it applies to every emitted value. A **quantile grid** is a list of probabilities and
the matching empirical quantiles. A **low-distinct** column has at most
`CATEGORICAL_DISTINCT_CAP = 30` distinct finite cells.

## 0. Findings map

### Revision 2 (Dennis r1: NO-GO, 3 HIGH, 5 MEDIUM, 5 LOW)

| Finding | Bar | Design | Resolution |
|---|---|---|---|
| H1 grid rounding to 12 decimal places can move a knot or leave it outside rounded `bin_edges`; int64 above 2**53 needs separate coverage | V1 | none | Grid values stored unrounded (JSON `repr` round-trips a float exactly), then clamped at fit into `[bin_edges[0], bin_edges[-1]]`. The `exact_values` bounds check compares `float(v)` with a stated tolerance. New AT-21, AT-22, AT-23 (section 8) |
| H2 consent on `exact_values` protects nothing beyond the already value-bearing plain snapshot | V1 | none | Exact mode's per-value `K` floor needs no consent and the change is not Breaking. Grid mode has the narrower interval and tail guarantee described in section 1. The two slices fold into one M slice (section 13) |
| H3 the snapshot registry sentry goes red | V2-LOG | none | `tests/sentry/profiling/test_snapshot_value_registry.py` and its fixture are in sections 10 and 12; the one slice registers both keys and the fixture fills both |
| M1 `quality/snapshot.py` is at 599 after #456 | V2-LOG | DESIGN (change-created) | Re-budgeted from 599 with two named trims; `MIN_RELEASE_COUNT` moves to `profiling/_value_redaction.py`; the decoder and all checks live in `_numeric_shape.py`, so `_spec.py` gains call lines only (section 12) |
| M2 wrong 24f reference | V2-LOG | none | 24f is QA-FIT-REDACT (`2026-11-08-qa-fit-redact.md`, engine half merged in #420) and has no count floor. Section 4.4, Q2 and Q4 rewritten. Row 24w (#456, `2026-11-10-gen-cond-cap.md`) is named only for the line budget |
| M3 dp clause in AT-3b | V2-LOG | none | AT-3b is gone with the consent gate; the carrier rule is AT-15a/b only |
| M4 review order | V2-LOG | none | Dennis, then Codex |
| M5 tests that do not bite | V2-LOG | none | `assert "quantile_grid" in stats` precondition in AT-10, AT-11a, AT-15a, AT-16; AT-12 asserts the key is present and `None` |
| L1 ladder and point count | V1 | none | Ladder written out as a list; `n = 100` gives 9 points (measured) |
| L2 ascending vs non-decreasing | V1 | none | Grid values are non-decreasing; the decoder accepts equal neighbours; AT-14a refuses only a decrease; the withheld check counts non-null keys only |
| L3 exception chaining | V2-LOG | none | Decoder translations raise only after the `except` block closes; AT-14a/b assert both cause and context are `None` |
| L4 wording | V2-LOG | none | "Every interval between grid points holds at least `K` rows" |

### Codex r1 (NO-GO, 2 HIGH, 4 LOW)

| Finding | Bar | Design | Resolution |
|---|---|---|---|
| H1 singleton quantile knots | V1 | none | Narrowed the grid guarantee: intervals and tails each hold at least `K` rows, but an interior knot may be a source value held by fewer than `K` rows. Plain snapshots are already value-bearing; redacted fits null the grid. Removed the false subset claim, added Q7 and AT-24 |
| H2 duplicated distinct cap | V2-LOG | DESIGN (change-created) | Rename the owner in `quality/snapshot.py` to public `CATEGORICAL_DISTINCT_CAP`, export it through `decoy_engine.quality`, and consume it from both classification and numeric-shape admission. Placement, choke-point guard and one controlling mutation are in sections 6, 8 and 12 |
| L3 window parity incomplete | V2-LOG | none | AT-10 now covers both grid and exact modes |
| L4 exception context retained | V2-LOG | none | Decoder translation raises the safe error only after the `except` block; AT-14a/b require both `__cause__` and `__context__` to be `None` |
| L5 AT-18 missed the low-distinct rare-share branch | V2-LOG | none | The rare-share case now has 30 total distinct values: 25 values repeated 10 times plus five singletons |
| L6 rounding evidence incorrect | V2-LOG | none | All evidence now says `_round` uses 12 decimal places. AT-22 is only an integer-formatting and floor test |

### Codex self-review r2 (GO)

| Finding | Bar | Design | Resolution |
|---|---|---|---|
| Old-snapshot parity omitted the low-distinct exact candidate | V1 | none | AT-9 now records and compares the base digest for an AT-3 low-distinct snapshot with both new keys absent |

No V1 or change-created DESIGN finding remains. No unresolved V2-LOG finding affects the verdict.

## 1. Goal

A number column fitted by `decoy fit` (or `compute_distribution_snapshot`) and generated with
`type: statistical` keeps its shape:

- One extreme value does not move the bulk of the output. The output never goes past the `K`-th
  smallest or largest source value, so a lone extreme row is neither emitted nor needed.
- A skewed column keeps its median and quartiles (within sampling error).
- A column with few distinct values emits exactly those values that at least `K` rows hold, at
  their source shares.

Scope of the guarantee: it applies to columns that get a grid (100 or more finite rows and more
than 30 distinct values) or exact values. A column under 100 rows keeps today's histogram path,
unchanged by this row, which can emit values anywhere in the source `[min, max]`. The loader
refuses a stored block that breaks the floor (exact counts under `K`, counts that do not sum to
the column's row count, or a grid interval or tail under `K` rows).

Guarantee: every interval between adjacent grid points and each tail beyond the grid ends holds at
least `K = 10` source rows, and no value outside `[K-th smallest, K-th largest]` is emitted.
An interior grid point may coincide with a source value held by fewer than `K` rows, the same
exposure today's plain snapshot already has through its stored quantiles (`p05`/`p50`/`p95`) and
`min`/`max` `bin_edges`. Plain snapshots are therefore value-bearing artifacts, and redacted fits
null the grid through `SNAPSHOT_VALUE_STATS`. Exact mode keeps its true per-value `K` floor and
needs no consent. Old snapshots generate byte-identical output.

Non-goals: the categorical, datetime and freetext samplers (categorical consent is unchanged);
joints and `condition_on` for numbers; the DP fit and DP sampler path; the operator-authored
`strategy: distribution` generator (`generators/_distribution.py`, owner question Q5);
coarsening the existing raw `min`, `max`, `bin_edges` and `quantiles` of a plain snapshot (a plain
snapshot is the operator's own data, as today; the redacted fit of row 24f already coarsens
sensitive columns); the CLI (no CLI change needed, section 9); the platform (parked).

## 2. Current behavior (engine `c15f1c37`)

- Fit. `quality/snapshot.py:277-279` sends every numeric column to `numeric_stats`
  (`:407-461`): `min`, `max`, `mean`, `std`, five quantiles `p05..p95` (`:428-433`) and
  `np.histogram` with `DEFAULT_NUMERIC_BINS = 10` equal-width bins over min to max (`:106`,
  `:444-451`). Floats are rounded by `_round` to 12 decimal places (`:587`). Nothing else
  describes the shape. Module size 596 lines on `c15f1c37`, 599 once #456 (row 24w) merges.
- Sample. `generation/statistical/_sample.py:305-330` reads only `bin_edges` and `bin_counts`;
  `_numeric_row` (`:195-205`) picks a bin by cumulative count, then a uniform value inside it (two
  `rng.random()` calls). Typing per #445: `emits_int` (`_cell_format.py:331-339`) and
  `format_numeric` (`:322-328`) round and clamp to `[edges[0], edges[-1]]`.
- Why it breaks. With one 1,000,000 among values near 100, bin one spans about 0 to 100,000 and
  holds 99.99% of rows, so the uniform draw inside it lands anywhere in that span. Skew does the
  same at a smaller scale. A four-price column has its mass in two or three bins and is drawn
  uniformly between prices.
- Low-distinct numbers. A numeric dtype never becomes `categorical`: the 30-distinct cap
  (`_CATEGORICAL_DISTINCT_CAP`, `snapshot.py:118`, renamed by this row to public
  `CATEGORICAL_DISTINCT_CAP`) applies to object columns only (`:283-287`).
- Disclosure today. `load_spec` (`_spec.py:299-581`) asks for `allow_real_categories: true` only on
  categorical columns (`:466-479`); numbers need no consent, and an int64 column already emits real
  integers through `round()`. A redacted fit (row 24f, `profiling/_snapshot_values.py:131-146`,
  rules in `profiling/_value_redaction.py:111-127`) rewrites a sensitive number column to one
  coarse bin and nulls `mean`, `std`, `quantiles`; any stats key the registry does not classify is
  nulled (default deny, `_value_redaction.py:360-370`). `_refuse_withheld_values`
  (`_spec.py:243-296`) refuses only categorical or conditioned columns.
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
  histogram this row replaces.
- Disclosure floor. Statistical disclosure control top and bottom coding and the minimum-frequency
  threshold rule (Hundepool et al., Statistical Disclosure Control, Wiley 2012, sections 3.4.3
  and 4.2; R sdcMicro `topBotCoding`), already cited in `profiling/_value_redaction.py`. Agencies
  use thresholds from 3 to 11 (the CMS cell-size suppression policy suppresses cells under 11).
  Exact mode applies the minimum-frequency rule per value. Grid mode applies the same `K` as a
  minimum source-row mass for every adjacent interval and tail, while plain snapshot knots remain
  value-bearing as they are today.
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
than 30 distinct finite cells (or a low-distinct column that fails the exact-mode rule of 4.2):

1. `K = MIN_RELEASE_COUNT = 10`; `t = max(0.001, K / n)`.
2. The ladder, in this order, is the 103 probabilities
   `[0.001, 0.005, 0.01, 0.02, 0.03, ..., 0.98, 0.99, 0.995, 0.999]`: 0.001 and 0.005, then
   `k / 100` for `k = 1..99`, then 0.995 and 0.999. Each is computed as `k / 1000` from an integer
   `k` so no float step accumulates.
3. Candidates, ascending: `t`, then every ladder point strictly inside `(t, 1 - t)`, then `1 - t`.
4. Thinning, left to right: keep a candidate `p` only when `(p - last_kept) * n >= K - 1e-9` and
   `(1 - t - p) * n >= K - 1e-9`. The ends are always kept. Every interval between grid points
   then holds at least `K` rows, and so does each tail beyond the ends.
5. `values = np.quantile(finite, probs, method="linear")`, stored unrounded as Python floats
   (JSON `repr` round-trips them exactly). `probs` are stored as computed.
6. Clamp each value into `[bin_edges[0], bin_edges[-1]]` of the same column's stats. `_round` can
   move an edge inward by up to `5e-13` (a tied maximum of `1.0000000000004` gives an edge of
   `1.0`); the clamp moves a grid value only toward the interior, so it stays inside the `K`-th
   extremes and always passes the decoder's bounds check.

Measured point counts: `n = 100` gives 9 points (0.1 to 0.9 by 0.1); `n = 150` gives 13;
`n = 1,000` gives 99; `n >= 10,000` keeps all 103 (about 2.4 KB of JSON per column).

Why 12-decimal-place rounding was dropped (H1). On the AT-21a input, `_round` would change the top
grid value of a tied `20/3` maximum from `6.666666666666667` to `6.666666666667`. Grid values remain
unrounded. AT-22 separately proves that integer formatting and its clamp preserve the release
floor; it is not rounding evidence for `_round`.

Sample. When a numeric spec carries a grid (and `carrier is None`):

- Treat the first probability as 0 and the last as 1, so the tail mass beyond the ends is drawn
  inside the outer intervals. Interior quantiles stay exact; no value past the ends is made.
- Per row: `rng.seed(col_seed + start + i)`; `u = rng.random()`;
  `j = min(bisect_right(P, u) - 1, len(P) - 2)`;
  `x = v[j] + (u - P[j]) / (P[j+1] - P[j]) * (v[j+1] - v[j])`. One random draw per value (the
  histogram path uses two). Equal neighbours (a plateau) give that value exactly. The null draw
  follows as today.
- Typing per #445, unchanged rules: with a `cell_format`, `format_numeric(x, fmt, v[0], v[-1],
  as_int=emits_int(spec))`; without one, `int(min(max(round(x), v[0]), v[-1]))` when
  `emits_int`, else `x`. The clamp bounds are the grid ends, not `bin_edges`. `emits_int` and
  `cell_format_arrow_type` are not touched. Above 2**53 the grid works at float64 resolution
  (one unit in the last place is 2 between 2**53 and 2**54); the floor holds when compared as
  floats (AT-23).
- No grid (old snapshot, fewer than 100 rows, redacted fit, DP carrier): the histogram path, byte
  for byte.

Why the outlier case works: `1 - t` with `t >= K/n` sits at or below the `K`-th largest cell, so
the top grid point is below every value held by fewer than `K` rows above it. The one 1,000,000
row never enters the grid. A cluster of `K` or more far rows does enter (AT-5): it is a real part
of the distribution.

### 4.2 (b) Low-distinct numbers: exact values

Fit, for a numeric column with at most `CATEGORICAL_DISTINCT_CAP` distinct finite cells, using the
same public symbol that makes an object column categorical:

- The distinct count and the per-value counts run on the original series (an integer dtype stays
  integer, so two int64 values that share a float64 never merge), not on the float finite cells.
- Values with at least `K` rows are kept with their counts; values with fewer than `K` rows are
  dropped, and their row total is written as `dropped_count` (a count, value-free).
- Exact mode applies when the dropped rows are at most 1% of finite cells. Stored:
  `"exact_values": {"values": [...], "counts": [...], "dropped_count": int}`, values strictly
  ascending. Values are written as the source cells: Python `int` for an integer dtype (no float
  detour, so an int64 above 2**53 is stored and emitted exactly), `float` otherwise (JSON `repr`
  round-trips exactly). No rounding.
- Otherwise (more than 1% of rows on rare values): a grid when `n >= 100`, else nothing new.

Exact mode and a grid are never both written for one column; the decoder refuses a block with both.

Sample: `rng.choices(values, weights=counts)`, the categorical draw; dropped mass is redistributed
the way categorical `other_mode: redistribute` does. Output is `int(v)` when `emits_int(spec)`,
else `float(v)`; no `format_numeric` (the values are source cells already).

Disclosure (H2). Exact values need no consent. Exact mode has a true per-value `K` floor. Grid
mode instead guarantees `K` source rows in each adjacent interval and tail:

- A grid can expose a singleton interior knot because an empirical quantile may coincide with a
  source value held by fewer than `K` rows. That is the same class of exposure already present in
  a plain snapshot's stored `p05`/`p50`/`p95` quantiles and `min`/`max` `bin_edges`. Plain
  snapshots are value-bearing artifacts; redacted fits withhold the grid through
  `SNAPSHOT_VALUE_STATS` (AT-12 and AT-24).
- Exact mode is stronger per value: every stored and emitted exact value is held by at least `K`
  rows. A value held by fewer than `K` rows is omitted from `exact_values`.
- Categorical columns keep `allow_real_categories` unchanged (their `top_values` have no count
  floor). Whether categories move to the same floor is a recorded follow-up, not this row.

| Situation | Result |
|---|---|
| Plain snapshot, column has `exact_values` | Exact values emitted; no flag needed |
| Redacted fit (`distribution_snapshot_document`, `include_values=False`), sensitive column | `exact_values` and `quantile_grid` are nulled (registry rule `NULL`); generation takes today's one-coarse-bin path |
| `include_values=True` | Both kept; the `raw` marker already says so |
| Sensitive column listed in `redacted_columns` but a hand edit restored a non-null `exact_values` or `quantile_grid` | Refused, `plan.statistical_snapshot_values_redacted` |
| DP-verified column (`carrier` set) carrying either key | Keys ignored; the DP path is unchanged |

Bounds check for exact values (H1). The decoder checks
`edges[0] - tol <= float(v) <= edges[-1] + tol` with `tol = 5e-13 + abs(edge) * 2**-52`: half the
12-decimal rounding unit of `_round` plus one float unit at that edge. An int64 above 2**53
compares as `float(v)` against `float(max)`, which are equal for the largest value. Grid values
need no tolerance (they are clamped at fit).

### 4.3 Snapshot format and old snapshots

No version bump. `schema_version` stays `distribution-snapshot/v1`; both keys are optional
additions under `stats` of a `numeric` column, the pattern `cell_format`, `fanout` and the
`high_cardinality` marker already use. A version bump would make plan compile
(`plan/_generation.py:120-127`) refuse every snapshot users already hold.

- Old snapshot, new engine: no keys, histogram path, byte-identical output (AT-9).
- New snapshot, old engine: the keys are ignored (the old sampler reads only the histogram), so
  it generates as before.
- `kind` stays `numeric`, so fidelity, shape fidelity, the diagnostic kind-drift check and the
  #445 typing all see the same kind as today.

The keys are written by default from `compute_distribution_snapshot`, so the CLI fit (default
arguments) and `distribution_snapshot_document` both carry them with no new keyword. Snapshot
bytes change for every fitted number column that qualifies.

Consumers inventoried (api-and-compatibility): CLI `fit` and `report fidelity` (keys ignored by
fidelity), plan compile pinning (hashes the file, unchanged), the platform (persists the dict,
parked), the compat corpus (old snapshot, histogram path), the five histogram readers in section 2
(unchanged keys).

### 4.4 Relation to row 24f (QA-FIT-REDACT) and row 24w

Row 24f is QA-FIT-REDACT (`docs/plans/2026-11-08-qa-fit-redact.md` in the 24f plan worktree); its
engine half is merged (#420). It built the redacted fit: `distribution_snapshot_document`, the
`SNAPSHOT_VALUE_STATS` registry with default deny, and the coarse range for sensitive number
columns. It has no count floor. This row plugs into that work and nothing else:

- Both new keys are classified `Rule.NULL` in the 24f registry, so a redacted fit drops them.
- `_refuse_withheld_values` counts a non-null `quantile_grid` or `exact_values` on a redacted
  column as a restored value.
- The coarse range keeps working: a redacted column has no grid, so it uses the one coarse bin.

`K` is this row's own constant, `MIN_RELEASE_COUNT = 10`, defined in
`profiling/_value_redaction.py` beside the registry it serves. Whether any other snapshot key gets
a count floor is a separate decision, not part of this row.

Row 24w (GEN-COND-CAP, #456, `2026-11-10-gen-cond-cap.md`) is named only because it takes
`quality/snapshot.py` to 599 lines; section 12 budgets from there.

## 5. Failure modes

- F1 Small columns. With `n` near 100, `t = 0.1`: the tails beyond p10 and p90 are drawn inside
  the outer intervals, so the output range is about p10 to p90. Accepted: it is what the floor
  costs. Under 100 rows nothing changes from today (unless exact mode applies).
- F2 Trimmed mean. A lone extreme row is not reproduced, so the output mean is the mean without
  it (AT-1: about 100 against a source mean of 150). Intended; owner question Q4.
- F3 A far cluster of `K` or more rows is smeared across its grid interval (AT-5: 0.46% of rows
  above 200 against 0.25%). Bounded by the interval width; the ladder is finer in the tails to
  keep it small.
- F4 Rounding or edge drift puts a grid value outside `bin_edges` and the decoder refuses a
  snapshot the engine wrote itself. Closed by storing values unrounded and clamping at fit
  (AT-21); the clamp is the one place that reconciles the two precisions. AT-22 separately checks
  the integer output clamp against the floor.
- F5 Rare values in an exact column are never emitted (AT-4). That is the floor.
- F6 A hand-written or corrupted block (decreasing probabilities or values, values outside
  `bin_edges`, both keys present, bad counts) is refused with `plan.statistical_stats_degenerate`
  at load, never sampled silently.
- F7 Snapshot size grows by about 2.4 KB per qualifying number column. Accepted; a 200-column
  table grows by under 0.5 MB.
- F8 A builder loosens a bound or re-pins a golden to pass. Every bound in section 8 is fixed;
  section 10 lists the only goldens that may move.
- F9 Old snapshots keep the C2 defect until re-fit. Changelog says so; owner question Q6.
- F10 Int64 above 2**53 on the grid path loses exactness to float64 (AT-23). Accepted: the floor
  still holds as floats, and an exact column keeps exact ints.

## 6. Choke points and design notes

- One fit choke point: `add_numeric_shape(columns, df)`, called once from
  `compute_distribution_snapshot` after the per-column pass (beside `add_cell_format`), lazily
  imported like `add_fanout` and `add_cell_format` (`generation` imports `quality`). Both fit
  paths (CLI default call and `distribution_snapshot_document`) go through it. It skips
  `high_cardinality` columns (they are categorical) and non-numeric kinds. Guard: AT-12 and the
  `numeric_only` golden fail if a fit path bypasses it; mutation: drop the call, both go red.
- One distinct-cap owner: public `CATEGORICAL_DISTINCT_CAP` in `quality/snapshot.py`, exported by
  `decoy_engine.quality`. Object classification and `add_numeric_shape` both consume that symbol.
  Guard: AT-18 exercises the boundary through both paths. Mutation: change the constant from 30
  to 29 and both the 30-distinct object case and the 30-distinct numeric case turn red.
- One read choke point: `decode_numeric_shape(column, stats, carrier) -> NumericShape | None`,
  called by `load_spec` (so compile and validate see the errors) and by `sample_column` (to build
  the tables). Every check (types, order, bounds, both keys, counts) and the withheld-key test
  (`numeric_shape_withheld(stats) -> bool`) live in `_numeric_shape.py`; `_spec.py` adds call
  lines only. The spec dataclass and `spec_to_dict` do not change: the keys travel inside
  `stats`, so the serialized Plan format is unchanged.
- Design notes: one module owns both halves, as `_cell_format.py` does
  (`generation/statistical/_numeric_shape.py`); a frozen `NumericShape` dataclass is the typed
  context the sampler receives instead of loose lists; no flag argument (the sampler dispatches on
  which table the decoder returned). When validation catches a library exception, it records only
  a value-free failure token, exits the `except` block, and then raises `StatisticalSpecError(...)`
  from the no-active-exception path. This makes both `__cause__` and `__context__` `None`. No other
  new structure.
- The class this fixes: "a sampler reads a summary that one row can stretch". The construction
  fix is the floor in the grid; the guard is the property test AT-17. No new sentry.

## 6a. Enumerations (from the code at engine `c15f1c37`)

### Routes that produce statistical number values

- In-memory generation: `sample_column` numeric branch, `generation/statistical/_sample.py:305`;
  grid, exact or histogram chosen by the decoder.
- Pinned-snapshot route: plan compile reads the pinned bytes once (`plan/_generation.py:120`) and
  calls `load_spec`, which runs the same decoder (`generation/statistical/_spec.py:466` area).
- Windowed route: window admission checks the `bin_edges` span (`generation/_window_admission.py:244`);
  grid and exact values sit inside `bin_edges` (clamp and tolerance), so admission is unchanged,
  and each window reseeds per row. AT-10 proves full-run parity for both modes.
- DP carrier route: `carrier` set means the histogram path; both keys are ignored (AT-15a/b).

### Exit paths

- `load_spec` refusal for a malformed block: `plan.statistical_stats_degenerate`, raised after any
  translating `except` block has closed, with `__cause__ is None` and `__context__ is None`
  (AT-14a/b).
- `_refuse_withheld_values` refusal for a restored non-null key on a redacted column:
  `plan.statistical_snapshot_values_redacted` (`_spec.py:243`, AT-13a/b).
- Normal return: values drawn; no new exception type, no partial output (pure functions).

### FK shapes

N/A: number columns take no `condition_on` or FK fan-out in the statistical generator; the grid and
exact draws are per column only, and the FK paths do not read `stats`.

### Fields (every key this plan adds)

- `stats.quantile_grid.probs`: strictly increasing floats in `(0, 1)`, value-free.
- `stats.quantile_grid.values`: non-decreasing floats, value-bearing, registry `NULL` when redacted.
- `stats.exact_values.values`: strictly ascending source cells, value-bearing, registry `NULL`.
- `stats.exact_values.counts` and `stats.exact_values.dropped_count`: ints, value-free, nulled
  with their container.

### Output surfaces

- Snapshot JSON (`quality/snapshot.py:407` numeric stats, plus the new keys): section 7 rules.
- Generated column values: the floor keeps every emitted value inside the `K`-th extremes.
- Error messages from the decoder: column, member and counts only.
- Fidelity and shape-fidelity reports: unchanged keys, unchanged shape.
- Logs: no new log line.

## 7. Value-free rules

- Snapshot: both keys hold values and are registered in `SNAPSHOT_VALUE_STATS` as
  `("numeric", "quantile_grid"): Rule.NULL` and `("numeric", "exact_values"): Rule.NULL`, so a
  redacted fit drops them. Their nested members (`probs`, `counts`, `dropped_count`) are not
  separately classified; the whole container is nulled.
- Errors: decoder messages name the column, the member and counts, never a grid value or an exact
  value. AT-14a/b assert a source value does not appear.
- No new log line. No new report field. Fidelity output shape unchanged.
- Docs, changelog and test names use synthetic numbers only.

## 7a. Class table

| # | Class | Where it applies | Structure that closes it | Proof | Mutation that must turn it red |
|---|---|---|---|---|---|
| 1 | Value-bearing output | the two new keys in a redacted fit; decoder messages | registry rule `NULL` for both keys under default deny; messages name column, member and counts only | AT-12, AT-14a/b, registry sentry | remove the `quantile_grid` registry entry (AT-12 and the registry sentry red) |
| 2 | Cleanup missing on an exit path | none | N/A: pure functions, nothing opened or staged | n/a | n/a |
| 3 | State machine and concurrency | none | N/A: per-row reseed over an immutable spec, no cache | AT-10 | n/a |
| 4 | Route parity and silent fallback | in-memory, pinned, plan-load and windowed routes; DP carrier | one decoder used by `load_spec` and `sample_column`; carrier set means the histogram path | AT-10, AT-15a/b | decode only in `sample_column` (AT-14a at `load_spec` red) |
| 5 | Referential integrity | none | N/A: no joint or `condition_on` for numbers | n/a | n/a |
| 6 | Untrusted producer | a hand-edited snapshot block; a restored block on a redacted column | strict decoder (types, order, bounds, no unknown member, not both keys); withheld-values refusal | AT-13a/b, AT-14a/b | accept a `bool` count (AT-14b red) |
| 7 | Check then use (TOCTOU) | the snapshot path | unchanged: specs come from the read-once pinned bytes | existing pinned tests | n/a, no new read |
| 8 | Weak tests | band tests that a broken sampler could pass; tests that pass with no grid | two-sided bounds; source-property checks; `"quantile_grid" in stats` preconditions | AT-1, AT-5, AT-6, AT-10, AT-11a, AT-15a, AT-16 | replace the grid draw with the histogram draw (AT-1 red) |
| 9 | Refactor drift | old snapshots | absent keys mean the histogram path, byte for byte | AT-9 | always build an empty grid (AT-9 red) |
| 10 | Frozen contract drift | `distribution-snapshot/v1`, determinism, Plan serialization, draw-site catalogue | additive optional keys; `StatisticalSpec` unchanged; catalogue re-pin | AT-9, `test_draw_sites_catalogue_pinned.py`, compat corpus | write `quantile_grid: null` for every column (`numeric_only` and compat checks red) |

## 8. Acceptance tests (red first)

Shared harness, unless stated: `src` built as named;
`snap = compute_distribution_snapshot(pd.DataFrame({"x": src}))`;
`stats = snap["columns"]["x"]["stats"]`; `spec = load_spec(cfg, snapshot=snap)` with
`cfg = {"name": "x"}`; `out = sample_column(spec, 20_000, col_seed=7)`. Value similarity is
`compute_fidelity(snap, compute_distribution_snapshot(pd.DataFrame({"x": out})))["marginal"]["columns"][0]["value_similarity"]`
(the FID-RANGE IQR scale). Each `default_rng(seed)` is fresh with exactly the calls named.
"Proto" is a prototype of this design run on `c15f1c37` with the same seeds; bounds have margin
over it. "Clean chaining" means `err.__cause__ is None and err.__context__ is None`.

Regression controls expected to pass on the base before implementation: AT-7, AT-9 and AT-15a/b.
All other ATs are fail-before feature or validation proofs, or require a new helper that does not
exist on the base.

Grid, new file `tests/unit/generation/statistical/test_numeric_grid.py` unless noted:

| # | Test | Input | Bound | Today / proto |
|---|---|---|---|---|
| AT-1 | `test_one_outlier_does_not_move_the_bulk` | `np.append(default_rng(501).normal(100.0, 10.0, 19_999), 1_000_000.0)` | `95 <= mean(out) <= 105`; `abs(median(out) - median(src)) <= 1.0`; no value above 200; `max(out) <= sorted(src)[-10]`; value similarity `>= 0.95` | mean 49,850, sim 0.0 / mean 99.74, median 99.78, max 131.6, sim 0.975 |
| AT-2 | `test_lognormal_keeps_its_median` | `default_rng(502).lognormal(3.0, 1.2, 20_000)` | median within 5% of source; mean within 8%; p95 within 10%; similarity `>= 0.85` | median 78.0, sim 0.0 / median 19.66, mean 40.33, p95 139.9, sim 0.920 |
| AT-5 | `test_a_far_cluster_of_k_rows_is_kept` | `np.append(default_rng(505).normal(100.0, 10.0, 19_950), np.full(50, 1_000_000.0))` | `0.8 <= mean(out)/mean(src) <= 1.2`; share above 200 in `[0.001, 0.01]` | mean 52,006 / mean 2,462 (source 2,600), share 0.46% |
| AT-6 | `test_zero_inflated_keeps_its_zeros` | `np.append(np.zeros(12_000), default_rng(506).gamma(2.0, 30.0, 8_000))` | share of exact `0.0` within 0.01 of 0.60; median 0; similarity `>= 0.90` | 0% zeros, sim 0.697 / 59.8%, sim 0.959 |
| AT-7 | `test_a_normal_column_stays_good` | `default_rng(504).normal(50.0, 5.0, 20_000)` | similarity `>= 0.95` | 0.965 / 0.976 |
| AT-8 | `test_grid_needs_one_hundred_rows` | `default_rng(507).normal(100, 10, 100)` and its first 99 | 100 rows: grid present with 9 points, probs `0.1` to `0.9`; `min(out) >= sorted(src)[9]`, `max(out) <= sorted(src)[-10]`; 99 rows: no `quantile_grid` key | 9 points (measured) |
| AT-9 | `test_old_snapshot_is_byte_identical` | four snapshots (AT-7 source; an int64 column with a `cell_format`; AT-1 source; AT-3 low-distinct source) with both new keys deleted if present | for every grid and exact candidate, `sample_column` output digest equals the digest recorded on `main` before the change (record it in the red commit) | must be equal |
| AT-10 | `test_numeric_shape_windows_equal_a_full_run` (parametrized over grid and exact) | AT-2 grid snapshot and AT-3 exact snapshot; preconditions assert the selected non-null key is present | for each mode, `sample_column(..., start=k)` for `k` in 0, 1, 4,999 equals the full run's slice; two full runs are equal | |
| AT-11a | `test_grid_typing_follows_445` | int64 source `default_rng(508).integers(0, 1_000, 5_000)`; float source as AT-7; precondition `assert "quantile_grid" in stats` for both | int64: every value is `int` and `cell_format_arrow_type` is `int64`; float: every value is `float` | |
| AT-12 | `tests/unit/profiling/test_snapshot_values_numeric_shape.py::test_sensitive_number_column_drops_the_grid` | the existing `force_sensitive` fixture on an AT-7-shaped column, and on an AT-3-shaped column | in the document, `"quantile_grid" in stats and stats["quantile_grid"] is None` (and the same for `exact_values` on the AT-3 column); generation stays inside the coarse bounds; `include_values=True` keeps both keys non-null | |
| AT-13a | `test_restored_grid_on_a_redacted_column_is_refused` (same new profiling file) | redacted document, grid copied back from the raw snapshot | `plan.statistical_snapshot_values_redacted`; a redacted document with the key left `None` loads | |
| AT-14a | `test_malformed_grid_is_refused` (parametrized) | probs not strictly increasing; a prob outside `[0, 1]`; values decreasing; lengths differ; fewer than 2 points; a `bool` or string member; a value outside `bin_edges`; an unknown member | `plan.statistical_stats_degenerate` at `load_spec`; message contains no grid value; clean chaining. Equal neighbouring values load | |
| AT-15a | `test_dp_carrier_ignores_a_grid` | a DP-verified fixture with a grid added; precondition `assert "quantile_grid" in stats` | output equals the same fixture without it | |
| AT-16 | `test_grid_is_small` | AT-1 snapshot; precondition `assert "quantile_grid" in stats` | `len(json.dumps(stats["quantile_grid"])) <= 3_000`; `len(probs) <= 103` | 2,422 bytes |
| AT-17 | `test_grid_floor_properties` (hypothesis, `max_examples=200`, derandomized) | float arrays, `n` 100 to 2,000, with up to 5 values at `+-1e12` | probs strictly increasing; values non-decreasing; every `(p[i+1] - p[i]) * n >= K - 1e-9`; `v[0] >= sorted(src)[K-1]`; `v[-1] <= sorted(src)[-K]`; `bin_edges[0] <= v[0]` and `v[-1] <= bin_edges[-1]`; `load_spec` accepts; 1,000 drawn values inside `[v[0], v[-1]]` | |
| AT-19 | `test_grid_value_math` | `probs [0.1, 0.5, 0.9]`, `values [0, 10, 20]` through the pure draw function | `u=0.0 -> 0`; `u=0.25 -> 5`; `u=0.5 -> 10`; `u=0.75 -> 15` (ends treated as 0 and 1) | |
| AT-21 | `test_grid_values_stay_inside_the_edges` (parametrized) | (a) `np.append(1.0 + 0.01 * np.arange(200), np.full(20, 20 / 3))`; (b) `np.append(0.5 + 0.002 * np.arange(200), np.full(20, 1.0000000000004))` | (a) `v[-1] == 20 / 3` exactly; (b) `v[-1] == bin_edges[-1] == 1.0` (clamped); both: `v[0] >= sorted(src)[9]`, every value inside `bin_edges`, `load_spec` accepts | 12-decimal-place rounding gives `6.666666666667`; unclamped (b) gives `1.0000000000004 > 1.0` |
| AT-22 | `test_thirteen_digit_integers_keep_the_floor_after_integer_formatting` | `1_234_567_890_123 + np.arange(500)` (int64) | unrounded `v[0] == 1_234_567_890_132.98`; `load_spec` accepts; every output is an `int` inside `[sorted(src)[9], sorted(src)[-10]]` | integer-formatting and floor test only |
| AT-24 | `test_all_distinct_grid_has_interval_mass_and_redacts` | `range(101)` | plain fit has a grid; every adjacent probability interval and each tail has mass at least `K / 101`; every draw is in `[sorted(src)[9], sorted(src)[-10]]`; the existing `force_sensitive` redacted fit with `include_values=False` has `"quantile_grid" in stats and stats["quantile_grid"] is None` | singleton interior knots are accepted only in the plain value-bearing artifact |
| AT-23 | `test_int64_above_two_to_the_53` (parametrized) | (a) `[2**53 + 1, 2**53 + 3, 2**53 + 5] * 40` (int64); (b) `2**53 + 2 * np.arange(500)` (int64) | (a) `exact_values.values == [2**53 + 1, 2**53 + 3, 2**53 + 5]` as Python `int`, `load_spec` accepts, `set(out)` equals those three ints exactly; (b) `load_spec` accepts, every output `int`, `float(min(out)) >= float(sorted(src)[9])`, `float(max(out)) <= float(sorted(src)[-10])` | |

Exact values, new file `tests/unit/generation/statistical/test_numeric_exact.py`:

| # | Test | Input | Bound | Today / proto |
|---|---|---|---|---|
| AT-3 | `test_four_prices_stay_four_prices` | `default_rng(503).choice([4.99, 9.99, 19.99, 49.99], p=[0.4, 0.3, 0.2, 0.1], size=20_000)`, `cfg = {"name": "x"}` (no consent flag); run on the plain snapshot and on `distribution_snapshot_document(..., include_values=True)` (with `cell_format`) | `load_spec` accepts; every value is one of the four prices; each share within 0.015 of the source share; similarity `>= 0.99` | 0.18% on a price, sim 0.885 / 100%, largest share gap 0.0066, sim 1.0 |
| AT-4 | `test_a_rare_value_is_never_emitted` | `np.append(default_rng(503).choice(<same four prices and p>, size=19_999), 999.99)` | `exact_values.values` are the four prices, `dropped_count == 1`; `999.99` not in the output | proto: confirmed |
| AT-11b | `test_exact_typing_follows_445` | int64 `[1, 2, 3]` repeated to 3,000 rows; float `[0.5, 1.5]` repeated to 2,000 | int64: every value `int`; float: every value `float` | |
| AT-13b | `test_restored_exact_values_on_a_redacted_column_are_refused` | as AT-13a with `exact_values` | `plan.statistical_snapshot_values_redacted` | |
| AT-14b | `test_malformed_exact_values_are_refused` (parametrized) | values not strictly increasing; a count of `0` or a `bool`; `dropped_count < 0`; lengths differ; empty; both keys present; a value outside `bin_edges` plus `tol` | `plan.statistical_stats_degenerate`; no value in the message; clean chaining. In-tolerance row: values `[0.5, 1.0000000000004]` with `bin_edges[-1] == 1.0` loads | |
| AT-15b | `test_dp_carrier_ignores_exact_values` | DP-verified fixture with `exact_values` added | output equals the fixture without it | |
| AT-18 | `test_exact_mode_threshold` | numeric: 30 distinct values x 20 rows; 31 distinct x 20 rows; 25 values x 10 rows plus five singleton values (30 distinct, 5/255 rare); object: 30 and 31 distinct values | numeric 30: exact, no grid; numeric 31: grid, no exact; numeric 30 with 5/255 rare: grid, no `exact_values`; object 30 is categorical and object 31 is freetext | |
| AT-20 | golden fixture `numeric_low_distinct` in `tests/snapshots/test_distribution_snapshot_baseline.py` | `[10, 20, 30] * 40` int64 | new golden file | |

Mutation per mechanism (each must turn a named test red; record it in the PR): tail floor `t`
replaced by 0 (AT-1, AT-17); thinning removed (AT-17); ends kept at `t`/`1-t` instead of 0/1
(AT-19); 12-decimal-place rounding put back (AT-21a); fit clamp removed (AT-21b); remove the
integer output clamp (AT-22);
exact bounds check without `tol` (AT-14b in-tolerance row); grid written for a low-distinct column (AT-18); rare
values kept (AT-4); registry rule removed (AT-12, registry sentry); carrier check removed
(AT-15a/b); `add_numeric_shape` call dropped (AT-12, `numeric_only` golden); move a translated
raise back inside its `except` block (AT-14a/b); change `CATEGORICAL_DISTINCT_CAP` from 30 to 29
(both 30-distinct object and numeric cells in AT-18).

Must pass unchanged: `tests/unit/generation/test_statistical.py` (including
`test_distribution_shape_matches_source`), `test_statistical_redacted.py`,
`tests/unit/generation/statistical/test_cell_format_sampling.py`, `tests/unit/quality/`,
`tests/unit/profiling/test_snapshot_values.py` except the re-baseline in section 10,
`tests/integration/compat_corpus/`. A failure there is a stop and a report.

## 9. Typing, CLI and native

- #445 typing is kept: `emits_int` and `cell_format_arrow_type` are untouched; both new draw paths
  end in the same `int`/`float` decision (AT-11a, AT-11b, AT-23).
- CLI: no change. `decoy fit` gets the keys through the default call (`cli/fit.py:422`). At the
  next engine pin bump, any CLI test that pins fit bytes re-pins with this row cited, and the QA
  record's C2 and H3 rows take re-test numbers.
- Native: no native statistical sampler exists. The draw-site entry `gen.statistical_per_row`
  (`execution/native/_draw_sites_gen.py:121-145`) changes its `call_shape` to name "histogram,
  quantile-grid (one draw) or exact-value draw"; re-pin the digests in
  `tests/native/test_draw_sites_catalogue_pinned.py` in the same commit, after showing the old
  catalogue reproduces the old digest.
- Window admission (`generation/_window_admission.py:244-260`) keeps checking `bin_edges`; grid
  values are clamped inside `[bin_edges[0], bin_edges[-1]]` and exact values sit inside within
  `tol`, so its int64 span check still bounds every output. No admission change.

## 10. Goldens, pins and sentries that move

Each move is intentional and written in the diff:

- `tests/snapshots/golden/distribution_snapshot/numeric_only.sha256` (100 distinct normal values,
  so a grid is written). `mixed_with_joint` does not move (salary: 6 distinct values of 5 rows
  each, 30 rows: every value is under `K`, so no exact mode, and 30 rows is under 100, so no grid);
  the other fixtures are not numeric.
- New golden `numeric_low_distinct.sha256` (AT-20).
- `tests/native/test_draw_sites_catalogue_pinned.py` digests (call-shape text).
- `tests/sentry/profiling/test_snapshot_value_registry.py` (H3). On the current fixture
  (`_N = 40`, `:31`) `test_every_visited_key_is_classified_and_every_entry_is_visited`
  (`:127-131`) goes red because no column reaches 100 rows, so the two new registry entries are
  never visited, and the parametrized `test_every_value_entry_removes_what_the_raw_snapshot_holds`
  (`:179-189`) fails "the fixture never filled this slot". Fixture change: raise `_N` to 120
  (`_BALANCE` is built from `_N`, so `balance` stays a sensitive column with more than 30
  distinct values and gains a grid), and add a sensitive low-distinct number column whose three
  values each sit on 40 rows (it gains `exact_values`). The builder picks a column name the
  existing sensitivity rule flags and asserts it is in `redacted_columns`. Every other assertion
  in the file must pass unchanged except one: `:203` asserts `after == [[_N]]` for `bin_counts`,
  which a second sensitive number column turns into `[[_N], [_N]]`; change it to
  `assert after and all(a == [_N] for a in after)`. (Alternative if the builder prefers: keep
  `_frame()` as is and give the `exact_values` cases their own fixture frame.) Note for the
  `COARSE_RANGE` branch (`:199-208`): `raw_numbers` now also holds grid values and exact counts
  (40 and 120 here), so the new column's values (for example `31000.5`, `52000.5`, `73000.5`) must
  be chosen so the coarse bounds written at `:205-208` share no number with `raw_numbers`; the
  builder checks this on the first run rather than editing the assertion.
- A test that compares a redacted numeric entry against an exact key set
  (`test_snapshot_values.py::test_a_numeric_canary_column_keeps_a_coarse_unit_range_only`, if it
  lists keys) gains `quantile_grid: None` and `exact_values: None` where they apply.
- Any generation test that fits in process a number column that now qualifies (at least 100 rows
  and more than 30 distinct values, or at most 30 distinct values each on at least `K` rows) and
  pins output values. The builder lists them from the red-commit T1 run; each must be explained
  by "this column now has a grid" or "this column now has exact values".
- Not moving: `quality_fidelity`, `quality_report`, `quality_shape_fidelity` goldens (they hash
  reports, which read only the unchanged stats), the compat corpus (old snapshot, histogram path).

Regenerate with `UPDATE_SNAPSHOTS=1`. Any other moved digest or pinned number is a stop and a
report.

## 11. Changelog fragment

`changelog.d/gen-num-shape.md`, not Breaking: same snapshot plus same seed still gives the same
output, no new required flag, old snapshots unchanged.

```
### Changed (fitted number columns keep their shape, GEN-NUM-SHAPE, 2026-11-10)

A fit now stores a quantile grid (`stats.quantile_grid`) for each number column with at least
100 values and more than 30 distinct values, and statistical generation samples from it instead
of ten equal-width bins. One extreme value no longer spreads generated rows across the whole
range, and skewed columns keep their median. Every interval and tail has at least 10 source rows,
and generated numbers stay between the 10th smallest and 10th largest source value. An interior
grid knot can equal a source value held by fewer than 10 rows, as today's plain value-bearing
snapshot quantiles and bin edges already can. A redacted fit withholds the grid.

A number column with 30 or fewer distinct values stores those values (`stats.exact_values`),
leaving out any value held by fewer than 10 rows, and generation emits them at their source
shares. No extra flag is needed: every stored value is held by at least 10 source rows.

Snapshots fitted by earlier versions have neither key and generate exactly as before; re-fit to
get the new shape. A redacted fit drops both keys for sensitive columns.
```

## 12. Placement (checked against `CODEMAP.md`)

| File | Change | CODEMAP row | Size |
|---|---|---|---|
| `src/decoy_engine/quality/snapshot.py` | rename `_CATEGORICAL_DISTINCT_CAP` to public `CATEGORICAL_DISTINCT_CAP` with no line growth; object classification consumes it; lazy `add_numeric_shape(columns_block, df)` call after the column pass (+3 lines); docstring numeric row | quality (`CODEMAP.md:44`) | Budget from 599 (after #456): 599 + 3 = 602, so trim 1 is required: condense the docstring's "What this module does NOT do" list (`:35-40`, six lines) to two lines (-4), ending at 598. Trim 2 if a rebase adds lines: hoist the three lazy imports (`add_fanout`, `add_cell_format`, `add_numeric_shape`, `:198-205`) into one import line with one cycle comment before the post-pass block (-4). Must end at or under 600; no census entry |
| `src/decoy_engine/quality/__init__.py` | add lazy public export for `CATEGORICAL_DISTINCT_CAP`; numeric-shape admission imports it through this package API | quality (`CODEMAP.md:44`) | +2 |
| `src/decoy_engine/generation/statistical/_numeric_shape.py` | new: fit half (`add_numeric_shape`, grid, exact values, clamp) and read half (`NumericShape`, `decode_numeric_shape`, every check, `numeric_shape_withheld`, `grid_value`); imports public `CATEGORICAL_DISTINCT_CAP` from `decoy_engine.quality`; sits beside `_cell_format.py`, which has the same two halves | `CODEMAP.md:58` (statistical generation) and the "Generation" row of "Where Things Go" | about 330 |
| `src/decoy_engine/generation/statistical/_sample.py` | numeric branch dispatches grid, exact or histogram; docstring methodology | `:58` | 439 to about 470 |
| `src/decoy_engine/generation/statistical/_spec.py` | call lines only: `load_spec` calls `decode_numeric_shape` (carrier None only); `_refuse_withheld_values` calls `numeric_shape_withheld` | `:58` | 581 to about 585, must end at or under 600 |
| `src/decoy_engine/generation/statistical/__init__.py` | export `add_numeric_shape` | `:58` | 51 to 52; the engine root `__init__.py` is not touched |
| `src/decoy_engine/profiling/_value_redaction.py` | `MIN_RELEASE_COUNT = 10` with its SDC citation; two `SNAPSHOT_VALUE_STATS` entries | `:106` | 469 to about 475 |
| `src/decoy_engine/profiling/__init__.py` | export `MIN_RELEASE_COUNT` beside `POLICY_RAW` | `:106` | +2 |
| `src/decoy_engine/execution/native/_draw_sites_gen.py` | `call_shape` text | native bridge row | same |
| `tests/unit/generation/statistical/test_numeric_grid.py`, `test_numeric_exact.py` | new | "Unit tests" row | folder 6 to 8 files |
| `tests/unit/profiling/test_snapshot_values_numeric_shape.py` | new (AT-12, AT-13) | "Unit tests" row | |
| `tests/sentry/profiling/test_snapshot_value_registry.py` | fixture `_N` 40 to 120, one new sensitive low-distinct number column, `:203` assertion widened (section 10) | sentry row | about +3 |
| `tests/snapshots/test_distribution_snapshot_baseline.py` and goldens | `numeric_only` re-pin; `numeric_low_distinct` fixture | snapshot harness | |
| `docs/distribution-integrity/snapshot-and-fidelity.md:65`, `statistical-generation.md:70,129,138`, `extending.md:113-114`, `index.md:86` | numeric stats row, sampler text, limits, C2 and H3 rows marked fixed | doc index | |
| `CODEMAP.md:58` | add `_numeric_shape.py` and the allowed lazy edge "`quality/snapshot.py` calls `add_numeric_shape`" | | |
| `changelog.d/gen-num-shape.md` | fragment | "Changelog fragments" row | |

One new source module, in `generation/statistical/` (6 files to 7); none in `quality/` (25 files,
at the flat-folder limit). The cap remains owned by `quality/snapshot.py`; the quality package's
lazy facade makes it public without adding a module. `generation` already imports `profiling`
(`_spec.py:59`), so reading `MIN_RELEASE_COUNT` adds no new layer edge. `_numeric_shape.py` imports it as
`from decoy_engine.profiling import MIN_RELEASE_COUNT`, exported through `profiling/__init__.py`
the same way as `POLICY_RAW`. No census entry,
no new reason code (refusals reuse existing codes), no new sentry.

## 12a. Reuse inventory

| Need | Reused | Not built |
|---|---|---|
| Quantile computation | `numpy.quantile(method="linear")` (Hyndman-Fan type 7) | a hand-rolled quantile |
| Inverse-CDF draw | the existing per-row `random.Random` reseed in `_sample.py` | a new RNG or stream |
| Exact draw | `rng.choices(values, weights=counts)`, the categorical draw | a new weighted sampler |
| Distinct cap | public `decoy_engine.quality.CATEGORICAL_DISTINCT_CAP` (30), consumed by object classification and numeric-shape admission | a second threshold |
| Redaction | the 24f `SNAPSHOT_VALUE_STATS` registry and its default deny | a per-key redaction branch |
| Withheld-value refusal | `_refuse_withheld_values` and `plan.statistical_snapshot_values_redacted` | a new code |
| Typing | `emits_int`, `format_numeric`, `cell_format_arrow_type` (#445) | new typing rules |
| Module shape | the `_cell_format.py` pattern (fit half plus read half, lazy import from `quality`) | a new package |

## 12b. Frozen contracts touched

| Contract | Change | Evidence |
|---|---|---|
| `distribution-snapshot/v1` | additive optional keys under numeric `stats`; no version bump | AT-9, compat corpus, `numeric_only` re-pin |
| Determinism (same snapshot plus seed gives same output) | holds per snapshot; old snapshots byte-identical | AT-9, AT-10 |
| Plan serialization (`spec_to_dict`) | unchanged: the keys travel inside `stats` | existing plan round-trip tests |
| Draw-site catalogue | `gen.statistical_per_row` call-shape text changes | `test_draw_sites_catalogue_pinned.py` re-pin in the same commit |
| Snapshot value registry | two new `Rule.NULL` entries | `tests/sentry/profiling/test_snapshot_value_registry.py` with the extended fixture |
| Reason-code registry | no new code | existing reason-code sentry |

## 13. Slices, gates and risk

One slice. With the consent gate gone (H2) the exact-values part is a fit branch, a decoder branch
and a sampler branch with no disclosure logic of its own, so it carries the same risk as the grid
and the whole row stays M.

| Slice | Branch | Size | Content | Tests and gate |
|---|---|---|---|---|
| GEN-NUM-SHAPE | `feat/gen-num-shape` | M | public `CATEGORICAL_DISTINCT_CAP`, `MIN_RELEASE_COUNT`, both registry entries, `_numeric_shape.py` (grid and exact fit, clamp, decoder, withheld check), sampler branches, `_spec.py` call lines, `snapshot.py` call and trim, draw-site text and re-pin, registry sentry fixture, `numeric_only` re-pin and `numeric_low_distinct` golden, docs, CODEMAP, fragment. All ATs in section 8. | T0 per edit; T1 `tests/unit/generation`, `tests/unit/quality`, `tests/unit/profiling`, `tests/unit/plan`, `tests/sentry/profiling`, `tests/snapshots`, `tests/native/test_draw_sites_catalogue_pinned.py`, `tests/integration/compat_corpus`. Full suite on the merge head (triggers: serialization of a frozen artifact, a shared test fixture), Mac first, diffed against the saved main baseline. Review: Dennis, then Codex. |

Risk: R2 (artifact format and generated output change; value-bearing keys in a snapshot). Not
Breaking.

Builder rules: Conventional Commits subject, 72 characters max, no body unless it explains why,
final line `cam`; T0/T1 per `testing.md` "Test depth"; never widen a bound, skip a test or re-pin
an unlisted golden to pass; mutate each mechanism once (section 8) and show the named test goes
red.

## 14. Owner questions (recommended answer first)

1. Q1 Consent for exact values. Recommend: none. Exact mode has a true per-value `K` floor. A
   plain grid can already expose a singleton interior knot, just as today's plain snapshot
   quantiles and bin edges can, while a redacted fit nulls both new value-bearing keys. A consent
   flag on `exact_values` would not change that artifact boundary and would make the change
   Breaking. Recommended by Dennis r1; owner accepted the recommendation (proceeding on
   go-recommended).
   Alternative: reuse `allow_real_categories` (blocks every re-fitted low-distinct column, for no
   added protection).
2. Q2 Release floor `K`. Recommend 10, this row's own constant (`MIN_RELEASE_COUNT`). Lower (5)
   keeps more of a small column's tails; 11 (the CMS cell rule) changes little. Applying a count
   floor to other snapshot keys is a separate decision.
3. Q3 Exact-mode threshold. Recommend 30 distinct values, the same cap that makes an object column
   categorical (DataSynthesizer uses 20). One number for both kinds.
4. Q4 A lone extreme value is not reproduced, so the generated mean in the outlier case is about
   100, not the source's 150. Recommend accept: reproducing it means emitting a value held by one
   row, which the floor exists to prevent.
5. Q5 The operator-authored `strategy: distribution` generator (`generators/_distribution.py`)
   keeps equal-width bins. Recommend V2-LOG: its snapshot is typed by hand, not fitted.
6. Q6 Old snapshots keep the defect until re-fit. Recommend the changelog note only, no runtime
   warning (pre-release, few old snapshots in use).
7. Q7 Accept singleton interior grid knots in plain (value-bearing) snapshots; redacted fits withhold the grid. Recommended: yes (owner proceeding on go-recommended).

## 15. Pre-mortem

- Band tests flake: all draws are seeded per row and the bounds sit well outside the proto
  numbers; a red band means a determinism or method change, not noise.
- `snapshot.py` crosses 600 lines: section 12 names two trims; a census entry is not an allowed
  fix.
- A re-pinned golden hides a regression: section 10 is the whole list.
- Unrounded grid values depend on numpy's `quantile(method="linear")` interpolation, stable since
  numpy 1.22. Across numpy majors the last bits can differ, so the `numeric_only` golden could
  differ between environments with different numpy majors; the golden is pinned under the locked
  numpy, and a numpy major bump re-checks it.
- The registry sentry fixture change moves another assertion: stop and report rather than edit
  the assertion.
- #456 merges first or after: either way rebase, re-count `snapshot.py`, apply the trims.

## 16. Follow-ups (recorded, not in this row)

- Categorical `top_values` have consent but no count floor; whether they move to the same `K`
  floor is an owner decision for a later row.
- Coarsening the raw `min`/`max`/`bin_edges`/`quantiles` of a plain (non-redacted) snapshot.

Verdict: GO
