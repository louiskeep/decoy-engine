# Phase D / D2-cycle: cross-table FK cycles native on the bounded (SEQ) route

Status: plan (rev 4, pre-gate). rev 1 NO-GO (wrong re-derivation premise); rev 2 NO-GO
(transform semantics + memory); rev 3 NO-GO (2 HIGH: the gate again VALIDATED the
effective-map core, canonicalization, inheritance, and confirmed transforms resolved +
projected reads buildable, but found two NEW holes). rev 4 narrows per the gate's own
root-cause fixes - this is the "narrow further (more declines)" path Cam authorized:
- HIGH-1: inheritance is strategy-agnostic on the CONSUMER (correct), but PRODUCING the
  parent's effective output from a key-only read requires the producer be a PURE
  FUNCTION OF THE KEY VALUE. A `derived` key reading payload siblings, or a positional
  strategy with duplicate keys, cannot be reproduced narrowly. rev 4 admits only
  pure-value-function key producers (the allowlist belongs on the PRODUCER, not the
  consumer); declines derived/sibling/positional/conditional producers.
- HIGH-2: the combined-memory formula omitted `quarantine_entries` (whole masked rows),
  cross-table error caches, and the shared `PoolCache`. rev 4 prices them and declines
  quarantine-producing orphan policies on cyclic tables so retained state stays narrow.
Branches off main; rebases onto post-D2b main before build. Cam 2026-10-12: keep in v1
and ATTEMPT it; narrow further or defer to V2 if too hard. DECISION RULE: if rev 4 also
NO-GOs on a genuinely new deeper hole, defer D2-cycle to V2, record where it stopped,
and continue other Phase D work (do not grind further rounds).

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
  NOTE `order_work`'s edges are FK/composite edges, NOT arbitrary strategy sibling
  reads, so work-acyclicity is NECESSARY but NOT SUFFICIENT (rev-3 HIGH-1) - the
  producer-eligibility condition below is what closes the sibling-read gap.
- every FK KEY column in the component is produced by a PURE VALUE-FUNCTION strategy:
  its emitted value is a function of the key's own source VALUE alone, so a key-only
  projection reproduces it exactly (rev-3 HIGH-1). This ADMITS hash / passthrough /
  redact / truncate / FPE and the like; it DECLINES (config-detectable by strategy type
  + its declared inputs): `derived`/expression strategies that read payload siblings
  (`_derived.py:62` evaluates against full row context, missing from a key-only read),
  any POSITIONAL / row-index-dependent strategy (a last-wins value map loses distinct
  per-row outputs when duplicate source keys map to different positions), and
  `when:`-conditional KEYS (predicate-dependent output). The allowlist is on the
  PRODUCER; the CONSUMER (matched-child inheritance) stays strategy-agnostic - it copies
  whatever effective value the producer emitted.
- NO cyclic table carries a table-level TRANSFORM (filter / sort / dedup / derived
  column, `_transforms_table.py`). Config-only detection over the whole nonempty
  `transforms` declaration (rev-2 HIGH-1, gate-confirmed sound): a transform can
  drop/reorder/dedup rows based on PAYLOAD columns, which a narrow key-only pass would
  not reproduce (a projected post-transform contract is the later, bigger alternative).
- NO cyclic table uses an orphan/error policy that QUARANTINES whole rows (rev-3
  HIGH-2): quarantine entries hold full masked rows incl payload and accumulate across
  tables (`quarantine.py:184`, `_sequential.py:503`), defeating the narrow-state bound.
  Admit orphan policies that stay narrow (preserve / remap / warn); decline fail/
  quarantine on cyclic tables. Config-detectable from the orphan/error policy.
- the COMBINED memory estimate fits (see the memory contract); else decline.
Native-vs-Python stays the usual per-column type decision and is a speedup only (a
Python-fallback producer still emits a correct effective map). Also decline OOC cyclic
jobs. Self-reference is not a table cycle (handled by D2b drain-fold) - out of scope;
full-frame unchanged.

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
- Native vs Python for a parent key column is a per-column decision and a SPEEDUP only;
  a Python-fallback producer still emits a correct effective map. The native path
  applies to the strategies that have a kernel (hash via `decline_reason(column,
  input_type)` inside the hash dispatch, `_sequential_native.py:181`, plus the D2b
  redact/truncate/passthrough handlers); it is NOT a universal native gate for every
  admitted strategy (rev-3 LOW). The kernel WITNESS is scoped to the columns that
  actually ran native; an admitted pure-value-function producer with no kernel masks in
  Python and is proven by the ordinary oracle-parity assertion, not the kernel marker.

## Design (column-order effective-map, SEQ)

When routing detects a cyclic FK component (needs a real detector, see Routing):

