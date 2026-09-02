# Chunked-FK cascade safety: tighten manual self-mask admission

Status: plan (authored 2026-09-02, Opus; awaiting Codex plan-gate before build).
Held target branch `feat/native-phase3`; merges with the Phase-4 bundle. This is
a CORRECTNESS fix, independent of the FK reorder-route work (Task 4/6/7) and of
the deferred FK-4a fast path.

## 1. The bug

The public/manual self-mask entry `run_mask_pipeline_chunked`
(`execution/_chunked.py:347`) runs with an EMPTY relationship graph
(`_chunked.py:449`), so its per-chunk adapter never FK-resolves a child column;
it self-masks the child by the child's declared strategy. It admits FK-child
configs through `gate_fk_child_edges` (`execution/_chunked_fk.py:212`) whenever
the parent key uses a chunk-safe deterministic strategy and the child declares
the same one. That is byte-identical to the join oracle ONLY when the parent's
declared key strategy is the value that actually masks the parent key. Three
config shapes break that assumption and make the route emit a valid-looking
WRONG value with no error (a byte-parity / advertised-contract violation, not a
raw-data leak):

1. **Non-root parent (multi-hop cascade).** Chain A -> B -> C. In the oracle
   `b_id` is FK-resolved (its declared hash/fpe is overridden,
   `_pandas_adapter.py:366`). If A's key errors on value X, the oracle cascades
   `b_id=X` to None and that cascade RowError excludes X from C's parent map
   (`_pandas_adapter.py:305-312,628-645`, `_strategies/_orphan.py:127-138`), so
   `c_fk=X` cascades to None. The manual route masking C alone computes
   `hash(X)` and raises nothing (hash never row-errors; A's failing strategy
   never runs in C's chunk job).
2. **Parent-key `when`.** A when-gated parent key leaves some parent values raw;
   the oracle's parent map carries those raw identity entries and the child
   resolves to the RAW value (`_orphan.py:113-126`). The chunked route
   self-masks the child unconditionally -> `F(X)` instead of raw `X`.
3. **Child-key `when`.** The oracle FK-resolves the child column and never
   consults the child's own `when` (`_pandas_adapter.py:366-379`). The
   empty-graph route treats the child as a scalar and DOES honor its `when`
   (`_pandas_adapter.py:410`), leaving non-matching child rows raw. Divergence,
   no error.

The AUTOMATIC `run_pipeline` chunk route is unaffected: it rejects every
relationship-bearing job before reaching this gate (`_planner.py:375-379`).

## 2. The fix

Add three admission predicates to `gate_fk_child_edges`, each a pure read over
`config["relationships"]` and the `col_index` the gate already builds
(`_chunked_fk.py:275`). Keep every existing condition and its fail-closed error
unchanged. For an FK edge (parent table P, parent key column `pcol`, child table
`table`, child key column `ccol`):

- **8. Parent is a root.** Reject if P appears as any `children[].table` in
  `config["relationships"]` (P is itself an FK child). New code
  `chunked_fk_parent_not_root`. Closes shape 1.
- **9. No parent-key `when`.** Reject if `pcol`'s column entry has a non-None
  `when`. New code `chunked_fk_parent_when_unsupported`. Closes shape 2.
- **10. No child-key `when`.** Reject if `ccol`'s column entry has a non-None
  `when`. New code `chunked_fk_child_when_unsupported`. Closes shape 3.

Predicate 8 is load-bearing. 9 and 10 are cheap and required for completeness.

## 3. Behavior contract (what "correct" means)

- The manual chunked route ADMITS an FK-child edge only when every existing
  condition AND predicates 8/9/10 hold. Any violation FAILS CLOSED: the gate
  raises the specific new `PlanCompileError` code, so the route refuses the job
  rather than silently emitting a diverging value. This matches the gate's
  existing raise-on-inadmissible contract (it does not "reroute"; the public
  entry point fails closed).
- The legitimate single-hop shape (root parent, chunk-safe matching strategy,
  matching namespace/provider_config/dtype, no `when`, `orphan_policy==remap`)
  is UNCHANGED: it still admits and stays byte-identical to the oracle.
- No change to the automatic route, the oracle, or any out-of-core module.

## 4. Explicit non-goal (do NOT close here)

A ROOT parent whose key strategy can itself row-error (fpe/date_shift/bucketize)
still diverges in DISPOSITION when a referencing child row exists: the oracle
quarantine-removes the cascaded child row, while the chunked route raises
`RowErrorsFailedError` (`_chunked.py:599-602`). That is loud-vs-quarantine, not
a silent wrong value, and is the pre-existing documented "chunked has no
quarantine machinery, fail closed on any row error" limitation. Out of scope.

## 5. Acceptance tests (authored before impl; no later contributor weakens them)

1. **Gate-kill, per new code** (extend `test_chunked_fk_gate_kills.py`): a
   3-table A->B->C config asserts `gate_fk_child_edges(config, table="C")`
   raises `chunked_fk_parent_not_root` (B is non-root); a parent-key `when`
   asserts `chunked_fk_parent_when_unsupported`; a child-key `when` asserts
   `chunked_fk_child_when_unsupported`. Each pins `.code` and `.path`.
2. **Multi-hop cascade divergence proof** (mirror
   `test_chunked_fk.py:170-268`): build the 3-table oracle where A's key errors
   on value X under a quarantine-covering config; show the oracle emits
   `C.c_fk = None` for the X-referencing row (the value the empty-graph route
   would have hashed); assert the gate now blocks that config end-to-end via
   `run_mask_pipeline_chunked(..., table="C")`.
3. **No-regression**: the existing single-hop byte-parity test
   (`test_chunked_fk.py` `TestByteParityWithRemapOrphans`, root `customers ->
   orders`, no `when`) stays admitted and byte-identical; the full chunked-FK
   gate suites (`test_chunked_fk_gate_kills.py`, `test_chunked_fk.py`,
   `test_de10_chunked_fk_*`, `test_chunked_admitted_set.py`) stay green.
4. **Predicate unit coverage**: each new predicate rejects its violating config
   and leaves a compliant config admitted; the admitted-set snapshot is updated
   only for the newly-rejected shapes.

VERIFY bar: coverage + mutation on the CHANGED lines of `gate_fk_child_edges`
(the three predicates + their reject codes) with 0 unresolved
correctness-critical logic; ruff/format/mypy(3.12) clean.

## 6. Failure modes (each fails closed)

| Condition | Behavior | Test |
|---|---|---|
| parent table is itself an FK child (non-root) | raise `chunked_fk_parent_not_root` | #1, #2 |
| parent key column has a `when` | raise `chunked_fk_parent_when_unsupported` | #1 |
| child key column has a `when` | raise `chunked_fk_child_when_unsupported` | #1 |
| legitimate single-hop root parent, no `when` | admit (unchanged) | #3 |

## 7. Tasks

- [ ] A. Add predicates 8/9/10 + three reject codes to `gate_fk_child_edges`
  (reuse the existing `col_index`; a small helper to test "table is a child of
  any edge" over `config["relationships"]`). Keep existing conditions intact.
- [ ] B. Tests #1-#4.
- [ ] C. VERIFY (gate suites + parity + mutation on the changed predicate) ->
  dennis REVIEW -> Codex FINAL gate. HELD, push, no merge.

## 8. Risks / open questions for the plan-gate

- Confirm predicates 8/9/10 are COMPLETE for the empty-graph route: is there any
  other config shape where the child self-mask diverges from the FK-resolved
  oracle that these three (plus the existing conditions) do not cover? The FRAME
  found (c) multi-parent and (d) dtype/canonicalization already closed by
  existing conditions; confirm no fourth silent hole.
- Confirm `col.get("when")` is the authoritative per-column `when` the runtime
  honors (`plan/_compile.py:621`), so predicate 9/10 keys on the same field the
  oracle/route branch on.
- Confirm predicate 8's "P appears as a `children[].table`" is the exact
  complement of "P is a root", including self-referential and multi-parent
  edges.
