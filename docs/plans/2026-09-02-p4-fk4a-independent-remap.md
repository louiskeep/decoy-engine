# P4 FK-4a: independent-remap fast path (no-join FK for hash/fpe + remap edges)

Status: plan (authored 2026-09-02 overnight, Opus; awaiting Codex PLAN-gate
before build). Held target branch `feat/native-phase3`; Phase 4 merges once at
the end. INDEPENDENT of the bounded-reorder route work (Task 4/6/7) -- shares no
code with it.

> Phase 4 backlog item, authored autonomously overnight per Cam's "keep going
> into the backlog" directive while the reorder-route premise decision waits.
> Design doc: `docs/plans/2026-08-31-part2-phase4-plan.md` ("P4-A ... FK-4a
> independent-remap default: no join for hash/fpe parent keys ... the default and
> cheapest path"). FRAME recon 2026-09-02 confirmed: the mechanism ALREADY EXISTS
> and is tested (the orphan-REMAP closure `_orphan.make_remap_fn`, the self-mask
> `run_mask_pipeline_chunked` route, and the fully-worked admission gate
> `_chunked_fk.gate_fk_child_edges`). FK-4a promotes that proven mechanism into
> the native/streaming route's admission; it is NOT new algorithm work.

## 1. Why this slice exists

For an FK edge whose PARENT key column is masked by a DETERMINISTIC per-value
transform (hash or FPE), the masked parent key is `F(seed, namespace/tweak,
value)` with no row/state/context dependence (verified: `_hash.py` keys on
`(mask_key, namespace)`; `fpe.py` is a keyed HMAC-Feistel bijection keyed on
`(seed, namespace)` + a join-group tweak, position-independent). So the child's
FK value can be transformed by the SAME `F` to reproduce the masked parent value
DIRECTLY -- no join to a materialized parent relation. This is the cheapest FK
path: no parent-key relation build, no dedup, no join, no reorder. Today
`execution/native/_dispatch.py::_table_in_declared_relationship` reroutes ANY
FK-touching table to the join-based oracle (`run_mask_pipeline_chunked`),
deferring all FK streaming; FK-4a lets the safe subset run natively.

## 2. Scope

IN (conservative first slice):
- A boolean predicate `edge_is_join_free` (extract from / wrap
  `_chunked_fk.gate_fk_child_edges`, which currently RAISES rather than returns a
  bool): true iff EVERY FK edge where `table` is the CHILD passes all existing
  admission conditions -- parent strategy in `CHUNK_SAFE_STRATEGIES` (hash/fpe),
  child declares the same value-keyed strategy, matching namespace (for
  namespace-requiring strategies), `orphan_policy == "remap"`, child
  provider_config byte-identical to the parent's, provable-equal parent/child FK
  key dtype family, single-column FK. Reuse the gate's exact logic (do not
  reimplement); keep every existing fail-closed error.
- Narrow `_table_in_declared_relationship` (or `_static_route_decision`) so a
  table is NOT rerouted to the oracle when it participates in FK ONLY as a
  join-free CHILD: it is a child of >= 1 edge, ALL its child-edges are
  `edge_is_join_free`, AND it is NOT a parent of any edge (pure child-role). When
  admitted natively, the child FK column is masked as an ordinary native
  keyed-kernel column applying the parent column's strategy `F` (the same `F` the
  orphan-REMAP path already runs via the parent node's plan slice), reproducing
  the masked parent value per row.
- Native parity-matrix coverage proving the admitted native FK output is
  byte-identical to the join oracle.

