# Mutation grading: `execution/_chunked_fk.py` -- substrate/RI-guard tier

TQ substrate sweep (branch `tq/substrate-sweep`), FULL-TRIAGE grade by
`scripts/tq_mutate.py` with default survived-bucket re-adjudication (finding #16
RESOLVED). Every surviving mutant is individually adjudicated into one of THREE
buckets (definitions used across every ledger): PROVEN EQUIVALENT (no test can kill
it -- unreachable/invariant/byte-identical); ACCEPTED NON-CONTRACT (a test COULD
kill it, but only by pinning something the sweep does not treat as a machine
contract -- free-text message prose, or telemetry timing arithmetic -- so it is
deliberately left, a scoping decision); and RESIDUAL (killable AND touching a real
machine contract, deferred only because the kill fixture is expensive -- these are
tracked as owed work, e.g. `_sequential` 131/137/141). This module has 415 killed +
4 proven equivalent + 105 accepted non-contract + 0 residual. `_chunked_fk.py` is the chunked-route FK safety
layer: `gate_fk_child_edges` is the COMPILE-TIME admission gate that fails closed
(`PlanCompileError` with a coded reject) on any FK child edge not safe to run
chunk-by-chunk (conditions (a)-(f): chunk-safe strategies, declared keys,
namespace agreement, orphan_policy=remap, dtype-family agreement); `_dtype_family`
is the coarse RI-equivalence dtype classifier condition (f) keys on;
`reject_lossy_chunked_fk_passthrough` is the per-chunk runtime guard that fails
closed on a null-bearing passthrough FK key beyond `2**53`;
`fk_passthrough_columns_for_table` / `_col_index_from_config` are the config
readers. These GUARD RI on the chunked route (the RI-critical key IDENTITY itself
is in `_fk_keys`, graded to logic-100% in the crown jewels).

## Bar / framing

The RI-critical key identity lives in `_fk_keys` (100%, done); this module is the
route ADMISSION/GUARD layer. Measure-first substrate bar = max(baseline 62.98 +
15, 75) = **77.98%**; the grade clears it at 79.20% inclusive.