1. **Pass 1 - effective key maps in COLUMN-dependency order (narrow).** Order the FK
   KEY columns of the cyclic component by the acyclic COLUMN graph. For each parent
   key column in that order, read ONLY that column's narrow source data, mask it with
   its strategy (native where admitted), and record the EFFECTIVE source->masked map:
   the actual emitted value per source key (token / raw-if-when-off /
   preserved-raw-orphan), with errored-parent rows excluded into the cascade cache and
   duplicate source keys resolved last-wins. Because admission restricts producers to
   PURE VALUE-FUNCTION strategies, the key-only read carries every input the producer
   needs (no payload sibling, no row position), and last-wins over duplicate source keys
   is LOSSLESS (equal source value => equal emitted value, so no distinct parent-row
   output is dropped) - that is exactly what `_parent_map` produces on the full frame,
   reproduced on narrow columns. A child key column that depends on an already-computed
   parent map consumes it in order.
2. **Pass 2 - mask each cyclic table.** Load each table, mask its payload columns
   normally (D2a/D2b per-column decisions), and resolve its FK child keys by INHERITING
   from the pass-1 effective maps via `resolve_fk_keys` (map lookup + the unchanged
   EXCLUDE-then-CASCADE-then-policy precedence, REMAP re-running the parent handler as
   today). Evict each table after masking.

Memory contract (rev-2 HIGH-2 + rev-3 HIGH-2 fix - peak over the WHOLE component
schedule, accounting for ALL run-retained state, not just the largest table). Existing
SEQ retains a parent's map until ALL its consumers finish (`_sequential.py:529`), and
the admitted mixed topology includes incoming acyclic parents feeding the cycle and
outgoing children consuming it. Peak at any point =
- (every live parent map + its error cache still retained), where "retained" means
  until the LAST consumer across BOTH passes AND outgoing children finishes - not merely
  pass-1 key work (rev-3 HIGH-2; `_sequential.py:301` consumer bookkeeping adapts); PLUS
