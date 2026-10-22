Status: record (survey evidence for the STORM-split plan)

Rules consulted: 00-universal, development-loop, refactoring, api-and-compatibility, verification, documentation (this is factual survey evidence for the plan of the same date; the plan carries the full routing list).

# STORM-split survey — three-repo consumer + surface inventory

Companion evidence for [`2026-10-09-storm-split-taxonomy.md`](2026-10-09-storm-split-taxonomy.md). All file:line references captured 2026-10-10 against engine worktree `refactor/storm-split-taxonomy`, decoy-platform `main`, and `decoy` CLI `main`. This document is the "survey §A–§E" the plan cites; it carries the locator inventory so a builder can act without re-surveying. It is authoritative for the FUNCTIONAL migration surface but not claimed exhaustive for every DOC/comment mention; the completeness backstop is the no-`decoy_engine.storm`-import sentry + the leftover-`storm_`-name sentry + full three-repo CI (plan test 4, 6, 8), which catch any locator this record missed.

Convention below: **FUNCTIONAL** = a production import/call that breaks on a hard rename; **DOC** = a docstring/comment mention (update for accuracy, non-breaking); **TEST** = a test-tree reference.

---

## §A. decoy-platform consumers

### A.1 Functional engine-symbol imports (production)
- `api/classify/router.py:51` `decoy_engine.storm.model_pack.classify` (classify entrypoints); `:57` `decoy_engine.storm.model_pack.provenance`; `:61` `decoy_engine.storm.model_pack.types.ModelPackManifest`.
- `api/storm/ml_accounting.py:42` `decoy_engine.storm.model_pack.types` (re-exported under the platform name).
- `api/storm/detectors_router.py:32` `decoy_engine.storm.CustomDetectorSpec`; `:33` `decoy_engine.storm.detectors.REGISTERED_DETECTORS`; `:34` `decoy_engine.storm.domains.domain_for`; `:35` `decoy_engine.storm.name_hints.loader.load_name_hint_terms`.
- `api/storm/post_mask_hook.py:260` `decoy_engine.storm.postmask.run_storm_post_mask` (RENAME → `run_post_mask`); `:279` `decoy_engine.storm.postmask.SCHEMA_VERSION` (value KEEP).
- `api/storm/router.py:44` `decoy_engine.storm.run_storm` (RENAME → `profile_columns`); `:2051` `decoy_engine.storm.recommendations.*`.
- `api/storm/helpers.py:741` `decoy_engine.storm.types.FieldStats, StormProfile` (RENAME → `ColumnProfile`), used at `:746,:753`. **Wrapped in `try/except ImportError: return []` at `:743`** — the swallow must be narrowed (plan 3d) so a missed rename fails loudly; the same try also imports non-renamed `decoy_engine.walks`, so narrow surgically.
- `scripts/benchmark_canonical.py:112` `decoy_engine.storm.run_storm` (RENAME → `profile_columns`).

### A.2 Doc/comment references (accuracy-only, non-breaking)
`api/storm/schemas.py:234` (StrategyDefault mirror), `api/storm/__init__.py:4`, `api/storm/router.py:6,15,17`, `api/storm/builtin_catalog.py:3`, `api/storm/post_mask_service.py:32`, `api/models.py:1609` (StormPostMaskReport), `api/pipelines/preflight.py:23`, `api/storm/detectors_model.py:65` (name_hints YAML path), `api/storm/detectors_router.py:143,156` (name_hints + domains path prose).

### A.3 Test sites
`tests/test_classify_fields.py`, `tests/test_ds3_ml_feature_accounting.py`, `tests/test_engine_version_floor.py` (`:78` imports `decoy_engine.storm.model_pack.classify._default_pack_dir` — a rename site AND it parses the `pyproject` pin), `tests/test_pipeline_templates.py`, `tests/test_post_mask_sampling.py`, `tests/test_storm_detection_review.py`, `tests/test_storm_overrides.py`.

Web (`web/src/...`) imports **no** engine symbols — only the `run_storm` wire field and `/storm/...` routes (both KEEP, §D).

---

## §B. Version gates + install shapes

- `api/engine_compat.py:163` `ENGINE_TRANSFORM_OWNERSHIP_SPECIFIER = "==0.8.0rc1"` + `assert_engine_transform_ownership` (SpecifierSet + `apply_table_transforms` attr check; never module paths).
- `pyproject.toml:29` engine pin `==0.8.0rc1`.
- `deploy/Dockerfile.production:81-83` installs engine from sibling SOURCE non-editable; `ARG DECOY_ENGINE_EXPECTED_VERSION=0.8.0rc1` at `:92`; version assert at `:94`; ownership gate at `:99`.
- CI `.github/workflows/backend.yml` + `fixtures-verify.yml` check out the engine at a **pinned SHA** (`ENGINE_PIN_SHA` / `ENGINE_PIN_VERSION="0.8.0rc1"`) and `pip install -e` (editable). So platform CI is already protected against a moving engine `main`; the `==` gate protects prod only if the engine rc is bumped in the same engine PR.
- **Dev/editable hazard:** an un-migrated platform checkout run against a renamed editable engine hits the hard rename immediately (metadata-version can lag). Rollback must restore a tested platform/engine PAIR, not just revert engine main.

