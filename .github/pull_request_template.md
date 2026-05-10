## Summary

<!-- 1-3 sentences on what changed and why. Link to a roadmap item or plan if applicable. -->

## Test plan

<!-- Bulleted checklist of how this was verified. -->

- [ ] `pytest tests/unit/`
- [ ] `pytest tests/integration/`
- [ ] (substrate-touching) `pytest tests/ -m benchmark` and the relevant smoke benchmark on `main` did not regress

## Docs checklist

<!-- Skip a box only if the change genuinely doesn't touch that surface; never delete the box. -->

- [ ] **`docs/architecture.md`** updated if I changed the engine's domain-component map
- [ ] **ADR added** if I made a non-obvious architectural decision (one that a future contributor might re-litigate). Format and threshold in [`docs/adr/template.md`](../docs/adr/template.md).
- [ ] **Guide updated** if I changed the target state of a feature in a `*_GUIDE.md` (e.g. `SHARED_ENGINE_ARCHITECTURE.md`, `DISGUISES_GUIDE.md`, `STORM_FORECAST_GUIDE.md`, `PIPELINE_GRAPH_GUIDE.md`, `CONNECTOR_SDK_CONTRACT.md`, `POLARS_FOR_PANDAS_USERS.md`, `BENCHMARKING_GUIDE.md`)
- [ ] **CodeTour fixed** if I moved a line that any stop in `.tours/1-onboarding.tour` or `.tours/2-hardest-flow.tour` points at. *A wrong tour is worse than no tour.*
- [ ] **Public API rule** — if I added a new public symbol, it's in `decoy_engine/__init__.py.__all__`. The Sphinx + autoapi build at `.github/workflows/docs.yml` picks it up automatically.
- [ ] **Plan filed under `plans/`** if this PR is more than a one-PR change

## Cross-repo coordination

- [ ] If this PR introduces new product vocabulary, the cross-repo glossary in [`decoy-platform/GLOSSARY.md`](https://github.com/louiskeep/decoy-platform/blob/main/GLOSSARY.md) needs a matching entry.
- [ ] If this PR ships, supersedes, or pivots a numbered roadmap item, the matching status in [`decoy-platform/ROADMAP.md`](https://github.com/louiskeep/decoy-platform/blob/main/ROADMAP.md) is updated.
- [ ] If this PR changes the `Logger` Protocol shape, the platform's [`LOGGING_GUIDE.md`](https://github.com/louiskeep/decoy-platform/blob/main/LOGGING_GUIDE.md) (sections 4 + 5) is the source of truth — coordinate the change there first.
- [ ] If this PR breaks a downstream `decoy` (CLI) or `decoy-platform` callsite, sibling PRs are linked here.
