# Masking relationships under memory constraints

**Status:** Phase 0 (measurement) + Option 2 (`run_sequential`) landed 2026-06-30; see section 6. Next: wire a lazy per-table loader into `run_pipeline`/the platform job runner so production jobs take this path.
**Audience:** Engine tech lead / PO.
**Scope:** How to mask FK-related tables (referential integrity preserved) without holding every table full-frame in RAM.
**Review:** Adversarially reviewed (Dennis, 2 BLOCKER / 3 MAJOR / 3 MINOR). All findings folded in below; the §1 lever and Option 1 were corrected substantially, and the §4 sequencing was flipped to ship Option 2 first. See §5 for the review trail.

---

## 1. Problem statement

When a config declares `relationships`, the engine takes its most memory-hungry path:

- **Chunked/streaming execution is rejected outright** for any config with relationships
  (`src/decoy_engine/execution/_chunked.py:155-164`, error `chunked_relationships_unsupported`).
  The code's stated rationale, "resolving a child key reads the parent's complete source->masked
  map, which needs the whole parent frame", is itself slightly overstated: it needs only the
  parent **key** columns, not the whole frame (see §1.2).
- **All tables load full-frame, simultaneously.** The adapter builds `frames` from a
  **fully-materialized** `sources: Mapping[str, pa.Table]` handed in by the caller
  (`_pandas_adapter.py:157`), masks in FK-topological order, and only converts back to Arrow at
  the end. The `for node in ordered` loop (`:198-254`) never evicts a finished parent table.

The practical ceiling: the v2 perf baseline **deferred the 10M-row tier specifically for memory
reasons**, "a 10M-row run requires more memory than a standard dev box (estimated 5-10 GB Parquet
load + multi-column faker)" (`docs/v2/perf/engine-v2-baseline-report.md:90-91`). On a wide
healthcare schema with 3 related tables you are realistically looking at 10-20 GB+ resident, which
OOMs a typical dev/CI box before throughput is even the constraint.

### 1.1 What actually consumes the memory

Two independent costs, and only one is fundamental:

1. **Full-frame, all-tables-at-once load**, every column of every table resident together,
   because the adapter receives an already-materialized mapping of all sources. This is the
   dominant cost and the one worth attacking.
2. **The FK source->masked map**, `_resolve_fk_node` / `_parent_map` build a
   `dict[_KeyTuple, _KeyTuple]` from the parent. It already snapshots **only the key columns**
   (`source_snapshots` keyed on `parent_cols`, `_pandas_adapter.py:176-184`), so the map is narrow.
   But a 10M-entry Python tuple->tuple dict is still ~1-2 GB on its own.

### 1.2 The determinism lever and the by-reference reality that limits it

Masking is keyed on `(seed, namespace, source_value)` via HKDF->HMAC
(`src/decoy_engine/determinism/_derive.py:210-252`). The `job_seed` is **config-derived and
data-independent** (`plan/_seed.py:51-103`, defaults to 0), so the same `(seed, namespace, value)`
reproduces the same masked output across processes and passes. That determinism *is* the
referential-integrity mechanism.

**The tempting conclusion, and why it is only half true.** One might assume a child FK column can
"mask itself" via the keyed hash and never read the parent. But that is **not how the engine masks
FK children today.** An FK child node is resolved purely by **parent-map lookup** and then
`continue`s, *skipping its own strategy handler entirely* (`_pandas_adapter.py:202-215`). The child
column is masked **by reference** to the parent's source->masked map, not by running a strategy.
`docs/relationships.md` confirms the model: a child column carries the parent's `namespace`, but
its masked value comes from FK resolution, not from independently re-deriving it.

So the determinism lever is real but conditional. A child FK column *could* be made to self-mask
(and thus stream independently) **only if all of these hold**:

- (a) the FK **key** columns use a value-deterministic strategy (the `CHUNK_SAFE_STRATEGIES` set,
  `_chunked.py:70-81`);
- (b) the **child** column independently declares **the same value-keyed strategy** as the parent;
- (c) the **child** column declares **the same namespace** as the parent
  (today this is auto-bound by walking `profile.relationships` in
  `relationships/_namespace.py:225-253`, a relationship-less chunked profile never runs that
  binding); **and**
