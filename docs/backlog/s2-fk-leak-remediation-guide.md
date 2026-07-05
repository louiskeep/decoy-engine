# S2 BLOCKER Remediation Guide: quarantine-aware FK resolution (raw-PII child leak)

**Tech-lead-authored (Opus). Build agent: Sonnet. Implement strictly from this guide.**
Program: "Finish Open-Ended Surfaces". Sprint S2, dennis BLOCK remediation. Engine track.
Worktree: `/home/cam/vscode/engine-integration`, branch `program/finish-open-ended`, builds on head `56ca3a9` (S2).
Repo docstrings/comments are ASCII only (no em-dash, no `->`/`<->` arrow glyphs in prose): the `source_hygiene` sentry gate rejects them. Keep every comment/docstring you write ASCII-clean. Use the word "to" or a hyphen pair is fine only inside code; in prose write "maps to".

---

## 0. The blocker, in one paragraph (verified by running the repro)

When a **masked parent FK-key column** keeps its raw value on a *covered* per-row error
(`date_shift`/`bucketize` emit `format_error` and LEAVE the original cell; `code_set` emits
`mask_error` and leaves it -- `_date_shift.py:84`, `_bucketize.py:126`, `_code_set.py:118`), the parent
row IS quarantined out of the parent output, but the FK parent-map is built from the FULL pre-filter
frame (`_pandas_adapter.py::_parent_map`, L398-441), so `parent_map[raw_key] = raw_key`. Every child row
referencing that key resolves through the map (`resolve_fk_keys`, `_orphan.py:57`) and receives the RAW
parent key; the child row carries no row-error of its own, so it is never quarantined or flagged, and it
is PUBLISHED. Raw PII leaks silently into committed child output. This reproduces on BOTH
`execution_mode="full_frame"` (`_pipeline.py` D8 block) and `execution_mode="sequential"`
(`_sequential.py`). Confirmed live:

```
$ python scratchpad/repro_leak.py
route: full_frame
CHILD parent_id out: [..., 'notadate']
raw 'notadate' in CHILD output : True   <-- LEAK
```

**Read before you touch code:**
- `src/decoy_engine/execution/_pandas_adapter.py` -- `run()` L143-234, `_dispatch_mask_node` L236-297, `_resolve_fk_node` L337-396, `_parent_map` L398-441. (you edit all four)
- `src/decoy_engine/execution/_sequential.py` -- `run_sequential` L96-339, per-table loop L201-291. (you edit)
- `src/decoy_engine/execution/_strategies/_orphan.py` -- `resolve_fk_keys` L37-102. (you edit)
- `src/decoy_engine/execution/_pipeline.py` -- `run_pipeline` D8 block L433-477, `_execution_telemetry` L113-136, route decision L267-321. (you edit)
- `src/decoy_engine/execution/_row_errors.py` -- `RowError`, `RowErrorRecord`, `drain_row_errors`. (read only; `RowError` you will construct)
- `src/decoy_engine/quarantine.py` -- `compute_quarantine` L105-191 (already extracted; you reuse it, no change).
- `src/decoy_engine/relationships/_graph.py` -- `RelationshipEdge` (L86-91: `parent_table`, `parent_columns`, `child_table`, `child_columns`, `namespace`, `orphan_policy`), `OrphanPolicy` enum (PRESERVE/REMAP/WARN/FAIL, L64-67), `parents_of` L113-119.
- `tests/integration/test_fk_sequential_row_error_leak.py` -- the SAFE-case test to EXTEND (currently masks the key via faker, which never errors -- that is why it passes today while the leak is open).
- `scratchpad/repro_leak.py` -- the executed leak reproduction; the HIGH test is built from this shape.

---

## 1. Ground truth (verified in code -- do not re-derive; build on these)

1. **Row errors are position-indexed against the full table frame.** `RowError.row_index`
   (`_row_errors.py:41`) is the 0-based position in the table's frame. The S1 when-gate fix already
   remaps gated-subset indices back to full-table positions, so `row_index` is ALWAYS the full-frame
   position (pinned by `test_fk_sequential_row_error_leak.py::...full_table_index`). `_parent_map`
   iterates the parent frame `for i in range(n)` (L434) by that same position, so `row_index` and `i`
   are directly comparable. This alignment is what makes the exclusion exact.
2. **A parent FK-key column is always snapshotted pre-mask.** `run()` L179-187 and `run_sequential`
   L213-217 snapshot every `edge.parent_columns` column BEFORE masking. So in `_parent_map`, `src_lists`
   (L431) carries the RAW pre-mask key value at each parent row; when a key cell errored and was kept raw,
   `src_lists[.][i]` and `masked_lists[.][i]` are BOTH the raw value (that is the `raw_key -> raw_key`
   map entry that leaks).
3. **A row-error on a NON-key column must NOT change FK resolution.** The existing accepted "safe case"
   (`test_fk_sequential_row_error_leak.py`, bad cell on non-key `age`, key `id` via faker) quarantines the
   bad parent row from the PARENT output but keeps the child pointing at the correctly masked key. That is
   the pre-existing byte-parity behavior. The fix must fire ONLY when the error is on a **parent-key
   column**; a non-key error leaves the map and the child untouched.
4. **`resolve_fk_keys` (`_orphan.py`) has exactly one caller** (`_resolve_fk_node`, `_pandas_adapter.py:392`).
   `_parent_map` has three call sites (L360, L364 inside `_resolve_fk_node`; L257 the explicit sequential
   pre-build). `_dispatch_mask_node` has two (L206 in `run()`, L221 in `run_sequential`). All contained;
   the signature changes are safe.
