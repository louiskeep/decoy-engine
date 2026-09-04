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

## Risk

R2 (repo policy): a multi-module structural + import-compatibility refactor, not
"low". Retains the written-plan, cross-model plan gate, independent dennis
review, full verification, and exact-artifact final gate already applied.

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
   ~244-311, ~68 LOC). `_RunHead.max_value` uses `pyarrow.compute`, so this
   sibling imports `pyarrow`, `pyarrow.compute as pc`, and `pathlib.Path`.
   `_external_sort.py` imports it back.

Result: `BoundedExternalSorter` core ~520 LOC, under the 600 cap; no allowlist
entry needed for this file.

COMPATIBILITY BINDING: a test imports `_min_row_bytes` directly from
`_external_sort`. Keep a re-export in `_external_sort.py`
(`from ._external_sort_bounding import _min_row_bytes`) so that path still
resolves. Do NOT rewrite the test's import -- it is a compatibility tripwire.

## Plan B -- `_stream_join.py` (1173 -> target, extract then ratchet the core)

Two extractions (the third, EXPLAIN-method, extraction is DROPPED -- see below):

1. `_stream_join_plan.py` -- the DuckDB EXPLAIN-plan unordered-verification
   helpers `_is_global_sort_operator`, `_disable_join_optimizers`,
   `_iter_plan_nodes`, `_verify_unordered_plan_or_raise`, `_subtree_scan_names`
   (lines ~109-291, ~160 LOC). Leaf: take dict/Any + str, import `ExecutionError`
   + typing. `_disable_join_optimizers` closes over `_PINNED_DISABLED_OPTIMIZERS`,
   so that module-level constant MOVES with it into this sibling. NOTE:
   `_contiguity_error` does NOT belong here -- it is part of the reorder cursor
   unit (moves in extraction 2). Parent imports these back.
2. `_stream_join_cursors.py` -- the COMPLETE cohesive reorder/cursor/lifecycle
   unit, so the sibling never imports back from the parent (the cycle the plan
   gate caught): move ALL of `_contiguity_error`, `_guarded_reorder_iter`,
   `_release_reorder` (lines ~293-356), `_OrderedJoinRows` (~973-1051),
   `JoinRowCursor` (~1053-1154), `_concat_join_row_batches`,
   `_row_alignment_error` (~1156-end). `_OrderedJoinRows` calls
   `_guarded_reorder_iter`/`_release_reorder`, so they MUST travel with it.
   This sibling then depends only on Arrow, `weakref`, `ExecutionError`, and
   `BoundedExternalSorter` (from `_external_sort`) -- zero parent import, no
   cycle. Parent imports these back.

DROPPED extraction (`_stream_join_explain.py`): the plan gate confirmed the
EXPLAIN methods are NOT cleanly liftable -- `_unordered_join_query` needs the
edge/relation/`_q`; `explain_join` owns staging checks, reader registration, and
cleanup; `_iter_unordered_join_rows` inspects `self._conn` during
abandoned-generator cleanup and is directly monkeypatched by tests. Contorting
the class to lift these would change signatures and break monkeypatch seams for
no real cohesion gain. Leave them on `StreamFkJoiner` and ratchet the residual.

Expected: after extractions 1-2 the residual `StreamFkJoiner` core is a cohesive
class modestly over 600. Per the sentry's own allowlist-as-ratchet pattern (used
for other cohesive cores, e.g. `storm/detectors.py`), record the final
`_stream_join.py` LOC in the allowlist with a decomposition-owed note. The plan
gate approved this ratchet and found no cleaner class split worth introducing
solely to reach 600 LOC.

COMPATIBILITY BINDINGS (from the ORIGINAL modules, keep existing importers
working; the plan gate found these are load-bearing, not deferrable):
- `_stream_join.py`: `from ._stream_join_cursors import JoinRowCursor,
  _OrderedJoinRows` and `from ._stream_join_plan import
  _verify_unordered_plan_or_raise` (consumers: `_stream_driver.py:93` imports
  `JoinRowCursor`; `test_out_of_core_stream_join_reorder.py` imports
  `_OrderedJoinRows` + `_verify_unordered_plan_or_raise` x4; the reorder harness
  and `scripts/route_reorder_vs_batchjoin_ab.py` import `JoinRowCursor`). Keep
  `_stream_join.__all__ = ["JoinRowCursor", "StreamFkJoiner"]` UNCHANGED.
- Do NOT rewrite any existing test/script import -- they are compatibility
  tripwires that prove the re-exports work.
- Correction to an earlier dependency claim: `_stream_driver_support.py` imports
  only `StreamFkJoiner`, under `TYPE_CHECKING` (not `_OrderedJoinRows`).

## Acceptance tests (behavior-preserving, matched to CI gates)

- Existing tests pass UNCHANGED: the full non-benchmark `pytest tests` gate (not
  just the execution subset), including the OOC/stream + full parity sweep --
  byte-identity of the reorder and batch-join routes is the real guarantee this
  refactor must not disturb. No test import is rewritten (moved-symbol imports
  are preserved by the compatibility re-exports and act as tripwires).
- `ruff check src tests testflight scripts` clean.
- `ruff format --check src tests testflight scripts` clean.
- `mypy src/decoy_engine testflight` clean (the CI scope/invocation, not a
  narrowed per-file `--python-version` run).
- Size sentry green: `_external_sort.py` under 600 with no allowlist entry;
  `_stream_join.py` allowlisted at its exact final LOC with a decomposition-owed
  comment (or under 600 if it lands there).
- Cycle check that actually exercises the modules: direct import of BOTH parent
  modules AND every new sibling (`python -c "import
  decoy_engine.execution.out_of_core._stream_join, ..."`), since the top-level
  `import decoy_engine` does not load `_stream_join` and would not surface a
  cycle.
- Mutation-ledger path hygiene: any existing mutation ledger under
  `docs/quality/mutation-ledgers/` that references a moved graded helper by its
  old module path is updated to the new sibling path (grep the ledgers for the
  moved symbols; the moved code is unchanged, so no re-grading of logic is
  required -- only the path reference). Changed-unit coverage stays green via
  the unchanged tests.

## Rollback

Each of Plan A and Plan B is an independent commit; a broken import or cycle
surfaces immediately in the import smoke test + `pytest tests` and is reverted
by dropping that one commit. No R3 side effect; HELD on `feat/native-phase3`, no
merge.