- (d) **orphans are handled without the parent key set** (see the orphan trap below).

Miss (b)/(c) and the child streams out either wrong or, if it has no strategy of its own, **raw,
leaking the unmasked FK key (PII) while the parent is masked, and breaking every join.** Miss (d)
and the byte output diverges from the full-frame path.

> **Exclusion that applies to every option:** `shuffle` (and any non-value-keyed strategy) on an FK
> key column cannot be reproduced independently, it is a permutation, not a keyed map, so it
> forces a parent-resident path regardless. `_chunked.py` already rejects `shuffle` as
> `strategy_not_chunk_safe`.

### 1.3 The orphan trap (this is what makes "just stream the child" unsafe)

Orphan handling lives in `resolve_fk_keys` (`_strategies/_orphan.py:37-102`). An orphan is a child
key with **no matching parent**, detected by `parent_map.get(key) is None`, which **requires the
full parent key set in memory.** The policies:

- `FAIL`, abort if any orphan exists.
- `REMAP`, mint a fresh masked value for the orphan by running the **parent column's keyed
  strategy** over it (`_pandas_adapter.py:374-411`).
- `WARN` and **`PRESERVE`**, **keep the orphan's source key unmasked** (`_orphan.py:99`: "PRESERVE
  and WARN both keep the source key unmasked").

The consequence is counter-intuitive and load-bearing: **`preserve` is the policy that most needs
the parent set**, because telling "orphan -> keep raw" apart from "legit -> masked value" *is* the
parent-set lookup. A child pass that never reads the parent cannot make that distinction and would
mask every key, including the orphans full-frame leaves raw, producing **different bytes** from
full-frame and silently changing documented `preserve` semantics. Any streaming-FK design has to
confront this explicitly; it is not a free case.

---

## 2. The five options

Ordered roughly cheapest-to-build first. They are **complementary**, not mutually exclusive, see
§4 for sequencing. Every option below assumes the cross-cutting precondition that **the same config
and seed are used across every pass/table/shard** (the determinism invariant from §1.2); this holds
today because `job_seed` is config-derived, and would silently break RI if a future seed source ever
became data-dependent.

### Option 1: Streaming FK children via self-masking (NOT just "lift the gate")

**Idea.** Allow chunked/streaming execution for related tables by making FK child columns
**self-mask** (run their own value-keyed strategy under the inherited namespace) instead of
resolving by parent-map lookup. Determinism then reproduces the parent's masked key without the
parent resident.

**This is a behavioral change to child-column masking, not an admission-gate tweak.** A naive lift
of the `_chunked.py:155-164` ban does **not** work: the chunked path hardcodes an empty relationship
graph (`_chunked.py:291`) and a relationship-less profile (`_chunked.py:238`), so no node is treated
as an FK child and the namespace auto-binding (`_namespace.py:225-253`) never runs. The child would
mask via whatever its own column config says, and if it declares no strategy (the by-reference
model), it streams out **raw FK keys (PII leak)**.

To be correct, Option 1 must:
1. Gate per FK edge on: (a) key strategy in `CHUNK_SAFE_STRATEGIES`, (b) child declares the same
   value-keyed strategy as the parent, (c) child namespace == parent namespace;
2. Thread the **real** relationships into both the chunked profile and the namespace registry so
   inheritance is enforced, not bypassed; and
3. Resolve the orphan problem (§1.3): admit only configs where orphans are **structurally
   impossible** (e.g. a validated clean join, or `FAIL` having already passed on the full dataset), **`preserve` does not qualify**, or explicitly redefine chunked-FK orphan semantics and accept
   a documented byte-parity exception to the chunked contract (`_chunked.py:5-6`).

**Pros.**
- Still the smallest *new code surface* of the streaming options once scoped correctly.
- Fully consistent with the determinism contract for the configs it admits.
- Removes the hard cliff for the common, simple case (deterministic FK keys, clean joins).

**Cons.**
- **Not** "a gate change, no new execution path", it changes how FK children are masked and must
  thread relationships through the chunked profile + namespace registry.
- Narrow and footgun-prone: every additional gate condition (b)/(c)/(d) is a place where an
  un-checked config silently leaks raw PII or diverges from full-frame output. The gate must be
  conservative and fail **closed** with a clear `PlanCompileError`.