5. **Full-frame drains after EVERY node** (`run()` L221) into a local `row_error_records` list, then
   `run_pipeline`'s D8 block (`_pipeline.py:447-471`) classifies covered vs uncovered and either
   `apply_quarantine`s (covered) or raises `RowErrorsFailedError` (uncovered). FK parents mask BEFORE FK
   children (`order_work`), and `_parent_map` is called lazily during CHILD dispatch, so by the time a
   child resolves, the parent's key-error rows are already drained and available.
6. **Sequential drains per-table** (`_sequential.py:241`), classifies uncovered and raises BEFORE the
   parent map is built (L255-257), BEFORE write, BEFORE eviction. The explicit `_parent_map` pre-build at
   L257 runs when processing the PARENT table -- so the parent's `table_records` are in scope right there.
   The parent-map/errored-key cache is retained until every child consumer is processed (L283-291).
7. **`compute_quarantine` is already the pure core** (`quarantine.py:105`) and removes rows by
   `record.row_index` from an output table. It is reused unchanged: a cascaded child row is expressed as
   an ordinary `RowErrorRecord` on the child table, so the SAME machinery quarantines it on both paths.
8. **Module sizes now:** `_pandas_adapter.py` 506, `_sequential.py` 388, `_orphan.py` 102, `_pipeline.py`
   487, `quarantine.py` 294. Sentry cap 600 (`tests/sentry/test_module_size.py`, `LIMIT=600`); neither
   adapter nor orphan is on the ALLOWLIST. See section 6 for the placement that keeps everything under cap.

---

## 2. Chosen remediation: EXCLUDE-then-CASCADE (hybrid), with rationale

Cam's decision offered two shapes: (A) exclude row-errored parent-key rows from `parent_map` so children
ORPHAN and follow their `orphan_policy`; or (B) cascade-quarantine the child rows whose parent key errored.
**Neither alone is sufficient -- you must do both, in this precedence.** Rationale, proven against the four
orphan policies:

If you ONLY exclude from the map (shape A) and then let `orphan_policy` handle the resulting orphans:
- **PRESERVE** keeps the original source key unmasked (`_orphan.py:83-85`) -- the child re-inherits the RAW
  errored key. **Leak reopens.** (The repro uses `orphan_policy="preserve"` precisely to expose this.)
- **WARN** is PRESERVE + an aggregated warning -- same raw re-inheritance. **Leak reopens.**
- **REMAP** re-runs the errored raw value through the parent column's own strategy (`_make_remap_fn`,
  `_pandas_adapter.py:443`). But that value is exactly the one that FAILED that strategy (e.g. `"notadate"`
  under `date_shift`), so REMAP hits the same uncoercible cell, emits ANOTHER row-error, and (keep-raw
  contract) leaves the raw value. **Leak reopens AND a stray row-error is minted.**
- **FAIL** raises `ExecutionError(orphan_fk_violation)` -- no leak, but it converts a *quarantine-covered*
  parent data-quality error into a hard job failure via the child, which is surprising.

