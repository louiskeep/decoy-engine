# Phase D / D2-cycle: cross-table FK cycles native on the bounded (SEQ) route

Status: plan (rev 3, pre-gate). rev 1 NO-GO (4 HIGH/1 MED, wrong re-derivation
premise); rev 2 NO-GO (2 HIGH; the gate VALIDATED the effective-map core and confirmed
H1/H2/H4/M5 resolved) with two contract gaps. rev 3 folds both and takes the
simplification the gate surfaced: since rev 2 INHERITS from the map (never re-derives),
the value-keyed four-strategy allowlist is unnecessary - inheritance works for any key
strategy; admission is gated on the WORK-NODE dependency graph being acyclic, no table
transforms on cyclic tables, and a combined-memory budget. Branches off main; rebases
onto post-D2b main before build. Cam 2026-10-12: keep in v1 and ATTEMPT it; defer to V2
if it gets too hard.

Rules consulted: 00-universal.md, risk-and-exceptions.md, development-loop.md,
architecture.md, reuse-first.md, testing.md, documentation.md,
autonomous-operation.md. Engine CLAUDE.md, compatibility-contract.md.

Risk: R2. Adds masking for a shape the bounded routes decline today, with byte-parity
+ RI promises. Plan + independent review + Codex plan gate before build.

## FRAME

**Problem.** A cross-TABLE FK cycle (A->B->A) masks TODAY only on full-frame (all
tables resident, no table-level order needed); the bounded routes decline. A LARGE
cyclic job (too big for a full-frame box) has nowhere to run and stays on the old
path. Move it to the bounded SEQ route.

**The corrected key insight (rev-1 was wrong here).** The TABLE graph cycles, but the
COLUMN graph is ACYCLIC (that is exactly why full-frame succeeds: it masks columns in
column-topological order, which exists). So the cycle is maskable WITHOUT a
table-level order by working in COLUMN-dependency order. Crucially, a child FK key
does NOT re-derive from its own source value; it INHERITS the parent key column's
EFFECTIVE OUTPUT (the actual masked value, which may be a token, or RAW if the parent
is `when:`-gated off, or a preserved-raw orphan, or absent if the parent row errored,
with duplicate source keys resolving last-wins). Re-derivation reproduces none of
those and also breaks on FK-key canonicalization (FK lookup treats `True`==`1`, the
hash kernel does not). The fix is to carry the effective MAP, not to re-hash.

**Definition of done.** On the SEQ route, an admitted cross-table cycle masks
byte-identically to the full-frame oracle (strict D0 dimensions), with RI and
orphan/cascade/policy/duplicate semantics identical to full-frame, while keeping only
the narrow key-column state of the cycle resident across tables (not every wide
frame). Non-admitted cycles and all OOC cyclic jobs keep today's decline with a clear
reason.

**Boundaries / v1 narrower-first cut (admission gate).** SEQ only. Admit a cyclic
component only when ALL of:
- its WORK-NODE dependency graph is acyclic (`order_work`, `_runner.py:233/319`, which
  already orders parents before children and rejects unresolved cycles). Table
  cyclicity does NOT imply column/work acyclicity - decline if the work graph cycles.
- NO cyclic table carries a table-level TRANSFORM (filter / sort / dedup / derived
  column, `_transforms_table.py`). Config-only detection (rev-2 HIGH-1): a transform
  can drop/reorder/dedup rows based on PAYLOAD columns, which a narrow key-only pass
  would not reproduce, so a transform-bearing cyclic table declines in this first cut
  (a projected post-transform contract is the later, bigger alternative).
- the COMBINED memory estimate fits (see the memory contract); else decline.
Key strategy is NOT restricted (inheritance copies the parent's effective output for
ANY strategy; native-vs-Python stays the usual per-column type decision). Also decline
`when:`-conditional KEY columns in a cycle (their effective output is predicate
-dependent) and OOC cyclic jobs. Self-reference is not a table cycle (handled by D2b
drain-fold) - out of scope; full-frame unchanged.

## Established solutions

- Column-topological masking of a table-cyclic FK graph is what full-frame already
  does; D2-cycle does the same ordering but retains only the narrow key columns across
  the component instead of all wide frames. No fixed-point iteration (the column graph
  is acyclic, so one pass in column order suffices).
- `_fk_resolve.resolve_fk_keys` is reused UNCHANGED, but must be supplied correct
  effective maps + error caches (not raw source-membership sets) - the gate confirmed
  it is reusable "once supplied correct maps". `_parent_map` is the reference for what
  an effective map contains (last-wins duplicates, error exclusion).