OUT (deferred, explicit non-goals -- stay on the oracle, fail-closed default):
- PARENT-side or MIXED-role tables (a table that is a parent of any edge). The
  parent-role data-flow (the oracle reading a natively-masked parent's relation)
  is a separate correctness question; a table that is any edge's parent keeps
  rerouting for now.
- Non-`remap` orphan policies (FAIL/PRESERVE/WARN need parent-membership
  knowledge = a join), composite (multi-column) FK, non-hash/fpe parent
  strategies, dtype-family-unprovable edges, namespace/config drift. All already
  fail-closed in the gate; FK-4a inherits that and does NOT widen it.
- Any change to the oracle, the join route, or the bounded-reorder route.

## 3. Behavior contract (what "correct" means)

- **Byte-parity.** For an admitted (join-free) FK child edge, the native
  independent-remap output (values, order, nulls, warnings) is IDENTICAL to the
  join-based oracle output for that edge. `F` is deterministic per value and both
  routes feed the same canonical input through the same `F` (same strategy +
  namespace/tweak + truncation/charset/checksum config, enforced by the gate), so
  equal source keys yield equal masked keys -- referential integrity preserved
  without a join.
- **Orphans.** Under `orphan_policy == "remap"` (the only admitted policy) the
  oracle ALSO mints the orphan's masked value via the parent strategy, so
  `F(child_key)` for a child key with no parent row is byte-identical to the
  oracle's REMAP. A null FK stays null on both routes (not treated as an orphan).
- **Fail-closed admission (never widen).** A table is admitted natively ONLY when
  `edge_is_join_free` holds for every child-edge AND it is not a parent. Any edge
  failing any gate condition (non-remap, dtype-unprovable, namespace/config
  mismatch, composite, non-hash/fpe) keeps the WHOLE table on the oracle. The
  default is reroute; native admission is the narrow exception. An
  unrecognized/ambiguous config reroutes, never admits.
- **No semantic change to the oracle path.** Non-admitted tables execute exactly
  as today.

## 4. Acceptance tests (authored before impl; no later contributor weakens them)

1. **Native-vs-oracle byte-parity for admitted edges** (`tests/parity/native/`):
   a hash-parent + hash-child+remap edge, and an fpe-parent + fpe-child+remap
   edge (matching namespace/join-group/config, provable dtype, single-column),
   each with: (a) all child FKs matching a parent, (b) orphan child FKs (no parent
   row), (c) null FKs, (d) duplicate child FKs. Assert the natively-admitted
   output == the join-oracle output, value+order+null+warning exact, via the
   existing FK parity fold (`test_out_of_core_fk_parity.py` helpers). Prove the
   native route ACTUALLY ran (route evidence), not a silent reroute.
2. **RI preserved end to end**: a parent masked (hash/fpe) + a child
   independent-remapped natively; assert every non-orphan child FK equals the
   masked parent key for its source row (the join would produce), and orphans
   equal `F(child_key)`.
3. **Gate-kill / fail-closed reroute** (extend `test_chunked_fk_gate_kills.py`
   patterns): for EACH non-admissible condition -- `orphan_policy != remap`,
   parent strategy not hash/fpe, child strategy mismatch, namespace mismatch,
   provider_config drift, unprovable/mismatched dtype family, composite FK, and
   the table-is-also-a-parent case -- assert the table REROUTES to the oracle
   (native admission does NOT fire) and the result still matches the oracle. A
   single failing condition on one edge keeps the whole table rerouted.
4. **Predicate unit tests**: `edge_is_join_free` returns True on a fully-admissible
   child table and False (with the same fail-closed reason as the raising gate) on
   each violating case. Extracting the bool must not change the gate's raising
   behavior where it is still used.
5. **No-regression**: the full FK parity suite (`test_out_of_core_fk_parity.py`),
   the chunked-FK gate suites (`test_chunked_fk_gate_kills.py`,
   `test_de10_chunked_fk_*`, `test_chunked_admitted_set.py`), and the native
   parity matrix stay green; the dispatch/route unit tests still route
   non-admitted tables to the oracle.

VERIFY bar: coverage + mutation on the CHANGED units (`edge_is_join_free` /
the narrowed dispatch predicate) 0 unresolved correctness-critical logic;
ruff/format/mypy(3.12) clean.

## 5. Failure modes (each fails closed to the oracle)

| Condition | Behavior | Test |
|---|---|---|
| orphan_policy != remap | reroute to oracle | #3 |
| parent strategy not hash/fpe (not join-free) | reroute | #3 |
| child strategy / namespace / config drift vs parent | reroute (existing gate errors) | #3 |
| parent/child FK dtype family unprovable or mismatched | reroute | #3 |
| composite (multi-column) FK | reroute | #3 |
| table is also a PARENT of any edge (deferred) | reroute | #3 |
| ambiguous / unrecognized relationship config | reroute (default) | #3 |

## 6. Tasks

- [ ] A. `edge_is_join_free` predicate (reuse `gate_fk_child_edges` logic; keep the
  raising gate intact for its existing callers). Unit tests #4.
- [ ] B. Narrow the dispatch reroute to admit pure-child join-free tables natively
  (child FK column = native keyed-kernel apply of the parent strategy). Route
  evidence marks the native FK admission.
- [ ] C. Parity + gate-kill + RI + no-regression tests (#1-#3, #5).
- [ ] D. VERIFY (parity + native matrix + mutation on changed units + lint/type)
  -> dennis REVIEW -> Codex FINAL gate. HELD, push, no merge.

## 7. Risks / open questions for the plan-gate

- Confirm that a natively-admitted child's FK column, masked by the parent's
  strategy `F`, uses EXACTLY the parent column's seed envelope (namespace/tweak),
  not the child column's own -- the orphan-REMAP path proves it is reachable via
  the parent node's plan slice, but the native keyed-kernel site must key on the
  parent's namespace, not the child's, or RI breaks. This is the single subtlety
  to nail in the build; the parity + RI tests (#1, #2) are the guard.
- Confirm the canonical input to `F` matches between the native child site and
  the oracle's parent masking (same `_canonicalize_source` / dtype handling), so
  "1" (int) vs "1.0" (float) cannot diverge; the dtype-family gate condition is
  the guard, the parity test the proof.
- Parent-side / mixed-role admission is deferred; confirm the pure-child scope is
  a coherent, shippable subset (a child-only leaf table with hash/fpe+remap FKs is
  the common case for the cheapest path).