So shape A satisfies "never inherit the raw errored key under ANY policy" for **zero** of the four
policies as-is. The requirement is absolute (dennis: "a child can NEVER inherit the raw errored key under
ANY policy"). Therefore:

> **Chosen design (hybrid, precedence-ordered):** exclude row-errored parent-key rows from `parent_map`
> (so they cannot resolve to the raw value), AND route the specific child rows whose key was excluded to
> **cascade-quarantine** -- i.e. record a synthetic per-row error on the child FK column carrying the SAME
> trigger as the parent key-error, so the existing D8 fail-loud/quarantine machinery removes (covered) or
> hard-fails (uncovered) those child rows uniformly on BOTH paths. `orphan_policy` is applied ONLY to
> *genuine* orphans (child keys that never existed in the parent at all); it NEVER sees a cascaded key.

**Why cascade-quarantine and not "orphan under policy":** the parent row was removed for a data-quality
reason (quarantine), not because it never existed (RI). A child of a quarantined parent is a quarantine
concern, not an RI/orphan concern. Making the child follow the parent's quarantine fate is the only choice
that is uniformly leak-free across all four policies, keeps the parent and child dispositions consistent
(both quarantined together, or both fail loud together), and reuses the entire honesty-pack pipeline with
zero new trigger semantics.

**Why the same trigger as the parent key-error (load-bearing):** coverage is then symmetric. If the parent
key-error's trigger is in `quarantine.triggers`, the parent proceeds and the child cascade is guaranteed
covered too (same trigger) -- both rows land in quarantine, job completes. If the trigger is NOT covered,
the parent short-circuits to `RowErrorsFailedError` BEFORE any child is built (sequential: L243-248
raises at the parent table; full-frame: the parent record is in `row_errors_uncovered`), so the cascade
never runs into a "parent covered, child uncovered" split. Do NOT invent a new `fk_parent_quarantined`
trigger -- it would require operators to add it to every quarantine config or face surprise fail-loud,
and it would break coverage symmetry.

### Precedence in `resolve_fk_keys` (exact order, per child row)
1. `mapped = parent_map.get(key)` is not None  ->  use the masked value (normal resolution, unchanged).
2. else if `key in errored_parent_keys`  ->  **cascade**: set masked value to `None` (never raw), record
   `(child_row_index, trigger)` for the caller to emit a `RowError`.
3. else  ->  **genuine orphan**: existing `orphan_policy` path (PRESERVE/WARN/REMAP/FAIL), byte-unchanged.

Precedence 1-before-2 also correctly handles the degenerate non-unique-parent-key case (a key value that
both errors in one row and masks cleanly in another): if a clean parent row supplies the key, the child
resolves to the real masked value and is NOT cascaded. Only keys present ONLY in the errored set cascade.

---

## 3. The exclusion + cascade algorithm (both paths share one implementation)

### 3.1 The per-parent key-error index (built by both callers)

Introduce a single plumbing structure, built incrementally as each table's row-errors are drained:

```python
# table -> column -> {row_index: trigger}
_KeyErrorRows = dict[str, dict[str, dict[int, str]]]
```

Both `run()` and `run_sequential` maintain one and fold each drained batch in. Only KEY columns matter to
`_parent_map`, but fold ALL drained records (cheap; `_parent_map` intersects with `edge.parent_columns`).

### 3.2 `_parent_map` (in `_pandas_adapter.py`) -- exclude + collect errored keys

Change the signature to accept the key-error index and an errored-key cache (both keyword-only, default
`None` for byte-parity and for every existing direct/test caller):

```python
def _parent_map(
    self,
    edge: RelationshipEdge,
    frames: dict[str, pd.DataFrame],
    source_snapshots: dict[tuple[str, str], pd.Series],
    parent_map_cache: dict[_NodeKey, dict[_KeyTuple, _KeyTuple]],
    *,
    key_error_rows: _KeyErrorRows | None = None,
    errored_keys_cache: dict[_NodeKey, dict[_KeyTuple, str]] | None = None,
) -> dict[_KeyTuple, _KeyTuple]:
```

Inside, before the `for i in range(n)` loop (currently L434), compute the excluded-row map for THIS
edge's key columns, as `{row_index: trigger}`:

```python
excluded: dict[int, str] = {}
if key_error_rows:
    tbl_errs = key_error_rows.get(ptable, {})
    for c in pcols:
        for ridx, trig in tbl_errs.get(c, {}).items():
            excluded.setdefault(ridx, trig)  # first key-column error wins the trigger
```

Then in the loop, skip excluded rows from the map and collect their raw key tuple + trigger:

```python
errored: dict[_KeyTuple, str] = {}
for i in range(n):
    raw = [col[i] for col in src_lists]
    if any(pd.isna(x) for x in raw):
        continue  # parent key with a null component cannot be referenced (unchanged)
    src_t = tuple(_fk_key_value(x) for x in raw)
    if i in excluded:
        errored.setdefault(src_t, excluded[i])
        continue  # EXCLUDE: a row-errored key never enters the resolution map
    out[src_t] = tuple(col[i] for col in masked_lists)
parent_map_cache[cache_key] = out
if errored_keys_cache is not None:
    errored_keys_cache[cache_key] = errored
return out
```

Also update the cache-hit early return (currently L413-415): when `parent_map_cache` already holds the
map, the `errored_keys_cache` entry was populated in the same prior call, so nothing extra is needed --
just return the cached map as today. (`errored_keys_cache[cache_key]` is written whenever
`parent_map_cache[cache_key]` is, so the two caches stay in lockstep.)

**Byte-parity:** when `key_error_rows` is `None`/empty, `excluded` is empty, `out` is built identically to
today, and `errored` is empty. Byte-identical map, no cascade. (See section 5.)

### 3.3 `resolve_fk_keys` (in `_orphan.py`) -- add the cascade branch

Add a keyword-only `errored_parent_keys` param and RETURN a third element (the cascade list). The only
caller is `_resolve_fk_node`, updated in 3.4.

```python
def resolve_fk_keys(
    child_keys: list[_KeyTuple | None],
    parent_map: dict[_KeyTuple, _KeyTuple],
    edge: RelationshipEdge,
    *,
    remap_fn: Callable[[list[_KeyTuple]], list[_KeyTuple]],
    errored_parent_keys: dict[_KeyTuple, str] | None = None,
) -> tuple[list[_KeyTuple | None], list[QualityWarning], list[tuple[int, str]]]:
```

Body: keep the existing loop, but between the `mapped is not None` branch and the orphan-collection
branch, insert the cascade branch (precedence 1-2-3 from section 2):

```python
cascade: list[tuple[int, str]] = []
for i, key in enumerate(child_keys):
    if key is None:
        continue
    mapped = parent_map.get(key)
    if mapped is not None:
        masked[i] = mapped
        continue
    if errored_parent_keys is not None and key in errored_parent_keys:
        masked[i] = None  # NEVER the raw key; the row-error path removes/fails this row
        cascade.append((i, errored_parent_keys[key]))
        continue
    orphan_positions.append(i)
    orphan_keys.append(key)
```

Return `(masked, warnings, cascade)` from every return point (the early `return masked, [], []` when there
are no orphans becomes `return masked, [], cascade`; the FAIL raise is unchanged; PRESERVE/WARN/REMAP
returns append `cascade`). **Genuine orphans are untouched** -- FAIL still raises, REMAP still remaps,
PRESERVE/WARN still preserve+warn -- because a cascaded key was already consumed in branch 2 and never
reaches `orphan_positions`.

### 3.4 `_resolve_fk_node` (in `_pandas_adapter.py`) -- thread caches, emit cascade RowErrors

Add the two keyword params, gather errored keys across all edges (multi-parent: first-hit-wins, mirroring
the map merge at L361-367), pass them to `resolve_fk_keys`, unpack the 3-tuple, and emit one `RowError`
per cascaded child row into `ctx.row_errors`:

```python
def _resolve_fk_node(self, node, edges, frames, source_snapshots, parent_map_cache,
                     node_by_key, ctx, *, key_error_rows=None, errored_keys_cache=None):
    ...
    parent_map = self._parent_map(edge, frames, source_snapshots, parent_map_cache,
                                  key_error_rows=key_error_rows, errored_keys_cache=errored_keys_cache)
    if len(edges) > 1:
        merged = dict(parent_map)
        for extra_edge in edges[1:]:
            extra_map = self._parent_map(extra_edge, frames, source_snapshots, parent_map_cache,
                                         key_error_rows=key_error_rows,
                                         errored_keys_cache=errored_keys_cache)
            for k, v in extra_map.items():
                merged.setdefault(k, v)
        parent_map = merged
    # gather errored keys for the edges this child resolves against
    errored_parent_keys: dict[_KeyTuple, str] = {}
    if errored_keys_cache is not None:
        for e in edges:
            for k, trig in errored_keys_cache.get((e.parent_table, e.parent_columns), {}).items():
                errored_parent_keys.setdefault(k, trig)
    ...
    masked_keys, warnings, cascade = resolve_fk_keys(
        child_keys, parent_map, edge, remap_fn=remap_fn,
        errored_parent_keys=errored_parent_keys or None,
    )
    for j, c in enumerate(child_cols):
        child_frame[c] = [None if mk is None else mk[j] for mk in masked_keys]
    for pos, trig in cascade:
        ctx.row_errors.append(
            RowError(
                column=child_cols[0],
                row_index=pos,
                trigger=trig,
                reason="FK parent key was quarantined for a row error; child row cascaded to "
                       "quarantine to prevent raw parent-key leak",
            )
        )
    return warnings
```

Import `RowError` at the top of `_pandas_adapter.py` (already imports `RowErrorRecord, drain_row_errors`
from `_row_errors`; add `RowError`). `child_cols[0]` is the attribution column for a composite FK (the
quarantine entry carries the whole child row regardless; the column is only for the manifest count key).
The emitted `RowError.row_index=pos` is the child-frame position, which is the full child frame in both
paths, so `compute_quarantine` removes exactly that child row. Note the child masked cell is already
`None` (branch 2), so even if a downstream bug skipped quarantine, the durable value is null, never raw.

### 3.5 `_dispatch_mask_node` (in `_pandas_adapter.py`) -- pass the caches through

Add the two keyword params and forward them to `_resolve_fk_node`:

```python
def _dispatch_mask_node(self, node, frames, relationship_graph, source_snapshots,
                        parent_map_cache, node_by_key, ctx,
                        *, key_error_rows=None, errored_keys_cache=None):
    ...
    if child_edges:
        with timed_strategy("fk_resolve", ",".join(node.columns)):
            return self._resolve_fk_node(node, child_edges, frames, source_snapshots,
                                         parent_map_cache, node_by_key, ctx,
                                         key_error_rows=key_error_rows,
                                         errored_keys_cache=errored_keys_cache)
    ...  # scalar/composite branches unchanged
```

### 3.6 `run()` full-frame wiring (in `_pandas_adapter.py`)

Add two locals next to `parent_map_cache` (L188) and `row_error_records` (L199):

```python
parent_map_cache: dict[_NodeKey, dict[_KeyTuple, _KeyTuple]] = {}
errored_keys_cache: dict[_NodeKey, dict[_KeyTuple, str]] = {}
key_error_rows: _KeyErrorRows = {}
```

Change the drain at L216-221 to fold each batch into `key_error_rows` and pass both caches into dispatch:

```python
for node in ordered:
    if node.table not in frames:
        continue
    warnings.extend(
        self._dispatch_mask_node(
            node, frames, relationship_graph, source_snapshots,
            parent_map_cache, node_by_key, ctx,
            key_error_rows=key_error_rows, errored_keys_cache=errored_keys_cache,
        )
    )
    batch = drain_row_errors(ctx.row_errors, table=node.table)
    row_error_records.extend(batch)
    for rec in batch:
        key_error_rows.setdefault(rec.table, {}).setdefault(rec.column, {})[rec.row_index] = rec.trigger
```

Because parents mask (and drain) before children dispatch, `key_error_rows` is fully populated for a
parent table by the time its child's `_parent_map` runs. The cascade `RowError`s emitted during child
dispatch are drained on the SAME loop iteration (the child node's own `batch`), attributed to the child
table, and flow out on `mask_result.row_errors` into `run_pipeline`'s D8 block unchanged. **No change to
`run_pipeline`'s D8 block is needed for correctness** (the cascade records look like any other covered/
uncovered row-error there) -- but see LOW-1 (section 8) for the ordering-parity fix.

### 3.7 `run_sequential` wiring (in `_sequential.py`)

Add two locals next to `parent_map_cache` (L181):

```python
parent_map_cache: dict[_NodeKey, dict[_KeyTuple, _KeyTuple]] = {}
errored_keys_cache: dict[_NodeKey, dict[_KeyTuple, str]] = {}
key_error_rows: dict[str, dict[str, dict[int, str]]] = {}
```

Right after draining `table_records` (L241-242), fold the KEY-relevant records into `key_error_rows`
BEFORE the fail-loud classification and BEFORE the parent-map pre-build:

```python
table_records = drain_row_errors(ctx.row_errors, table=table)
all_row_errors.extend(table_records)
for rec in table_records:
    key_error_rows.setdefault(rec.table, {}).setdefault(rec.column, {})[rec.row_index] = rec.trigger
if table_records:
    uncovered = tuple(r for r in table_records if not (q_enabled and r.trigger in q_triggers))
    if uncovered:
        raise RowErrorsFailedError(uncovered)
```

Pass both caches into the explicit parent-map pre-build (L255-257) so errored keys are computed at PARENT
time (this is where the parent frame is still resident and the key-error index for the parent is complete):

```python
for edge in graph.edges:
    if edge.parent_table == table:
        adapter._parent_map(edge, frames, source_snapshots, parent_map_cache,
                            key_error_rows=key_error_rows, errored_keys_cache=errored_keys_cache)
```

Pass `errored_keys_cache` (and `key_error_rows`, harmless) into the per-node dispatch (L219-230) so the
CHILD table's `_resolve_fk_node` reads the cache and emits cascade `RowError`s:

```python
for node in nodes_by_table.get(table, ()):
    warnings.extend(
        adapter._dispatch_mask_node(
            node, frames, graph, source_snapshots, parent_map_cache, node_by_key, ctx,
            key_error_rows=key_error_rows, errored_keys_cache=errored_keys_cache,
        )
    )
```

**Ordering that makes this work (do not reorder):** in FK-topo order the PARENT table iterates first --
its records drain (L241), fail-loud on uncovered (short-circuit), then `_parent_map` pre-builds the map
(excluding errored keys) and populates `errored_keys_cache`. The CHILD table iterates later; its
`_dispatch_mask_node` -> `_resolve_fk_node` reads the already-populated `errored_keys_cache`, emits
cascade `RowError`s into `ctx.row_errors`; those drain on the CHILD's own L241, are classified covered
(same trigger the parent proved covered) and quarantine-filtered out of the child output at L263-270
BEFORE the child is written to the sink at L272-273. Import `RowError` is not needed in `_sequential.py`
(the emission happens inside `_resolve_fk_node` in `_pandas_adapter.py`).

Release `errored_keys_cache` alongside `parent_map_cache` in the consumer-done cleanup (L283-291): add
`errored_keys_cache.pop(ck, None)` next to `parent_map_cache.pop(ck, None)`.

### 3.8 Transactional-sink ordering (requirement 5) -- confirm, no new code

On the sequential path the parent output (bad parent row already removed) is staged at L272-273 only after
its covered-quarantine filter; the child cascade is quarantined out of the child output before the child is
staged. If ANY table hits an uncovered record (parent OR a would-be-uncovered child, which cannot happen
with same-trigger cascade but is defended anyway), L243-248 raises inside the `try`, the `except
BaseException` handler (L322-330) calls `_tsink.abort()`, and `ParquetTransactionalSink` discards all
staging -- so a leaked child can NEVER commit while its parent commits, and a fail-loud run publishes
nothing. The commit at L319-320 is reached only when every table masked and quarantined cleanly. Ordering
holds; add no code, but assert it in the test (4.1 case d).

---

## 4. Orphan-policy composition table (the required trace)

After the fix, a child key that references a **row-errored parent key** is ALWAYS cascade-quarantined
(covered) or fail-loud (uncovered) via the row-error path, and NEVER reaches the `orphan_policy` branch.
`orphan_policy` governs ONLY genuine orphans (keys absent from the parent entirely). Result per policy:

| orphan_policy | genuine orphan (key never in parent) | child of a ROW-ERRORED parent key (the blocker) | raw key can leak? |
|---|---|---|---|
| **PRESERVE** | keep source key unmasked (unchanged) | cascade-quarantined (covered) or `RowErrorsFailedError` (uncovered) | **No** |
| **WARN** | preserve + one aggregated `orphan_fk` warning (unchanged) | cascade-quarantined (covered) or fail-loud (uncovered) | **No** |
| **REMAP** | remap via parent strategy (unchanged) | cascade-quarantined -- NOT remapped (avoids re-running the failing strategy on the bad value) | **No** |
| **FAIL** | raise `orphan_fk_violation` (unchanged) | cascade-quarantined (covered) or fail-loud `RowErrorsFailedError` (uncovered) | **No** |

**FAIL semantic note (flag, not a blocker):** under FAIL, a child of a *quarantine-covered* parent key
error is QUARANTINED (data-quality disposition), not raised as an orphan violation. This is deliberate:
the parent existed but was quarantined for a data-quality reason, so it is not an RI "missing parent"
orphan. This is leak-free and internally consistent (parent and child share the quarantine fate). If the
real corpus or a product owner decides FAIL must hard-fail whenever a covered-parent child appears, that
is a one-line policy branch (raise instead of cascade when `edge.orphan_policy is FAIL`). **Do NOT make
that change on your own** -- ship the uniform cascade-quarantine (which satisfies the absolute
"never inherit raw" requirement for all four policies) and leave a `# NOTE` at the cascade site pointing
here. Escalate only if a test in the suite encodes the opposite FAIL expectation (see section 9).

**The absolute invariant, restated for the test:** for EVERY orphan_policy, the raw errored parent key is
absent from the child output. The HIGH test (4.1) parametrizes at least PRESERVE (the repro's policy, the
worst case) and SHOULD add a FAIL/REMAP variant to prove the table above.

---

## 5. Byte-parity no-op argument (requirement 3) -- how to prove it

The fix is a strict no-op when no parent-KEY row-errors exist (the overwhelmingly common case), because:

1. `key_error_rows` only ever contains records for columns that emitted a `RowError`. `_parent_map`
   intersects it with `edge.parent_columns`; if no KEY column errored, `excluded` is empty.
2. With `excluded` empty, the `for i in range(n)` loop takes the identical branch for every row
   (`out[src_t] = ...`), producing a byte-identical `parent_map`, and `errored` stays `{}`.
3. With `errored_keys_cache[cache_key] == {}`, `_resolve_fk_node` passes `errored_parent_keys=None` to
   `resolve_fk_keys` (via the `or None`), so `resolve_fk_keys` takes its pre-fix branches exactly (no
   cascade, `cascade == []`), returns the same `(masked, warnings)` plus an empty third element, and
   `_resolve_fk_node` emits zero `RowError`s.
4. The SAFE case (row-error on a NON-key column, e.g. the existing `age` test) has `excluded` empty for
   the FK edge (its key column `id` did not error), so it is byte-identical too -- the parent row is still
   quarantined from the parent output via its `age` error, the child still keeps its masked key. This is
   the pre-existing behavior; the fix does not touch it.

**Proof obligations (tests, section 7):**
- 3.2-style sequential-vs-full-frame equivalence on a clean FK fixture stays byte-identical (already in
  `test_pipeline_routing.py`; must still pass unchanged).
- The full existing suite (`test_sequential_eviction.py`, `test_transactional_sink.py`,
  `test_run_pipeline.py`, `test_quarantine_e2e.py`, `test_row_errors_e2e.py`,
  `test_when_gate_row_error_leak.py`, and the SAFE cases in `test_fk_sequential_row_error_leak.py`)
  passes unchanged -- these ARE the byte-parity pins. If any moves, the fix perturbed the common path;
  STOP (section 9).
- Add a direct unit assertion (in `test_pipeline_routing.py` or a new `test_fk_key_error_exclusion.py`)
  that `_parent_map(..., key_error_rows=None)` returns a dict equal to `_parent_map(...)` pre-fix on a
  small parent frame (call it both ways on the same frame; equal).

---

## 6. Module-size (600 LOC ratchet) implications

Placement is chosen to keep growth off the two largest files:
- **`_orphan.py`** (102 -> ~130): the cascade branch + the 3-tuple return live here. This is the natural
  home (it already owns FK-key/orphan resolution) and has the most headroom. Preferred growth target.
- **`_pandas_adapter.py`** (506 -> ~545): `_parent_map` exclusion (~12 lines), `_resolve_fk_node` errored-
  key gather + cascade emit (~12 lines), two params on `_dispatch_mask_node` (~4 lines), `run()` locals +
  fold (~6 lines). Stays under 600 with ~55 lines of headroom. **Do NOT add it to the ALLOWLIST.**
- **`_sequential.py`** (388 -> ~405): three locals + a fold + a `.pop` + two kwarg pass-throughs (~15
  lines). Well under.
- **`_pipeline.py`** (487 -> ~500): only the LOW-1 reorder + MEDIUM telemetry + NIT guard (section 8).
- **`quarantine.py`**: unchanged (`compute_quarantine` already extracted).

If `_pandas_adapter.py` unexpectedly crosses 600 (it should not), extract the errored-key gather + cascade
emit from `_resolve_fk_node` into a free function `resolve_fk_cascade(...)` in `_orphan.py` and call it,
rather than growing the adapter or touching the ALLOWLIST. Flag it if you hit this.

---

## 7. Tests the build MUST land (acceptance criteria)

Run the focused set continuously; the full gate (section 10) before declaring done.

### 7.1 HIGH -- the blocker proof: key-column error, raw absent from BOTH outputs, BOTH paths

**Extend** `tests/integration/test_fk_sequential_row_error_leak.py` (do not delete the SAFE cases -- they
are the byte-parity pins). Add a class `TestFkKeyColumnErrorLeakClosure` built from `scratchpad/repro_leak.py`:

- **Shape:** parent `id` = `["2020-01-01","2020-02-01","2020-03-01","2020-04-01","2020-05-01","notadate"]`,
  child `parent_id` = the same six values (one child per parent). The FK key `id` is masked by
  **`date_shift`** (`provider_config={"min_days":1,"max_days":30}`) -- a row-error-emitting strategy on the
  KEY column (this is the difference from the SAFE case, which masked `id` via faker). `orphan_policy`
  starts at `"preserve"` (the worst case). Each table needs a real backing parquet file (profile reads
  from disk) plus the in-memory `sources` dict, exactly as the existing helpers do.
- **Parametrize `execution_mode` over BOTH `"full_frame"` and `"sequential"`** (for sequential, pass a
  `ParquetTransactionalSink(tmp_path/"out")` and read the parquet back; for full_frame, no sink, read
  `result.outputs`). A single parametrized test body covers both paths -- this is requirement 4 (same fix,
  both paths).
- **Assertions (with quarantine `{enabled:True, output_path, triggers:["format_error"]}`):**
  - (a) `"notadate"` is ABSENT from the parent output `id` column.
  - (b) `"notadate"` is ABSENT from the child output `parent_id` column. **This is the assertion that
    FAILS today** (child leaks) and PASSES after the fix.
  - (c) exactly one parent row removed and exactly one child row removed (both outputs have 5 rows).
  - (d) the quarantine JSONL has a parent entry (`id == "notadate"`, trigger `format_error`) AND a child
    entry (the cascaded row; its `parent_id` is `None`, trigger `format_error`, reason mentions the
    parent-key cascade). Assert the child entry's `_source_table == "child"`.
- **Fail-loud variant (quarantine omitted):** `run_pipeline` raises `RowErrorsFailedError`; on the
  sequential path assert the sink target dir was NOT committed (`not (tmp_path/"out").exists()`); assert
  `"notadate"` does not appear in `str(exc)` (trap T3). The parent record's `row_index == 5`.
- **Policy sweep:** add at least a `orphan_policy="fail"` and a `orphan_policy="remap"` variant of the
  quarantine-covered case, asserting (b) raw absent from child for each -- proving the section 4 table.
  (A genuine-orphan row under each policy is optional but nice: add one child row whose `parent_id` is a
  value that exists in NO parent row, and assert PRESERVE keeps it, REMAP masks it, FAIL raises -- proving
  the fix left genuine-orphan handling intact.)

This test MUST FAIL on `56ca3a9` (before the fix) at assertion (b) and PASS after. Capture both in the
ledger (run it once before implementing to confirm red).

### 7.2 Byte-parity regressions (requirement 3)

- `tests/unit/execution/test_pipeline_routing.py` sequential-vs-full-frame equivalence: unchanged, still
  byte-identical on the clean FK fixture.
- Add a `_parent_map` no-op unit (as in section 5): `key_error_rows=None` equals the pre-fix map on a
  small frame.
- The SAFE cases already in `test_fk_sequential_row_error_leak.py` (non-key `age` error): unchanged.

### 7.3 MEDIUM -- `loaded_fully_in_memory` telemetry honesty

See section 8. Add to `test_pipeline_routing.py`:
- sequential + `source_loader=None` + non-empty `sources` -> `loaded_fully_in_memory is True`.
- sequential + a lazy `source_loader` + `sources={}` (or omitted) -> `loaded_fully_in_memory is False`,
  `outputs_streamed is True` (with a sink).
- sequential + a lazy `source_loader` + NON-empty `sources` -> `loaded_fully_in_memory is True` (the
  honesty fix: a resident `sources` dict means inputs are resident regardless of the loader).

### 7.4 LOW-1 -- quarantine-JSONL write-timing parity

Add a test (either file) that a job with BOTH a covered row-error AND an uncovered row-error raises
`RowErrorsFailedError` and writes NO quarantine JSONL on the full-frame path (matching sequential, which
raises before the post-loop write). See section 8 for the reorder that makes this true.

### 7.5 LOW-2 -- multi-table single-JSONL clobber pin

Add a multi-table quarantine test: a parent table with a covered `format_error` on a NON-key column AND a
child table with its own covered `format_error` on a non-FK column, both under one quarantine config.
Assert the single JSONL file contains BOTH tables' entries (parse lines; assert `_source_table` values
`{"parent","child"}` both present). This pins that the sequential single-post-loop `_write_jsonl` does not
clobber earlier tables (the truncating `"w"` mode).

