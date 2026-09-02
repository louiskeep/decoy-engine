# Chunked-FK admission soundness: close the manual self-mask byte-parity holes

Status: plan (authored 2026-09-02, Opus; plan-gate rounds 1-2 NO-GO remediated,
re-gate round 3 in progress). Held target branch `feat/native-phase3`; merges
with the Phase-4 bundle. CORRECTNESS fix, independent of the FK reorder-route
work (Task 4/6/7) and of the deferred FK-4a fast path.

## 1. The bug

The public/manual self-mask entry `run_mask_pipeline_chunked`
(`execution/_chunked.py:347`) runs with an EMPTY relationship graph
(`_chunked.py:449`), so its per-chunk adapter never FK-resolves a child column;
it self-masks the child by the child's declared strategy. It admits FK-child
configs through `gate_fk_child_edges` (`execution/_chunked_fk.py:212`) whenever
the parent key uses ANY `CHUNK_SAFE_STRATEGIES` strategy and the child declares
the same one. That is byte-identical to the join oracle ONLY when the child's
self-mask reproduces the exact value the parent key was masked to. Several
config shapes break that and make the route emit a valid-looking WRONG value
with no error (a byte-parity / advertised-contract violation, not a raw-data
leak). They fall into two families:

**Family A -- the parent key was not actually masked by its declared strategy,
or was masked conditionally:**
1. **Parent key is itself an FK child on the SAME column (multi-hop cascade).**
   In the oracle a parent key that is also a downstream FK child is FK-RESOLVED,
   not masked by its declared strategy (`_pandas_adapter.py:366`). If an upstream
   key errors, the cascade sets the parent key (and thus the referencing child)
   to None; the manual route masking the child alone computes `F(X)` instead.
2. **Parent-key `when`.** A when-gated parent key leaves some values raw; the
   oracle's parent map carries those raw entries (`_orphan.py:113-126`) and the
   child resolves to the RAW value; the chunked route self-masks it.
3. **Child-key `when`.** The oracle ignores the child FK column's own `when`
   (`_pandas_adapter.py:366-379`); the empty-graph route honors it
   (`_pandas_adapter.py:410`), leaving non-matching child rows raw.

**Family B -- the strategy's resolved output depends on state a single
self-masked child column cannot reproduce (so parent and child diverge even when
both are masked by the "same" strategy with equal config):**
4. **Composite / provider bundling.** Work-node kind derives from the resolved
   provider (`provider_is_composite`, `_runner.py:88`), not the declared
   strategy; a provider routes a key to bundle generation
   (`_pandas_adapter.py:380`) instead of scalar masking.
5. **FPE column-name tweak.** FPE's tweak is `(fpe_join_group or column_name)`
   (`_fpe.py:130-131`); absent a shared group, parent `id` and child `parent_id`
   use different tweaks -> different values.
6. **Whole-column format / percentile detection.** `date_shift` and `bucketize`
   detect their date format from WHOLE-COLUMN samples when no explicit
   `date_format` is pinned (`_date_shift.py:103`; the automatic route pins this
   in `_whole_column_state_rejections`, `_planner.py:427`); `top_code` caps on a
   WHOLE-COLUMN percentile. Parent and child columns hold different data, so the
   detected format / cap differs.

The AUTOMATIC `run_pipeline` chunk route is unaffected: it rejects every
relationship-bearing job before this gate (`_planner.py:375-379`).

## 2. The fix

Two structural changes to `gate_fk_child_edges` plus three cascade/`when`
predicates. Each is a pure read over `config["relationships"]` and the resolved
column entries the gate already handles (`col_index`, `_chunked_fk.py:275`).
Keep every existing condition and its fail-closed error unless noted. For an FK
edge (parent table P, parent key column `pcol`, child table `table`, child key
column `ccol`):

### 2a. Narrow the FK-self-mask parent-strategy allowlist (closes Family B)

Replace the existing condition "parent strategy in `CHUNK_SAFE_STRATEGIES`" with
"parent strategy in `FK_SELF_MASK_SAFE_STRATEGIES`", a curated subset proven to
be a pure function of `(value, matched-config)` with NO column-name /
whole-column / context dependence under the gate's existing config-equality
conditions:

    FK_SELF_MASK_SAFE_STRATEGIES = {hash, fpe, redact, truncate, passthrough}

