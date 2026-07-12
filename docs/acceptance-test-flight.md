# Acceptance test-flight suite

The acceptance test-flight suite is a set of deliberately-run, high-complexity
jobs that prove engine Phase-5 strategies compose inside real pipeline runs,
that post-run distribution is intact, and that relationship topologies hold
end to end. It is NOT a per-commit hook. A human runs it before merging a
large block of strategy, relationship, or generation work and merges only on
a PASS result.

## What the test-flight is

Each job drives the real `run_pipeline` spine with a realistic multi-table
config, then evaluates a full set of invariants against the output.

| Job | Topology | Tables |
|---|---|---|
| `a_healthcare_claims` | one-to-many, 3-level | members, claims, claim_lines |
| `b_retail_m2m` | many-to-many via junction | customers, products, orders |
| `c_hr_selfref` | self-referential FK + generate | employees, synthetic_events |
| `d_longitudinal_visits` | mixed (FK + standalone generate) | providers, patients, visits |
| `e_hostile_edge_cases` | mixed (FK + standalone single-row + empty generate) | people, accounts, singleton, empty_table |

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

Full-suite runs (not `--job`) also check each job's output against a
committed cross-process determinism fingerprint (see below); a drift there
fails the run just like an invariant failure. Record a new golden
deliberately after an intentional fixture/engine change:

    python scripts/test_flight.py --update-fingerprints

## Cross-process determinism fingerprint (TH-2.2 / P1-5)

`check_determinism` runs both pipeline calls in the SAME process
(`_runner.run_pipeline_twice`), so it cannot see a hash-seed- or
dict/set-iteration-order-dependent nondeterminism bug: both calls share one
`PYTHONHASHSEED` and therefore agree with each other even if a *different*
process would produce different output. That class of bug previously only
resurfaced by accident, noticed as a diff between separate CI runs.

`testflight/_fingerprint.py` closes the gap mechanically: every full-suite
run computes a SHA-256 fingerprint of each job's output tables (arrow schema
+ data) and compares it to a committed golden value in
`testflight/golden_fingerprints.json`. A mismatch means the identical seeded
config produced different output in a different process -- exactly the
cross-process nondeterminism class the in-process double-run cannot see. The
golden file is committed and updated only deliberately
(`--update-fingerprints`), never silently rewritten by a normal run; a
first-time-missing golden file is reported as a bootstrap NOTE, not a
failure. A single-job run (`--job`) skips this check for the same reason it
skips the strategy-coverage guard: a partial run cannot speak for the whole
golden set.

## Evidence report

The evidence report produced by `scripts/test_flight.py` names the job,
topology, elapsed time, and outcome for every invariant family. A passing
run looks like:

