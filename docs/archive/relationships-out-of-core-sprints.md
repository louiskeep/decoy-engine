# Option 4 Sprint Plan: Out-of-Core Relationship Backend

**Status:** Phase 0 (backend-neutral masking kernel + FK match-key
normalization) and Phase 1 Sprints 1.1-1.3 (admission gate, parent key
relation, child left join with `fail`/`remap`/`preserve`/`warn` orphan
policies) **built and dennis-approved 2026-06-30**, CI gates green, unmerged.
The capability track (C1-C5) landed 2026-07-01/02: the route streams end to
end and **the C5 acceptance is measured green: at a 1,024 MB hard memory cap,
out-of-core completes a 400k-rows/table FK chain that OOMs both full-frame
and `run_sequential`** (`relationships-memory-scaling.md` §6.3; §8 below).
The route ships behind `PolarsExecutionAdapter(enable_out_of_core=False)`
(default off); no production behavior change. Whole-stack FK-RI
memory-scaling merge stays gated on engine PR #22. See §8 for the
implementation review and known limitations. This revision supersedes the
original 12-sprint DuckDB-over-Parquet plan. It folds in an LLM-council
architecture review (2026-06-30) and a code-grounding pass against the live
tree. The headline change: Option 4 is **not** a standalone DuckDB execution
backend. It is a **hybrid** built on three already-partly-existing pieces, a
single masking kernel, the existing polars substrate and its admission gate,
and DuckDB used only as a spill-aware relational operator behind an Arrow
boundary.

**Scope:** An out-of-core path that preserves FK referential integrity for large
relationship graphs (10M to 100M+ rows per table) without keeping full
parent/child frames or Python FK maps resident, for both **masking** and
**generation**. The product mandate is maximal capability and correctness as a
self-hostable alternative to enterprise masking/generation tools, not shipping
speed.

**Source design:** `relationships-memory-scaling.md` (the five options, the
measured memory wall, the recommendation). This doc is the implementation
breakdown for Option 4 from that doc.

**Non-scope:** Option 3 key-map spill and Option 5 FK-shard partitioning remain
held as targeted fallbacks.

---

## 0. What changed from the original plan, and why

The original plan framed Option 4 as `execution/out_of_core/_duckdb.py` that
"binds strategies to DuckDB SQL/UDFs," with a fresh admission gate, hash-only
first, generation out of scope. The review found four framing errors. Each one
is corrected below and drives the new phase structure.

1. **Deterministic strategies cannot be native SQL or native polars expressions,
   ever.** Byte-identical HKDF to HMAC (`hash`), Feistel FPE (`fpe`), and keyed
   date offsets (`date_shift`) are per-value keyed callbacks in *every* backend.
   So "which engine lowers `fpe` better" is a non-question. The real decision
   splits cleanly in two: who owns the relational work (scan, join, anti-join,
   sort, spill), and where the one masking kernel lives so it is never
   reimplemented per backend. DuckDB owns the former. A backend-neutral kernel
   owns the latter. DuckDB never calls `derive()`.

2. **The single-lowering architecture is already half-built.** The V2 polars
   substrate (`execution/polars/_polars_adapter.py`) has native handlers
   (`_hash`, `_redact`, `_truncate`, `_categorical`, `_passthrough`, `_shuffle`)
   that call the same `derive()` primitive as pandas, plus `PandasStrategyPort`
   (`execution/polars/_strategies/_pandas_port.py:26`) that runs the existing
   pandas handler for the seven non-vectorizable strategies (`date_shift`,
   `bucketize`, `fpe`, `faker`, `formula`, `text_redact`, `nested`). Its gate
   `_is_fully_polars_native` (`_polars_adapter.py:152`) returns `False` the
   instant `relationship_graph.edges` is non-empty (`:155`). **FK joins are the
   labeled hole in that gate.** Option 4 fills the hole. It does not stand up a
   third masking implementation beside pandas and polars.

3. **Generation already has coordinate-based per-row determinism, but not a
   uniqueness primitive.**
   `GenDeriveContext.row_int(family, i)` (`generators/derivation.py:194`) derives
   row `i` from a keyed HMAC of the row index, not from a shared RNG stream, and
   `derive_index(seed, namespace, source, pool_size)`
   (`determinism/_derive.py:255`) is a keyed index map already used by the pool
   sampler. `derive_index` is not a permutation and may collide. The council's
   "coordinate-determinism contract" is therefore extend-and-wire, while the
   "keyed format-preserving permutation over the pool index space" must be built
   from the cycle-walking Feistel/FPE primitive used by the kernel. Generation at
   scale is promoted from a deferred afterthought to a first-class phase.

4. **A stale fact in both design docs.** There is no `chunked_relationships_unsupported`
   ban. Option 1 (chunked FK self-mask) landed; FK edges are admitted via
   `gate_fk_child_edges` (`execution/_chunked_fk.py`, called from
   `_chunked.py:189`). The real hard-stop for a non-value-keyed FK key column is
   `strategy_not_chunk_safe` (`_chunked.py:206`). Fix this wording in
   `relationships-memory-scaling.md` section 1 during Phase 0 docs.

**The single highest-leverage change:** the masking kernel moves to Phase 0,
ahead of any out-of-core code, scoped to deterministic per-value strategies, with
pandas refactored to call it on day one. That makes byte-parity structural for
kernel consumers (pandas and out-of-core), proven against a pre-refactor snapshot,
instead of a perpetual diff between two implementations. The polars-native path
remains a parity-by-test surface unless and until its handlers are folded onto the
same kernel. It is also the correct V2 fold-point, so it is extracted once and
consumed by both V2 and Option 4.

---

## 1. Recommended architecture

Four layers, with a hard internal boundary between the masking kernel and the
relational engine.

1. **Backend-neutral masking kernel (the keystone).** Arrow-array-in,
   Arrow-array-out. The home for deterministic per-value masking logic: HKDF to
   HMAC, FF1/FF3 Feistel with alphabet/radix/tweak derivation, keyed date offset
   with leap/overflow/timezone handling, redact/truncate/text_redact/bucketize/
   passthrough, and derive input canonicalization defined once. Cross-row and
   grouped strategies are outside this kernel and must reject out of the
   out-of-core route until they have bounded relational lowerings. pandas and the
   out-of-core path invoke the kernel; polars-native remains parity-tested unless
   folded onto it. Built on the existing `determinism/_derive.py` primitives
   (`derive`, `DeriveContext.for_column`, `derive_index`, `derive_value`) and a
   byte-preserving move or alias of the derive canonicalization helper from
   `generation/pool/_canonicalize.py`.

2. **Relational/spill engine behind an Arrow boundary.** Owns scan, hash-join,
   anti-join, group-by, sort, and spill-to-disk. Defaults to **DuckDB** at high
   row counts (years-hardened grace/partitioned hash join and buffer manager).
   It owns set operations only and never touches `derive()`. The boundary is
   Arrow-in/Arrow-out so the operator is swappable.

3. **Orphan policy and remap minting as Python orchestration over streamed Arrow
   batches.** Reusing the exact kernel code paths. Never encoded in SQL and never
   "falling out of" the join. A left join is a mechanism; each policy is explicit
   control flow over the three disjoint partitions it yields.

4. **polars as the in-memory substrate and the single admission gate.** Extend
   `_is_fully_polars_native` to admit relationship graphs once the FK path
   exists, rather than building a second gate in `execution/out_of_core/`. One
   adapter, one gate, one fallback rule. polars interoperates zero-copy with the
   DuckDB join step over Arrow, so the substrate and the spill operator do not
   have to be the same library.

### The DuckDB-vs-polars adjudication

Real disagreement, adjudicated: keep the **substrate and gate in polars**
(aligned with the V2 direction, one dependency surface, vectorized strategies run
as zero-callback expressions), and make the **join/spill operator pluggable
behind the Arrow boundary, defaulting to DuckDB** at high row counts. Build the
FK pipeline as a polars LazyFrame path
(`scan_parquet -> select(key, masked_expr) -> sink_parquet`); when profiling
shows polars' streaming join degrading on skew or wide multi-way joins, swap
*only that join step* for a DuckDB query over the same staged Parquet and hand
the result back to Arrow. DuckDB earns its place exactly where it is decisively
ahead (the relational operator under memory pressure) without ever becoming a
home for masking logic. The product mandate (correctness over speed) is why the
load-bearing RAM guarantee should not ride polars' youngest subsystem.

---

## 2. Compatibility envelope

Start narrow, expand only after parity is pinned. The envelope is unchanged in
spirit from the original; the strategy floor is `hash`, growth is one strategy at
a time through the shared kernel.

**Initial admitted config (Phase 1):**

- Mask-only jobs.
- File/Parquet sources with Arrow-compatible schemas.
- Single-column FK edges.
- Acyclic relationship graph.
- Parent FK key strategy: `hash`.
- Child FK column follows existing by-reference semantics; no duplicate child
  strategy required.
- `orphan_policy: fail` first.
- No validators, quarantine, or vault in the first executable slice.
- No generated tables, `shuffle` keys, composite FKs, cycles, or streaming
  connectors.

**Expansion order:** add `preserve`, `warn`, `remap`; then validators/quarantine;
then vault; then multi-parent and diamond graphs; then composite FKs; then more
key strategies through the kernel (`fpe`, `date_shift`, then namespace-agnostic
ones); then generation; then platform auto-routing behind an explicit opt-in.

---