This DROPS `date_shift`, `bucketize`, `top_code` (whole-column format/percentile
state, shape 6), and `text_redact` / `text_mask` (cross-column purity not proven
in this slice) from FK self-masking. None is used as an FK key in the current
test corpus (existing FK tests key only on hash/fpe/redact/truncate/passthrough),
and an FK key masked by a date/percentile/free-text strategy is outside the
supported surface. New code `chunked_fk_parent_strategy_not_self_mask_safe`.
Closing the class by an allowlist (rather than one reject code per unsafe
strategy) also fails closed against any FUTURE whole-column strategy added to
`CHUNK_SAFE_STRATEGIES`. (Extending the allowlist later, e.g. to `date_shift`
with a pinned `date_format`, is a documented future refinement, each gated on
its own byte-parity proof.)

### 2b. Predicates

- **8. Parent KEY NODE is not itself an FK child.** Reject if the exact endpoint
  `(P, (pcol,))` appears as a `(child_table, child_columns)` in any edge
  (`relationships/_graph.py:112` keys FK override on the exact tuple, so this is
  the EXACT complement of "pcol self-masks by its declared strategy"; a
  table-wide check would wrongly reject the legit distinct-column self-FK
  `employees.id -> employees.manager_id` and other-column-child parents). New
  code `chunked_fk_parent_not_root`. Closes shape 1.
- **9 / 10. No effective `when` on the parent / child key column.** Reject when
  the column's `when` is a non-blank string, mirroring the compiler's
  normalization (`plan/_seed_envelope.py:245`: `isinstance(when_raw, str) and
  when_raw.strip()`), so a blank/whitespace `when` does not over-reject. New
  codes `chunked_fk_parent_when_unsupported`, `chunked_fk_child_when_unsupported`.
  Close shapes 2-3.
- **11. Neither FK endpoint carries a `provider`.** Reject any non-null
  `provider` on `pcol` or `ccol` (composite recognition needs the resolved
  registry, `_runner.py:88`, which the gate does not hold; the conservative
  registry-free form rejects any provider on an FK key -- acceptable, an FK key
  with a generation provider is outside the supported surface). New code
  `chunked_fk_endpoint_not_scalar`. Closes shape 4.
- **12. FPE endpoints have a shared non-empty tweak group.** When the parent
  strategy is `fpe`, reject unless `pcol`'s `provider_config.fpe_join_group` is a
  NON-EMPTY string (a falsey/empty group resolves to the column-name tweak,
  `_fpe.py:130`). Equality of the group across parent and child is already
  guaranteed by the existing provider_config-equality condition, so this
  predicate only asserts the group is present and non-empty. New code
  `chunked_fk_fpe_tweak_mismatch`. Closes shape 5.

## 3. Behavior contract (what "correct" means)

- The manual chunked route ADMITS an FK-child edge only when every existing
  condition AND 2a AND predicates 8-12 hold. Any violation FAILS CLOSED: the
  gate raises the specific `PlanCompileError` code, so the route refuses the job
  rather than silently emitting a diverging value (the gate's existing
  raise-on-inadmissible contract; it does not "reroute").
- The legitimate shape stays admitted and byte-identical: a root-key parent
  (its key node is not an FK child), an `FK_SELF_MASK_SAFE_STRATEGIES` strategy
  matched on both sides, matching namespace/provider_config/dtype, no effective
  `when` on either key, no provider on either key, and (for fpe) a shared
  non-empty `fpe_join_group`.
- No change to the automatic route, the oracle, or any out-of-core module.

## 4. Explicit non-goal (do NOT close here)

A parent key that self-masks correctly but whose strategy can emit a per-row
`RowError` diverges in DISPOSITION when a referencing child row exists: the
oracle quarantine-removes the cascaded child row (`_pipeline_finalize.py:179`),
while the chunked route raises `RowErrorsFailedError` on any recorded row error
(`_chunked.py:599`). That is loud-vs-quarantine, not a silent wrong value, and
is the pre-existing documented "chunked has no quarantine machinery, fail closed
on any row error" limitation. With the 2a allowlist the only such strategy still
admitted is a strategy in the safe set that can row-error; `redact`, `truncate`,
`passthrough`, `hash` do not, and `fpe` raises `StrategyError` job-fatally
(`_fpe.py:150`), not via the row-error channel. So this non-goal is now largely
moot but is documented for completeness.

## 5. Acceptance tests (authored before impl; no later contributor weakens them)

Gate-kill tests extend `test_chunked_fk_gate_kills.py`; byte-parity / divergence
proofs mirror `test_chunked_fk.py:170-268` (`TestByteParityWithRemapOrphans`).

