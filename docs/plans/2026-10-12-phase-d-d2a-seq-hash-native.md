# Phase D / D2a: SEQ-route parity contract + hash on the compiled kernel

Status: built
Shipped: merge PR #241 (262f031f)
quality gate. rev 1 NO-GO (4 HIGH/1 MEDIUM, no blocker); rev 2 folds all five, grounded on
Codex-verified SEQ routing/conversion probes.

Rules consulted: 00-universal.md, risk-and-exceptions.md, development-loop.md,
architecture.md, reuse-first.md, testing.md, documentation.md,
autonomous-operation.md. Engine CLAUDE.md (established methodology, 600/700 LOC cap,
log hygiene, snapshot-before-extraction), compatibility-contract.md.

Risk: R2. Moves masking compute on a bounded FK route to a compiled kernel with a
byte-parity promise and an RI invariant. Plan + independent review + Codex plan gate
before build.

## FRAME

**Problem.** The sequential (SEQ) FK route masks entirely in Python: `run_sequential`
(`_sequential.py:367` loop) calls `PandasExecutionAdapter._dispatch_mask_node`
(`_pandas_adapter.py:359`), which routes FK children to `_resolve_fk_node:387` and
scalar/payload columns to a pandas handler (`_strategies/_<op>.py`). SEQ is the
bounded route carrying every FK job too big for full-frame and declined by OOC, so
its Python masking is the bottleneck for large relational jobs. No SEQ operator is
native today.

**Definition of done (D2a), staged, test-only foundation FIRST.**
1. **(lands first, test-only)** A strict SEQ parity contract + a SEQ-specific
   native-execution witness (the D0-analogue). Extend
   `tests/unit/execution/test_sequential_eviction.py:62`'s `Table.equals` parity to
   the strict D0 comparator (schema incl. field metadata + nullability, row/col
   order, null-vs-NaN), with per-dimension controls and a frozen SEQ-FK-chain
   baseline. Freeze pre-change SEQ behavior (including the edge cases below) BEFORE
   any handler change.
2. The `hash` operator masks on the compiled companion kernel on the SEQ route for
   admitted Arrow types, reusing the D1a kernel, byte-identical to the Python hash
   handler except the pre-accepted NFC divergence, declined-with-reason + no silent
   fallback, qualified by SEQ's existing ingest guards (below).

**Boundaries / non-goals.** Only the SEQ route, only `hash`. SEQ's bounded
table-at-a-time pandas ORCHESTRATION is retained (program §5). No route-decision
change, no new FK shape (multi-parent/diamond = D2b; composite-scalar-child stays
rejected), no OOC/full-frame change, no removal or weakening of any existing SEQ
guard, no change to `resolve_fk_keys` precedence or the orphan/REMAP cascade.

**Reuse.** The compiled hash kernel + per-column decision exist from D1a
(`out_of_core/_hash_native.py`: `native_hash_array`, `decline_reason`,
`BoundHashKernel`; admitted set `native/_requirements.py:is_admitted_native_hash_type:110`).
Reused unchanged. Accepted NFC divergence carries over (same kernel), already
documented.

## FK execution model on SEQ (rev-1 HIGH-1: three distinct paths, not one)

Verified: a matched FK child does NOT re-derive its hash; it INHERITS the parent's
masked value from `parent_map` (`_fk_resolve.py:87`). REMAP reaches the parent hash
handler through a SEPARATE direct `handler.run(...)` at `_orphan.py:144`, not the
scalar seam. So "native hash on an FK key" means native on the PARENT key column;
the three paths are distinct and the guarantee/witness must treat them separately:

1. **Native parent/payload derivation** — a payload hash column, or an FK PARENT key
   column, is masked by the kernel at the scalar/handler seam. This is where the
   native kernel call happens. The parent_map is built pairing the source snapshot
   with the masked frame (`_pandas_adapter.py:635`); native parent hashing preserves
   that mapping as long as output assignment preserves row alignment (see conversion
   contract).
2. **Matched-child inheritance** — a matched child FK key takes the parent's already
   masked value via `resolve_fk_keys`; NO kernel call, NO re-derivation. The
   disposition reports these as INHERITED (with parent provenance), never as native
   kernel executions. `resolve_fk_keys` is unchanged.