## Phase 0: Foundational contracts (de-risk, shared with V2)

**Intent:** Build the three contracts everything else depends on before any
out-of-core code lands. All three are reusable by the V2 rewrite, so this phase
is not throwaway. Do not block Option 4 on the full S11 to S12 V2 rewrite;
extract the kernel as a standalone shared asset now and build the FK path on top.

### Sprint 0.1: Backend-neutral masking kernel

**Deliverables**

- New `src/decoy_engine/kernel/` (or `determinism/kernel/`) exposing deterministic
  per-value strategy logic as Arrow-array-in / Arrow-array-out callables,
  parameterized by `(seed, namespace, strategy_config)`.
- Move or alias derive source canonicalization: `_canonicalize_source`
  (`generation/pool/_canonicalize.py:59`) and `_encode_int` (`:44`) move into the
  kernel byte-for-byte. This function remains the canonical bytes for HMAC /
  derive input only. It is not the FK equality key and must not replace the
  `_fk_key_value` lineage.
- Refactor the pandas `SCALAR_HANDLERS` (`execution/_strategies/__init__.py:41`)
  so the deterministic ones (`hash`, `fpe`, `date_shift`, `redact`, `truncate`,
  `text_redact`, `bucketize`, `passthrough`) call the kernel rather than holding
  their own copy of the logic. pandas becomes a kernel consumer after its current
  output bytes are pinned by snapshot tests.
- Design the contract for deterministic per-value strategies up front even though
  later sprints only wire a subset out-of-core. The kernel boundary is the most
  consequential decision in the feature and must not be discovered inside a later
  sprint as "a UDF wrapper for hash."

**Acceptance**

- Existing pandas strategy tests pass unchanged.
- A pre-refactor snapshot pins pandas handler output bytes before extraction.
- A kernel parity matrix proves byte identity between the kernel and the frozen
  pandas snapshot across strings, ints, floats, nulls, empty strings, and mixed
  Arrow scalar inputs, for every deterministic per-value strategy.
- `engine-v2-parity.yml` and `engine-v2-substrate-matrix.yml` stay green.

**Non-goals:** no DuckDB, no FK join, no new execution path.

### Sprint 0.2: Canonical join-key and null-sentinel invariant

**Deliverables**

- A single canonical FK match-key function, generalized from `_fk_key_value`
  (`execution/_pandas_adapter.py:71`), that is the only place int/float/bool/null
  equality normalization is defined, applied identically at three call sites that
  must never diverge: (a) the join key on both parent and child, (b) the
  orphan-match predicate, (c) the value handed to `preserve`/`remap` as "the source
  key." This is separate from derive canonicalization. FK match-normalized values
  may later be fed into derive canonicalization for `remap`, but the derive bytes
  function does not define FK equality.
- An explicit **null sentinel** encoded into the canonical key on both sides, so
  relational matching reproduces Python dict semantics.

**Why this is its own sprint (two landmines):**

- The int/float hazard *inverts* in Arrow engines. pandas accidentally
  over-matches int64 `123` and float64-because-null `123.0` via nullable-int
  boxing. Arrow/polars/DuckDB have a real nullable-int type and will instead
  silently *fail* to match across `Int64`/`Float64`. Same hazard, opposite bug.
- Python dicts match `None` as a real key; SQL/Arrow joins use three-valued logic
  where `NULL != NULL`. A child FK of NULL that pandas matches to a NULL parent
  via dict lookup will not match in a relational join. The sentinel fixes this.

**Acceptance**

- Property tests prove the canonical key is identical across pandas-dict matching
  and Arrow-join matching for int/float/bool/null/string and float-normalized
  keys, on both clean and orphaned rows.
