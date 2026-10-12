# Phase D / D2a: SEQ-route parity contract + hash on the compiled kernel

Status: plan (rev 1, pre-gate).

Rules consulted: 00-universal.md, risk-and-exceptions.md, development-loop.md,
architecture.md, reuse-first.md, testing.md, documentation.md,
autonomous-operation.md. Engine CLAUDE.md (established methodology, 600/700 LOC
module cap, log hygiene, snapshot-before-extraction), compatibility-contract.md.

Risk: R2. Moves masking compute on a bounded FK route to a compiled kernel with a
byte-parity promise and an RI invariant. Plan on disk + independent plan review +
Codex plan gate before build; observable behavior, failure modes, and acceptance
tests defined here first.

## FRAME

**Problem.** The sequential (SEQ) FK route masks entirely in Python today: per
table, `_sequential.py::run_sequential` (`:367` loop) calls
`PandasExecutionAdapter._dispatch_mask_node` (`_pandas_adapter.py:359`), whose
scalar branch (`:414-433`) runs each operator's pandas handler
(`_strategies/_<op>.py`). SEQ is the bounded route that carries every FK job too
big for full-frame and declined or unsupported by the out-of-core (OOC) route, so
its masking is a pure-Python bottleneck for exactly the large relational jobs Phase
D targets. No SEQ operator is native today.

**Definition of done (D2a).** Two things, staged:
1. A SEQ per-executor PARITY CONTRACT + native-execution WITNESS (the D0-analogue
   for SEQ), so any SEQ native kernel is graded against strict parity and proven
   consumed. SEQ already has an ordered + schema `Table.equals` parity test
   (`tests/unit/execution/test_sequential_eviction.py:62`) but WITHOUT metadata
   checking; this extends it to the strict contract (schema incl. field metadata +
   nullability, row/col order, null-vs-NaN) that D0 pinned for OOC.
2. The `hash` operator (payload AND FK-key hash columns) masks on the compiled
   companion kernel on the SEQ route for parity-eligible admitted Arrow types,
   reusing the D1a kernel; every other column and every non-eligible hash column
   keeps the Python handler with a caller-visible declined-with-reason, no silent
   fallback.

**Boundaries / non-goals.** Only the SEQ route and only the `hash` operator (the
other operators are D2b/D2c). SEQ's bounded table-at-a-time pandas ORCHESTRATION is
RETAINED and is explicitly not a violation (program §5: `to_pandas_fk_safe` ingest,
the `frames` hold, `pa.Table.from_pandas` out all stay); making SEQ fully
Arrow-native is out of Phase D. No route-decision change. No new FK shape admitted
(multi-parent/diamond is D2b; composite-scalar-child stays rejected). No OOC or
full-frame change.

**Reuse.** The compiled hash kernel and the per-column decision already exist from
D1a: `out_of_core/_hash_native.py` (`native_hash_array`, `decline_reason`,
`BoundHashKernel`) over `execution/native/_crypto_ext.KeyedDerivationKernel.derive_batch`
and the admitted-type set `is_admitted_native_hash_type`
(`native/_requirements.py:110`). D2a reuses these unchanged; it adds only the SEQ
seam that calls them and the SEQ parity/witness infra. The accepted NFC
Unicode-version divergence from D1a carries over verbatim (same kernel), already
documented.

## Pinned guarantee and non-goals

**Guarantee.** On the SEQ route, a `hash` column whose source Arrow type is in the
D1a admitted set is masked by the compiled kernel and is byte-identical to the
current Python hash handler except the pre-accepted NFC divergence; a non-admitted
type, or any other operator, keeps the Python handler and is reported declined with
a value-free reason on the caller-visible disposition. Output for every other
column and every non-hash run is byte-identical to today (proven by the extended
SEQ parity test). A hash FK key preserves RI: parent and child derive the same
masked value because they run the same kernel on the same source value (the
existing SEQ parent-map / `resolve_fk_keys` path is unchanged).

**Non-goals.** No silent fallback (a kernel fault or type drift aborts the run with
no commit, as D1a). No change to `resolve_fk_keys` precedence, the orphan/REMAP
cascade, eviction, or the sink. No new dispatch behavior for non-hash operators.

## Established solutions

- Native-OOC mechanism (D1a, proven + merged): per-column decision pinned at
  table-prep, compiled kernel loaded once and threaded, declined-with-reason on the
  caller-visible disposition, insertion-boundary witness, abort-on-drift/fault with
  no commit. D2a ports this mechanism to the SEQ seam; it does not invent a new one.
- Strict parity contract (D0, `docs/plans/2026-10-11-phase-d-d0-parity-contract-witness.md`):
  the exact per-field comparator D2a's SEQ parity test adopts.
- `cryptography`/HKDF-SHA256 keyed hash via the existing kernel; no new crypto.

## Design notes (module placement is load-bearing)

