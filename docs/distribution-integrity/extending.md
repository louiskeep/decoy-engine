# Extending and open work

Status: current

[Back to the developer reference](index.md)

Where to make common changes, the rules that constrain them, and the defects
and gaps known today.

## Rules that apply to every change here

- **Use established methodology.** Survey the standard tool first and cite it
  in the module docstring. Add non-trivial primitives to
  `docs/methodology-registry.yaml`.
- **Reports stay aggregate-only.** Nothing that reaches a report may carry a
  cell value or a category label. `tests/privacy/` checks this.
- **Additive by default.** New metrics go in named nested fields
  (`extra_metrics.<name>`). Changing `similarity`, `overall_score` or the grade
  mapping is a re-baseline of the platform's honest-capability statement and
  its pinned test (`tests/test_di0_quality_contract.py` in `decoy-platform`).
- **Measurement does not gate.** Keep new checks report-only or warn-only.
  Gating is a platform or caller decision.
- **No Decoy-authored privacy math.** Any DP quantity comes from OpenDP or
  `dp_accounting`.
- **Module size.** 600 lines target, 700 hard cap
  (`tests/sentry/test_module_size.py`). The DP modules were split along this
  line already; follow the same pattern.
- **Golden stability.** Snapshot and report output is byte-stable. A change
  that moves a golden digest needs to say so.

## Recipes

### Add a fidelity metric

1. Implement a pure function over snapshot `stats` dicts in a named
   subpackage under `quality/` (for example `quality/metrics/`), not another flat
   module. No raw frames.
2. Wire it in `fidelity.py` under `extra_metrics.<name>` with its own
   `value`, `method`, `comparable` and, when incomparable, `reason`.
3. If it can be unavailable while the primary score is comparable, extend the
   warning collection in `report.py` the way `gof_unavailable` does.
4. Tests: bounded in [0, 1], symmetric, deterministic, unchanged existing
   goldens, plus one new golden.

### Capture a new statistic at fit time

1. Add it to the relevant `_*_stats` function in `snapshot.py`.
2. Decide whether it changes existing snapshot bytes. A new key does. Samplers
   and comparators must tolerate its absence, because old snapshots exist.
3. Seeds follow the snapshot's content digest, so re-fitting with a new field
   changes generated values for exact snapshots. Call that out.
4. A new statistic is exact. It does not enter the DP artifact unless a DP
   mechanism is added for it.

### Add a sampler kind or option

1. Validate the config in `_spec.py` with a new reason code. Keep `_sample.py`
   pure draw logic.
2. Preserve the per-row seeding contract (`col_seed + start + i`) so chunked
   and windowed generation stay byte-identical.
3. If the option is incompatible with DP, reject it in
   `plan/_checks_dp.py::check_dp_generate_contract`.
4. If the plan pins the spec, extend `spec_to_dict` / `spec_from_dict` so old
   serialized plans still load.

### Add a masking strategy

1. Give it a value in `execution/_distribution_behavior.py::_STATIC_BEHAVIOR`.
2. Add default expectations in `policy.py` for both value-identity and shape
   scoring, set to what the comparators actually report for it.
3. Add a test-flight distribution case if it is a preserve or coarsen class.

### Add a DP mechanism or carrier

1. Build and invoke it only in `quality/dp_budget.py`.
2. Add its query spec to `dp_schedule.py` and make the schedule's query count
   reproducible from the declared schema, since the plan check recomputes it.
3. Extend the carrier codec in `carriers.py` if a new carrier is needed. The
   conversion must stay total and stability-1.
4. Bump the `dp` schema version if the artifact shape changes, and keep
   verification of prior versions.
5. Update [What Decoy does not prove](../what-we-cannot-prove.md) in the same
   change.
6. Run the `dp_certified` suites on the certified profile.

### Change a dependency

