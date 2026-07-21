# OOC-B / fix#1: single streaming join per FK edge

Status: approved design (Cam, 2026-07-21). Clean replacement, no runtime flag.
Correctness-critical. Merge gate: byte-parity + dennis (Opus) + Codex final + GCP flat-floor proof; else PARK.

## Problem

The out-of-core (OOC) FK-preservation route's peak memory floor rises with row
count instead of staying flat. GCP evidence: at a fixed 8 GB cap, a 100M-row run
completes at ~3.8 GB peak RSS, a 200M-row run climbs to ~5.4 GB and then fails.
The never-OOM safety net (fix#2 joiner-release, disk-aware routing, SPRINT-1
phase-aware preflight) keeps it failing *safely*, but the underlying floor still
scales with parent cardinality, so large graphs need ever-larger caps.

## Root cause (single structure)

In the streaming sink path, `ChildFkBatchJoiner.__init__`
(`src/decoy_engine/execution/out_of_core/_batch_join.py:150-164`) materializes
the parent-key relation as a DuckDB **TEMP TABLE**, once per FK edge, so
`join_batch` can run a fresh `LEFT JOIN` against it for every child batch
(hundreds of joins per large child). That table holds one row per distinct
parent key with its masked value; DuckDB cannot fully spill its
buffer-manager/control state, so on a 33.3M-row parent it pins ~3.2 GB resident
and stays live for the whole child stream. `_memory_estimate.py`'s
`predict_ooc_build_floor_bytes` prices exactly this at ~190 bytes/parent-row.

Everything else on the path is already streamed and spillable in DuckDB: the
child keys (bounded batch reader), the last-write-wins relation dedup
(`_relation.py`, deliberately structured as COPY -> max(row_nr) GROUP BY -> JOIN
BACK to avoid pinning O(distinct-key) state), the orphan anti-join count, and
the per-batch Python conversions (bounded by `batch_rows`). The materialized
parent TEMP TABLE is the one non-spillable, cardinality-proportional object.

fix#2 (PR #86) kept this table; it only prevented two of them being co-resident
(helps linear chains, not high-fan-in). fix#1 removes the table.

## Goal / non-goals

Goal: eliminate the per-edge O(parent-cardinality) resident structure so the OOC
memory floor stops scaling with parent rows, high-fan-in children stop
multiplying pinned relations, and per-edge wall clock drops (one join per edge
instead of N per-batch joins).

Non-goals (explicitly out of scope):
- The resident whole-child path (`_join.py::mask_child_fk`). It already uses the
  single-streaming-join shape and is not the scale problem. Left untouched.
- Changing masking output, orphan-policy semantics, FK-key encoding, relation
  build, routing/preflight *policy* (only the floor *estimate* is retuned, below).
- Any change visible to the pandas oracle. Output must be byte-identical.

## Approach (A: key pre-pass + single join + zip)

The resident path (`_join.py::mask_child_fk`, `_join.py:69-182`) already
implements fix#1's core: parent as a `read_parquet` VIEW (never materialized),
one `LEFT JOIN child_keys x parent_keys ORDER BY __decoy_row_nr`, result read
back through `to_arrow_reader(batch_rows)`, orphan policy via
`_append_output_batch`. It is "resident" only because its final step writes
columns back into a whole `pa.Table`. fix#1 gives the *streaming sink runner*
that same single-join shape, emitting incrementally to the sink instead.

Rejected alternatives: (B) swap the TEMP TABLE for a `read_parquet` view but keep
per-batch joins -- each of the hundreds of joins re-scans/re-builds the parent,
same transient O(parent) cost and far slower; a half-measure. (C) unify
`_join.py` and `_batch_join.py` into one engine -- drags in the resident path we
scoped out and widens blast radius on crown-jewel code. YAGNI.

### Restructure `_stream_table` into three phases per child table

Today (`_runner.py`) the FK join is fused into the per-batch rewrite loop:
`for out in mask_stream: joiner.join_batch(out, key_source=raw_batch)`. Replace
that with three phases:

1. **Key pre-pass.** Stream the raw child source once, extracting only
   `(__decoy_row_nr, __decoy_fk_join_key, __decoy_src_<i>)` per FK edge into a
   staged spillable form (a DuckDB TEMP TABLE of keys per edge, mirroring
   `_join.py:121` `CREATE TEMP TABLE child_keys` -- child keys are O(child) but
   spillable to `temp_directory`; no masking runs here). For a child with N
   incoming edges, one pass stages all N key sets. `__decoy_row_nr` is assigned
   positionally over the source in this pass.

2. **One streamed join per edge.** For each edge, run the single
   `LEFT JOIN child_keys x parent_keys(read_parquet VIEW) ORDER BY __decoy_row_nr`
   and expose the result as a bounded `to_arrow_reader(batch_rows)`. FAIL policy
   runs its anti-join count first and raises before any output (as `_join.py:126-140`
   does). This yields, per edge, a row_nr-ordered reader of the FK column's new
   values (plus the match indicator and src passthrough that
   `_append_output_batch` needs). The parent hash is now a spillable grace-hash
   join build under `memory_limit`, not a pinned table.

3. **Mask pass, zipped to sink.** Stream the raw child a second time; the
   existing mask stage masks the non-FK columns per batch. Drive emission from the
   mask stream: for each masked batch of `num_rows`, a **forward-only,
   row-aligned cursor** over each edge's ordered FK reader supplies exactly
   `num_rows` FK values, slicing across FK-reader batch boundaries as needed
   (mask-stream batch sizes and the join reader's `batch_rows` batches need not
   align, e.g. at parquet row-group boundaries). At most one FK-reader batch per
   edge is resident at a time, so memory stays bounded regardless of alignment.
   Overwrite the FK column(s) via the existing replace logic and emit to the sink.
   `_append_output_batch` applies orphan policy per FK-reader batch, verbatim.

