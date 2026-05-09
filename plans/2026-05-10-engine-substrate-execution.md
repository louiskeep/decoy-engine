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
| 1. Arrow runner cache + eviction + STORM benchmark | shipped | (this branch) |
| 2. Op-type registry + connector SDK contract | shipped | (this branch) |
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

## Phase 2 result

- Every existing op declares `NATIVE_ENGINE = "pandas"`. No behavior change — the runner still resolves to pandas mode by default.
- `CONNECTOR_SDK_CONTRACT.md` committed at engine root: connectors return Arrow, accept Arrow; pandas-returning connectors keep working via runtime wrapper through Phase 7.
- 5 new tests covering: declaration presence, valid-value check, mode resolution, frozen Phase-2 baseline, unknown-kind fallback.
- Total: 422 passing (+19 from Phase 1; same 8 pre-existing failures).

## Phase 1 result

- **STORM benchmark on 50K-row HIPAA-shaped fixture: 2.4% overhead.** Well below the 10% threshold. Decision per the plan: STORM stays `NATIVE_ENGINE = "pandas"` in Phase 2.
- Tests: 14 new (13 cache + 1 benchmark), all passing. Existing suite unchanged (389 → 389 passing of pre-existing tests; 8 pre-existing failures untouched).
- Files added: `src/decoy_engine/graph/conversion.py`, `src/decoy_engine/graph/registry.py`, `tests/unit/test_graph_runner_cache.py`, `tests/benchmark/test_storm_arrow_boundary.py`.
- Files touched: `src/decoy_engine/graph/runner.py` (cache → `dict[str, pyarrow.Table]`, eager eviction, preview pin), `pyproject.toml` (add `pyarrow`, `hybrid` extra).
