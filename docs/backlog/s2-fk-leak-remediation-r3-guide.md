# S2 BLOCKER Remediation Guide (Round 3): exhaustive FK-topology leak closure

**Tech-lead-authored (Opus). Build agent: Sonnet. Implement strictly from this guide.**
Program: "Finish Open-Ended Surfaces". Sprint S2, dennis round-2 BLOCK remediation. Engine track.
Worktree: `/home/cam/vscode/engine-integration`, branch `program/finish-open-ended`, builds on head `10e8ade`.
Repo docstrings/comments are ASCII only (no em-dash, no arrow glyphs like `->`/`<->` in prose): the
`source_hygiene` sentry gate rejects them. Keep every comment/docstring you write in `src/`/`tests/`
ASCII-clean. In this doc, prose says "maps to"; code blocks and matrix tables use the literal arrow.

This is the THIRD guide for one leak class. Two prior guides closed the topologies they considered and
MISSED others, causing repeat BLOCKERs. The mandate this round is EXHAUSTIVE topology coverage so there
is no round 4. Everything below was verified by reading the code at `10e8ade` and running dennis's
round-2 repros plus three new probes (diamond, mutual-cycle, middle-hop). Repro results are inline.

Prior guides (read for context, do NOT re-derive their closed cases):
- `docs/backlog/s2-fk-sequential-wiring-guide.md` (S2 wiring + row-error gap)
- `docs/backlog/s2-fk-leak-remediation-guide.md` (round-2: EXCLUDE-then-CASCADE, closed cross-table /
  composite / grandchild / remap-re-leak / non-unique-key)

---

## 0. What round 2 already closed (verified still green at `10e8ade`) - do NOT touch these

The round-2 EXCLUDE-then-CASCADE fix (shared `key_error_rows` index + `errored_keys_cache`; exclusion in
`_parent_map`, cascade branch in `resolve_fk_keys`, cascade emit in `_resolve_fk_node`) is CORRECT for
every topology where the FK parent-key node and the FK-child node live in DIFFERENT tables. Re-running
dennis's repros at `10e8ade` (venv `source .venv/bin/activate`):

```
repro_leak.py        (single cross-table)        CHILD raw 'notadate' absent : True   CLOSED
bypass_seq.py        (cross-table, sequential)   CHILD raw absent            : True   CLOSED
bypass_composite.py  (composite, partial-col)    full_frame/sequential leak  : False  CLOSED
bypass_grandchild.py (3-hop, top parent errors)  child+grand leak            : False  CLOSED
bypass_nonunique.py  (remap-re-leak + preserve)  ANY RAW LEAK                : False  CLOSED
probe_diamond.py     (2 parents -> 1 child)      full_frame/sequential leak  : False  CLOSED (new probe)
probe_midhop.py      (3-hop, MIDDLE errors)      child+grand leak            : False  CLOSED (new probe)
```

These are byte-parity pins for this round. If any moves, the fix perturbed a closed path: STOP (section 9).

## 0.1 The TWO open findings this guide closes

1. **BLOCKER - self-referential FK** (`employees.id` referenced by `employees.manager_id`, one table is
   its own parent). Verified at `10e8ade`:
   ```
   verify_selffk.py:  [full_frame] ids=[] manager_ids=[]              RAW_LEAK=False
                      [sequential] ids=['2020-01-14'] manager_ids=['notadate']  RAW_LEAK=True   <-- LEAK
   verify_selffk_auto.py: route: sequential pure_mask_fk  manager_ids=['notadate']  RAW_LEAK=True (DEFAULT)
   ```
   Sequential leaks the raw errored key into the child column on the DEFAULT (auto) path. Full-frame is
   leak-free but "over-quarantines" (empties the 2-row table). This guide FIXES sequential and reconciles
   the divergence so BOTH paths are leak-free AND equivalent (section 3 + section 4).

2. **HIGH - when-gated duplicate key** (accepted as a documented limitation per Cam's decision, no
   enforcement code). Verified at `10e8ade`:
   ```
   verify_dupwhen.py: [full_frame/preserve] parent_out=['notadate','2020-01-04'] child_out=['notadate']  CHILD_RAW_LEAK=True
                      [sequential/preserve] parent_out=['notadate','2020-01-04'] child_out=['notadate']  CHILD_RAW_LEAK=True
                      (identical for remap and fail; leaks CONSISTENTLY on BOTH paths and ALL policies)
   ```
   This is NOT a quarantine-escape: the raw value is present in the child ONLY because it is ALSO present
   in the PARENT output (row 1), which the user's `when` gate deliberately left unmasked. Net-new exposure
   is NIL. Accept + document + pin (section 5).

3. **MEDIUM (functional, non-leak) - mutual cross-table cycle** surfaced while enumerating topologies.
   Verified at `10e8ade`:
   ```
   probe_cycle.py: [cycle/full_frame] OK outputs=['A','B']
                   [cycle/sequential] RAISED ExecutionError [relationship_cycle]
                   AUTO (default)     RAISED ExecutionError [relationship_cycle]   <-- REGRESSION
   ```
   A cross-table FK cycle (A references B, B references A) RAN under full-frame before S2; under S2's
   default `auto` routing it now routes to `run_sequential`, whose `table_topo_order` raises on a
   cross-table cycle. This is NOT a leak (both paths are leak-free: full-frame closes via cascade,
   sequential fails closed before masking anything). But it is a functional regression of an FK job under
   the new default. A cheap routing guard closes it (section 6). Flagged for a Cam scope call.

---

