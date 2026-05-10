# decoy-engine

Shared data masking and synthetic generation library for the Decoy product family. The CLI (`decoy`) and the web platform (`decoy-platform`) both import this as a library — neither contains masking or generation logic.

This site hosts the **auto-generated API reference**, regenerated on every push to `main` by [`.github/workflows/docs.yml`](https://github.com/louiskeep/decoy-engine/blob/main/.github/workflows/docs.yml). It pairs with the hand-curated guides at the [repo root](https://github.com/louiskeep/decoy-engine#readme) and the cross-repo orientation in [`decoy-platform/`](https://github.com/louiskeep/decoy-platform).

## What's documented here

- **Public API surface** — everything in `decoy_engine.__init__.__all__`, plus public-ish module symbols elsewhere under `src/decoy_engine/`. Browse via the API reference toctree below.
- **Module structure** — auto-generated module pages with class hierarchies, function signatures, and links back to source on GitHub.

## What's not here

- Anything under `decoy_engine.internal.*`. That subpackage is private per the public-API contract in [`CLAUDE.md`](https://github.com/louiskeep/decoy-engine/blob/main/CLAUDE.md#public-api-rule); names there can change between minor versions without notice. Auto-API skips the whole subpackage by config — see `docs/conf.py` `autoapi_ignore`.
- Hand-written guides describing target state (`*_GUIDE.md`) and architecture decisions (`docs/adr/`). Those are authored, not generated; they live in the repo and are linked below.
- The cross-repo `ROADMAP.md`, `GLOSSARY.md`, and `TAXONOMY_GUIDE.md`. Those live in `decoy-platform/`.

```{toctree}
:maxdepth: 2
:caption: API reference
:hidden:

api/index
```

## Hand-curated companion docs

The auto-generated reference is the *what*; these are the *why* and the *how*. Read in roughly this order if you're new to the engine:

| Doc | What it covers |
|---|---|
| [`SHARED_ENGINE_ARCHITECTURE.md`](https://github.com/louiskeep/decoy-engine/blob/main/SHARED_ENGINE_ARCHITECTURE.md) | The substrate split (DuckDB / Polars / pandas over Arrow), the engine ↔ CLI ↔ platform boundary, the shared-library rationale. |
| [`docs/architecture.md`](https://github.com/louiskeep/decoy-engine/blob/main/docs/architecture.md) | Domain-component map. C4 layer this repo owns: pipeline graph, transforms, generators, `ExecutionContext`. |
| [`PIPELINE_GRAPH_GUIDE.md`](https://github.com/louiskeep/decoy-engine/blob/main/PIPELINE_GRAPH_GUIDE.md) | Graph YAML format, `NATIVE_ENGINE` declaration, op-type registry, how to write a new op. |
| [`STORM_FORECAST_GUIDE.md`](https://github.com/louiskeep/decoy-engine/blob/main/STORM_FORECAST_GUIDE.md) | The analysis (STORM) + recommender (FORECAST) module spec. |
| [`DISGUISES_GUIDE.md`](https://github.com/louiskeep/decoy-engine/blob/main/DISGUISES_GUIDE.md) | Disguise YAML schema, the 8-bundle launch set, the 22 field detectors, name-hint logic. |
| [`CONNECTOR_SDK_CONTRACT.md`](https://github.com/louiskeep/decoy-engine/blob/main/CONNECTOR_SDK_CONTRACT.md) | Third-party connector contract — return `pyarrow.Table`, runner converts at op boundaries. |
| [`POLARS_FOR_PANDAS_USERS.md`](https://github.com/louiskeep/decoy-engine/blob/main/POLARS_FOR_PANDAS_USERS.md) | Contributor cheat sheet for Polars relational ops. |
| [`BENCHMARKING_GUIDE.md`](https://github.com/louiskeep/decoy-engine/blob/main/BENCHMARKING_GUIDE.md) | Perf-regression discipline. Every new op ships with a benchmark. |

## Decisions

[Architecture Decision Records](https://github.com/louiskeep/decoy-engine/tree/main/docs/adr) capture the durable rationale for non-obvious engine choices. Currently:

- **ADR-0001** — Polars + DuckDB hybrid engine substrate. Why we run three engines instead of one, and what each one owns.
- **ADR-0002** — Two key resolvers (`derive_key` and `pipeline_derive_key`) in `ExecutionContext`. Why mask and generate take opposite scope arguments, and why a single-resolver design was rejected.

ADRs are immutable once landed; reversals are recorded as new ADRs that supersede the old one. Format and threshold are in [`docs/adr/template.md`](https://github.com/louiskeep/decoy-engine/blob/main/docs/adr/template.md).

## Guided onboarding tours

If you're new to this codebase, the [CodeTour](https://marketplace.visualstudio.com/items?itemName=vsls-contrib.codetour) extension plays two tours from `.tours/` — open in VS Code:

- [`1-onboarding.tour`](https://github.com/louiskeep/decoy-engine/blob/main/.tours/1-onboarding.tour) — 9-stop walkthrough of the engine's structure: public API, `Logger` Protocol, `ExecutionContext`, the masker, the transforms factory, a representative transform, the graph runner, end-to-end tests.
- [`2-hardest-flow.tour`](https://github.com/louiskeep/decoy-engine/blob/main/.tours/2-hardest-flow.tour) — 9-stop walkthrough of the graph runner's Arrow cache + per-op engine dispatch + bounded-RSS eviction. The hottest path in the engine.

## Cross-repo

The cross-repo glossary (Disguise, STORM, MIRROR, hybrid engine, the in-flight `forge → decoy` rename) lives in [`decoy-platform/GLOSSARY.md`](https://github.com/louiskeep/decoy-platform/blob/main/GLOSSARY.md). The roadmap that says what to build next across all three repos lives in [`decoy-platform/ROADMAP.md`](https://github.com/louiskeep/decoy-platform/blob/main/ROADMAP.md).

The CLI's onboarding tour is in [`decoy/.tours/1-onboarding.tour`](https://github.com/louiskeep/decoy/blob/main/.tours/1-onboarding.tour). The platform's is in [`decoy-platform/.tours/1-onboarding.tour`](https://github.com/louiskeep/decoy-platform/blob/main/.tours/1-onboarding.tour).
