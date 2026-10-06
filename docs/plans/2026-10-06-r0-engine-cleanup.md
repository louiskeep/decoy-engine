Status: plan (revision 3, author = Opus). Codex plan-gate rounds 1-2 REVISE (MEDIUMs only) folded; round 3 is the final round before owner escalation.
Rules consulted: 00-universal, development-loop, refactoring, testing, code-review, workspace-and-vcs, destructive-operations, api-and-compatibility

# R0 (engine): small cleanups from the 2026-10-06 health report

Source: `decoy-platform docs/records/2026-10-06-codebase-health-and-refactor-report.md` section 2. It is scheduled as the "R0 cleanup, Now" row of the roadmap Refactor track (Cam, 2026-10-06).

Branch: `feat/r0-engine-cleanup`. It is cut from engine main AFTER C5b-i merges, because item 4 reuses C5b-i's `DeriveContext.derive_sources`.

Every item is small and behavior-preserving, except item 1. Item 1 is additive: callers catching `DecoyError` now catch more.

## Design notes

- **Principles applied.** Liskov / interface contract on the exception taxonomy: `DecoyError` documents "base class for all decoy_engine exceptions", but 14 classes violate it. Callers that rely on the documented contract silently miss most runtime failures.
- **Established pattern.** A single library root exception that all public errors inherit from (the `requests.RequestException` / `sqlalchemy.exc.SQLAlchemyError` convention).
- **Where facts live.** The hierarchy lives in each class's own base list. No registry is added.

## Items

1. **Reparent the engine's exception roots under `DecoyError`** (rev 2: full inventory).
   - Codex round 1 found 35 engine-defined exception classes outside `DecoyError`; rev 1 listed only 14. Every one gets an explicit decision, recorded in a table in the build record.
   - **Default: reparent the ROOT of each family** (its subclasses follow): `ValidationError` (`errors.py:54`), `VaultError`, `MaskSecretError` (and with it `KeyedStrategyRequiresSecret`, `MissingMaskSecret`, `WeakMaskSecret`), `ExecutionError` (and `StrategyError`), `TransformError`, `CommitError`, `PlanCompileError` (its existing descendant `NamespaceConfigError`, `relationships/_namespace.py:38`, follows and keeps that relationship), `DeterminismError`, `GenerationError` and, INDEPENDENTLY, `PoolCapacityError` (both inherit `Exception` directly at `generation/pool/_errors.py:12,34`; reparent each to `DecoyError`, without making capacity errors children of `GenerationError`), `CompositeError`, `StatisticalSpecError`, `ProviderError` (and `AdapterError`), `IdentifierError` (and `IdentifierFormatError`), `DpError`, `DpBudgetError`, `DistributionSnapshotError`, `ProvenanceError`, `CarrierError`, `NerUnavailableError`, `NameHintLoaderError`, `ModelPackLoadError`, `PipelineConfigError`, `Ff1Error`, `DrawSiteProtocolError`.
   - **Keep stdlib ancestry alongside `DecoyError`** wherever a class inherits from a stdlib exception today: `PipelineConfigError`, `ModelPackLoadError` and `Ff1Error` keep `ValueError`, and `DrawSiteProtocolError` keeps `RuntimeError`. Use the form `class X(DecoyError, ValueError)` so `except ValueError` callers (which `PipelineConfigError` explicitly promises) keep working.
   - **Do NOT reparent internal control-flow carriers:** `ShadowDifference`, `PoolBuildFailed` (lane-internal signals, never meant to reach a caller) and the private `_InfeasibleAtEpsQError`. Record each with its reason.
   - Constructors, attributes and messages are unchanged. Codex verified that `NativeChunkSchemaDriftError`'s multiple inheritance keeps a valid MRO, that no cross-repo code catches `DecoyError`, that no class map or negative-ancestry check is affected, and that no specific handler is shadowed. The builder re-runs that grep and records it.
   - The pre-existing fact that `ExecutionError` cannot be pickled (keyword-only constructor) is out of scope and recorded as a known issue.
