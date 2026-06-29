# Acceptance test-flight suite

The acceptance test-flight suite is a set of deliberately-run, high-complexity
jobs that prove engine Phase-5 strategies compose inside real pipeline runs,
that post-run distribution is intact, and that relationship topologies hold
end to end. It is NOT a per-commit hook. A human runs it before merging a
large block of strategy, relationship, or generation work and merges only on
a PASS result.

## What the test-flight is

Each job drives the real `run_pipeline` spine with a realistic multi-table
config, then evaluates a full set of invariants against the output. Three
jobs are included in the initial set:

| Job | Topology | Tables |
|---|---|---|
| `a_healthcare_claims` | one-to-many, 3-level | members, claims, claim_lines |
| `b_retail_m2m` | many-to-many via junction | customers, products, orders |
| `c_hr_selfref` | self-referential FK + generate | employees, synthetic_events |

The suite lives in `testflight/` at the repo root. It is excluded from the
default `pytest tests` collection by `addopts` in `pyproject.toml` and by the
`testpaths` setting, so it never enters the regression-gate or the polars
substrate-matrix workflows.

## How to run it

One-command entry (recommended for pre-merge gate):

    python scripts/test_flight.py

This runs all jobs, prints the evidence report to stdout, writes it to
`testflight/_artifacts/report.md`, and exits non-zero on any failure.

Single-job run:

    python scripts/test_flight.py --job a_healthcare_claims

pytest entry (runs jobs AND the mutation-control teeth):

    pytest testflight -m testflight

The suite requires the `[geo]` extra for `geo_generalize` lat/lng support.
Install it first:

    uv sync --extra dev --extra geo

If the geo extra is absent, the geo invariants hard-fail with a clear message.
They never silently skip: a skipped geo check is false confidence.

## Evidence report

The evidence report produced by `scripts/test_flight.py` names the job,
topology, elapsed time, and outcome for every invariant family. A passing
run looks like:

```
DECOY TEST-FLIGHT  (engine 0.1.0, seed 42, substrate pandas)   RESULT: PASS

== Job a_healthcare_claims  [one_to_many_multilevel]  ... ==
  determinism      : MATCH   hash ...  (2 runs byte-identical)
  tables           : members ... claims ... claim_lines ...
  fk integrity     : OK  ...
  quality grade    : members A  claims B  ...
  distribution     : preserve OK  coarsen OK  correlation OK
  checksums        : ssn luhn .../... valid  npi .../... valid
  safe harbor      : restricted-zip3 planted ... / suppressed ...
  quarantine       : planted ... / quarantined ...
  sentinels        : CLEAN (0 leaks)
  computed columns : OK  case_when branches hit
  coverage         : ... strategies
...
STRATEGY COVERAGE: .../... live scalar strategies exercised
RESULT: PASS
```

Each line carries expected-vs-found integers so a deviation is visible even
when all booleans pass.

## What the test-flight proves

The suite asserts the following invariant families:

**Determinism.** Two identical pipeline calls with the same seed produce
byte-identical output tables and quality-metrics blocks. Catches any
non-deterministic strategy or ordering bug.

**FK integrity.** For every declared relationship, every non-null child key
in the output exists in the parent masked key set (belt-and-suspenders: both
via the built-in `fk_intact`/`no_orphan_children` validators AND via a direct
set-membership assertion). Covers one-to-many multi-level (3 hops), M2M
both-parent resolution, and self-FK closure.

**Distribution fidelity.** For mask tables: `compute_quality_report` is
called directly on source vs masked output (never output vs output), with
declared joint pairs, followed by `apply_quality_policy` with the full
strategy map. Explicit teeth the policy alone does not provide:

- Constant-collapse guard: a preserve-class column (fpe, hash) must retain
  at least 0.99x the source cardinality.
- Real-coarsening guard: a coarsen-class column (bucketize, geo_generalize,
  bucket_perturb) must have strictly fewer distinct values than the source
  AND no value outside the allowed bucket set.
- Correlation-preservation: declared joint pairs must score above a tolerance
  on the pairwise similarity metric.
- Null-rate drift: the diagnostic block must pass; per-column null drift must
  stay within the declared threshold.
- Grade floor: preserve-dominant tables must achieve grade A or B.

For generate tables: output categorical frequencies are compared to declared
weights within TVD tolerance; numeric mean and std are checked against
declared params.

**Correlation through masking (Phase 3c).** For column pairs masked by a
value-changing strategy (fpe, hash, code_set), the engine TVD joint metric
scores 0.0 even when the joint structure is perfectly preserved (value labels
become disjoint). The suite uses a harness-side relabel-invariant metric:
Cramers V computed over contingency COUNTS (not value labels). The assertion
is `abs(V_out - V_src) <= tol`. A faithfully FPE-masked correlated pair
passes; a pair where one column was independently shuffled after masking
fails.

**Checksums.** For every fpe-checksum column (luhn, npi, vin, isbn13, ean13,
gtin), every output value is validated by `decoy_engine.checksums.validate`.

**Safe Harbor suppression.** No restricted ZIP3 prefix survives at zip5
resolution. The count of suppressed rows equals the planted count. The
`geo_generalize_cascade` warning is present.

**Quarantine counts.** `result.quality_metrics["quarantine"].total_quarantined`
equals the planted invalid-row count. Quarantined rows are absent from the
main output. The JSONL quarantine file line count matches.

