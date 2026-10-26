Status: record

# Phase 2 route-coverage: FK key-mask seam inventory (slice 4 precondition)

Program: route-coverage program, phase 2 (`decoy-platform/docs/ROADMAP.md`, "Remote (ready)" row).
Baseline: `origin/main` at the B-6 vault write-seam merge (PR #322), 2026-10-26.

This record is the slice-4 precondition the handoff names: "Before touching anything,
INVENTORY every key-mask callsite the FK key path threads through ... The address-faker OOC
path must route through the same key-mask seam, not a parallel one." It is a read-only survey
of the current seam. No behaviour changed. It does not design the slices; the gated phase-2
plan does that, and this record is written while that plan is not present in the checkout (see
the blocker note at the end).

## The shared FK key-mask seam

One function applies a strategy to an out-of-core FK key column or key slice:

- `mask_column(values, seed, mask_key, *, column=None, ...)` at
  `src/decoy_engine/execution/out_of_core/_mask.py:106`.

Its docstring (lines 122 to 131) pins the FK-key contract: `column` carries the column name
for strategies whose output depends on it (fpe's per-column tweak). It is `None` only on the
FK parent-key / remap call paths, which the compat gate restricts to the column-independent
strategies (hash / redact / truncate / passthrough), so a `None` column never reaches a
strategy that needs it.

## Every FK key-path callsite that threads through the seam

Confirmed by `grep -rn "mask_column(" src/decoy_engine/execution/out_of_core/`:

| Callsite | Role | Call |
|---|---|---|
| `_stream_join.py:698` | child inherited-key remap (stream join) | `mask_column(pa.array(normalized, from_pandas=True), seed, self._job_seed)` |
| `_batch_join.py:346` | child inherited-key remap (batch join) | `mask_column(pa.array(normalized, from_pandas=True), seed, self._job_seed)` |
| `_batch_join.py:564` | parent-key remap (batch join) | `mask_column(pa.array(normalized, from_pandas=True), parent_seed, key)` |
| `_relation.py:134` | per-relation key masking loop | `mask_column(kept, seed, job_seed)` |

The handoff cites `_relation.py:118`; that line is the `for key_array, seed in zip(...)` loop
header whose body issues the `mask_column` call at line 134. Same callsite.

All four pass `column=None` (positional args only), so today every one is bound by the
column-independent restriction above.

Not FK-key (payload path, listed for completeness, out of slice-4 scope): `_mask.py:322` and
`_mask.py:417`, the in-module payload callers.

## The admission gate (the decision slices 4 to 6 change)

The FK key surface is decided before any `mask_column` call, in the compat gate:

- `_INITIAL_SUPPORTED_STRATEGIES = frozenset({"hash", "redact", "truncate", "passthrough"})`
  at `src/decoy_engine/execution/out_of_core/_compat.py:28`.
- Enforced at `_compat.py:459`: a parent FK key whose strategy is not in that set appends
  `OutOfCoreRejection("out_of_core_parent_strategy_unsupported", ...)`. That rejection reroutes
  the table off the out-of-core route (it is a decline-with-reason, not a raise); an explicit
  `execution_mode="out_of_core"` turns it into a fail-closed `ConfigError`.

Verified current admission (via `out_of_core_admission`, companion built, ABI
`decoy-native-abi-3`):

- fpe parent key -> `(False, "out_of_core_parent_strategy_unsupported")`. Slice 5 flips this
  to admitted, and must thread the `column` name through the four callsites above so fpe's
  per-column tweak has its input (the reason fpe is excluded today).
- categorical parent key -> `(False, "out_of_core_parent_strategy_unsupported")`. This must
  stay declined: categorical is not injective, so it must never be an FK key transform
  (standing constraint, guarded by the prep test below).

The matching sequential-route facts are already asserted by
`tests/parity/test_phase_d_fpe_key_route.py` (fpe key routes sequential, explicit OOC fails
closed). Phase 2 extends the out-of-core route, so the new parity is out-of-core vs the
full-frame oracle.

## Conclusion for slice 4 (address faker) and slice 5 (fpe key)

The seam is shared and single. The address-faker out-of-core path (slice 4) and the fpe FK
key out-of-core path (slice 5) both extend `mask_column` and the `_compat` admission set. They
must not introduce a parallel key-mask function. The faker index kernel already exists
(`decoy_engine_native.derive_index_batch`, `KeyedDerivationKernel` sibling); the fpe kernel
already exists (`fpe` crate compiled into the companion, reached via `native_fpe_array` /
`build_ff1_tweak`). No new crypto primitive is to be hand-rolled.

## Held pending the gated plan

These phase-2 items are design-dependent and are NOT implemented or contract-pinned here,
because the gated plan (with the folded Codex corrections) is not present in this checkout:

- Slice 4 concrete shape: faker as an FK key vs faker payload on the out-of-core route. There
  is no faker shape helper under `tests/parity/_d2*`, and the key-vs-payload decision is the
  plan's. The prep test file carries a skipped placeholder, not an asserted contract.
- Slice 6 cascade: key-column-only quarantine / cascade track. The bound (never a general
  column) and the mechanism are the plan's.
- Single-column resident escape hatch: prereq-gated on phase-1 B3/B4 being on main. Neither
  phase-1 nor its B3/B4 seams are on main or in any reachable branch as of this record.
- Out-of-core quarantine track for the above.

## Blocker (why this is a record, not a build)

The authoritative phase-2 plan `docs/plans/2026-10-26-substrate-route-coverage.md` and its real
Codex GO-WITH-FIXES review are absent from every branch, tag, and the full history of the six
in-scope repos. The file at `docs/plans/codex-phase2-plangate.md` is an unrelated NO-GO gate for
the 2026-08-28 native masking Phase 2, not this program. The ROADMAP row says the plan was
drafted and gated; it was never pushed into a repo this worker can reach. The crypto/FK design
the slices need lives in that plan, and the standing constraints forbid hand-rolling it, so the
build is held until the plan is provided.