- Does nothing for configs using `remap`/`warn`/`fail` or non-deterministic key strategies.

**Effort.** Small-Medium (S/M) once the gate + orphan decision are settled (the original "S" was
optimistic). **Memory ceiling after:** one chunk per admitted table.

---

### Option 2: Sequential table processing with eviction  *(recommended first step)*

**Idea.** Stop holding every table's full width at once. Process tables in FK-topological order:
load -> mask -> write one table, then **evict its wide columns**, retaining only the narrow
**key-column** source->masked maps that downstream children still need. FK children resolve by
lookup against those retained maps exactly as today, so **all orphan policies keep working**.

**Where / scope (larger than a loop refactor).** The all-tables-resident cost is the
fully-materialized `sources` mapping the adapter receives (`_pandas_adapter.py:157`). Sequential
eviction therefore requires changing the adapter's **input contract**, a lazy per-table loader
instead of a materialized `Mapping[str, pa.Table]`, and the multi-table caller, plus the
incremental-output path. This touches the adapter's public `run()` signature, not just the internal
dispatch loop.

**Pros.**
- Keeps **full orphan-policy support** (`remap`/`warn`/`fail`/`preserve`), no determinism-precondition
  gymnastics, no PII-leak surface.
- Strategy-agnostic for non-key columns; no restriction on what the bulk of each table uses.
- No new external dependency, no on-disk format.

**Cons.**
- Memory still scales with the **largest single table** plus the retained parent **key** maps; a
  10M-row key dict is still ~1-2 GB. Real improvement, but bounded.
- Changes the adapter input contract and a public signature; eviction ordering must guarantee a
  child is never resolved after its parent map was dropped (multi-parent / composite-FK / diamond
  graphs make this non-trivial).
- Incremental output ordering may interact with whole-run invariants (quality metrics, timing).

**Effort.** Medium (M). **Memory ceiling after:** largest table + retained parent key maps.

---

### Option 3: Spill the parent key map to disk

**Idea.** When orphan enforcement is required at scale, run a parent pass that emits only the
`source->masked` **key map** to an on-disk KV store (LMDB / SQLite / a sorted parquet sidecar), then
stream the child in chunks and look each FK key up against the on-disk map. The map is sized by
parent key cardinality, not parent width or RAM.

**Pros.**
- Preserves **full orphan policy** at effectively unbounded parent size, memory is one chunk + the
  map's page cache.
- Decouples parent width from the join (only key columns touch the map).
- Opt-in backend; existing in-memory path stays the default for small jobs.

**Cons.**
- New moving part: embedded KV dependency (LMDB) or a hand-rolled sorted-sidecar merge-join, plus
  temp-file lifecycle / crash-safety.
- Random-access disk lookups are slower than a Python dict (mitigable by sorting child by key and
  merge-joining).
- I/O correctness surface that must stay byte-identical to the in-memory result, including the
  **int/float key-normalization hazard**: `_fk_key_value` (`_pandas_adapter.py:69-81`) already
  collapses an int64 parent key `123` and a float64-because-null child key `123.0` to the same key.
  The on-disk map must apply that normalization **before** serialization or real matches are missed.
  Null and composite-key serialization must match too.

**Effort.** Medium-Large (M/L). **Memory ceiling after:** one chunk + map page-cache, regardless of
parent size.

---

### Option 4: Out-of-core backend (DuckDB / polars-lazy)

**Idea.** Express the FK remap as a **join over on-disk Parquet** in an engine that spills to disk
natively. The deterministic masking transforms become keyed UDFs/expressions; the query engine owns
the join, the spill, and the memory budget. DuckDB hash-joins 10M+ rows out-of-core routinely, and
the project already standardizes on pyarrow/Parquet and is moving toward a polars substrate (v2
rewrite, S11-S12).

**Pros.**
- The genuine "10M+ across many tables" answer, arbitrary size in bounded RAM, no per-job tuning.
- Aligns with the committed polars/Parquet direction; could fold into v2 rather than be throwaway.
- The query engine also parallelizes scan + join, addressing throughput, not just memory.

**Cons.**
- Largest build and biggest correctness surface: every deterministic strategy must be reproduced
  **byte-identically** as a UDF/expression, or determinism/RI and the regression gates break.