- (the component's retained narrow key maps/error caches); PLUS
- (the cross-table error state `all_row_errors` + `key_error_rows`, `_sequential.py:443`
  - narrow, keyed by errored rows); PLUS
- (the shared strategy `PoolCache`, which survives table eviction, `_sequential.py:308`
  - bounded by distinct values seen); PLUS
- (the current table's working set: load + pandas conversion + output overlap),
under lazy inputs + a draining sink. `quarantine_entries` (whole masked rows incl
payload, `quarantine.py:184`) is NOT in this formula because the admission gate declines
quarantine-producing policies on cyclic tables (above) - so retained state stays narrow
by construction. The plan defines explicit RELEASE points (drop a map once its last
cross-pass/child consumer finishes) and a COMBINED-budget admission check that reserves
all the resident terms above and rejects when the estimate exceeds budget, with an
explicit decline when any term's bound is unavailable (decline, do not assume it fits).
The win is jobs whose AGGREGATE frames exceed RAM while this combined live state fits; a
single oversized cyclic table, or a combined estimate over budget, declines. Pass 1
needs a PROJECTED key-columns-only read; the current loader reads the whole file
(`_pipeline_sources.py:126`, `_readers.py:270`), but PyArrow `read_table`/
`ParquetFile.read` expose `columns=` (gate-confirmed), so a real projected read is a
buildable PREREQUISITE (build it, or decline opaque whole-table loaders - do not read
the whole frame and select after, which violates the narrow-pass contract).

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
combined live state (retained parent maps + error caches + component narrow maps +
cross-table error state + PoolCache + the current table's working set), checked against
budget at admission. Non-admitted cycles (`when:`-conditional-key / non-pure-value
-function-producer e.g. derived-sibling-read or positional / inherited-key-dependency /
oversized-single-table / transform-bearing-cyclic-table / quarantine-producing-policy /
work-node-cyclic / combined-budget-over) and OOC cyclic jobs decline with a clear coded
reason. Acyclic masking and full-frame unchanged.

**Non-goals.** No re-derivation of matched keys. No fixed-point iteration. No OOC
cyclic support. No projected post-transform contract for transform-bearing cyclic tables
(they decline this cut). No support for sibling-reading/positional key PRODUCERS in a
cycle (they decline; CONSUMER inheritance stays strategy-agnostic). No quarantine-row
accounting in the bound (quarantine policies decline on cyclic tables instead). No
change to full-frame, `_fk_resolve` precedence, or acyclic routing.

## Acceptance tests (authored before impl; include the rev-1 counterexamples)

- **Parity:** 2-table and 3-table cycles, SEQ byte-identical to full-frame on strict
  dimensions; RI asserted both directions. Cover a RANGE of pure-value-function key
  PRODUCERS (hash native, redact/truncate/passthrough) to prove the producer set is not
  silently narrower than stated; AND a case where the matched CHILD's own configured
  strategy differs from the producer's, proving the child INHERITS the producer's
  effective value rather than running its own strategy (consumer stays
  strategy-agnostic, rev-3).
- **The rev-1 counterexamples MUST pass identically to full-frame:** (a) an INCOMING
  acyclic hash parent with `when:` false stays RAW and its matched child inherits that
  RAW value (admitted only if the key is unconditional; if the KEY itself is
  `when:`-gated it declines - test both, per rev-3 LOW); (b) the column chain
  `A.id->B.mid->A.back` where an orphan preserved at `B.mid` stays raw and `A.back`
  inherits the raw value (the column-acyclic table cycle supported today) - the
  effective map carries the preserved-raw value, NOT a re-hash.
- **The rev-3 counterexample MUST decline (not silently mis-mask):** the cycle
  `A.id->B.mid->A.back` with `A.id` using a `derived`/expression strategy that reads a
  payload sibling (e.g. `payload + 100`), no table transforms, work-graph acyclic - all
  the other admission conditions pass, but a key-only read of `A.id` cannot evaluate the
  expression, so the component DECLINES on the pure-value-function-producer condition
  (config-detectable). Also: a positional/row-index producer with duplicate source keys
  declines (a last-wins map would drop distinct per-row outputs).
- **Canonicalization:** a boolean parent `True` matched by an integer child `1`
  inherits the parent's effective token (map lookup), byte-identical to full-frame;
  numeric normalization + duplicate-normalized-key last-wins covered.
- **Orphan/cascade/policy:** preserve/remap/warn/fail + errored-parent cascade through
  a cyclic edge, resolved from the pass-1 effective maps + error cache, identical to
  full-frame.
- **Memory:** a cyclic job whose AGGREGATE frames exceed a set budget but whose
  COMBINED live state (all retained parent maps + error caches + component narrow maps +
  cross-table error state + PoolCache + the current table's working set) fits runs on
  SEQ within the bound; a single oversized cyclic table declines (true memory limit); a
  mixed topology (incoming acyclic parent + cyclic component + outgoing acyclic child)
  whose COMBINED estimate exceeds budget declines even though no single table is
  oversized (rev-2 HIGH-2); an admitted run asserts each parent map is RELEASED only
  after its last consumer ACROSS BOTH PASSES AND outgoing children (peak stays bounded,
  not monotonically growing, rev-3 HIGH-2). **Quarantine:** a multi-table job with a
  wide-payload quarantine policy on a cyclic table DECLINES (it would accumulate whole
  masked rows across tables, rev-3 HIGH-2) - proving the bound is not fooled by
  releasing maps alone.
- **Declines (fail closed, coded, no data in the message):** a `when:`-conditional
  KEY cycle; a non-pure-value-function PRODUCER cycle (derived/sibling-reading, and
  positional/row-index - rev-3 HIGH-1, config-detectable by strategy type + inputs); an
  inherited-key-dependency cycle the pass can't resolve; a cyclic table carrying a
  table-level TRANSFORM (filter/sort/dedup/derived - rev-2 HIGH-1); a cyclic table with
  a quarantine-producing orphan/error policy (rev-3 HIGH-2); a component whose WORK-NODE
  dependency graph cycles (not just the table graph); an OOC cyclic job (unchanged).
  Each decline is detectable at config/route time without inspecting row data.
- **Routing:** an incoming acyclic parent + a cyclic component + an outgoing acyclic
  child + a disconnected acyclic table all in one job order and mask correctly;
  self-reference still routes as today (not treated as a table cycle).
- **Witness / native evidence:** a KERNEL-backed pass-1 producer (e.g. hash) is proven
  on the kernel via the D2b marker witness; a no-kernel pure-value-function producer is
  proven by oracle parity instead (rev-3 LOW - the witness is scoped to columns that ran
  native, not asserted for every producer); inheritance is a map lookup (no kernel).
- **Acyclic unchanged:** every existing acyclic + full-frame-cycle test passes;
  full-frame still masks small cycles. **Module size** under caps (new
  `execution/_sequential_cycle.py`); **NFC** re-assert; **perf gate** on a sized cyclic
  workload (capability-first: it runs bounded at all; native speed on the key columns).

## Known context for the gate

- Revises the program plan's KEEP-CURRENT/V2 cycle disposition to "v1, SEQ bounded,
  unconditional pure-value-function key producers, no table transforms and no
  quarantine-producing policies on cyclic tables, full-retained-state combined-budget
  admission, column-order effective-map"; land the program-plan update with this slice.
  The admitted set the gate confirmed viable: unconditional, transform-free,
  non-quarantining hash/passthrough/redact/truncate chains forming table cycles.
- Depends on D2b; rebase onto post-D2b main before build. D2b provides a native kernel
  SEAM only; D2-cycle adds the component detector, the column-order effective-map pass,
  the projected read (or its prerequisite), and the routing wiring.
- "Attempt, defer if too hard" (Cam): if the effective-map-in-column-order pass or the
  projected read proves larger than a bounded slice, narrow further (more declines) or
  defer to V2 and record where it stopped.

## Docs to update

`docs/compatibility-contract.md` (SEQ masks admitted cross-table cycles; the decline
narrows), CODEMAP for the new module, the program-plan cycle disposition.