### 7.6 NIT -- forced sequential never silently falls through

Add a test that `execution_mode="sequential"` on a relationship-bearing job with NO mask-kind table
raises `ConfigError` (does not silently run full-frame). See section 8.

---

## 8. Smaller fixes to land in the SAME pass

### MEDIUM -- honest `loaded_fully_in_memory` (`_pipeline.py`)

The current `_execution_telemetry` (L127-136) sets `loaded_fully_in_memory = source_loader is None`, but
`run_pipeline` always does `caller_sources = dict(sources)` (L236) -- so if the caller passed a non-empty
`sources`, the inputs ARE resident even with a lazy loader. Thread residency in:

- Add a `sources_resident: bool` param to `_execution_telemetry`.
- Compute `loaded_fully_in_memory = sources_resident or (source_loader is None)`.
- At the sequential-branch call (L307-312) pass `sources_resident=bool(caller_sources)`. At the
  full-frame call (L475-477) `sources_resident=True` (full-frame always holds all inputs).

Genuinely bounded input residency is now reported ONLY when a lazy `source_loader` is supplied AND
`sources` is empty/omitted -- which is the only configuration that actually bounds inputs. Tests in 7.3.

### LOW-1 -- align quarantine-JSONL write timing (`_pipeline.py` D8 block, L447-471)

