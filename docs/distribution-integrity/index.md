# Distribution integrity: developer reference

How the engine measures, copies and protects the statistical shape of data.
This is the map for anyone changing `decoy_engine.quality`,
`decoy_engine.generation.statistical`, or the code that calls them.

For task-oriented usage see the CLI user guide in the `decoy` repo,
`docs/guides/shape-and-privacy/`.

```{toctree}
:maxdepth: 1

snapshot-and-fidelity
statistical-generation
differential-privacy
extending
```

## Scope

Four capabilities share one vocabulary and one artifact family:

| Capability | Entry point | Output |
|---|---|---|
| Summarize a frame | `quality.compute_distribution_snapshot` | `distribution-snapshot/v1` dict |
| Compare source and output | `quality.compute_quality_report`, `quality.apply_quality_policy` | `quality-report/v1`, `quality-policy/v1` |
| Sample new columns from a snapshot | `generation.statistical.load_spec`, `sample_column` | Column values |
| Fit a private snapshot | `quality.dp.fit_dp_snapshot` | `distribution-snapshot/v1` with a `dps-marginal/v3` `dp` block |

Masking strategies are not part of this subsystem. They are measured by it.
Masked output carries no differential privacy.

## Data flow

```text
                 source frame
                      |
        +-------------+----------------+
        |                              |
compute_distribution_snapshot     fit_dp_snapshot
  (exact, value-dependent)        (OpenDP, noisy, certified stack only)
        |                              |
  distribution-snapshot/v1     snapshot + dp block (dps-marginal/v3)
        |                              |
        +-------------+----------------+
                      |
          plan compile: load_spec, check_statistical_columns,
          verify_dp_snapshots (pins bytes, checks budget)
                      |
                sample_column  --->  generated table
                                          |
   source frame + output frame --> compute_quality_report --> quality-report/v1
                                          |
                                 apply_quality_policy --> quality-policy/v1
```

The exact fit and the DP fit are separate code paths. The DP fit never calls
`compute_distribution_snapshot`, because that function looks at values to make
its decisions (column kind, the 30-distinct cutoff, min and max), which a DP
fit must not do.

## Module map

All paths are under `src/decoy_engine/`.

### Measurement (`quality/`)

| Module | Role |
|---|---|
| `snapshot.py` | Exact per-column and per-pair summary. The only step that reads raw values for measurement. |
| `diagnostic.py` | Structural checks between two snapshots: column survival, row count, kind drift, null drift. |
| `fidelity.py` | Value-identity similarity per column and per pair, plus the aggregate. |
| `_distribution_gof.py` | KS complement and chi-square Cramer's V, computed from snapshots. Additive, in `extra_metrics`. |
| `shape_fidelity.py` | Similarity on sorted frequency vectors, ignoring what the values are. |
| `report.py` | Assembles `quality-report/v1`; maps score to grade; collects warnings and omissions. |
| `policy.py` | Turns a report plus thresholds into a pass, warn or fail verdict. |
| `synth_report.py`, `_dcr.py`, `_attacks.py` | Privacy-side metrics for synthetic output: new-row rate, distance to closest record, opt-in attack metrics. |
| `_retention_gate.py` | Fit-time warning when top-K truncation or the free-text cutoff drops too much category mass. |

### Generation (`generation/`)

| Module | Role |
|---|---|
| `statistical/_spec.py` | Validates one statistical column against a snapshot. Owns the `allow_real_categories` gate. |
| `statistical/_sample.py` | Deterministic samplers per kind. |
| `_statistical_column.py` | Dispatch for `type: statistical` inside `synthesize.py`. |
| `_fidelity_gate.py` | After generation, scores statistical columns against their snapshot and logs a warning below `fidelity_warn_threshold`. |

### Differential privacy (`quality/dp*.py`, `quality/carrier*.py`, `plan/_checks_dp*.py`)

