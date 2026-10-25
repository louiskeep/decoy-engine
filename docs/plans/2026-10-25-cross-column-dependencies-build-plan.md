# Build plan: cross-column dependency groups (decoy-engine)

Status: plan (Phase 2). Follows the Phase-1 discussion (`cross-column-dependencies-design.md`, dennis +
Codex both-approved). This plan is code-grounded and reuse-first: it must not duplicate the primitives the
discussion identified. It will go through the plan gate (dennis + Codex) before any build.

## 0. The one correctness fact this plan is built on

`derived`, `case_when`, `group_key`, and `joint_mask`'s `key_by` all read the CURRENT MUTABLE per-table
frame (`_pandas_adapter.py:386`), and non-FK nodes are ordered by a lexicographic tie-break
(`_runner.py:60`, `order_work`). (`case_when` is NOT a separate handler: it is a construct in the
`derived` expression grammar, so the PR-1 fix set is `derived` (any expression, including `case_when`),
`group_key`, and `joint_mask`.) So which value a dependent reads (source vs a sibling's masked value) is
decided by column-name sort order, not by declared data dependency. This is a latent WRONG-VALUE bug today
(verified at runtime by the Phase-1 gate), independent of this feature. The deterministic, ordered
cross-column precedents are: FK key propagation, where the child reads the parent's MASKED value from the
LIVE frame under the ordering edge (the pre-mask `source_snapshots` hold only the source keys for the
source-to-masked map, not a masked snapshot); and `date_shift` group_by, which reads a pre-mask SOURCE
anchor (`group_anchor_snapshots`). So FK is the precedent for a MASKED-driver read (live frame) and
`date_shift` is the precedent for a SOURCE-driver read (pre-mask snapshot). Neither is a post-mask
snapshot; no post-mask snapshot exists.

The feature and the bug fix are the same mechanism: make a dependent column read its drivers' intended
values through an explicit ordering edge (a live-frame read for a masked driver, a pre-mask snapshot for a
source driver), never by lexicographic luck.

## 1. Goal and scope

Goal: a user declares that columns depend on each other, and the engine keeps them consistent after masking,
deterministically. Reuse the existing primitives; add only the ordering edge + the driver-read mechanics
(no new snapshot stage; see §2.3).

Recommended scope split (the gate and Cam refine; pre-release, nothing locked):
- V1 (correctness + core, full-frame/sequential):
  1. Fix the latent order-dependent read: make `derived`/`case_when`/`group_key`/`joint_mask` read declared
     drivers deterministically under an ordering edge (a masked driver from the live frame, a source driver
     from a pre-mask snapshot), never by lexicographic luck.
  2. Explicit functional derivation and conditional assignment (situations 3.3, 3.4) reading drivers'
     MASKED values deterministically.
  3. Keep the joint/tuple case (3.2) working, with its key read made deterministic.
- V2 (demand-gated): out-of-core/streaming derivation (OOC sibling-context channel); constrained reference
  SELECTION (filter candidates against retained drivers); general string checksums.

## 2. The mechanism (reuse map, no fork)

1. Declaration surface. Reuse the existing shapes, do not invent a parallel one:
   - joint/tuple: `coherent_with: [cols]` + `joint_mask` (already shipped).
   - functional/conditional: the `derived` strategy with its `case_when` grammar (already shipped), plus a
     way to mark which referenced columns are DRIVERS whose MASKED value must be read. Prefer inferring the
     driver set from the expression's column refs (`check_derived_column_refs` already extracts them) over a
     new config key; add an explicit opt-in only if inference is ambiguous.
2. Ordering. Add dependency edges (dependent -> its driver columns) into `order_work` (`_runner.py:233`),
   alongside the FK edges. `order_work` today takes only `(work, relationship_graph)` and derives edges
   solely from `relationship_graph.edges`; driver refs live in config, so this needs a NEW column-dependency
   input channel (a new param, or an extension of the graph object), with FK ordering kept dominant (design
   §4). The new edges are same-table (WorkNode keys are `(table, columns)`; `check_derived_column_refs`
   enforces same-table refs at `_checks.py:726`). Cycle detection: `check_derived_column_refs`
   (`_checks.py:652`) detects cycles ONLY among `derived` columns, same-table, DFS terminating at
   non-derived nodes; it does NOT see `group_key` sibling refs or `joint_mask` `key_by` edges. So either
   EXTEND it to the new edge types at plan time, or scope the plan-time cycle check to derived-derived and
   rely on the runtime `cyclic_work_ordering` backstop (`_runner.py:320`) for group_key/joint_mask cycles.
   Do not add a second independent validator. A driver column ref must resolve to the NODE that WRITES it,
   which is not always 1:1 column=node: a COMPOSITE provider is a bundle node writing several columns
   (`_runner.py:112`), so a ref to any of its outputs resolves to that one node, and that node must run
   before any dependent. Scalar `joint_mask` is NOT a bundle node, so confirm how its output columns are
   represented in the plan before wiring the edge. The builder must derive the writer-node mapping from the
   actual per-provider representation, not assume column=node and not assume scalar `joint_mask` is a bundle.
3. Driver read (NO new snapshot). There is NO post-mask snapshot in the engine today: `source_snapshots`
   and `group_anchor_snapshots` are PRE-mask only, built for FK-parent columns and group anchors
   (`_pandas_adapter.py:232,273-281`). The masked-sibling read that works (FK child reading a masked parent)
   does NOT use a snapshot; it reads the masked value from the LIVE frame under the ordering edge
   (`_pandas_adapter.py:633-636`). So:
   - a driver whose MASKED value is wanted: reuse that FK pattern (ordering edge + live-frame read). Do NOT
     build a post-mask snapshot stage; that is the parallel machinery this plan forbids.
   - a driver whose SOURCE (pre-mask) value is wanted: reuse the pre-mask snapshot, but note
     `source_snapshots`' build set iterates FK-parent columns only today and must be EXTENDED to the
     declared non-FK driver columns.
   Either way the explicit ordering edge removes the lexicographic-luck read (the bug fix).
4. Per-situation wiring (driver reads follow §3: live frame for a masked driver, pre-mask snapshot for a
   source driver):
   - 3.2 joint/tuple: `joint_mask`/composite, with `key_by` read deterministically under the ordering edge
     (live frame if the declared phase is masked, pre-mask snapshot if source), fixing its order-dependent
     key. Validity still comes from the reference row.
   - 3.3/3.4 functional/conditional: `derived`/`case_when` reading each driver per §3 under the ordering edge.
   - coordinated/interval-preserving: `date_shift` group_by (already correct; the SOURCE-anchor precedent).
   - constrained reference selection: V2, a `joint_mask` variant that filters candidate rows by a retained
     driver before the keyed pick (`joint_mask.py:285` does not filter today).
   NOTE (group_key already streams): `group_key` is admitted on the chunked route and its admission checks
   use effective driver types under `order_work` (`_chunked_group_key.py:218`). Any change to its read
   semantics must keep that chunked admission correct (update it, do not break it); treat group_key's
   chunked path as a constraint, not a greenfield.
5. Scale: V1 runs full-frame/sequential (where sibling context exists). OOC is V2: the per-column OOC kernel
   lacks a sibling-context channel (`out_of_core/_compat.py:104`); the chunked path already passes the chunk,
   so chunked is closer. In AUTO mode a derivation job on an OOC-eligible shape FALLS BACK to
   sequential/full-frame and runs correctly (not an error); only an explicit `execution_mode=out_of_core`
   declines with the coded error. V1 relies on that existing auto-fallback and adds no new decline path.

## 3. Behavior and failure modes

- A derived-to-derived dependency cycle is rejected at PLAN time (reuse `_checks.py:652`). A cycle routed
  through `group_key`/`joint_mask` edges is rejected at plan time only if that checker is extended to those
  edges, otherwise by the runtime `cyclic_work_ordering` backstop (consistent with acceptance test 4).
- A driver that is suppressed/dropped, or absent, is a coded error (the dependent has nothing to read).
- Determinism: for a fixed config, the output is reproducible AND independent of column-name sort order
  (the bug-fix invariant).
- Privacy: exact recompute from a weakly-masked driver preserves the driver's residual identifying
  information; warn in docs, do not silently "repair."
- OOC/native: in AUTO mode an OOC-shaped derivation job falls back to sequential/full-frame and runs
  correctly; under explicit `execution_mode=out_of_core` it declines with an actionable coded error. Never
  a silent source-value read.

## 4. Acceptance tests (write these first)

1. BUG-FIX INVARIANT: a `derived`/`case_when` column that references a masked driver produces the SAME
   output whether its column name sorts before or after the driver (rename the columns; output stable),
   AND that stable output equals the MASKED-driver result, not the source value (so a fix that
   deterministically read SOURCE would still fail this test). This fails on main today and is the core
   regression this plan fixes.
2. A derived column reads the driver's MASKED value deterministically (e.g. `full_name = concat(first,
   last)` with first/last redacted yields redacted-based output regardless of name order).
3. `joint_mask` `key_by` reads the SOURCE key deterministically via the pre-mask snapshot (the `date_shift`
   group-anchor precedent), not the live masked frame and not by lexicographic luck; the emitted tuple is
   the same for the same source key. Separately, `group_key.group_by` reads the MASKED group value
   deterministically under the ordering edge (per its docstring), not by lexicographic luck.
4. A derived-to-derived dependency cycle is rejected at PLAN time with the coded error. A cycle routed
   through `group_key`/`joint_mask` edges is also rejected: at plan time if `check_derived_column_refs` is
   extended, otherwise by the runtime `cyclic_work_ordering` backstop. Assert whichever the chosen design
   produces.
5. A dropped/absent driver is rejected with the coded error.
6. `date_shift` group_by interval preservation still holds (regression guard on the precedent).
7. OOC behavior: under explicit `execution_mode=out_of_core` a derivation declines with the actionable
   coded error; in AUTO mode on an OOC-eligible shape it FALLS BACK to sequential/full-frame and yields
   correct output. Neither path silently reads a source value.
(Constrained selection and OOC derivation get their own tests when those V2 slices are built.)

## 5. No-duplication checklist (the gate must confirm each)

- Ordering: extend `order_work`; do not add a parallel scheduler.
- Validation: reuse `check_derived_column_refs` (`_checks.py:652`); do not add a second cycle checker.
- Driver read: a MASKED-driver read reuses the FK ordering-edge + live-frame pattern (no post-mask snapshot
  exists, do not build one); a SOURCE-driver read extends the existing pre-mask `source_snapshots` build set
  to the declared driver columns. Do not invent a third snapshot path.
- Expression: reuse the `derived`/`case_when` grammar; do not add a new mini-language.
- Joint/tuple + reference tables: reuse `coherent_with`/`joint_mask`/composite + `reference_tables` loader.
- Coordinated shift: reuse `date_shift` group_by; do not re-implement interval preservation.

## 6. Decisions for the plan gate and Cam
- Confirm the V1/V2 split in section 1 (is masked-driver functional derivation V1, or V2 behind the
  bug-fix-only V1?). The bug fix itself (section 2.3) is V1 regardless, because it is a correctness fix.
- Driver-set inference from expression refs vs an explicit config key (section 2.1).
- Whether the bug fix ships as its own small PR first (it is a standalone correctness fix) ahead of the
  larger feature.
- Module-size budget (sentry, census-authoritative): `plan/_checks.py` is ~712 LOC (shrunk since the first
  draft; still over the 700 hard max, which may only shrink), so extending `check_derived_column_refs` in
  place first needs an extraction to shrink it; `_pandas_adapter.py` is 697 LOC (DENSE, 3 under the cap), so
  the driver-ordering / source-snapshot extension there gains at most +3 lines via a visible census bump,
  else a small extraction. Budget both into the build.
- Semantics flip + frozen surface: the fix makes `derived`/`case_when`/`group_key`/`joint_mask` key_by read
  drivers' MASKED values by default. Correct the `derived` docstring (it says source), audit shipped
  configs/tests that rely on the current accidental source-read, and decide whether `when:` predicate
  column-refs (the same latent bug and ordering issue; interacts with the pinned
  `when_predicate_reads_masked_column` compat behavior, `docs/compatibility-contract.md:446`) are in or out
  of V1 scope. The plan is currently silent on `when:`; pick a scope.

### Recommended resolutions (pre-release, not locked; starred ones are Cam's final call)
- V1/V2 split *: V1 = the bug fix (PR 1) + full-frame/sequential functional derivation + the deterministic
  joint key; V2 = OOC derivation, constrained reference selection, string checksums. Recommended.
- Bug-fix-first: YES. Ship the order-dependent-read fix as PR 1 (standalone correctness fix, acceptance
  test 1), ahead of the feature surface.
- Driver-set: INFER from the expression's column refs (`check_derived_column_refs`); add an explicit config
  key only if a case is ambiguous.
- Driver-read assignment per strategy (SETTLED, grounded in each strategy's own documented contract, not a
  guess; a build-time code read corrected `group_key` below):
  - `derived` (any expression, including `case_when`): MASKED. Read drivers from the LIVE frame under the
    ordering edge (acceptance test 1 pins "stable output equals the MASKED-driver result").
  - `group_key`'s `group_by`: MASKED. Its own docstring is explicit ("masked group_by read post-mask ...
    NOT a pre-mask snapshot"), so align the code to that intent: read the masked group_by from the LIVE
    frame under the ordering edge. (This CORRECTS an earlier draft that said SOURCE; the build-time read
    caught it against the docstring. Keep the chunked `group_key` admission on effective driver types
    correct, `_chunked_group_key.py:218`.)
  - `joint_mask`'s `key_by`: SOURCE. Its docstring says "key_by: Source column" and it is the FK-key
    identity analogy (same source entity -> same reference tuple), so read the SOURCE key from a PRE-MASK
    snapshot, reusing the `date_shift` group_by precedent (`ctx.group_anchor_snapshots`, the same FK-snapshot
    machinery) rather than the live frame. "Deterministic under the ordering edge" here means via that
    snapshot, not a masked live read.
  `when:` predicate refs stay OUT of PR 1 (they interact with the pinned `when_predicate_reads_masked_column`
  compat behavior, `docs/compatibility-contract.md:446`).
- `when:` scope *: OUT of V1. It has the same latent order-dependent read and interacts with the pinned
  `when_predicate_reads_masked_column` compat behavior, so fold it into the V2 slice with its compat
  review rather than flipping predicate semantics inside the V1 bug fix.
The two starred items (V1/V2 boundary, `when:` scope) are genuine product-scope calls for Cam; the rest are
engineering defaults the builder should follow unless Cam overrides.
