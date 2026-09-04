# P4-A.1: FK-4a join-free self-mask on the out-of-core route

Status: plan

> Part 2 Phase 4, slice P4-A (FK streaming), sub-slice 1. Design doc:
> `docs/plans/2026-08-31-part2-phase4-plan.md`. Ledger: auto-memory
> `decoy-engine-efficiency-plan.md`. Phase 4 merges once at the end after all
> testing; this slice is built and held, not merged.

## Gate history and the narrowed, provably-correct scope (READ FIRST)

Two Codex plan-gate rounds (2026-09-01) reshaped this slice. Round 1 caught the
`_batch_remap_values`/`fk_key_value` reuse bug (join-path normalization vs the
parent's raw masking). Round 2 (NO-GO) established that FK-4a-in-OOC is narrower
and more intricate than "reuse the chunked gate", on five points that now scope
the slice:

- **Strategy allowlist, not `CHUNK_SAFE_STRATEGIES`.** The OOC `mask_column`
  kernel supports a subset, and some safe-looking strategies are not pure
  functions of (seed, namespace, raw value, config): FPE's tweak is the column
  name, so a parent `id` and child `customer_id` diverge unless an identical
  non-empty `fpe_join_group` makes the tweak column-independent; date_shift,
  bucketize, top_code are not in the OOC kernel. The provably-safe intersection
  for the FIRST cut is **hash** (canonical FK key strategy) plus the trivial
  **redact/truncate/passthrough**. FPE is deferred to a follow-up that proves
  the join-group tweak; date_shift/bucketize/top_code wait on OOC kernels.
- **Identity-`fk_key_value` key types only.** The oracle normalizes a REMAP
  orphan through `fk_key_value` before masking (`fk_key_value(True)=1` ->
  `hash(1)`); the OOC join path matches that. Raw self-masking gives `hash(True)`
  for a matched row (correct, = parent) but the SAME `hash(True)` for an orphan
  (wrong, oracle wants `hash(1)`). A join-free path applies one transform and
  cannot tell matched from orphan, so it is oracle-correct only when
  `mask(raw) == mask(fk_key_value(raw))` for every key, i.e. `fk_key_value` is
  identity: string, bytes, integer, date/timestamp, non-integral numerics.
  **Reject bool, whole-valued float, whole-valued decimal** (condition (f)'s
  family check does NOT exclude these; bool/bool passes it).
- **Exact Arrow type, not dtype family**, for representation-sensitive
  strategies (truncate, passthrough): timestamp timezone, dictionary vs plain
  encoding, and integer width all fold into one family but stringify/preserve
  differently. Require exact effective type on both FK key columns.
- **Seam rework.** The parent key relation is built in `_emit.py` when the
  parent table streams, before the child joiner is constructed, so skipping it
  requires classifying every edge BEFORE streaming and skipping the relation
  build only when ALL of a parent's outgoing edges are join-free. The applier
  must implement the full surface `_runner.py`/`_emit.py` consume, including
  `observed_types` (the no-sink `assemble_resident` and the child-as-parent
  `_relation_masked_types` read it). Classification runs on the actual source
  schemas the OOC entrypoint has (the compiled `Plan`'s `ColumnSeed` carries no
  declared dtype), not the validated-config `gate_fk_child_edges` signature.
- **Seed authority: the child `ColumnSeed`.** Conditions (b)/(c)/(e)/(f) force
  every value-affecting field of the child seed equal to the parent's, and for
  the allowlisted strategies none depends on column name or position (that is
  precisely why FPE is excluded). Hazard 3's earlier "parent seed" note is
  superseded.

Net: the first cut is **hash + trivial strategies, identity-`fk_key_value`
exact-typed keys, `orphan_policy=remap`, single-column edges**, with a
schema-based edge classifier and a full-surface join-free applier. Everything
outside that admits nothing new and takes today's join path unchanged.

## Provenance and the reframing that scopes this slice

A recon pass over the live out-of-core (OOC) FK route (`execution/out_of_core/`)
established that the current path is **already process-memory-bounded**:

- `ChildFkBatchJoiner` (`_batch_join.py`) joins **one child batch at a time**
  against the parent key relation, with a per-batch `ORDER BY __decoy_row_nr`
  that is bounded by batch size, not child cardinality.
- Parent-key dedup (`_relation.py`) is a `max(row_nr) GROUP BY` (a scalar
  aggregate DuckDB bounds) plus a join-back, holding only fixed-size spillable
  state.

The unbounded `ORDER BY __decoy_row_nr` external sort that produced the 200M@8GB
21 GB peak was a **different, reverted architecture** (the OOC-B single
streaming join, PR #107 merged / #108 reverted). Main was deliberately reverted
to today's bounded `_batch_join.py`. The external-reorder reland
(`docs/plans/2026-07-22-ooc-b-external-reorder-implementation.md`) is a large,
DuckDB-version-pinned effort; it is **out of scope here** (sub-slice P4-A.3).

What the OOC route does **not** have, and the chunked route does, is the
**FK-4a join-free path**: when a child FK edge is a pure function of the parent
key masking, the child can reproduce the parent's masked key by masking its own
key value, with **no join at all**. Today the OOC route always opens a DuckDB
connection and joins, even for that case. This slice adds the join-free path.

### Why this is the highest-value, lowest-risk first sub-slice

- **Independent.** It needs neither the bounded external sorter (P4-A.2) nor the
  streaming-join reland (P4-A.3). It is a new admission branch plus a per-batch
  column transform.
- **Common case.** Hash/FPE parent keys under `orphan_policy=remap` are the
  typical FK masking shape.
- **Structurally never-OOM for the admitted edge.** The admitted path opens
  **no DuckDB connection**, builds **no parent key relation**, and holds only
  one batch of the child at a time. There is no join state to bound.
- **Reuses proven kernels.** The per-row self-mask is exactly the
  `_batch_remap_values` kernel `ChildFkBatchJoiner` already runs for REMAP
  orphans (`_batch_join.py:328`); this slice applies it to **every** row of an
  admitted edge instead of only the orphan positions.
- **Reuses proven admission logic.** The admission conditions are the chunked
  route's `gate_fk_child_edges` (`_chunked_fk.py:212`), already Codex-gated and
  mutation-graded across three prior slices.

## Goal and the byte-parity contract

For an FK child edge admitted under the FK-4a conditions, the OOC route replaces
the child FK column by masking the child's own key value per row
(`F(seed, old_fk)`), with no join. Output MUST be **byte-identical to the
existing join path**, which is itself byte-identical to the pinned pandas oracle
(`tests/parity/test_out_of_core_fk_parity.py`). Where byte-identity cannot be
guaranteed from config alone, the edge is **not admitted** and falls through to
today's join path unchanged. A compatibility rejection beats byte drift.

Non-admitted edges, non-FK columns, the relation build for outgoing edges, and
every other route behavior are untouched.

## The mechanism: the OOC join-free path IS the chunked FK-4a self-mask, streamed

The correct model, and the one this slice builds: an FK-4a-admitted OOC edge
masks the child's **raw** key value with the child column's own `ColumnSeed`,
through the identical kernel dispatch the parent relation already uses
(`hash_array` for hash, `mask_column` otherwise, over the raw filtered key,
`_relation.py:116-130`). This is byte-for-byte the same operation the **chunked
FK-4a route** performs on that child column, so the OOC join-free output equals
the chunked FK-4a output, which is already proven byte-identical to the pandas
oracle across three landed slices. There is no new byte-parity surface.

Why matched rows need no join: the parent stored `M = strategy(seed, ns, raw
old_pk)` (raw, no normalization). The child's raw FK value equals the parent's
raw key for a matched row, and admission condition (f) forces the two FK key
columns into the **same dtype family**, so the child masking its own raw value
with the same strategy/seed/namespace/provider_config produces the identical
bytes `M`. Referential integrity holds without observing the parent. Orphans
under REMAP get `strategy(seed, ns, raw old_fk)` too, the same self-mask the
chunked route applies to an orphan, which is oracle-parity there.

### The kernel that must NOT be reused: `_batch_remap_values`

`_batch_remap_values` (`_batch_join.py:328`) normalizes each key through
`fk_key_value` **before** masking. That normalization is a **join-path
artifact**: the join path canonicalizes join keys so a bool parent key and a
0/1 int child key collide in the parent map (`fk_key_value` docstring), and its
REMAP-orphan minting reuses that normalized value. The parent's stored masked
value is `strategy(raw)`, never `strategy(fk_key_value(raw))`. So reusing
`_batch_remap_values` for a matched row diverges from the parent whenever
`fk_key_value` is non-identity: it maps bool `True` to `1`, whole-valued float
`1.0` to `1`, whole-valued decimal to int. A live kernel probe confirms the
divergence: `hash(True)` != `hash(1)`; `truncate(True)` = `"True"` but
`truncate(1)` = `"1"`; `truncate(1.0)` = `"1.0"` but `truncate(1)` = `"1"`. The
join-free path must mask the **raw** value (mirroring the parent and the chunked
route), and admission condition (f) already forecloses the cross-family case
(`chunked_fk_child_key_dtype_mismatch`) that would otherwise let a parent-int /
child-bool edge through: with same-family raw masking on both sides, `hash(True)`
meets `hash(True)`, never `hash(1)`.

## Admission conditions (port of `gate_fk_child_edges`, OOC-specific)

An OOC incoming edge with child table `T` is FK-4a-admitted only when ALL hold
(same lettering as `_chunked_fk.py:212`):

- (a) parent key strategy in `CHUNK_SAFE_STRATEGIES`.
- (b) child FK column explicitly declares the same value-keyed strategy.
- (c) for `NAMESPACE_REQUIRING_STRATEGIES` (hash, fpe, date_shift) only: child
  namespace and parent-column namespace both present and equal; parent-column
  namespace is authoritative (`ColumnSeed.namespace`); mismatch or missing =>
  reject. Namespace-agnostic strategies skip this sub-check.
- (d) `orphan_policy == 'remap'`.
- child `provider_config` equals the parent's (same value-affecting settings).
- parent and child FK key dtype families match, and both are declared
  (value-sensitive strategies cannot prove identical masked values otherwise).
- single-column edge only; composite FK not admitted (falls to join path).

Reusing the chunked collector verbatim is preferred over re-deriving. The
production function `gate_fk_child_edges` currently **raises** on failure (fail
closed for the chunked route, where non-admission means the job cannot run
chunked). The OOC route needs **predicate** semantics instead: a non-admitted
edge is legal and simply uses the join path. So this slice factors the
per-edge decision into a reusable classifier returning admit / reject-reason
(the chunked raising wrapper keeps its behavior by raising on the same reason).

## Byte-parity hazards to resolve in the plan-gate (author's flagged risks)

These are the traps every prior slice hid one of; the plan-gate must clear each:

1. **Output dtype uniformity.** With raw-masking, every row (matched and orphan)
   is `strategy(seed, ns, raw child key)`, one uniform Arrow type:
   `masked_output_type(child_seed, child_raw_dtype)` (`_mask.py`), which equals
   the parent relation's stored masked type because both sides run the same
   strategy over the same dtype family. The join-free applier fixes this single
   type up front from the child seed, not by unioning candidate types. Do NOT
   route through `_resolve_output_types` (`_batch_join.py:365`): that function
   unions the parent masked-key type with the `fk_key_value`-normalized REMAP
   type, which is the join path's mixed-type contract and reintroduces the
   normalization this slice must avoid. The plan pins the fixed type equals the
   parent relation's masked type for every admitted shape (Task 4), so a
   streaming sink and the no-sink reassembly both see the parent's type.
2. **RESOLVED: `fk_key_value` normalization must not be applied.** The initial
   draft reused `_batch_remap_values`, whose `fk_key_value` normalization is a
   join-path artifact that diverges from the parent's raw masking for bool,
   whole-valued float, and whole-valued decimal keys (probe: `hash(True)` !=
   `hash(1)`; `truncate(1.0)` = `"1.0"` != `truncate(1)` = `"1"`). Root cause:
   the parent stores `strategy(raw)`, so the child must also mask `raw`. Fix:
   the applier masks the raw child key via the child `ColumnSeed`, the same
   kernel dispatch `_relation.py` uses for the parent; admission condition (f)
   (`chunked_fk_child_key_dtype_mismatch`/`_unprovable`) already rejects the
   cross-family case that would make raw masking diverge. A structural test
   asserts the applier never calls `fk_key_value` or `_batch_remap_values`
   (Task 6).
3. **Namespace authority.** The parent-column namespace, not the edge namespace,
   is the masking key (`_seed_envelope.py:260`). The OOC admission must read the
   parent **column** seed's namespace, matching the chunked gate, and must use
   the parent column's `ColumnSeed` (not the child's) to mint values so the
   namespace is provably the parent's.
4. **Composite-FK scalar-child interaction.** The OOC route already rejects the
   SC2/CF2 composite-scalar shape
   (`out_of_core_composite_fk_scalar_child_unsupported`, `_batch_join.py`
   docstring). FK-4a admits single-column edges only, so it never overlaps that
   shape, but the plan-gate confirms the new branch is unreachable for composite
   edges (they must reach the existing rejection or the join path, never the
   self-mask path).
5. **Orphan reporting parity.** The join path counts orphans (WARN totals, FAIL
   raising). Under `orphan_policy=remap` there is no FAIL/WARN reporting to
   preserve, but the plan-gate confirms REMAP today emits no orphan-count side
   effect the join-free path would drop (e.g. metrics, logs). If REMAP does
   surface an orphan count anywhere, the join-free path must reproduce it or the
   edge is not admitted.
6. **Multi-edge overlap on a shared child column.** `_runner.py` phase 1 lets
   several incoming edges overwrite the same child column, each keying off the
   immutable raw batch. The join-free branch must key off the same immutable raw
   batch and write into the output batch with identical last-write-wins ordering
   as the join path, so a table with one join-free and one join-path edge on
   overlapping columns is byte-identical to all-join-path today.
7. **Orphan REMAP parity anchor is the chunked route, not the OOC join path.**
   For a key whose type makes `fk_key_value` non-identity but whose parent and
   child share that dtype family (admitted), the raw-masking join-free path and
   the `fk_key_value`-normalizing OOC join path produce **different** orphan
   values. They cannot both be oracle-parity. The chunked FK-4a route
   raw-self-masks orphans and is the proven oracle reference, so the join-free
   path (also raw) matches the oracle; the OOC join path's orphan handling for
   such keys is a separate, pre-existing question this slice does not touch.
   Task 4 therefore anchors acceptance to the **oracle** and to the **chunked
   FK-4a route**, and asserts join-path parity only where `fk_key_value` is
   identity (string, bytes, int, date/timestamp, non-integral numerics). A
   direct test builds a REMAP edge with orphans on a same-family key type where
   `fk_key_value` would normalize, proving raw-masking is oracle-correct and the
   old `_batch_remap_values` reuse would have diverged.

## Route seam

`_runner.py` phase 1 constructs one `ChildFkBatchJoiner` per incoming edge and
calls `join_batch` per source batch. The seam: at joiner-construction time,
classify each incoming edge; for FK-4a-admitted edges, substitute a lightweight
per-batch self-mask applier (same `join_batch(batch, key_source=...)`
signature and `output_types` property, so the phase-1 loop is unchanged) that
runs `mask_column` over the fixed output type and never opens a connection.
Admission is computed once per edge (not per batch). The applier reuses
`_resolve_output_types` and `_replace_fk_columns` from `_batch_join.py`.

## Tasks

- [ ] **Task 1: Classifier.** Factor the per-edge FK-4a decision out of
  `gate_fk_child_edges` into a predicate that returns admit / reason, keeping
  the chunked raising wrapper behavior-identical (it raises on the same reason).
  Unit tests: every reject reason, plus a positive admit, mirrored from the
  chunked gate's existing tests. No behavior change to the chunked route
  (re-run its admission tests green).
- [ ] **Task 2: Join-free applier.** A per-batch self-mask applier with the
  `ChildFkBatchJoiner` construction/`join_batch`/`output_types`/`close`
  surface, opening no DuckDB connection and building no relation. Per-batch
  values: mask the **raw** child key via the child `ColumnSeed`, using the same
  kernel dispatch `_relation.py:116-130` uses for the parent (`hash_array` for
  hash, `mask_column` otherwise). Do NOT call `_batch_remap_values` or
  `fk_key_value` (hazard 2). Output type: fix one type up front from the child
  seed (`masked_output_type(child_seed, child_raw_dtype)`), NOT via
  `_resolve_output_types` (hazard 1). Column replacement via `_replace_fk_columns`
  (reused unchanged). Empty-batch and null-key handling: a null child FK key
  masks to null under both routes (kernels are null-preserving); pin it matches
  the chunked route.
- [ ] **Task 3: Route seam.** Classify edges once in `_runner.py` phase 1;
  route admitted edges to the applier, all others to `ChildFkBatchJoiner`
  unchanged. No change to non-FK masking, relation build, or the no-sink
  reassembly.
- [ ] **Task 4: Byte-parity tests.** Extend
  `tests/parity/test_out_of_core_fk_parity.py` so every FK-4a-admissible shape
  (hash, fpe, date_shift with namespaces; the namespace-agnostic strategies;
  each with orphans present under REMAP) asserts the admitted (join-free) output
  is byte-identical to the **pandas oracle** (hard gate) AND to the **chunked
  FK-4a route** on the same inputs (the tightest anchor; both raw-self-mask).
  Assert the fixed output type equals the parent relation's masked type (hazard
  1). Include the hazard-7 case: a same-family key type where `fk_key_value`
  would normalize (so the pre-fix `_batch_remap_values` reuse would have
  diverged), with orphans under REMAP, proving raw-masking is oracle-correct.
  Include a multi-edge overlap case (hazard 6) and a mixed join-free +
  join-path table. Join-path parity is asserted only for shapes where
  `fk_key_value` is identity (hazard 7); elsewhere the oracle is the reference.
