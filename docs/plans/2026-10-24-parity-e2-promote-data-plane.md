# Parity E2: promote platform data-plane modules into the engine

Status: plan, PRE-RELEASE (2026-10-24). Not build-ready until plan-gate review. Part of the parity program
(`decoy-platform/docs/plans/2026-10-24-cli-platform-parity-program.md`); source finding: parity audit §5
("Data semantics that live platform-side") and §7. Platform adoption half: `decoy-platform/docs/plans/
2026-10-24-parity-p2-adopt-engine-data-plane.md`.

Rules consulted: 00-universal, development-loop, risk-and-exceptions, architecture, refactoring,
reuse-first, api-and-compatibility, performance, testing, documentation; engine `CLAUDE.md` (library code
does not know its callers; use established methodology; 600-LOC cap); platform best-practices §3.3.

Risk: R2 (broad cross-repo refactor of execution-adjacent code; behaviour preservation must be proven by
byte parity, not assumed).

## 1. Guarantee and non-goals

Guarantee: after E2, how bytes are read into chunks, how CSV/Parquet/fixed-width are read and written,
how a job's memory need is estimated, and how FK-route outputs are finalised (validators + quarantine)
each live in exactly one place, `decoy_engine`, behind public entry points that both the CLI and the
platform call. Platform behaviour is byte-identical before and after adoption (P2), proven by the
existing platform parity and prodsim fixtures.

Non-goals: credential storage (stays platform; the engine keeps `credentials_ref` opaque), job queue /
ledger / target locks (orchestration; see C4 and the program index), the cloud *upload* writer's spool
and multipart policy (platform-owned until a second consumer needs it), changing any estimate's numbers
(the calibrated multipliers move as-is; recalibration is A-2's job).

## 2. What moves, from where, to where

| Platform module (LOC) | Owned semantics | Engine home (new or extended) | Public entry |
|---|---|---|---|
| `api/jobs/native_reader.py` (212), `csv_record_framer.py` (221) | byte-budgeted chunk cutting, dictionary ownership, CSV record framing, row-size enforcement, pricing check | `decoy_engine/io/bounded_reader.py` | `iter_parquet_chunks(path, budget, *, table)`, `iter_csv_chunks(...)`, `iter_csv_records(...)`, `TableBudget` |
| `api/jobs/v2_cloud_staging.py::_read_sources_as_arrow` + the CSV `dtype=str`/NA rules (563 total, ~100 move) | resident read semantics per declared format | `decoy_engine/io/read.py` | `read_source_table(descriptor, base_dir, *, open=...)` returning `pa.Table`; cloud bytes arrive through an injected opener so the engine never holds credentials |
| `api/jobs/fixed_width_output.py` (233) | `effective_target_format`, `require_layout`, lossless Arrow->pandas, integer coercion contract, `write_fixed_width_table` | `decoy_engine/profile/_fixed_width_writer.py` (extend; `write_fixed_width` exists) | `write_fixed_width_table(table, layout, path)`, `effective_target_format(descriptor)` |
| `api/jobs/admission.py` (1358), `admission_fk.py` (674) | memory model: CSV multiplier, Parquet footer pricing, per-phase reservations, FK discount | `decoy_engine/execution/capacity.py` (extend `estimate_job_capacity`, today out-of-core-FK only) | `estimate_job_capacity(config, base_dir, *, budget_bytes)` returns ingestion peak, route estimate, FK discount; `CapacityEstimate` gains the fields the platform persists |
| `api/jobs/native_budget.py` (597) thread/chunk/decoder pricing | native thread grant rule (knee, cores), per-table chunk budget, decoder envelope | `decoy_engine/execution/native/_budget.py` | `price_native_budget(config, source_facts, *, cores, max_threads)`; persistence of the result stays platform |
| `api/jobs/v2_sequential.py:255-285` validators + quarantine over staged outputs, class-A policy in `v2_node_runs.py` | FK-route finalisation | `decoy_engine/execution/_pipeline_finalize.py` (already does this for `run_pipeline`) | `finalize_outputs(outputs, config, *, sources)` called by both the in-process spine and the explicit FK route callers |

Stays in the platform: `CloudAccount` resolution, `_resolve_credentials_ref`, pinned-download identity,
spool directories, `ChunkedTargetWriter` upload policy, `Job.native_budget` persistence, the memory
`Ledger`, target locks, heartbeats.

## 3. Established solutions

- Architecture already chosen and documented: "library core + thin wrappers" as dbt, Prefect, Dagster
  (`decoy-platform/docs/architecture/shared-engine-architecture.md`). This plan restores it where it
  eroded.
- Refactoring steps are named Fowler moves: Move Function / Move Module into the library, Extract
  Function for the credential opener seam, Parameterize Function where platform constants become kwargs.
  Behaviour-preservation evidence is byte parity plus the platform's existing route tests.
- Bounded Arrow readers follow pyarrow's own `ParquetFile.iter_batches` and `csv.open_csv` streaming
  APIs; the framer stays because pyarrow's CSV reader cannot enforce a per-record byte cap.

## 4. Slices (each independently gateable; platform adopts in P2 after each)

1. **E2a bounded reader.** Move reader + framer; keep names; add `tests/unit/io/` by porting the platform
   tests verbatim. Acceptance: chunk boundaries identical for the frozen prodsim fixtures (hash of chunk
   row-count sequence).
2. **E2b read/write adapters.** `read_source_table` with an injected opener; fixed-width output writer
   consolidation (one `write_fixed_width*` family). Acceptance: platform `_read_sources_as_arrow` becomes a
   five-line call; byte parity on CSV NA/dtype fixtures; fixed-width round-trip fixture.
3. **E2c capacity model.** Fold `admission.py` estimators into `capacity.py` as additional estimators
   behind `estimate_job_capacity`; keep multipliers as named constants with their provenance comments.
   Acceptance: platform `estimate_admission` output equal to the engine call on the admission test corpus.
4. **E2d native budget pricing.** Move the pure pricing functions; platform keeps persistence and the
   grant check. Acceptance: `Job.native_budget` encode equal on fixtures.
5. **E2e FK-route finalisation.** Expose `finalize_outputs`; make `run_sequential`/`run_fk_out_of_core`
   callers use it. Acceptance: validator findings and quarantine manifests identical on the FK fixtures;
   no platform-side `validators._registry` import remains.
6. **E2f docs.** `docs/execution-backends.md`, API reference, CODEMAP rows; CHANGELOG; this plan archived.

Module-size rule: every new module under 600 LOC; `capacity.py` splits into `_capacity_ingest.py` /
`_capacity_fk.py` if needed, with the census entry if over 600.

## 5. Failure modes

Hidden platform-only constants (worker concurrency, cores) leaking into the library: make them kwargs
with no platform default. Credential leakage through the opener seam: the engine receives an already-open
binary stream or a callable returning one and never a dict of credentials (S5 safe-pattern 3). Divergent
behaviour during the transition: P2 adopts slice by slice behind the platform's existing settings flags,
with the old module deleted (pre-GA hard delete) once parity is shown, never kept as a fallback.

## 6. Done when

All six slices merged with byte-parity evidence; platform P2 adoption merged with the moved modules
deleted from `api/jobs/`; CLI plans C2 and C3 build on the public entries; `shared-engine-architecture.md`
no longer needs the "platform reads sources ahead of the adapter call" caveat.