Sequential raises on uncovered records BEFORE the single post-loop `_write_jsonl` (so a fail-loud run
writes no JSONL). Full-frame currently calls `apply_quarantine` (which writes JSONL for covered rows)
and THEN raises on the uncovered remainder -- so a mixed covered+uncovered run writes a partial JSONL then
raises. Align full-frame to raise-before-write: reorder the D8 tail so the uncovered/validator-failed
raises happen BEFORE `apply_quarantine` writes. Concretely, compute `validation_covered`,
`row_errors_covered`, `row_errors_uncovered` first (unchanged), then:

```python
if validator_failed and not validation_covered:
    raise ValidatorFailedError(v_report)
if row_errors_uncovered:
    raise RowErrorsFailedError(row_errors_uncovered)
if validation_covered or row_errors_covered:
    from decoy_engine.quarantine import apply_quarantine, quarantine_manifest
    outputs, q_summary = apply_quarantine(outputs, v_report, quarantine_cfg, row_errors=mask_row_errors)
    quality_metrics["quarantine"] = quarantine_manifest(q_summary)
```

Now both paths write the quarantine JSONL only on a fully-covered (successful) run, and a fail-loud run
publishes nothing durable. This is strictly more correct (a raised job leaves no partial artifact). It
does not change the fully-covered success path (the only path that writes JSONL). Test in 7.4. Add a
one-line docstring note in BOTH `_pipeline.py` (D8 block) and `_sequential.py` (finalize block) stating:
"quarantine JSONL is durable only on a successful (fully covered) run; a fail-loud run publishes nothing."

