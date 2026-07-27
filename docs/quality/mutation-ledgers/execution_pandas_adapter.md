# Mutation grading: `execution/_pandas_adapter.py` -- substrate bar 75%

TQ substrate sweep (branch `tq/substrate-sweep`), FULL-TRIAGE grade by
`scripts/tq_mutate.py` with default survived-bucket re-adjudication (finding #16
RESOLVED). `_pandas_adapter.py` is the pandas-backed execution adapter:
`PandasExecutionAdapter.run` (full-frame execution over all tables),
`_dispatch_mask_node` (per-column strategy dispatch + FK resolve timing),
`_resolve_fk_node` / `_parent_map` (the FK parent-map resolution + orphan
handling -- RI machinery), `run_single` (single-table convenience), `run_sequential`
(a thin wrapper over `execution._sequential.run_sequential`), plus the handler
table (`__init__`), the `_fk_key_value` compat wrapper, and the
`get_default_executor` singleton. The RI-critical key IDENTITY is in `_fk_keys`
(logic-100%, crown jewels); this module is the pandas-route resolution + dispatch
layer. Not crypto/RI proper, so the bar is **75% of LOGIC mutants**.

## Numbers

**Killed 460/498 = 92.37% LOGIC (tool-native, 0 unresolved). 38 survivors: 36
proven equivalent + 2 precisely-characterized residual.** Above the 75% bar
(measured max(57.63 + 15, 75) = 75%).

Baseline (5 existing execution/keyprovider files): 287/498 = 57.63%, 211 survived.
NOTE: mutmut's in-process run reported 210 of the 498 as ⏰ TIMEOUT (finding #8 --
the in-process runner mis-marks survivors as timeout on this heavy pandas suite);
`tq_mutate` re-adjudicated all 210 against the full selection, 0 left unresolved.
The full triage then adjudicated all 211 survivors across three kill files:
**173 additional kills**, 36 proven equivalent, 2 residual.

| Function | Killed by the sweep | Equivalent | Residual |
|---|---|---|---|
| `run` | 36 | 11 | 0 |
| `_dispatch_mask_node` | 31 | 9 | 0 |
| `_resolve_fk_node` | 51 | 2 | 0 |
| `_parent_map` | 25 | 4 | 0 |
| `run_sequential` (wrapper) | 20 | 2 | 0 |
| `run_single` | 6 | 5 | 2 |
| `__init__` / `_fk_key_value` / `get_default_executor` | 4 | 3 | 0 |

## Kills

### FK resolution -- `test_pandas_adapter_fk_kills.py` (`_resolve_fk_node` 51 + `_parent_map` 22)
Drive `PandasExecutionAdapter().run(...)` end-to-end over FK jobs and assert RI
outcomes: a three-table chain resolves each child against ITS OWN parent (a
nulled `_parent_map` cache_key collides the grandchild onto the parent map), an
errored parent key EXCLUDES from the map and CASCADES the child to null (dropping
`key_error_rows` / `errored_keys_cache` / the exclusion leaks the raw key),
multi-parent precedence (a `>2` / `edges[2:]` skip orphans the second parent), a
composite FK with a null in the second key column is a null-FK not an orphan,
REMAP mints a fresh masked value (needs `node_by_key`), PRESERVE keeps the raw
key, a >2**53 int FK key survives exact beside a null (the lossless-int path), and
an all-null resolved FK preserves the source uint width. The null-parent-key and
errored-key loops use `continue` not `break` (a `break` orphans every later row).

