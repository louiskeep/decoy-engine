# R0 engine cleanup: build record

Status: record
Plan: `docs/plans/2026-10-06-r0-engine-cleanup.md` rev 3. Branch `feat/r0-engine-cleanup`, cut from engine main 01c560da. Item 6 (branch and worktree pruning) belongs to the orchestrator and is not part of this build.

## Commits

| Item | Commit |
|---|---|
| 1 exception roots | 6dc536f6 |
| 2 CI | 64a4f119 |
| 3 docs accuracy | 1e71d244 |
| 5 size-cap policy | be52d46b |
| 4 date_shift | e725cb06 |

## Red before, green after

Tests were written first and run against the unmodified source.

| Test file | Red before | Green after |
|---|---|---|
| `tests/sentry/test_exception_hierarchy.py` (37 tests) | 35 failed, 2 passed (the allowlist-carriers check, because the carriers were never reparented, and the `NativeChunkSchemaDriftError` two-parent check, which already held) | 37 passed |
| `tests/sentry/test_ci_config.py` (3 tests) | 3 failed | 3 passed |
| `tests/unit/execution/test_date_shift_oracle_identity.py` (41 tests) | 2 failed (the call-count spy tests), 39 passed | 41 passed |

The 39 differential tests pass before the change by design: they compare the old implementation with itself until item 4 lands, then pin the new one to it. The two spy tests are the red-before proof for the batching.

## Exception decision table (item 1)

Reparented to `DecoyError` (subclasses follow their root):

| Root | Module | Form |
|---|---|---|
| `ValidationError` | `errors.py` | `DecoyError` |
| `VaultError` | `vault.py` | `DecoyError` |
| `MaskSecretError` (+ `KeyedStrategyRequiresSecret`, `MissingMaskSecret`, `WeakMaskSecret`) | `keyprovider.py` | `DecoyError` |
| `ExecutionError` (+ `StrategyError`, `NativeChunkSchemaDriftError`) | `execution/_errors.py` | `DecoyError` |
| `TransformError` | `execution/_transforms.py` | `DecoyError` |
| `CommitError` | `execution/_isolated_commit.py` | `DecoyError` |
| `PlanCompileError` (+ `NamespaceConfigError`) | `plan/_errors.py` | `DecoyError` |
| `DeterminismError` | `determinism/_derive.py` | `DecoyError` |
| `GenerationError` | `generation/pool/_errors.py` | `DecoyError` |
| `PoolCapacityError` | `generation/pool/_errors.py` | `DecoyError`, independent of `GenerationError` |
| `CompositeError` | `generation/composite/_errors.py` | `DecoyError` |
| `StatisticalSpecError` | `generation/statistical/_spec.py` | `DecoyError` |
| `ProviderError` (+ `AdapterError`) | `providers_v2/_errors.py` | `DecoyError` |
| `IdentifierError` (+ `IdentifierFormatError`) | `providers_v2/identifiers/_errors.py` | `DecoyError` |
| `DpError` | `quality/dp.py` | `DecoyError` |
| `DpBudgetError` | `quality/dp_budget.py` | `DecoyError` |
| `DistributionSnapshotError` | `quality/snapshot.py` | `DecoyError` |
| `ProvenanceError` | `quality/dp_provenance.py` | `DecoyError` |
| `CarrierError` | `quality/carriers.py` | `DecoyError` |
| `NerUnavailableError` | `storm/ner.py` | `DecoyError` |
| `NameHintLoaderError` | `storm/name_hints/loader.py` | `DecoyError` |
| `ModelPackLoadError` | `storm/model_pack/loader.py` | `DecoyError, ValueError` |
| `PipelineConfigError` | `config/_errors.py` | `DecoyError, ValueError` |
| `Ff1Error` | `transforms/_ff1.py` | `DecoyError, ValueError` |
| `DrawSiteProtocolError` | `execution/native/_draw_site_providers.py` | `DecoyError, RuntimeError` |

Left alone, on purpose:

| Class | Reason |
|---|---|
| `ShadowDifference` | Shadow-lane internal signal, caught inside the lane. |
| `PoolBuildFailed` | Shadow-lane internal carrier, same reason. |
| `_InfeasibleAtEpsQError` | Private search-loop signal inside `dp_budget`. |

Classes that already inherited `DecoyError` (`ConfigError` family, `Subset*`, `FKPreservationError` family, and so on) were not touched. `NamespaceConfigError` still descends from `PlanCompileError`, asserted by a test. The sentry walks every engine module with `pkgutil.walk_packages` and fails on any engine-defined exception class outside `DecoyError` that is not allowlisted. In this environment the walk skipped `decoy_engine.providers_v2.mimesis` and `decoy_engine.storm.model_pack.trainer` (optional extras absent); neither defines an exception class (checked with an AST scan of `src/`).

Handler grep: no `except DecoyError` exists under `src/`, and no handler orders a specific class after a now-broader one. The widened catch only changes behavior for external callers. `tests/native/test_chunked_entry_evidence.py` already asserts `NativeChunkSchemaDriftError` is caught by `except DecoyError` and still passes.

Known issue, out of scope: `ExecutionError` cannot be pickled because its constructor is keyword-only.

## date_shift (item 4)