```
DECOY TEST-FLIGHT  (engine 0.1.0, seed 42, substrate pandas)   RESULT: PASS

== Job a_healthcare_claims  [one_to_many_multilevel]  ... ==
  determinism      : MATCH   hash ...  (2 runs equal)
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
output tables and quality-metrics blocks that compare EQUAL as Python values
(`pa.Table.to_pydict()` dict equality plus arrow schema equality per table,
plus full `quality_metrics` dict equality minus known wall-clock timing keys)
-- this is value-equality, **not byte-identical** output (the former "byte-
identical" wording here was inaccurate; corrected TH-2.2). The two calls run
in the SAME process, so this in-process check cannot see a hash-seed- or
set-iteration-dependent ordering bug that only differs ACROSS processes; a
separate, always-on cross-process fingerprint check closes that gap (see
"Cross-process determinism fingerprint" below).

**FK integrity.** For every declared relationship, every non-null child key
in the output exists in the parent masked key set (belt-and-suspenders: both
via the built-in `fk_intact`/`no_orphan_children` validators AND via a direct
set-membership assertion). Covers one-to-many multi-level (3 hops), M2M
both-parent resolution, and self-FK closure.

**Distribution fidelity.** For mask tables: `compute_quality_report` is
called directly on source vs masked output (never output vs output), with
declared joint pairs, followed by `apply_quality_policy` with the full
strategy map. Explicit teeth the policy alone does not provide:

- Constant-collapse guard: a preserve-class column must retain its source
  cardinality (fpe exact 1.0x; hash >= 0.99x).
- Real-coarsening guard: a coarsen-class column (bucketize, geo_generalize,
  bucket_perturb) must have strictly fewer distinct values than the source
  AND no value outside the allowed bucket set.
- Correlation-preservation: declared joint pairs must score above a tolerance
  on the pairwise similarity metric.
- Null-rate drift: the diagnostic block must pass; per-column null drift must
  stay within the declared threshold.
- Grade floor: preserve-dominant tables must achieve grade A or B.

For generate tables: row count is asserted to equal the declared `TableSpec.row_count`
(TH-4.3); output categorical frequencies are compared to declared
weights within TVD tolerance; numeric mean and std are checked against
declared params.

**Correlation through masking (Phase 3c / TH-3.4).** For column pairs masked
by a value-changing strategy (fpe, hash, code_set), the engine TVD joint
metric scores 0.0 even when the joint structure is perfectly preserved (value
labels become disjoint). The suite uses a harness-side relabel-invariant
metric: Cramers V computed over contingency COUNTS (not value labels). The
assertion is `abs(V_out - V_src) <= tol`. A faithfully-masked correlated pair
passes; a pair where one column was independently shuffled after masking
fails. Declared across three independent strategy families so the tooth is
proven to bite generally, not only for one mask type: an fpe-fpe pair (Job B:
cat_code/risk_flag), a hash-hash pair (Job C: dept_hash/division_hash), and a
code_set-chapter pair (Job A: diagnosis/diagnosis_secondary, both
chapter_preserve:true).

**Checksums.** For every fpe-checksum column, every output value is validated
independently of the engine (TH-2.4 / P1-4): `luhn` is validated by calling
`stdnum.luhn.is_valid` directly, and `npi` by a from-spec reimplementation of
the CMS NPPES check-digit procedure built on the same `stdnum.luhn` primitive
-- **not** `decoy_engine.checksums.validate`. Before this change the harness
asked the engine's own validator whether the engine's own masked output was
valid, so a bug that broke both check-digit computation and validation the
same way would agree with itself and ship green. `iban`/`vin`/`isbn13`/
`ean13`/`gtin` have no independent harness implementation yet and are not
currently declared by any job (tracked in the checksum-scheme allowlist,
`testflight/_coverage.py`); calling `check_checksums` with one of those
schemes raises `NotImplementedError` rather than silently trusting the
engine's own validator.

**Safe Harbor suppression.** No output value **starts with** a restricted
ZIP3 prefix at any length >= 3 (TH-2.3 / P1-6) -- a full ZIP5, a bare zip3, or
a zip+4 shape ("03601-1234") alike. The prior check only scanned values of
exactly length 5; with this suite's row counts, non-restricted rows resolve
at the geo_generalize zip3 cascade level (a 3-character output), so a
restricted-skip regression could have leaked a 3-character or zip+4-shaped
prefix invisibly under the old check. The count of suppressed rows is
computed independently from the actual output data (counting the literal `""`
suppression marker in the column, not the engine's self-reported
`cascade_decisions` detail) and cross-checked against both the planted count
and the engine's `geo_generalize_cascade` warning, which must also be
present.

**Quarantine counts.** `result.quality_metrics["quarantine"].total_quarantined`
equals the planted invalid-row count. Quarantined rows are absent from the
main output. The JSONL quarantine file line count matches.

**No-leakage sentinels.** Unique raw PII strings planted in the source (a
known SSN, email, free-text phone inside notes) must not appear in any output
column of any table. Covers text_mask and text_redact spans.

**Computed-column correctness.** `derived` and `derived_aggregate` outputs are
recomputed independently of the engine from the output's input columns and
asserted equal. Aggregate formulas (`sum`/`count`/`avg`/`min`/`max`) are
computed with Python built-ins. Row-wise formulas (arithmetic, comparison,
`case_when`) are recomputed by a harness evaluator built on Python's own `ast`
module, sharing no code with the engine's expression evaluator
(`decoy_engine.expressions._lark_parser.evaluate`). This closes a former
circular blind spot (TH-1.3 / P0-3): until this change the harness recomputed
row-wise formulas with the *same* evaluator the engine's `derived` strategy
used, so a bug in that shared evaluator produced the same wrong value on both
sides and the invariant passed vacuously. Only the aggregate path was
independent before; row-wise is now independent too. The engine's `compile_expr`
is retained solely as a secondary smoke check that the manifest formula is valid
engine grammar. For `case_when` expressions, every branch must be exercised by
at least one row (branch-coverage check catches a broken branch that never
fires).

**Value-changing passthrough.** Every mask column whose strategy is
value-changing (`fpe`, `hash`, `code_set`) must genuinely change its data,
checked for *every* such column in the pipeline config (not only the columns
named in the distribution spec). A complete no-op (output value-set equals
source value-set) fails the value-set check. An FPE charset that covers only
some of the data's characters -- leaving the out-of-charset ones verbatim while
permuting the rest -- fails a per-position across-row retention check (TH-1.1 /
P0-1): for each character position, the fraction of rows whose output character
equals the source character is measured independently, and any *informative*
position (source alphabet >= 4 distinct characters, so low-entropy structural
positions such as an NPI leading `1`/`2` digit are excluded) retained verbatim
in >= 50% of rows is a leak. Testing each position independently catches a
*narrow* leak -- even a single informative position emitted verbatim among many
correctly-permuted ones, at any value width -- that an averaged whole-value
statistic would dilute below the floor. On an FPE column with too few
comparable rows to evaluate positionally the check reports an explicit SKIP (not
a silent pass). One job additionally declares the engine's `leak_check`
validator over its FPE-masked identity columns as a structural "forgot to mask"
net.

**chapter_preserve.** For code_set columns with `chapter_preserve: true`, the
ICD chapter bucket of each output code must match the chapter bucket of the
source code.

**joint_mask consistency.** Each output tuple for a joint_mask column must be
a row that exists in the reference table (NDC, MCC, etc.).

**Strategy coverage.** The union of strategies declared across all job
manifests must equal the live `SCALAR_HANDLERS` registry minus an explicit
documented allowlist. A new strategy added without a corresponding job or
allowlist entry fails the suite guard. Every coverage axis is derived from a
live source, not a static snapshot: strategies from a live SCAN of each job's
`config["tables"][*]["columns"][*]["strategy"]` (TH-3.1 / P1-7 -- the same
config dict `run_pipeline` executes, not the hand-maintained
`invariants.strategy_coverage` manifest list, which is demoted to an
assertion TARGET checked against that scan), validators from
`validators._registry._REGISTRY`, checksum schemes from
`checksums._VALIDATORS`, and generate types from `config._tables.GENERATE_TYPES`
(derived from the `GenerateColumnConfig.type` Literal via `get_args`, the
single validation authority for generate types). Adding an entry to any of
these registries without a job or allowlist entry fails the guard, and the
guard summary reports the true live count for each axis. Deleting a
strategy's only column from a job's config while leaving the name in that
job's `strategy_coverage` list also fails the guard (a per-job assertion, not
only the suite-wide union).

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

1. Run `python scripts/test_flight.py` and confirm the process **EXITS 0**
   (`echo $?`), not merely that it prints the `N/N` invariant line. The
   process exit code is the gate: it goes non-zero on a failed invariant AND
   on cross-process fingerprint drift, and the `FINGERPRINTS: x/x match golden`
   line must be present. A green `41/41` line with a non-zero exit is a FAIL
   (that exact split is how TH-3.2's determinism bug hid). The same
   cross-process check is also asserted by `pytest testflight -m testflight`
   (`test_fingerprint_gate.py`), so a determinism regression fails CI too.
2. Read the evidence report: inspect the expected-vs-found integers for every
   invariant family, not only the PASS banner.
3. If any invariant fails, the report names the failing job, table, column,
   family, and strategy. Fix the root cause; do not adjust tolerances without
   a recorded reason in the manifest and a comment in the PR.
4. The run writes `testflight/_artifacts/report.md` (gitignored, not committed).
   Read it for the evidence; when recording a pre-merge flight, attach it from
   the CI run's `testflight-report` artifact.
5. If a change intentionally alters job output (a fixture change, a new
   strategy default, ...), re-record the cross-process determinism
   fingerprint deliberately: `python scripts/test_flight.py
   --update-fingerprints`, then commit the updated
   `testflight/golden_fingerprints.json` alongside the change. An
   unexplained fingerprint drift is a nondeterminism bug, not a stale golden
   -- investigate before updating it.

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
5. Run a full-suite `python scripts/test_flight.py --update-fingerprints` and
   commit the updated `testflight/golden_fingerprints.json` -- a new job has
   no golden entry yet, and the next full-suite run fails loudly on the
   missing entry rather than silently skipping it.

No edits to the runner, invariant library, or test files are needed for a
new job unless it exercises a genuinely new invariant family.
