# SC1 review notes: out-of-core FK runner port (`sc1/out-of-core-optin`)

Branch: `sc1/out-of-core-optin`, built off `main` @ `8ca5bfe` in an isolated
worktree (`/home/cam/vscode/sc1-out-of-core`). Source branch ported from:
`feat/option4-out-of-core` (25 commits ahead / 28 behind `main` at the time of
this port; merge-base `b76ab10`).

This file exists because the build instructions said: **if a reconciliation is
non-obvious or touches fail-loud/leak/quarantine/FK-resolution logic, stop and
record it here rather than guess.** Three items below cleared that bar. None
of them block review by themselves, but all three deserve a human read before
merge.

## 1. `_transactional_sink.py` was NOT redundant -- it needed a real, careful merge

The build brief characterized `_sequential.py` and `_transactional_sink.py` as
"now redundant/divergent with main's -- drop them and use main's versions,"
because the branch predates Option 2 landing on main. That framing holds for
`_sequential.py` (option4's own 8-line diff there is a comment-only
clarification about `write_batches` isinstance semantics; dropped clean, no
functional loss -- verified by diffing option4's change against
`merge-base(main, feat/option4-out-of-core)`).

It does **not** hold for `_transactional_sink.py`. Main's copy is byte-identical
to the merge-base (main never touched this file independently), and option4's
86-line diff is a **pure additive extension**, not a divergent reimplementation:
it adds a `write_batches(table, batches, *, schema)` method to the
`TransactionalSink` Protocol, to `_CallableSinkAdapter` (a materialize-then-call
fallback), and to `ParquetTransactionalSink` (a real `ParquetWriter`-based
streaming implementation, with `_validate_table_name`/`_stage_dest` extracted
as shared helpers). `write()`/`commit()`/`abort()` are byte-identical to main's
current versions -- untouched.

**Why this matters for review, not just correctness:** `write_batches` is
required by `out_of_core/_emit.py` (`sink.write_batches(...)`), so if this
diff had actually been dropped as "redundant," the ported out-of-core route
would import cleanly but crash the first time a caller passed a real
`TransactionalSink` (any resident-table-free run). I ported the extension.

**The one real behavior change worth a second look:** `TransactionalSink` is a
`@runtime_checkable` `Protocol`, and `isinstance()` against a
`runtime_checkable` Protocol matches by attribute-name presence across **all**
protocol members. Adding `write_batches` as a fourth member means any
caller-supplied sink object that implements only the pre-existing three
methods (`write`/`commit`/`abort`) **no longer matches**
`isinstance(sink, TransactionalSink)` in `_sequential.run_sequential`'s
dispatch -- it now falls through to being treated as a plain callable, which
raises `TypeError` at the first `write()` call rather than running
transactionally. This is a real, if narrow, compatibility edge: the
compatibility-contract framing in this repo (frozen-surface governance,
pre-GA) is exactly the kind of thing that should see this called out rather
than silently shipped.

