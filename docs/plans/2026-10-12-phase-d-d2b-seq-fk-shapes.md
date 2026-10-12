# Phase D / D2b: SEQ-route hash native across all acyclic FK shapes + composite keys

Status: plan (rev 1, pre-gate).

Rules consulted: 00-universal.md, risk-and-exceptions.md, development-loop.md,
architecture.md, reuse-first.md, testing.md, documentation.md,
autonomous-operation.md. Engine CLAUDE.md (established methodology, 600/700 LOC
cap, log hygiene), compatibility-contract.md.

Risk: R2. Extends native masking across FK shapes with byte-parity + RI promises;
lifts a previously-rejected shape (composite). Plan + independent review + Codex
plan gate before build.

Scope note (Cam 2026-10-12): v1 masks ALL FK shapes natively in Rust to retire the
old pandas/full-frame path. D2b is the `hash`-across-shapes + composite step on the
SEQ route. Cross-table CYCLES are a separate v1 slice (D2-cycle, survey-confirmed
tractable); other operators are D2c.

## FRAME

**Problem.** D2a put `hash` native on the SEQ route for the single-hop scalar FK
case (parent derives native, matched child inherits, REMAP re-derives). The SEQ
route already MASKS every acyclic FK shape correctly in pandas today (chain,
diamond, multi-parent, self-reference, junction, composite), because the shapes are
just tables resolved in `table_topo_order` through the shared
`_dispatch_mask_node` / `_parent_map` / `resolve_fk_keys` path. What is NOT yet
proven or wired is that `hash` runs NATIVE and byte-identical across all those
shapes, and composite (multi-column) FK keys were never exercised on the native
seam. This slice closes that.

**Definition of done.** For `hash` columns on the SEQ route, every acyclic FK shape
masks on the compiled kernel, byte-identical to the Python handler except the
pre-accepted NFC divergence, with the per-shape RI and orphan/policy semantics
unchanged, proven by the D2a strict parity contract + witness extended to each
shape:
- **Chain (A->B->C):** a middle column that is both child and parent hashes native
  end to end; per-hop orphan REMAP is native when the parent column is native (the
  D2a gate already does this).
- **Diamond / multi-parent:** the shared child inherits the first-hit-wins merged
  map (declared edge order); each parent column derives native; the policy-conflict
  guard is untouched.
- **Self-reference:** the drain-fold order is preserved; the self-FK child sees its
  parent-key node's errors; hash native on both.
- **Junction / multiple distinct FKs:** each edge resolves per-edge; native parity.
- **Composite / multi-column key:** each composite PARENT key column derives native
  (already a scalar hash D2a's `prepare_table` routes); the tuple is composed in
  Python/Arrow outside the kernel as today; the composite child inherits tuples (no
  kernel call); orphan composite tuples REMAP per-column native.

**Boundaries / non-goals.** Only `hash`, only the SEQ route, only acyclic shapes.
Cross-table cycles stay on today's behavior (handled by the separate D2-cycle v1
slice). Other operators are D2c. No change to `resolve_fk_keys` precedence, the
orphan/cascade channel, `_parent_map`, the first-hit-wins merge, the policy-conflict
guard, or `table_topo_order`. The OOC-side composite-scalar-child decline
(`out_of_core_composite_fk_scalar_child_unsupported`) is OOC-specific and stays as
is; D2b does not touch OOC.

**Reuse.** D2a's mechanism is the whole engine: `_sequential_native.SeqNativeRun`
(`prepare_table` decides each non-fk-child scalar hash column from its pinned Arrow
type; the three FK paths native-parent / inherited-child / REMAP), the pinned-type
conversion, the SEQ witness (`tests/parity/_seq_witness.py`), and the strict parity
contract. D2b adds shape coverage + the composite parent-column decisions; it adds
no new kernel and (per the survey) composite needs NO tuple-keying kernel support
because the whole table is resident on SEQ.

## Pinned guarantee and non-goals

