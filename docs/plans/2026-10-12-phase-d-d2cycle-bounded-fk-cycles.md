# Phase D / D2-cycle: cross-table FK cycles native on the bounded (SEQ) route

Status: plan (rev 1, pre-gate). Branches off main; rebases onto post-D2b main before
build (it extends the D2a/D2b SEQ native mechanism and D2b's re-derivation decision).

Rules consulted: 00-universal.md, risk-and-exceptions.md, development-loop.md,
architecture.md, reuse-first.md, testing.md, documentation.md,
autonomous-operation.md. Engine CLAUDE.md (established methodology, 600/700 LOC cap,
log hygiene, snapshot-before-extraction), compatibility-contract.md.

Risk: R2. Adds masking support for a shape the bounded routes decline today, with
byte-parity + RI promises. Plan + independent review + Codex plan gate before build.

Scope note (Cam 2026-10-12): v1 moves every FK shape that works today onto the Rust
path to retire the old full-frame-only path. Cross-table cycles mask on full-frame
TODAY, so they are a migration target for v1 (not net-new like composite chains). The
survey (a3c400d) confirmed this is tractable: value-keyed determinism removes the
ordering barrier; the work is a bounded two-pass key collection, not a fixed-point
solver.

## FRAME

**Problem.** A cross-TABLE FK cycle (A has an FK to B and B has an FK to A, so neither
table's keys can be masked "parent-first") is masked TODAY only by auto-routing to
full-frame, which holds every table resident and needs no table-level order
(`test_pipeline_routing.py:563`, reason `cross_table_cycle`). The bounded routes
decline: SEQ `table_topo_order` raises `relationship_cycle` (`_sequential.py`), OOC
`out_of_core_relationship_cycle_unsupported`. So a LARGE cyclic job (too big for a
full-frame box) has nowhere to run. That is the only gap, and it keeps a shape on the
old path.

**Why it is tractable (survey-confirmed).** Masking is value-keyed and deterministic:
a masked key is a pure function of `(seed, namespace, source_value)`, so a child FK
RE-DERIVES the identical token from its OWN source value and referential integrity
holds WITHOUT resolving the parent first. The current decline is a conservative guard
from two design choices, not a math barrier: (a) a matched child INHERITS the parent's
masked value rather than re-deriving (`_sequential_native.py` module docstring); (b)
the table-at-a-time eviction model needs a topological order. Full-frame already masks
these column-acyclic table cycles today (proof the values are computable); the only
thing SEQ lacks is full-frame's all-resident model.

**Definition of done.** On the SEQ route, a cross-table cycle whose FK KEY columns all
use a value-keyed strategy masks natively and byte-identically to the full-frame
oracle, within the bounded-memory envelope (key columns resident, not whole frames),
with orphan/cascade/policy semantics unchanged. A cycle whose key uses a
non-value-keyed strategy, or any OOC cyclic job, keeps today's decline with a clear
reason (not a silent change).

**Boundaries / non-goals.** SEQ route only. Only value-keyed KEY strategies on the
cycle's key columns (the set where re-derivation provably equals inheritance):
`{hash, redact, truncate, passthrough}` (exactly OOC's key allowlist). Payload columns
keep the normal per-column decision (D2a/D2b). OOC cyclic support stays deferred (OOC
has no cascade channel and streams joins; a separate effort). No change to full-frame
(it already handles cycles), to `_fk_resolve` precedence, or to the acyclic routing.
Self-reference is NOT a table cycle (already handled, D2b drain-fold) and is out of
scope here.

## Established solutions

- Two-pass / fixed-point key assignment for cyclic reference graphs is the standard
  way to mask a cycle without a topological order: resolve the stable key mapping
  first, then apply. Here the "stable mapping" is free because the key is value-keyed
  (no iteration to a fixed point is needed; one collection pass suffices). Cite the
  survey's analysis.
- Reuse, not reinvent: D2b already added a RE-DERIVATION decision model
  (`_sequential_native._remap_decisions`, keyed on a parent column, re-derives orphan
  keys on the kernel without rehashing matched children). D2-cycle generalizes that
  same re-derivation primitive from "orphan REMAP" to "cycle-member key", so the
  kernel path and the admitted-type predicate are reused unchanged. The D1a hash
  kernel and the redact/truncate/passthrough handlers are reused as-is.
- `_fk_resolve.resolve_fk_keys` EXCLUDE-then-CASCADE-then-policy precedence is reused
  for orphan determination against the collected key sets.

## Design (two-pass, SEQ)

When `run_sequential` detects a cyclic FK component (the existing table-cycle
detector), instead of declining, run the cycle members through a two-pass scheme while
acyclic tables keep today's single-pass topo flow:

1. **Pass 1 - collect narrow source keys (bounded).** For each table in the cyclic
   component, read ONLY its FK key columns (parent-key and child-key source values),
   not the wide frame. Build the per-edge source-key sets `resolve_fk_keys` needs for
   orphan determination. Memory is the key columns of the cycle members only, well
   under the full-frame footprint; this is what makes a large cyclic job fit.
2. **Pass 2 - mask each table independently.** Load each cyclic table in turn, mask its
   own columns from their own source values (value-keyed, so no dependency on another
   table having masked first). An FK child key RE-DERIVES its token from the child's
   own source value via the D2b re-derivation path (identical to the parent's token by
   value-keyed determinism), with orphan/errored-parent/REMAP/preserve/warn/fail
   resolved against the pass-1 key sets. Evict each table after masking as today.

Admissibility gate: every FK KEY column in the cyclic component must use a value-keyed
strategy `{hash, redact, truncate, passthrough}` (the re-derivation == inheritance
set). If any key column uses a non-value-keyed strategy, the cycle declines with a
clear coded reason (fail closed) - the two-pass equivalence does not hold there.
Payload columns are unrestricted (normal D2a/D2b decisions).

Module placement: the two-pass orchestration + the cycle admissibility gate go in a
NEW module (e.g. `execution/_sequential_cycle.py`), NOT in `_sequential.py` (677) or
`_pandas_adapter.py` (697) which are at the size cap. The re-derivation decision is
reused from `_sequential_native.py`.

## Pinned guarantee and non-goals

**Guarantee.** On SEQ, a value-keyed-key cross-table cycle masks byte-identically to
the full-frame oracle (strict D0 dimensions), RI holds (every FK edge's child token
equals its parent's by value-keyed re-derivation), orphan/cascade/policy behave as on
the acyclic routes, and peak memory stays bounded by the cycle members' KEY columns
(not their full frames). A non-value-keyed-key cycle and every OOC cyclic job keep
today's decline with a clear reason. Acyclic masking and full-frame are unchanged.

**Non-goals.** No OOC cyclic support. No fixed-point iteration (unnecessary). No change
to full-frame, `_fk_resolve` precedence, or acyclic routing. No new crypto.

## Observable behavior and failure modes

- Value-keyed-key cycle on SEQ: masks natively, byte-identical to full-frame; RI holds
  parent+child across the cycle; disposition reports the cycle members' key columns as
  re-derived (not inherited), value-free.
- Non-value-keyed-key cycle: declined with a coded reason (identifier-only), run
  aborts with nothing committed on a transactional sink.
- OOC cyclic job: unchanged decline.
- A cycle too large even for the two-pass key collection: the existing OOM-risk
  reject applies (bounded, not full-frame-sized).
- Log hygiene: no cell/key/value in reasons or logs.

## Acceptance tests (authored before impl; not weakened later)

- **Parity:** a 2-table cycle (A<->B) and a 3-table cycle, value-keyed keys (hash +
  redact/truncate/passthrough variants), mask on SEQ byte-identically to the
  full-frame oracle on every strict D0 dimension; RI asserted (child token == parent
  token across both directions of the cycle).
- **Re-derivation == inheritance:** a control proving the SEQ two-pass result equals
  what parent-first inheritance would produce for the value-keyed key set (the
  equivalence the design rests on), per admitted key strategy.
- **Orphan / cascade / policy:** orphan keys in a cyclic edge under preserve / remap /
  warn / fail resolve against the pass-1 key sets exactly as on the acyclic routes
  (reuse the `_fk_resolve` precedence tests, adapted to the cycle).
- **Memory bound:** a large cyclic job that would NOT fit full-frame masks on SEQ
  within the route's memory envelope (peak RSS bounded by key columns), proven on a
  sized workload; this is the headline "moves the large cyclic job off full-frame".
- **Decline (fail closed):** a cycle with a non-value-keyed key strategy declines with
  the coded reason, no sink commit; an OOC cyclic job still declines; the error carries
  no data.
- **Acyclic unchanged:** every existing acyclic + full-frame cycle test passes
  unchanged (full-frame still masks small cycles; `test_pipeline_routing.py:563`).
- **Witness:** the SEQ witness proves the cycle key columns were masked on the kernel
  (re-derived), reusing the D2b marker approach.
- **Module size** under caps (new `_sequential_cycle.py`), **G3/G4 pins**, NFC
  re-assert (same kernel), **perf gate** (predeclared) on a sized cyclic workload.

## Known context for the gate

- This revises the program plan's KEEP-CURRENT/V2 cycle disposition (§4/§10) to "v1,
  SEQ bounded, value-keyed keys" per Cam 2026-10-12. Land the program-plan update with
  this slice. See [[decoy-phase-d-v1-fk-rust-all-shapes]].
- Depends on D2b (the re-derivation decision model + the SEQ native mechanism);
  rebase onto post-D2b main before build.
- OOC cyclic support, non-value-keyed-key cycles, and a full-frame-independent large
  cyclic path beyond the key-column bound are explicit non-goals / later work.

## Docs to update

`docs/compatibility-contract.md` (SEQ now masks value-keyed-key cross-table cycles;
the decline narrows to non-value-keyed keys + OOC), CODEMAP for the new module,
the Phase D program-plan cycle disposition.