- I found and fixed the one in-repo place this bites: the test fixture
  `_RaisyAbortSink` in `tests/unit/execution/test_transactional_sink.py`
  (`test_run_sequential_abort_error_does_not_mask_original`) implemented only
  the three-method shape; I added a `write_batches` that raises
  `AssertionError` (matching option4's own fix -- it hit the identical issue
  during that branch's own development), so the fixture still matches the
  Protocol and the test still exercises `run_sequential`'s real transactional
  dispatch rather than accidentally exercising the callable-fallback path.
- I grepped the whole engine repo (`grep -rln "def commit(self)" src tests`)
  for other minimal three-method sink implementations and found none besides
  that one fixture. I could **not** grep the platform repo or any external
  caller from here -- if `decoy-platform` (or any other consumer) passes a
  hand-rolled `TransactionalSink`-shaped object into `run_sequential` without
  implementing `write_batches`, this port silently changes its dispatch path
  from transactional to callable-fallback, and it will raise `TypeError` on
  first write instead of running as before. **Please verify with a
  cross-repo grep for `TransactionalSink` implementers, or at minimum a
  platform-side smoke test of `run_sequential` with its actual sink
  object(s), before merge.**

Ported the corresponding new tests (`test_write_batches_*`,
`test_callable_sink_adapter_write_batches_*`) unchanged from option4.

## 2. Dropped `PolarsExecutionAdapter(enable_out_of_core=...)` wiring entirely

Option4's diff to `src/decoy_engine/execution/polars/_polars_adapter.py` adds
an `enable_out_of_core: bool = False` constructor parameter and, inside
`run()`, a branch that calls `run_fk_out_of_core` when both
`self._enable_out_of_core` and `check_out_of_core_compatibility(...).accepted`
are true. Default is `False`, so nothing routes through it unless a caller
explicitly constructs `PolarsExecutionAdapter(enable_out_of_core=True)` --
`select_execution_adapter`/`run_pipeline`/`_planner.py` never do.

I judged this **out of scope for SC1 and dropped it**, rather than port it:
the build brief's acceptance bar states "out_of_core is reachable only via the
explicit `run_fk_out_of_core` entrypoint," and this flag is a second call path
into the capability, wired into a core, heavily-used substrate dispatch method
(`PolarsExecutionAdapter.run()`) that also independently gained new logic on
main since the branch point (the Sprint 2 honesty-pack row-error draining,
`D7`). Keeping `_polars_adapter.py` fully untouched (identical to current
main) removes any ambiguity about whether this branch changes routing, at the
cost of one possible invocation style (`PolarsExecutionAdapter(enable_out_of_core=True).run(...)`)
that SC2 (or a follow-up) can reintroduce deliberately, reconciled against
whatever `_polars_adapter.py` looks like at that point. `run_fk_out_of_core`
itself is unaffected: it calls `check_out_of_core_compatibility` and fails
closed internally regardless of any adapter-level wiring, so this is a pure
scope cut, not a safety gap.

Consequently I also did **not** port the three new `test_polars_adapter.py`
tests that exercise this flag (`test_out_of_core_compatible_fk_routes_when_opted_in`,
`test_out_of_core_accepts_preserve_policy`,
`test_out_of_core_rejects_multi_parent_same_child_fk`) -- they test a surface
this branch does not add. `check_out_of_core_compatibility` itself is fully
covered by the ported `out_of_core` unit test suite regardless.

Verified: `git diff --stat main -- src/decoy_engine/execution/polars/_polars_adapter.py`
on this branch is empty.

## 3. `_chunked_fk.py` / `_chunked.py` bundles a second, related-but-distinct capability

The build brief's category (a) explicitly lists `_chunked_fk.py` as a NEW file
to port, and category (b) lists `_chunked.py` as an edited-existing file to
reconcile. Worth being explicit that this is **not** the out-of-core (DuckDB)
route itself: `_chunked_fk.py` is a fail-closed admission gate
(`gate_fk_child_edges`) for the pre-existing **single-table chunked streaming**
path (`run_mask_pipeline_chunked`, non-FK, non-DuckDB), extending it to admit
FK **child** edges that can self-mask under four strict conditions (parent
strategy chunk-safe, child declares the identical strategy + namespace,
`orphan_policy: remap`, single-column only). Previously any relationship in a
chunked config was a blanket `chunked_relationships_unsupported` rejection.

Main independently added substantial new, non-overlapping functionality to
`_chunked.py` since the branch point: `chunk_result_sink`, the Sprint-2
honesty-pack fail-closed row-error raise (`RowErrorsFailedError` the moment
any chunk reports a row error -- chunked streaming has no quarantine
machinery), and `concat_masked_chunks`/`aggregate_chunk_warnings`/
`aggregate_chunk_timings`. I hand-merged: kept every line of main's additions
untouched, and layered in option4's docstring section, the
`CHUNK_SAFE_STRATEGIES`/`gate_fk_child_edges` import (replacing the inline
frozenset, now the single authoritative definition in `_chunked_fk.py`), and
the `check_chunked_compatibility` relationships branch (now calls
`gate_fk_child_edges(config, table=table)` instead of raising a blanket
rejection). The two branches' edits touched disjoint regions of the file, so
this was a clean layer, not a text-conflict resolution -- flagging it here
because it is still a real behavior change (chunked jobs with FK children are
now sometimes admitted, where every one was rejected before), not because the
merge itself was ambiguous.

Updated the one existing test this changes:
`tests/unit/execution/test_chunked.py::test_relationships_rejected` ->
`test_relationships_with_preserve_policy_rejected`, asserting
`chunked_fk_orphan_policy_not_remap` instead of the old
`chunked_relationships_unsupported` (a `PRESERVE`-policy edge is exactly the
shape the new gate rejects, just with a specific reason now).

## Ported wholesale, no conflict (main untouched these paths since the branch point)

- `src/decoy_engine/execution/_fk_keys.py` (new: canonical FK match-key
  normalization -- `fk_key_value`/`fk_join_key`/`fk_join_key_tuple`/
  `NULL_FK_KEY`)
- `src/decoy_engine/kernel/` (new package: `hash_array`/`redact_array`/
  `truncate_array`/`passthrough_array`/`canonicalize_derive_source` --
  backend-neutral masking kernel shared by the pandas adapter and out_of_core)
- `src/decoy_engine/execution/out_of_core/` (new package, all 11 modules:
  `_batch_join.py`, `_budget.py`, `_compat.py`, `_duckdb.py`, `_emit.py`,
  `_join.py`, `_mask.py`, `_relation.py`, `_runner.py` (`run_fk_out_of_core`),
  `_source.py`, `_stage.py`, `__init__.py`)
- `src/decoy_engine/execution/_strategies/_hash.py`, `_redact.py` (both
  swapped to call the shared kernel; main had not touched either since the
  branch point, verified with `git diff --quiet base main -- <path>` before
  taking the whole file)
- `scripts/fk_memory_probe.py` (main untouched; option4's 800-line rewrite
  adds `--mode out_of_core`, `--capability`, `--mem-cap-mb`)
- `docs/relationships-memory-scaling.md`, `docs/relationships-out-of-core-sprints.md`
  (main untouched the first; the second is a new design doc many ported
  module docstrings cite by path)
- `docs/index.md` (small toctree addition; hand-merged alongside main's own
  unrelated toctree addition, non-overlapping lines)
- All out_of_core/kernel/chunked_fk unit tests, the perf sentinel
  (`tests/perf/test_out_of_core_memory_sentinel.py`), and
  `tests/perf_fixtures/fk_relational.py` / `tests/unit/test_fk_memory_probe_classifier.py`

## Reconciled (main's fail-loud / honesty-pack behavior preserved)

- `src/decoy_engine/execution/_pandas_adapter.py`: `_fk_key_value` now
  delegates to `decoy_engine.execution._fk_keys.fk_key_value` instead of
  reimplementing the same int/float normalization inline. Verified safe: both
  call sites in this file (`_resolve_fk_group`'s child-key build and
  `_parent_map`'s source-key build) already filter out null/NaN before
  calling `_fk_key_value` (`na_array[i]` / `pd.isna(x)` guards), so the extra
  `None`/`NaN` -> `NULL_FK_KEY` branch `fk_key_value` adds over the old inline
  function is dead code at these call sites -- a strict superset, not a
  behavior change. This does **not** touch the S2 quarantine-aware FK
  resolution logic itself (`_resolve_fk_group`, `_parent_map`'s
  `key_error_rows`/`errored_keys_cache` handling), only the key-normalization
  helper both use.
- `src/decoy_engine/execution/_strategies/_truncate.py`: kept every one of
  main's Sprint-13 fail-closed `StrategyError` raises
  (`truncate_length_invalid`/`truncate_keep_invalid`/`truncate_mask_char_invalid`)
  verbatim; only the actual truncation computation (after all three configs
  validate) now calls the shared `truncate_array` kernel instead of the old
  inline pandas string-slicing, which is byte-identical for every
  `(length, keep, mask_char)` combination (verified by the full
  `tests/unit/execution` suite, including `test_chunked.py`'s
  chunk-vs-full-frame parity tests over truncate).
- `src/decoy_engine/execution/_transactional_sink.py`: see item 1 above.
- `src/decoy_engine/execution/_chunked.py`: see item 3 above.
- `docs/index.md`: two small, non-overlapping toctree insertions.

## Dropped as genuinely redundant (verified, not assumed)

- `src/decoy_engine/execution/_sequential.py`: option4's 8-line diff is a
  comment-only clarification of `write_batches` isinstance semantics; no
  functional change. Kept main's version untouched.
- `src/decoy_engine/execution/polars/_polars_adapter.py`: see item 2 above --
  dropped as an explicit scope cut, not because it was redundant, but
  recorded here since a reviewer diffing "what changed" should know it was
  considered and deliberately excluded.

## Skipped (docs, deferred to the house docs step, not blocking SC1's acceptance bar)

`CHANGELOG.md` and `CODEMAP.md`: both diverged independently and substantially
on `main` since the branch point (12 and 6 commits respectively touching them)
and both need combining two sets of real additions, not a mechanical merge.
Neither affects code behavior or the verification commands below. Per this
repo's stated per-sprint process ("DEVELOP -> SELF-CHECK -> REVIEW ->
REMEDIATE -> docs (barry) -> CI-gate -> GATE-2 -> merge"), documentation
sync is a distinct step after review, not part of the code port; flagging
here rather than merging in ad hoc doc text so barry's pass starts from a
clean diff.

## What was verified (see also the report to the requester)

- Full `tests/unit` suite: 3707 passed, 29 skipped, 1 failed. The failure
  (`tests/unit/test_v2_cloud_sources.py::TestCloudSourceEndToEnd::test_read_s3_source_to_arrow_via_moto`,
  `ModuleNotFoundError: No module named 'pydantic_settings'`) reproduces
  identically on unmodified `main` in this same environment (verified) --
  it is a pre-existing sibling-repo (`decoy-platform`) dependency gap in this
  devbox's engine venv, not caused by this port, and touches no file in this
  branch's diff.
- `tests/unit/execution` (640 tests, includes all out_of_core/chunked_fk/
  transactional_sink/kernel tests): all green.
- Perf sentinel, default gate (`pytest tests/perf/test_out_of_core_memory_sentinel.py`,
  no marker override): 2 passed (peak-RSS budget, small-scale overhead ratio).
- Perf sentinel, `-m benchmark` (the two opt-in, minutes-long tests): 2
  passed, including `test_out_of_core_completes_where_in_memory_routes_oom`
  -- the Sprint C5 capability proof: at 400k rows/table x 16 columns x 3
  tables under a hard 1,024 MB `RLIMIT_DATA` cap, `full` and `sequential`
  both report `outcome == "oom"`, `out_of_core` reports
  `outcome == "completed"` with `parity == "ok"` and both FK edges
  non-vacuously checked, and the driver's own `proven` flag is `True`.
- `ruff check` and `ruff format --check` on every ported/reconciled file:
  clean.
- `mypy src/decoy_engine testflight` (whole tree, matching CI's exact
  invocation): `Success: no issues found in 347 source files`.
- `git diff --stat main -- <_pipeline.py, _pipeline_routing.py, _planner.py,
  _substrate.py, execution/__init__.py, polars/_polars_adapter.py>`: empty
  for all six. No auto-routing surface touched; `run_fk_out_of_core` is
  reachable only by importing `decoy_engine.execution.out_of_core` directly.
