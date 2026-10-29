# decoy_engine package map

One line per package or module. The full navigation map with history is
`CODEMAP.md` at the repo root; this file is the quick in-package index and a
sentry (`tests/sentry/test_package_map_paths.py`) asserts every path here exists.

| Path | What lives here |
|---|---|
| `__init__.py` | Public API (`__all__` is the contract) |
| `config/` | `PipelineConfig` and the source/target/table/relationship models |
| `plan/` | `compile_plan` -> frozen `Plan`, seed envelope, plan-time checks |
| `execution/` | `run_pipeline` spine, routes (full-frame oracle, unified slice, chunked dispatcher, sequential and out-of-core FK), strategy handlers, native companion bridge, transactional sink, capacity |
| `progress.py` | Progress contract: `ProgressSink`, value-free events, `use_progress` |
| `generation/` | The generation core (resident and streamed), pinned generation, pools |
| `generators/` | `ColumnGenerator` (formula and cardinality bounds the V2 core reuses) |
| `kernel/` | Backend-neutral Arrow masking kernels (hash, redact, truncate, passthrough) |
| `transforms/` | Transform implementations the strategy handlers reuse (fpe, text_mask, code_set, derived, joint_mask, geo_generalize, group_key, windowed_date) |
| `expressions/` | Closed-grammar parsers (`derived`, `when`) and the `formula` safe-eval |
| `detection/` | PII detectors, patterns, validators, NER, name hints |
| `profiling/` | Column profiler, classification, sentinels, recommendations, ML model pack, eval |
| `verification/` | Post-mask integrity checks |
| `profile/` | Source profiling, readers (CSV, Parquet, fixed-width), fixed-width writer |
| `io/` | Bounded source readers shared by CLI and platform: CSV record framer, Parquet/CSV byte-budget chunk cutter (`ChunkBudget`) |
| `quality/` | Fidelity report, distribution snapshot, DP fit |
| `validators/` | Job-level validators (`validators:` block) |
| `quarantine.py` | Quarantine-row routing (`quarantine:` block) |
| `validation/`, `validation_result.py` | `validate_config` and the validation wire shape |
| `relationships/` | Relationship graph, namespace registry, orphan policy |
| `subset/` | FK-aware subsetting (preflight, plan, run) |
| `providers_v2/`, `providers.py` | Provider registry and identifier adapters |
| `identifiers.py`, `checksums.py`, `codesets/`, `reference_tables/` | Identifier families, check digits, code corpora, reference datasets |
| `disguises/` | Compliance disguise bundles |
| `determinism/`, `keyprovider.py`, `_reference_date.py` | Seed protocol, key derivation, reference-date anchoring |
| `vault.py`, `unmask.py` | Token vault write and unmask |
| `connectors/`, `sdk.py` | Connector SDK and in-tree file connectors (S3, GCS, SFTP, Postgres snapshot) |
| `data_discovery.py` | Read-only SQL over output (DuckDB) |
| `scratch/` | F25 protected scratch (Slice 1): ScratchManager choke-point, typed capabilities, SQLite ResourceLedger under an ownership-authority seam, kernel project-quota provider. Disconnected from production execution until later slices |
| `context.py`, `instrumentation/` | Caller context and `Logger` protocol; opt-in timing collector |
| `errors.py` | Engine exception types |
| `release.py` | `RELEASE_PHASE` / `is_pre_ga` |
| `walks/` | Cross-file drift/inference helpers (platform-consumed, not public API) |
| `internal/` | Private helpers (regex sentry enforces the boundary) |