3. **REMAP (orphan) derivation** — an orphan child key is minted by re-running the
   PARENT's handler via `make_remap_fn` (`_orphan.py:144`); this path is admitted for
   native SEPARATELY and the native decision is threaded into that `handler.run`
   call, with a REMAP input-type check. If not natively admitted there, it stays
   Python (declined-with-reason).

Decision transport: the per-table native decision for a column is threaded to the
scalar handler (via `run_with_when_gate`) AND to the REMAP `handler.run`, carrying
the parent decision's lifetime past the parent frame's eviction. The when-gate path
(`_when_gate.py`) passes the decision through explicitly.

## pandas <-> Arrow conversion contract (rev-1 HIGH-2)

SEQ handlers take/emit a pandas column; the kernel is Arrow in/out. The conversion
is specified, not "convert back":
- **Input:** reconstruct an Arrow array of the column's PINNED source type (captured
  at prep), not via pandas type inference. Inference is lossy here: it turns
  `large_string` -> `string` and an all-null string/bool column -> `null`, which a
  naive source-type-drift check would wrongly reject. Validation distinguishes these
  EXPECTED pandas representation changes from ACTUAL drift.
- **Output:** POSITIONAL assignment matching the existing handler's dtype behavior
  (today `_hash.py:59` assigns `masked.to_pylist()`). Index-aligned assignment is
  WRONG: a frame indexed `[1,3]` (a noncontiguous `when` selection,
  `_when_gate.py:250`) would misplace tokens and null the rest (probe-confirmed).
  Empty output must match the handler's dtype (list-assignment yields Arrow `double`
  today vs Series `null`) - the contract pins the handler-equivalent result.
- Codex's typed-conversion probes matched the kernel values for admitted inputs; the
  risk is purely the unqualified conversion, which this contract removes.

## Admission qualified by existing SEQ guards (rev-1 MEDIUM-5)

The guarantee is NOT "any admitted Arrow type." SEQ runs `reject_null_bearing_int`
before conversion (`_guards.py:59`), rejecting null-bearing integer hash columns
(including parent/payload keys); only FK children are exempt. D2a does NOT remove or
weaken this guard: a parent/payload nullable-integer hash column keeps its current
rejection (frozen + tested), and native admission applies only to inputs that clear
the existing ingest guards. Nullable child inheritance is tested separately.

## Witness: a SEQ-specific consumption proof (rev-1 HIGH-3)

The OOC witness requires identical Arrow buffer addresses (`_kernel_witness.py:181`);
that CANNOT hold on SEQ, which rebuilds output via `pa.Table.from_pandas`
(`_sequential.py:482`) giving new buffers (probe-confirmed). Replacing identity with
plain value-equality would let the prohibited equal-Python substitution pass. So
D2a defines a SEQ-specific proof:
- Trace the native result through the conversion + positional assignment to the
  emitted column, and check emitted VALUES + SCHEMA independently.
- A CAUSAL control: a test-only native stand-in produces a DISTINGUISHABLE output
  (not equal to the Python result); the witness must see that distinguishable output
  in the emitted table, proving the native result is what was consumed. A
  discarded-native + equal-Python substitution then fails the proof.
- Cover conditional (`when`) stitching and the inherited-FK-value case explicitly
  (an inherited child is proven inherited, not native).

## Atomicity scoped to the sink (rev-1 HIGH-4)

SEQ's abort guarantee depends on the sink. A genuine TRANSACTIONAL sink stages tables
during the loop and commits once (`_sequential.py:561`), aborting on a precommit
fault - there D2a's "a kernel fault aborts with nothing committed" holds and is
tested (fault after an earlier staged write). A CALLABLE sink has no-op commit/abort
(`_sequential.py:340`); earlier tables are already delivered
(`test_sequential_eviction.py:122`), so a later fault leaves them externally visible.
D2a does NOT change this; it documents callable partial-delivery and scopes the
atomicity claim to transactional sinks. "No half-native" is defined SEPARATELY from
transaction atomicity: it means no Python retry after a native selection for a column
(a native fault propagates, it does not silently fall back to Python mid-column).

## Observable behavior and failure modes

- Admitted payload/parent hash column clearing the ingest guards: native, byte-
  identical to the Python handler (modulo accepted NFC); disposition = native.