- Named, separately-asserted invariants (not folded under a generic "key
  normalization" bullet) for the int/float-inversion and null-sentinel cases.

**Non-goals:** no join execution yet.

### Sprint 0.3: Coordinate-determinism and origin-agnostic key relation

**Deliverables**

- A documented **coordinate-determinism contract**: a generated value is keyed on
  `(seed, namespace, table, column, row_index)` via HKDF, so any shard generates
  its rows from coordinates with zero shared RNG state. Formalize and unify the
  two existing coordinate paths: `GenDeriveContext.row_int(family, i)`
  (`generators/derivation.py:194`) and the per-row reseed idiom in
  `generation/statistical/_sample.py:27`. Decide and record whether the two
  generation engines (`generators/` engine A and `generation/` engine B)
  converge on one context or stay separate with a shared kernel.
- A **mask/generate-agnostic parent-key relation** abstraction: "the set of keys
  that exist for this table, regardless of origin (masked or minted), persisted
  to Parquet." Do not hardcode "parent key relation = masked `hash` output." This
  is the seam that lets generation reuse the FK join machinery later. Designing
  it now is cheap; retrofitting it after Phase 1 locks around mask-only is the
  expensive path the mandate explicitly tells us to avoid.

**Acceptance**

- Design note reviewed against `relationships-memory-scaling.md` and this doc.
- No runtime behavior change; contract is documented and has a typed interface
  with a pandas-backed reference implementation.

**Non-goals:** no generation execution; no backend.

### Sprint 0.4: UDF throughput and GIL spike

**Deliverables**

- A measurement spike: run the kernel's `fpe` and `date_shift` over 1M, 10M Arrow
  arrays under the out-of-core calling convention, recording per-row cost, GIL
  contention, and cross-thread scaling.
- A decision record on the kernel's calling convention: whether it releases the
  GIL, runs in native threads, or needs a Rust hot path for the crypto-heavy
  strategies. This must come *before* the calling convention is frozen in Phase 1,
  because per-row cipher work does not SIMD and can make the out-of-core path
  slower per row than pandas for crypto-heavy tables. At ~190s mask time for 1M
  rows on a hash-heavy width-16 chain (measured, `relationships-memory-scaling.md`
  section 6), crypto/Faker-heavy schemas extrapolate to the hours range, so
  throughput, not memory, may be the time ceiling.

**Acceptance**

- A written throughput budget and a calling-convention decision, with numbers.
- A go/no-go on a native hot path, deferred to Phase 4 if not yet needed but
  scoped now.

**Non-goals:** no optimization yet; measure before fixing.

---

## Phase 1: Out-of-core mask-only slice

**Intent:** First end-to-end FK out-of-core pass: clean parent/child, single
`hash` FK, `orphan_policy: fail`, atomic output. Forces the hard questions
(kernel lowering, key normalization, join correctness, atomic publication)
against real data.

### Sprint 1.1: Admission gate (extend the polars gate, do not fork it)

**Deliverables**

- Extend `_is_fully_polars_native` (`_polars_adapter.py:152`) and its routing so
  a relationship graph can be admitted to an out-of-core FK path instead of
  unconditionally falling back to the pandas oracle. One gate, one fallback rule.
- A structured compatibility result (`OutOfCoreCompatibility` /
  `OutOfCoreRejection`) with a rejection taxonomy covering every current
  exclusion: generated tables, non-file source, composite FK, unsupported key
  strategy, unsupported orphan policy, validators/quarantine unsupported, vault
  unsupported, relationship cycle, unsupported target sink, and any payload column
  using a cross-row or grouped masking strategy (`grouped_series`, `windowed_date`,
  `derived_aggregate`, `group_key`, `joint_mask`) before a bounded relational
  lowering exists.
- Fail closed: rejection is preferable to byte drift, and the out-of-core path
  must never silently fall back inside itself.

**Acceptance**

- Accepted: clean parent/child, single FK, parent `hash`, `orphan_policy: fail`.
- Rejected with specific codes for every exclusion above.
- Tests prove fail-closed rejection and no silent in-path fallback.

### Sprint 1.2: Parent key relation, out-of-core

**Deliverables**

- `src/decoy_engine/execution/out_of_core/_relation.py`: build a narrow
  `source_key -> masked_key` relation from a parent Parquet source, projecting
  only the FK key column plus the kernel's masked-key expression. No payload
  columns.
- A DuckDB connection helper (`out_of_core/_duckdb.py`) with explicit temp
  directory and memory options, behind the Arrow boundary, used only for the
  scan/project/materialize. Soft-import DuckDB with a clear
  `BackendUnavailableError`; decide hard-dep vs optional-extra in this sprint.
- Apply the Sprint 0.2 canonical key (incl. null sentinel) before any join key is
  serialized.
- Secure-temp handling for raw source keys staged on disk: restricted-permission
  per-run temp directories, best-effort cleanup on failure, and an explicit
  decision record on encrypted spill versus keyed opaque join keys before any
  customer-visible routing.

**Acceptance**

- Parent key relation byte-matches the pandas full-frame parent key output
  exactly, via the Sprint 0.1 kernel.
- Null keys and duplicate parent keys match existing semantics.
- Temp files cleaned on success and failure.
- Raw source-key staging uses the secure-temp policy above; no world-readable temp
  paths.
- Peak-memory test shows no full parent pandas materialization.

### Sprint 1.3: Child left join with `orphan_policy: fail`

**Deliverables**

- Stream child source rows, join to the staged parent key relation, replace the
  child FK column with the parent masked key.
- Detect unmatched child keys (anti-join) as orphans; abort before publishing if
  any orphan exists. `fail` is a streaming count over the anti-join (a reducer,
  not a resident Python set), wired into the transactional commit gate.
- Private test entrypoint `run_fk_out_of_core(config, sources, *, sink,
  backend_options)`.

**Acceptance**

- Clean parent/child `hash` FK job is byte-identical to pandas full-frame output.
- Orphan `fail` raises before commit and leaves no complete-looking output.
- Row order preserved, or explicitly documented and normalized in tests (see risk
  register #6).
- Peak-memory test shows no full parent/child pandas materialization.

### Sprint 1.4: Graph-wide transactional staged output

**Deliverables**

- Reuse `TransactionalSink` / `ParquetTransactionalSink`
  (`execution/_transactional_sink.py:45`,`:95`). Stage all admitted table outputs;
  commit only after the whole graph succeeds; a late failure publishes nothing.
- Budget roughly 2x output footprint in staging during commit.

**Acceptance**

- Multi-table job commits atomically (single POSIX dir rename) only on full
  success.
- A late orphan/guard failure leaves no published partial dataset.

### Sprint 1.5: Memory and throughput probe

**Deliverables**

- Perf probes comparable to `scripts/fk_memory_probe.py`, at 250k/500k/1M and
  10M where hardware allows, recording peak RSS, temp disk, runtime, and parity
  status. A small default-gate memory sentinel plus a larger opt-in target.

**Acceptance**

- Out-of-core path stays under a documented memory budget on the 1M tier.
- 10M run documented as passed, skipped (hardware), or blocked by a concrete
  defect, with comparison to full-frame and sequential paths.

---

## Capability track (objective correction, 2026-07-01): build this BEFORE Phase 2 breadth

**Objective (user directive).** The point of Option 4 is *capability at large row
sizes, sized to the user's hardware*: the app must NOT OOM when large relational
tables are used: it processes whatever the machine can spill-accommodate. This
is functionality ("yes we can do that"), not a performance/peak-RSS play.

**Success criterion (the yardstick that replaces the S1.5 peak-RSS comparison).**
Out-of-core *completes* a multi-table FK job, with referential integrity intact,
at a memory ceiling below the dataset size: a workload at which full-frame AND
`run_sequential` OOM. Peak-RSS-vs-Option-2 at in-RAM scales is explicitly NOT
the metric; efficiency (closing the DuckDB per-edge overhead so the route is also
*cheaper* than Option 2) is a deferred, planned sprint (see "Deferred: efficiency"
below), not a blocker.

**Why the route does not yet have the capability (measured by S1.5, pre-C1).**
It still holds O(total rows) resident in three places, so it OOMs at the scales
it exists to unlock: (a) the runner takes `sources: dict[str, pa.Table]` fully
materialized (every input table in RAM); (b) `_relation.py::
build_parent_key_relation_from_tables` `.to_pylist()`s whole parent key columns
into a Python list sized by parent rows (CLOSED by Sprint C1, see its status
marker below); (c) `_join.py::mask_child_fk` `.to_pylist()`s whole child FK
columns and reads the whole join back via `to_arrow_table()` (CLOSED by Sprint
C2, see its status marker below). The fix is to keep only *bounded batches*
resident in Python/Arrow and let DuckDB own the O(data) relational work (dedup,
join, anti-join) with on-disk spill. The masking kernel (Phase 0) and FK-key
normalization already exist; they just need to run batch-wise, not whole-column.

### Sprint C1: Chunk-bounded parent key relation

**Deliverables**
- Rewrite `build_parent_key_relation` so the parent source is consumed in Arrow
  record batches (bounded size), applying the Phase-0 kernel mask + `fk_join_key`
  normalization per batch, streaming each batch into DuckDB (registered
  `RecordBatchReader` or an intermediate spill), and letting DuckDB do the
  last-write-wins dedup (`QUALIFY row_number()`) with disk spill. No
  `.to_pylist()` over a whole column; no Python list sized by parent cardinality.
- Keep the existing `hash`/namespace parent-key restriction for now (widened in
  the strategy-coverage sprint).

**Acceptance** (amended 2026-07-01; the original "byte-identical relation
Parquet" criterion is unmeetable and was never true of the pre-rewrite build
either: the relation Parquet's ROW ORDER is nondeterministic run to run in
BOTH the old and new builds, because DuckDB parallelizes the window function
over a streamed reader. Measured: 3 identical old-code runs produced 2
distinct file hashes. Logical content is identical everywhere, and the
consumer (`_join.py`) re-sorts by child `__decoy_row_nr`, so final output
determinism is unaffected. No ORDER BY is added to the COPY: a full sort at
scale would add exactly the memory pressure this track exists to remove, for
cosmetic determinism of a transient staging artifact.)
- Sorted-content parity: the set of `(join_key -> masked_key)` pairs in the
  relation Parquet is identical to a golden/single-shot build on the existing
  fixtures, last-write-wins dedup included (pinned by a cross-batch
  dedup-order test whose duplicate key carries DIFFERENT masked values, so an
  ORDER BY direction regression cannot hide behind hash masking).
- A boundedness test through the PUBLIC entry points: masking runs per batch
  (every `hash_array` call bounded by `batch_rows`), DuckDB is handed a lazy
  `RecordBatchReader` (never a materialized staging table, zero batches pulled
  at register time), and the batch count pulled equals the number of batch
  windows holding at least one non-null key row (`ceil(rows / batch_rows)` for a
  fully non-null parent; all-null windows are skipped).

**Status: landed (2026-07-01).** Both entry points stream bounded batches into
DuckDB; `build_parent_key_relation` masks per batch inside the streaming loop
via an injected per-batch callback, so no whole-column `hash_array` or
`.to_pylist()` survives on either entry point (the only remaining `to_pylist`
is the per-batch sliced source key column, bounded by `batch_rows`).
Intermediate-schema note: an empty or all-null-key parent now yields a
string-typed `__decoy_masked_key` column, because the staging schema declares
the masked type up front, where the old build let DuckDB cast the null-typed
staging to int32; verified equivalent through `mask_child_fk` and pinned by
test. Residual: input tables still arrive fully resident (that is C3,
input-side streaming), and the runner path's parent frame is masked upstream
by the adapter as a full table.

### Sprint C2: Chunk-bounded child join

**Deliverables**
- Rewrite `mask_child_fk` to stream child source batches, compute join keys per
  batch, LEFT JOIN each batch against the parent relation Parquet via DuckDB
  (`read_parquet`, spill-aware), apply the orphan policy per batch, and emit
  output batches to the sink, never materializing the whole child table or the
  whole join result in Python/Arrow. Orphan `fail` stays a streaming anti-join
  count (already a reducer); `preserve`/`warn`/`remap` handled batch-wise.

**Acceptance** (amended 2026-07-01, before implementation, to name the exact
residency the sprint bounds and avoid a silent scope substitution: the bound
is on JOIN-PROCESSING residency, not the child frame itself)
- Byte-identical child output to the current implementation on all existing
  fixtures (linear chain, fanout/diamond, orphan policies), plus a cross-batch
  orphan fixture (child larger than the batch size, with orphans, matches, and
  null keys spanning batch boundaries under every orphan policy).
- Memory-bounded test as in C1, scoped to join processing: the child-key side
  enters DuckDB as a lazy bounded `RecordBatchReader` (never a materialized
  whole-child staging table), the ordered join result is read back as a
  streamed record-batch reader (never one whole Arrow table), and all per-row
  Python work is bounded by the batch size. NOT bounded by C2: the child
  input/output FRAME residency and full output-to-sink streaming (both C3,
  the lazy input side), and `remap_values`, which stay runner-precomputed at
  child-row cardinality; their at-scale bounding is Sprint 2.1 / risk
  register #1.

**Status: landed (2026-07-01).** `mask_child_fk` streams bounded child-key
batches (row number, join key, carried source key components) into DuckDB via
a registered lazy `RecordBatchReader`; DuckDB owns a spillable copy (the FAIL
anti-join count and the join are two scans over a single-pass stream), the
LEFT JOIN, and the `ORDER BY row_nr`, and the result is consumed batch-wise
through a streamed reader. Orphan policies are applied per result batch, with
per-batch output arrays concatenated (with whole-column-equivalent type
promotion) into the FK columns of the resident child frame. The only
`.to_pylist()` calls left in `_join.py` are batch-bounded (sliced source key
columns on the way in, single result batches on the way out).

### Sprint C3: Streaming input side (lazy sources)

**Deliverables**
- A lazy source abstraction (path-based, batch-readable) so a table is never
  fully resident before C1/C2 consume it. Wire it into `run_fk_out_of_core` /
  `_runner.py` alongside the existing full-table API (in-memory `sources` stays
  supported for small jobs). The probe fixture already exposes a `lazy_loader`
  primitive to model this.

**Acceptance**
- A job whose *input* tables collectively exceed a set memory cap runs to
  completion (no full-input materialization at any point).

**Status: landed (2026-07-01).** `LazySource` (C3b), the fixed-schema
per-batch child joiner (C3c, record below), and the batch-streaming runner
(C3d). See the C3 status paragraph in §8. The acceptance criterion is proven
at a literal hard cap by Sprint C5 (the capped input chain is ~59 MB Parquet
expanding to a ~1.4-1.5 GB resident working set, run under a 1,024 MB cap).

#### C3c type-support and divergence record

The per-batch child FK joiner (`_batch_join.py`) fixes one Arrow output type
per FK component up front, from schemas alone, because a streaming Parquet
writer needs a single fixed schema. That changes the FK-key type envelope
relative to the whole-table path; the record below is the authoritative list.

Fail-closed rejections (`out_of_core_fk_key_dtype_unsupported`, raised at
construction, before any output exists). Each is a capability the whole-table
path has that the batch joiner rejects up front; in every case a
compatibility rejection beats byte drift.

- Decimal keys, mixed or not: whole-column inference digit-fits precision and
  scale from the values, so no fixed type is guaranteed byte-identical.
- uint64 keys: the Python round trip lands in int64 or uint64 by value
  magnitude, so the round-trip type is value-dependent.
- Timezone-aware timestamps: the round trip drifts the zone and is not pinned
  by tests.
- string with binary, and any other promotable multi-type mix outside
  {int64, float64}: a permissive merge matches whole-column inference only
  when the data actually mixes the candidates, so a run exercising only the
  narrower candidate would silently drift scalar values.
- String-masked parent with a numeric child key whose data never mixes: the
  whole-table path survives only on data luck and crashes with a raw
  ArrowTypeError on the first orphan.

Accepted divergence (pinned in tests): a merged float64 output stays float64
where the whole-child path narrows all-integral float keys to int64.
Reachable only when a float64 FK key column matches nothing (all orphans).
The streaming runner (C3d) must gate or accept this config explicitly; that
obligation transfers to it.

All-null or empty FK child column: streaming emits the fixed type with
nulls where the whole-table path emits a null-typed column. Values are
identical; the streaming schema is the intended one (it is writable).

FAIL error detail: the whole-table path reports the total orphan count across
the child; the batch joiner raises on the first offending batch with that
batch's count. Correct for a transactional sink; only the count in the
message differs.

### Sprint C4: Hardware-sized memory budget and spill

**Deliverables**
- A single knob that sizes the DuckDB `memory_limit` (already a `connect_duckdb`
  param) and the Arrow batch size to the host (an explicit config, defaulting to
  a fraction of detected available RAM) plus an explicit temp-disk location and
  a documented spill budget. This is the "sized to the user's hardware" part.

**Acceptance**
- The same job completes across two different memory caps; the smaller cap spills
  more to disk but still produces byte-identical output.

**Status: landed (2026-07-02).** `out_of_core/_budget.py` (`resolve_budget`:
explicit budget or 1/4 of detected host RAM into a DuckDB `memory_limit`
string plus a floored/capped `batch_rows`; `check_temp_disk_budget`: coded
fail-closed spill guard), `batch_rows` and `temp_disk_budget_bytes` threaded
through `run_fk_out_of_core`. Byte-identical output across batch sizes is
pinned by `tests/unit/execution/test_out_of_core_budget.py`; see the C4
status paragraph in §8.

### Sprint C5: Capability proof (the acceptance for the whole track)

**Deliverables**
- A test (opt-in / `benchmark`-marked, since it is deliberately large) that caps
  memory below the dataset size, runs a multi-table FK job that OOMs full-frame
  and `run_sequential` at that cap, and asserts out-of-core completes with
  correct FK integrity. Document the numbers (dataset size, cap, in-memory-path
  failure mode, out-of-core success) in `relationships-memory-scaling.md`.

**Acceptance**
- Out-of-core is demonstrably the *only* path that completes the capped workload.

**Status: landed (2026-07-02), acceptance met.** `scripts/fk_memory_probe.py
--capability` runs all three routes in hard-capped subprocesses
(`--mem-cap-mb`, RLIMIT_DATA) over one shared chunk-written Parquet chain. At
400k rows/table x 3 tables, width 16, 1,024 MB cap: full OOM, sequential OOM,
out-of-core completed (426 MB peak RSS, FK parity ok on both edges, 1,960
links checked per edge, 106 MB peak scratch disk). Opt-in test
`test_out_of_core_completes_where_in_memory_routes_oom`; numbers and cap
mechanics in `relationships-memory-scaling.md` §6.3 and the C5 status
paragraph in §8.

### Deferred: efficiency (planned later sprint, NOT a blocker)

Closing the DuckDB per-edge overhead so out-of-core is also cheaper/faster than
`run_sequential` at in-RAM scales (fewer connections per edge, avoid the duplicate
Arrow↔Python conversions the S1.5 §6.2 root-cause bullet describes). Tracked as a
follow-up once the capability above is proven; the user directive is functionality
first, efficiency later.

---

## Phase 2: Orphan policies, topology, and bounded controls

> Re-sequenced (2026-07-01): the capability track above lands before this breadth
> work. Phase 2 widens *which configs* qualify for the out-of-core path; it does
> not deliver the core not-OOM capability on its own.

### Sprint 2.1: Orphan policy lowering (`preserve`, `warn`, `remap`)

**Deliverables**

- `preserve`: orphan rows keep the raw source key; matched rows take the masked
  parent key; union, order preserved.
- `warn`: identical output to `preserve` plus one aggregated count to the quality
  channel. Never surface raw orphan keys in logs; emit a count, or a
  hashed/truncated identifier if a debug sample is required.
- `remap`: mint orphan values by running the parent's own kernel expression over
  the orphan-key array under the parent column's namespace and seed. Reuse the
  Option 1 proven equivalence (self-mask equals remap; `_chunked_fk.py` condition
  (d)) and share the literal expression-builder with that path. Done in Python
  over the anti-join output, bounded by orphan cardinality, then unioned back.

**Acceptance**

- Byte parity with pandas for `preserve`, `warn`, `remap`, including nulls,
  strings, ints, and float-normalized keys.
- Warning aggregation matches current quality-warning shape closely enough for
  platform/API callers to consume without special cases.
- Only resident set is the orphan partition (remap) or a running count
  (fail/warn); guarded against pathological orphan cardinality (risk register #1).

### Sprint 2.2: Multi-table topology

**Deliverables**

- Compile the relationship graph into an out-of-core execution order (reuse
  `table_topo_order`, `execution/_sequential.py:234`).
- One parent key relation per FK edge, or per parent key column where that keeps
  semantics simplest; ref-counted cleanup of staged key relations (the
  multi-parent / diamond eviction discipline already proven on Option 2's
  `_parent_map` ref-counting).
- Support chains, fan-out, multi-parent child tables, and diamonds for
  single-column FKs.

**Acceptance**

- Parity tests cover parent to child to grandchild, fan-out, diamond, and
  multi-parent shapes.
- All outputs commit atomically after the whole graph succeeds; a late orphan
  failure publishes no partial dataset.

### Sprint 2.3: Composite FK support

**Deliverables**

- Canonical composite key serialization for joins that matches the pandas
  `_KeyTuple` behavior (`_pandas_adapter.py:68`), reusing the Sprint 0.2 canonical
  per-component normalization and null sentinel.
- Parent key relation with multiple source and masked columns; child FK
  replacement across all components; orphan policy lowering extended to composite
  keys.

**Acceptance**

- Composite clean, `fail`, `preserve`, `warn`, `remap` cases match pandas.
- Type-normalization tests include mixed int/float components, null components,
  string components; namespace handling explicit per component.

### Sprint 2.4: Strategy coverage beyond `hash` (and the chunk-wise fix)

**Deliverables**

- Admit one key strategy at a time through the kernel. Recommended order: `fpe`,
  `date_shift`, then the namespace-agnostic `redact`, `truncate`, `passthrough`,
  `text_redact`, `bucketize`. Reuse `CHUNK_SAFE_STRATEGIES` and
  `NAMESPACE_REQUIRING_STRATEGIES` (`execution/_chunked_fk.py:18`,`:41`) as the
  admission floor.
- **Make `PandasStrategyPort` chunk-wise** (`_pandas_port.py:40`). Today it does
  `frame.select(column).to_pandas()` on the whole column, which silently
  re-materializes the full column and defeats the out-of-core property for any
  job touching `date_shift`, `fpe`, `bucketize`, `faker`, `formula`,
  `text_redact`, or `nested`. Make per-batch application an explicit acceptance
  criterion, not an assumption. Once a strategy is kernelized, it should not need
  the port at all on the out-of-core path.

**Acceptance**

- Each newly admitted strategy has a pandas-vs-kernel-vs-out-of-core parity
  matrix for parent keys, child resolution, and `remap` orphans.
- A `PandasStrategyPort`-routed strategy demonstrably processes 10M rows without
  full-column materialization.
- Unsupported strategies (notably `shuffle`, non-value-keyed) never route
  out-of-core silently; rejected with specific codes.

### Sprint 2.5: Bounded whole-output controls (validators, quarantine, vault)

**Deliverables**

- Reformulate global checks as streaming reducers (HyperLogLog for distinct
  count, counting sketches, sampled drift stats) or a two-pass-over-staged-Parquet
  pass. Run validators after all outputs are staged and before commit; apply
  quarantine before final publication.
- For controls that genuinely cannot stay bounded (for example exact global
  uniqueness needing a full sort/hash-set), **explicitly reject the config out of
  the out-of-core route** rather than quietly materializing. This replaces the
  original plan's "load staged outputs only when required," which is the side
  door that reintroduces full materialization (risk register #2).
- Vault capture: stream vaulted entries to a durable store, narrow-key from the
  start (see Sprint 4.2 for the at-scale design).

**Acceptance**

- Validator failure without quarantine aborts and publishes nothing.
- Validator failure with quarantine publishes filtered main outputs plus the
  quarantine artifact.
- Controls run in the same ordering as `run_pipeline`.
- An unboundable control produces an actionable rejection, never a silent
  whole-output load.

---

## Phase 3: Generation at scale (the mask-only plan's blind spot)

**Intent:** A credible enterprise alternative must generate FK-related tables at
100M+ rows. Generation is architecturally divergent from masking and reuses the
Phase 0 contracts. Much of the determinism machinery already exists
(`generators/derivation.py`, `generation/pool/`, `determinism/derive_index`); this
phase wires it to the out-of-core key relation and closes the streaming gaps.

### Sprint 3.1: Coordinate-keyed generation over the out-of-core key relation

**Deliverables**

- Wire the existing `GenDeriveContext` coordinate path
  (`generators/derivation.py:101`, `row_int(family, i)`) to emit a table's keys
  into the mask/generate-agnostic parent key relation (Sprint 0.3), so generated
  tables feed the same FK join machinery as masked tables.
- Resolve the two-generation-engine question recorded in Sprint 0.3 (converge or
  share kernel).

**Acceptance**

- A generated parent table's key relation is byte-identical between full-frame
  generation and the coordinate-keyed out-of-core path for a fixed seed.
- Generation shards produce identical output regardless of shard boundary.

### Sprint 3.2: Index-derivable parent keys and on-the-fly child FK

**Deliverables**

- Where parent PKs are index-derivable (key for parent index `i` = HKDF of `i`,
  via the kernel), a child samples a parent by drawing an index in `[0, P)` and
  deriving the key on the fly: O(1) per row, zero parent residency.
- Where parent keys are arbitrary/externally supplied, fall back to the staged
  key relation (bounded by key cardinality, not width). Document the difference
  so config authors can choose the cheap path.

**Acceptance**

- Generated child FKs reference valid parent keys with no resident parent map
  when keys are index-derivable.
- Parity with a full-frame generation reference for both key regimes.

### Sprint 3.3: Streaming distribution fit

**Deliverables**

- Replace any whole-column fit with streaming estimators: Welford for
  mean/variance, a t-digest or KLL sketch for quantiles/histograms, accumulated
  copula/correlation parameters in one pass. Touches `generation/statistical/`
  (`_sample.py`, `_spec.py`) and `generation/_distribution.py`.

**Acceptance**

- Fitted parameters from the streaming path match the full-load fit within a
  documented tolerance, and never load a whole reference column.

### Sprint 3.4: Skewed fan-out sampler

**Deliverables**

- A CDF-inversion / Zipfian child-per-parent sampler keyed per child index, no
  resident fan-out table. Parents with zero children are normal; orphans are
  simply not minted.

**Acceptance**

- Generated fan-out reproduces a target power-law within tolerance; memory flat
  in row count.

### Sprint 3.5: Two-pass global differential-privacy budget

**Deliverables**

- An explicit two-pass design for DP: pass 1 streams sufficient statistics, pass
  2 streams the globally-calibrated mechanism. The epsilon budget is global over
  the column/dataset, not per-chunk. Integrate with the existing DP code in
  `quality/dp.py` (note: DP currently lives in the quality layer, not generation;
  decide whether it moves or is called from the generation path).

**Acceptance**

- A naive per-chunk epsilon spend is impossible by construction (test asserts
  total epsilon equals the configured budget regardless of chunk count).
- Output distribution matches a full-frame DP reference within tolerance.

**Note:** this is genuinely new engineering, comparable in size to the join work,
because a per-chunk epsilon re-spend is a privacy bug, not a perf bug.

### Sprint 3.6: Keyed-permutation pool uniqueness at scale

**Deliverables**

- Replace any resident "used-value" tracking set with a keyed format-preserving
  permutation over the pool's index space. Do not use `derive_index`
  (`determinism/_derive.py:255`) for this: it is a keyed hash-to-index and may
  collide. Build the permutation from the kernel's FF1/FF3-style cycle-walking
  Feistel primitive over `[0, pool_size)`, then reuse the existing pool machinery
  (`generation/pool/_sampler.py`, `_builder.py`). Uniqueness holds by construction
  because the primitive is a bijection over the index space.
- A fail-fast pool-exhaustion check (`pool_size < target_rows`) before any work.

**Acceptance**

- Generating N unique values from a pool of size >= N uses memory flat in N (no
  used-set), with proven uniqueness and reproducibility.
- A regression test demonstrates that `derive_index` can collide for the uniqueness
  use case and is not called by the permutation path.

### Sprint 3.7: Composite, freetext, NER, and cross-row limits

**Deliverables**

- Within-row composite dependencies (`generation/composite/`) stream as-is.
- Cross-row dependencies (running totals, sequences, monotonic IDs) break pure
  streaming: implement windowed/ordered passes or document a bounded-window
  limit and reject beyond it.
- NER/freetext (`storm/ner.py`, `generation/statistical/_sample.py::_lorem_text`)
  is CPU-bound but stateless per row: parallel and bounded-memory; the constraint
  is throughput/batching (multi-process fan-out), not memory.

**Acceptance**

- Composite within-row parity at scale; cross-row generators either stream within
  a documented window or reject cleanly; NER throughput scales across processes.

---

## Phase 4: Hardening and routing readiness

### Sprint 4.1: Native hot path for crypto-heavy strategies (conditional)

Gated on the Sprint 0.4 spike. If `fpe`/`date_shift`/Faker throughput is the time
ceiling, add a GIL-releasing native (Rust) kernel hot path behind the same
Arrow-in/out interface, with a parity matrix against the Python kernel.

### Sprint 4.2: Vault capture at scale

A 100M-row reversible masked-to-original mapping is a durable out-of-core write
and a security/storage problem, not a memory tweak. Streamed append to a durable
store (Parquet or LMDB), narrow-key, with its own crash-safety and access design.
An in-memory accumulator here is just risk register #1 again.

### Sprint 4.3: Cycle and self-referential FK handling

- True cycles have no topological order: reject by default with a specific code.
- Self-referential FKs (employee to manager, BOM, category trees) are a common
  enterprise pattern and the cheapest cycle: intra-table ordering, or a two-pass
  null-then-backfill. Given the "credible alternative" goal, give this case a real
  design and tests, not a permanent silent rejection.

### Sprint 4.4: Spill and temp-disk budget, skew controls

Put a number on it. The original plan said "document spill/temp policy" with no
value. Explicit temp-dir quotas, skew awareness for hot parent keys with massive
fan-out (which can blow a single spill partition), monitoring, and fail-fast on
budget so a 100M-row spill trades OOM for a clean error, not ENOSPC that takes
down co-located jobs.

### Sprint 4.5: Default routing readiness

- Routing precedence among full-frame, sequential (Option 2), chunked self-mask
  (Option 1), and out-of-core (Option 4); a kill switch; production telemetry for
  fallback reason and backend failure.
- An adversarial review (dennis) focused on parity, partial-output safety,
  temp-file cleanup, resident-map invariants, and SQL/UDF security posture.
- No default auto-routing until the review signs off; platform opt-in behind a
  feature flag first (the existing path is the fallback for non-admitted jobs).

**Acceptance**

- Opted-in platform FK jobs route out-of-core only when compatibility passes;
  non-admitted jobs get an actionable rejection and use the safe path.
- All BLOCKER/HIGH findings fixed or explicitly deferred out of the default route.
- Operator docs explain when out-of-core is used and how to diagnose
  rejection/fallback.

---

## 3. Ranked scale-risk register

The actual ceiling is not the FK join (DuckDB handles it). Design against these,
in this order.

**Tier 1 (most likely to be the real ceiling even after the join is out-of-core):**

1. **Any resident key-to-value map.** remap over a huge orphan set, vault, global
   uniqueness validators, generation with non-index-derivable parent keys: each
   re-erects the 1 to 2 GB wall. **Invariant: no Python structure may ever be
   sized by total key cardinality.** Anything cardinality-sized is a DuckDB
   relation or a spilled store. This is the single most likely place a "fixed"
   Option 4 still OOMs.
2. **Whole-output-view controls (validators / quarantine / quality metrics).** The
   side door to full materialization. Stream (sketches) or two-pass, or reject.
   A program of work comparable to the join itself.
3. **UDF throughput / GIL for `fpe`, `date_shift`, Faker, NER.** Out-of-core fixes
   memory, not wall-clock. Per-row crypto does not SIMD; a GIL-holding UDF
   serializes engine parallelism. Likely the time ceiling. Spike early (Sprint
   0.4); it can change the kernel calling convention.
4. **Join spill, partition skew, temp-disk budget.** The thing DuckDB was chosen
   for, but a hot key with massive fan-out can still blow a single spill
   partition, and 100M-row spills can need tens to hundreds of GB of temp disk
   (OOM traded for ENOSPC). Needs quotas, skew awareness, fail-fast.

**Tier 2 (bounded correctness effort, not a scale ceiling):**

5. **Vault at scale** (Sprint 4.2): durable streamed write, its own design.
6. **Row-order preservation:** joins reorder; an out-of-core sort of 100M rows can
   cost as much as the join. Either carry a row-index and re-sort, or relax the
   order contract explicitly.
7. **Graph-wide crash-safety** (Sprint 1.4): publish nothing until the whole graph
   passes; budget ~2x output footprint in staging.
8. **Composite / multi-parent / diamond:** lower risk than they look; a mechanical
   port of Option 2's proven ref-counted retention; on-disk relations make it
   easier. Composite reduces to canonical-serialization parity.
9. **Cyclic / self-referential** (Sprint 4.3): reject true cycles; design the
   self-referential case.
10. **Parquet row-group / streaming IO:** lowest risk; pin row-group size on
    write, read in batches, expose one validated tunable.

**Design-against order: #1 -> #2 -> #3 -> #4.** The topology items are effort, not
ceiling.

---

## 4. Cross-cutting test matrix

Every sprint that changes execution extends these groups:

- **Parity:** pandas full-frame output equals out-of-core output for admitted
  configs (the kernel makes this structural; the matrix proves it).
- **Atomicity:** failure leaves no published partial outputs.
- **Orphans:** each policy with matched and unmatched keys.
- **Types:** string, int, float-normalized, null (sentinel), bool, and mixed
  Arrow/pandas scalar representations; the int/float-inversion and null three-
  valued-logic invariants asserted by name.
- **Topology:** chain, fan-out, diamond, multi-parent, later composite and
  self-referential.
- **Controls:** validators, quarantine, vault, warnings, quality metrics, each
  either bounded-streaming or explicitly rejected.
- **Resource behavior:** no all-table materialization; no resident structure sized
  by key cardinality; temp files cleaned on success and failure; temp-disk under
  budget.
- **Generation:** coordinate-determinism across shard boundaries; index-derivable
  vs staged key regimes; streaming-fit tolerance; global epsilon equals budget;
  pool uniqueness without a used-set.

---

## 5. Implementation guardrails

- Fail closed. Compatibility rejection beats byte drift.
- The kernel is the single source of masking truth. pandas calls it from Sprint
  0.1, so the oracle *is* the lowering; there is no "until a contract is shared"
  window.
- DuckDB owns set operations only. It never calls `derive()`.
- One admission gate (the extended polars gate). Never a second gate in
  `out_of_core/`.
- Treat orphan policies as explicit control flow. A left join detects orphans; it
  does not implement policy.
- No Python structure sized by total key cardinality, ever.
- Publish nothing until the whole graph passes validation and commit.
- No customer-visible routing until parity, atomicity, temp cleanup, and the
  resident-map invariant are tested.
- Honor the documented ~600 LOC orchestration-module cap (advisory, not gated:
  enforced by review, see `_chunked_fk.py` precedent of extracting to stay under
  it). No em-dashes in docs or code comments.
- Keep `RELEASE_PHASE` pre-ga semantics: out-of-core stays behind opt-in until the
  Sprint 4.5 review; flipping to ga binds the compatibility contract.

---

## 6. Suggested first milestone

Phase 0 in full, then Phase 1 Sprints 1.1 to 1.5 as one milestone:

1. Backend-neutral kernel, pandas refactored onto it (0.1).
2. Canonical join-key and null-sentinel invariant (0.2).
3. Coordinate-determinism and origin-agnostic key relation contract (0.3).
4. GIL/throughput spike and calling-convention decision (0.4).
5. Extended admission gate, parent key relation, child `fail` join, graph-wide
   transactional output, memory probe (1.1 to 1.5).

That produces a useful, reviewable out-of-core mask-only backend on a single
masking lowering, with generation and the broad strategy/topology matrix
deferred but their seams already cut. It forces the genuinely hard questions
(byte-identical kernel lowering, key normalization on both sides, join
correctness, orphan control flow, atomic output, crypto throughput) without
committing to the full matrix or platform routing first.

---

## 7. Adversarial review (dennis)

**Overall verdict: APPROVE-WITH-CHANGES.** The architecture is the right one and
the four framing corrections in section 0 are, with one exception, accurate
against the live tree (I checked every cited symbol). Two findings are BLOCKER-grade
because, if a sprint is implemented as the doc currently words it, it produces
incorrect code or breaks the headline FK invariant. Sprints 0.1/0.2 and 3.6 are
NEEDS-REWORK and must not start until their BLOCKER is closed. Everything else is
fixable in place.

Severity tally: 2 BLOCKER / 4 MAJOR / 5 MINOR / 1 NIT.

### What this plan gets right

The instinct to extract the masking kernel before any out-of-core code, and to make
pandas call it from day one, is correct and is the single best decision in the doc.
The `PandasStrategyPort` whole-column claim is true and is a genuine landmine:
`_pandas_port.py:40` does `frame.select(column).to_pandas()` on the entire column,
so any out-of-core job touching a ported strategy silently re-materializes a full
column. Flagging that as an explicit acceptance criterion (Sprint 2.4) is exactly
right. The "no `chunked_relationships_unsupported` ban" correction is accurate: no
such symbol exists in the tree; FK edges are admitted via `gate_fk_child_edges`
(`_chunked_fk.py:61`, called from `_chunked.py:189`) and the real hard-stop is
`strategy_not_chunk_safe` (`_chunked.py` ~:206). The `DuckDB-never-calls-derive()`
boundary is genuinely achievable for the `hash` mask case: the masked key is
precomputed in the parent-key-relation pass (Sprint 1.2) and the join (Sprint 1.3)
matches on the raw source key and substitutes the precomputed masked key, so the
join step needs no `derive()`. The scale-risk ranking (resident key map, then
whole-output controls, then UDF throughput, then spill/skew) is well prioritized,
and the DP "per-chunk epsilon is a privacy bug, not a perf bug" callout is correct
and correctly sized as net-new engineering.

### BLOCKER

**B1. Sprint 3.6: `derive_index` is not a permutation; "uniqueness by construction"
is mathematically false as written.** The deliverable says replace the used-value
set with "a keyed format-preserving permutation over the pool's index space,
reusing `derive_index` (`determinism/_derive.py:255`)" and asserts "Uniqueness holds
by construction." `derive_index` is `int.from_bytes(derive(...)[:8], "big") %
pool_size` (`_derive.py:286-287`). That is a keyed hash with collisions, not a
bijection. Calling it per row to draw pool indices produces duplicate indices
(birthday collisions) and therefore duplicate values, which is precisely the
property the sprint claims to guarantee against. The primitive that gives a
guaranteed bijection over `[0, pool_size)` is a format-preserving cipher
(cycle-walking Feistel), which the kernel already has to build for the `fpe`
strategy. *Fix:* rewrite Sprint 3.6 to build the permutation from the FF1/FF3
cycle-walking cipher over the index space, not from `derive_index`. Do not start
3.6 until the primitive is corrected, or the test "uniqueness without a used-set"
will be unsatisfiable.

**B2. Sprints 0.1 and 0.2 conflate two canonicalizations with contradictory type
domains, and the conflation breaks the int/float null-match that 0.2 exists to
preserve.** Sprint 0.1 says move `_canonicalize_source` (`generation/pool/_canonicalize.py:59`)
into the kernel "so masking, generation, and FK key building share one
canonical-bytes function." But `_canonicalize_source` *hard-errors on any float*
(`_canonicalize.py:86-95`) and encodes bool as bytes. The FK *match* normalization
`_fk_key_value` (`_pandas_adapter.py:71`) does the opposite: it *accepts* floats,
normalizes whole-number floats to int (`:81-82`), leaves non-integer floats as
float, and passes bool through unchanged. These are two different jobs: one
produces bytes for the HMAC input, the other normalizes a value so an int64 parent
key and a float64-because-null child key compare equal in a dict. If "FK key
building" is routed through `_canonicalize_source` as 0.1 states, the float64
child key that 0.2's whole reason for existing is to match would hard-error instead
of matching the int64 parent. *Fix:* state explicitly that FK match-normalization
(`_fk_key_value` lineage) and derive-canonicalization (`_canonicalize_source`
lineage) are and remain two separate functions; the kernel move of
`_canonicalize_source` must be byte-preserving (its own docstring says any change
is a `SEED_PROTOCOL_VERSION` conversation), and the FK key path feeds normalized
values *into* canonicalization, it does not replace it. Reword 0.1's "share one
canonical-bytes function" claim before 0.1 starts.

### MAJOR

**M1. "Byte-parity is structural / everyone runs the same code" is overstated, and
the doc contradicts itself on zero-callback.** The polars-native handlers are a
third lowering. For `hash` the native handler does call `derive()` per value
(`execution/polars/_strategies/_hash.py:53`), so section 0 point 2 is accurate, but
the adjudication section then claims "vectorized strategies run as zero-callback
expressions" as a reason to keep the substrate in polars. That is false for the
strategies that matter: `hash`, `fpe`, `date_shift` are per-value keyed callbacks
in polars too (which is the doc's own point 1). Parity between the polars-native
expression path and the kernel therefore remains a test obligation, not a
structural guarantee. *Fix:* scope the "structural" claim to kernel consumers
(pandas and out-of-core) and keep the polars-native path as a parity-by-test
surface, or fold polars-native onto the kernel in 0.1 and drop the zero-callback
argument.

**M2. Out-of-core payload masking has no story for cross-row / grouped masking
strategies, and the rejection taxonomy omits them.** The out-of-core path masks the
whole table, not just the FK key. The pandas `SCALAR_HANDLERS` registry
(`execution/_strategies/__init__.py:41-65`) contains `grouped_series`,
`windowed_date`, `derived_aggregate`, `group_key`, and `joint_mask`, which are not
per-row pure: a group or window can span batch boundaries, so per-batch application
is wrong, not just slow. The Sprint 1.1 rejection taxonomy and Sprint 2.4 strategy
list never enumerate these, and the section 1 claim that the kernel is "the only
home for every strategy's keyed logic" is false for them: they cannot be an
Arrow-array-in / array-out callable. Sprint 3.7 acknowledges cross-row dependencies
for *generation* but the same hazard exists in *masking* and is unaddressed. *Fix:*
add these to the Sprint 1.1 rejection taxonomy as hard exclusions (a payload column
with a cross-row strategy rejects the job out of the out-of-core route), and stop
claiming the kernel covers "every strategy."

**M3. Phase 0 refactors the shipping pandas oracle first; the doc must pin the
oracle bytes before touching it.** Sprint 0.1 refactors `SCALAR_HANDLERS` so the
deterministic handlers (including `fpe` Feistel and `date_shift` leap/overflow/tz,
the two highest bug-density strategies) call the kernel. Pandas is the oracle every
other path parities against, so any byte drift in the extraction does not just break
out-of-core, it breaks the baseline the entire V2 substrate matrix is pinned to. The
repo rule (`CLAUDE.md`, "Snapshot before extraction (V2.0-A snapshot harness
mandatory)") applies directly and is not cited. *Fix:* make Sprint 0.1 pin the
pandas per-strategy output bytes via the snapshot harness *before* the refactor, and
state that the parity matrix compares against the frozen pre-refactor snapshot, not
against the post-refactor handler comparing to itself.

**M4. The staged parent-key relation and the DuckDB spill contain raw, unmasked
join keys on disk; for a privacy product that needs an explicit at-rest design.**
The join matches on the raw source key (B-correct: that is how derive() stays out of
the join), which means the staged `(source_key, masked_key)` relation and any DuckDB
spill partition hold unmasked PII keys in the temp directory. Sprint 1.2 covers
cleanup on success and failure, but cleanup is not the same as exposure control: a
crashed or killed job leaves raw keys on disk, and co-located jobs share the temp
volume. Sprint 4.5 mentions "SQL/UDF security posture" and 4.4 sets a temp-disk
quota, but neither addresses unmasked-key-at-rest. *Fix:* add an explicit secure-temp
requirement (restricted-permission temp dir, crash-safe wipe, and a decision on
whether spilled join keys must be encrypted or hashed-with-a-recoverable-sidecar)
to Sprint 1.2 and the Sprint 4.5 security review scope.

### MINOR

**m1. `generation/statistical/_sample.py` is not a coordinate-determinism path of
equal standing; it is the old `col_seed + i` idiom.** Sprint 0.3 lists "the per-row
reseed idiom in `generation/statistical/_sample.py:27`" alongside `row_int` as "the
two existing coordinate paths" to "formalize and unify." But `_sample.py` reseeds a
`random.Random` with `col_seed + i` (its own docstring), which is exactly the F3
arithmetic that `GenDeriveContext` was built to *replace* (`generators/derivation.py:101-117`:
"Replaces ... the F3 `column_seed + i` arithmetic"). It is chunk-safe, so the Phase 3
shard-determinism acceptance holds, but unifying it onto the HKDF coordinate contract
is a determinism rewrite with fixture-drift risk, not "extend-and-wire," and per-row
`random.Random` reseeding is a throughput problem at 100M rows that the Sprint 0.4
spike (scoped only to `fpe`/`date_shift`) does not cover. *Fix:* call this out as a
migration with output-drift risk, and extend the 0.4 spike to the per-row reseed.

**m2. "One gate" understates Sprint 1.1.** `_is_fully_polars_native`
(`_polars_adapter.py:152`) is a boolean that returns `False` on any edge (`:155`);
the out-of-core path is a third destination, and Sprint 1.1 introduces a new
`OutOfCoreCompatibility` / `OutOfCoreRejection` taxonomy. That is a richer admission
decision plus a three-way router, not a flipped boolean. The guardrail "never a
second gate in `out_of_core/`" is fine as a code-location rule, but the doc should
admit the decision surface grows. *Fix:* reword "one gate, one fallback rule" to
describe the boolean becoming a router with a structured out-of-core admission
result that lives in the adapter, not in `out_of_core/`.

**m3. The orphan-cardinality "guard" is asserted, not specified.** Risk register #1
and Sprint 2.1 both say `remap` is "guarded against pathological orphan cardinality"
but neither specifies the mechanism. A guard that is actually a hope is the invariant
("no Python structure sized by total key cardinality") quietly failing, because a
fully-mismatched join makes the orphan partition approach total child cardinality.
*Fix:* specify a concrete bound: spill the orphan partition to the relational engine,
or hard-reject above a configured orphan-fraction threshold.

**m4. No inter-table parallelism story for masking.** Tables are processed in strict
topological order (`table_topo_order`, `_sequential.py:234`). At 100M+ rows across
many tables, serial table processing leaves cores idle, and the source design doc
explicitly noted process-level parallelism as the thing Options 2/4 lack and Option 5
has. Risk #3 covers per-row UDF cost but not inter-table serialization. *Fix:* add a
note on whether independent subtrees of the FK graph can run concurrently, or
explicitly accept serial-table wall-clock as a known limit.

**m5. The stated generation coordinate tuple does not match what the code keys on.**
Sprint 0.3 documents the contract as keyed on `(seed, namespace, table, column,
row_index)`, but `GenDeriveContext.for_column` keys the column root on the strategy
*config fingerprint* (`generators/derivation.py:159-169`), not on `(table, column)`
identity. Two columns or two tables with identical config share a row stream. *Fix:*
confirm whether fingerprint-keying is adequate when two different parent tables
generate FK keys with the same config, or make table/column identity part of the
root.

### NIT

**n1.** The "vectorized strategies run as zero-callback expressions" argument for
keeping the substrate in polars is weak for the Phase 1 floor, since `hash` is a
per-value callback in every backend. Keep the polars decision on its real merits
(one dependency surface, V2 alignment, the cheap structural strategies redact/
truncate/passthrough/shuffle genuinely do vectorize) rather than on a property the
shipping strategy does not have.

---

## 8. Implementation status: Phase 0 + Phase 1 S1.1-1.5 (2026-06-30) + C1-C5 (2026-07-01/02)

**S1.4 landed:** `run_fk_out_of_core` now stages each table to the sink and
evicts it from memory right after that table's outgoing parent-key relations
are built (`out_of_core/_runner.py`), instead of retaining every masked table
in `outputs` for the whole graph. Behavior is unchanged when `sink is None`.
This bounds the OUTPUT side only: every input table in `sources` stays fully
resident for the run, so peak RSS is still O(all inputs) plus the current table.
Input-side streaming is a later sprint; S1.4 does not claim a full memory bound.

**S1.5 landed (memory and throughput probe):** `scripts/fk_memory_probe.py`
gained a `--mode out_of_core` (`ParquetTransactionalSink`, fresh subprocess per
tier for clean RSS isolation, a polled peak-scratch-disk sampler scoped to the
runner's transient `temp_dir` only: relation/join key staging plus DuckDB
spill, NOT the committed full-width output, which is reported separately as
`committed_output_mb`; and a post-run parity check against the committed
Parquet output that builds the parent map over the full parent key column so it
is not vacuous at scale). The 250k/500k/1M sweep,
the 1M-tier memory budget, and the 10M disposition are recorded in
`relationships-memory-scaling.md` section 6.2; the short version: **the probe
found that the route does not yet realize its intended memory win.** Out-of-core
peak RSS is close to or worse than full-frame below 250k rows/table (e.g. +30%
at 5,000 rows/table) and only 1.9-11.4% better at 250k-1M, while the
already-shipped Option 2 (`run_sequential`, no DuckDB) stays 25-28% better than
full-frame at every measured tier. Root cause, traced to the code:
`_relation.py::build_parent_key_relation_from_tables` and
`_join.py::mask_child_fk` both called `.to_pylist()` over whole key columns and
built Python lists sized by table cardinality (parent rows, child rows
respectively) before/after the DuckDB join, which is exactly the Tier-1
scale-risk register item (§3, item 1: "no Python structure may be sized by
total key cardinality"). Both halves have since been closed: the relation half
by Sprint C1 and the join half by Sprint C2 (both 2026-07-01, see below). The
route was also slower than both
other paths at every tier (1M: 268.6s vs full-frame's 208.0s and sequential's
207.7s). 10M was skipped per the sprint brief's timebox (1M took 268.6s, over
the "comfortably under ~2 min" bar for 10M to be opt-in); projected 10M peak
RSS from the measured slope is ~28 GB, in the same range as full-frame's
already-documented ~33 GB extrapolation, i.e. not yet a materially different
ceiling at this row count. A fast, default-gate memory sentinel
(`tests/perf/test_out_of_core_memory_sentinel.py`) and a slower opt-in
(`benchmark`-marked) comparative test at the tier the real win appears (1M)
were added; the fast sentinel intentionally does NOT assert "below
full-frame" at its small tier, because that would be a false assertion against
measured behavior (it instead bounds absolute peak RSS and the overhead ratio
vs full-frame). This is implementation-status reporting, not a Phase 2 fix:
closing the gap (making the relation/join genuinely chunk-bounded) was future
work at S1.5 time, tracked as a known limitation below; it has since landed as
Sprints C1 and C2.

**C1 landed (chunk-bounded parent key relation, 2026-07-01):** `_relation.py`
now streams bounded Arrow record batches (default 65,536 rows) into DuckDB
through a registered lazy `RecordBatchReader`, and DuckDB owns the
last-write-wins dedup with on-disk spill. Masking is per batch on BOTH entry
points: `build_parent_key_relation_from_tables` (runner/production path)
slices and filters the pre-masked columns per batch, and the plan-aware
`build_parent_key_relation` runs the Phase-0 `hash_array` kernel on each kept
source slice inside the streaming loop (filter-then-hash equals
pre-mask-then-filter because the kernel is per-value deterministic and skips
nulls). No Python or Arrow structure in the relation builder is sized by total
parent cardinality anymore. Acceptance was amended to sorted-content parity
because the relation Parquet's row order is nondeterministic in both the old
and new builds (see the C1 acceptance note in the capability track). Residual:
input tables still arrive fully resident, which is C3 (input-side streaming),
and the runner path's parent frame is masked upstream by the adapter as a
full table.

**C2 landed (chunk-bounded child join, 2026-07-01):** `_join.py::mask_child_fk`
now runs a narrow, streamed join. The child-key side (global row number,
`fk_join_key` encoding, and the raw source key components carried as sliced
Arrow arrays for `preserve`/`warn` reconstruction) enters DuckDB as a lazy
`RecordBatchReader` of bounded batches (default 65,536 rows, same bound as
C1); because the stream is single-pass and the orphan-FAIL anti-join count
plus the join are two scans, DuckDB owns a spillable copy of the narrow key
table rather than Python holding one. The masked-key LEFT JOIN is ordered by
the global row number (DuckDB external sort, spill-aware) and read back
through a streamed record-batch reader, never one whole Arrow table; orphan
policies run per result batch (remap indexes the runner-precomputed remap
arrays by global row number), and per-batch output arrays are concatenated
into the FK columns of the resident child frame using Arrow's permissive
field-merge promotion (`pa.unify_schemas`), which an oracle battery in
`test_out_of_core_join_chunked.py` proves byte-identical to whole-column
`pa.array()` inference across strings, nulls, all-null batches, int/float,
decimal precision/scale widening, string/binary, bool, bytes, and timestamps,
plus the mixes where both builds raise (string/int, bool/int, bytes/int,
beyond-double ints with floats). Decimal mixed with non-decimal key values has
no promotion byte-identical to whole-column inference, so that combination is
rejected fail closed (`out_of_core_fk_key_dtype_unsupported`) at the join
concat, before any output is produced, instead of drifting. FK column output
is byte-identical to the pre-C2 build on all fixtures, including a new
cross-batch orphan fixture spanning batch boundaries under every orphan
policy. One disclosed, intentional schema-fidelity change on non-FK columns:
because C2 splices only the FK columns into the resident child frame, non-FK
child columns keep their source Arrow types (for example `dictionary<string>`
and `large_string`), where the pre-C2 build returned the DuckDB-round-tripped
table and degraded those types to `string`. Values on those columns are
byte-identical; only the in-memory column type differs, in the richer
direction, and a test pins the preserved types. Residuals, per the amended C2
acceptance: the child input/output frame is still fully resident and output
staging to the sink is whole-table (both C3, input-side streaming), and
`remap_values` are still runner-precomputed at child-row cardinality
(Sprint 2.1 / risk register #1).

**C3 landed (streaming input side, 2026-07-01):** `LazySource`
(`out_of_core/_source.py`, a path-backed batch-readable Parquet handle; C3b),
the fixed-schema per-batch child joiner `ChildFkBatchJoiner`
(`_batch_join.py`; C3c, type envelope recorded in the C3c section above), and
the batch-streaming runner (`_runner.py` rewrite plus `_emit.py`/`_stage.py`;
C3d). `run_fk_out_of_core` now accepts `sources: Mapping[str, pa.Table |
LazySource]` and, on the sink path, streams each table end to end: bounded
raw batches through per-batch masking and per-batch FK joins into
`TransactionalSink.write_batches` under one analytically fixed schema, with
parent-key columns teed to a narrow staged Parquet copy for the outgoing
relations. No whole table is resident at any point on the lazy-source path
(pinned by a test that forbids `LazySource.to_table`). The no-sink path
reassembles batches with whole-column type semantics, byte-identical to the
whole-table build and the pandas oracle on the parity suite.

**C4 landed (hardware-sized memory budget and spill, 2026-07-02):**
`out_of_core/_budget.py` gives a caller one knob: `resolve_budget(budget_bytes
| None)` returns the DuckDB `memory_limit` string plus a `batch_rows`, sized
to an explicit budget or to a conservative 1/4 of detected host RAM (POSIX
sysconf, /proc/meminfo fallback, coded failure when neither exists; no psutil
dependency). Floors keep the result sane (64 MB budget floor; `batch_rows` in
[1,024, 65,536], never sized by table cardinality; the ceiling IS the pinned
default so big hosts do not silently change pinned behavior).
`run_fk_out_of_core` gained `batch_rows` (threaded through the relation
builds, join iteration, and sink staging; byte-transparent on output, pinned
by test) and `temp_disk_budget_bytes` (the spill guard
`check_temp_disk_budget`, checked at table boundaries; trips fail-closed with
`out_of_core_temp_disk_exceeded` and aborts the sink). Acceptance held: the
same job completes across different caps with byte-identical output
(`tests/unit/execution/test_out_of_core_budget.py`).

**C5 landed (capability proof, 2026-07-02):** the track's acceptance is
measured and pinned. `scripts/fk_memory_probe.py` gained `--mem-cap-mb`
(hard `resource.setrlimit` cap applied in the worker; RLIMIT_DATA by default
after testing both, RLIMIT_AS proved reservation-blunt) and `--capability`
(all three routes in capped subprocesses over one shared chunk-written
Parquet chain). At the tuned operating point (400k rows/table x 3 tables,
width 16, 1,024 MB cap): full-frame OOM, `run_sequential` OOM, out-of-core
completed at 426 MB peak RSS with FK parity verified on the committed output
(both FK edges, 1,960 links per edge, non-vacuous) and 106 MB peak scratch
disk. Opt-in
`benchmark`-marked test:
`test_out_of_core_completes_where_in_memory_routes_oom`
(`tests/perf/test_out_of_core_memory_sentinel.py`). Numbers, cap mechanics
(allocator pinning, failure classification), and the operating-point
rationale: `relationships-memory-scaling.md` section 6.3.

Landed on `feat/option4-out-of-core`: the kernel (`src/decoy_engine/kernel/`),
FK match-key normalization (`src/decoy_engine/execution/_fk_keys.py`), and the
out-of-core route (`src/decoy_engine/execution/out_of_core/`: `_compat.py`,
`_relation.py`, `_join.py`, `_duckdb.py`, `_runner.py`), wired into
`PolarsExecutionAdapter` as a three-way router behind `enable_out_of_core`
(default `False`). Admitted surface matches §2: mask-only, single-column FK
edges, acyclic graph, parent key strategy in `hash`/`redact`/`truncate`/
`passthrough`, `fail`/`remap`/`preserve`/`warn` orphan policies. Reviewed
twice by dennis; **8 findings remediated, final verdict 8/8 closed, 0 new
defects**. CI gates (parity, substrate matrix, sentry) green. Not yet merged;
not yet wired into the platform job runner; whole-stack FK-RI memory-scaling
merge stays gated on engine PR #22.

### Known limitations

- **Bool-typed FK keys diverge from the pandas oracle (LOW, pathological,
  pre-GA).** `fk_key_value` (`execution/_fk_keys.py`) normalizes a bool FK key
  to itself, but pandas's dict-based parent map collides `True == 1`
  (`hash(True) == hash(1)`), so a bool child key can match an int parent there.
  The out-of-core join encodes the two distinctly (`\x00BOOL:1` vs
  `\x00INT:1`, `fk_join_key`) and would instead orphan a bool child key that
  the pandas oracle would have matched. `check_out_of_core_compatibility`
  (`out_of_core/_compat.py`) does not currently reject bool-typed FK columns,
  so this is reachable, not just theoretical, for any config that uses a
  bool-typed column as an FK key (pathological but not impossible). Fix
  options for a later sprint: either reject bool-typed FK columns at the gate
  (simplest, narrows the admitted surface further) or make the out-of-core
  join key bool-collapsing-into-int to match pandas (changes the canonical FK
  key encoding, needs its own parity pass). Tracked here beyond the inline
  comment in `_fk_keys.py:28-33` so it survives beyond a single code review.
- **Relation builder and child join chunk-bounding CLOSED by C1 + C2; input
  side CLOSED by C3 (measured by S1.5, pre-GA; capability proven by C5).**
  Both halves of the Tier-1 risk-register item (§3.1, "no Python structure may
  be sized by total key cardinality") are closed: Sprint C1 (landed
  2026-07-01) rewrote `_relation.py` so both `build_parent_key_relation` and
  `build_parent_key_relation_from_tables` stream bounded record batches into
  DuckDB via a registered lazy `RecordBatchReader`, with masking applied per
  batch, and Sprint C2 (landed 2026-07-01) rewrote `_join.py::mask_child_fk`
  so the child-key side streams the same way and the ordered join result is
  read back batch-wise, never as one whole Arrow table. Sprint C3 (landed
  2026-07-01) closed the input side: `run_fk_out_of_core` takes `LazySource`
  inputs and streams each table through per-batch masking/joins into the sink,
  so no table is whole-resident on that path (see the C3 status paragraph
  above). Remaining residual: `remap_values` for `orphan_policy: remap` are
  minted per batch in the streaming joiner, but the whole-table
  `mask_child_fk` path still precomputes them at child-row cardinality; that
  is Sprint 2.1 / risk register #1. The S1.5 peak-RSS measurements above
  predate C1-C3 and stand as the historical record; the capability yardstick
  for the track, Sprint C5, is measured and green
  (`relationships-memory-scaling.md` section 6.3).
