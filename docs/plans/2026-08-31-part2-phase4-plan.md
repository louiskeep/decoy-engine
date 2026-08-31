Status: plan

# Part 2 Phase 4: global and relational streaming (research + per-strategy design)

Part 1 (Phases 0-3, the native masking hot path) is merged. This document is the Phase 4 research and
design artifact: the DuckDB/dependency capability map, the established algorithms, and a complete
per-strategy table (how each strategy works today, how it must work streaming, the established method,
and whether an identical-output streaming form exists). It defines dependency-closed slices; it does
NOT commit a build. Part 2's activation rule requires the smallest dependency-closed slice with a
frozen workload, baseline, target, budget, benefit, and safety/evidence deps, activated only on
measured evidence.

## The hard rule (from Cam, 2026-08-31)

Every strategy's streaming output MUST be byte-identical to the current pandas Python code, without
exception, tested to the Phase 2-3 parity + mutation bar. This is not a re-freeze program: no strategy
adopts a different-but-deterministic contract to become streamable. If a strategy cannot be reproduced
exactly under a bounded-memory method, it STAYS ON THE ORACLE (coded ineligibility, never a silent or
approximate substitution). We do not rush; correctness of the identical output outranks covering more
strategies. Use established methods (DuckDB native ops where they bound memory; textbook external-sort;
established keyed-hash/join patterns), not hand-rolled novelties.

## Evidence in hand (2026-08-31, this box, 12 GB)

Native C1 masking, peak RSS (external VmHWM) is FLAT with row count: 215 MB (10k), 340 (1M), 355 (3M),
329 (10M, allocator-pinned). Oracle full-frame grows: 2053 MB (1M), 5754 MB (3M), ~19 GB extrapolated
at 10M (OOMs a 12 GB box). At 10M native completes in ~9 min at ~330 MB where the oracle cannot run at
all on laptop-class hardware. This is the first hard datapoint for the 100M-on-laptop thesis and the
baseline the FK-streaming slice must preserve.

## 1. The current reality: THREE routes, not a tier ladder (repo census)

Phase 4 is not greenfield. The engine already has three execution surfaces; a strategy's "streaming
status" depends on which it is on:

- **Oracle** (`_pipeline.py`, `_sequential.py`, `_chunked.py`): the pandas ground truth, full-frame or
  table-resident, does the real FK parent-map join. The byte-parity reference for everything else.
- **Native columnar-streaming** (`execution/native/_dispatch.py`): chunked, NON-FK only (any declared
  relationship reroutes the whole table to oracle). Admits exactly 5 strategies today: `passthrough`,
  `redact`, `truncate`, `hash` (compiled Rust kernel), `faker` (C1-allowlisted bounded pool).
- **Out-of-core FK-relational** (`execution/out_of_core/`): DuckDB-backed batch streaming, used when a
  table participates in a declared FK relationship and clears `check_out_of_core_compatibility`. This
  route ALREADY streams a WIDER payload surface than the native route: `fpe`, `categorical`
  (deterministic), `text_redact`, `text_mask`, conditional `code_set`/`bucket_perturb`. Its FK
  join/parent-key surface is narrower (`hash`/`redact`/`truncate`/`passthrough`).

Two corrections this forces on the naive framing:
1. FK streaming EXISTS. The Phase 4 FK question is "unify the OOC payload surface with the native route,
   widen the join/parent-key surface, and make the OOC join never-OOM at arbitrary N" (the shelved
   OOC-B work), not "build FK streaming."
2. `fpe`/`text_redact`/`text_mask`/deterministic `categorical`/`code_set`/`bucket_perturb` are NOT
   held; they stream today inside an FK context. The real native-route gap for them is "also admit them
   for a plain non-FK table," which is mostly kernel/port work, not algorithm work.

## 2. The two shared primitives Phase 4 needs

Almost every remaining non-partitionable strategy reduces to one of two primitives. Build these once,
correctly, and the per-strategy work becomes small.

### 2a. Durable global row number (DGRN)

