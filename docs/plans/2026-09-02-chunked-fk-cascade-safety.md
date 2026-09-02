# Chunked-FK admission soundness: restrict self-mask to the robust key strategies

Status: plan (authored 2026-09-02, Opus; plan-gate rounds 1-3 NO-GO, RESCOPED to
hash+passthrough per Cam's "hash-only, any dtype" decision, re-gate round 4 in
progress). Held target branch `feat/native-phase3`; merges with the Phase-4
bundle. CORRECTNESS fix, independent of the FK reorder-route work (Task 4/6/7)
and the deferred FK-4a fast path.

## 1. The bug

The public/manual self-mask entry `run_mask_pipeline_chunked`
(`execution/_chunked.py:347`) runs with an EMPTY relationship graph
(`_chunked.py:449`); its per-chunk adapter never FK-resolves a child column, it
self-masks the child by the child's declared strategy. It admits FK-child
configs through `gate_fk_child_edges` (`execution/_chunked_fk.py:212`) for ANY
`CHUNK_SAFE_STRATEGIES` parent key. That is byte-identical to the join oracle
ONLY when the child's self-mask reproduces the exact value the parent key was
masked to. Three independent families of config break that and make the route
emit a valid-looking WRONG value with no error (a byte-parity / contract
violation, not a raw-data leak):

- **A. Parent key not actually masked by its declared strategy, or masked
  conditionally.** (1) The parent key is itself a downstream FK child on the SAME
  column, so the oracle FK-RESOLVES it (`_pandas_adapter.py:366`) and an upstream
  cascade sets it (and the referencing child) to None; the manual route computes
  `F(X)` instead. (2) A parent-key `when` leaves some values raw
  (`_orphan.py:113-126`); the child resolves to raw, the route self-masks. (3) A
  child-key `when` is ignored by the oracle (`_pandas_adapter.py:366-379`) but
  honored by the empty-graph route (`_pandas_adapter.py:410`).
- **B. Strategy output depends on column/whole-column state.** fpe's tweak is the
  column name absent a shared group (`_fpe.py:130`); `date_shift`/`bucketize`
  detect format from whole-column samples; `top_code` caps on a whole-column
  percentile. Parent and child columns hold different data -> different output.
