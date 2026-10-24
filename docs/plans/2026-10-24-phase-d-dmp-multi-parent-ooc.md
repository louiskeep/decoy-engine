Status: plan (Phase D / Dmp build-slice: multi-parent FK admitted on the OUT-OF-CORE route by a merged first-hit-wins parent relation. rev 1, pre-gate. Written by the remote-agent engine track (ROADMAP A-9); built tests-first on `feat/phase-d-dmp` after Dcomp-OOC; the owner's two-model plan gate runs on this document and its findings are folded as later revisions.)

# Phase D / Dmp: multi-parent FK on the out-of-core route (first-hit-wins, declared order)

Rules consulted: dev-rules `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md`, `documentation.md`, `reuse-first.md`, `testing.md`, `architecture.md`; engine `CLAUDE.md` (established methodology, 600/700 LOC census, `internal/` boundary, comments explain why, pre-GA hard delete, no em-dashes); `docs/compatibility-contract.md`. Grounding: the Phase D program plan `docs/plans/2026-10-10-phase-d-fk-routes-program.md` (section 4 "Multi-parent" row: SEQ admit first-hit-wins, OOC "DECLINE unless a slice adds it", HIGH); the multi-parent semantics in `relationships/_graph.py` (module docstring and `orphan_policy_conflict`), `_pandas_adapter._resolve_fk_node` (the `setdefault` merge in edge order, REMAP through the first edge's parent) and `_fk_resolve.gather_errored_parent_keys`; the D2b SEQ coverage (`test_phase_d_d2b_seq_shapes.py`, first-hit precedence with distinguishable tokens, reversed-order control, policy conflict); the #253 pin of the out-of-core decline (`tests/unit/execution/test_out_of_core_relation.py`); a probe of the oracle and SEQ on the shapes below at `origin/main @ 0b8219f` (2026-10-24). Base: `origin/main @ 0b8219f`. Branch: `feat/phase-d-dmp`. Depends on nothing in Dcomp-OOC (independent branches); the test helpers are shared by import.

## 0. What is already true, and what this slice adds

Multi-parent FK (one child column tuple declaring edges to N parent tables, the polymorphic-association pattern) is DONE on the full-frame oracle and the sequential route: the graph requires one orphan policy per shared child tuple (`orphan_policy_conflict` at compile, unchanged here), the executor merges the parent maps first-hit-wins in `graph.edges` order, a row is an orphan only when absent from every parent, REMAP mints through the FIRST edge's parent seeds, and the WARN / FAIL reports name the first edge. D2b proved it hash-native on SEQ with distinguishable per-parent tokens and a reversed-order control; #253 found "Dmp as a feature slice already shipped" and pinned the one gap, the out-of-core decline `out_of_core_multi_parent_child_unsupported`.

This slice ADMITS the shape on the out-of-core route. The route today opens one joiner per incoming edge and lets later edges overwrite a shared child column ("the whole-child contract" for OVERLAPPING but distinct edges), which for a multi-parent tuple would be last-hit-wins with wrong orphan accounting; that is why the gate declines it. The design below keeps every joiner, driver and lane untouched: the N member relations of one child tuple are merged, before the child table streams, into ONE first-hit-wins parent relation, and the child is then joined exactly as a single-parent child. The oracle's dict `setdefault` in edge order and the merged relation's `min(edge_rank)` per join key are the same function.

Not a kernel slice (program section 0): FK key resolution on OOC is DuckDB's join; parent keys and payload are already on the Rust kernels (D1a). The merge is a DuckDB relational step in the same bounded shape `_relation.py` already uses.

## 1. Risk

R2 (RI-critical: a wrong precedence silently rebinds a child to the wrong parent's masked key; a wrong orphan count changes the FAIL/WARN disposition). Adversarial acceptance: the strict nine-dimension comparator against the oracle with DISTINGUISHABLE per-parent tokens (different namespaces, so a reversed precedence cannot hide behind equal tokens), the reversed-declaration control, and warnings / errors / evidence / provenance each on its own channel.

## 2. Guarantee and non-goals

**Guarantee.** For every multi-parent FK job the out-of-core gate admits (same strategy, payload and acyclicity rules as today; only the multi-parent rejection is removed), the out-of-core output is strictly identical to the oracle's on every dimension but `field_metadata` (the D0-G1 literal), under PRESERVE, REMAP and WARN; FAIL with a genuine orphan raises the oracle's code and message (naming the first edge's parent); the WARN warning is the oracle's object (first edge's identifiers, orphans counted against ALL parents); the precedence is `graph.edges` order (the order `parents_of` yields and the oracle's `setdefault` walks), proven with distinguishable tokens and the reversed control; the invariance set is the Dcomp one (batch size 1/2/default, both lanes, resident and sink, `native_threads` 1/4, companion present and hidden); composite multi-parent (N composite parents for one composite child tuple) is admitted by the same mechanism; the member relations' masked key types must be identical, otherwise the job fails closed with the sanctioned `out_of_core_fk_key_dtype_unsupported` before any output.

**Non-goals.** No change to the graph-level `orphan_policy_conflict`, the oracle, SEQ, `_fk_resolve`, the joiners (`_batch_join`, `_stream_join`), the drivers' per-edge loops, the route seam, or the subset independent-traversal path (program section 4: "do NOT unify with subset independent-traversal"; the subset module is untouched). No heterogeneous-type merge (declined, section 6). No change to the errored-parent cascade semantics: OOC admits no row-error strategy, so `gather_errored_parent_keys` has no out-of-core counterpart to build.

## 3. FRAME (verified at 0b8219f)

- Precedence source of truth: `build_relationship_graph` SORTS edges by `(parent_table, parent_columns, child_table, child_columns)`, so "declared order" at execution time means `graph.edges` order, which `parents_of` preserves and `_resolve_fk_node` walks (`edges[0]` first). A hand-built `RelationshipGraph` with edges in another order is honoured in that order by the oracle (probed: reversing the two edges flips the shared key to the second parent's token). The out-of-core runner's `_edge_indexes` builds `incoming[table]` from `graph.edges` in order, so grouping incoming edges by `child_columns` preserves the same precedence with no new ordering rule.
- Relation shape: every `ParentKeyRelation` is a deduplicated parquet file `(__decoy_fk_join_key, __decoy_masked_key, __decoy_masked_key_1, ...)`, one masked column per key component, built last-write-wins by `_relation._build_relation` through the three-step STAGE / `max(row_nr) GROUP BY key` / JOIN-BACK form chosen because `arg_max` and window forms keep O(distinct-key) state resident. The merge reuses exactly that form with `min(edge_rank)` as the aggregate: fixed-size spillable state per group, each statement its own single-blocking-operator plan.
- Runner seam: `run_fk_out_of_core` resolves `table_incoming_edges` and passes `parent_relations` (keyed by edge) to `decide_route`, `_stream_table` and `stream_table`; every downstream consumer takes `(incoming_edges, parent_relations)` and opens one joiner per edge with REMAP seeds from `edge.parent_columns`, the WARN/FAIL identity from `edge`, and `_fk_component_map` keyed by child column ("well-defined because the gate rejects multiple parents"). Substituting, per table, one REPRESENTATIVE edge (the first in order) per child tuple and a per-table relation mapping that binds the representative to the merged relation makes every consumer correct without modification: REMAP seeds come from the first parent (the oracle's `make_remap_fn(edges[0])`), the warning and error name the first parent, the component map is one joiner per child tuple, and the phase memory limits divide by the number of joiners actually opened.
- Established pattern: the polymorphic association resolved by a priority union (`UNION ALL` with a source rank, `min(rank)` per key, join back) is the standard SQL rendering of a COALESCE-in-declared-order lookup; DuckDB's grace/hybrid hash join and external aggregate execute it with spill. Cited in the new module's docstring.

## 4. Design

New module `execution/out_of_core/_multi_parent.py` (well under 600 LOC), two functions:

- `resolve_incoming(incoming_edges, parent_relations, *, temp_dir, memory_limit) -> tuple[RelationshipEdge, ...]`: group the table's incoming edges by `child_columns` preserving first appearance; a singleton group passes through unchanged; a multi-parent group yields its first edge as the representative, REBOUND in `parent_relations` to `merge_parent_relations(...)` of the members' relations in group order; the other members' entries are dropped and their relation files unlinked (an edge's relation serves one child, so nothing later reads them), which keeps the spill footprint at one relation per child tuple once the merge is done. The transient peak while merging is the staged `UNION ALL` copy (the members' sum) beside the members; `_spill_estimate` does not price that copy (recorded in section 8). Returns the representatives in original relative order.
- `merge_parent_relations(relations, *, temp_dir, memory_limit) -> ParentKeyRelation`: fail closed (`out_of_core_fk_key_dtype_unsupported`, identifiers only) unless every member's masked key columns carry identical Arrow types (read from the parquet footers); otherwise COPY the rank-tagged `UNION ALL` of the members to a staged parquet, COPY `min(__decoy_edge_rank) GROUP BY __decoy_fk_join_key` to a winners parquet, COPY the join-back to the merged relation parquet, unlink the scratch files, and return a `ParentKeyRelation` with the members' `masked_key_columns` and `max(max_sort_payload_row_bytes)`.

Runner wiring (`_runner.py`, a handful of lines at the top of the per-table loop; the test-only whole-child oracle helper `_remap_values` moves to `_batch_join.py`, the module whose per-batch mint it defines, and the runner's private `_column_seed` copy gives way to `_relation._column_seed`, so the runner shrinks and its census entry ratchets down): `table_incoming_edges = resolve_incoming(incoming[table_name], parent_relations, temp_dir=root / "relations" / table_name / "merged", memory_limit=<undivided build cap>)`; `decide_route` and the drivers keep taking `parent_relations`, which now binds the representative to the merged relation. The merged file lives under the `relations` subtree the runner already wipes. `_runner.py` was at its census (683); after the move it is below it and the entry ratchets down to the exact new size.

Gate (`_compat.py`): the `child_targets` count check and `out_of_core_multi_parent_child_unsupported` are deleted (pre-GA hard delete); the docstring's "no multiple-parent resolution yet" sentence goes with them. Nothing else in the gate changes: strategy allowlists, cycle, self-reference, composite rules all still apply per edge.

Comments and docstrings: `_fk_component_map` (both copies) say "well-defined because the runner resolves one representative edge per child tuple"; the `_runner.py` module docstring's "edges may overlap on a child column ... later edges overwriting" sentence is qualified (that contract is for DISTINCT edges that overlap; a multi-parent tuple is merged first-hit-wins before the join).

Fan-in sizing (`_max_concurrent_ooc_instances`, `_spill_estimate.max_per_table_edge_count`) keep counting graph edges, which over-counts the joiners a multi-parent table opens; conservative, recorded, not changed.

## 5. Probe-grounded acceptance ledger (oracle and SEQ probed at 0b8219f; the out-of-core column is the build target)

Shapes: `_d2b_shapes.multi_parent_shape` (P1.k and P2.k -> X.ref; child `[only1, only2, shared, nowhere]`, "shared" in both parents under different namespaces), plus the slice's own builders (three parents, composite multi-parent, int passthrough parents, heterogeneous masked types, reversed declaration, overlapping keys across batch boundaries).

| ID | Case | Oracle (probed) | Out-of-core (target) |
|---|---|---|---|
| M1 | PRESERVE: `X.ref = [h_P1(only1), h_P2(only2), h_P1(shared), "nowhere"]`; no warnings | ok | AGREE (all dimensions but metadata); `h_P1(shared) != h_P2(shared)` asserted so precedence is observable |
| M2 | REMAP: the orphan mints through P1's seed (`a498a8f5...` on the probe); the shared key is P1's token | ok | AGREE; evidence shows two `mask_batch` native entries (P1.k, P2.k) and one `fk_remap` Python entry for X.ref |
| M3 | WARN: one `orphan_fk` warning, provider `ns_x`, column `ref`, parent `P1`, `orphan_rows=1` | ok | AGREE; the warning object identical |
| M4 | FAIL: the oracle raises `1 orphan row(s) in X.('ref',) reference no parent key in P1.('k',) (orphan_policy=fail).` (the FIRST edge's parent) | raise | same code, nothing published (sink aborted); message byte-identity across routes is the Dcomp-OOC contract (one producer for the error) and lands with that slice |
| M5 | reversed declaration (edges P2 then P1 in the graph): the shared key resolves to P2's token | ok | AGREE with the oracle on the reversed graph; differs from M1 on row 2, which is the control |
| M6 | three parents, keys spread so every rank wins at least once, plus a key in all three | ok | AGREE; first rank wins |
| M7 | composite multi-parent: P1.(k,j) and P2.(k,j) -> C.(k,j), partial-null tuple, orphan under REMAP | ok (probed values) | AGREE |
| M8 | int64 passthrough parents, int child with a null and an orphan | ok, `int64` | AGREE, `int64` |
| M9 | batch sizes 1 and 2, sink-batch and sink-reorder, overlapping key on a batch boundary | ok | AGREE with the oracle; the two lanes equal on all nine dimensions |
| M10 | `native_threads` 4 vs 1 | n/a | identical output |
| M11 | heterogeneous masked types (P1 int64 passthrough, P2 float64 passthrough, int child) | ok (float column) | fails closed `out_of_core_fk_key_dtype_unsupported` before any output; the sanctioned reject, routed to SEQ by the caller |
| M12 | `orphan_policy_conflict` at graph build | raise | unchanged (retained regression from D2b and the unit suite) |
| M13 | the #253 decline pins | n/a | flipped: the gate admits the same-column multi-parent plan and `run_fk_out_of_core` is value- and strict-equal to the oracle on it |
| M14 | witness: both parents' key columns native-consumed; the merged relation masks nothing (no kernel observation attributed to it); companion hidden -> legacy-only | n/a | holds |

## 6. Declines (coded, caller-visible)

- D-1 heterogeneous masked key types across the parents of one child tuple: `out_of_core_fk_key_dtype_unsupported` at merge time (types only, no identifiers or values). The oracle emits a mixed column whose type is value-derived; a fixed schema cannot reproduce that byte-identically, so the route rejects rather than drifts. The static gate cannot see types, but the router's schema-aware hazard (`_pipeline_routing_fk_dtype.fk_out_of_core_dtype_hazard`) now compares the members' masked key types and diverts such a job to the sequential route under `auto` before any parent is written; only a direct `run_fk_out_of_core` caller or a forced `execution_mode="out_of_core"` reaches the merge-time rejection, and then nothing is published.
- D-2 everything the gate declined before stays declined for the same reasons (cycle, self-reference, composite scalar-child, composite chain, unsupported key strategies, hash key without namespace).

## 7. Tests (authored first; the admission tests must fail on the current gate, then pass)

- `tests/parity/test_phase_d_dmp_multi_parent_ooc.py`: M1 to M11 strict against the oracle on the resident path (on whatever companion the box has; the compiled kernel here), M9 on both sink lanes, M10, M14 with the stand-in companion and with the companion hidden, and the D-1 decline literal.
- `tests/unit/execution/test_out_of_core_relation.py`: the two #253 tests become admission tests (gate accepts; the runner's output is value-equal to the oracle), so the decline cannot silently come back either.
- `tests/unit/execution/test_out_of_core_multi_parent.py`: unit coverage of `merge_parent_relations` (precedence by rank, one row per key, identical-type guard, scratch files removed, width bound is the max) and `resolve_incoming` (singletons pass through untouched, order preserved, representative is the first edge).
- Retained regression: `test_out_of_core_fk_parity.py`, `test_out_of_core_route_seam_parity.py`, `test_phase_d_d2b_seq_shapes.py` (SEQ multi-parent), `test_multi_parent_fk.py`, D0, Dcomp-OOC, every sentry.

## 8. Open cells (honest hand-off)

- OOC REMAP minting stays Python (the D1a disposition), as recorded by Dcomp-OOC.
- Fan-in sizing counts graph edges rather than representatives: conservative over-count on multi-parent tables; a later tightening if a real schema hits the fan-in guard.
- Disk pricing: `_spill_estimate` does not price the merge's transient staged copy (the members' sum, deleted when the merge completes) nor credit the member files unlinked afterwards; `check_temp_disk_budget` still bounds the footprint at table boundaries. A real polymorphic schema with very large parents is the case to re-price.
- The oracle's mixed-type multi-parent column (D-1) is value-typed by pandas; whether that is the right reference for a fixed-schema route is an owner decision (same family as the D0 gaps).

## 9. Done when

- [ ] M1 to M14 pass as literals; the admission tests failed on the old gate first.
- [ ] `_multi_parent.py` under 600 LOC with the pattern citation; `_runner.py` census bump visible (or offset) and recorded in the census history.
- [ ] `out_of_core_multi_parent_child_unsupported` is gone from `src/` and its two pins are flipped; `orphan_policy_conflict` unchanged.
- [ ] Docs: CHANGELOG, `SEMANTIC_DIFFERENCES.md` (D-1), the program plan's multi-parent row annotated, `_fk_component_map` docstrings.
- [ ] Retained regression green; required CI green; regression-gate delta versus main is zero new failures; PR open, not merged by the agent.