Several strategies are deterministic and per-row EXCEPT that they key on the row's global position
(`i` from `enumerate`), which today only exists in a full-frame pass: `sequence` (`start+i*step`),
`windowed_date` (`derive(mask_key, ns, i.to_bytes(8))`, flagged "partitionable but uncertain" in the
determinism protocol for exactly this reason), and any strategy that needs a stable per-row order.
The primitive: a durable, contiguous global row number assigned to each source row once, stable across
partition boundaries, carried through the chunk route (the OOC route already mints `__decoy_row_nr`;
this generalizes it to the native chunk route). With a DGRN, `sequence` (`start + DGRN*step`) and
`windowed_date` (`derive(mask_key, ns, DGRN.to_bytes(8))`) become byte-identical per-row streaming ops,
no sort needed -- the cheapest Phase 4 wins. Byte identity has admission conditions: the DGRN must be
assigned at the SAME logical point the oracle enumerates (before any route-local filtering or
quarantine, so the numbering matches), with the same namespace, NumPy version + RNG call, and pandas
date parsing/arithmetic/formatting; `sequence` must use Python integer semantics (no native overflow).
DGRN does NOT solve the whole-column seeded-RNG generation strategies (`null_probability`,
`categorical`(gen), etc.) -- those advance one contiguous stream and stay on the oracle.

### 2b. Bounded external sorter (from OOC-B, already Codex-consulted)

DuckDB's global `ORDER BY` does NOT bound memory (the OOC-B 200M@8GB run peaked ~21 GB;
`memory_limit` covers the buffer manager, not the sort's working set), and DuckDB's `ROW_NUMBER()`
window state does not spill on the pinned 1.5.4 (OOMs at ~33M groups). So order-restoration and
per-group ordinals cannot go through DuckDB. Decoy's own byte-capped external merge sorter
(`_external_sort.py::ExternalRowNrSorter`, textbook run-generation + capped-fan-in k-way merge
collapsing to one run, fail-closed contiguity) is the bounded primitive. Generalize it to
`BoundedExternalSorter` keyed by an arbitrary deterministic key. It never changes an output value, only
the memory envelope, and it is the shared engine for: FK row_nr reorder (OOC-B), and the grouped-series
durable ordinal. (It does NOT rescue shuffle -- see the per-strategy table.)

## 3. Capability map (DuckDB 1.5.4 + deps): what bounds memory, what does not

