# Living documentation — `decoy-engine` stub

> **Status:** planning
> **Branch:** `claude/living-documentation-plan-WxN92`
> **References:** [`decoy-platform/plans/2026-05-12-living-documentation.md`](https://github.com/louiskeep/decoy-platform/blob/claude/living-documentation-plan-WxN92/plans/2026-05-12-living-documentation.md) (master), [`docs/index.md`](../docs/index.md), [`docs/architecture.md`](../docs/architecture.md), [`SHARED_ENGINE_ARCHITECTURE.md`](../SHARED_ENGINE_ARCHITECTURE.md), [`CAPABILITIES_GUIDE.md`](../CAPABILITIES_GUIDE.md), [`CONNECTOR_SDK_CONTRACT.md`](../CONNECTOR_SDK_CONTRACT.md), [`CONNECTOR_SDK_GUIDE.md`](../CONNECTOR_SDK_GUIDE.md), [`PIPELINE_GRAPH_GUIDE.md`](../PIPELINE_GRAPH_GUIDE.md), [`STORM_FORECAST_GUIDE.md`](../STORM_FORECAST_GUIDE.md), [`DISGUISES_GUIDE.md`](../DISGUISES_GUIDE.md)
> **Last reviewed:** 2026-05-12

## Why this stub exists

The cross-repo plan lives in `decoy-platform/`. This file records what the engine repo owes to that plan so a contributor working here can find it without crossing repos.

## What `decoy-engine` already provides (the head start)

- **Sphinx + autoapi site** at `docs/` (`conf.py`, `index.md`, `Makefile`, `_static/`) regenerated on every push to `main` via `.github/workflows/docs.yml`. This is the canonical engine API reference. The master plan **links to it from Mintlify rather than duplicating.**
- **Two CodeTour onboarding walkthroughs** in `.tours/` — `1-onboarding.tour` (9 stops through the public API → context → masker → transforms → graph runner) and `2-hardest-flow.tour` (the graph runner's Arrow cache + per-op engine dispatch).
- **ADRs** in `docs/adr/` — currently 0001 (Polars+DuckDB hybrid) and 0002 (two key resolvers).
- **Architecture map** at `docs/architecture.md` with mermaid C4 diagrams.
- **Durable guides** at repo root: `SHARED_ENGINE_ARCHITECTURE.md`, `CAPABILITIES_GUIDE.md`, `CONNECTOR_SDK_CONTRACT.md`, `CONNECTOR_SDK_GUIDE.md`, `PIPELINE_GRAPH_GUIDE.md`, `STORM_FORECAST_GUIDE.md`, `DISGUISES_GUIDE.md`, `POLARS_FOR_PANDAS_USERS.md`, `BENCHMARKING_GUIDE.md` — each with `Status:` + `Last reviewed:` headers.

Phase B reuses all of the above. The plan adds: JSON Schema export, Mintlify entry points that *link* into Sphinx, and the Disguise auto-render pipeline.

## What `decoy-engine` owes the plan

### Phase B (generated reference)

- **B3. Pipeline YAML JSON Schema export.** New script under `scripts/build_yaml_schema.py`. Output:
  - `decoy-engine/docs/_static/pipeline-yaml.schema.json` — committed alongside the Sphinx build so it's IDE-discoverable.
  - `decoy-web/docs/cli/reference/pipeline-yaml.mdx` — mdx with collapsible field documentation. Pre-commit hook regenerates when the Pydantic models under `src/decoy_engine/` change.
  - Source: the existing `PipelineConfig` Pydantic model surfaces `model_json_schema()` for free. The script reshapes it into mdx — grouped by section, with examples cribbed from `examples/` in `decoy/`.
- **B4. Cross-link from Mintlify into Sphinx.** No engine-side work; `decoy-web/docs/engine/public-api.mdx` is a curated landing page that links into specific Sphinx pages (Masker, DataGenerator, run_graph, ExecutionContext, the Logger Protocol). Anchor stability matters — don't rename module sections without coordinating.

### Phase D (Disguise pages)

- **D1. Per-Disguise auto-render.** New script under `scripts/build_disguise_pages.py`. Walks `src/decoy_engine/disguises/*.yaml`, extracts the bundle metadata + per-field rules + STORM/FORECAST match weights, emits one mdx per Disguise to `decoy-web/docs/disguises/`. The hand-written intro paragraph for each Disguise lives in a sibling `.intro.md` file in `decoy-web/` so engine changes never overwrite marketing prose.
- Lock-step with `decoy-platform/ROADMAP.md` Item 31 phases as new Disguises ship.

### Phase F (living loop)

- **F1 freshness check** scans this repo's `*_GUIDE.md` headers along with sibling repos. Engine-side guides (SHARED_ENGINE_ARCHITECTURE, CAPABILITIES, CONNECTOR_SDK_*, PIPELINE_GRAPH, STORM_FORECAST, DISGUISES) carry `Status: target` / `partial`; the script triggers issues on stale `Last reviewed:`.
- **F4 PR template**: this repo's `.github/pull_request_template.md` (today: minimal) gains the "Docs updated?" checkbox + a reminder to update `__all__` if changing the public API surface.
- **F5 `dev-help.md` section**: new section listing this repo's doc surfaces — Sphinx (`docs/`), the eight `*_GUIDE.md` files, ADRs, and the CodeTour walkthroughs.

## What stays in-repo, not on Mintlify

Per the master plan's stay-in-repo policy:
- [`CLAUDE.md`](../CLAUDE.md), [`dev-help.md`](../dev-help.md), [`CONTRIBUTING.md`](../CONTRIBUTING.md), [`SECURITY.md`](../SECURITY.md), [`LICENSE`](../LICENSE), [`NOTICE`](../NOTICE), [`TRADEMARKS.md`](../TRADEMARKS.md)
- [`docs/architecture.md`](../docs/architecture.md) + `docs/adr/`
- All `*_GUIDE.md` files at repo root (durable specs; Mintlify excerpts and links rather than duplicating)
- [`POLARS_FOR_PANDAS_USERS.md`](../POLARS_FOR_PANDAS_USERS.md) (contributor-only — Polars onboarding for new contributors)
- [`BENCHMARKING_GUIDE.md`](../BENCHMARKING_GUIDE.md) (contributor-only — per-op benchmark convention)
- `plans/*.md`
- `.tours/*.tour` (VS Code CodeTour, contributor onboarding)
- The Sphinx output under `docs/` — this is the canonical engine API ref. Mintlify links into it; it's not "in-repo only" the same way `CLAUDE.md` is, but its source lives here and its build is owned here.

## What gets new content from this repo to Mintlify

- `engine/overview.mdx` — curated landing page summarizing the engine's role; excerpts `SHARED_ENGINE_ARCHITECTURE.md` §TL;DR + §The Three-Layer Architecture
- `engine/public-api.mdx` — links into Sphinx (B4)
- `engine/execution-context.mdx` — excerpts `context.py` docstrings + the Logger Protocol pattern from `SHARED_ENGINE_ARCHITECTURE.md`
- `engine/transforms/` — list of the 14 transforms with one-paragraph descriptions, links to Sphinx for signatures
- `engine/connectors/` — Connector SDK landing; excerpts `CONNECTOR_SDK_CONTRACT.md` + `CONNECTOR_SDK_GUIDE.md`
- `engine/advanced/pipeline-graph.mdx` — excerpts `PIPELINE_GRAPH_GUIDE.md`
- `engine/advanced/hybrid-engine.mdx` — excerpts `SHARED_ENGINE_ARCHITECTURE.md` §Hybrid Engine Substrate
- `engine/advanced/ml-detection.mdx` — excerpts the ML Inference Boundary section
- `cli/reference/pipeline-yaml.mdx` — autogenerated (B3)
- `disguises/*.mdx` — autogenerated (D1)

## Open questions specific to this repo

- **Sphinx site URL.** Currently a GitHub-Pages build off `gh-pages` (per `docs.yml`). Mintlify lives at `decoy.dev/docs`. Two options for the engine API ref: keep the Sphinx site at e.g. `engine.decoy.dev` and link from Mintlify, or move Sphinx behind `decoy.dev/docs/engine/api/`. Recommendation: keep separate — Sphinx output is best-in-class for Python API ref and Mintlify embeds it via iframe or link. Reconfirm with the master-plan owner before Phase B4 lands.
- **CodeTour discoverability.** `.tours/` is great for contributors who install the VS Code extension. Should we also render the tour scripts as a Mintlify page so non-VS Code contributors can read them? Recommendation: defer; the tours are contributor-only and aren't worth the maintenance cost on Mintlify.
- **ADR cross-repo home.** ADRs live in each repo's `docs/adr/`. Master plan says "linked from `concepts/`, not duplicated." Confirm the link strategy before Phase D ships.
- **`forge` → `decoy` rename leaks.** `SHARED_ENGINE_ARCHITECTURE.md` still uses `forge` / `forge-platform` in places (visible in the doc itself: §Migration Path, §Repo Structure). Phase A4's extension of the `TAXONOMY_GUIDE.md` grep gate to `decoy-web/docs/` should catch any leak into Mintlify; the in-repo cleanup is tracked separately.
