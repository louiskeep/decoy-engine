# Chunked-FK admission soundness: restrict self-mask to the robust key strategies

Status: plan GATE-APPROVED (2026-09-02, Opus). Codex plan-gate = GO with
implementation guidance now folded in: predicate 12 is a two-stage
declared(gate)+real(per-chunk runtime) check; safe set refined (scale>=0,
zoneinfo-resolvable tz, reject dict-wrappers before unwrap, preserve pa.null
carveout); many existing chunked-FK/DE-10 passthrough/redact fixtures must be
rebased to hash. Ready for the Sonnet build.
Prior plan-gate rounds narrowed the strategy allowlist; Cam then chose hash-only,
and the Polars-hash round-4 gate required the EXACT-dtype companion (predicate
12). This plan DEPENDS ON the Polars-hash fix (`2026-09-02-polars-hash-kernel-
parity.md`) landing first; its own Codex plan-gate runs after that. Held target
branch `feat/native-phase3`; merges with the Phase-4 bundle. CORRECTNESS fix,
independent of the FK reorder-route work (Task 4/6/7)
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
representation / execution substrate (C). Per Cam's decision, exactly ONE
strategy is admitted for FK self-masking:

- **hash**: canonicalizes the value and emits a hex digest via the shared kernel
  (`kernel/_scalar.py::hash_array`). After the companion Polars-hash fix
  (`2026-09-02-polars-hash-kernel-parity.md`) routes the Polars adapter through
  the same kernel, hash is cross-adapter byte-identical for the common key
  dtypes; predicate 12 (below) restricts FK keys to exactly that safe dtype set.
  `passthrough` is DROPPED too: it leaves the child value untouched, so a
  tz-aware timestamp key keeps the child's timezone representation while the
  oracle writes the parent's (arrow-unequal for the same instant); a "keep raw
  keys" FK is also a niche, and hash-only is the simplest sound surface.

### 2a. Restrict the FK-self-mask parent-strategy allowlist to hash

Replace the existing "parent strategy in `CHUNK_SAFE_STRATEGIES`" condition with
"parent strategy == `hash`". This DROPS `fpe`, `redact`, `truncate`, `passthrough`
and, already, `date_shift`, `bucketize`, `top_code`, `text_redact`, `text_mask`.
New code `chunked_fk_parent_strategy_not_self_mask_safe`. A minimal single-strategy
allowlist closes the strategy x representation x substrate hole class by
construction and fails closed against any future `CHUNK_SAFE_STRATEGIES`
addition. Extending it later (e.g. `passthrough` on string keys, or `truncate`
via a shared kernel) is a documented future refinement, each gated on its own
byte-parity proof. The existing passthrough-FK-key byte-parity tests
(`test_chunked_fk.py:970`, `:1022`) flip to gate-kills asserting the new reject
code.

