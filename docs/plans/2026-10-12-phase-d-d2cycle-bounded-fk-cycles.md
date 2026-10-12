# Phase D / D2-cycle: cross-table FK cycles native on the bounded (SEQ) route

Status: plan (rev 2, pre-gate). rev 1 NO-GO (4 HIGH/1 MEDIUM): the "re-derive a
matched FK key from the child's source value" premise was WRONG. rev 2 replaces it
with the correct approach the gate pointed to: build the narrow EFFECTIVE
source->masked-key MAPPING in COLUMN-dependency order, then inherit from it (no
re-derivation of matched keys). Branches off main; rebases onto post-D2b main before
build. Cam 2026-10-12: keep in v1 and ATTEMPT it; defer to V2 if it gets too hard.

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

**Boundaries / v1 narrower-first cut.** SEQ only. Admit a cyclic component only when
every FK KEY column in it uses an UNCONDITIONAL value-keyed strategy
`{hash, redact, truncate, passthrough}` with compatible canonicalization. DECLINE
(fail closed, clear reason), as a deliberate first cut: `when:`-conditional keys in a
cycle; a key whose effective value depends on an inherited-key dependency the pass
cannot resolve in column order; and a single cyclic table whose own working set does
not fit (that is a true memory limit, not a cycle limit). OOC cyclic support, payload
cycle-specific handling beyond the normal per-column decision, and self-reference
(not a table cycle; handled by D2b drain-fold) are out of scope. Full-frame is
unchanged.

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

Memory contract (honest, rev-1 H3 fix): peak = the retained narrow key-column maps for
the component PLUS the largest single table's working set (load + pandas conversion +
output overlap), conditional on lazy inputs and a draining sink. The win is jobs whose
AGGREGATE frames exceed RAM while each table's working set fits - NOT a job with one
oversized cyclic table (that still cannot run and declines). Pass 1 needs a PROJECTED
(key-columns-only) read; if the loader cannot project, that is a prerequisite
(specify it or decline until it exists).

Canonicalization (rev-1 H1 fix): because pass 1 stores the parent's EFFECTIVE OUTPUT
keyed by the normalized FK key and pass 2 INHERITS by the same normalized lookup
(identical to the oracle), the `True`==`1` divergence cannot arise - there is no
re-derivation. Admit only edges whose key canonicalization matches the oracle's FK
normalization (the value-keyed set does); others decline.

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
component key-maps + the largest table's working set. Non-admitted cycles
(conditional/inherited-key-dependency/oversized-single-table/non-value-keyed-key) and
OOC cyclic jobs decline with a clear coded reason. Acyclic masking and full-frame
unchanged.

**Non-goals.** No re-derivation of matched keys. No fixed-point iteration. No OOC
cyclic support. No matched-key canonicalization beyond the value-keyed set. No change
to full-frame, `_fk_resolve` precedence, or acyclic routing.

## Acceptance tests (authored before impl; include the rev-1 counterexamples)

- **Parity:** 2-table and 3-table cycles, value-keyed keys (hash + redact/truncate/
  passthrough), SEQ byte-identical to full-frame on strict dimensions; RI asserted
  both directions.
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
  per-table working set fits runs on SEQ within the bound; a single oversized cyclic
  table declines (true memory limit).
- **Declines (fail closed, coded, no data in the message):** a `when:`-conditional
  KEY cycle; a non-value-keyed-key cycle; an inherited-key-dependency cycle the pass
  can't resolve; an OOC cyclic job (unchanged).
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
  unconditional value-keyed keys, column-order effective-map"; land the program-plan
  update with this slice.
- Depends on D2b; rebase onto post-D2b main before build. D2b provides a native kernel
  SEAM only; D2-cycle adds the component detector, the column-order effective-map pass,
  the projected read (or its prerequisite), and the routing wiring.
- "Attempt, defer if too hard" (Cam): if the effective-map-in-column-order pass or the
  projected read proves larger than a bounded slice, narrow further (more declines) or
  defer to V2 and record where it stopped.

## Docs to update

`docs/compatibility-contract.md` (SEQ masks admitted cross-table cycles; the decline
narrows), CODEMAP for the new module, the program-plan cycle disposition.
