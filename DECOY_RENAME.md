# Decoy rename — `forge-engine`

This repo's slice of the **Forge → Decoy** rebrand. The engine is consumed by both `decoy` (CLI) and `decoy-platform`; this rename has to ship first.

## Why

See the brand reference (May 2026). "Forge" was crowded in dev tooling; "Decoy" captures masking + generation in one word.

## Cross-cutting rules (apply everywhere)

- **Do NOT rename the word "mask" globally.** Mask is the locked taxonomy term for a single field-level transform inside a Disguise. Only rename when it refers to the *bundle* (those become Disguises). The engine's `MaskRegistry`, `Masker`, `masker/` module are correctly named — leave them alone.
- **Public API symbols stay.** `Pipeline`, `PipelineConfig`, `DataGenerator`, `MaskRegistry`, `ConnectorRegistry` describe primitives, not the brand. Renaming them would force every consumer to refactor for no value.
- **HIPAA, never HIPPA.** CI grep gate.

## Changes

### Package + module path
- `pyproject.toml`: `name = "forge-engine"` → `name = "decoy-engine"`.
- `src/forge_engine/` → `src/decoy_engine/`. Every internal import updates.
- `SHARED_ENGINE_ARCHITECTURE.md`, `CLAUDE.md`, `dev-help.md`, README: rename brand references, leave architecture / API symbol names alone.

### What stays
- All public API class names listed above.
- The string "mask" wherever it refers to a single-field transform.
- All transform identifiers: `faker`, `hash`, `redact`, `map`, `shuffle`, `date-shift`, `formula`, `passthrough`.
- All Alembic-equivalent assets (this repo has none currently — note for future).

### What this rename unlocks
The follow-on docs in this repo (`STORM_FORECAST_REPORT.md`, `DISGUISES.md`) will add new modules under `decoy_engine/`:
- `decoy_engine/storm/` — analysis event compute
- `decoy_engine/forecast/` — recommender (pure function over StormProfile)
- `decoy_engine/disguises/` — YAML compliance bundles + field detectors

These names are taxonomy-locked (capitalized intentionally in user-facing strings; lowercased as Python module names). Do NOT prefix them with `decoy_` again.

## Sequencing

1. Land this rename first.
2. Publish `decoy-engine` to whatever index the team uses.
3. Then `decoy` (CLI) and `decoy-platform` can flip their dep.

## Verification

- `pytest` — existing suite passes unchanged (this is the load-bearing check; the public API didn't change).
- `python -c "from decoy_engine import Pipeline, PipelineConfig, DataGenerator, MaskRegistry, ConnectorRegistry"` succeeds.
- `grep -ri "forge_engine\|forge-engine" src/` returns zero hits.
- `grep -ri "HIPPA" .` returns zero hits.
- A minimal masking pipeline runs end-to-end against a sqlite fixture.