TAXONOMY (Codex batch gate, honest labeling). The 109 survivors split into TWO
kinds, NOT one "equivalent" bucket:
- **4 PROVEN EQUIVALENT** (no test can kill them): `_dtype_family` 36/37 (the
  `"string"` prefix is redundant with `"str"`, so no input matches via it alone);
  `reject_lossy` 17 (`skip_nulls=True` is pyarrow's `min_max` default) and 24
  (`or`->`and` where min/max are both-None or both-non-None together).
- **105 ACCEPTED NON-CONTRACT SURVIVORS** (a test COULD kill them, but the sweep
  deliberately does not): the message-PROSE mutants across the 12 coded rejects
  (gate 93 + reject_lossy 12). A full-message-equality assertion would kill each,
  but that is the brittle anti-pattern the sweep avoids (any legitimate message
  reword breaks it); the code / path / offending names ARE pinned, so the machine
  contract is covered. NOTE `PlanCompileError.message` is a public field callers
  may inspect, so this is a contract-SCOPING policy decision, not a claim that the
  prose is semantically inert.

So this module is NOT "100% killable / all-survivors-proven-equivalent" -- that
earlier framing was an overclaim. It IS 0-residual under the three-bucket
definition above (no killable-contract-touching deferred work), but only because
the 105 message-prose survivors are classed ACCEPTED NON-CONTRACT, not killed: it
is 79.20% inclusive, with 105 accepted-non-contract survivors + 4 proven
equivalents.

DECISION (RESOLVED 2026-07-28, Cam): accept the substrate-tier prose-equivalent
policy for this FK-GUARD layer; do NOT pin the 12 reject messages verbatim. The
100%-inclusive bar (pin the message prose too) is reserved for the RI/crypto
IDENTITY surface, which is `_fk_keys` (graded logic-100%); this module is the route
ADMISSION/GUARD, one tier out. Three reasons: (1) the machine contract of every
reject -- the `.code`, the `.path`, and the offending table/column/strategy NAMES
carried as data -- IS pinned, so a wrong verdict, wrong code, or wrong offending
name is caught; only the free-text explanatory sentence is unpinned. (2) A
full-message-equality assertion is the brittle anti-pattern the sweep avoids across
all 30+ graded modules -- any legitimate reword of a human-facing message would
break it, coupling the test suite to copy, not behavior. (3) Consistency: every
prior module (incl. this module's own `reject_lossy` tests) treats reject prose as
accepted non-contract; holding this one module to a stricter standard would be an
inconsistent, higher-maintenance bar for no behavioral coverage gain. `PlanCompileError.message`
remains a public field; this is a deliberate contract-SCOPING decision (exact prose
is out of the TQ contract), not a claim the prose is semantically inert.

## Numbers

**Killed 415/524 = 79.20% LOGIC inclusive (tool-native, 0 unresolved). 109
survivors: 4 proven equivalent + 105 accepted non-contract survivors (message
prose, killable only via brittle full-message-equality -- see Taxonomy).** Above
the 77.98% measure-first bar.

Baseline (3 existing FK test files only): 330/524 = 62.98%, 194 survived -- the
selection under-covered `gate_fk_child_edges`'s reject branches and `_dtype_family`'s
per-family branches. The full triage added two kill files
(`test_chunked_fk_helper_kills.py`, `test_chunked_fk_gate_kills.py`) and adjudicated
all 194: **85 additional kills**, 4 proven equivalent, 105 accepted non-contract.

| Function | Killed by the sweep | Proven equiv | Accepted non-contract (prose) |
|---|---|---|---|
| `gate_fk_child_edges` | 32 (code/path/name/separator + control-flow + dict-default) | 0 | 93 |
| `_dtype_family` | ~38 (every family branch + return) | 2 (`"string"` redundant prefix) | 0 |
| `reject_lossy_chunked_fk_passthrough` | ~8 (boundary + control-flow + name) | 2 (skip_nulls-default + or/and) | 12 |
| `fk_passthrough_columns_for_table` | 5 | 0 | 0 |
| `_col_index_from_config` | 2 | 0 | 0 |

## Kills

### `gate_fk_child_edges` (32) -- `test_chunked_fk_gate_kills.py`
One test per coded reject trips the branch with a raw-dict config and asserts the
machine fields: the exact `.code` and `.path` (kills every `path=None` /
`path_prefix=None` mutant), the offending table/column/strategy NAME(s) carried as
data in the message (kills every `message=None` mutant via `in None`), and the
`", "` separator where a message joins multiple names (the `'XX, XX'` mutant).
Behavioral kills: the three `continue`->`break` guards (a non-dict relationship /
non-dict child / non-matching child before a rejecting entry -- `break` abandons
the loop so the reject never fires), the composite-edge `or`->`and`, and the two
`_col_index.get(..., {})` -> `None` defaults (a missing FK column then raises a
typed `PlanCompileError` via the `{}` default, an `AttributeError` under `None`).

### `_dtype_family` (~38) -- `test_chunked_fk_helper_kills.py`
A parametrized case per family exercises exactly one prefix branch / return, with
a representative dtype that matches ONLY via that prefix (so a string-literal
XX-wrap / re-case makes the value fall through to the lowercased passthrough and
diverge). Non-ambiguous inputs where the input word equals the family literal
(`"boolean"` not `"bool"`, `"bytes32"` not `"bytes"`) keep the PREFIX load-bearing.
The decimal scale-keyed family (precision + width irrelevant to RI) and the bare
`decimal`/`numeric` unprovable sentinel are pinned per-value.

### `reject_lossy_chunked_fk_passthrough` (~8)
Boundary: a null-bearing passthrough key with max EXACTLY `2**53` (and min exactly
`-2**53`) is admitted, killing the `>`->`>=` / `<`->`<=` mutants; above the bound
raises with the offending `table.column` named (data). Control-flow: an ordered
LIST of columns (the loop accepts any iterable) forces a skippable column (absent /
non-integer / null-free / all-null) BEFORE the lossy one, so `continue`->`break`
is observable.

### `fk_passthrough_columns_for_table` (5) / `_col_index_from_config` (2)
The parent/child role `and`/`or` guards (killed with a name-COLLISION config so the
leaked column survives the `& passthrough_columns` intersection), the list-ordered
`continue`->`break` guards, and the `next(..., None)` default (a FK edge naming a
table absent from `config.tables` returns `set()`, not a `StopIteration`).

## Non-residual survivors (109): 4 proven equivalent + 105 accepted non-contract

- **`gate_fk_child_edges` message prose (93):** XX-wrap / re-case of the free-text
  explanatory sentences in each of the 12 coded rejects (`chunked_fk_orphan_policy_not_remap`
  60-68, `_composite_unsupported` 82-86, `_parent_strategy_not_safe` 112-114,
  `_parent_namespace_missing` 138-142, `_parent_namespace_mismatch` 154-157,
  `_child_namespace_missing` 171-176, `_child_namespace_mismatch` 186-190,
  `_child_strategy_missing` 204-208, `_child_config_mismatch` 237-245,
  `_child_key_dtype_unprovable` 266-286, `_child_key_dtype_mismatch` 298-318). Each
  carries no machine contract; the code, path, and offending names are pinned
  separately.
- **`_dtype_family` (2):** mut 36/37 wrap the `"string"` prefix, which is redundant
  with the earlier `"str"` prefix (`"string"` starts with `"str"`), so no input
  matches via `"string"` alone -- unkillable.
- **`reject_lossy` (14):** message prose (36-47, same policy as above); mut_17 drops
  `skip_nulls=True` which is pyarrow's `min_max` default (identical result); mut_24
  `col_min is None or col_max is None` -> `and`, equivalent because a column's min
  and max are both None (all-null) or both non-None together, so `or` == `and`.

## Candidate findings

None. No mutation exposed a wrong admission verdict, wrong reject code/path, wrong
offending name, wrong dtype-family classification, or a lossy-key pass-through the
guard does not already refuse.

## Regenerate

Repoint `[tool.mutmut]` `only_mutate` to `src/decoy_engine/execution/_chunked_fk.py`
and the test selection to the FIVE files: `tests/unit/execution/test_chunked_fk.py`,
`tests/unit/execution/test_de10_chunked_fk_passthrough.py`,
`tests/unit/execution/test_de10_chunked_fk_declared_dtype.py`,
`tests/unit/execution/test_chunked_fk_helper_kills.py`, and
`tests/unit/execution/test_chunked_fk_gate_kills.py`; then
`rm -rf mutants && python scripts/tq_mutate.py --run`. `source_paths` stays at the
package root.