**Guarantee.** On the SEQ route, `hash` masks natively and byte-identically (modulo
accepted NFC) for every acyclic FK shape above; RI holds (a child key's token equals
its parent's by inheritance, composite by tuple of per-column native tokens); orphan
policy {preserve, remap, warn, fail}, the EXCLUDE-then-CASCADE errored-parent
channel, and multi-parent first-hit-wins + policy-conflict all behave exactly as
today. Output for every non-hash column and every shape not opted into native is
byte-identical to today.

**Non-goals.** No cycles (D2-cycle). No other operators (D2c). No semantics change
to any FK resolution path. No OOC change.

## Established solutions

- D2a native-SEQ mechanism (merged #241): the per-column decision, the three FK
  paths, the pinned-type conversion, the SEQ value/schema witness with the causal
  marker control, atomicity scoped to the transactional sink. D2b reuses all of it.
- D1a compiled hash kernel (`out_of_core/_hash_native.py::native_hash_array`, the
  per-column `derive_batch`); unchanged. Composite is per-column derivation + Python
  tuple composition (survey Section B), so no kernel change.
- The strict D0 parity comparator + goldens (`self_fk`, `composite_key`,
  `nullable_fk`, `orphan_fk`, `relational_parent_child`).

## Design notes

Composite parent key columns are already individual scalar `hash` work nodes
(`_runner.py:253-258`), so `prepare_table` already decides them native; the composite
CHILD is a `composite_fk_group` WorkNode that inherits source-tuple->masked-tuple
pairs from `_parent_map` (`_pandas_adapter.py:656-667`) with no kernel call. So
composite native is: confirm each composite parent column takes the native decision,
confirm the inherited tuple carries the native tokens, and add the disposition +
witness coverage for the composite group. If any composite-specific wiring is needed
(e.g. recording the group's inheritance in the disposition), it goes in
`_sequential_native.py` (currently 323 LOC), NOT in `_sequential.py` (675) or
`_pandas_adapter.py` (697) which are at the size cap. House style: typed decision
reused; one place per fact; witness proves native-consumed per shape.

## Observable behavior and failure modes

- Each acyclic shape: hash native on parent/payload columns; matched child inherits
  (incl. composite tuple); orphan REMAP native when the parent column is native,
  else Python with a value-free reason; disposition reports path per column
  (`scalar` / `fk_inherited` / `fk_remap`), composite child as inherited.
- Multi-parent: first-hit-wins preserved; a policy conflict still raises
  `orphan_policy_conflict` at graph build (unchanged).
- Self-ref: drain-fold error visibility preserved.
- Abort/no-commit, no-Python-retry-after-native, log hygiene: all as D2a.

## Acceptance tests (authored before impl; not weakened later)

Extend the D2a strict parity contract + SEQ witness to each shape; each asserts
native == Python handler (byte, modulo NFC) AND native == full-frame oracle (strict
D0 dimensions), with the witness proving the native tokens were consumed:
- **Chain (3-table):** parity + table order; a middle hash column native end to end;
  per-hop orphan REMAP native (parent native) and declined-to-Python (parent not
  native).
- **Diamond / multi-parent:** first-hit-wins overlapping-key precedence; per-edge
  orphan policies; a policy-conflict case still raises; shared child inherits native
  parent tokens; witness shows parents native, child inherited.
- **Self-reference:** `self_fk` golden parity on SEQ; drain-fold error-visibility
  case; hash native.
- **Junction / multiple distinct FKs:** 4-tuple orphan key, per-child policy,
  `nullable_fk` golden.
- **Composite:** `composite_key` golden + canonical matched / orphan / partial-null
  tuple cases; each parent column native; composite child inherits the native tuple;
  orphan composite tuple REMAP native per-column; witness coverage for the group. A
  control that the OOC scalar-child decline is unchanged (OOC-side, untouched).
- **Unset / non-hash byte-identity:** any shape with no admitted hash column is
  byte-identical to today (whole-table digest).
- **Witness non-fakeability per shape:** the discarded-native + equal-Python control
  fails the marker proof for each shape (reuse the D2a causal control).
- **Guards / abort:** nullable-int hash key still rejected; transactional-sink abort
  leaves nothing committed; no Python retry after a native selection.
- **Module size** under caps; **G3/G4 pins** updated only if a call is added;
  **NFC** re-asserted (same kernel).
- **Perf (predeclared):** a multi-shape FK workload (chain + composite) native p50
  below the Python handler, identical digests, memory within the route envelope.

## Known context

- Cross-table cycles: separate v1 slice **D2-cycle** (survey a3c400d: tractable, no
  fixed-point solver; value-keyed re-derivation + a bounded two-pass source-key
  collection). D2b keeps today's cycle behavior unchanged.
- The Phase D program plan (shape x route matrix) is still only on the unmerged
  `feat/phase-d-fk-routes` (e2401aad); its matrix now reads "native everywhere
  (cycles via D2-cycle)". Land it on main (separate housekeeping).
- Multi-parent is flagged HIGH in the program: the first-hit-wins semantics must not
  be unified with independent-subset traversal; D2b preserves it exactly.
- Composite cross-chunk tuple keying is the OOC concern, NOT SEQ (whole table
  resident); no kernel work on SEQ.

## Docs to update

`docs/compatibility-contract.md` (SEQ hash now native across all acyclic FK shapes +
composite; same admitted set + accepted NFC), CODEMAP if a module is added,
`docs/strategies.md` only if a user-visible note is needed (none expected).