- Matched FK child: inherits the parent's masked value; disposition = inherited (not
  native). Orphan/REMAP: native if separately admitted, else declined Python.
- Non-admitted type, companion absent, or guard-rejected (e.g. parent nullable int):
  Python handler / existing rejection, disposition = declined-with-reason
  (value-free: strategy, column, Arrow type).
- Any non-hash operator: unchanged.
- Native fault / source-type drift on a pinned column: propagates; with a
  transactional sink the run aborts with nothing committed; never a silent Python
  retry.
- Log hygiene: no cell/key/value in logs or reason codes.

## Design notes (module placement)

`_pandas_adapter.py` ~697 LOC and `_sequential.py` ~669 against the 700 hard cap, so
the native decision + conversion + disposition go in a NEW module
`execution/_sequential_native.py` (mirroring `out_of_core/_hash_native.py`); the
scalar handler, the when-gate, and the REMAP call thread a typed decision object
into it. No destructive change to `_dispatch_mask_node`'s pinned inventory; if a new
dispatch call is unavoidable, declare it in the G3 pin. House style: typed decision
over a flag; one place for the admitted-type fact (reuse D1a's); comments explain why
(Arrow boundary; inherited vs native; sink-scoped atomicity). G4 pins full-frame
`.run`, not `run_sequential`; neither pin is weakened.

## Acceptance tests (authored before impl; test-only foundation lands first)

- **SEQ strict parity contract** (lands first, test-only): extend
  `test_sequential_eviction.py` to the D0 comparator (schema + field metadata +
  nullability, order, null-vs-NaN) with per-dimension controls + a frozen SEQ-FK
  baseline; includes the conversion edge cases below as pre-change freezes.
- **Conversion edge cases** (strict parity, each byte-identical to today's handler):
  empty input, all-null string/bool, non-default/noncontiguous index (a `when`
  selection like `[1,3]`), zero matches, `large_string`, integer-width boundaries.
- **SEQ native witness:** the value/schema + causal-distinguishable-output proof
  above; discarded-native/equal-Python substitution fails; declined column witnesses
  Python-only; inherited FK child witnesses inherited (not native).
- **Parity:** native vs Python hash on SEQ byte-identical for admitted types that
  clear the ingest guards (utf8/large_utf8/bool/int+uint widths), across shapes.
- **FK three paths:** native parent key + inherited matched child + REMAP orphan
  (native-admitted and declined variants); RI holds parent+child vs the full-frame
  oracle; parent_map keying still correct after native parent hashing.
- **Guards frozen:** a parent/payload nullable-integer hash column still rejected
  with the existing code (unchanged); nullable child inheritance works.
- **Atomicity:** transactional sink - fault after an earlier staged write aborts with
  nothing committed; callable sink - partial-delivery documented behavior preserved.
- **Unset/non-eligible byte-identity:** a SEQ run with no admitted hash column is
  byte-identical (whole-table digest) to today; companion absent matches Python.
- **Module size** under caps; new module under 600. **G3/G4 pins** updated if a call
  is added. **NFC** divergence re-asserted (D1a kernel) with the crate-version guard.
- **Perf gate** (predeclared, companion box): SEQ hash native p50 below the Python
  handler on a 5M-row FK workload within the route's memory envelope; identical
  digest.

## Known context for the gate

- The governing Phase D program plan (`...2026-10-10-phase-d-fk-routes-program.md`,
  §4 shape x route matrix, §7 slice order) is NOT on main; it lives on the unmerged
  `feat/phase-d-fk-routes` (e2401aad, Codex-approved). This plan cites D2a scope but
  stands alone. FLAG for a planner/Cam: land the program plan on main. Not a
  dependency for D2a.
- `resolve_fk_keys` is shared by SEQ + full-frame; unchanged here. D2b (multi-parent/
  diamond, self-FK drain-fold, OOC-declined strategies) and D2c (remaining operators
  + platform, gated after A3) are later slices.

## Docs to update

`docs/compatibility-contract.md` (SEQ runs hash natively, same admitted set + accepted
NFC + the guard qualification), CODEMAP pointer for the new module, `docs/strategies.md`
only if a user-visible note is needed (none expected; output byte-identical modulo
the documented NFC note).
