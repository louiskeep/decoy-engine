# Decoy Engine Codemap

## One-Line Project Summary

Shared Python data engine for Decoy masking, generation, plan-compile execution, connectors, STORM profiling, and the Disguise registry.

## Tech Stack

| Area | Stack |
|---|---|
| Runtime | Python 3.10 |
| Data | pandas, Polars, PyArrow |
| Config | YAML, Pydantic |
| Tests | pytest |
| Consumers | `decoy` CLI and the commercial platform API |

## Entry Points

| Path | Purpose |
|---|---|
| `src/decoy_engine/__init__.py` | Public API exports |
| `src/decoy_engine/config/_pipeline.py` | `PipelineConfig` (the V2 validation choke-point) |
| `src/decoy_engine/plan/_compile.py` | `compile_plan` -> frozen `Plan` |
| `src/decoy_engine/execution/_pipeline.py` | `run_pipeline` (unified mask+generate spine; routing lives in `_pipeline_routing.py`: S2 default routes relationship-bearing pure-mask jobs to `run_sequential` for bounded memory via the `execution_mode` kwarg, fallback to full-frame for non-FK/mixed/validator jobs or an explicit non-pandas `substrate`; `auto_chunk` (default `True`) then routes eligible large single-table non-relationship mask jobs through chunked execution, a memory-only win with byte-identical output; opt-in `fidelity_report` attaches a label-free `quality-report/v1` per mask table; `substrate`/`fpe_chunk_count`/`max_workers`/`fallback_to_pandas` route full-frame mask-kind work through `select_execution_adapter` (`substrate` defaults `"pandas"`, byte-identical route); `explain_plan` (default `False`) surfaces the planner's classification on every route) |
| `src/decoy_engine/execution/_pipeline_routing.py` | `run_pipeline`'s execution-route decisions (S2 relationship routing + S3 auto-chunk routing composition), split out to hold the 600-LOC cap |
| `src/decoy_engine/quality/report.py` | `compute_quality_report` -> aggregate-only fidelity report (diagnostic + value-identity + shape) |
| `src/decoy_engine/execution/_substrate.py` | `select_execution_adapter`; `require_bool` / `require_positive_int` public fail-closed knob validators |
| `src/decoy_engine/execution/_planner.py` | `classify_job` -> `ExecutionPlan` (execution-mode planner; classification never routes on its own EXCEPT the `chunked` mode, which `run_pipeline`'s `auto_chunk` knob routes; surfaced via `run_pipeline(explain_plan=True)`) |
| `src/decoy_engine/execution/_pandas_adapter.py` | `PandasExecutionAdapter` (default) |
| `src/decoy_engine/execution/polars/_polars_adapter.py` | `PolarsExecutionAdapter` |
| `src/decoy_engine/generation/synthesize.py` | `generate_tables` (GENERATE entry) |
| `src/decoy_engine/storm/profiler.py` | `run_storm` |
| `src/decoy_engine/validation_result.py` | `ValidationResult` wire shape + `VALIDATION_CODES` |
| `src/decoy_engine/sdk.py` | Public Connector SDK (`FileSource`, `FileSink`, capabilities) |
| `src/decoy_engine/subset/__init__.py` | `run_subset_preflight` / `plan_subset` / `run_subset` (FK-aware subsetting: a pre-mask stage that carves a referentially-intact slice of a multi-table Parquet dataset; Sprint G) |
| `src/decoy_engine/release.py` | `RELEASE_PHASE` / `is_pre_ga` (the pre-GA/GA switch the CI gates read) |
| `tests/integration/golden/test_execution_e2e.py` | Canonical end-to-end caller shape |

## Directory Map

| Path | What Lives Here |
|---|---|
| `src/decoy_engine/` | Engine package |
| `src/decoy_engine/identifiers.py` | Stable sub-import namespace for identifier families (Ein/Mrn/Ndc/Npi/Ssn adapters, domains, validators); use `decoy_engine.identifiers.EinValidator` instead of the top-level package |
| `src/decoy_engine/checksums.py` | Check-digit registry for seven structured-identifier schemes (luhn, npi, iban, vin, isbn13, ean13, gtin). Public functions: `validate(scheme, value) -> bool` and `calc_check_digit(scheme, body) -> str`. python-stdnum 2.2 backs luhn/iban/ean13/isbn13/gtin; npi and vin are hand-rolled per CMS NPPES and NHTSA 49 CFR Part 565 / ISO 3779. Used internally by the FPE `checksum:` parameter to recompute check digits after permutation. |
| `src/decoy_engine/expressions/` | Two distinct expression evaluators. `_safe_eval.py` holds the simpleeval-backed `safe_eval` / `BASE_GLOBALS` / `MASK_GLOBALS` / `make_mask_globals` API used by the `formula` strategy (re-exported from the package, no caller change). `_lark_parser.py` + `grammar.lark` hold the Lark closed-grammar parser (`compile_expr` / `evaluate` / `CompiledExpression`) used by the `derived` strategy (SP-10, built) and planned for `case_when` (SP-10b). The grammar is the security boundary: no `eval()` or dynamic execution anywhere on the Lark path. |
| `src/decoy_engine/reference_tables/` | Static Parquet dataset loader used by `joint_mask` (SP-08). Public API: `load_table(name, path=None) -> ReferenceTable`. Schema convention: every table must have an `id` column (int64); rows are id-sorted at load. Three public-domain tables ship in `data/`: `us_zip5_city_state` (USPS/Census ACS), `vehicle_make_model_year` (NHTSA vPIC), and `us_zip3_population` (HHS-restricted 3-digit ZIP prefixes per 45 CFR 164.514(b)(2)(i)(B), used by `geo_generalize`). Customer-provided tables are accepted via the `path` argument. `ReferenceTable.keyed_row` is deterministic within a table version but NOT stable across versions with different row counts (see CHANGELOG SP-06 and SP-08 entries). Note: `code_set` (SP-09) uses a separate corpus loader in `decoy_engine/codesets/`, not this loader. |
| `src/decoy_engine/_MAP.md` | Engine package navigation map |
| `src/decoy_engine/config/` | `PipelineConfig`, `RelationshipConfig`, `TableConfig`, source/target descriptors |
| `src/decoy_engine/plan/` | `compile_plan` + frozen `Plan`; `_seed.py` holds the shared seed validator used by the compiler, pipeline profile path, and generation; `_graph.py` builds plan-side relationship and namespace tuples; `_seed_envelope.py` builds per-table/column/group `SeedEnvelope` (split from `_compile.py`, F11d) |
| `src/decoy_engine/execution/` | `ExecutionAdapter` Protocol, `PandasExecutionAdapter`, `select_execution_adapter`, `_strategies/` (column-strategy handlers), `polars/` (Polars adapter), `_sequential.py` (bounded-memory masked table-by-table FK execution in FK-topological order; S2 is the default path for eligible jobs routed by `run_pipeline`), `_transactional_sink.py` (all-or-nothing sink protocol + Parquet reference implementation), `_planner.py` (execution-mode planner), `_chunked.py` (`run_mask_pipeline_chunked`, `concat_masked_chunks`; backs the `auto_chunk` route), `_pipeline_routing.py` (composes the sequential + auto-chunk routing decisions) |
| `src/decoy_engine/execution/_transactional_sink.py` | `TransactionalSink` three-method protocol (write/commit/abort; `abort` is best-effort and must not raise) and `ParquetTransactionalSink` file-based reference implementation. `commit()` publishes via a single atomic POSIX directory rename (`os.replace`, POSIX rename(2)); either every Parquet file lands at once or nothing is published (visibility-atomicity, not fsync durability). A pre-existing non-empty target causes commit to fail closed. Both are exported from `decoy_engine.execution`. Used by `run_sequential` (`_sequential.py`) for FK jobs that require all-or-nothing output. |
| `src/decoy_engine/generation/` | `generate_tables` + composite + pool helpers; `_referenced_formula.py` runs the cross-column formula post-pass (lazy-imported) |
| `src/decoy_engine/relationships/` | `build_relationship_graph`, `build_namespace_registry`, `check_orphan_fk_policy_completeness`, `OrphanPolicy` |
| `src/decoy_engine/providers_v2/` | `ProviderRegistry`, identifier adapters |
| `src/decoy_engine/profile/` | Profile types + `profile_source`; `_fixed_width_reader.py` (`read_fixed_width`, fixed-width file parsing per layout spec) |
| `src/decoy_engine/storm/` | Profiling, detectors, and post-mask integrity checks (`postmask/residual_pii.py`, `postmask/fk_preservation.py`) |
| `src/decoy_engine/storm/` | Profiling and detectors; `_classification.py` holds column-shape classification helpers; `_distributions.py` holds distribution-snapshot builders; `_patterns.py` holds the detector regex catalog; `_validators.py` holds detector validation helpers (all split from `profiler.py`/`detectors.py`, F11b/F11c) |
| `src/decoy_engine/storm/` | Profiling and detectors; `eval/` (labeled fixtures + regex-baseline recognition harness + ML measurement substrate) and `features/` (deterministic per-column feature builder) are off-run-path field-recognition tooling (BF2 / ML0); `eval/split.py` + `eval/bands.py` are scaffolding for ML2.2; `model_pack/` holds the LightGBM pack loader + featurizer + ML3.1 classification function + ML3.2 HMAC provenance signing |
| `src/decoy_engine/validators/` | Job-level validator framework (SP-05 / P5.INFRA.4; extended Sprint 2 honesty pack). `validate(outputs, config, *, sources=None)` runs all configured validators after column passes complete and returns a frozen `ValidationReport`. 11 built-in validators: `luhn`, `npi`, `iban`, `vin` (delegate to SP-04 checksums), `fk_intact`, `no_orphan_children` (SDV HMA1 parent-first DAG pattern), `leak_check` (`_leak_check.py`: post-mask residual-value verification, two-tier ratio-thresholded), `regex_match`/`column_in_set` (`_generic.py`: Great Expectations-style generic column checks), `parent_window_respected`/`reconciliation_holds` (`_relationship_checks.py`: relationship-aware, mirror `_fk_validators.py`'s edge lookup). Fail-closed by default: any failure raises `ValidatorFailedError`. |
| `src/decoy_engine/execution/_row_errors.py` | Per-row strategy-error side channel (Sprint 2 honesty pack, D7). `RowError` (handler-facing) / `RowErrorRecord` (table-attributed, on `ExecutionResult.row_errors`); `drain_row_errors` is the shared drain point both execution adapters call after every node dispatch. Producers: `bucketize`/`date_shift` (`format_error`), `code_set` (`mask_error`). |
| `src/decoy_engine/quarantine.py` | Quarantine-row support (SP-05 / P5.B; generalized Sprint 2 honesty pack D9). `apply_quarantine(outputs, report, quarantine_config, *, row_errors=())` normalizes validator findings + row errors into one worklist and routes failing rows to a JSONL file, removing them from main output; job continues and succeeds. Wired triggers: `validation_fail`, `format_error`, `mask_error`. Fail-closed guards: empty `output_path` raises, unwired triggers raise, misconfigured FK/relationship validators raise, an uncovered row error raises `RowErrorsFailedError`. |
| `src/decoy_engine/subset/` | FK-aware row subsetting (Sprint G, SS1-SS5): a pre-mask stage that pulls a referentially-intact slice of a multi-table Parquet dataset (deterministic sample / filter / explicit keys, closed over the declared `relationships` graph). Public surface: `run_subset_preflight`, `plan_subset`, `run_subset`, `relationships_from_config`, `subset_inputs_from_config` (see `subset/__init__.py`; not re-exported from top-level `decoy_engine` this sprint). `_preflight.py` (SS1 fail-closed FK-validity pre-check, an anti-join adapter over `validation/post/_checks/_fk_validity.py`'s semantics), `_seed.py` (SS2 sample/filter/keys seed selection), `_closure.py` (SS3 downward+upward fixpoint closure engine, the novel core), `_policy.py` (SS4 fan-out budget + dry-run estimate, hard-fail-before-materialization), `_materialize.py` + `_manifest.py` (SS5 Parquet write + evidence manifest with no raw key values or filter literals), `_edges.py`/`_keys.py`/`_types.py`/`_errors.py` (support). Parquet-only, file/batch sources only, manual `relationships` declaration only; polymorphic FKs unsupported. `decoy subset` CLI (SS6) and platform UI (SS7) are follow-ons, not built here. |
| `src/decoy_engine/validation/` | `validate_config` |
| `src/decoy_engine/validation_result.py` | `ValidationResult`, `ValidationMessage`, `VALIDATION_CODES` |
| `src/decoy_engine/sdk.py` | Public Connector SDK (file-shaped) |
| `src/decoy_engine/connectors/` | In-tree file connectors (`s3.py`, `gcs.py`, `sftp.py`) |
| `src/decoy_engine/internal/` | Private helpers (crypto, faker setup, validators) |
| `src/decoy_engine/disguises/` | Disguise registry (post-FORECAST replacement) |
| `src/decoy_engine/instrumentation/` | Public timing / collector helpers |
| `src/decoy_engine/determinism/` | Seed protocol, key derivation |
| `src/decoy_engine/transforms/` | Leaf modules kept because V2 strategies in `execution/_strategies/` import them: `base.py`, `code_set.py`, `date_shift.py`, `derived.py`, `formula.py`, `fpe.py`, `geo_generalize.py`, `joint_mask.py`, `text_mask.py`; a future sprint may relocate them into `_strategies/_reused_v1/`. |
| `src/decoy_engine/transforms/text_mask.py` | `text_mask` core logic (SP-07): HMAC-SHA256-keyed span masking, `DETECTOR_DEFAULTS` dispatch table (TIER-1 and TIER-2 entries), `unmatched_span_policy` enforcement, raw-value isolation. Calls `iter_spans` directly; STORM is the single detector source. |
| `src/decoy_engine/execution/_strategies/_text_mask.py` | `TextMaskHandler` (SP-07): thin V2 StrategyHandler wrapping `transforms/text_mask.py`. Registered in `SCALAR_HANDLERS`. Reads `detectors`, `per_detector_strategy`, `unmatched_span_policy`, and `token` from `plan.provider_config`. |
| `src/decoy_engine/transforms/joint_mask.py` | `joint_mask` core logic (SP-08): HMAC-SHA256-keyed reference-row selection (mask mode via `ReferenceTable.keyed_row`, RFC 2104) and seeded random row selection (gen mode via `numpy.default_rng`). `JointMaskConfig` and `validate_joint_mask_config` enforce fail-closed execution-time validation. Cross-version keyed-access caveat: `keyed_row` position is `HMAC(...) % row_count`; not stable if `row_count` changes. |
| `src/decoy_engine/execution/_strategies/_joint_mask.py` | `JointMaskHandler` (SP-08): thin V2 StrategyHandler wrapping `transforms/joint_mask.py`. Multi-column: writes all `columns` from `provider_config` in one pass. Reads `columns`, `reference`, `key_by`, and `mode` from `plan.provider_config`. |
| `src/decoy_engine/transforms/geo_generalize.py` | `geo_generalize` core logic (SP-08): HIPAA Safe Harbor ZIP cascade per 45 CFR 164.514(b)(2). `cascade_zip_column` computes in-dataset counts once then applies the configured cascade levels for each row. Restricted ZIP3 prefixes loaded from `us_zip3_population` via `load_table`. `CascadeEvidence` frozen dataclass holds the cascade decision label for each row. |
| `src/decoy_engine/execution/_strategies/_geo_generalize.py` | `GeoGeneralizeHandler` (SP-08): thin V2 StrategyHandler wrapping `transforms/geo_generalize.py`. Surfaces cascade decisions as a `QualityWarning` (code `geo_generalize_cascade`) when any row was generalized past `zip5`. |
| `src/decoy_engine/transforms/code_set.py` | `code_set` core logic (SP-09): HMAC-SHA256-keyed corpus-code selection (mask mode, RFC 2104, output != input via domain-exclusion) and `derive_index`-keyed row-by-row selection (gen mode, SEED_PROTOCOL_VERSION-covered). `CodeSetConfig` and `validate_code_set_config` enforce fail-closed execution-time validation. Corpus loader sorts by `code` ascending for stable keyed access. `chapter_preserve` fail-closed: absent chapter raises `PlanCompileError(code_set_chapter_absent)`; sole-member bucket raises `PlanCompileError(code_set_sole_member_bucket)`. SP-06 cross-version caveat applies: HMAC % candidate_count remaps if corpus row count changes. |
| `src/decoy_engine/execution/_strategies/_code_set.py` | `CodeSetHandler` (SP-09): thin V2 StrategyHandler wrapping `transforms/code_set.py`. Registered in `SCALAR_HANDLERS`. Reads `code_set`, `chapter_preserve`, `corpus_source`, and `mode` from `plan.provider_config`. Gen mode threads `row_index` and `plan.namespace` into `apply_code_set` for row-indexed variation and cross-column decorrelation. |
| `src/decoy_engine/transforms/derived.py` | `derived` core logic (SP-10): `DerivedConfig.from_dict` parses config and compiles the expression via `compile_expr` at config-parse time; `apply_derived` is the single row-evaluation entry point for both mask mode and generate mode. Null propagation (`explicit_null`/`sentinel`/`default`) and optional numeric bounds clipping are applied here. `_get_column_refs` extracts referenced column names from the compiled parse tree for `plan/_checks.py` to use at plan-compile time. No `eval()` or dynamic execution on this path. |
| `src/decoy_engine/execution/_strategies/_derived.py` | `DerivedStrategyHandler` (SP-10): thin V2 StrategyHandler wrapping `transforms/derived.py`. Registered in `SCALAR_HANDLERS`. Iterates the DataFrame row by row, builds a row context dict, and calls `apply_derived`. No RNG; deterministic by construction. Generate-mode wiring lives in `generation/synthesize.py::_derived_generate`. |
| `src/decoy_engine/codesets/` | Shipped code corpora for the `code_set` strategy (SP-09). Four Parquet files: `icd10.parquet` (65 rows, CMS ICD-10-CM, US public domain), `hcpcs.parquet` (32, CMS HCPCS, US public domain), `ndc.parquet` (38, FDA NDC, US public domain; `chapter` column is a Decoy-defined therapeutic bucket A/B/C/D, not a native NDC attribute), `mcc.parquet` (62, ISO 18245 MCC; see NOTICE). All corpora have a `code` column (string); corpora with chapter support also have a `chapter` column. |
| `src/decoy_engine/reference_tables/data/us_zip3_population.parquet` | HHS-restricted 3-digit ZIP prefix table (SP-08). Schema: `id` (int64) + `zip3` (str). Contains the 17 3-digit ZIP prefixes whose geographic units have population below 20,000 per the Census-based determination (45 CFR 164.514(b)(2)(i)(B)). Loaded by `geo_generalize` via `load_table("us_zip3_population")`. |
| `src/decoy_engine/generators/` | `columns.py` and `derivation.py` kept for V2 reuse; `generation/synthesize.py` imports `ColumnGenerator`. `derivation.py` exports `GenDeriveContext` (v6 per-column generation derivation) and `strategy_config_fingerprint`. `_distribution.py` holds distribution-snapshot sampler methods and `_formula.py` holds formula evaluation methods, both private mixins folded into `ColumnGenerator` (split from `columns.py`, F11a). |
| `src/decoy_engine/walks/` | Cross-file / drift / inference helpers; consumed by `tests/integration/test_walks_*`. Not part of the public API. |
| `src/decoy_engine/forecast/` | Empty (only `__pycache__`); the V1 FORECAST recommender was removed in S22. Safe to delete. |
| `tests/` | `unit/`, `integration/golden/`, `integration/compat_corpus/`, `parity/`, `perf/`, `perf_fixtures/`, `benchmark/`, `privacy/`, `security/`, `sentry/`, `connectors/`, `snapshots/` |
| `tests/integration/compat_corpus/` | Cross-version compatibility corpus: locked artifacts a later engine must still read (compatibility-contract section 3.1/3.2); `regenerate.py` mints the baseline |
| `scripts/` | Dev/CI scripts; includes `check_compat_preflight.py` (compat pre-flight gate) and `prove_regression.py` (bugfix regression-proof gate) |
| `docs/` | Local engine docs (security notes, parity, the compatibility contract, in-repo index). Active planning lives in the commercial platform repo. |
| `.pytest_cache/`, `__pycache__/`, `logs/`, `mappings/` | Ignore generated/runtime content |

## Where Do I Find...

| Task | Start Here |
|---|---|
| Current roadmap | Maintained in the commercial platform repo |
| Engine audit | Maintained in the commercial platform repo |
| Remediation plan | Maintained in the commercial platform repo |
| Public exports | `src/decoy_engine/__init__.py` |
| Identifier validators/adapters/domains | `src/decoy_engine/identifiers.py` |
| Check-digit validation and FPE checksum support | `src/decoy_engine/checksums.py` |
| Expression parser (closed grammar, `derived`/`case_when`) | `src/decoy_engine/expressions/_lark_parser.py` (`compile_expr`, `evaluate`, `CompiledExpression`) |
| Formula safe-eval (`formula` strategy) | `src/decoy_engine/expressions/_safe_eval.py` (`safe_eval`, `make_mask_globals`, `BASE_GLOBALS`, `MASK_GLOBALS`) |
| Reference-table loader (`joint_mask` tables) | `src/decoy_engine/reference_tables/` (`load_table`, `ReferenceTable`) |
| `code_set` corpus loader (shipped + customer corpora) | `src/decoy_engine/transforms/code_set.py` (`load_corpus`, `CodeSetConfig`) |
| `code_set` shipped corpora (ICD-10, HCPCS, NDC, MCC) | `src/decoy_engine/codesets/` |
| Job-level validator framework (validators: config block) | `src/decoy_engine/validators/` (entry: `validate`; types: `ValidationReport`, `ValidatorFinding`) |
| Quarantine-row routing (quarantine: config block) | `src/decoy_engine/quarantine.py` (`apply_quarantine`, `quarantine_manifest`) |
| FK-aware subsetting: public entrypoints, scope, config surface | `src/decoy_engine/subset/__init__.py` (public surface); `src/decoy_engine/config/_subset.py` (`subset:` PipelineConfig block); [relationships](docs/relationships.md) "Subsetting" section; CHANGELOG "Sprint G FK-aware subsetting core" entry |
| FK-aware subsetting core logic: preflight, seed selection, closure engine, fan-out/dry-run, materialization+manifest | `src/decoy_engine/subset/_preflight.py`, `_seed.py`, `_closure.py`, `_policy.py`, `_materialize.py`, `_manifest.py` |
| Config schema | `src/decoy_engine/config/_pipeline.py` |
| Relationship schema | `src/decoy_engine/config/_relationships.py` (reference doc lives in the commercial platform repo) |
| Fixed-width format config (S4) | `src/decoy_engine/config/_fixed_width.py` (`FixedWidthColumn`, `FixedWidthLayout`, column-spec for fixed-width file parsing) |
| Plan compilation | `src/decoy_engine/plan/_compile.py` |
| Seed normalization (shared validator) | `src/decoy_engine/plan/_seed.py` |
| Execution strategies | `src/decoy_engine/execution/_strategies/` |
| `text_mask` strategy: config surface, TIER table, operator doc | `docs/strategies.md` (text_mask section) |
| `text_mask` core logic: span masking, dispatch table, HMAC keying | `src/decoy_engine/transforms/text_mask.py` |
| `joint_mask` strategy: config surface, modes, cross-version caveat | `docs/strategies.md` (joint_mask section) |
| `joint_mask` core logic: HMAC-keyed row selection, gen-mode sampling | `src/decoy_engine/transforms/joint_mask.py` |
| `geo_generalize` strategy: config surface, cascade levels, Safe Harbor framing | `docs/strategies.md` (geo_generalize section) |
| `geo_generalize` core logic: ZIP cascade, restricted-prefix check, evidence | `src/decoy_engine/transforms/geo_generalize.py` |
| HHS-restricted ZIP3 prefix table | `src/decoy_engine/reference_tables/data/us_zip3_population.parquet` |
| `code_set` strategy: config surface, modes, chapter_preserve, corpora, cross-version caveat | `docs/strategies.md` (code_set section) |
| `code_set` core logic: HMAC-keyed selection, corpus loader, chapter_preserve, gen mode | `src/decoy_engine/transforms/code_set.py` |
| `derived` strategy: config surface, both modes, validation timing, security note, carry-forwards | `docs/strategies.md` (derived section) |
| `derived` core logic: expression evaluation, null propagation, bounds clipping, ref extraction | `src/decoy_engine/transforms/derived.py` |
| `derived` generate-mode wiring (synthesize path) | `src/decoy_engine/generation/synthesize.py::_derived_generate` |
| Substrate selection | `src/decoy_engine/execution/_substrate.py` |
| Pandas adapter | `src/decoy_engine/execution/_pandas_adapter.py` |
| Bounded-memory FK execution: routing + row-error enforcement (S2) | `src/decoy_engine/execution/_pipeline.py` (routing predicate `_sequential_eligible`, route decision, telemetry); `src/decoy_engine/execution/_sequential.py` (`run_sequential` table-by-table FK-topological masking with per-node row-error draining, per-table fail-loud/quarantine, cascaded child leak closure) |
| Transactional sink protocol and Parquet reference implementation | `src/decoy_engine/execution/_transactional_sink.py` (`TransactionalSink`, `ParquetTransactionalSink`; both exported from `decoy_engine.execution`) |
| Polars adapter | `src/decoy_engine/execution/polars/_polars_adapter.py` |
| Execution-mode planner (observe-only; `run_pipeline(explain_plan=True)`) | `src/decoy_engine/execution/_planner.py` (`classify_job`, `ExecutionPlan`) |
| Auto-chunk routing (`run_pipeline(auto_chunk=True)`, default on) | `src/decoy_engine/execution/_chunked.py` (`run_mask_pipeline_chunked`, `concat_masked_chunks`); eligibility gates live in `_planner.py` |
| Job-level performance gates (opt-in `pytest.mark.benchmark`) | `tests/perf/test_job_performance_gates.py` |
| Generation | `src/decoy_engine/generation/synthesize.py` |
| Relationships and namespace | `src/decoy_engine/relationships/_graph.py`, `_namespace.py` |
| Provider registry | `src/decoy_engine/providers_v2/_registry.py` |
| Validation surface | `src/decoy_engine/validation/_config.py`, `src/decoy_engine/validation_result.py` |
| Connectors | `src/decoy_engine/sdk.py`, `src/decoy_engine/connectors/` |
| STORM | `src/decoy_engine/storm/` |
| ML field-recognition evaluation (BF2 / ML0) | `src/decoy_engine/storm/eval/` (harness, split, bands); see CHANGELOG §"ML-foundation measurement substrate" |
| ML3.1 column-type classification | `src/decoy_engine/storm/model_pack/classify.py` (`classify_fields` function); see CHANGELOG §"ML3 field classification and provenance" |
| ML3.2 manifest provenance signing | `src/decoy_engine/storm/model_pack/provenance.py` (`sign_manifest`, `verify_manifest`); signatures enforced by `ModelPackLoader` when `DECOY_PACK_SIGNING_KEY` is set |
| Canonical caller shape | `tests/integration/golden/test_execution_e2e.py::_run` |
| Parity notes | `tests/parity/SEMANTIC_DIFFERENCES.md` |

## Known Issues / Flags

| Issue | Note |
|---|---|
| `docs/ml-benchmarking-and-privacy.md` missing | Referenced extensively in `src/decoy_engine/storm/eval/` (harness.py, split.py, bands.py) and test files with citations like §A.1 / §A.3 / §A.4 / §A.7 / §B.4. This document is the authoritative ML measurement standard but is not present in the engine repo. Likely lives in the commercial platform repo. Cross-link when available. |
| `docs/v2/ml/baseline-report.json` path | Uses retired "v2" nomenclature. Should be relocated to `docs/ml/baseline-report.json` or similar at a future sprint. Flagged for relocation, do not move independently (shared with commercial platform schema docs). |

## Conventions

| Situation | Convention |
|---|---|
| Add public API | Export deliberately from `__init__.__all__` |
| Add masking strategy | Implement `StrategyHandler` under `execution/_strategies/`, wire into the Pandas adapter dispatch (and Polars counterpart if you target both substrates), add unit + golden coverage |
| Add provider | Register in `providers_v2/_registry.py`; the planner closed-checks unknown providers with `code=unknown_provider` |
| Add connector | Inherit from `FileSource` / `FileSink` in `sdk.py`, declare capabilities, ship in-tree under `connectors/` or as an external package via the `decoy.connectors` entry point |
| Add STORM detector | Add detector provenance, positive/negative tests |
| Shared CLI/platform behavior | Implement here first, then wrap in CLI/platform |

## Gotchas

| Gotcha | Note |
|---|---|
| Validation runs once at the choke-point | `PipelineConfig.model_validate(yaml).model_dump()` validates strictly; downstream engine functions do not re-validate |
| Expression safety is sensitive | Avoid adding direct `eval()` paths; use the safe-eval helpers in `expressions/_safe_eval.py` for `formula` strategy work, or `expressions/_lark_parser.py` for closed-grammar `derived`/`case_when` work |
| Engine is library code | Do not import platform or CLI |
| Substrate selection respects env | `DECOY_SUBSTRATE=polars\|pandas` overrides the default per `_substrate.py` |
| Public stubs exist | Check capability docs before claiming production behavior |
| Leaf V1 packages | `transforms/` and `generators/` keep a few files that V2 strategies still import; do not re-introduce V1 dispatch in them |

## Ignore For Navigation

| Path | Reason |
|---|---|
| `.pytest_cache/`, `__pycache__/`, `.mypy_cache/`, `.ruff_cache/` | Generated |
| `logs/`, `mappings/` | Runtime output |
| `docs/_build/` | Generated docs |
| `src/decoy_engine/forecast/` | Empty post-S22 (only `__pycache__`); pending deletion |
| `tests/benchmark/calibration/results.md` | Read only for benchmark tasks |