### LOW-2 -- multi-table quarantine test

Test only (7.5). No production change; it pins the existing single-post-loop write.

### NIT -- forced sequential must not silently fall through (`_pipeline.py:294`)

`if has_mask_table and route == "sequential":` silently drops to full-frame if `route == "sequential"` but
`has_mask_table` is False. Guard it: fold `has_mask_table` into the route decision. After computing
`eligible/route_reason`, in the `execution_mode == "sequential"` branch (L279-285) also require a mask
table:

```python
elif execution_mode == "sequential":
    if not eligible:
        raise ConfigError(f"execution_mode='sequential' requested but the job is not "
                          f"sequential-eligible ({route_reason}).")
    if not has_mask_table:
        raise ConfigError("execution_mode='sequential' requested but the job has no mask-kind "
                          "table to run through the sequential path.")
    route = "sequential"
```

Leave the `if has_mask_table and route == "sequential":` branch guard as-is (defensive; now unreachable
with `route=="sequential"` implying `has_mask_table`). Test in 7.6.

---

## 9. STOP and escalate to Cam (do not guess) if:

1. **A test in the suite encodes that `orphan_policy=FAIL` must HARD-FAIL (not quarantine) a child of a
   covered-parent key error.** Section 4 ships uniform cascade-quarantine; if an existing/expected test
   demands FAIL raise instead, that is a product semantics decision (RI-strictness vs data-quality
   disposition). Hand back the conflicting expectation; do not flip the behavior on your own.