| Capability | Bounded OOC? | Use for Decoy | Source |
|---|---|---|---|
| DuckDB external hash join (>=1.2.0, radix-partition both sides) | YES (perf "graceful degrade", pair with enforced `memory_limit` + disabled optimizers) | FK join 4b; already used by the OOC route | DuckDB "Saving Private Hash Join" |
| DuckDB scalar `GROUP BY` hash aggregate (external) | YES for scalar state (`max(int)` over 20M groups @ 600MB) | last-write-wins dedup `max(row_nr) GROUP BY` | DuckDB "External Aggregation" |
| DuckDB `ORDER BY` global sort | Supports external sort, but NOT reliably PROCESS-memory-bounded (Decoy measured 21 GB @ 8 GB limit, 200M): `memory_limit` bounds the buffer manager, not every allocation | do NOT route order-restore through it for the never-OOM guarantee; use Decoy's own capped external sorter | OOC-B measurement; DuckDB OOM guidance |
| DuckDB `ROW_NUMBER()`/window at wide cardinality | Supports larger-than-memory windowing, but the measured plan (`QUALIFY row_number()`, 33M groups @ 1.6 GB) OOMed -- it did not meet Decoy's envelope | do NOT use for the per-group ordinal on the pinned 1.5.4; use the external sorter (the measured failure, not a categorical "never spills", is the reason) | repo measurement (`_relation.py`) |
| DuckDB `arg_max(struct)`/`list()`/`string_agg` wide state | NO (state doesn't serialize) | avoid; split into scalar-state plans | repo `_relation.py` |
| DuckDB <-> Arrow scan (input) | YES (streamed `RecordBatchReader`) | single-pass child stream | DuckDB "SQL on Arrow" |
| DuckDB ASOF join memory behavior | UNKNOWN (undocumented) | open question; measure before use | -- |
| pyarrow `sort_indices`/`take` | NO (in-memory kernels) | only inside a caller byte-cap (the external sorter's pattern) | Arrow docs |
| pyarrow IPC streaming | YES (sequential batch read/write) | spill mechanism for sorted runs | Arrow docs |
| numpy | NO spill primitive | cap array size before calling | -- |
| polars streaming engine (`collect(engine="streaming")`) | YES but UNUSED + silent-fallback + version-pin conflict (`opendp[polars]` pins 1.36.1) | option to EVALUATE with a fail-closed coverage gate; not wired; FK/composite already route to the pandas oracle | polars docs |

Load-bearing negative: order-restoration and per-group ordinals must use Decoy's own external sorter,
never DuckDB `ORDER BY` or `ROW_NUMBER()`. FK joins CAN use DuckDB's bounded external hash join.

## 4. Established algorithms (cited)

- **External merge sort** (Knuth TAOCP v3 §5.4): run generation + k-way merge, resident memory O(M)
  independent of N. The shared sort primitive.
- **Deterministic shuffle**: Fisher-Yates is inherently sequential over the whole array, so no ONE-PASS
  streaming algorithm reproduces it. Two forms exist: (a) an EXACT reproduction at O(n) DISK / bounded
  RAM: materialize the index array `[0..n-1]` to a file-backed `numpy.memmap`, run the SAME pinned NumPy
  PCG64 shuffle against it (identical draws and swaps), then externally gather the values by the
  resulting indices -- byte-identical, but O(n) disk and pathological random I/O; (b) established
  streamable substitutes -- sort-by-keyed-random-key (BigQuery `ORDER BY FARM_FINGERPRINT`, Spark, Dask,
  US10713589) or a Feistel/format-preserving index permutation -- both O(1)/bounded but a DIFFERENT
  permutation, so ruled out by the identical-output rule. Under identical-output, shuffle is DEFERRED to
  the oracle for now (the exact form (a) is buildable but expensive; activate only if evidence justifies
  the O(n)-disk cost), NOT theoretically impossible.
- **Durable per-group ordinal** (SQL `ROW_NUMBER() OVER (PARTITION BY g ORDER BY k, tiebreak)`
  semantics): sort by `(group, order, stable-tiebreak)` then a single pass holding O(1) per-group state.
  A stable tiebreak is MANDATORY for a deterministic ordinal; execute the sort with the bounded external
  sorter (not DuckDB's window).
- **FK-preserving join**: two cases. (4a) If the masked parent key is a keyed FUNCTION of the old key
  `new_pk = F(seed, old_pk)` (Decoy's `hash`, `fpe`), the child FK remaps INDEPENDENTLY via the same F
  (`new_fk = F(seed, old_fk)`) -- NO JOIN, O(1)/row, RI automatic FOR MATCHED KEYS. This is the default
  and cheapest path, but it is admission-gated (the repo's `_chunked_fk.py:210` already encodes the
  conditions): parent and child must evaluate the EXACT same function over the EXACT same canonical input
  -- same seed/key, namespace, hash truncation / FPE tweak / charset / checksum config, compatible
  raw-value + canonicalization domains and key types, and defined component semantics for composite
  keys. Orphan policy matters: only `remap` is join-free byte-identical (an orphan FK masks through the
  same F); `warn`/`fail`/`preserve` need knowledge of parent membership, so they cannot use the pure
  join-free path. (4b) Any non-function parent mask (pool selection, global uniqueness,
  parent-dependent collision resolution) or a membership-requiring orphan policy needs the actual join:
  GRACE/hybrid partitioned hash join (DuckDB's own, bounded).

## 5. The complete per-strategy design table

Legend for "Streaming design": how the byte-identical streaming form works, or "ORACLE" if none exists
under the identical-output rule. "Gap" classes: NATIVE (already on the native route), OOC (already
streams in FK context), DGRN (needs the durable global row number), SORT (needs the bounded external
sorter), PORT (partitionable, only an implementation port), PREPASS (needs a whole-column pre-pass),
ORACLE (no bounded identical-output form).

### Masking

| Strategy | Now | partition | Streaming design (established method) | Identical? | Gap |
|---|---|---|---|---|---|
| passthrough | Arrow zero-copy | yes | already native | yes | NATIVE (done) |
| redact | py-loop over Arrow | yes | already native (kernel) | yes | NATIVE (done) |
| truncate | py-loop over Arrow | yes | already native (kernel) | yes | NATIVE (done) |
| hash | Rust keyed kernel | yes | already native; FK via 4a (F(seed,old_key)) | yes | NATIVE (done); FK-4a |
| faker (C1) | Python PoolSampler | yes (reuse mode) | already native (Phase 3); widen provider allowlist next | yes | NATIVE (done) / PORT (widen) |
| fpe | HMAC-Feistel per-value | yes | streams in OOC today; add a native non-FK kernel/port; FK via 4a | yes | OOC / PORT |
| categorical (det) | derive_index/CDF per-value | yes | streams in OOC today; native port | yes | OOC / PORT |
| text_redact | per-cell span splice | yes | streams in OOC today; native port | yes | OOC / PORT |
| text_mask | per-span keyed | yes | streams in OOC today; native port | yes | OOC / PORT |
| code_set (mask, no chapter) | HMAC modular select | yes | streams in OOC conditionally; native port | yes | OOC / PORT |
| bucket_perturb (explicit fmt) | HKDF offset per-value | yes | streams in OOC with explicit date_format; native port | yes | OOC / PORT |
| group_key | per-row `derive` | yes | PORT only: draw is per-row source-keyed; grouped into cross-row set by convenience, NO correctness blocker | yes | PORT (clean) |
| date_shift | vectorized + per-value fmt | yes | needs a per-value format-error quarantine channel on the stream route (its only blocker); then per-value keyed | yes | PORT + row-error channel |
| bucketize | vectorized | n/a | same per-value format-error channel gap as date_shift | yes | PORT + row-error channel |
| windowed_date | keyed on row index `i` | uncertain | DGRN: pin `i` to the durable global row number, then per-row keyed (exact) | yes | DGRN |
| sequence (gen) | `start+i*step` | needs DGRN | DGRN: per-row positional, exact | yes | DGRN |
| grouped_series | per-group sequential walk | no | SORT: bounded-external-sort by `(group, order, stable-tiebreak)`, one pass per-group walk with `derive(seed,ns,group)`; admit ONLY shapes where the ordinal is proven byte-identical to pandas groupby order, else ORACLE | yes on admitted shapes | SORT (proof-gated) |
| top_code (percentile cap) | whole-column | global | PREPASS: one bounded pass to compute the percentile cap (a scalar), then per-row; exact | yes | PREPASS |
| derived_aggregate | whole-column scalar broadcast | global | PREPASS: one bounded aggregate pass (DuckDB scalar GROUP BY is bounded), then broadcast; exact | yes | PREPASS |
| shuffle | whole-column PCG64 Fisher-Yates | no | DEFERRED-oracle: an exact reproduction exists at O(n) DISK (memmap the index array, run the same PCG64 shuffle, gather) but is expensive; keyed-sort/Feistel are bounded but change output. Oracle for now; activate the exact external form only on evidence | yes, but only via O(n)-disk external permutation | ORACLE (deferred, not impossible) |
| joint_mask | multi-col ref-table row select | yes (within version) | PORT: per-row source-keyed, but needs a reference-table state channel on the stream route | yes | PORT + state channel |
| geo_generalize | whole-dataset k-anon cascade | no | ORACLE: thresholds each row on whole-dataset counts per generalization level | no bounded identical form | ORACLE (Phase 5) |
| derived | per-row Lark expr, dynamic type | n/a | ORACLE: needs same-row sibling context + data-dependent output type | -- | ORACLE (Phase 5) |
| formula | shared RNG over whole column | no | ORACLE: one shared RNG stream advanced across rows; order-dependent | -- | ORACLE (Phase 5) |
| nested | per-cell JSON + child dispatch | n/a | ORACLE until the child dispatch stack ports; architectural | -- | ORACLE (Phase 5) |
| composite / group | multi-col bundle | pool-keyed | PORT: pool-backed keyed selection (like faker) but multi-column output channel needed | yes | PORT (multi-col) |

### Generation (none on a streaming route today; generation runs a separate path)

| Strategy | Now | partition | Streaming design | Identical? | Gap |
|---|---|---|---|---|---|
| statistical (per-row reseed) | `rng.seed(col_seed+i)` per row | yes | route generation through a streaming coordinator; already partition-safe by construction | yes | PORT (route generation) |
| identifier (deterministic, 9 adapters) | per-row `derive_value` | yes | per-row source-keyed; clean once generation is routed | yes | PORT |
| pool_deterministic | per-row derive_index (reuse) | yes (reuse) | the Phase 3 pool primitive; widen beyond C1 allowlist | yes | PORT |
| sequence | positional | DGRN | DGRN (as above) | yes | DGRN |
| reference (FK gen) | one shared `random.Random` stream | no | ORACLE: stream-positional; coupled to generated-FK-parent resolution | no bounded identical form | ORACLE |
| categorical (gen), null_probability, distribution_snapshot, pool_nondeterministic, composite_build | whole-column seeded RNG (`choices(k=n)`, `random(n)`, `.integers(size=n)`) | no | ORACLE: one contiguous seeded RNG stream over the whole column; a partition can't resume mid-stream at the identical draw | no bounded identical form | ORACLE |
| identifier (non-deterministic) | unseeded | no | ORACLE (non-deterministic by contract) | n/a | ORACLE |

## 6. Dependency-closed slices (activation records, smallest-first)

Ordered by evidence and dependency. Each is a separate Cam activation with a frozen
workload/baseline/target/budget/benefit/safety-deps.

- **P4-A: FK streaming to arbitrary N (un-shelve OOC-B) + FK-4a fast path.** Most-evidenced (200M OOM
  proven; OOC-B Codex-consulted + preserved). Land the FK-4a independent-remap default (no join for
  `hash`/`fpe` parent keys), and the OOC-B bounded external reorder for the 4b join case. Byte-parity is
  the existing hard gate. Builds `BoundedExternalSorter`, the shared primitive.
- **P4-B: DGRN + `windowed_date` + `sequence`.** Small: define the durable global row number on the
  native chunk route, then two per-row strategies become byte-identical streaming ops. No sort, no join.
- **P4-C: native ports of the OOC-already-streaming payload strategies** (`fpe`, `categorical`-det,
  `text_redact`, `text_mask`, `code_set`, `bucket_perturb`) + `group_key` (clean PORT). Widens the
  non-FK native route to what OOC already proves streamable. Kernel/port work, not new algorithms.
- **P4-D: PREPASS strategies** (`top_code`-percentile, `derived_aggregate`): one bounded pre-pass
  (DuckDB scalar GROUP BY is bounded) then per-row.
- **P4-E: `grouped_series`** via the external sorter, admission-gated on the differential proof that the
  ordinal equals pandas groupby order byte-for-byte.
- **Row-error channel** (cross-cutting): `date_shift`/`bucketize` need a per-value format-error
  quarantine channel on the stream route before they port; sequence it with P4-C.
- **Oracle-held for exact output:** `shuffle` (DEFERRED: an exact O(n)-disk external permutation is
  buildable but expensive; oracle until evidence justifies it), `geo_generalize`, `derived`, `formula`,
  `nested`, and the stream-positional generation RNG strategies (`reference`, `categorical`(gen),
  `null_probability`, `distribution_snapshot`, `pool_nondeterministic`, `composite_build`). Recorded as
  coded ineligibilities. Only shuffle has a known exact bounded-disk path; the rest need an architectural
  change or are non-deterministic by contract.

## 7. Acceptance (per slice)

- BYTE-IDENTICAL output to the pinned pandas oracle for every streamed strategy (values, order, nulls,
  warnings), the Phase 2-3 parity + mutation bar. Exact, or the strategy stays on the oracle.
- Bounded-memory envelope proven at the claimed tier (never-OOM, resident memory independent of row
  count) under the OOC-B allocator + sorter discipline.
- The intended route provably ran (route evidence); oracle completion / admission rejection / silent
  fallback are gate failures.
- dennis + Codex per slice; nothing weakens a Part 1 gate or the frozen FK byte-parity contract.

## 8. Open questions for Cam / for measurement

- P4-A (FK never-OOM) is the recommended first activation: most-evidenced, byte-identical already,
  builds the shared sorter, design preserved.
- The polars streaming engine is a real bounded option but unused, silent-fallback, and version-pinned;
  evaluate only with a fail-closed coverage gate, not as a default.
- DuckDB ASOF join memory behavior is unmeasured; do not design on it until tested on 1.5.4.
- Confirm the `grouped_series` ordinal==pandas-order equivalence set before P4-E (it decides how much of
  grouped-series can stream vs stays on the oracle).