### Execution + dispatch -- `test_pandas_adapter_exec_kills.py` (`run` 37 + `_dispatch_mask_node` 31 + `run_sequential` 20)
`run`: lossless-int FK typing (a big int64 key beside a null rounds when the
FK-safe set is emptied), group-anchor pre-mask snapshots (date_shift group_by
fails closed on an empty snapshot), the full-frame key-error cascade + drain
attribution, REMAP `node_by_key`, the packaged `ExecutionResult` (row_errors,
quality_metrics corpora block, timings), an absent-table node skipped with
`continue` not `break`, and the boundary-conversion telemetry via a BOUNDED
`0 < ms < 1000` assert (catches the perf_counter-epoch leak; pure-magnitude
perturbations are equivalent -- finding #18). `_dispatch_mask_node`: the current
table stamped before dispatch, the FK-resolve/composite/scalar timing label +
column + separator, the node_by_key + key-error-cache threading, and the
`composite_fk_group_no_edge` / `unsupported_strategy` coded raises. `run_sequential`
(wrapper): each threaded arg (self/plan/loader/registry/graph/namespace_registry/
sink/quarantine_config/unconfigured_column_policy/key_provider) reaches the
delegate -- byte parity or a coded divergence when nulled/dropped.

### Small surfaces -- `test_pandas_adapter_helper_kills.py` (10)
`__init__` registers the fpe handler at the CONFIGURED chunk_count under the "fpe"
key (a renamed key leaves the default-count SCALAR handler); `_fk_key_value`
forwards the real value (not the null sentinel); `get_default_executor` returns a
real adapter (not None); `run_single`'s multi-table guard raises the coded error
with the identifying `table=` data.

## EQUIVALENT survivors (38) -- proven

Each verified to survive the full 8-file selection standalone (rc 0).

- **Cache / unreachable-default (FK, 6):** `_parent_map` 7, 8 (parent-not-in-frames
  returns `{}` either way), 14, 16 (snapshot default unreachable -- every parent
  key column is snapshotted first); `_resolve_fk_node` 16, 52 (`>= 1` on a single
  edge/column -- identical map / null-mask). (`_parent_map` 2, 3, 53 -- the cache
  hit/write mutants -- the FK kill file leaves them surviving, but the BROADER
  8-file selection kills them, so they are counted killed, not equivalent.)
- **Message prose + inert separator (dispatch, 9):** `_dispatch_mask_node` 63, 65,
  68, 69, 74, 76, 82, 84 (coded-raise message prose, code asserted separately), 92
  (a `","` separator on a scalar single-column node, never applied).
- **Telemetry noise-band + route-inert (run, 11):** `run` 40, 42, 150, 151, 152, 154
  (boundary_conversion_ms magnitude perturbations inside the timing noise band, no
  non-flaky observable -- finding #18; confirmed at jobs=1), 35 (top_code int/float ingestion identical,
  caps bounded < 2**53), 44 (`and`->`or` guard invariantly True -- the anchor col
  always exists in its own frame), 52 (`relationship_graph=None` not read on this
  path), 146, 148 (`preserve_index` None vs False on a RangeIndex frame -- benign
  index metadata only, data/columns/types identical).
- **Wrapper + init plumbing (10):** `run_sequential` 5, 16 (`pool_cache=None` ->
  delegate builds a fresh PoolCache, byte-identical); `run_single` 11, 12 (guard
  message prose), 18, 20, 25 (`pool_cache=None` / `namespace_registry=None` forward
  -- `run` rebuilds / composite-only, byte-identical); `__init__` 1 (no-op), 2
  (`self._fpe_chunk_count` is write-only, never read); `get_default_executor` 1
  (`substrate=None` still returns a valid adapter, only the cache key differs).

## Residual (2) -- precisely characterized, killable, deferred

`run_single` 21, 28: the `key_provider` forward to `run` (`=None` / dropped kwarg).
`run_single` is a thin wrapper; the existing keyprovider tests exercise `run`'s
key_provider directly but not through `run_single`. KILLABLE with a keyed
single-table `run_single` call (a keyed column masks off `job_seed` without the
provider, so the output diverges). Deferred as a low-value thin-wrapper forward (2
of 498, above bar); the exec cluster proves the SAME key_provider threading on
`run` and `run_sequential`. Recipe recorded; flagged for a decision.

## Candidate findings

None. No mutation exposed a wrong FK resolution, a leaked errored key, a wrong
orphan verdict, a wrong dispatch route, or a full-frame byte divergence.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to
`src/decoy_engine/execution/_pandas_adapter.py` and the test selection to the
EIGHT files: `test_pandas_adapter.py`, `test_de03_output_projection.py`,
`test_hash_bucketize.py`, `test_run_pipeline_substrate.py`,
`tests/unit/test_de02_keyprovider.py`, `test_pandas_adapter_helper_kills.py`,
`test_pandas_adapter_fk_kills.py`, and `test_pandas_adapter_exec_kills.py`; then
`rm -rf mutants && python scripts/tq_mutate.py --run`. `source_paths` stays at the
package root. NOTE: the selection carries a bounded conversion-timing assertion --
the timing mutants (run 40-42, 150-154) are far from the bound so `--jobs 6` is
safe here (unlike `_sequential`'s near-threshold sign flip, finding #18), but the
timing survivors were also confirmed at `--jobs 1`.