- The D1a hash kernel + the redact/truncate/passthrough handlers mask the parent key
  columns natively (that is the native speedup); the child inheritance is a map lookup
  (no kernel). D2b's kernel seam is reused for the native key masking; D2-cycle adds
  NO matched-key re-derivation (rev-1's mistake).
- Native vs Python for a parent key column is the usual per-column TYPE decision, via
  `decline_reason(column, input_type)` (`_sequential_native.py:174`) - the same type
  predicate D2a/D2b use - NOT a strategy-name allowlist (rev-2 M5). Inheritance is
  strategy-agnostic (it copies the parent's effective output for any strategy); the type
  predicate only chooses whether the parent KEY masking runs on the kernel or in Python.
  A column that declines native still masks correctly in Python and still feeds a
  correct effective map; native is a speedup, never a correctness gate.

## Design (column-order effective-map, SEQ)

When routing detects a cyclic FK component (needs a real detector, see Routing):

1. **Pass 1 - effective key maps in COLUMN-dependency order (narrow).** Order the FK
   KEY columns of the cyclic component by the acyclic COLUMN graph. For each parent
   key column in that order, read ONLY that column's narrow source data, mask it with
   its strategy (native where admitted), and record the EFFECTIVE source->masked map:
   the actual emitted value per source key (token / raw-if-when-off /
   preserved-raw-orphan), with errored-parent rows excluded into the cascade cache and
   duplicate source keys resolved last-wins - exactly what `_parent_map` produces, but
   on narrow columns. A child key column that depends on an already-computed parent map
   consumes it in order.
2. **Pass 2 - mask each cyclic table.** Load each table, mask its payload columns
   normally (D2a/D2b per-column decisions), and resolve its FK child keys by INHERITING
   from the pass-1 effective maps via `resolve_fk_keys` (map lookup + the unchanged
   EXCLUDE-then-CASCADE-then-policy precedence, REMAP re-running the parent handler as
   today). Evict each table after masking.

Memory contract (rev-2 HIGH-2 fix - peak over the WHOLE component schedule, not just
the largest table). Existing SEQ retains a parent's map until ALL its consumers finish
(`_sequential.py:529`), and the admitted mixed topology includes incoming acyclic
parents feeding the cycle and outgoing children consuming it. So peak at any point =
(every live parent map + its error cache that is still retained) + (the component's
retained narrow key maps/error caches) + (the current table's working set: load +
pandas conversion + output overlap), under lazy inputs + a draining sink. The plan
defines explicit map RELEASE points (drop a map once its last consumer finishes) and an
admission check that rejects when the COMBINED estimate exceeds budget, with an explicit
policy when an estimate is unavailable (decline, do not assume it fits - schema/config
prove eligibility, not consumption). The win is jobs whose AGGREGATE frames exceed RAM
while this combined live state fits; a single oversized cyclic table, or a combined
estimate over budget, declines. Pass 1 needs a PROJECTED key-columns-only read; the
current loader reads the whole file (`_pipeline_sources.py:126`, `_readers.py:270`), so
a real projected read is a PREREQUISITE (build it, or decline until it exists - do not
read the whole frame and select after, which does not satisfy the bound).

Canonicalization (rev-1 H1 fix): pass 1 stores the parent's EFFECTIVE OUTPUT keyed by
the SAME normalized FK key the oracle uses (`_fk_keys.py:75`, which treats `True`==`1`),
and pass 2 INHERITS by that same normalized lookup. So canonicalization is inherited
from the oracle unconditionally - the `True`==`1` divergence cannot arise because there
is no re-derivation and no second normalization path. No per-edge canonicalization
admission test is needed (rev 3 drops rev-2's "value-keyed set" phrasing; it conflated
key normalization with masking strategy).

Routing (rev-1 H4 fix): add a real strongly-connected-component detector over the
table FK graph (the current detector returns only a bool), topologically order the
component graph (acyclic tables in topo order; each cyclic component via the two-pass),
and wire cycle admission through BOTH explicit-SEQ and the automatic bounded-routing
branch (lift the cycle decline for ADMITTED cycles on SEQ only). Preserve OOC's
independent cycle rejection and the self-edge exclusion.

## Pinned guarantee and non-goals

**Guarantee.** An admitted SEQ cross-table cycle masks byte-identically to full-frame
(strict dimensions); RI/orphan/cascade/duplicate/policy match full-frame exactly
(same `resolve_fk_keys` precedence, same effective-map semantics); peak memory is the
combined live state (retained parent maps + error caches + component narrow maps + the
current table's working set), checked against budget at admission. Non-admitted cycles
(`when:`-conditional-key / inherited-key-dependency / oversized-single-table /
transform-bearing-cyclic-table / work-node-cyclic / combined-budget-over) and OOC cyclic
jobs decline with a clear coded reason. Acyclic masking and full-frame unchanged.

**Non-goals.** No re-derivation of matched keys. No fixed-point iteration. No OOC
cyclic support. No projected post-transform contract for transform-bearing cyclic tables
(they decline this cut). No strategy-name admission gate (inheritance is strategy
-agnostic; the type predicate governs only native-vs-Python for the key column). No
change to full-frame, `_fk_resolve` precedence, or acyclic routing.

## Acceptance tests (authored before impl; include the rev-1 counterexamples)

- **Parity:** 2-table and 3-table cycles, SEQ byte-identical to full-frame on strict
  dimensions; RI asserted both directions. Cover a RANGE of key strategies to prove
  inheritance is strategy-agnostic (hash native, redact/truncate/passthrough, and at
  least one non-value-keyed strategy that inheritance still copies correctly) - the
  removed rev-2 allowlist must not reappear as a hidden restriction.
- **The rev-1 counterexamples MUST pass identically to full-frame:** (a) a hash parent
  with `when:` false stays RAW and its matched child inherits that RAW value (admitted
  only if the key is unconditional; if the KEY itself is `when:`-gated it declines -
  test both); (b) the column chain `A.id->B.mid->A.back` where an orphan preserved at
  `B.mid` stays raw and `A.back` inherits the raw value (the column-acyclic table cycle
  supported today) - the effective map carries the preserved-raw value, NOT a re-hash.
- **Canonicalization:** a boolean parent `True` matched by an integer child `1`
  inherits the parent's effective token (map lookup), byte-identical to full-frame;
  numeric normalization + duplicate-normalized-key last-wins covered.
- **Orphan/cascade/policy:** preserve/remap/warn/fail + errored-parent cascade through
  a cyclic edge, resolved from the pass-1 effective maps + error cache, identical to
  full-frame.
- **Memory:** a cyclic job whose AGGREGATE frames exceed a set budget but whose
  COMBINED live state (all retained parent maps + error caches + component narrow maps +
  the current table's working set) fits runs on SEQ within the bound; a single oversized
  cyclic table declines (true memory limit); a mixed topology (incoming acyclic parent +
  cyclic component + outgoing acyclic child) whose COMBINED estimate exceeds budget
  declines even though no single table is oversized (rev-2 HIGH-2); an admitted run
  asserts each parent map is RELEASED after its last consumer (peak stays bounded, not
  monotonically growing).
- **Declines (fail closed, coded, no data in the message):** a `when:`-conditional
  KEY cycle; an inherited-key-dependency cycle the pass can't resolve; a cyclic table
  carrying a table-level TRANSFORM (filter/sort/dedup/derived - rev-2 HIGH-1,
  config-only detection); a component whose WORK-NODE dependency graph cycles (not just
  the table graph); an OOC cyclic job (unchanged). Each decline is detectable at
  config/route time without inspecting row data.
- **Routing:** an incoming acyclic parent + a cyclic component + an outgoing acyclic
  child + a disconnected acyclic table all in one job order and mask correctly;
  self-reference still routes as today (not treated as a table cycle).
- **Witness / native evidence:** the parent key columns' pass-1 masking is proven on
  the kernel (reuse the D2b marker witness); inheritance is a map lookup (no kernel).
- **Acyclic unchanged:** every existing acyclic + full-frame-cycle test passes;
  full-frame still masks small cycles. **Module size** under caps (new
  `execution/_sequential_cycle.py`); **NFC** re-assert; **perf gate** on a sized cyclic
  workload (capability-first: it runs bounded at all; native speed on the key columns).

## Known context for the gate

- Revises the program plan's KEEP-CURRENT/V2 cycle disposition to "v1, SEQ bounded,
  unconditional keys (any strategy), no table transforms on cyclic tables,
  combined-budget admission, column-order effective-map"; land the program-plan update
  with this slice.
- Depends on D2b; rebase onto post-D2b main before build. D2b provides a native kernel
  SEAM only; D2-cycle adds the component detector, the column-order effective-map pass,
  the projected read (or its prerequisite), and the routing wiring.
- "Attempt, defer if too hard" (Cam): if the effective-map-in-column-order pass or the
  projected read proves larger than a bounded slice, narrow further (more declines) or
  defer to V2 and record where it stopped.

## Docs to update

`docs/compatibility-contract.md` (SEQ masks admitted cross-table cycles; the decline
narrows), CODEMAP for the new module, the program-plan cycle disposition.