---

## §C. decoy CLI consumers (`/home/cam/vscode/decoy/`)

### C.1 Functional engine-symbol imports (production)
- `src/decoy/api.py:559` `from decoy_engine import run_storm` (call `:592`) → `profile_columns`.
- `src/decoy/cli/init.py:350` `from decoy_engine.storm import run_storm` (call `:351`) → `profile_columns`.
- `src/decoy/cli/storm.py:648` `from decoy_engine import run_storm` (call `:653`) → `profile_columns`; `:1413` `from decoy_engine.storm.postmask import run_storm_post_mask` (call `:1465`) → `run_post_mask`.
- `src/decoy/cli/demo.py:146` `from decoy_engine import run_storm` (call `:149`) → `profile_columns`.
- `src/decoy/cli/profile.py:153,165` `from decoy_engine import run_storm` → `profile_columns`.
- **Failure-path imports (finding 1):** `src/decoy/cli/extras.py:193` and `src/decoy/cli/run.py:688` `from decoy_engine.storm.ner import NerUnavailableError`. The class name KEEPs; only the path moves to `decoy_engine.detection.ner`. These catches translate missing-extra ImportErrors into install guidance — a stale import silently bypasses that translation, so both need explicit failure-path coverage.

### C.2 Doc/comment references (accuracy-only)
`src/decoy/api.py:13,536,555`, `src/decoy/_native_gate.py:217` (and `:244` reads the `run_storm` config key — KEEP), `src/decoy/cli/storm.py:103,1395,1398,1453,1571`, `src/decoy/cli/_init_inference.py:65,242`, `src/decoy/cli/profile.py:27,157`.

**NOT doc/comment — KEEP as observable output (finding, rev 4):** `src/decoy/cli/storm.py:547` (an option-`help=` string) and `src/decoy/cli/preflight.py:615` (a runtime diagnostic) both render the retained `run_storm` config key in user-visible CLI text. They fall under the output-preservation guarantee (§D KEEP), not the rewrite-for-accuracy set — leave the `run_storm` token intact.

### C.3 Test sites
`tests/e2e/test_ner_missing_extra.py`, `tests/e2e/test_storm_integrity.py`, `tests/sentry/test_profile_raw_value_isolation.py`, `tests/unit/test_api.py`, `tests/unit/test_extras.py`, `tests/unit/test_init_inference.py`, `tests/unit/test_native_gate.py`.

### C.4 CLI install workflows + floor (finding 1 — the gap)
- Floor only: `pyproject.toml:88` `"decoy-engine>=0.6.0"` — **no `==` pin, no version/ownership gate.** Extras `ner/ml/cloud/vault/native` (`:116-128`) map to engine extras (`decoy-engine[...]`), KEEP.
- CI installs engine from a **moving `@main`**: `.github/workflows/ci.yml:114`, `:162`, `:231` (`pip install "git+https://github.com/louiskeep/decoy-engine@main"`); `.github/workflows/release-smoke.yml:80` (same). These are what break the instant the engine rename lands on `main`, because they pull HEAD, not a pinned artifact.

---

## §D. Keep-vs-rename resolution (Cam: "storm = product label, internals descriptive")

### RENAME (public symbols + module paths)
| Old | New |
|---|---|
| `decoy_engine.storm.*` module tree | `decoy_engine.{detection,profiling,verification}.*` |
| `run_storm` (`profiler.py:445`) | `profile_columns` |
| `_run_storm_inner` (`profiler.py:525`, internal) | `_profile_columns_inner` |
| `StormProfile` (`types.py`) | `ColumnProfile` |
| `run_storm_post_mask` (`postmask/runner.py:38`) | `run_post_mask` |
| `StormPostMaskReport` (`postmask/types.py`) | `PostMaskReport` |
| `storm_profiles_to_snapshot` (`walks/cross_file.py:85`, public in `walks.__init__:24,56`) | `profiles_to_snapshot` |
| `DetectorMatch`, `FieldStats`, `SentinelFlag` | names KEEP, path moves |
| exception classes (`NerUnavailableError`, `NameHintLoaderError`, `ModelPackLoadError`) | names KEEP, path moves |