## 1. Ground truth (verified in code at `10e8ade` - build on these, do not re-derive)

1. **Full-frame `run()` drains PER NODE** (`_pandas_adapter.py:235-242`): after EACH node dispatch it
   drains `ctx.row_errors`, attributes to `node.table`, and folds each record into `key_error_rows`
   (table -> column -> {row_index: trigger}) immediately. So when a later node dispatches, the earlier
   node's key-errors are already in `key_error_rows`.
2. **Sequential `run_sequential` drains PER TABLE** (`_sequential.py:265-274`): it runs the WHOLE
   `nodes_by_table[table]` loop first (`:241-254`), and only AFTER the loop drains + folds + classifies.
   This is the root of the self-ref leak (1.5).
3. **`_parent_map` excludes row-errored parent-key rows** (`_pandas_adapter.py:511-534`): rows in
   `key_error_rows[parent_table][key_col]` are skipped from the resolution map and recorded (with their
   trigger) into `errored_keys_cache[cache_key]`. It has a CACHE-HIT EARLY RETURN
   (`_pandas_adapter.py:487-490`): if `parent_map_cache[cache_key]` already exists, it returns the cached
   map and does NOT recompute or repopulate `errored_keys_cache`. A stale (pre-error-fold) cache entry is
   never corrected.
4. **`resolve_fk_keys` precedence** (`_orphan.py:81-102`), per child row: (1) `parent_map.get(key)` is not
   None -> use the masked value; (2) else `key in errored_parent_keys` -> CASCADE (masked value = None,
   record `(row_index, trigger)`); (3) else -> genuine orphan, apply `orphan_policy`. The cascade at (2)
   pre-empts the orphan-policy branch entirely, so all four policies are uniformly safe for an errored key
   (section 4). A cascaded key NEVER reaches the orphan branch.
5. **`order_work` orders a self-FK parent-key node BEFORE its FK-child node WITHIN a table**
   (`_runner.py:128-134`): the self-FK edge `parent=(employees,(id,))`, `child=(employees,(manager_id,))`
   adds `deps[(employees,(manager_id,))].add((employees,(id,)))` in the SAME work-node graph, and the
   Kahn topo sort (`_kahn_sorted`) places `id` before `manager_id`. This ordering guarantee is what makes
   the section-3 fix correct; it is empirically confirmed by the fix working (1.6). NO ordering fix is
   needed.
6. **`table_topo_order` skips self-edges** (`_sequential.py:407-408`: `if edge.parent_table ==
   edge.child_table: continue`), so a self-ref table does NOT raise `relationship_cycle`; it is masked as
   a single table, which is exactly why the self-ref leak is reachable on the sequential path. A
   CROSS-table cycle is NOT skipped and DOES raise (finding 3 / section 6).
7. **`compute_quarantine` removes rows by `row_index` and dedups per (table, row_index)**
   (`quarantine.py:150-177`). A self-ref row that is both a failing parent (its own key errored) AND a
   cascaded child (it references its own errored key) produces two records at the same `row_index`; dedup
   removes the row exactly once. No double-count, no crash.
8. **Module sizes at `10e8ade`:** `_pandas_adapter.py` **600 (AT the 600 cap, zero headroom)**,
   `_sequential.py` 432, `_orphan.py` 184, `_pipeline.py` 515, `quarantine.py` 294, `_runner.py` 208.
   Sentry `LIMIT=600` (`tests/sentry/test_module_size.py`); none of these is on the ALLOWLIST. Section 7.

### 1.5 Why sequential leaks self-ref but full-frame does not (the mechanism)

Self-ref table `employees` with `id` (date_shift, parent key) and `manager_id` (FK child of `id`):

- Full-frame `run()`: dispatch `id` -> drain + fold `key_error_rows[employees][id][0]=format_error`
  (`:235-242`) -> dispatch `manager_id` -> `_parent_map` sees the fold, EXCLUDES row 0, cascades the child
  row referencing it. Both the failing parent row (0) and the cascaded child row (1) become row-errors on
  `employees` -> D8 quarantines both -> table empties. Leak-free.
- Sequential `run_sequential`: the `nodes_by_table[employees]` loop dispatches `id` THEN `manager_id`
  (`:241-254`) with NO drain between them, because the drain is after the loop (`:265`). So `manager_id`'s
  `_parent_map` runs with an EMPTY `key_error_rows` -> row 0 is NOT excluded -> `parent_map["notadate"] =
  "notadate"` (the errored key kept raw) -> `manager_id` row 1 resolves via precedence-1 to the RAW key.
  The post-loop parent-map pre-build (`:289-298`) cannot correct it: `parent_map_cache[(employees,(id,))]`
  is already populated from the in-loop `manager_id` dispatch, so `_parent_map` hits the cache-hit early
  return (`:487-490`) and the stale (leaky) map stands. LEAK.

### 1.6 The fix, verified before writing this guide

Applying the section-3 change (move the drain + fold INTO the node loop, per node) and re-running:

```
verify_selffk.py:      [full_frame] ids=[] manager_ids=[]  RAW_LEAK=False
                       [sequential] ids=[] manager_ids=[]  RAW_LEAK=False   <-- FIXED + EQUIVALENT
verify_selffk_auto.py: route: sequential  manager_ids=[]   RAW_LEAK=False   <-- DEFAULT PATH FIXED
verify_dupwhen.py:     unchanged (still the accepted when-gate limitation, both paths, all policies)
repro_leak / bypass_seq / bypass_composite / bypass_grandchild / bypass_nonunique / probe_diamond /
probe_midhop:          all still leak-free (no regression)
Focused suite (71 tests) + tests/unit/execution + tests/integration (618 passed, 1 skipped): GREEN.
```