### 2b. Predicates (families A, the composite override, and the dtype restriction)

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
- **12. FK key dtype is in the EXACT cross-adapter-safe set (TWO-STAGE).** hash
  is cross-adapter byte-identical only for the safe key dtypes (below). This must
  be an EXACT type check, NOT the existing coarse dtype-FAMILY comparison (which
  collapses `date32`/`date64`, timestamp variants, and decimal scales at
  `_chunked_fk.py:161` / `_chunked_fk_dtype.py:125`, admitting
  `date64`-as-`date32`, `decimal256`-as-`decimal128`, fixed-offset-as-IANA). Per
  the plan-gate, it CANNOT live entirely in `gate_fk_child_edges` (config-only,
  never sees data); implement it in TWO stages:
  1. **Declared check at the gate** (`gate_fk_child_edges`): exact-parse and
     validate BOTH endpoint declarations; an unsafe/unprovable declared type
     raises `PlanCompileError(code="chunked_fk_key_dtype_not_cross_adapter_safe")`.
     Use anchored parsers + Arrow-constructor validation for parameterized
     timestamp/decimal strings (`pa.type_for_alias` does not parse them on the
     pinned PyArrow 24). This does NOT replace the existing missing-dtype or
     parent/child compat comparison (`_chunked_fk.py:513`).
  2. **Real-type check at the existing per-chunk runtime guard**
     (`_chunked_fk_dtype.py:122`, real type = `chunk.schema.field(col).type`,
     runs at `_chunked.py:538` BEFORE `adapter.run` and thus before Polars
     ingestion at `polars/_conversion_boundary.py:43`): an unsafe real type
     raises `ExecutionError` with the SAME code. `fk_declared_dtypes_for_table`
     already gathers both endpoints (`_chunked_fk_dtype.py:67`); each endpoint's
     real type is checked when ITS table is streamed (a child-only invocation
     cannot see the remote parent's real schema -- per-table contract).
  **Exact safe set:** string, large_string, all signed/unsigned integer widths,
  bool, `date32` ONLY, `timestamp` (s/ms/us/ns) with a non-empty
  `zoneinfo.ZoneInfo`-resolvable tz, and `decimal32/64/128` with scale >= 0
  (scale 0 is hash-identical -- use `>= 0`, not `> 0`). REJECT dictionary
  wrappers BEFORE unwrapping (the family helper unwraps at
  `_chunked_fk_dtype.py:38`, unsuitable here), plus `date64`, `time64`,
  negative-scale / 256-bit decimal, fixed-offset (non-IANA) tz, tz-naive, and
  float. PRESERVE the existing `pa.null()` runtime carveout
  (`_chunked_fk_dtype.py:126`, vacuously safe -- all values null, pinned by
  `test_de10_chunked_fk_declared_dtype.py:282`). Together with the landed
  Polars-hash fix, this makes hash-only FK self-mask byte-parity-sound across
  both adapters for every ADMITTED key dtype.

No fpe-tweak predicate is needed: fpe is dropped by 2a.

## 3. Behavior contract

- ADMIT an FK-child edge only when every existing condition AND 2a AND
  predicates 8-11 hold. Any violation FAILS CLOSED (the gate raises the specific
  `PlanCompileError` code; it does not "reroute").
- The legitimate shape stays admitted and byte-identical: a root-key parent
  (its key node is not an FK child), `hash` on both sides, an FK key whose exact
  dtype is in the cross-adapter-safe set (predicate 12), matching
  namespace/provider_config, no effective `when`, no provider on either key.
- No change to the automatic route, the oracle, or any out-of-core module.

## 4. Explicit non-goal

With the allowlist restricted to `hash`, the admitted strategy emits no per-row
`RowError`, so the prior loud-vs-quarantine disposition concern is moot.
Representation/substrate parity for `fpe`/`truncate`/`redact`/`passthrough` FK
self-masking, and hash on the EXOTIC key dtypes (`date64`, `decimal256`,
negative-scale decimal, fixed-offset-tz, `time64`, dictionary-wrapped exotics),
are deferred: those strategies stay rejected, and those dtypes are rejected by
predicate 12 (the Polars adapter's ingestion corrupts/rejects them before any
handler runs -- fixing that needs adapter-ingestion surgery, out of scope per
Cam). Reviving any is a separate slice with its own cross-adapter byte-parity
proof.

## 5. Acceptance tests (authored before impl; no later contributor weakens them)

Gate-kill tests extend `test_chunked_fk_gate_kills.py`; byte-parity proofs
mirror `test_chunked_fk.py:170-268` (`TestByteParityWithRemapOrphans`).

1. **Allowlist gate-kill.** A parent FK key masked by each now-dropped strategy
   (`passthrough`, `fpe`, `redact`, `truncate`, `date_shift`, `bucketize`,
   `top_code`, `text_redact`, `text_mask`) rejects with
   `chunked_fk_parent_strategy_not_self_mask_safe`. Include, as divergence
   witnesses documenting WHY: a `truncate` tz-timestamp (UTC vs local -> different
   string), a `redact` non-scalar `redact_with` with reordered keys, an fpe
   default-column-tweak, and a `passthrough` tz-aware timestamp (child keeps its
   own tz representation, arrow-unequal to the parent's).
1b. **Dtype gate-kill, two-stage (predicate 12).** Split into declared vs real:
   - **Declared gate-kill**: a `hash` FK key DECLARED exotic (`date64`,
     `decimal256` / negative-scale decimal, fixed-offset non-IANA tz, `time64`,
     tz-naive, float, dictionary-wrapped) raises
     `chunked_fk_key_dtype_not_cross_adapter_safe` at the gate.
   - **Real-type runtime rejection**: a SAFE-declared key whose REAL runtime
     Arrow type is exotic (e.g. `date64`-real declared `date32`,
     `decimal256`-real declared `decimal128`, fixed-offset-real declared IANA)
     raises the same code at the per-chunk guard -- for the CHILD role, the
     PARENT role, and a LATER chunk (not just the first). Prove on BOTH adapters
     that rejection occurs BEFORE Polars ingestion.
   - **Carveout preserved**: an all-null `pa.null()` real column on a
     safe-declared key stays ADMITTED (vacuously safe).
   - **Admitted end-to-end**: parameterize a chunked-vs-oracle byte-parity proof
     across EVERY safe dtype class (string/large_string/int widths/bool/date32/
     IANA-tz-timestamp all units/decimal scale>=0) -- admitted + byte-identical.
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
6. **Positive lock + no-regression + REBASE.** A root-key hash FK edge on a
   safe-dtype key stays admitted and byte-identical (the existing hash
   `TestByteParityWithRemapOrphans` stays green). CRITICAL (plan-gate): MANY
   existing chunked-FK / DE-10 tests use `passthrough`/`redact` as the FK-key
   strategy (not only the two named passthrough tests -- e.g.
   `test_de10_chunked_fk_declared_dtype.py:149`). The hash-only allowlist now
   REJECTS those, so they must be REBASED to valid `hash` fixtures, or they never
   reach the behavior they were meant to cover; the pure passthrough-admission
   tests (`test_chunked_fk.py:970`, `:1022`) instead FLIP to gate-kills asserting
   the new strategy reject code. Audit every `test_chunked_fk*` / `test_de10_*` /
   `test_chunked_admitted_set` fixture: rebase strategy-incidental ones to hash,
   flip admission-of-a-dropped-strategy ones to gate-kills. The full chunked-FK
   gate suites stay green; update the admitted-set snapshot for the newly-rejected
   shapes.

VERIFY bar: coverage + mutation on the CHANGED lines of `gate_fk_child_edges`
(the hash-only narrowing + predicates 8-12 + reject codes), 0 unresolved
correctness-critical logic; ruff/format/mypy(3.12) clean. DEPENDS ON the
Polars-hash fix (`2026-09-02-polars-hash-kernel-parity.md`) landing first: hash
is only cross-adapter byte-identical for the safe dtypes AFTER it.

## 6. Failure modes (each fails closed)

| Condition | Behavior | Test |
|---|---|---|
| parent strategy != hash (incl passthrough/fpe/redact/truncate/...) | raise `chunked_fk_parent_strategy_not_self_mask_safe` | #1 |
| FK key dtype not in the exact cross-adapter-safe set (declared OR real) | raise `chunked_fk_key_dtype_not_cross_adapter_safe` | #1b |
| parent key node `(P, pcol)` is itself an FK child endpoint | raise `chunked_fk_parent_not_root` | #2, #4 |
| parent/child key column has an effective (non-blank) `when` | raise the matching `when` code | #2, #5 |
| either FK key endpoint declares a `provider` | raise `chunked_fk_endpoint_not_scalar` | #2 |
| root-key hash parent, safe-dtype key, no when/provider | admit (unchanged) | #3, #6 |

## 7. Tasks

- [ ] A. Narrow condition (a) to `parent strategy == hash`; add predicates 8-11
  + reject codes to `gate_fk_child_edges`. Predicate 12 is TWO-STAGE: (i) the
  exact DECLARED-type validator in `gate_fk_child_edges` (anchored parsers +
  Arrow-constructor validation), (ii) the exact REAL-type check extending the
  per-chunk runtime guard in `_chunked_fk_dtype.py` (reject dict-wrappers before
  unwrap, preserve the `pa.null()` carveout, raise the same code as
  `ExecutionError`). Keep the existing missing-dtype / parent-child compat
  conditions intact.
- [ ] B. Tests #1-#6 (incl the two-stage #1b + the passthrough/redact fixture
  REBASE audit in #6).
- [ ] C. VERIFY (gate suites + parity + mutation on the changed gate/runtime
  lines) -> dennis REVIEW -> Codex FINAL gate. HELD, push, no merge.

## 8. Risks / open questions for the plan-gate

- Confirm `{hash, passthrough}` is fully sound: hash is representation-/adapter-
  stable via the shared kernel, and passthrough is a pure identity with no
  stringification, so neither has a family-C hole. Name any residual.
- Confirm predicate 8's exact `(P, (pcol,))`-is-a-child-endpoint test matches the
  FK-override key for single-column edges, including self-referential and
  multi-parent edges.
- Confirm the newly-rejected shapes have no legitimate current use beyond the
  passthrough case handled above (the admitted-set snapshot update is the guard).
