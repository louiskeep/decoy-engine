# Engine QA checklist (V1)

Status: draft, written 2026-11-11 from the engine docs and source. Not yet run.

This folder lists what the V1 engine is expected to do, section by section, and the
checks a QA pass should run against each. It is a list of expectations and questions.
It holds no results. Record results in a dated QA report next to the existing ones
(`decoy/docs/records/`), citing the check ids used here.

It is not a roadmap and tracks no work. A check that fails becomes a roadmap row in
`decoy/docs/ROADMAP.md`, as the earlier QA findings did.

## Sections

Eighteen sections, 225 checks.

| # | File | Area | Checks |
|---|---|---|---|
| 01 | [Config and validation](01-config-and-validation.md) | `PipelineConfig`, strict schema, `validate_config` | CFG-01 to CFG-12 |
| 02 | [Sources, targets and file I/O](02-sources-and-file-io.md) | CSV, Parquet, fixed-width, declared columns, sinks | SRC-01 to SRC-12 |
| 03 | [Pre-scan, profiling and detection](03-pre-scan-profiling-detection.md) | Profiles, detectors, disguises, scan redaction | SCAN-01 to SCAN-12 |
| 04 | [Plan compile](04-plan-compile.md) | `compile_plan`, frozen plan, plan checks | PLAN-01 to PLAN-11 |
| 05 | [Keys, determinism and namespaces](05-keys-determinism-namespaces.md) | Seed, mask secret, master key, namespaces | KEY-01 to KEY-13 |
| 06 | [Core mask strategies](06-mask-core-strategies.md) | hash, fpe, faker, categorical, shuffle, redact, truncate, tokenize, passthrough | MASK-01 to MASK-16 |
| 07 | [Date and number strategies](07-mask-dates-and-numbers.md) | date_shift, bucket_perturb, bucketize, top_code, windowed_date | DATE-01 to DATE-12 |
| 08 | [Free-text strategies](08-mask-free-text.md) | text_redact, text_mask, NER | TEXT-01 to TEXT-12 |
| 09 | [Domain strategies](09-mask-domain-strategies.md) | geo_generalize, joint_mask, code_set, composites, group_key, grouped_series | DOM-01 to DOM-14 |
| 10 | [Expressions and conditional masking](10-expressions-and-when.md) | formula, derived, derived_aggregate, `when:` | EXPR-01 to EXPR-12 |
| 11 | [Relationships and subsetting](11-relationships-and-subsetting.md) | FK preservation, orphans, composite and self keys, subset | REL-01 to REL-13 |
| 12 | [Generation](12-generation.md) | Generate types, uniqueness, pooling, references | GEN-01 to GEN-14 |
| 13 | [Fit, fidelity and differential privacy](13-fit-fidelity-dp.md) | Snapshots, statistical columns, quality report, DP fit | FIT-01 to FIT-13 |
| 14 | [Declared casting, validators and quarantine](14-casting-validators-quarantine.md) | Parse and output formats, validators, row errors | CAST-01 to CAST-13 |
| 15 | [Execution routes and the native companion](15-execution-routes-native.md) | Routing, chunking, out-of-core, Rust parity | ROUTE-01 to ROUTE-12 |
| 16 | [Token vault and unmask](16-vault-and-unmask.md) | `vault: true`, `unmask_pipeline` | VAULT-01 to VAULT-11 |
| 17 | [Security boundaries](17-security-boundaries.md) | File access, secret providers, error text, expression sandbox | SEC-01 to SEC-12 |
| 18 | [Evidence, reasons and compatibility](18-evidence-reasons-compat.md) | Reason codes, progress events, run result, public API | EVID-01 to EVID-11 |

## How each section is laid out

- **What it covers**: the capability in a sentence or two.
- **Expected behaviour**: the promises the docs make.
- **Checks**: a table of what to run and what should happen. Each has a stable id.
- **Open points**: places where the docs disagree with each other or with the code, or
  where the expectation could not be confirmed while writing this. Settle these first;
  a check built on an open point may be testing the wrong thing.

## Conventions for a QA pass

- Run each check through the public API (`decoy_engine.__all__`) or the `decoy` CLI.
  A check that only passes through a private module is a finding.
- Set `global_settings.seed` on every config, and a mask secret
  (`mask_secret_ref: env:DECOY_MASK_SECRET`, at least 32 bytes) on every config with a
  keyed strategy. See section 05.
- "Refused" means the run stops before any output is published and the error carries a
  registered reason code. Where this checklist names a code, the code is part of the
  expectation. Where it says only "a registered code", confirm the code and add it here.
- A check table names a reason code as `namespace.code` (`plan.derived_cyclic_reference`).
  `tests/sentry/test_qa_checklist_reasons.py` fails when a named code is not in
  `decoy_engine.reasons`, or when a registered code appears without its namespace.
- A check marked **Exploratory.** has no pass condition because no doc or code sets one.
  Record what it observes. A result that looks wrong becomes an open point, not a failure.
- "Value-free" means the message, log line or evidence entry names tables, columns,
  positions and counts, and never a cell value, a secret or a path the caller did not
  supply.
- Run determinism checks twice in separate processes and compare output bytes.
- Run each route-sensitive check with and without the native companion installed, and
  note which one a result came from (`decoy_engine.native_companion_status()`).

## Recording a result

For each check record: id, engine version, companion present or absent, pass or fail,
and for a failure the command, the config and the observed output with values removed.
A check that cannot be run as written is recorded as blocked with the reason, and the
check text is corrected here.

## Sources this was written from

`docs/strategies.md`, `docs/relationships.md`, `docs/determinism.md`,
`docs/declared-casting.md`, `docs/execution-backends.md`, `docs/reasons.md`,
`docs/capability-matrix.md`, `docs/guides/`, `docs/security/`,
`docs/distribution-integrity/`, the module docstrings under `src/decoy_engine/`, and the
2026-11-08 generation completeness audit in the CLI repo.