2. **The byte-parity pins move** (any of `test_sequential_eviction`, `test_transactional_sink`,
   `test_run_pipeline`, `test_quarantine_e2e`, `test_row_errors_e2e`, `test_when_gate_row_error_leak`, or
   the SAFE cases in `test_fk_sequential_row_error_leak`). The fix must be a no-op on the common path; a
   moved pin means the exclusion or the plumbing perturbed clean FK resolution. Hand back the diverging
   table + columns; do not "adjust the assertion."
3. **The cascade trigger cannot be made to match the parent trigger** (e.g. a composite FK where two key
   columns errored under DIFFERENT triggers and coverage differs). Section 3.2 takes the first key-column
   error's trigger; if that produces a "parent covered, child uncovered" split in a real config, stop and
   describe it -- the coverage-symmetry argument (section 2) needs a product call on which trigger wins.
4. **The same fix fails 2-3 times** (the child leak persists, or byte-parity keeps drifting, or the
   abort/commit ordering leaks). The mental model is wrong; hand back what you tried and observed.

Escalation = append the blocker to the program ledger
`~/.claude/plans/decoy-finish-open-ended-program.md` and stop the loop; that is a successful autonomous
outcome.

---

## 10. CI-gate mirror -- every command must pass before this counts as done

Run from the worktree root with the repo venv (`source .venv/bin/activate`). Mirrors `.github/workflows/ci.yml`
+ `docs.yml` and the `decoy-ci-environment-gotchas` note.

