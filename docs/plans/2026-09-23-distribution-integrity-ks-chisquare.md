# A2: KS + chi-square methods in the distribution-integrity comparator

Status: plan, BUILD-READY (Codex GO-WITH-CHANGES 2026-09-23; changes incorporated below).
Risk: R2. Author: Opus. Locked: additive and report-only, grade unchanged; hand-rolled from
snapshots (no scipy); new metrics live in named nested fields, not new column entries.
Roadmap: `decoy-platform/docs/ROADMAP.md` "Ready to build" and "NEXT SET OF WORK" item 4.

## FRAME

### Problem

The distribution-integrity Quality report is what a customer means by "masked with
distribution integrity." Its comparator (`quality/fidelity.py`, `compute_fidelity` at
`:96`) scores numeric columns with a normalized quantile-grid RMSE and categorical /
datetime columns with Total Variation Distance. It implements no KS test and no
chi-square; the module docstring cites SDV's KSComplement only as prior art, and the
backend-feature-tracks doc lists KS / chi-square goodness-of-fit as deferred
higher-ceiling follow-ups. So when a customer or auditor asks for a standard
goodness-of-fit statistic, we do not have one to show.

### Definition of done

The comparator emits, per applicable column, a KS-complement score (numeric) and a
chi-square-based score (categorical) alongside the existing methods, computed from the
existing aggregate snapshots, bounded in [0, 1], symmetric, deterministic and byte-stable,
with no raw values read and no heavy new runtime dependency. The shipped grade semantics
are unchanged unless we deliberately decide otherwise (see open questions).

### Boundaries

- Additive and report-only. Do not change the existing quantile-RMSE / TVD numbers, and
  (recommended) do not change the `overall_score` or the A-F grade mapping.
- Compute from the snapshots that `compute_distribution_snapshot` already produces
  (numeric histogram + quantile grid; categorical top-K counts). Do not read raw cell
  values: the fidelity layer is aggregate-only by privacy invariant, and it must stay
  pure dict-in / dict-out.

### Applicable rulebooks

`feature-dev`, `testing`, `verification`, plus the established-methodology rule in engine
`CLAUDE.md` (survey the standard tool, cite it in the module docstring). `reuse-first` and
`config-secrets-and-supply-chain` govern the scipy-vs-hand-roll dependency decision.

### Risk

R2: a statistical method that feeds a customer-facing integrity claim. Report-only and
additive keeps blast radius small, but correctness of the statistic is the whole point, so
it carries the full R2 review and a high mutation bar on the metric functions.

## Approach

Survey first (established-methodology): SDV `QualityReport` uses KSComplement (numeric) and
TVComplement (categorical); `scipy.stats.ks_2samp` and `scipy.stats.chisquare` are the
reference implementations; NIST SP 800-188 sec. 4 frames marginal utility metrics. We
reuse the formulas, computed from our summaries:

1. Numeric KS-complement (binned approximation). We do not hold raw samples, so build an
   empirical CDF for each side from the numeric snapshot, evaluate both on the UNION of the
   two sides' histogram bin edges using one documented mass-allocation rule (uniform within
   a bin), take `D = max |CDF_src - CDF_out|`, and report `ks_complement = 1 - D`, bounded
   [0, 1] and symmetric. The p05-p95 quantiles only help place edges; counts are never
   reallocated to "refine" a bin (that needs the raw data). Constant columns are handled
   explicitly: two identical constants score 1.0. Name the reported metric so the binned
   approximation is unmistakable; it is not a raw two-sample KS (resolution is the snapshot
   bin count, default 10).
2. Categorical chi-square score. Build a COMMON partition across both sides first: union the
   two top-K label sets and fold every unmatched label (including a side's `other_count`
   remainder) into one shared `other` bucket, so a label in one side's top-K but the other's
   `other_count` is never read as a zero count (which would fabricate the contingency table).
   Compute the two-sample (homogeneity) chi-square statistic on that shared partition,
   normalize to Cramer's V (bounded [0, 1]; needs only the statistic, n, and table shape, so
   no chi-square CDF), and report `chi_similarity = 1 - V`. Mark a column with insufficient
   identifiable support `comparable: false`. State plainly that this is coarsened homogeneity
   over the shared partition, with no p-value and no full-support claim. Size the computation
   for the opt-in full-vocabulary snapshot (up to 100k labels), not only a K = 20 vector.
3. Emit both in NAMED NESTED fields on each existing column entry (for example
   `extra_metrics.ks_complement`, `extra_metrics.chi_cramers_v`), each with its own value, a
   `method` tag, and a `comparable` flag. Do NOT add duplicate column entries and do NOT
   replace the primary `similarity` / `method` that drives the marginal score. Pass only
   scores and statistics to the report, never snapshot labels or edges. Keep the
   `_SCORE_PRECISION = 6` rounding pin so the JSON stays byte-stable across BLAS / numpy
   builds.

Recommendation on the dependency: hand-roll both from the snapshot vectors. They are exact,
closed-form computations over small summary arrays; adding scipy pulls a large runtime
dependency for two formulas and reintroduces cross-build float wobble that the precision
pin exists to avoid. Cite scipy / SDV as the source patterns in the docstring per the
established-methodology rule. This is the one dependency decision for the plan-gate.