The fix is a STRICT NO-OP for every non-self-ref topology: only self-ref has an FK child dispatching
inside the same table's node loop as its parent key, so only self-ref is affected by folding per-node
instead of per-table-batch. This is the minimal-blast-radius fix.

---

## 2. The self-ref correct-behavior decision (Cam decision 1) - decided and justified

**Question:** what is correct for a self-ref row whose own key errored (it is both the failing parent AND,
if it references itself or is referenced, a child)?

**Decision: cascade-quarantine the referencing child row, exactly as for cross-table.** A self-ref table
is not special: the table being its own parent does not change FK semantics. The rule is the same one the
whole design rests on: a child of a row-errored parent key is cascade-quarantined (masked value None,
synthetic RowError with the parent's trigger) so the raw key can never reach committed child output, under
ANY orphan_policy. Consequences, all correct:

- The failing-parent row (its key errored, covered trigger) is quarantined as a data-quality error.
- Any row referencing that errored key via the self-FK is cascade-quarantined (masked None, then removed).
- In the degenerate case where every surviving row references the errored row (the repro: 2 rows, row 1
  references row 0), the table empties. That is correct: both the failing parent and its only referrer are
  removed together, keeping parent and child dispositions consistent (the invariant).
- A row that references ITSELF and errors (id="x" errors, manager_id="x") gets two records at the same
  row_index; `compute_quarantine` dedups; the row is removed once. No leak, no crash.

**Why not the alternative** (keep the referencing row but null/orphan its self-FK): that would (a) require
treating self-ref differently from cross-table (a special case in `resolve_fk_keys`), and (b) leave a row
with a dangling FK pointing at a now-removed parent - an inconsistent published row. Cascade-quarantine
keeps the two paths equivalent and reuses the entire honesty-pack pipeline with zero new semantics.

**This is exactly the behavior full-frame already produces** (empties the table). So reconciling the
MEDIUM path-divergence (Cam decision 1, tail) means making SEQUENTIAL match FULL-FRAME. Full-frame is the
reference; it is not "over-quarantining" incorrectly - emptying is the correct cascade outcome for a
2-row table where one row fails and the other references it. After the section-3 fix both paths empty the
table (verified 1.6): leak-free AND row-equivalent. The section-8 equivalence test pins this.

---

## 3. The fix: per-node incremental key-error fold in `run_sequential` (Cam decision 1)

One change, in `_sequential.py`, inside the `try` block's per-table loop. Move the drain + fold from AFTER
the node loop (`:265-274`) to INSIDE it, per node, so an intra-table FK-child node's `_parent_map` sees
the parent-key node's errors before it resolves (mirroring full-frame `run()` at `_pandas_adapter.py:235-242`).
Keep the fail-loud CLASSIFICATION per-table (after the loop, on the accumulated records) so timing
(raise BEFORE parent-map pre-build / write / eviction) is unchanged.

### 3.1 Exact edit (`_sequential.py`)

Replace the node loop + post-loop drain/fold block (currently `:241-274`) with:

```python
                table_records_list: list[RowErrorRecord] = []
                for node in nodes_by_table.get(table, ()):
                    warnings.extend(
                        adapter._dispatch_mask_node(
                            node,
                            frames,
                            graph,
                            source_snapshots,
                            parent_map_cache,
                            node_by_key,
                            ctx,
                            key_error_rows=key_error_rows,
                            errored_keys_cache=errored_keys_cache,
                        )
                    )
                    # S2 self-ref FK: drain + fold per node, BEFORE the next node
                    # in this table dispatches, so an intra-table FK-child node
                    # (a self-referential FK) sees the parent-key node's errors
                    # when it builds the parent map. Mirrors full-frame run()
                    # (_pandas_adapter.py per-node drain). For every non-self-ref
                    # topology no FK child dispatches inside a parent table's node
                    # loop, so this is byte-identical to the prior per-table drain.
                    batch = drain_row_errors(ctx.row_errors, table=table)
                    table_records_list.extend(batch)
                    for rec in batch:
                        key_error_rows.setdefault(rec.table, {}).setdefault(rec.column, {})[
                            rec.row_index
                        ] = rec.trigger

                # Per-table fail-loud classification stays here (after the loop),
                # so a table raises on any uncovered record BEFORE its parent map
                # is pre-built, BEFORE it is written to the sink, and BEFORE its
                # frame is evicted (timing unchanged from the prior code).
                table_records = tuple(table_records_list)
                all_row_errors.extend(table_records)
                if table_records:
```

The existing `if table_records:` block (uncovered classification + `raise RowErrorsFailedError`), the
parent-map pre-build (`:289-298`), the Arrow convert, the covered-quarantine filter (`:304-311`), the
write, and the eviction all stay EXACTLY as they are. `table_records` is now a tuple built from the
accumulated list; `compute_quarantine(..., row_errors=table_records)` already accepts a tuple.

### 3.2 Why the post-loop parent-map pre-build now yields the correct map

For self-ref, `manager_id`'s in-loop dispatch calls `_parent_map` for edge `(employees,(id,))` AFTER `id`
has drained (per-node), so the cached map already EXCLUDES row 0 and `errored_keys_cache` is populated.
The post-loop pre-build hits the cache-hit early return and returns that correct map. For pure cross-table
parents (no intra-table FK child), `_parent_map` is NOT called during the loop; the post-loop pre-build
builds it fresh with the fully-folded `key_error_rows`, exactly as today. Both correct.

### 3.3 Node ordering guarantee (Cam decision 1 requires stating this)

`order_work` GUARANTEES the parent-key node precedes the FK-child node within a self-ref table, via the
self-FK edge that adds `deps[child_node].add(parent_node)` in the same work-node graph (ground truth 5;
`_runner.py:128-134`). No ordering fix is required. This is not assumed: the section-1.6 experiment
empties the table, which is only possible if `id` drained before `manager_id` resolved. If a future change
ever removed that dep, the section-8 self-ref test would fail (leak reopens), catching it.

### 3.4 Record-order note (no code, verified non-issue)

Per-node draining groups `all_row_errors` and the quarantine JSONL by node within a table (node order ==
`order_work` order) instead of by raw emission order. The SET of records is identical; `compute_quarantine`
dedups by (table, row_index); the multi-table JSONL test asserts SET membership, not line order; and the
618-test sweep is green. Do NOT assert JSONL line order across paths (already a documented caveat).

### 3.5 Module docstring update (`_sequential.py`)

The module docstring (`:37-54`) says the drain happens "immediately after that table's mask-node loop".
Update it to say the drain + `key_error_rows` fold happen PER NODE inside the loop (for the self-ref case),
while the fail-loud classification + quarantine filter stay per-table after the loop. Keep it ASCII-clean.

---

## 4. Orphan-policy composition (Cam mandate: sweep warn/fail/preserve/remap x each topology)

The cascade branch (`resolve_fk_keys` precedence 2, `_orphan.py:88-100`) fires for an errored parent key
BEFORE the orphan-policy branch (precedence 3). So for the LEAK case (child references a row-errored
parent key), the policy is IMMATERIAL: all four policies cascade-quarantine identically. `orphan_policy`
governs ONLY genuine orphans (keys absent from every parent), which this fix does not touch.

| orphan_policy | child of a ROW-ERRORED parent key | genuine orphan (unchanged) | raw key can leak? |
|---|---|---|---|
| PRESERVE | cascade-quarantine (covered) or fail-loud (uncovered) | keep source key | No |
| WARN     | cascade-quarantine (code-identical to PRESERVE + 1 warning) | preserve + aggregated warning | No |
| REMAP    | cascade-quarantine, NOT remapped (never re-runs the failing strategy on the bad value) | remap via parent strategy | No |
| FAIL     | cascade-quarantine (covered) or fail-loud `RowErrorsFailedError` (uncovered) | raise `orphan_fk_violation` | No |

**FAIL semantic note (kept from round 2, do NOT change without a product call):** under FAIL, a child of a
quarantine-COVERED parent key error is quarantined (data-quality disposition), not raised as
`orphan_fk_violation` (RI disposition). The existing `# NOTE` at `_orphan.py:89-97` documents this. It is
leak-free and internally consistent. STOP + escalate only if a test encodes the opposite FAIL expectation
(section 9).

Because policy is immaterial to the cascade, the tests (section 8) sweep policies on a REPRESENTATIVE
subset rather than the full cartesian product (which would be dozens of near-identical tests). The reduced
sweep is justified by the single `resolve_fk_keys` precedence chain: prove the errored-key case cascades
regardless of policy once (self-ref, cross-table already pinned), and prove genuine orphans are untouched
(existing `test_genuine_orphan_untouched_by_the_fix`).

---

## 5. The accepted when-gate limitation (Cam decision 2) - wording, correction, NOTE, pin

### 5.1 The mechanism (verified, `verify_dupwhen.py`)

Parent `id = ["notadate","notadate","2020-01-01"]`, `keep = [1,0,1]`, `id` masked by `date_shift` gated by
`when: keep == 1`:

- Row 0 (keep=1): date_shift runs, "notadate" is uncoercible -> `format_error`, kept raw. Quarantined
  (covered), removed from parent output.
- Row 1 (keep=0): the `when` gate SKIPS date_shift. `id` stays raw "notadate". No row-error emitted. Survives.
- Row 2 (keep=1): "2020-01-01" masks to "2020-01-04".
- Parent output = `["notadate","2020-01-04"]`. The raw "notadate" is PRESENT in the parent output (row 1),
  put there by the user's own `when` gate.
- `_parent_map`: row 0 excluded (errored); row 1 maps identity `"notadate" -> "notadate"` (a when-gate
  unmasked key maps identity by the RI contract); row 2 maps `"2020-01-01" -> "2020-01-04"`.
- Child `parent_id = "notadate"` resolves via precedence-1 (map HIT on the row-1 identity entry) to
  "notadate". This is NOT a cascade (precedence-2 only fires when the key is ONLY in the errored set;
  here a surviving clean row supplies it - the same precedence-1-before-2 rule that correctly handles the
  non-unique-key case in round 2).

This leaks CONSISTENTLY on both paths and all policies (verified), because it is a property of the
identity-map + when-gate semantics, NOT of the sequential path or the orphan policy.

### 5.2 Why accept (nil net exposure) - the exact documented-limitation wording

Put this wording in (a) the CHANGELOG limitation note, (b) `docs/relationships-memory-scaling.md` (or the
FK/quarantine doc), and (c) as an inline `# NOTE` in `resolve_fk_keys` at the precedence-1 branch:

> **Accepted limitation (when-gated duplicate parent key).** When a `when` gate leaves a parent FK-key row
> unmasked AND that same raw key value ALSO appears on a different parent row that row-errored, a child
> referencing that key value resolves (via the identity-map contract, precedence-1) to the raw value
> carried by the when-gate-unmasked parent row. This is NOT a quarantine escape: the raw value is present
> in the child ONLY because the user's `when` gate deliberately left that duplicate parent row unmasked,
> so it is ALREADY present in the parent output. Net-new exposure is NIL. Enforcing "cascade even on a
> when-gated duplicate" would BREAK referential integrity: the child would point to null/quarantine while
> the parent row survives with the raw key, producing a dangling reference for a row the user intentionally
> chose to leave unmasked. The identity-map contract (an unmasked parent key maps to itself, and children
> mirror it) is the correct behavior; this case is documented, not enforced.

### 5.3 Correct the FALSE claim in the prior guide (Cam decision 2a)

`docs/backlog/s2-fk-leak-remediation-guide.md` section 2, the precedence paragraph, claims: "Precedence
1-before-2 also correctly handles the degenerate non-unique-parent-key case ... if a clean parent row
supplies the key, the child resolves to the real masked value". This is FALSE for the when-gate case: when
the "clean" surviving row is clean because it was when-GATED (unmasked, kept RAW), precedence-1 resolves
the child to the RAW value, not a "real masked value". Append a correction to that paragraph:

> CORRECTION (round 3): precedence-1 yields a "real masked value" only when the surviving parent row was
> actually masked. When the surviving duplicate row is unmasked because a `when` gate skipped it,
> precedence-1 yields the RAW (when-gate-unmasked) value. See the accepted when-gate limitation in
> `s2-fk-leak-remediation-r3-guide.md` section 5: this is nil-net-exposure (the raw value is already in
> the parent output) and is accepted + documented + pinned, not enforced.

Do the same one-line correction to the `_orphan.py` module docstring's precedence-1 description
(`:36` area) and the `resolve_fk_keys` docstring precedence-1 line, so the code's own docs stop implying
precedence-1 is always a masked value.

### 5.4 Pin it (Cam decision 2c) - a test + an inline NOTE

Add `TestWhenGatedDuplicateKeyAcceptedLimitation` (section 8.4) that PINS the accepted behavior: for the
`verify_dupwhen.py` shape, assert the child DOES inherit the raw when-gated value AND assert that same raw
value is present in the PARENT output (proving nil net exposure). Parametrize both `execution_mode`s. This
makes the behavior intentional: a future change that "fixes" it fails the pin loudly and forces a
conscious decision. Add the inline `# NOTE` from 5.2 at the precedence-1 branch in `resolve_fk_keys`.

---

## 6. MEDIUM (functional, non-leak): mutual cross-table cycle routing guard

`_sequential_eligible` returns True for a relationship-bearing pure-mask job even when the table graph has
a cross-table cycle, so `auto` routes it to `run_sequential`, whose `table_topo_order` raises
`relationship_cycle` (verified: `probe_cycle.py`, AUTO raises). Full-frame runs the same job (its
work-node graph is a DAG even when the table graph cycles, because FK parent-KEY columns depend on
nothing). This is a functional regression of an FK job under S2's new default. It is LEAK-FREE either way
(full-frame closes via cascade; sequential fails closed before masking), so it is not a leak-mandate cell,
but it must not ship as a silent regression.

**Fix (required, small):** make the router fall back to full-frame for a cyclic table graph on `auto`, and
raise a CLEAR error (not the cryptic cycle error) on explicit `sequential`. Add a pure helper in
`_pipeline.py` and consult it in the route decision (`:282-303`):

```python
def _has_cross_table_fk_cycle(graph: RelationshipGraph) -> bool:
    """True if the table-level FK graph has a cycle across DISTINCT tables.
    Sequential masking orders whole tables (table_topo_order), so a cross-table
    cycle cannot be sequenced; self-edges (self-ref FK) mask within one table
    and are not a table-level cycle."""
    from collections import defaultdict

    succ: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        if edge.parent_table != edge.child_table:
            succ[edge.parent_table].add(edge.child_table)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}

    def visit(node: str) -> bool:
        color[node] = GRAY
        for nxt in succ.get(node, ()):  # DFS back-edge = cycle
            c = color.get(nxt, WHITE)
            if c == GRAY or (c == WHITE and visit(nxt)):
                return True
        color[node] = BLACK
        return False

    return any(color.get(n, WHITE) == WHITE and visit(n) for n in list(succ))
```

In the route decision, after `eligible, route_reason` are computed:

```python
    cyclic = _has_cross_table_fk_cycle(graph)
    if execution_mode == "full_frame":
        route, route_reason = "full_frame", "override_full_frame"
    elif execution_mode == "sequential":
        if not eligible:
            raise ConfigError(...)  # unchanged
        if cyclic:
            raise ConfigError(
                "execution_mode='sequential' requested but the FK graph has a "
                "cross-table cycle, which the sequential path cannot order; use "
                "execution_mode='full_frame' or 'auto'."
            )
        if not has_mask_table:
            raise ConfigError(...)  # unchanged
        route = "sequential"
    else:  # "auto"
        if eligible and cyclic:
            route, route_reason = "full_frame", "cross_table_cycle"
        else:
            route = "sequential" if eligible else "full_frame"
```

`_execution_telemetry` needs no change (it reports `route_reason` verbatim). Test in section 8.5. This
adds ~20 lines to `_pipeline.py` (515 -> ~535, under 600).

**STOP + escalate to Cam (scope question):** whether a cross-table FK cycle is a SUPPORTED topology at all.
If product decides cross-table FK cycles should be rejected at config/profile validation (they currently
are not - `build_relationship_graph` accepts them and full-frame runs them), this guard becomes moot and
the rejection belongs upstream. Ship the guard now (it makes `auto` non-regressing and gives explicit
`sequential` a clear error) and flag the upstream-rejection question for Cam in the PR. Do NOT invent an
upstream rejection on your own.

---

## 7. THE EXHAUSTIVE FK-TOPOLOGY MATRIX (every cell: closed / accepted / fixed, with code reason)

Legend: **CLOSED** = raw errored key provably cannot reach committed child output (no work). **FIXED** =
was a leak, closed by section 3. **ACCEPTED** = the documented when-gate limitation (section 5).
**FAILS-CLOSED** = path rejects the job before masking (no leak, no output). Each row states the code
reason and the evidence repro. Orphan-policy column: "uniform" means all four policies behave identically
for the errored-key case because the cascade (precedence-2) pre-empts the policy branch (section 4).

| # | Topology | full_frame | sequential | orphan_policy sweep | code reason | evidence |
|---|----------|-----------|-----------|--------------------|-------------|----------|
| 1 | Single cross-table (parent.id -> child.fk) | CLOSED | CLOSED | uniform (cascade) | `_parent_map` excludes errored parent-key row; `resolve_fk_keys` precedence-2 cascades child; run() per-node drain + run_sequential per-table drain both fold before child dispatch | `repro_leak.py`, `bypass_seq.py`, `bypass_nonunique.py` |
| 2 | Self-referential (1-table cycle; employees.id <- employees.manager_id) | CLOSED (empties, correct) | **FIXED** (was LEAK) | uniform (cascade) | full_frame drains per-node so child sees parent-key error; sequential drained per-TABLE (child resolved before fold) -> section-3 per-node fold closes it; `order_work` orders id before manager_id | `verify_selffk.py`, `verify_selffk_auto.py` |
| 3 | Composite / multi-column FK, partial-column error ((region,id), only id errors) | CLOSED | CLOSED | uniform (cascade) | `_parent_map` `excluded.setdefault(ridx, trig)` excludes the WHOLE row when ANY key column errored (first key-col error wins the trigger); child key-tuple cascades | `bypass_composite.py` |
| 4 | Multi-hop / grandchild, TOP parent errors (p -> c -> g) | CLOSED | CLOSED | uniform (cascade) | top error cascades to c; c's cascaded rows quarantined out of c output; g resolves against c's post-error map (errored c keys already excluded) | `bypass_grandchild.py` |
| 4b | Multi-hop, MIDDLE table key errors (p -> c(err) -> g) | CLOSED | CLOSED | uniform (cascade) | c.id error excludes it from c's outgoing map; g child referencing it cascades; c's own row quarantined | `probe_midhop.py` (new) |
| 5 | Diamond (pA, pB -> one child c, pA.id errors) | CLOSED | CLOSED | uniform (cascade) | multi-parent merge (`_resolve_fk_node` `edges[1:]` first-hit-wins) + `gather_errored_parent_keys` unions errored keys across edges; child row referencing pA's errored key cascades | `probe_diamond.py` (new) |
| 6 | Mutual cross-table cycle (A -> B -> A) | CLOSED (cascade; work-node DAG) | FAILS-CLOSED (`table_topo_order` raises) | n/a (no masking on sequential) | full_frame closes via cascade; sequential cannot order a cross-table cycle. NOT a leak. Section-6 guard makes `auto` fall back to full_frame (was a regression) | `probe_cycle.py` (new) |
| 7 | When-gated duplicate key (dup raw value on a when-gate-unmasked row AND an errored row) | ACCEPTED | ACCEPTED | independent (precedence-1 map hit, not a cascade) | the surviving when-gate-unmasked parent row supplies an identity map entry; precedence-1 resolves the child to the raw value already present in the PARENT output; nil net exposure | `verify_dupwhen.py` |

**Every cell beyond the accepted when-gate case (#7) is CLOSED, FIXED here, or FAILS-CLOSED (no leak).**
Only #2 needs code (section 3). #6 needs the non-leak routing guard (section 6). #7 is accepted + pinned
(section 5). There is no un-addressed leak cell.

Orphan-policy note for the matrix: cells 1-5 are "uniform" because the errored-key cascade fires at
precedence-2, before the orphan_policy branch (section 4). Cell 7 is policy-INDEPENDENT for a different
reason: it resolves at precedence-1 (a clean surviving row supplies the key), so the orphan branch is
never reached either. Genuine orphans (any topology) follow orphan_policy unchanged and are pinned by the
existing `test_genuine_orphan_untouched_by_the_fix`.

---

## 8. Tests the build MUST land (exact acceptance criteria)

Extend `tests/integration/test_fk_sequential_row_error_leak.py` (do NOT delete existing classes; they are
byte-parity pins). Add to `tests/unit/execution/test_pipeline_routing.py` for the routing guard. Run the
focused set continuously; the full gate (section 10) before declaring done. Build the self-ref config from
`scratchpad/verify_selffk.py`, the when-gate config from `scratchpad/verify_dupwhen.py`.

### 8.1 SELF-REF leak closure, both modes (THE blocker proof) - `TestSelfRefFkKeyErrorLeakClosure`

Shape: one table `employees`, `id = ["notadate","2020-01-01"]` masked by `date_shift`
(`{"min_days":1,"max_days":30}`), `manager_id = [None,"notadate"]` masked by `faker` (person_email) and
declared as an FK child of `employees.id` (`orphan_policy="preserve"`). Real backing parquet + in-memory
`sources` dict. Quarantine `{enabled:True, output_path, triggers:["format_error"]}`.

- Parametrize `execution_mode` over `["full_frame","sequential"]` (sequential passes a
  `ParquetTransactionalSink(tmp_path/"out")` and reads parquet back; full_frame reads `result.outputs`).
- Assert raw "notadate" is ABSENT from BOTH `id` and `manager_id` in the output. **This FAILS today at
  `10e8ade` for `sequential` (manager_id == ['notadate']) and PASSES after section 3.** Capture red first.
- Assert the table is EMPTY (both self-referencing rows removed: the failing parent + its referrer), so
  the cascade behavior of section 2 is pinned.
- Quarantine JSONL: assert a record for `id == "notadate"` (trigger `format_error`) AND a cascaded record
  for the `manager_id` row (trigger `format_error`, reason mentions the parent-key cascade).

### 8.2 SELF-REF full_frame vs sequential EQUIVALENCE (Cam MEDIUM) - same class

Run the 8.1 config once `execution_mode="full_frame"` (no sink, read outputs) and once
`execution_mode="sequential"` (no sink, in-memory outputs), assert `result.outputs["employees"]` is
byte-equal across the two (both empty, same schema). This asserts the two paths AGREE (the MEDIUM).

### 8.3 SELF-REF orphan-policy sweep - same class

Add a variant that does NOT empty the table: `id = ["notadate","2020-01-01","2020-03-01"]`,
`manager_id = [None, "2020-01-01", "notadate"]` (row 1 references a CLEAN parent key, row 2 references the
errored key). Parametrize `orphan_policy` over `["preserve","warn","fail","remap"]` and `execution_mode`
over both. Assert raw "notadate" absent from `manager_id` for every cell, and that row 1 (references the
clean key) survives with its correctly masked value (proving only the errored-key referrer cascades, not
every child). This proves the section-4 uniform-cascade for self-ref across all policies.

### 8.4 WHEN-GATE accepted-limitation PIN (Cam decision 2c) - `TestWhenGatedDuplicateKeyAcceptedLimitation`

From `verify_dupwhen.py`: parent `id = ["notadate","notadate","2020-01-01"]`, `keep = [1,0,1]`, `id` masked
by `date_shift` with `when: "keep == 1"`, plus `keep` passthrough; child `parent_id = ["notadate"]` faker
FK of `parent.id`; quarantine covers `format_error`. Parametrize `execution_mode` over both.

- Assert the child `parent_id` output DOES contain "notadate" (the accepted behavior - a PIN, not a bug).
- Assert the PARENT `id` output ALSO contains "notadate" (row 1, when-gate-unmasked) - proving NIL net
  exposure: the child value is not new, it mirrors a value already in the parent output.
- Docstring the class with the section-5.2 wording so the intent is unmistakable. If a future change makes
  the child value absent, THIS test fails and forces a conscious decision.

### 8.5 ROUTING GUARD for cross-table cycle (section 6) - in `test_pipeline_routing.py`

- Build the `probe_cycle.py` config (A -> B, B -> A, faker keys). Assert `execution_mode="auto"` runs and
  `result.quality_metrics["execution"]["route_reason"] == "cross_table_cycle"` and `execution_mode ==
  "full_frame"` (fell back, no raise). Today at `10e8ade` this RAISES `relationship_cycle`.
- Assert `execution_mode="sequential"` on the same config raises `ConfigError` with the clear cross-table
  cycle message (not the raw `relationship_cycle` ExecutionError).
- Assert a self-ref config is UNAFFECTED by the guard: `_has_cross_table_fk_cycle` returns False for a
  self-edge, so a self-ref pure-mask job still routes `sequential` under `auto` (add a unit assertion on
  the helper: self-edge -> False, A/B mutual edges -> True).

### 8.6 No-regression pins (must stay green unchanged)

The section-0 repros, the existing `TestFkKeyColumnErrorLeakClosure` (cross-table key error, policy
sweep), `TestSequentialMultiTableQuarantineJsonlNotClobbered`, `test_sequential_eviction.py`,
`test_transactional_sink.py`, `test_when_gate_row_error_leak.py`, `test_quarantine_e2e.py`,
`test_row_errors_e2e.py`, `test_run_pipeline.py`. If ANY moves, the section-3 fold or the section-6 guard
perturbed a closed path: STOP (section 9).

---

## 9. Module-size (600 LOC ratchet) - pre-emptive extraction (Cam LOW)

The section-3 fix is `_sequential.py`-only (432 -> ~440 after the docstring update; well under 600) and
adds NO net LOC to the at-cap `_pandas_adapter.py`. The section-5 when-gate NOTE lands in `_orphan.py`
(184, ample headroom), NOT in the adapter. So the fix as specified does not itself breach the 600 cap.

HOWEVER, `_pandas_adapter.py` sits at EXACTLY 600 with zero headroom, which blocks the very next change to
FK resolution and is the LOW dennis flagged. Per the mandate, regain headroom NOW with a pure extraction
(no ALLOWLIST edits, all files <= 600):

- **Extract `_make_remap_fn` from `_pandas_adapter.py` into `_orphan.py`** as a free function
  `make_remap_fn(edge, node_by_key, ctx, handlers)`. It is a self-contained closure factory
  (`_pandas_adapter.py:537-574`, ~38 LOC) that uses no instance state except `self._handlers` (pass it as
  a param). `_orphan.py` already owns REMAP semantics (its docstring documents REMAP), so this is its
  natural home. Update the single call site in `_resolve_fk_node` (`:443`) from `self._make_remap_fn(edge,
  node_by_key, ctx)` to `make_remap_fn(edge, node_by_key, ctx, self._handlers)` and add the import.
  Pinned byte-identical by the existing REMAP tests (`test_fk_sequential_row_error_leak.py` remap sweep +
  any `orphan`/`remap` unit tests).

**Target LOC after this round (all <= 600, none on ALLOWLIST):**

| file | at `10e8ade` | after round 3 | why |
|------|-------------|--------------|-----|
| `_pandas_adapter.py` | 600 | ~563 | remove `_make_remap_fn` (-38), +1 import |
| `_orphan.py` | 184 | ~230 | +`make_remap_fn` (~40), +when-gate NOTE (~6), +precedence doc correction |
| `_sequential.py` | 432 | ~440 | per-node fold restructure (net ~0) + docstring update |
| `_pipeline.py` | 515 | ~535 | `_has_cross_table_fk_cycle` + guard (~20) |
| `quarantine.py` | 294 | 294 | unchanged |

If `_orphan.py` unexpectedly nears 600 (it will not; ~230), the module docstring can move detail into the
FK doc. Do NOT add any file to the ALLOWLIST.

---

## 10. STOP and escalate to Cam (do not guess) if:

1. **A test encodes that `orphan_policy=FAIL` must HARD-FAIL (not quarantine) a child of a covered-parent
   key error.** Section 4 keeps the round-2 uniform cascade; do not flip it on your own.
2. **Any byte-parity pin in section 0 or 8.6 moves.** The section-3 fold must be a no-op on every
   non-self-ref path; a moved pin means it perturbed a closed path. Hand back the diverging table/columns;
   do not "adjust the assertion."
3. **The self-ref equivalence test (8.2) does not go byte-equal across paths** after the fold. That means
   the parent-key-before-child ordering (3.3) or the cache lockstep (3.2) was perturbed. Hand back the diff.
4. **Product decides cross-table FK cycles are unsupported and must be rejected upstream** (section 6
   scope flag). Ship the routing guard, flag the upstream-rejection question in the PR; do not invent an
   upstream rejection.
5. **The same fix fails 2-3 times** (self-ref leak persists, or byte-parity keeps drifting). The mental
   model is wrong; hand back what you tried and observed.

Escalation = append the blocker to the program ledger `~/.claude/plans/decoy-finish-open-ended-program.md`
and stop the loop; that is a successful autonomous outcome.

---

## 11. CI-gate mirror - every command must pass before this counts as done

Run from the worktree root with the repo venv (`source .venv/bin/activate`). Mirrors
`.github/workflows/ci.yml` + `docs.yml` and the `decoy-ci-environment-gotchas` note. The sibling engine dir
is `forge-engine` locally.

```bash
# 0. FIRST confirm the self-ref blocker is RED at 10e8ade (before your fix), then implement:
python scratchpad/verify_selffk.py          # expect: [sequential] ... RAW_LEAK=True
python scratchpad/verify_selffk_auto.py     # expect: RAW_LEAK(default/auto): True

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

# AFTER the fix, confirm every topology probe is leak-free / correct:
for p in verify_selffk verify_selffk_auto verify_dupwhen repro_leak bypass_seq \
         bypass_composite bypass_grandchild bypass_nonunique probe_diamond \
         probe_midhop probe_cycle; do echo "== $p =="; python scratchpad/$p.py; done

# docs (treat warnings as errors); use the .[docs]-only install (S1 gotcha: dev+geo
# extras trip extra toctree warnings CI does not see)
pip install -e ".[docs]"
sphinx-build -b html -W --keep-going docs docs/_build/html

# no-extras environment run (import guards must hold without optional deps):
# fresh venv: pip install -e .   (no extras), then pytest with importorskip guards intact
```

Known pre-existing red (NOT yours; note in the ledger, do not fix): `test_v2_cloud_sources.py`
moto/`pydantic_settings` failure (S1 carry-forward, reaches into the platform sibling). The round-2 build
also could not build a no-extras env locally (no pip in `.venv`); verify no-extras + `.[docs]`-only sphinx
at PR/CI time.

---

## 12. Files changed / exports (summary for the barry docs step)

No new public exports; no version bump (additive, pre-GA). Do not touch `release.py` / `is_pre_ga`.

- `execution/_sequential.py` - per-node drain + `key_error_rows` fold inside the table loop (self-ref
  fix); keep per-table classification; update module docstring.
- `execution/_strategies/_orphan.py` - receive extracted `make_remap_fn`; add the when-gate accepted-
  limitation `# NOTE` at the `resolve_fk_keys` precedence-1 branch; correct the precedence-1 docstring line.
- `execution/_pandas_adapter.py` - remove `_make_remap_fn` (moved to `_orphan.py`), update its call site;
  regains headroom to ~563.
- `execution/_pipeline.py` - `_has_cross_table_fk_cycle` helper + route-decision guard (auto falls back to
  full_frame with `route_reason="cross_table_cycle"`; explicit sequential raises a clear `ConfigError`).
- `quarantine.py` - unchanged.
- Docs: correct the false precedence-1 claim in `s2-fk-leak-remediation-guide.md`; CHANGELOG limitation
  note (section 5.2 wording); note the self-ref leak-class closure on both paths.

Tests: extend `tests/integration/test_fk_sequential_row_error_leak.py` (self-ref closure both modes,
self-ref equivalence, self-ref policy sweep, when-gate accepted-limitation pin); add cross-table-cycle
routing-guard tests + `_has_cross_table_fk_cycle` unit to `tests/unit/execution/test_pipeline_routing.py`.