- [ ] **Task 5: Admission-boundary tests.** Each non-admitted shape (wrong
  strategy, config mismatch, dtype-family mismatch, undeclared dtype, namespace
  mismatch/missing, non-remap policy, composite) provably takes the join path
  and stays byte-parity to the oracle (no regression, no accidental admission).
- [ ] **Task 6: Memory + no-normalization evidence.** A test asserting an
  admitted edge opens no DuckDB connection and builds no parent relation
  (structural never-OOM: spy/patch that `connect_duckdb` /
  `build_parent_key_relation_aligned` are not called for the admitted edge), and
  a structural test asserting the applier never calls `fk_key_value` /
  `_batch_remap_values` (hazard 2 regression guard). This is the always-on proof
  for this slice; no large-N benchmark is needed because the admitted path holds
  no row-count-scaled state by construction.
- [ ] **Task 7: Module-size + lint + mutation.** Keep new logic in free
  functions (mutmut decorated-class limitation). Mutation-grade the classifier
  and the applier's admission/type logic to the 0-unresolved-logic bar; prose
  survivors adjudicated per the established ledger policy. ruff + mypy on the
  diff. Respect the module-size sentry (new module if `_batch_join.py` or
  `_chunked_fk.py` would exceed ceiling).

## Non-goals (explicitly deferred)

- The bounded external sorter generalization (`BoundedExternalSorter`) —
  P4-A.2.
- The streaming-join / external-reorder reland for the non-function (4b) case —
  P4-A.3. Today's bounded per-batch join remains the 4b path.
- Forcing LazySource / sink for the residual resident-source and no-sink
  reassembly residency — tracked separately; not required for this slice's
  never-OOM claim, which is scoped to the admitted edge's FK handling.
- Any route-threshold or planner change. FK-4a is a within-route optimization;
  the OOC route is still chosen by the existing thresholds.

## Acceptance

- Byte-identical to the pandas oracle AND to the chunked FK-4a route on every
  admitted shape (Task 4); join-path parity where `fk_key_value` is identity.
- Every non-admitted shape unchanged and still oracle-parity (Task 5).
- Admitted edge opens no connection / builds no relation (Task 6).
- Chunked route admission behavior unchanged (Task 1).
- 0 unresolved-logic mutation survivors on new logic; ruff + mypy clean; sentry
  green (Task 7).
- Held on `feat/native-phase3`; no merge.