| Module | Role |
|---|---|
| `dp.py` | `fit_dp_snapshot`: drives the release session and serializes the artifact. Does not import `opendp`. |
| `dp_budget.py` | The only module that imports `opendp`. Mechanism backend, calibration search, composition. |
| `dp_session.py` | `OpenDpReleaseSession`: allocation, release and composition for one fit. |
| `dp_schedule.py` | Frozen query-schedule dataclasses. |
| `dp_ledger.py` | Sums composed totals across distinct release IDs at plan compile. |
| `dp_provenance.py` | Certified-stack gate for fit and for generation. |
| `dp_fit_schema.py`, `dp_policy.py`, `dp_schema.py` | Column-schema parsing, the normalization policy text, schema constants. |
| `carriers.py`, `carrier_adapter.py` | Typed carriers (`number`, `text`, `flag`) and the pandas boundary. |
| `plan/_checks_dp.py`, `plan/_checks_dp_carrier.py` | Compile-time checks for `global_settings.dp`. |

### Callers

| Caller | What it uses |
|---|---|
| `execution/_pipeline_finalize.py` | `compute_quality_report` per mask table when `run_pipeline(fidelity_report=True)`. |
| `execution/_distribution_behavior.py` | Static per-strategy label of what a strategy does to a distribution. Observational only. |
| `testflight/_distribution.py`, `_distribution_generate.py` | Acceptance checks built on the report and policy. |
| `decoy` CLI | `fit.py`, `report.py` (`report fidelity`), `run.py` (`--fidelity`). |
| `decoy-platform` | `api/quality/runner_hook.py` (best-effort report after a job), `api/jobs/routes_quality.py`. |

## Invariants

These hold across the subsystem. Tests enforce most of them.

1. **Reports are aggregate-only.** A `quality-report/v1` contains scores and
   counts, never a cell value or a category label. Snapshots do contain
   labels and stay inside `compute_quality_report`.
2. **Snapshots are deterministic.** Same frame and arguments give byte-identical
   JSON. Floats are rounded to 12 places. No NaN, no numpy scalars.
3. **Comparison is snapshot-to-snapshot.** `diagnostic`, `fidelity`,
   `_distribution_gof` and `shape_fidelity` take dicts and never see a frame.
4. **Measurement never fails a job.** Reports, the inline fidelity option and
   the generation fidelity gate are report-only or warn-only. Policy returns a
   verdict; the caller decides what to do with it.
5. **Real category values need consent.** A statistical column refuses a plain
   categorical snapshot without `allow_real_categories: true`. The only
   exemption is a snapshot the compiler verified as a DP release.
6. **One OpenDP site.** Only `quality/dp_budget.py` imports `opendp`
   (`tests/unit/quality/test_opendp_dependency.py`).
7. **Decoy computes no privacy quantity itself.** Per-query loss comes from
   OpenDP's privacy map; composition comes from `dp_accounting`.
8. **DP fits fail closed** on an uncertified platform or package set, before
   any private cell is read.

## Schema versions

| Artifact | Version string |
|---|---|
| Snapshot | `distribution-snapshot/v1` |
| DP block inside a snapshot | `dps-marginal/v3` |
| Diagnostic | `quality-diagnostic/v1` |
| Report | `quality-report/v1` |
| Shape fidelity | `quality-shape-fidelity/v1` |
| Policy verdict | `quality-policy/v1` |

## Related documents

- [What Decoy does not prove](../what-we-cannot-prove.md): the binding
  statement of the DP guarantee and its limits.
- [Acceptance test-flight](../acceptance-test-flight.md): how distribution
  fidelity is asserted in the acceptance suite.
- [Determinism](../determinism.md): seeds and keys for generation.
- Plans with the design history: `docs/plans/2026-09-23-distribution-integrity-ks-chisquare.md`,
  `2026-10-22-di1b-gof-omission-propagation.md`,
  `2026-07-22-dps-scope-b-implementation-guide.md`,
  `2026-07-23-dps-codec-implementation-guide.md`.
- Product claims: `docs/backlog/distribution-integrity/honest-capability-statement.md`
  in `decoy-platform`.