Masking strategies run exactly once (phase 3 only): phase 1 is pure key
extraction. The cost is two *reads* of the source bytes, not double masking.

### Correctness backbone: the `__decoy_row_nr` ordering invariant

Both the per-edge join output (phase 2, `ORDER BY __decoy_row_nr`) and the mask
stream (phase 3) are strictly row_nr-ordered, with row_nr assigned positionally
over the source. The zip aligns row-for-row iff the source re-reads in identical
row order across phases 1 and 3. Parquet/LazySource reads are order-deterministic;
the parity oracle catches any violation. This invariant must be asserted
explicitly (e.g. a guard that the zipped batch's expected row_nr range matches
the reader's), not left implicit.

### Two subtleties to preserve exactly

- **Typing (fixed schema vs observed chunks).** The sink path has a *fixed*
  output schema decided before it sees all chunks; the resident path derives the
  final column type from observed chunk types (the documented narrowing
  divergence). `ChildFkBatchJoiner` handles this via `output_types` /
  `observed_types` (`_batch_join.py:172-187`). fix#1 must reproduce today's
  *sink-path* typing byte-for-byte -- keep the fixed-schema behavior and the
  `observed_types` accounting; do not accidentally adopt the resident path's
  value-derived narrowing.
- **Orphan policies.** FAIL (total anti-join precount, raise before any output),
  REMAP (values minted per result batch, batch-local row numbering, never sized
  by total child cardinality), PRESERVE/WARN (keep `fk_key_value`-normalized
  source key; WARN emits one `QualityWarning`). All must route through the
  unchanged `_append_output_batch` and produce identical bytes and warnings.

### Memory model update (must track the mechanism)

`_memory_estimate.py::predict_ooc_build_floor_bytes` currently prices the pinned
relation at ~190 bytes/parent-row. With the TEMP TABLE gone, the per-edge
resident floor drops to the spillable grace-hash join's bounded working set plus
batch-sized Python conversions. The floor estimate MUST be retuned to the new
mechanism, or the preflight will over-estimate and needlessly warn/fail. This is
part of the change, not a follow-up: the memory model and the execution must stay
in sync (root-cause discipline). Re-derive the coefficient from the same
first-principles basis the current one uses, and validate against the GCP re-run.

## Interfaces / units

- Replace `ChildFkBatchJoiner` (per-batch join against materialized parent) with a
  streaming joiner that: constructs from `(edge, parent_relation, temp_dir,
  memory_limit, ...)`, stages child keys once, runs one join, and exposes an
  ordered bounded reader of FK output for the zip. Prefer a small, single-purpose
  unit with a clear interface (stage_keys / open_ordered_reader / close) that the
  runner drives; keep the DuckDB SQL and `_append_output_batch` reuse identical to
  `_join.py`. Keep it under the ~600 LOC orchestration cap; if `_runner.py` grows,
  factor the three-phase driver into its own helper module.
- `_stream_table` (`_runner.py:247`) becomes the three-phase driver. `_open_joiner`
  (`_runner.py:436`) and the rewrite loop (`_runner.py:373-378`) are replaced.
  `emit_to_sink` and the downstream `build_parent_key_relation_aligned` (this
  table as a *parent* to its own children) are unchanged.

## Error handling

- FAIL orphan policy raises `orphan_fk_error` before any output for the edge
  (unchanged semantics).
- Missing child column, remap-values-missing, and the existing fail-closed OOC
  errors keep their current codes and messages.
- Disk/temp preflight (`check_temp_disk_budget`, `check_disk_spill_preflight`) and
  the memory preflight (`enforce_ooc_memory_preflight`) still run; the memory
  preflight uses the retuned floor estimate.
- The row_nr alignment guard raises a fail-closed internal error (not silent
  truncation) if the zip ever sees a mismatched row_nr range.

## Testing / validation (the merge gate)

1. Byte-identical output: `tests/parity/test_out_of_core_fk_parity.py` (pandas
   oracle) plus composite-FK, orphan (FAIL/REMAP/PRESERVE/WARN), partial-null, and
   the SC2/CF2 gate cases; `test_out_of_core_routing_parity.py`,
   `_group_b_parity`, `_group_c_parity`; the DE-10 RI family
   (`test_de10_fk_lossless_typing`, `_chunked_fk_declared_dtype`,
   `_chunked_fk_passthrough`); `test_orphan_fk`, `test_orphan_fk_policy_dedup`;
   `test_out_of_core_batch_join` / `_relation` (update to the new joiner);
   `test_out_of_core_join_chunked`. All green, byte-identical.
2. New tests: high-fan-in child (N edges) memory-shape assertion; the row_nr
   alignment guard; the two-pass source re-read determinism; the retuned floor
   estimate.
3. Module-size sentry, ruff (repo-wide), mypy, full CI.
4. dennis (Opus) adversarial gate -> when green, Codex final gate.
5. GCP re-run at 200M@8GB proving `completed=true` with a flat floor (the 16GB/8GB
   before-numbers are the baseline). Empirical proof the floor stopped rising.

If parity or any gate fails, PARK and hand back rather than force it (roadmap rule).

## Rollout

Clean replacement, no runtime flag (Cam, 2026-07-21). Pre-GA (hard-delete, no
compatibility contract yet) so a revert is cheap. One PR, one branch
(`feat/ooc-b-single-streaming-join`).
