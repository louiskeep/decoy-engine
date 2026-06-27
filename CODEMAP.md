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
| `src/decoy_engine/execution/_pipeline.py` | `run_pipeline` (unified mask+generate spine; opt-in `fidelity_report` attaches a label-free `quality-report/v1` per mask table) |
| `src/decoy_engine/quality/report.py` | `compute_quality_report` -> aggregate-only fidelity report (diagnostic + value-identity + shape) |
| `src/decoy_engine/execution/_substrate.py` | `select_execution_adapter` |
| `src/decoy_engine/execution/_pandas_adapter.py` | `PandasExecutionAdapter` (default) |
| `src/decoy_engine/execution/polars/_polars_adapter.py` | `PolarsExecutionAdapter` |
| `src/decoy_engine/generation/synthesize.py` | `generate_tables` (GENERATE entry) |
| `src/decoy_engine/storm/profiler.py` | `run_storm` |
| `src/decoy_engine/validation_result.py` | `ValidationResult` wire shape + `VALIDATION_CODES` |
| `src/decoy_engine/sdk.py` | Public Connector SDK (`FileSource`, `FileSink`, capabilities) |
| `src/decoy_engine/release.py` | `RELEASE_PHASE` / `is_pre_ga` (the pre-GA/GA switch the CI gates read) |
| `tests/integration/golden/test_execution_e2e.py` | Canonical end-to-end caller shape |

## Directory Map

| Path | What Lives Here |
|---|---|
| `src/decoy_engine/` | Engine package |
| `src/decoy_engine/_MAP.md` | Engine package navigation map |
| `src/decoy_engine/config/` | `PipelineConfig`, `RelationshipConfig`, `TableConfig`, source/target descriptors |
| `src/decoy_engine/plan/` | `compile_plan` + frozen `Plan`; `_seed.py` holds the shared seed validator used by the compiler, pipeline profile path, and generation |
| `src/decoy_engine/execution/` | `ExecutionAdapter` Protocol, `PandasExecutionAdapter`, `select_execution_adapter`, `_strategies/` (column-strategy handlers), `polars/` (Polars adapter) |
| `src/decoy_engine/generation/` | `generate_tables` + composite + pool helpers; `_referenced_formula.py` runs the cross-column formula post-pass (lazy-imported) |
| `src/decoy_engine/relationships/` | `build_relationship_graph`, `build_namespace_registry`, `check_orphan_fk_policy_completeness`, `OrphanPolicy` |
| `src/decoy_engine/providers_v2/` | `ProviderRegistry`, identifier adapters |
| `src/decoy_engine/profile/` | Profile types + `profile_source` |
| `src/decoy_engine/storm/` | Profiling and detectors |
| `src/decoy_engine/validation/` | `validate_config` |
| `src/decoy_engine/validation_result.py` | `ValidationResult`, `ValidationMessage`, `VALIDATION_CODES` |
| `src/decoy_engine/sdk.py` | Public Connector SDK (file-shaped) |
| `src/decoy_engine/connectors/` | In-tree file connectors (`s3.py`, `gcs.py`, `sftp.py`) |
| `src/decoy_engine/internal/` | Private helpers (crypto, faker setup, validators) |
| `src/decoy_engine/disguises/` | Disguise registry (post-FORECAST replacement) |
| `src/decoy_engine/instrumentation/` | Public timing / collector helpers |
| `src/decoy_engine/determinism/` | Seed protocol, key derivation |
| `src/decoy_engine/transforms/` | Three leaf modules (`base.py`, `date_shift.py`, `formula.py`, `fpe.py`) kept because V2 strategies in `execution/_strategies/` import them; a future sprint may relocate them into `_strategies/_reused_v1/`. |
| `src/decoy_engine/generators/` | Two leaf modules (`columns.py`, `derivation.py`) kept for the same V2 reuse reason; `generation/synthesize.py` imports `ColumnGenerator`. `derivation.py` exports `GenDeriveContext` (v6 per-column generation derivation, replaces the pre-v6 `synthetic_column_seed`) and `strategy_config_fingerprint`. |
| `src/decoy_engine/walks/` | Cross-file / drift / inference helpers; consumed by `tests/integration/test_walks_*`. Not part of the public API. |
| `src/decoy_engine/forecast/` | Empty (only `__pycache__`); the V1 FORECAST recommender was removed in S22. Safe to delete. |
| `tests/` | `unit/`, `integration/golden/`, `integration/compat_corpus/`, `parity/`, `perf_fixtures/`, `benchmark/`, `privacy/`, `security/`, `sentry/`, `connectors/`, `snapshots/` |
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
| Config schema | `src/decoy_engine/config/_pipeline.py` |
| Relationship schema | `src/decoy_engine/config/_relationships.py` (reference doc lives in the commercial platform repo) |
| Plan compilation | `src/decoy_engine/plan/_compile.py` |
| Seed normalization (shared validator) | `src/decoy_engine/plan/_seed.py` |
| Execution strategies | `src/decoy_engine/execution/_strategies/` |
| Substrate selection | `src/decoy_engine/execution/_substrate.py` |
| Pandas adapter | `src/decoy_engine/execution/_pandas_adapter.py` |
| Polars adapter | `src/decoy_engine/execution/polars/_polars_adapter.py` |
| Generation | `src/decoy_engine/generation/synthesize.py` |
| Relationships and namespace | `src/decoy_engine/relationships/_graph.py`, `_namespace.py` |
| Provider registry | `src/decoy_engine/providers_v2/_registry.py` |
| Validation surface | `src/decoy_engine/validation/_config.py`, `src/decoy_engine/validation_result.py` |
| Connectors | `src/decoy_engine/sdk.py`, `src/decoy_engine/connectors/` |
| STORM | `src/decoy_engine/storm/` |
| Canonical caller shape | `tests/integration/golden/test_execution_e2e.py::_run` |
| Parity notes | `tests/parity/SEMANTIC_DIFFERENCES.md` |

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
| Expression safety is sensitive | Avoid adding direct `eval()` paths; use the existing safe-eval helpers in `expressions.py` |
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
