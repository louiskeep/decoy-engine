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
| 3. Polars relational ops | shipped | (this branch) |
| 4. DuckDB source/sink + `engine: hybrid` flag | shipped | (this branch) |
| 5. Preview path + error translation | shipped | (this branch) |
| 6. Parity test suite + dogfood review | shipped | (this branch) |
| 7. Docs + Polars cheat sheet | shipped | (this branch) |
| 8. Default flip + cleanup | shipped | (this branch) |

## Notes

- The implementation plan referenced `pandas-query` translation and a `_legacy/` directory of frozen pandas ops for parity tests. I'm taking a lighter approach: each ported op keeps a pandas fallback path inside the same module guarded by `NATIVE_ENGINE` resolution at the runner. This keeps the diff tighter and avoids duplicating op registration.
- Phase 1's STORM benchmark is informational. Per the plan, if Arrow→pandas overhead is ≥ 10%, declare `NATIVE_ENGINE = "arrow"` for STORM. The benchmark records the number; the decision goes in the commit message.
- Phase 4's `engine: hybrid` flag is the dogfood mechanism. Default stays `engine: pandas` until Phase 8 to keep the cutover safe.

## Phase 8 result

- `_resolve_engine_mode` default flipped from `"pandas"` → `"hybrid"`. Graphs without an explicit `engine:` key now run through the polars / duckdb ops by default.
- `engine: pandas` remains a valid opt-out for one release cycle (per the plan's "hard cutover safety net"). The pandas fallback paths inside each ported op stay alive through that window.
- 2 new tests confirm the flip: `test_engine_default_is_hybrid_after_phase_8` (default resolves to "hybrid"), `test_engine_pandas_opt_out_still_works` (the safety hatch still works).
- CLAUDE.md updated to reference the new guides (CONNECTOR_SDK_CONTRACT, POLARS_FOR_PANDAS_USERS) and mention the hybrid substrate in the SHARED_ENGINE_ARCHITECTURE bullet.
- 487 passing total. Same 8 pre-existing failures unchanged.

### Out of scope for this branch (post-Phase-8 follow-ups)

- Delete `_apply_pandas` from each ported op once the safety-hatch window closes (Phase 9).
- `chunked_iterator()` cleanup in `csv_connector.py:111-126` — depends on the platform-side wrapper code that still uses it.
- README.md tier-number update — sales/marketing follow-up, not blocking.
- Calibration benchmark on a 32 GB box with a 50M-row pipeline — deferred to a manual harness run when real-shape customer data is available.

## Phase 7 result

- `SHARED_ENGINE_ARCHITECTURE.md` gets a new "Hybrid Engine Substrate" section with the three-engine boundary diagram, the per-op engine table, and the `engine: hybrid` opt-in flag explanation.
- New `POLARS_FOR_PANDAS_USERS.md` cheat sheet at engine root: top-20 idiom translations, the `.map_elements()` footgun with three patterns (rewrite / declare pandas / OK case), lazy vs eager mental model, our workload's specific engine assignments.
- `CONNECTOR_SDK_CONTRACT.md` already shipped in Phase 2; the full SDK guide (external-author tutorial) is a follow-up since it depends on the customer SDK from Roadmap Item 24 which isn't in this branch's scope.
- `forge-platform/plans/2026-05-07-etl-direction-and-connector-sdk.md` update is out of scope — different repo, would conflict with the parallel Sprint C work. Tracked as a Sprint C+1 follow-up.
- 485 passing (no test churn — docs only).

## Phase 6 result

- 14 new edge-case parity tests covering empty input / single-row / all-null / unicode / duplicate-laden fixtures + source.file edge cases (empty CSV, unicode data).
- Total parity matrix: **56 tests** across 11 ops × 7 fixture shapes.
- Dogfood review document committed at `plans/2026-05-10-hybrid-engine-dogfood-review.md`. Recommendation: PROCEED to Phase 8.
- 485 passing; 8 pre-existing pandas-3.0 failures unchanged.

## Phase 5 result

- New `decoy_engine.graph.errors` module with `translate(exc, op_kind, node_id)` that maps polars / duckdb exception shapes to user-friendly `OpError` messages. Polars / duckdb imports are lazy — pandas-only installs don't pay the cost.
- Runner wraps every op exception (in both `run_graph` and `preview_graph`) through `translate_engine_error`. NodeRunRecord and PreviewResult.error now carry the friendly message instead of the raw traceback class.
- Preview-boundary serialization (Arrow → pandas → list-of-lists) was already in place from Phase 1's runner refactor; Phase 5 verifies it via tests asserting identical output for the same pipeline run on `engine: pandas` vs `engine: hybrid`.
- 13 new tests: 7 translator unit tests (polars ColumnNotFoundError, ComputeError, duckdb CatalogException, OpError pass-through, unknown-exception fallback, end-to-end runner) + 6 preview-identity tests. 471 passing total.

## Phase 4 result

- Four source/sink ops ported to DuckDB: `source.file`, `source.db`, `target.file`, `target.db`. All declare `NATIVE_ENGINE = "duckdb"`. Pandas fallback path retained.
- `source.file` reads CSV / parquet via DuckDB (`read_csv_auto` / `read_parquet`); LIMIT pushdown for preview mode.
- `target.file` writes via `COPY ... TO` with FORMAT CSV / PARQUET — streaming write, no pandas materialization for the common case.
- `source.db` / `target.db` use SQLAlchemy + Arrow conversion: cleaner test path than postgres_scanner extension fetch, and the connector contract (return Arrow) is met. Native DuckDB scanners are a Phase 4.5 follow-up.
- The runner stashes `__engine` in node config so source ops (no upstream input to dispatch on) know which path to take.
- 9 new parity tests + 1 integration test ("three engines in one pipeline" — DuckDB at I/O, Polars in middle, pandas for mask). 458 passing total.

## Phase 3 result

- Seven relational ops ported to Polars: `filter`, `sort`, `dedupe`, `derive`, `drop_column`, `select_column`, `limit`. Each op now declares `NATIVE_ENGINE = "polars"` and has both `_apply_pandas` (legacy) and `_apply_polars` (new) impls dispatched on input type. The pandas paths stay alive through Phase 7 to keep the safety hatch under `engine: pandas`.
- Filter + derive use `pl.SQLContext` to evaluate predicate / expression strings — same shape pandas-query / pandas-eval supports for the cases we use. Documented divergences in `tests/parity/SEMANTIC_DIFFERENCES.md` (5 rows so far).
- The `engine: hybrid` opt-in YAML key is wired earlier than the architecture plan called for (it shipped in Phase 4 originally) so the polars implementations are exercisable in tests without further plumbing. Validator rejects unknown engine values cleanly.
- 26 new tests: 20 parity (across all 7 ops) + 6 integration (runner-level hybrid mode). 448 passing, same 8 pre-existing failures.

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
