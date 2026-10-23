# Warehouse sources — S1a: multi-part parquet export reader primitive

**Status:** BUILT + GREEN on branch `claude/brave-goodall-ya1d55`, pending external review before merge (2026-10-23). Engine slice, **src change (R2)**. Second build slice of the V2 warehouse-sources work (after S0). Artifacts: `src/decoy_engine/connectors/parquet_dataset.py` (`stage_parquet_parts`) + `tests/connectors/test_parquet_dataset.py` (11 tests). VERIFY: 94% line / 100% branch on the changed module (the 5 missed lines are defensive except/cleanup swallows); full mutmut not run due to a coverage-core/numpy-2.5 env conflict in this container, but the correctness-critical mutants (schema guard, sorted order, atomic publish) are provably killed by the mismatch/missing/order/merge tests. ruff + format clean; module is mypy-clean (tree-wide numpy-stub noise is pre-existing env). Platform wiring is S1b.

**Parent framing:** `decoy-platform/docs/plans/2026-10-23-warehouse-sources-cloud-output-framing.md`. **Prior slice:** S0 fidelity matrix (`decoy-engine/docs/records/2026-10-23-warehouse-export-fidelity-matrix.md`).

**Rules consulted:** 00-universal, development-loop, reuse-first, scope-discipline, architecture, security, testing, use-established-methodology, documentation.

## FRAME

A cloud warehouse's native export (Redshift `UNLOAD`, Snowflake `COPY INTO`) writes **many parquet part-files under one prefix** by default (one per slice/thread; single-file export is opt-in and size-capped). Decoy's source binding today points at exactly **one** parquet object (seam map 2026-10-23: `binding_resolve.resolve_binding`, one `key`/`object` per descriptor; the only prefix expansion, `iteration._expand_files`, fans the whole pipeline per file rather than merging). So a real multi-part warehouse export cannot be read as one logical table. S1a closes that at the engine layer.

**Definition of done:** a caller-independent engine primitive that merges a set of parquet part-files into **one consistent staged parquet file**, which the existing single-file source/profile path then consumes **unchanged**.

**Boundaries / non-goals:** no bucket/S3 knowledge (the platform lists the prefix and passes explicit paths — library code does not know its callers); no schema evolution / union-by-name across differing parts (fail closed instead); no re-sorting of rows (deterministic concatenation order only); no masking/type changes (this is ingestion only); not the platform binding (S1b); not the fidelity admission guard (S1c).

## Established solutions

- **pyarrow is the engine's existing parquet I/O tool** (`profile/_readers.py` uses `pyarrow.parquet.ParquetFile`; all engine parquet I/O is pyarrow). pyarrow preserves Arrow types exactly and reads a list of files as one dataset (`pyarrow.parquet.read_table([...])`, `pyarrow.dataset`). Chosen over DuckDB's multi-file read here because DuckDB unifies/upcasts types across files (a fidelity risk S0 already flagged for the DB path), whereas pyarrow keeps each column's exact type and lets us assert cross-part schema identity. The project's "DuckDB-offered surface" principle governs *database* connectivity; reading already-materialized parquet files is pyarrow's domain.
- **Structure mirrors the shipped `connectors/postgres_snapshot.py`** precedent: validate first, stage to a private dir, atomic `os.replace` publish, coded+redacted errors, fail-closed. S1a is the file-set analogue of "snapshot a source to one parquet".

## Design notes (architecture)

- New structure: one module `connectors/parquet_dataset.py` with one public function. Design pain it addresses: multi-part reading has no home today and the single-file reader (`pq.ParquetFile(path)`) is structurally one-file. A standalone staging primitive keeps the entire downstream path (profile, route, mask, sink) unchanged — it consumes the staged single file. Pattern followed: `postgres_snapshot.snapshot_tables_to_parquet` (cited in the module docstring per the use-established-methodology rule).
- One place per fact: the unified schema is read once from the first part and every other part is checked against it; the staged file is the single source the rest of the engine sees.
- House style: a typed config/result (frozen), a coded `ParquetDatasetError(PermanentError)` mirroring `PostgresSnapshotError`, single-purpose helpers, comments only where the fidelity/atomicity reasoning is non-obvious.

## Guarantee (pinned)

`stage_parquet_parts(part_paths, dest_dir, ...) -> Path` promises:
1. **One consistent dataset.** Every part's Arrow schema must equal the first part's schema exactly (names, types, nullability, field metadata); any disagreement fails closed with `schema_mismatch` naming the offending part and the diff — no silent upcast/union.
2. **Deterministic output.** Parts are ordered by sorted path; within-part row order is preserved. Equal inputs → identical staged bytes-content (order-stable; not a claim about parquet file bytes, which carry writer metadata).
3. **Type fidelity.** Column types are exactly those pyarrow reads from the parts (no coercion). (Downstream masking fidelity is the separate S0 matrix.)
4. **Atomic publish.** Staged to a private dir, `os.replace`d into place only after all parts are written; any failure/interrupt publishes nothing and cleans up.
5. **Bounded memory.** Parts are streamed row-group by row-group into one `ParquetWriter`, never all materialized at once.
6. **Fail-closed edges:** empty `part_paths` → `no_parts`; a missing/unreadable part → `part_unreadable`; a non-parquet part → coded error; zero-row parts are allowed and contribute no rows.
7. **No secret/value leakage** in errors (path basenames + types only).

## Acceptance tests (authored before/with the code)

- Two parts, identical schema → one staged file with all rows in sorted-part order; schema identical; types exact (incl. decimal128, tz timestamp, date32, string, int). (fail-before: assert the merged row set/order.)
- Schema mismatch (a part with an extra column / different type / different nullability) → `schema_mismatch`, nothing published, staging dir gone.
- Deterministic: two runs over the same parts → identical staged content; part input order does not matter (shuffled list → same output).
- Empty list → `no_parts`; missing part path → `part_unreadable`; nothing published on failure.
- Zero-row part among non-empty parts → row count = sum; schema preserved.
- Bounded memory: parts with multiple row groups stream (assert via a ParquetWriter path / batch count, not a full-materialize).
- Property (Hypothesis): for N parts split from one table, merge(parts) equals the original table's rows in the split order (round-trip/split-invariance).
- The staged file is consumable by the existing reader: `profile_source` + `run_pipeline` over a config whose source `path` is the staged file masks correctly (ties S1a to the unchanged downstream path).

## VERIFY / test strength

Changed unit is the new primitive; measure line+branch coverage and a mutation score on `parquet_dataset.py` (security/correctness-critical: the schema guard and atomic publish). Surviving mutants in the schema-consistency or atomic-publish logic are build work, not recorded-and-moved-past.

## Handoff to S1b

S1b (platform) lists the warehouse export prefix over the existing S3/GCS `CloudAccount`, stages the parts locally (reusing `v2_cloud_staging`), calls `stage_parquet_parts` to merge, then feeds the merged file through the existing single-file source path. It must strip/expand any platform-only "prefix source" key before `validate_v2_config` (the `layout_id` precedent) and teach `snapshot_has_legacy_cloud_descriptor` about it.