- **Orphan policy does NOT "fall out of a join."** A LEFT join surfaces orphans (null right side),
  but `REMAP` must mint values by running the parent strategy over orphan keys
  (`_pandas_adapter.py:374-411`), `WARN` must emit one aggregated count, and `FAIL` must abort the
  query, each is a non-trivial UDF + control-flow design on top of the join. The "all policies
  kept" claim is **unproven** until that orphan lowering is scoped.
- New heavy dependency (DuckDB) or deep polars-lazy reliance; risk of two divergent masking
  implementations unless the strategy layer is refactored to a single lowering. That refactor, not
  the join, is the real cost.

**Effort.** Large (L/XL). **Memory ceiling after:** engine-managed; effectively unbounded input.

---

### Option 5: FK-shard partitioning

**Idea.** Pre-partition the key space by `hash(fk_key) % N`. For each shard, load only the parent
rows and the child rows whose FK falls in that shard, mask the shard in memory with the existing
adapter, write it, move on. A child key and its parent key hash to the same shard, so RI and full
orphan policy hold within each independently-sized shard.

**Hard precondition (narrower than it looks).** Hash-sharding co-partitions exactly **one key
spine**. A `parent -> child -> grandchild` chain where edge1's key != edge2's key cannot be sharded
by both keys at once: you can't simultaneously partition `child` by its parent key and by the key
its grandchild references. So Option 5 effectively **requires a single shared key spine across the
whole relationship graph**. The §1 "3 related tables" healthcare example is already >=2 edges and
likely fails this. Multi-parent and diamond graphs fail it too. State this as a hard precondition,
not a footnote.

**Pros.**
- Preserves RI **and** full orphan policy with bounded memory, **without** rewriting the masking
  core, each shard runs the existing full-frame adapter.
- Memory tunable directly via `N`.
- Naturally parallelizable across shards (process-level), parallelism the engine otherwise lacks.

**Cons.**
- Single-key-spine precondition rules out most multi-edge schemas (see above).
- Requires a stable hash over the FK key matching across parent and all referencing children,
  including composite keys, subtle to get right.
- Operationally heavier (temp shard files, N-way orchestration, cleanup) than an in-process fix.

**Effort.** Medium-Large (M/L). **Memory ceiling after:** one shard (tunable via N).

---

## 3. Comparison at a glance

| # | Option | Effort | Orphan policies kept | Memory ceiling | Key caveat |
|---|--------|--------|----------------------|----------------|-----------|
| 1 | Streaming FK children (self-mask) | S/M | structurally-no-orphan only (NOT `preserve`) | one chunk / table | behavioral change + PII-leak/orphan footguns |
| 2 | Sequential + eviction | M | all | largest table + key maps | changes adapter input contract / `run()` signature |
| 3 | Spill key map to disk | M/L | all | one chunk + map page-cache | KV dep + int/float key normalization |
| 4 | Out-of-core backend | L/XL | all *(orphan lowering unproven)* | engine-managed (unbounded) | byte-identical strategy lowering required |
| 5 | FK-shard partitioning | M/L | all | one shard (tunable) | requires single shared key spine across graph |

Cross-cutting: FK **key** columns masked with non-value-keyed strategies (e.g. `shuffle`) force a
parent-resident path under every option. All multi-pass options assume identical config+seed across
passes (§1.2).

---

## 4. Recommendation (for tech-lead decision)

The original draft recommended shipping Option 1 first as "provably correct, cheapest, just a gate
change." Review showed that is unsafe: Option 1 as a bare gate-lift either leaks raw FK keys (B1) or
diverges from full-frame `preserve` output (B2). Revised sequencing:

1. **Ship Option 2 first.** It is the cheapest **safe** increment: full orphan-policy support, no
   determinism-precondition gymnastics, no PII-leak surface. It clears the near-term wall for any
   job whose largest single table fits in RAM (the common case once you stop holding all tables at
   once). Budget for the input-contract change, not a loop tweak.
2. **Then Option 1**, only after it gains the child-strategy + child-namespace gate conditions and
   an explicit orphan-safety decision (structurally-no-orphan, or a documented byte-parity
   exception). It becomes a fast-path for clean-join, deterministic-key configs on top of Option 2.
