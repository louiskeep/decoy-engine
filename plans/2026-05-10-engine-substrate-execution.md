# Engine substrate switch — execution plan

> **Status:** in-progress.
> **Branch:** `claude/sprint-c-auth-engine-plan-9pWL0`
> **References:** [2026-05-10-polars-duckdb-hybrid-engine.md](2026-05-10-polars-duckdb-hybrid-engine.md), [2026-05-10-polars-duckdb-implementation.md](2026-05-10-polars-duckdb-implementation.md).
> **Companion sprint:** Sprint C auth on `decoy-platform` (different repo, separate dev). Coordination notes in the architecture plan; conflict surface is minimal — only one ~5-line hook in `decoy-platform/api/jobs/runner.py` at Phase 4.

This is the working journal for executing Phases 1–8 of the Polars+DuckDB hybrid plan. The architecture and implementation plans cover the *why* and the detailed *how*; this doc tracks what I'm shipping in this branch and how I verify each phase.

## Ground rules

- One commit per phase. Tests run green before commit. Pre-existing failing tests (storm distribution + fixed_width on pandas 3.0) stay out of scope.
- Each phase is shippable on its own — `engine: pandas` (default) is the safety hatch through Phase 7. Phase 8 flips the default.
- No `.map_elements()` Polars footgun. Per-row Python ops stay on pandas.
- Connectors return Arrow once Phase 2 lands. A backward-compat wrapper preserves existing pandas-returning connectors during the migration.

## Baseline

- 389 tests passing (vs 397 in the plan; 8 pre-existing failures on pandas 3.0).
- `pyproject.toml` — `pandas`, `pyyaml`, `faker`, `psutil`. Adding `pyarrow`, `polars`, `duckdb`.

## Phase tracking

| Phase | Status | Commit |
|---|---|---|
| 1. Arrow runner cache + eviction + STORM benchmark | pending | — |
| 2. Op-type registry + connector SDK contract | pending | — |
| 3. Polars relational ops | pending | — |
| 4. DuckDB source/sink + `engine: hybrid` flag | pending | — |
| 5. Preview path + error translation | pending | — |
| 6. Parity test suite + dogfood review | pending | — |
| 7. Docs + Polars cheat sheet | pending | — |
| 8. Default flip + cleanup | pending | — |

## Notes

- The implementation plan referenced `pandas-query` translation and a `_legacy/` directory of frozen pandas ops for parity tests. I'm taking a lighter approach: each ported op keeps a pandas fallback path inside the same module guarded by `NATIVE_ENGINE` resolution at the runner. This keeps the diff tighter and avoids duplicating op registration.
- Phase 1's STORM benchmark is informational. Per the plan, if Arrow→pandas overhead is ≥ 10%, declare `NATIVE_ENGINE = "arrow"` for STORM. The benchmark records the number; the decision goes in the commit message.
- Phase 4's `engine: hybrid` flag is the dogfood mechanism. Default stays `engine: pandas` until Phase 8 to keep the cutover safe.