**No-leakage sentinels.** Unique raw PII strings planted in the source (a
known SSN, email, free-text phone inside notes) must not appear in any output
column of any table. Covers text_mask and text_redact spans.

**Computed-column correctness.** `derived` and `derived_aggregate` outputs
are recomputed in pure Python from the output's input columns and asserted
equal. For `case_when` expressions, every branch must be exercised by at
least one row (branch-coverage check catches a broken branch that never fires).

**chapter_preserve.** For code_set columns with `chapter_preserve: true`, the
ICD chapter bucket of each output code must match the chapter bucket of the
source code.

**joint_mask consistency.** Each output tuple for a joint_mask column must be
a row that exists in the reference table (NDC, MCC, etc.).

**Strategy coverage.** The union of strategies declared across all job
manifests must equal the live `SCALAR_HANDLERS` registry minus an explicit
documented allowlist. A new strategy added without a corresponding job or
allowlist entry fails the suite guard.

## Honest limitations

**This suite does not certify privacy or compliance.** See
[What Decoy does not prove](what-we-cannot-prove.md) for the full honest
boundary.

Limitations specific to the test-flight:

- **Correlation through a non-bijective mask is not covered.** The Phase 3c
  Cramers V metric is valid for bijective strategies (fpe, hash) where every
  source value maps to exactly one output value and the contingency cell
  counts are preserved. For value-merging strategies (bucketize,
  geo_generalize, code_set when the target corpus is smaller than the source),
  the contingency structure changes and the count-preservation argument does
  not hold. Those pairs are checked via the distribution fidelity metric with
  strategy-aware policy bands (coarsen class), not by the Cramers V metric.

- **Pandas only.** The full strategy catalog (fpe, geo_generalize, code_set,
  joint_mask, text_mask, bucket_perturb, etc.) is not implemented in the
  polars adapter, so the suite runs only on the pandas substrate. Per-strategy
  substrate parity is covered separately by `tests/parity/` and the
  `engine-v2-substrate-matrix` workflow.

- **Pairwise blind spot.** Correlations are only checked for declared joint
  pairs. An undeclared pair is never tested. The manifests declare the
  correlations that matter; undeclared pairs are out of scope by design.

- **Orphan remap out-of-charset gap.** When `orphan_policy: remap` is used
  with an FPE column and the orphan key's characters are all outside the FPE
  charset (e.g. uppercase-only against an alphanum charset), FPE has nothing
  to permute and returns the value verbatim. The suite plants an in-charset
  orphan key for the remap assertion (and asserts the output differs from the
  source), so the assertion is valid for in-charset orphans. Out-of-charset
  orphan keys are not tested here; see the limitation documented in
  [What Decoy does not prove](what-we-cannot-prove.md).

- **Hash value-identity floor.** The hash strategy produces a new value that
  is not the source value (low value-identity score is expected). The suite
  asserts cardinality is preserved (>= 0.99x source nunique) but does not
  assert the exact hash output values; those are pinned by the determinism
  check across the two runs, not by a golden snapshot.

- **Metric coarseness.** The quality module uses quantile-RMSE (numeric) and
  TVD (categorical/datetime). A strategy could shift a distribution subtly
  within the policy tolerance. The constant-collapse, coarsening, and
  correlation guards tighten the net, but subtle within-tolerance shape shifts
  remain a human-QA concern by design.

- **Generate-table baseline.** For generated tables (no source frame),
  expected distributions are derived from the declared config
  (weights, params). The baseline is only as strong as the config and carries
  sampling noise at low N. No golden distribution snapshots are committed.

- **Seeded-generator drift.** The fixture generators use a fixed seed
  (numpy + faker, pinned in `uv.lock`). A faker or numpy bump changes fixture
  output. The suite checks a source fingerprint and fails loudly on drift
  rather than silently comparing against a shifted fixture.

## Merge checklist

Before merging a large block of strategy, relationship, or generation work:

1. Run `python scripts/test_flight.py` and confirm all jobs PASS.
2. Read the evidence report: inspect the expected-vs-found integers for every
   invariant family, not only the PASS banner.
3. If any invariant fails, the report names the failing job, table, column,
   family, and strategy. Fix the root cause; do not adjust tolerances without
   a recorded reason in the manifest and a comment in the PR.
4. Commit the updated `testflight/_artifacts/report.md` with the passing run
   as part of the merge PR.

This gate is referenced in ADR-0005 (platform repo:
`docs/architecture/adr/platform/0005-mechanical-enforcement-of-methodology.md`)
under the CI-gate program as the deliberate human-run pre-merge gate for
large engine blocks. ADR-0005 lives in the platform repo because the
CI-gate program spans both repos; the test-flight itself lives entirely in
the engine repo.

## Adding a job

To add a new job:

1. Create `testflight/jobs/<name>/` with a `manifest.yaml` and `fixture.py`.
2. Declare a `topology`, the `tables`, `relationships`, and the full
   `invariants` block (including `strategy_coverage` entries for every new
   strategy the job exercises).
3. Run `python scripts/test_flight.py --job <name>` to verify it passes.
4. Confirm the suite-level strategy coverage guard still passes with the
   updated manifest set.

No edits to the runner, invariant library, or test files are needed for a
new job unless it exercises a genuinely new invariant family.