Check whether it moves the certified profile. If it does, follow
[Re-pinning](differential-privacy.md#re-pinning) in the same change.

## Open defects

Measured in the ten-question QA pass, `docs/records/2026-11-10-cli-qa-generation-distribution-models.md`
in the `decoy` repo, and re-tested on engine `5c24a75e`. IDs below are that
record's. It holds the evidence, the repro steps, the re-test numbers and a
mapping to roadmap rows. Locations are file and function names on current engine main.

### Fixed since the QA pass

| ID | Fix | Re-test |
|---|---|---|
| C1 | Engine #421: the column seed includes table and column name. | No repeated rows for identical statistical or `pyint` columns; independent columns at -0.01. |
| C3 (nulls) | Engine #438: `similarity` is value score times `missing_value_similarity`. | A 90% null column generated with no nulls now scores 0.096. |
| H2 (engine) | Engine #434 and #436: `cell_format` records the null rate and the sampler applies it. | 10% and 90% nulls came out 9.7% and 90.2%. `decoy fit` does not write the block yet (roadmap row 24l). |
| H3 (decimals) | Engine #436: rounded to the fitted decimal places. | Prices have 2 decimals; the shape defect stays open below. |
| M3 (time part) | Engine #436: floored to the fitted resolution. | Date-only source gives dates; month shape stays open below. |
| H6 (flagged columns) | Engine #420: `distribution_snapshot_document` withholds values of profiler-flagged columns. | Flagged `diagnosis` and `salary` withheld. `decoy fit` does not call it yet (roadmap row 24f). |

### Wrong data with no warning

| ID | Defect | Where | Notes for a fix |
|---|---|---|---|
| C2 | One outlier ruins a numeric column (mean 150 to 50,084 on re-test). | `quality/snapshot.py` (`numeric_stats`); `generation/statistical/_sample.py` (`_numeric_row`) | Equal-width bins over min to max. Options: quantile-based bins, winsorized range plus explicit tail mass, or sampling from the stored quantile grid. Changes snapshot bytes. Roadmap row 24u. |
| H3 | Skew and discrete numerics distorted (median 20.5 to 92.1; 4 prices to 3,585 values with a `cell_format` block, 20,000 without). | Same as C2 | A low-distinct numeric could keep exact values like a categorical. Needs the same disclosure gate. Roadmap row 24u. |
| H1 | `condition_on` falls back silently when a parent's cells were truncated at 25 (0.72% invented pairs on re-test). | `quality/snapshot.py` (`_joint_snapshot`); `generation/statistical/_sample.py` (`_conditional_tables`, `sample_column`) | Expose the cap; warn at fit when `cell_count` exceeds it and at compile when a parent has no cells. Roadmap row 24w. |
| M2 | Unseen parent value falls back silently. | `_sample.py` (`sample_column`) | Same warning path as H1. |
| M3 | Dates keep only the year mix (66.5% January to 8.4% on re-test). | `quality/snapshot.py` (`_datetime_stats`); `_sample.py` (`sample_column`) | Month and weekday bins. |

### Measurement blind spots

| ID | Defect | Where | Notes for a fix |
|---|---|---|---|
| C3 | Numeric similarity divides by the source range, so a wide range hides any error. The C2 file graded B; the outlier column scored 0.940 (unchanged on re-test) and its KS extra 0.997. | `quality/fidelity.py` (`_numeric_similarity`; formula in the module docstring) | Normalize by an interquantile range, or compare on a rank scale. Shape-only scoring caught it (0.102). Changing the score is a re-baseline of the platform contract. Roadmap row 24v. |
| H5 | The generation warn-gate default (0.8) did not fire on the C2 file (0.979; 0.861 on re-test with null scoring). | `generation/_fidelity_gate.py` (`DEFAULT_FIDELITY_WARN_THRESHOLD`); `config/_global_settings.py` (`fidelity_warn_threshold`) | Inherits C3. Logger-only, and since #426 and #429 nothing is written unless the host configures logging; the CLI does not surface it. Roadmap row 24o. |
| M5 | The in-run report and the CLI report infer kinds differently (C versus A on one run). | `execution/_pipeline_finalize.py` (`compute_fidelity_reports`); CLI `report.py` | One inference path, or record the kinds used in the report. |
| F-DI0-1 | Chi-square unavailable on disjoint or constant categories. | `quality/_distribution_gof.py` | Primary `tvd` is correct for both. |
| n/a | Unknown policy keys pass silently. | `quality/policy.py` (`apply_quality_policy`) | A mistyped key is ignored and an unknown mode becomes `report`. A typo disables a gate. |

### Disclosure

| ID | Defect | Where | Notes for a fix |
|---|---|---|---|
| H6 | A label held by one row is stored with `count: 1` and can be generated. Exact `min` and `max` are stored for numerics with no consent gate. #420 covers profiler-flagged columns only. | `quality/snapshot.py` (`_categorical_stats`, `numeric_stats`); the gate in `generation/statistical/_spec.py` (`load_spec`) covers categories only | Roadmap row 24f. A minimum-count floor and rounded bounds are the usual controls. |
| H7 | Free text with 30 or fewer distinct values is classed categorical, stored and emitted verbatim. Not flagged by the profiler, so #420 keeps it. | `quality/snapshot.py` (`_stats_for`, `_CATEGORICAL_DISTINCT_CAP`) | Roadmap row 24f. |

### Differential privacy

| ID | Defect | Where | Notes for a fix |
|---|---|---|---|
| H8 | The allocation search costs 11 s, 36 s and 85 s for 2, 8 and 20 columns and is cached in memory only, so every CLI call pays it. | `quality/dp_session.py` (`_allocate_epsilon_cached`); `quality/dp_budget.py` (`_search_largest`) | The result is a pure function of the public schedule, the request and library versions, so it could be persisted. Only the scalar may be cached, never a measurement or certificate. |
| M7 | A release with no labels validates and then fails at run (`statistical_stats_degenerate`). | `generation/statistical/_sample.py` (`sample_column`) | Check at compile. Same class as roadmap row 24b. |
| M8 | The noisy `row_count` can be 0 or far off on small data. | `quality/dp.py` (`_fit_dp_snapshot_with_backend`) | By design; document, and consider a compile warning. |
| M9 | A forged or hand-edited DP snapshot is accepted. | `plan/_checks_dp.py` (`verify_dp_snapshots`) | Documented limit in [What Decoy does not prove](../what-we-cannot-prove.md). Needs artifact authentication. |

### CLI surface

| ID | Defect | Where |
|---|---|---|
| H4 | Report warnings (`null_drift`, `kind_drift`) are in the JSON only. | CLI `report.py` |
| M1 | A renamed parent cannot be used with `condition_on`; the error suggests a `--joint` that would not help. | `generation/statistical/_spec.py` (`load_spec`); `plan/_checks.py` (`check_statistical_columns`) |
| M4 | Zero-padded codes are fitted as numbers. | CLI `fit.py` |
| M6 | `--parse-dates` on an unparseable column falls back to text with no message. | CLI `fit.py` |
| n/a | Plain `fit` prints no disclosure warning. | CLI `fit.py` |

## Gaps by program stage

The platform program fixes the order: measure, persist, improve, gate
(`docs/plans/2026-10-16-distribution-integrity-program.md` in
`decoy-platform`).

| Stage | State |
|---|---|
| Measure | Built for masking. Generation fidelity is measured against a reference but is internal-only on the platform. |
| Persist | One report row per job on the platform. No distinct skipped, failed or partial state. No fingerprints or engine version recorded by the hook. |
| Improve | Not started. Strategy changes driven by measured drift. |
| Gate | Not built. Policy returns a verdict; nothing enforces it on a job. |

Not built at all:

- **One-sample conformance.** Testing generated output against a declared
  spec, as opposed to a source. Only two-sample comparison exists.
- **Joint differential privacy.** Correlations and conditionals under DP
  (PrivBayes, MST or AIM style). The library survey names SmartNoise-Synth's
  MST and AIM as the candidate.
- **Certified stacks beyond one platform.** macOS, Windows, ARM, musl.
- **Binary-level provenance** and an authenticated artifact (schema v4 on the
  roadmap).
- **Finer date and text models.** Month or weekday bins; anything beyond length
  for free text.

The single cross-repo roadmap is `docs/ROADMAP.md` in the `decoy` repo. Do not
start a separate one here.