2. **CI.**
   - Add `timeout-minutes` to every `ci.yml` job that lacks one. The value is the job's observed duration plus headroom; the builder reads recent workflow history if available, otherwise uses 60 for `regression-gate` and 20 for the others.
   - Add `--durations=25` to the `regression-gate` pytest invocation.
   - Align the CI ruff pin (`ci.yml:41` 0.15.14) with the `[lint]` extra (`pyproject.toml:199` 0.15.22), and run BOTH `ruff check` and `ruff format --check` under the new version (CI enforces format at `ci.yml:45`). Fix any new findings in this slice.
   - Bump `.pre-commit-config.yaml`'s ruff hook (`:13`, currently v0.5.0) to the same version.
3. **Docs accuracy.**
   - `CLAUDE.md`: "import-linter enforced" becomes "regex sentry enforced (`tests/sentry/test_public_import_boundary.py`)". Replace the `graph/runner.py` threshold reference with the actual census policy. Fix the roadmap link to point at decoy-platform's `docs/ROADMAP.md`.
   - `errors.py`: fix the stale comments naming `decoy_engine.graph.validators.*` (`:62`) and `graph/runner.py` (`:137`).
   - `pyproject.toml`: fix the stale polars/duckdb "hybrid default" comment (polars masking was removed; polars stays for subset).
   - CODEMAP:176: the link actually names the existing `docs/v2/ml/baseline-report.json`. Correct the stale relocation note only; do not move the artifact.
4. **`date_shift` oracle per-row work** (`execution/_strategies/_date_shift.py` ~:150-207), rev 2 with an explicit dtype-preserving recipe. Two changes:
   - **(a) Output assembly** (rev 3 recipe). Today `df[column] = [col.iloc[i] if unusable[i] else formatted.iloc[i] for i ...]` assigns a Python list, and pandas infers the column dtype from the exact scalar objects in it. Two traps:
     - Codex probes: empty and NaN-only inputs become `float64`, None-only stays `object`, NaT-only becomes datetime, nullable-string NA becomes `object`. A direct `np.where` assignment yields `object`.
     - Round 2: boxing the source through `to_numpy(dtype=object)` turns `np.float32` / `np.float16` NaN into Python floats, so an all-null float32 column comes out `float64`.

     Recipe: `out = formatted.to_numpy(dtype=object).tolist()` (formatted values are already Python `str`, the same objects `formatted.iloc[i]` returns). Then for each index in `np.flatnonzero(unusable)`, set `out[i] = col.iloc[i]`. That is the SAME expression the original uses, so every unusable cell keeps its exact original scalar type. Assign `df[column] = out`.

     The per-row `iloc` cost remains only for unusable rows (usually few). An all-unusable column costs what it does today, but stays byte- and dtype-identical. The builder confirms `formatted.iloc[i]` and `formatted.to_numpy(dtype=object)[i]` are the same type for usable rows in both `fmt` branches (`strftime` and `astype(str)`).
   - **(b) The per-row `derive(...)` loop.** Batch it with `DeriveContext.for_column(ctx.mask_key, plan.namespace).derive_sources(plan.namespace, canonical_sources_of_usable_rows)`.
     - Keep the same key, namespace, canonical bytes (including the `.item()` normalization at ~:182) and the same set and order of usable rows.
     - Canonicalize only usable rows, exactly as today.
     - Keep the null-anchor self-anchor fallback.
   - The row-error loop stays as is.
   - Output must be byte-identical: values, dtype, row errors and their order. C4's chunked tests pin Arrow string output and could hide a pandas dtype drift, so item 4's acceptance compares the pandas frame directly (below), and the native date_shift tests must run without skips (companion venv).
5. **Size-cap policy (E8).** `tests/sentry/test_module_size.py`:
   - The module docstring already permits dense exceptions (`:17`). Reword it so "a split would be artificial" is an explicit, named justification, and keep the exact-count and legacy shrink-only rules unchanged.
   - Move the long per-file growth history comments into `docs/decisions/` ADR-0005 (or a dated appendix the ADR links). Keep one-line rationales in the census.
   - No ceiling values change in this slice.