## Observable behavior

- Identical source and output snapshots: `ks_complement == 1.0` and `chi_similarity == 1.0`.
- A numeric column shifted entirely past its source range: `ks_complement` near 0.
- A categorical column with disjoint value sets: `chi_similarity` near 0.
- Empty or kind-mismatched columns: recorded `comparable: false`, excluded from any
  aggregate, same as the existing methods.
- Existing quantile-RMSE / TVD scores, `overall_score`, and grade are unchanged for every
  fixture (additive-only proof).

## Known failure modes

- Divide-by-zero when an expected categorical count or a numeric range is zero: guard and
  return `comparable: false` with a reason, never a NaN in the JSON.
- Binned-CDF coarseness overstating KS similarity for a within-bin shift: documented
  limitation; the quantile-grid refinement reduces but does not remove it.
- Non-determinism from float order-of-operations: prevented by the precision pin plus a
  fixed reduction order; asserted by a byte-stability test.
- Silent asymmetry (a metric that changes when source and output swap): asserted by a
  symmetry test for both metrics.

## Acceptance tests

- `test_ks_complement_identical_is_one`, `test_chi_similarity_identical_is_one`.
- `test_ks_complement_fully_shifted_near_zero`.
- `test_chi_similarity_disjoint_categories_near_zero`.
- `test_new_methods_bounded_0_1` (property test over randomized snapshots).
- `test_new_methods_symmetric` (swap source/output, scores unchanged).
- `test_scores_are_byte_stable` (repeat run produces identical JSON; precision pin holds).
- `test_identical_constants_score_1_0` and `test_undefined_support_is_comparable_false`
  (a genuinely zero-support side gives no NaN, an honest skip). These replace the earlier
  blanket zero-range case, which was wrong.
- `test_label_in_one_topk_other_othercount_not_zeroed` (the common-partition guard: a label
  in one side's top-K but the other's `other_count` must not fabricate a zero cell).
- `test_grade_and_overall_score_unchanged` (regression on a shipped fixture proving A2 is
  additive and does not move the grade).
- Test strength: mutation on the two metric functions, treated as correctness primitives
  (a surviving mutant there is build work, not a number to record). Line + branch coverage
  on the guards. Bars from a measured baseline, not a chosen number.

## Out of scope

Changing the grade or `overall_score`; strategy-aware weighting; p-value reporting (which
would need the chi-square distribution CDF and a dependency); a raw-sample two-sample KS
(the layer is aggregate-only by privacy invariant); platform surfacing beyond passing the
new fields through the existing report shape.

## Open questions (for the plan-gate and Cam)

1. Additive-only vs folded-in. Recommendation: keep the two new scores as extra reported
   fields and leave `overall_score` and the A-F grade computed from the primary methods, so
   already-run jobs' grade semantics do not shift. Folding them in is a separate,
   grade-moving decision.
2. scipy vs hand-roll. Recommendation: hand-roll (above). Confirm at the gate.
3. Whether to also report the raw KS statistic `D` and chi-square statistic (not only the
   complements) for auditors. Low cost; recommend yes, as extra fields.

## Plan-gate review + revisions (Codex, 2026-09-23)

Verdict: GO-WITH-CHANGES. Resolutions below are folded into the plan.

1. Numeric KS is an approximation; specify one construction. Snapshots hold independently
   chosen histogram edges plus five quantiles and cannot recover a raw two-sample KS.
   Decision: compute the KS statistic as the maximum gap between binned empirical CDFs built
   on the UNION of both sides' bin edges, with a single documented mass-allocation rule;
   quantiles do not "refine" bin counts. Name the product-facing metric so the binned
   approximation is unmistakable. Handle constant columns explicitly: two identical
   constants must score 1.0, which overrides the earlier blanket "zero range is
   incomparable" acceptance case (that case was wrong and is replaced).
2. Categorical chi-square needs a common partition. Each snapshot picks its own top-K, so a
   label in one side's top-K may sit in the other's `other_count`; treating a missing count
   as zero builds a false contingency table. Decision: build shared buckets across both
   sides, fold unmatched labels into a shared `other`, mark insufficient identifiable
   support as `comparable: false`, and state this is coarsened homogeneity with no p-value.
   The full-vocabulary snapshot path can hold up to 100k labels, so the implementation is
   not always a small vector.
3. Keep primary entries intact. Each column has one `similarity` / `method` / `comparable`
   entry that drives the marginal score. Put the new metrics in NAMED NESTED fields with
   their own comparability and reason; do not append duplicate column entries or replace the
   primary method tag. Pass only scores and statistics to the report, never snapshot labels
   or edges.
4. Stronger proofs. Add hand-calculated nontrivial KS and 2xK chi-square cases, unequal
   sample sizes, mismatched top-K sets, constants, malformed/empty bins, a pre-change
   absence test, pinned `now_iso` for byte tests, and an old-grade regression against fixed
   pre-change values.
5. Open questions resolved within scope: keep the grade additive (no Cam call), and both the
   hand-roll choice and raw-statistic inclusion are engineering calls, not Cam-gated. R2
   confirmed.