Differential test: frozen copy of the old handler against the new one, compared with `pd.testing.assert_frame_equal(check_dtype=True)`, a cell-by-cell type and sentinel identity check, and row-error equality in order. Cases: empty (object and float), NaN-only, None-only, NaT-only, nullable-string NA-only, all-null float16/float32/float64, each float dtype with values, mixed valid/null/unparseable, all-valid, all-unparseable, duplicate and non-default and string index, multiple ordered row errors, auto-detected format, the `astype(str)` branch, a wide shift range, and group_by anchors (numpy int, numpy bool, nullable Int64 with a null, strings, null anchors falling back to the row's own date, 2**62 and 2**63-1 integers, all-null anchors, non-default index). A tz-naive datetime source raises the same `GenerationError` code on both paths.

Spy tests: exactly one `DeriveContext.for_column` and one `derive_sources` call per column, over only the usable rows' canonical bytes in row order, with the `.item()` normalization visible for a numpy bool anchor.

## Mutants (hand-applied one at a time, tests re-run, source restored from a backup copy)

| Mutant | Result | Killed by |
|---|---|---|
| Revert `VaultError` base to `Exception` | killed | hierarchy table and sentry walk |
| `PoolCapacityError` made a child of `GenerationError` | killed | independence test |
| `derive_sources` called with a wrong namespace | killed | differential tests |
| Per-row scalar `derive` loop restored | killed | call-count spy |
| `np.where` result assigned directly | killed | dtype cases |
| Unusable rows filled from `col.to_numpy(dtype=object)` | killed | all-null float16/float32 cases |
| `formatted` used for unusable rows | killed | differential tests |

## Verification

- Runs used `/home/cam/.cache/decoy-native-venv/bin/python` with `PYTHONPATH=src`, one process at a time through `pytest-one`.
- Broad run: `tests/sentry`, `tests/unit/execution`, `tests/native`, `tests/physical/test_shadow_date_shift.py`, `tests/parity/native`, plus every test file that names a changed class (239 files): 18369 passed, 119 skipped, 59 xfailed, 1 failed, 39 errors. The 39 errors are `fixture not found` for the `tests/unit/plan` conftest fixtures, caused by mixing directory and file arguments in one invocation. Re-running `tests/unit/plan`, `tests/unit/generation` alone gives 864 passed, 19 skipped, 0 errors. The 119 skips are the DP proof-stack tests and other optional extras; none are date_shift, exception or inventory tests.
- The 1 failure is `tests/unit/test_public_api.py::TestV2BehaviorRegressionPinsS11::test_v2_strategies_derive_per_strategy_namespace`. See the next section.
- ruff 0.15.14 (local mirror) and ruff 0.15.22 (the new CI pin): `ruff check` and `ruff format --check` over `src tests testflight scripts` are clean. `mypy src`: clean.

## Open: existing test pins `_date_shift.derive`

`tests/unit/test_public_api.py::test_v2_strategies_derive_per_strategy_namespace` monkeypatches `decoy_engine.execution._strategies._date_shift.derive` and records the namespace of each call. Batching removes the per-row `derive` call from that module, so the attribute no longer exists. The plan says existing tests pass unchanged, and this one cannot while item 4 batches. The test was not edited. Its intent (the column namespace is bound into the derivation, never a shared `"mask"` label) is covered by the new spy test, which checks the namespace passed to `for_column` and `derive_sources`. The owner needs to approve repointing the old test's spy at `DeriveContext.for_column`/`derive_sources` (a spy-seam change, same assertions) or choose another route.

## Judgment calls

- CI timeouts: observed job times from three green runs were ruff 0.2 min, mypy 1 min, ml 2-3 min, packaging 1-1.4 min, dp-certified 3.2 min, regression-gate 27-44 min. Failed runs reached 2h+. Chosen caps: ruff 10, mypy 15, regression-gate 75 (plan fallback of 60 sat too close to the 44-minute observation), the other three 20.
- `ci.yml` has one ruff pin. The new sentry also checks the pre-commit hook rev.
- `_draw_site_providers.py` is a legacy over-max module (985 LOC, shrink-only). The new import added one line, tripping the size sentry. The last `DrawSiteProtocolError` docstring entry was reflowed from four lines to three to keep the file at 985. No census value changed.
- `derive_sources` derivation is skipped when a column has no usable rows, so `DeriveContext.for_column` is not built in that case. The old loop never derived without a usable row, so a bad seed length still raises only when a usable row exists.
- Anchors and values are read from `list(col)` rather than `col.iloc[i]`, because the old loop iterated the Series and numeric dtypes yield Python scalars from iteration but numpy scalars from `iloc`.
- Item 3: CLAUDE.md already pointed at `decoy-platform/docs/ROADMAP.md`; the stale roadmap text was in `CODEMAP.md:112` ("Maintained in the commercial platform repo"), fixed there. The `errors.py:137` comment named `graph/runner.py` and a nonexistent `graph/errors.py::translate`; both references were removed rather than replaced with a guessed location.
- Item 5: ADR-0005 lives in the platform repo, so the growth history went to `docs/decisions/module-size-census-history.md` (excluded from the Sphinx build by `decisions/**`). Comment text moved verbatim; census entries got one-line rationales.
- The `errors.py` comment fixes (item 3) are in the item 1 commit because the file also carries the `ValidationError` reparent.

## Docs gate

`grep -n "import-linter\|graph/runner.py" CLAUDE.md src/decoy_engine/errors.py` returns nothing.

## Plan-author resolution of the stopped test

`tests/unit/test_public_api.py::TestV2BehaviorRegressionPinsS11::test_v2_strategies_derive_per_strategy_namespace` monkeypatched `_date_shift.derive`, which item 4 removes by design (date_shift now derives once per column through `DeriveContext`). The test's intent is that each strategy binds its column's own namespace, never a shared label. Its date_shift spy now records the namespace passed to `DeriveContext.for_column`. The assertions are unchanged (hash `{"A_ns"}`, date_shift `{"B_ns"}`). 17 passed.