3. **Option 4 (DuckDB / polars-lazy) is the durable scale story** for "10M+ across many tables,"
   ideally folded into the v2/polars rewrite so the byte-identical strategy-lowering work is done
   once. Scope the orphan-policy lowering explicitly, it does not come free with the join.
4. Hold **Options 3 and 5** as targeted fallbacks: Option 3 if a customer needs orphan enforcement
   at a scale that breaks Option 2 before Option 4 lands; Option 5 only where the relationship graph
   genuinely has a single shared key spine and process-level parallelism is the priority.

### Open questions for the tech lead

- How common are clean-join / `preserve` configs in practice? That sizes Option 1's real coverage
  after the safety conditions narrow it.
- Does the v2/polars rewrite already imply a single strategy-lowering layer? If so, Option 4's
  dominant cost may already be partially funded.
- What is the actual target scale, is 10M the ceiling or a waypoint to 100M+? Above ~50M, Options
  1/2 stop helping and 4 (or 5) becomes mandatory.

### Suggested next step

DONE (2026-06-30): the wall is now measured, not extrapolated. See section 6. The committed 10M
fixture (`tests/perf_fixtures/schema.py:165`) was the wrong shape for this (a single flat table with
no `relationships`, so it never takes the full-frame FK path); a multi-table FK fixture and a memory
probe were built instead.

---

## 6. Measurement (2026-06-30)

The §1 ceiling was a paper estimate. It is now measured. The committed perf tiers are a single flat
table and cannot exercise the relationships full-frame path, so two artifacts were added:

- `tests/perf_fixtures/fk_relational.py` - a parametrized parent to child to grandchild FK chain
  (wide payload columns, controllable orphan fraction) fed straight to `PandasExecutionAdapter.run`,
  which is the path that materializes every table at once (`_pandas_adapter.py:157`) and holds all
  outputs to the end (`:257`). This is the exact cost §1 describes and the surface Option 2 changes.
- `scripts/fk_memory_probe.py` - runs one tier per process and reports peak process RSS
  (`getrusage().ru_maxrss`, the high-water mark that decides OOM) plus `tracemalloc` peak (Python
  objects only: the FK parent maps and masked-key strings, not the pyarrow/pandas C buffers).
- `tests/perf/test_fk_memory_scaling.py` - a small committed regression net (FK integrity across the
  chain, orphan-policy behavior, a loose memory sentinel) that runs in the default gate in ~2s.

**Rows sweep** (3-table chain, 16 payload columns/table, `preserve`, 2% orphans; 8 GB Linux box,
Py 3.11, pyarrow 24, numpy 2.4):

| rows / table | peak RSS | tracemalloc peak | mask time |
|---|---|---|---|
| 100k | 474 MB | 79 MB | 17 s |
| 250k | 980 MB | 193 MB | 49 s |
| 500k | 1,818 MB | 386 MB | 94 s |
| 1M | 3,469 MB | 772 MB | 189 s |

Peak RSS is strongly linear in rows: ~3.3 MB per 1k rows-per-table (slope 3.38, 3.35, 3.30 across the
tiers), i.e. `peak_RSS ~= 144 MB + 3.3 MB * (rows / 1000)`. Implications:

- The near-term wall on this 8 GB box (about 5.3 GB free) lands at **~1.5M rows/table**.
- Extrapolated to the deferred tier, **10M rows/table is ~33 GB** resident for this 3-table width-16
  chain. That confirms, and slightly exceeds, the §1 "10-20 GB+" estimate, and explains why the v2
  baseline deferred the 10M tier on memory grounds.

**Width sweep** (250k rows/table, `preserve`): keys-only 700 MB; 8 columns 840 MB; 32 columns
1,266 MB - about 18 MB per payload column at 250k. Two takeaways: (a) memory scales with payload
width as well as rows, so the dominant cost is genuinely "every column of every table resident at
once," which is exactly what Option 2 attacks; (b) even keys-only is 700 MB, because the three Arrow
sources, their three pandas copies, and the accumulated outputs are all held concurrently and the
`hash` strategy emits a 64-char hex string per key. Holding one table plus narrow key maps (Option 2)
rather than three full frames plus sources plus outputs cuts the multiplier; the measured before/after
is below.