### KEEP (user-facing / wire / persisted / telemetry — a rename here is a break)
- `run_storm` **config field** everywhere: engine `config/_pipeline.py:81`; the `config.get("run_storm")` reads at `execution/physical/_shadow_generation.py:421`, `execution/_unified_slice_admission.py:246`; platform `Pipeline`/`PipelineCreate`; web; CLI `_native_gate.py:244`.
- `"storm-post-mask/v1"` schema string VALUE (persisted on platform `JobStormReport` rows).
- **`storm_run_group_id`** (finding 2): persisted platform column `api/storm/models.py:30,200` + API response field `api/storm/schemas.py:97,147,173,304,362,579`. The engine occurrence (`walks/cross_file.py:7`) is a **docstring only** — update text for the new vocabulary but the identifier itself KEEPs across the wire. Add a wire-field preservation assertion.
- `"storm.scan"` telemetry event + platform `queue_worker` mode + UI; `storm.read_samples` permission; `/storm/...` routes + `web/src/storm/...`; the CLI's own `storm` product surface (`storm_app`, `ui/storm_animation.py`).

---

## §E. Initializer export specs (acyclic: detection = base; profiling + verification → detection; no profiling↔verification edge)

- `detection/__init__`: `CustomDetectorSpec, DetectorMatch, DetectionSignal, Domain, DOMAIN_BY_DETECTOR, domain_for, REGISTERED_DETECTORS, Span, load_name_hint_terms, NerUnavailableError`. No profiling/verification import at init.
- `profiling/__init__`: `profile_columns, ColumnProfile, FieldStats, SentinelFlag, Distribution, TopValue, StrategyDefault, DEFAULT_STRATEGY_BY_DETECTOR, get_default_strategy, known_detector_ids` (may import detection types for embedding). `model_pack` stays a sub-package, NOT re-exported; deep paths preserved.
- `verification/__init__`: `run_post_mask, PostMaskReport, SCHEMA_VERSION, Severity, FKPreservationFinding, PolicyValidationFinding, ResidualPIIFinding` (imports detection only).
- Top-level `decoy_engine/__init__.py:240,321-434` re-exports `DetectorMatch` (from detection), `FieldStats`, `SentinelFlag`, `ColumnProfile`, `profile_columns`.

### §E.2 Engine storm package tree (what moves)
`detection/` ← `detectors.py`, `_patterns.py`, `_validators.py`, `ner.py`, `domains.py`, `name_hints/`, detection types (`Span`, `DetectorMatch`, `CustomDetectorSpec`, `DetectionSignal`).
`profiling/` ← `profiler.py`, `_classification.py`, `_distributions.py`, `sentinels.py`, `recommendations.py`, `model_pack/`, `features/`, `eval/`, profile types (`StormProfile`→`ColumnProfile`, `FieldStats`, `Distribution`, `TopValue`, `SentinelFlag`).
`verification/` ← `postmask/` (`run_storm_post_mask`→`run_post_mask`, `StormPostMaskReport`→`PostMaskReport`; `SCHEMA_VERSION` value kept).

### §E.3 Engine-internal storm-named symbols OUTSIDE the storm package
- `walks/cross_file.py:85` `storm_profiles_to_snapshot` → `profiles_to_snapshot` (public, consumer-transition item; platform `helpers.py` imports it via `run_cross_file_walk`); `:34` imports `StormProfile`; `:58` call site; `:7` docstring (mentions `storm_run_group_id`, a KEEP field).
- `profile/_pii.py:33` imports `run_storm, DetectorMatch`; `:62` call → retarget to `profiling`; `profile/_walk.py:17` docstring.
- `execution/native/_kernels_scalar.py:58` FUNCTIONAL `from decoy_engine.storm.detectors import iter_spans` → retarget to `decoy_engine.detection.detectors`.
- `scripts/gen_capability_matrix.py:135` FUNCTIONAL `from decoy_engine.storm.detectors import _SPAN_DETECTORS, REGISTERED_DETECTORS` → retarget to `decoy_engine.detection.detectors`.
- `profiler.py:525` `_run_storm_inner` → `_profile_columns_inner` (internal; a no-`decoy_engine.storm`-import sentry will NOT catch a leftover `storm_`-named symbol — needs a separate symbol-name grep sentry).

### §E.4 Sentries that reference storm paths (retarget)
module-size census (detectors 1046, profiler 635, eval/corpus 976, model_pack/trainer 715); methodology-citation glob + `docs/methodology-registry.yaml` (model_pack/provenance.py, ner.py); log-interpolation (model_pack/loader.py, profiler.py); exception-hierarchy (ner.NerUnavailableError, name_hints.loader.NameHintLoaderError, model_pack.loader.ModelPackLoadError); ruff per-file-ignore `storm/detectors.py E402`. NOT `test_public_import_boundary.py` (internal-only).