- **C. Strategy output depends on value REPRESENTATION or execution SUBSTRATE**
  (found by the plan-gate's executable probes, HEAD e556f9de). Coarse
  dtype-family equality collapses all timestamp types (`_chunked_fk.py:182`;
  runtime validates only the family, `_chunked_fk_dtype.py:133`), but `truncate`
  and `fpe` STRINGIFY the value (`kernel/_scalar.py:100`, `_fpe.py:142`), so the
  same instant in UTC vs a local tz yields different strings. `redact` with a
  non-scalar `redact_with` falls back to POSITIONAL `Series.where`
  (`_redact.py:33`), so reordered child keys diverge. `truncate` also diverges by
  ADAPTER: relationship jobs fall back to pandas while the empty-graph self-mask
  runs Polars-native (`_polars_adapter.py:141`), and pandas stringifies a bool as
  `"True"` while Polars casts to `"true"`.

The AUTOMATIC `run_pipeline` chunk route is unaffected: it rejects every
relationship-bearing job before this gate (`_planner.py:375-379`).

## 2. The fix

The root cause is that `gate_fk_child_edges` admits value-keyed strategies whose
resolved output is NOT a pure function of the logical key value alone: it varies
by cascade/`when` state (A), column/whole-column state (B), or value
representation / execution substrate (C). Only two admitted strategies are
provably a pure function of the logical value, robust across dtype, timezone,
and adapter:

- **hash**: canonicalizes the value and emits a hex digest via the shared kernel
  (`kernel/_scalar.py::hash_array`); representation- and adapter-stable.
- **passthrough**: a no-op identity (`value = value`); no stringification, no
  dtype/adapter dependence. (Explicitly admitted + byte-parity-tested as an FK
  key today, `test_chunked_fk.py:970`; a sound "keep raw keys" choice.)

### 2a. Restrict the FK-self-mask parent-strategy allowlist

Replace the existing "parent strategy in `CHUNK_SAFE_STRATEGIES`" condition with
"parent strategy in `FK_SELF_MASK_SAFE_STRATEGIES = {hash, passthrough}`". This
DROPS `fpe`, `redact`, `truncate` (families B/C representation, substrate, and
positional holes the plan-gate proved) and, already, `date_shift`, `bucketize`,
`top_code`, `text_redact`, `text_mask`. New code
`chunked_fk_parent_strategy_not_self_mask_safe`. Closing the class by a
minimal robust allowlist (rather than one reject code per hole across the
strategy x dtype x substrate matrix) also fails closed against any FUTURE
`CHUNK_SAFE_STRATEGIES` addition. Extending the allowlist later (e.g. `truncate`
restricted to string keys on a shared kernel) is a documented future refinement,
each gated on its own byte-parity proof.

> Deviation note (flagged for Cam): the decision was "hash-only". passthrough is
> included because it is a provably-sound no-op explicitly admitted and
> byte-parity-tested as an FK key (`test_chunked_fk.py:970`); strict hash-only
> would regress those green tests for no correctness gain. Drop passthrough on
> request and those tests flip to gate-kills.

### 2b. Predicates (families A + the composite override)

- **8. Parent KEY NODE is not itself an FK child.** Reject if the exact endpoint
  `(P, (pcol,))` appears as a `(child_table, child_columns)` in any edge
  (`relationships/_graph.py:112` keys FK override on the exact tuple; a
  table-wide check would wrongly reject the legit distinct-column self-FK
  `employees.id -> employees.manager_id`). New code `chunked_fk_parent_not_root`.
- **9 / 10. No effective `when` on the parent / child key column.** Reject when
  the column's `when` is a non-blank string, mirroring the compiler's
  normalization (`plan/_seed_envelope.py:245`, `isinstance(when_raw, str) and
  when_raw.strip()`). New codes `chunked_fk_parent_when_unsupported`,
  `chunked_fk_child_when_unsupported`.
- **11. Neither FK endpoint carries a `provider`.** Reject any non-null
  `provider` on `pcol` or `ccol` (a provider can route even a hash-declared key
  to composite bundle generation, `_runner.py:88` / `_pandas_adapter.py:380`;
  the gate lacks the registry, so reject any provider on an FK key
  conservatively). New code `chunked_fk_endpoint_not_scalar`.

No fpe-tweak predicate is needed: fpe is dropped by 2a.

## 3. Behavior contract

- ADMIT an FK-child edge only when every existing condition AND 2a AND
  predicates 8-11 hold. Any violation FAILS CLOSED (the gate raises the specific
  `PlanCompileError` code; it does not "reroute").
- The legitimate shape stays admitted and byte-identical: a root-key parent
  (its key node is not an FK child), a `{hash, passthrough}` strategy matched on
  both sides, matching namespace/provider_config/dtype, no effective `when`, no
  provider on either key.
- No change to the automatic route, the oracle, or any out-of-core module.

## 4. Explicit non-goal

With the allowlist restricted to `{hash, passthrough}`, neither admitted
strategy emits a per-row `RowError`, so the prior loud-vs-quarantine disposition
concern is moot. Representation/substrate parity for `fpe`/`truncate`/`redact`
FK self-masking is deferred (those strategies stay rejected); reviving any of
them is a separate slice with its own byte-parity proof across dtype and adapter.

## 5. Acceptance tests (authored before impl; no later contributor weakens them)

Gate-kill tests extend `test_chunked_fk_gate_kills.py`; byte-parity proofs
mirror `test_chunked_fk.py:170-268` (`TestByteParityWithRemapOrphans`).

1. **Allowlist gate-kill.** A parent FK key masked by each now-dropped strategy
   (`fpe`, `redact`, `truncate`, `date_shift`, `bucketize`, `top_code`,
   `text_redact`, `text_mask`) rejects with
   `chunked_fk_parent_strategy_not_self_mask_safe`. Include, as divergence
   witnesses documenting WHY: a `truncate` tz-timestamp (UTC vs local -> different
   string), a `redact` non-scalar `redact_with` with reordered keys, and an fpe
   default-column-tweak.
2. **Predicate gate-kills.** Same-key A->B->C chain ->
   `chunked_fk_parent_not_root`; parent/child non-blank `when` -> the two `when`
   codes; a `provider` on the parent (then child) FK key ->
   `chunked_fk_endpoint_not_scalar`. Each pins `.code` and `.path`.
3. **Exact-key-node correctness (8 not over-broad).** The distinct-column self-FK
   `employees.id -> employees.manager_id` stays ADMITTED and byte-identical;
   `A.id->B.a_id` + `B.id->C.b_id` stays admitted; only the SAME-key chain
   rejects.
4. **Multi-hop cascade divergence proof.** The 3-table oracle where A's key
   errors on X emits `C.c_fk = None`; assert the gate now blocks that config
   end-to-end.
5. **`when` normalization.** A blank/whitespace `when` on an FK key does NOT
   reject; a non-blank one does.
6. **Positive allowlist lock + no-regression.** hash and passthrough FK keys
   stay admitted and byte-identical (the existing hash `TestByteParityWithRemap
   Orphans` and `test_passthrough_parent_ns_none_admitted...` stay green); the
   full chunked-FK gate suites (`test_chunked_fk_gate_kills.py`,
   `test_chunked_fk.py`, `test_de10_chunked_fk_*`, `test_chunked_admitted_set.py`)
   stay green; update the admitted-set snapshot for the newly-rejected shapes.

VERIFY bar: coverage + mutation on the CHANGED lines of `gate_fk_child_edges`
(the allowlist narrowing + predicates 8-11 + reject codes), 0 unresolved
correctness-critical logic; ruff/format/mypy(3.12) clean.

## 6. Failure modes (each fails closed)

| Condition | Behavior | Test |
|---|---|---|
| parent strategy not in {hash, passthrough} | raise `chunked_fk_parent_strategy_not_self_mask_safe` | #1 |
| parent key node `(P, pcol)` is itself an FK child endpoint | raise `chunked_fk_parent_not_root` | #2, #4 |
| parent/child key column has an effective (non-blank) `when` | raise the matching `when` code | #2, #5 |
| either FK key endpoint declares a `provider` | raise `chunked_fk_endpoint_not_scalar` | #2 |
| root-key hash or passthrough parent, no when/provider | admit (unchanged) | #3, #6 |

## 7. Tasks

- [ ] A. Add `FK_SELF_MASK_SAFE_STRATEGIES = {hash, passthrough}` + narrow
  condition (a); add predicates 8-11 + reject codes to `gate_fk_child_edges`.
  Keep other existing conditions intact.
- [ ] B. Tests #1-#6.
- [ ] C. VERIFY -> dennis REVIEW -> Codex FINAL gate. HELD, push, no merge.

## 8. Risks / open questions for the plan-gate

- Confirm `{hash, passthrough}` is fully sound: hash is representation-/adapter-
  stable via the shared kernel, and passthrough is a pure identity with no
  stringification, so neither has a family-C hole. Name any residual.
- Confirm predicate 8's exact `(P, (pcol,))`-is-a-child-endpoint test matches the
  FK-override key for single-column edges, including self-referential and
  multi-parent edges.
- Confirm the newly-rejected shapes have no legitimate current use beyond the
  passthrough case handled above (the admitted-set snapshot update is the guard).