**Caveats.** The constant is schema-specific (width 16, `hash` strategy, equal-size tables); the
robust, schema-independent finding is the linearity in rows and the all-tables-resident multiplier.
The numbers above are single-box and indicative, not a cross-hardware budget.

### 6.1 Option 2 before/after (`run_sequential`, 2026-06-30)

`PandasExecutionAdapter.run_sequential` (load one table, mask, emit via a sink, evict its wide frame,
retain only the narrow parent key maps) measured against full-frame `run` on the same chain, via
`scripts/fk_memory_probe.py --mode full|sequential` (lazy per-table generation so the three tables
are never resident together):

| tier | full-frame peak RSS | sequential peak RSS | reduction |
|---|---|---|---|
| 250k x w16 | 980 MB | 737 MB | -25% |
| 500k x w16 | 1,809 MB | 1,322 MB | -27% |
| 250k x w48 | 1,541 MB | 1,145 MB | -26% |

A consistent **~25-27% peak-RSS reduction**, holding across both scale and width. This is a real but
**bounded** win, exactly the §2/§4 framing of Option 2: the ceiling is the largest single table plus
the retained parent key maps, not one third of full-frame, because (a) at peak the current table's
Arrow source, its pandas frame, its output, and one retained key map are all live, and (b) the key
maps and 64-char `hash` output are heavy and do not shrink. The win grows with chain length and
fan-out (more sibling/child tables full-frame would hold at once); for the deep 10M+ tier, Option 4
(out-of-core) remains the durable answer per §4. Correctness is byte-identical to `run`
(`tests/unit/execution/test_sequential_eviction.py`). Caveat: the probe's lazy generation adds a
per-table build transient, so a Parquet-backed loader in production should do at least this well.

**Review + production note (dennis, 2026-06-30).** Adversarial review confirmed byte-parity across
multi-parent, diamond, composite-FK, both-parent-and-child, and self-FK shapes (not just the 3-table
chain), and the ref-counted parent-map eviction is correct (0 blocker). One condition for production:
the `sink` path is **non-transactional on abort**. `run` is atomic (it raises before emitting any
output); `run_sequential(sink=...)` emits tables incrementally, so an orphan `FAIL` or a late
per-table guard rejection leaves earlier tables already delivered to the sink. The behavior is
documented and pinned by a test, but before `run_sequential` is wired into `run_pipeline` or the
platform job runner, the sink needs an explicit commit/abort signal (a transactional sink) so a
failed run cannot leave a partial, complete-looking dataset. That wiring + transactional-sink design
is the next deliverable; the engine API landed here is stable.

---

## 5. Review trail

Adversarial review (Dennis) returned **2 BLOCKER / 3 MAJOR / 3 MINOR / 2 NIT**, all incorporated:

- **B1** (now §1.2 / Option 1): FK children are masked **by reference** via parent-map lookup and
  skip handler dispatch (`_pandas_adapter.py:202-215`); a bare gate-lift leaks raw FK keys.
- **B2** (now §1.3): `preserve` keeps orphans **raw** and therefore **requires** the parent key set
  (`_orphan.py:99`), the opposite of the original draft's claim; gating Option 1 on `preserve` was
  backwards.
- **M1** (Option 4): orphan `remap`/`warn`/`fail` do not fall out of a join; "all policies kept"
  marked unproven pending an orphan-lowering design.
- **M2** (Option 5): requires a single shared key spine across the graph; multi-edge chains aren't
  shardable by one hash, promoted from footnote to hard precondition.
- **M3** (Option 2): not a pure loop refactor; changes the adapter input contract and `run()`
  signature (`_pandas_adapter.py:157`).
- **m1** (§2 preamble): identical-config+seed-across-passes precondition stated explicitly
  (`plan/_seed.py:51-103`).
- **m2** (Option 3): int/float key-normalization hazard via `_fk_key_value`
  (`_pandas_adapter.py:69-81`).
- **m3** (§1): corrected the "needs the whole parent frame" overstatement, only key columns.
- Nits: removed the misleading "byte-identical already proven" framing for the FK case; resequenced
  to Option 2 first.

The §1 factual claims about current behavior were verified accurate against the code during review;
the corrections above are about the *design conclusions*, not the description of today's engine.
