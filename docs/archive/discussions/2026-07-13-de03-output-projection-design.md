# DE-03 — fail-closed output projection: design

Design pass output (2026-07-13), verified read-only against source. Operationalizes the prior independent finding in `docs/adversarial-architecture-review-2026-07-12.md` §DE-03. Drives Sprint 2 of the crypto/DE-11 delivery.

## Bug (confirmed, worse than first stated)
Columns with no strategy are dropped from the seed envelope (`plan/_seed_envelope.py:99-102`) and the work list is built only from the envelope → raw data reaches output with no warning. **Also:** a table with `columns: []` or an omitted `tables:` entry gets no `TableSeed` (`:303-304`) → the *whole table* passes through raw, silently. Categories (c) known-but-unstrategied and (d) runtime schema-drift collapse to the same runtime behavior, so enforcement must live at the adapter (compile can't see (d)).

## Enforcement design (fixed regardless of the policy fork)
New shared module `execution/_output_projection.py`:
- `known_output_columns(plan, table)` = `per_column` keys ∪ composite-FK `per_group` coherent_columns. A table absent from the envelope → empty set (fail-closed by construction).
- `enforce_output_projection(table, output_columns, plan)` → raise `ExecutionError(undeclared_output_columns)` if `output_columns − known` is non-empty.

Wire into all **five** emission routes, each before its point-of-no-return:
1. `execution/_pandas_adapter.py::run()` before `outputs = {...}` (~:258)
2. `execution/polars/_polars_adapter.py::_run_polars_native` before `outputs = {...}` (~:230) (pandas-oracle path covered transitively)
3. `execution/_sequential.py::run_sequential` after `pa.Table.from_pandas` (~:424), before sink write
4. `execution/out_of_core/_runner.py::_fixed_output_schema` (~:379-403) — earliest/cheapest, fails before any batch streams
5. `execution/_chunked.py::run_mask_pipeline_chunked` — per-chunk name-set check (also catches mid-stream drift)

Isolated/governed subprocess path inherits the fix (it wraps one of these adapters).

### Generate-echo conflict + resolution (tech-lead call, 2026-07-13)
Build surfaced: in mixed generate+mask configs, `run_pipeline` (`_pipeline.py:450-457,477`) merges the generate table's real data into `sources` and the full-frame adapters (pandas `:258`, polars-native `:230`) echo it into `outputs`. Generate tables have no `TableSeed`, so `known_output_columns` = empty set → the two full-frame wiring points would false-positive on every generated column. (The 3 streaming routes are generate-incompatible, so unaffected.)

Resolution — keep the guard **adapter-internal and bypass-resistant** (do NOT move it to the orchestration layer, which a direct adapter call could bypass). Preference order:
1. **Preferred — extend `known_output_columns`** to "all columns the plan DECLARES as legitimate output": mask table = `per_column ∪ per_group` coherent_columns; **generate table = its `generate_columns`**. Uniform across all 5 routes, correct-by-construction, no signature change — *if* the `plan` the adapters receive carries generate-table declarations.
2. **Fallback — plumb the minimal generate-table-name set** into the 2 full-frame adapters' `run()` so they exempt generate-echo tables (only if the plan doesn't already carry generate declarations).
3. **Last resort — run_pipeline-level enforcement** for the 2 full-frame routes, only if 1 and 2 are infeasible (note: bypassable by direct adapter calls).
Invariant preserved in all options: a genuinely-undeclared mask column, or a whole mask table with empty `columns:`, still fails closed.

## `strategy: passthrough` already exists
First-class strategy (`_strategies/_passthrough.py`), and it's the **documented** pattern (`docs/quickstart.md` declares non-PII `account_status` as `strategy: passthrough`). So "declare intent for every column" is already the documented contract — Option B below is not a new concept.

## `no_profile` mode — no special-casing
The engine never compiles against zero schema info: `no_profile=True` only skips distinct-count checks; it still profiles the real first-chunk schema. `known_output_columns` is well-defined identically in both modes.

## Sibling fix (faker-without-provider)
`_seed_envelope.py:101-102` + `_checks.py:79-81` silently drop a declared `faker` column with no `provider`. Add a dedicated compile check `check_faker_requires_provider` → `PlanCompileError` (primary fix, free at compile; runtime projection is the backstop), following the Sprint-13 honesty-pack precedent.

## residual_pii — orthogonal, keep unmerged
`storm/postmask/residual_pii` is an optional, probabilistic, POST-publication content-heuristic advisory. The projection is the mandatory, deterministic, PRE-publication schema-closure gate. Two layers; do not merge; projection must not call into residual_pii.

## THE FORK (Cam's decision) — category (c): known column, no strategy
- **Option A — permissive:** only hard-error on true drift (d); a known-but-unstrategied column keeps passing through raw (today's behavior). Zero blast radius; leaves half the DE-03 hole open.
- **Option B — fail-closed (recommended):** every column must appear with a real strategy or `strategy: passthrough`; anything else errors. Closes (c)+(d) uniformly, matches the documented quickstart contract and the adversarial review. Blast radius: ~76 unit + 14 integration + 5 parity test files reference `columns:` blocks (44 already use explicit passthrough); known casualty `tests/perf_fixtures/fk_relational.py` (wide undeclared payload columns). Mitigation: `global_settings.unconfigured_column_policy` shipping as `warn` (transitional) → `error` before the pre-GA flip, so nothing breaks day one.

Recommendation: **Option B with the warn→error migration bridge.**

## DECISION (Cam, 2026-07-13): Option B — fail-closed + migration bridge
- Add `global_settings.unconfigured_column_policy: warn | error`.
- **Default couples to release phase:** `warn` while `is_pre_ga()` (migration window — existing configs keep working, undeclared columns log a structured warning and still pass through, so nothing breaks day one), `error` at GA (fail-closed binds automatically, no manual flip to forget). Explicit setting overrides.
- Because pre-GA default is `warn`, the build does NOT rewrite the ~40-90 existing test configs; the `error` path is proven by explicit `policy: error` test cases (drift caught, whole-table caught, known-unstrategied caught). The warn path must emit warnings via the structured warnings channel (NOT stderr — SC7b stdout-contamination lesson) and must not break existing tests that assert on output.