1. **Allowlist gate-kill (2a).** A parent FK key masked by each DROPPED strategy
   (`date_shift`, `bucketize`, `top_code`, `text_redact`, `text_mask`) rejects
   with `chunked_fk_parent_strategy_not_self_mask_safe`. A `date_shift` FK key
   with ambiguous dates is included as a divergence witness (parent/child detect
   different formats) to document WHY it is dropped.
2. **Predicate gate-kills.** Same-key A->B->C chain ->
   `chunked_fk_parent_not_root`; parent-key non-blank `when` /
   child-key non-blank `when` -> the two `when` codes; a `provider` on the parent
   (then child) FK key -> `chunked_fk_endpoint_not_scalar`; an fpe edge with an
   empty/absent `fpe_join_group` -> `chunked_fk_fpe_tweak_mismatch`. Each pins
   `.code` and `.path`.
3. **Exact-key-node correctness (8 not over-broad).** The distinct-column
   self-FK `employees.id -> employees.manager_id` stays ADMITTED and
   byte-identical; `A.id->B.a_id` + `B.id->C.b_id` stays admitted for its
   children; only the SAME-key chain rejects.
4. **Multi-hop cascade divergence proof.** The 3-table oracle where A's key
   errors on X emits `C.c_fk = None`; assert the gate now blocks that config
   end-to-end.
5. **FPE tweak parity.** An fpe parent+child with a shared non-empty
   `fpe_join_group` stays admitted and byte-identical (RI preserved); an
   empty/absent group rejects.
6. **`when` normalization.** A blank/whitespace `when` on an FK key does NOT
   reject; a non-blank one does.
7. **No-regression.** The existing single-hop byte-parity test
   (`TestByteParityWithRemapOrphans`, root `customers -> orders`, hash) and the
   full chunked-FK gate suites (`test_chunked_fk_gate_kills.py`,
   `test_chunked_fk.py`, `test_de10_chunked_fk_*`, `test_chunked_admitted_set.py`)
   stay green; update the admitted-set snapshot only for the newly-rejected
   shapes.

VERIFY bar: coverage + mutation on the CHANGED lines of `gate_fk_child_edges`
(the allowlist narrowing + predicates 8-12 + their reject codes) with 0
unresolved correctness-critical logic; ruff/format/mypy(3.12) clean.

## 6. Failure modes (each fails closed)

| Condition | Behavior | Test |
|---|---|---|
| parent strategy not in FK_SELF_MASK_SAFE_STRATEGIES (date_shift/bucketize/top_code/text_redact/text_mask) | raise `chunked_fk_parent_strategy_not_self_mask_safe` | #1 |
| parent key node `(P, pcol)` is itself an FK child endpoint | raise `chunked_fk_parent_not_root` | #2, #4 |
| parent/child key column has an effective (non-blank) `when` | raise the matching `when` code | #2, #6 |
| either FK key endpoint declares a `provider` | raise `chunked_fk_endpoint_not_scalar` | #2 |
| fpe edge without a non-empty `fpe_join_group` | raise `chunked_fk_fpe_tweak_mismatch` | #2, #5 |
| legit root-key parent, safe strategy, no when/provider, hash or fpe+shared-group | admit (unchanged) | #3, #5, #7 |

## 7. Tasks

- [ ] A. Add `FK_SELF_MASK_SAFE_STRATEGIES` + narrow condition (a); add
  predicates 8-12 + reject codes to `gate_fk_child_edges` (a helper for the exact
  `(table, cols)` child-endpoint membership; the `when` normalization mirror; the
  provider and fpe-group reads). Keep other existing conditions intact.
- [ ] B. Tests #1-#7.
- [ ] C. VERIFY (gate suites + parity + mutation on the changed predicates) ->
  dennis REVIEW -> Codex FINAL gate. HELD, push, no merge.

## 8. Risks / open questions for the plan-gate

- Confirm the allowlist `{hash, fpe, redact, truncate, passthrough}` is COMPLETE
  and SOUND: each member is a pure `(value, matched-config)` function with no
  column-name / whole-column / context dependence, and no member has a residual
  hole (e.g. a truncate/redact/passthrough config channel that varies by
  column). If any member still has a hole, name it.
- Confirm dropping `text_redact` / `text_mask` is acceptable conservatism (they
  may be provably pure, but are excluded fail-closed and unused as FK keys), not
  a regression of a real config.
- Confirm predicate 8's exact `(P, (pcol,))`-is-a-child-endpoint test matches
  the FK-override key for single-column edges, including self-referential and
  multi-parent edges.
