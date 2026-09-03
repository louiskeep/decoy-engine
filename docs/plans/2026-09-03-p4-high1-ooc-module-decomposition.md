# Phase 4 HIGH-1: out-of-core module-size decomposition

Status: plan

## Why

The `feat/native-phase3` branch fails the module-size sentry
(`tests/sentry/test_module_size.py`, 600-LOC orchestration cap) on two
out-of-core modules, so the branch CI is red and the eventual Phase-4 PR cannot
merge green:

- `src/decoy_engine/execution/out_of_core/_stream_join.py` -- 1173 LOC
- `src/decoy_engine/execution/out_of_core/_external_sort.py` -- 688 LOC

Neither is in the allowlist. Both predate the Phase-4 slices (the reorder-route
work grew adjacent modules, not these), so this is a branch-hygiene slice, not a
feature. Dennis flagged it as the Task-6-gate HIGH-1 carry-forward and it is the
last CI-red blocker before Phase-4 merge-readiness.

## Scope and non-goals

This is a PURE-MOVE refactor: relocate cohesive units to sibling modules and
update imports. No behavior change, no signature change, no logic edit. The
acceptance bar is byte-identical behavior (every existing test passes unchanged)
plus a green size sentry.

Non-goals: no algorithmic change to the sorter or the joiner; no public-API
change (`StreamFkJoiner`, `BoundedExternalSorter`, `run_ordered_join`,
`_OrderedJoinRows` keep their import paths via re-export where any external
caller depends on them); no touching the four Phase-4 slices' modules.

## Import-dependency facts (verified)

- `_external_sort.py` imports only `ExecutionError` internally; it is imported by
  `_stream_join.py` (for `BoundedExternalSorter`). Its extraction targets are
  leaf helpers.
- `_stream_join.py` imports `_external_sort.BoundedExternalSorter` and several
  OOC modules; it is imported by `_stream_driver.py` and
  `_stream_driver_support.py` (for `StreamFkJoiner` and `_OrderedJoinRows`).
- No extraction below creates a cycle: every new sibling is imported BY its
  parent module and imports nothing back from it.

## Plan A -- `_external_sort.py` (688 -> ~520, clean, under cap)

Two extractions into new siblings in the same package:

1. `_external_sort_bounding.py` -- the batch-bounding free functions
   `_min_row_bytes`, `_is_supported_key_type`, `_materialize`,
   `_iter_bounded_views`, `_bounded_batches` (lines ~140-243, ~100 LOC). They
   take Arrow objects + ints and import only `pyarrow` (+ the small local
   constants they use, which move with them). `_external_sort.py` imports them
   back.
2. `_external_sort_run.py` -- the `_RunHead` spilled-run reader class (lines
   ~244-311, ~68 LOC). Imports `pyarrow` + `pathlib.Path`. `_external_sort.py`
   imports it back.

Result: `BoundedExternalSorter` core ~520 LOC, under the 600 cap; no allowlist
entry needed for this file.

## Plan B -- `_stream_join.py` (1173 -> target, extract then ratchet the core)

Three extractions:

1. `_stream_join_plan.py` -- the DuckDB EXPLAIN-plan unordered-verification
   helpers `_is_global_sort_operator`, `_disable_join_optimizers`,
   `_iter_plan_nodes`, `_verify_unordered_plan_or_raise`, `_subtree_scan_names`,
   `_contiguity_error` (lines ~109-291, ~180 LOC). Leaf: take dict/Any + str,
   import only `ExecutionError` + typing. Parent imports them back.
2. `_stream_join_cursors.py` -- the owning-iterator / cursor types
   `_OrderedJoinRows` (lines ~973-1051) and `JoinRowCursor` (lines ~1053-1154),
   plus `_concat_join_row_batches` / `_row_alignment_error` (lines ~1156-end)
   (~180 LOC). `_OrderedJoinRows` imports `BoundedExternalSorter` from
   `_external_sort` (not from `_stream_join`), so no cycle. Parent imports them
   back and RE-EXPORTS `_OrderedJoinRows` so `_stream_driver*` import paths are
   unchanged (or update those two importers directly -- decided at build time,
   whichever keeps the diff smaller and cycle-free).
3. `_stream_join_explain.py` -- `StreamFkJoiner`'s EXPLAIN/optimizer-introspection
   methods, IF they can be lifted to free functions taking the connection +
   query without pulling instance state (`_run_explain_json`,
   `_disabled_optimizers`, `explain_join`, `_unordered_join_query`,
   `_iter_unordered_join_rows`, ~120 LOC). If any is too entangled with `self`
   to lift cleanly, leave it on the class -- do NOT contort the class to hit a
   line target.

Expected: after extractions 1-2 the file is ~810; extraction 3 (if clean) brings
it near ~690. The residual `StreamFkJoiner` core is a cohesive class that will
likely remain modestly over 600. Per the sentry's own allowlist-as-ratchet
pattern (used for other cohesive cores, e.g. `storm/detectors.py`), record the
final `_stream_join.py` LOC in the allowlist with a decomposition-owed note.
This is the right-sized outcome: extract what cleanly separates, ratchet the
cohesive remainder rather than fracturing one class across files.

## Acceptance tests (behavior-preserving)

- Full `tests/unit/execution/` and OOC/stream suites pass UNCHANGED (no test
  edits beyond import paths, and only if a test imports a moved private symbol
  directly).
- Full parity sweep (`tests/parity/`) green -- byte-identity of the reorder and
  batch-join routes is the real guarantee this refactor must not disturb.
- `mypy --python-version 3.13` clean on every touched module.
- Size sentry green: `_external_sort.py` under 600 with no allowlist entry;
  `_stream_join.py` either under 600 or allowlisted at its exact final LOC with
  a decomposition-owed comment.
- No import cycle (import-linter / a clean `python -c "import decoy_engine"`).

## Risk

Low. Pure moves guarded by an existing byte-parity suite; the only realistic
failure modes are a broken import path or an accidental cycle, both caught
immediately by mypy + the import smoke test + the full suite. No R3 side effect;
HELD on `feat/native-phase3`, no merge.