`_pandas_adapter.py` is 697 LOC and `_sequential.py` 669 LOC against a 700 hard cap
(engine CLAUDE.md; `tests/sentry/test_module_size.py`). D2a must NOT inflate either.
The SEQ native decision + the Arrow boundary + the witness-observable seam go in a
NEW module `execution/_sequential_native.py` (mirroring `out_of_core/_hash_native.py`):
it exposes a `seq_hash_array(column, decision, ...) -> masked` that converts the
handler's pandas column to Arrow, calls the shared `native_hash_array`, and converts
back, plus the per-column `decline_reason` (delegating to the D1a resolver) and the
disposition entry. The scalar seam calls into it for `hash` only; all other
strategies are untouched.

Seam choice: the native-or-Python decision is made ONCE per table at prep (which
hash columns are admitted), not per row, and consulted at the scalar branch. To
avoid changing `_dispatch_mask_node`'s pinned call inventory destructively, the hash
handler (`_strategies/_hash.py::run`) gains an optional pre-resolved native decision
(threaded from the SEQ executor), exactly as D1c threaded `native`/`preserve`
through the Group-(c) seam without disturbing unrelated dispatch. House style: typed
decision object over a flag argument; one place for the admitted-type fact (reuse
D1a's); comments explain why (Arrow boundary needed because SEQ handlers are pandas).

## Observable behavior and failure modes

- Admitted hash column on SEQ: masked natively, byte-identical to the Python handler
  (modulo accepted NFC), disposition shows the native backend.
- Non-admitted-type hash column / companion absent: Python handler, disposition
  shows declined-with-reason (value-free: strategy, column, Arrow type only).
- Any non-hash operator: unchanged Python handler, no disposition entry.
- Type drift on a pinned native column, or a mid-run kernel fault: abort the run,
  sink aborts, nothing committed (never half-native), as D1a.
- Hash FK key: native on both parent and child; RI holds by the shared-kernel
  determinism; the existing parent-map / `resolve_fk_keys` / REMAP path is unchanged
  and still drives child inheritance.
- Log hygiene: no cell/key/value in logs or reason codes.

## Acceptance tests (authored before impl; not weakened later)

- **SEQ parity contract (D0-analogue):** extend `test_sequential_eviction.py`'s
  `Table.equals` parity to the strict comparator (schema + field metadata +
  nullability, row/col order, null-vs-NaN) with per-dimension controls; a frozen
  baseline over a SEQ FK chain. This is the test-only foundation and lands first
  (red-before-green where meaningful).
- **SEQ native witness:** a SEQ-bound witness (the OOC `_kernel_witness.py` spies OOC
  modules only; build the SEQ analogue spying the SEQ seam + an insertion check at
  the `pa.Table.from_pandas` boundary `_sequential.py:482`) proving the native hash
  result is the array the masked table carries; a discarded-native + equal-Python
  substitution fails; a declined column witnesses Python-only.
- **Parity:** native vs Python hash on SEQ byte-identical (admitted types: utf8,
  large_utf8, bool, int/uint widths) across batch/table shapes; non-admitted type
  declines and matches the Python handler.
- **Unset/non-eligible byte-identity:** a SEQ run with no admitted hash column is
  byte-identical to today (whole-table digest); a run with hash present but companion
  absent is byte-identical to the Python handler.
- **FK:** a hash FK key on SEQ preserves RI parent+child (normal + orphan/REMAP);
  compare against the full-frame oracle.
- **Abort:** type drift and a mid-run kernel fault abort with no sink commit.
- **G3 dispatch pin:** if the scalar seam gains a call, update
  `test_column_access_surfaces.py` `_PINNED_DISPATCH_CALLS` (G3) and/or
  `_PINNED_RUN_CALLS` (G4) to declare it; the threading approach aims to avoid a new
  dispatch call, but the pin is checked either way.
- **Module size:** `_pandas_adapter.py` and `_sequential.py` stay under 700 (census
  if a touched file crosses 600); the new `_sequential_native.py` under 600.
- **NFC accepted divergence:** carried over from D1a (same kernel), re-asserted on
  the SEQ route with the crate-version guard.
- **Perf gate:** SEQ hash native p50 below the Python handler on a 5M-row
  single-table-plus-FK workload, within the route's memory envelope; byte-identical
  digest. (Predeclared; run on a companion box.)

## Known context for the gate

- The governing Phase D program plan (`docs/plans/2026-10-10-phase-d-fk-routes-program.md`,
  the §4 shape x route matrix and §7 slice order) is NOT on main; it lives on the
  unmerged `feat/phase-d-fk-routes` branch (e2401aad, Codex-approved). This plan
  cites its D2a scope but stands on its own. FLAG: the program plan should land on
  main; a planner/Cam should confirm its intended landing. D2a does not depend on it
  being merged.
- SEQ `_fk_resolve.resolve_fk_keys` is shared by SEQ and full-frame only; D2a must
  not alter it.
- `D2b` (multi-parent/diamond, self-FK drain-fold, OOC-declined strategies) and
  `D2c` (remaining operators + platform sequential activation, gated after A3) are
  separate later slices.

## Docs to update

`docs/compatibility-contract.md` (SEQ route now runs hash natively, same admitted
set + accepted NFC as D1a), CODEMAP if the new module warrants a pointer,
`docs/strategies.md` only if a user-visible behavior note is needed (none expected;
output is byte-identical modulo the already-documented NFC note).
