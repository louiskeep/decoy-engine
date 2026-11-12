Status: record

# F32 E1 build, half 2 of 2: ordinal labels, shape cells, no-network sentry

Plan: Platform `plan/f32-support-bundles` `39c2772c` (revision 5), sections 1.2, 2 Rule 3, 6.2 T-E2/T-E4/T-E9, 10 row E1. This branch `feat/f32-e1-labels` is stacked on `feat/f32-e1-types` (see `2026-11-02-f32-e1-types-build.md` for the seam and the type-side evidence) and merges after it. Both split the single branch `feat/f32-e1` (head `55b28933`, base `492f35ac`), built here on `origin/main` `9eec6123`.

Code status: `_labels.py` is byte-identical to `55b28933`. The `__init__.py` export of `LabelAllocator` is the only edit to existing code. The review fixes are in the types half. `test_labels.py` is the reviewed file with its import block sorted by `ruff check --fix` (I001: `decoy_engine.reasons` above `decoy_engine.support`) and the 9999-label cap cell moved in from `test_bundle_fields.py`. The sentry scans `SUPPORT.rglob("*.py")` (was `glob`).

## 1. Red and green

Red: the tests-only commit, with `LabelAllocator` not yet exported: `tests/unit/support/test_labels.py` fails at collection (`ImportError`). The shape cells and the sentry pass on the types half by design, so they are not red-first; their evidence is the mutation table.

| ID | Test | Head |
|---|---|---|
| T-E2 | `test_labels.py::test_same_table_same_label_across_sections`, `::test_label_from_position_not_bytes` | pass |
| T-E4 | `::test_concurrent_builds_independent`, `::test_ordinal_cap` | pass |
| T-E9 | `test_support_no_network.py::test_no_network_imports`, `::test_seeded_fixture_is_red` | pass |
| shape cells | `test_shape_cells.py`: full valid bundle through pickle, `deepcopy`, `asdict`; column ownership and repeats; edge arity and ownership; declines and `counts_log2` order; tight `ms` and `arity` ranges; `None` refused; repeated phases | pass |

## 2. Mutations (each applied once, run, reverted; tree clean after)

| # | Mutation | Test | Result |
|---|---|---|---|
| L6 | table label from `hash(name)` | `test_same_table_same_label_across_sections` | red |
| L7 | allocator table map shared at module level | `test_label_from_position_not_bytes` | red (`-x` stops at the first failure) |
| L8 | `import urllib.request` in `_shape.py` | `test_no_network_imports` | red |
| S1 | drop table column-ownership check | `test_table_columns_owned_and_unique` | red |
| S2 | drop repeated-column check | same | red |
| S3 | drop edge arity check | `test_edge_arity_and_ownership` | red |
| S4 | drop edge ownership check | same | red |
| S5 | declines unsorted and repeats allowed | `test_route_declines_sorted_and_unique` | red |
| S6 | `counts_log2` order and repeats allowed | `test_event_counts_sorted_and_unique` | red |
| S7 | `ms` range widened to +-2**63 | `test_int_ranges_are_tight` | red |
| S8 | `arity` range widened to +-2**63 | `test_int_ranges_are_tight` | red; first version of the cell survived because the column-count check also refused, so it now uses arity 0 and 9 with matching column counts |
| S9 | `None` accepted in every field | `test_none_refused_in_non_optional_fields` | red |
| S10 | repeated phases allowed | `test_timings_phase_repeats_refused` | red |

L6 to L8 were also run on `55b28933` (M6 to M8 in the original record). S1 to S10 are new: they were the mutants that survived the reviewed tests.

## 3. Gates (T1 only, no full suite)

`ruff check --no-cache src tests testflight scripts`: All checks passed (exit 0). The original record's ruff row said rc 0 but the head had an I001 in `test_labels.py`; the gates run used a cache, and the fix is this branch's sorted import. `ruff format --check src tests`: clean. `mypy src/decoy_engine`: Success, 679 source files. `pytest tests/unit/support tests/sentry/test_support_no_network.py tests/sentry/test_public_import_boundary.py tests/sentry/test_reason_registry.py`: 62 passed. The full suite, secret census and licence gates ran on `55b28933` only (32294 passed, 0 failed) and were not rerun.

## 4. Not verified

- The full suite, `tests/sentry` as a whole and the module-size sentry on this head.
- Cross-bundle unlinkability beyond position-only labels (V2-LOG, plan 12).
- The exact per-node timings are not bucketed (V2-LOG, plan 12).
- No Platform or CLI code was run; Python 3.10 only.

## 5. Main merge, 2026-11-12

Merged `feat/f32-e1-types` (which carries `origin/main` `426e6d05`) into this branch with a plain merge, no rebase. One conflict, in `src/decoy_engine/support/__init__.py` (import block): kept the types half's `from decoy_engine.reasons import Outcome, RetryClass, Severity` and this half's `from decoy_engine.support._labels import LabelAllocator`.

Edits in this half: `tests/sentry/test_sentry_manifest.py` lists `tests/sentry/test_support_no_network.py` in `SENTRIES`; the `CODEMAP.md` "Support bundles" row names that sentry and `_labels.py`; fragment `changelog.d/f32-e1-labels.md` (`check_fragment.py --base origin/main` exits 0). No `.importlinter`, snapshot, enum or registry edit: those are in the types half. `_labels.py` imports only `decoy_engine.support._shape`, so the facade rule passes.

Results: `tests/sentry tests/unit/support tests/unit/test_reason_registry.py` on the Mac: 5109 passed, 10 failed, the 10 tests in `tests/sentry/layout/test_import_layers.py` that fail on macOS by design. Those 15 layout tests pass on Linux through `scripts/dev/gates.sh --steps layers`. ruff check, ruff format --check and mypy on `support`: clean.

Mutations on this head (each applied once, run against `tests/unit/support` and the no-network sentry, reverted; tree clean after): L6, L7, L8 and S1 to S10 all red, each on the test named in the table above. L7 and L6 also trip three other `test_labels.py` cells.

Not run: the full suite.

## 6. Review round 2

Merged the round 2 fixes of `feat/f32-e1-types` (record `2026-11-02-f32-e1-types-build.md`, section 7): `support.ReasonRef` is gone and the bundle types carry `decoy_engine.reasons.ReasonRef`. In this half, `test_labels.py` and `test_shape_cells.py` import nothing named `ReasonRef` from `support` and take their reason from `route.FK_RELATIONSHIP.spec.ref` (plain `str` fields). `_labels.py` is unchanged. One conflict, in the `CODEMAP.md` support row: kept this half's row without `ReasonRef`.