6. **Prune merged branches and worktrees** (no code). Delete local branches fully merged into `origin/main`, and remove worktrees whose branch is merged AND whose working tree is clean, with NO untracked files (`git status --porcelain --untracked-files=all` empty). Skip:
   - the current branch and `main`
   - any protected branch
   - detached-HEAD and locked worktrees
   - any worktree git refuses to remove without force

   Use ordinary `git worktree remove` and `git branch -d` (never `-D` or `--force`).
   - NEVER touch the parked OOC-B worktrees or branches (`ooc-b`, `fix/ooc-b-*`, `feat/ooc-b-*`; they hold unpushed commits by design) or any worktree with uncommitted changes.
   - List what was removed in the build record.
   - This is done by the orchestrator, not the builder.

## Acceptance tests (written first)

1. **Hierarchy.**
   - A parametrized test asserts every reparented root is a subclass of `DecoyError`, including `PoolCapacityError` independently.
   - `PoolCapacityError` is NOT a subclass of `GenerationError`, and `NamespaceConfigError` IS still a subclass of `PlanCompileError`: both relationships are asserted.
   - The stdlib-ancestry classes are still subclasses of `ValueError`/`RuntimeError`.
   - A sentry walks `decoy_engine` modules and fails if any ENGINE-DEFINED public exception class does not inherit `DecoyError`, unless it is in an explicit allowlist with reasons (the control-flow carriers).
   - Optional-extra modules (`storm`, `quality/dp*`) are covered with `importorskip`, and the sentry records which modules were skipped.
   - Existing constructor/attribute tests are unchanged.
2. **`date_shift` byte identity.** A differential test runs the old implementation (a test-local reference copy) against the new one and compares the pandas FRAME directly (`pd.testing.assert_frame_equal(check_dtype=True)`, plus a per-cell type/identity check for sentinels such as None vs NaN vs NaT vs pd.NA). Cases, each separately:
   - empty
   - NaN-only, None-only, NaT-only, nullable-string NA-only
   - explicitly typed all-null `float16`, `float32` and `float64` columns, plus each of those with unparseable values mixed in (round 2: the narrow-float case)
   - mixed valid/unparseable/null
   - a numpy bool/int anchor versus a nullable anchor
   - large integers
   - a null-anchor fallback
   - a duplicate and a non-default index
   - multiple ordered row errors

   Row errors must match in value and order. The existing date_shift oracle, native parity (no skips, companion venv) and chunked tests pass unchanged.
3. **CI.** A sentry asserts every `ci.yml` job has `timeout-minutes` and that the CI ruff pin equals the `[lint]` extra's.
4. **Docs.** A grep gate in the build record shows "import-linter" and `graph/runner.py` no longer appear in CLAUDE.md or `errors.py`.

Mutants:
- Revert one class's base.
- Drop the `derive_sources` batching in favor of a wrong namespace.
- Restore the per-row scalar `derive` loop. Killed by a call-count spy asserting exactly one `DeriveContext.for_column` and one `derive_sources` call per column, so a regression to expensive per-row derivation is caught.
- Assign the `np.where` result directly instead of `.tolist()` (killed by the dtype cases).
- Fill unusable rows from `col.to_numpy(dtype=object)` instead of `col.iloc[i]` (killed by the float16/float32 all-null cases).
- Use `formatted` for unusable rows.

## Risk, gates

- **Risk: R1-R2.** Item 1 widens what `except DecoyError` catches, and no production consumer catches it today.
- **Gates:** Codex plan gate → Sonnet build → dennis → Codex final → ci-mirror → merge under the standing authority.

## Plan-gate history

- Round 1 (Codex, gpt-6-astra): REVISE, 0 BLOCKER / 2 MEDIUM / 2 LOW, all folded in rev 2.
  - MEDIUM: incomplete exception inventory (35 classes, not 14) → explicit per-root decisions, stdlib ancestry kept, control-flow carriers excluded.
  - MEDIUM: the date_shift vectorization would change dtype inference → `.tolist()` recipe, per-sentinel frame-level tests, a call-count mutant.
  - LOW: ruff format check, pre-commit pin, CODEMAP path, size-policy wording → folded.
  - LOW: pruning safeguards → folded.
- Round 2 (Codex): REVISE, 2 MEDIUM; both round-1 LOWs CLOSED. Folded in rev 3.
  - Inventory: `PoolCapacityError` reparented independently; the `NamespaceConfigError` → `PlanCompileError` relationship preserved and asserted.
  - dtype: unusable rows take `col.iloc[i]` exactly as today (narrow fallback), with float16/float32 tests and a mutant.