```bash
# 0. FIRST, confirm the blocker is red on 56ca3a9 (before your fix), then implement:
python scratchpad/repro_leak.py            # expect: raw 'notadate' in CHILD output : True

# lint + format (pinned versions matter)
ruff check src tests testflight scripts
ruff format --check src tests testflight scripts

# types
mypy src/decoy_engine testflight

# full regression gate (sentry module_size + source_hygiene run inside this)
pytest tests -m "not benchmark" --tb=short

# focused fast loop while iterating
pytest tests/integration/test_fk_sequential_row_error_leak.py \
       tests/unit/execution/test_pipeline_routing.py \
       tests/unit/execution/test_sequential_eviction.py \
       tests/unit/execution/test_transactional_sink.py \
       tests/integration/test_when_gate_row_error_leak.py \
       tests/integration/test_quarantine_e2e.py \
       tests/integration/test_row_errors_e2e.py -q

# docs (treat warnings as errors); use the .[docs]-only install to avoid the
# S1 local gotcha (dev+geo extras trip extra toctree warnings CI does not see)
pip install -e ".[docs]"
sphinx-build -b html -W --keep-going docs docs/_build/html

# no-extras environment run (import guards must hold without optional deps)
# fresh venv: pip install -e .   (no extras), then pytest with importorskip guards intact
```

Known pre-existing red (NOT yours; note in the ledger, do not fix): `test_v2_cloud_sources.py`
moto/`pydantic_settings` failure (S1 carry-forward, reaches into the platform sibling). Local sphinx dev+geo
toctree warnings (12) if you install extras beyond `.[docs]`; use the `.[docs]`-only recipe above.

---

## 11. Files changed / exports (summary for the barry docs step)

Code edits (no new public exports; no version bump -- additive, pre-GA):
- `execution/_strategies/_orphan.py` -- `resolve_fk_keys` gains `errored_parent_keys` kwarg + returns a
  3-tuple (adds cascade list); precedence map-then-errored-then-orphan.
- `execution/_pandas_adapter.py` -- `_parent_map` excludes row-errored parent-key rows + fills
  `errored_keys_cache`; `_resolve_fk_node` gathers errored keys, unpacks the 3-tuple, emits cascade
  `RowError`s; `_dispatch_mask_node` + `run()` thread `key_error_rows`/`errored_keys_cache`.
- `execution/_sequential.py` -- builds `key_error_rows` per table, threads both caches through the
  parent-map pre-build and per-node dispatch, releases `errored_keys_cache` with `parent_map_cache`.
- `execution/_pipeline.py` -- MEDIUM telemetry honesty (`sources_resident`), LOW-1 raise-before-write
  reorder + docstring note, NIT forced-sequential-needs-mask-table guard.
- `quarantine.py` -- unchanged.

Tests: extend `tests/integration/test_fk_sequential_row_error_leak.py` (key-column-error class, both
execution modes, policy sweep, fail-loud); add to `tests/unit/execution/test_pipeline_routing.py`
(telemetry honesty, `_parent_map` no-op); add LOW-1 write-timing test, LOW-2 multi-table single-JSONL
test, NIT forced-sequential test.

Docs (barry): note in CHANGELOG that the FK-child raw-key leak class is closed on both execution paths via
quarantine-aware FK resolution (exclude row-errored parent keys + cascade-quarantine their children);
update CODEMAP for the `_orphan.py` cascade role if it tracks per-module responsibilities.
