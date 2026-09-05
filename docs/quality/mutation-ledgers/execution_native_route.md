# Mutation grading: `_native_route.py` + `_native_route_exec.py` (native-route production seam)

Scope: the two modules behind the single-pass streaming native lane --
`_native_route.py` (`static_candidacy`, `peek_and_admit`, the ledger/report
dataclasses) and `_native_route_exec.py` (`try_native_route` and its private
helpers: `_resolve_truncate_keep`, `_resolve_strategy_cfg`, `_schema_drift_reason`,
`_mask_one_batch`, `_validate_ledger`, `_masked_batches`, `_execution_envelope`,
`_run_native_streaming`). Graded with `mutmut` (`only_mutate` scoped to the two
files, selection = the two covering test files below), readjudicated per-mutant.

Covering tests before this pass: `tests/parity/native/test_native_route_production_seam.py`
and `tests/unit/execution/test_native_route_transactional_failures.py`. Both drive
the lane through the production `run_pipeline` entry with happy-path configs, which
is why most of the survivors below were config/default/arithmetic edges those
configs never varied.

## Numbers

**Before: 502/674 killed = 74.48%.** After adding
`tests/unit/execution/test_native_route_units.py` (direct unit tests of the private
helpers) plus one test extending `test_native_route_transactional_failures.py`:
**641/674 killed = 95.10%.** 139 of the 172 original survivors are now killed by an
assertion on the exact behavior the mutant broke; the remaining 33 are judged
genuinely equivalent below, grouped by class, with no killable-and-undocumented
residual among them (each was individually reproduced with `mutmut run
<mutant-name>` against the updated test set before being accepted as equivalent,
not left un-investigated because it was originally on the survivor list).

## Equivalent survivors (33), by class

**Boolean-default collapse under `bool()` (2)** -- `static_candidacy`
`x_static_candidacy__mutmut_101` (`col.get("vault", None)`),
`__mutmut_103` (`col.get("vault", )`, i.e. the default arg dropped entirely,
which is also `None`): both replace the real default `False` with `None`, and
`bool(None) == bool(False)`, so the vault-decline check reads identically either
way. (The genuinely different default, `col.get("vault", True)`, mutmut_106, IS a
real behavior change and is killed by
`test_static_candidacy_admits_resident_when_vault_key_is_absent`.)

**`_resolve_truncate_keep`'s `from_end` default, same collapse (2)** --
`__mutmut_10` (`cfg.get("from_end", None)`), `__mutmut_12` (`cfg.get("from_end",
)`, default dropped to `None`): both again swap `False` for `None` under a
`bool(...)` wrapper, so no `cfg` shape can tell them apart from the real code.

**`_masked_batches`'s `i > 0` -> `i >= 0` (1)** -- `__mutmut_7`: at `i == 0`,
`batch` in the loop IS `first` (see `_rechain`), so
`_schema_drift_reason(expected_schema, batch.schema)` compares `first.schema`
against itself and always returns `None`. Running that comparison on the first
batch too changes nothing observable.

**Message-only prose, never branched on (8)** -- every one of these mutates or
drops an `ExecutionError`'s `message=` argument (`ExecutionError.__init__` gives
`message` a `""` default, so dropping the kwarg is legal, not a crash) while
leaving `.code` untouched; no caller or test reads `.message`, only `.code`.
`_masked_batches` `__mutmut_19` (message dropped), `__mutmut_21` (message dropped,
alternate paren shape); `_validate_ledger` `__mutmut_3`, `__mutmut_5` (first raise,
message dropped/reshaped), `__mutmut_12`, `__mutmut_14` (second raise, same),
`__mutmut_17`, `__mutmut_18` (second raise, message text wrapped/upper-cased). The
`.code` mutations on these same two raises (`"native_route_ledger_invalid"` case
changes, dropped/`None` `code=`) ARE real and are killed by
`test_validate_ledger_raises_on_attempted_completed_mismatch` and
`test_validate_ledger_raises_on_any_single_nonzero_oracle_or_fallback_count`.

**`try_native_route` precondition guards the source itself marks unreachable (6)**
-- every one of these sits on a branch already commented
`# pragma: no cover - precondition` in the source, because `static_candidacy` /
`peek_and_admit` already proved the invariant the guard restates before
`try_native_route` ever reaches it: `__mutmut_31`, `__mutmut_32`, `__mutmut_33`
(the "candidacy admitted with no table name" `AssertionError`'s message text),
`__mutmut_36` (the "admitted a non-LazySource entry" message text), `__mutmut_61`
(the "admitted=True with no batch/iterator" message text), and `__mutmut_58` (the
guard's own condition, `... is None or ... is None` -> `and`) -- the `or` can never
observably differ from `and` on a branch neither disjunct can ever satisfy for a
real caller.

**`try_native_route` passes an unused `plan` through (1)** -- `__mutmut_63`
(`plan=None` in the call to `_run_native_streaming`): that function's first line is
`del plan  # admission already resolved the declared-column set; unused here`, so
no value of `plan` is ever observable past that call.

**`_run_native_streaming`'s own precondition guard, same class as above (4)** --
`__mutmut_38`, `__mutmut_39`, `__mutmut_40`, `__mutmut_41`: the `sink is None`
raise inside the `streaming=True` branch is marked
`# pragma: no cover - precondition: sink_mode="streaming" implies a real sink`;
`static_candidacy` never returns `sink_mode="streaming"` without a real sink, so
only the message text differs here, never anything reachable.

**`_run_native_streaming`'s dead post-commit writes (3)** -- `__mutmut_42`
(`committed = None` initial value), `__mutmut_54`, `__mutmut_55` (`committed =
True` after a successful `sink.commit()` mutated to `None`/`False`): `committed` is
read only inside the `except BaseException:` clause, which is unreachable once
`sink.commit()` has returned without raising (there is nothing else in the `try`
block after it that can fail), so whatever it is set to at that point is never
read.

**`_run_native_streaming`'s diagnostic-only `table` param to `_validate_ledger` (2)**
-- `__mutmut_51`, `__mutmut_60` (`table=None` in both call sites): same class as
the message-prose group above -- `_validate_ledger`'s `table` argument is used
only inside its raised messages, never in a branch.

**`_run_native_streaming`'s `pa.Table.from_batches(..., schema=None)` (2)** --
`__mutmut_65`, `__mutmut_67`: every batch in `masked` was already constructed by
`_mask_one_batch` with `schema=out_schema`, so pyarrow's schema-inference from the
first batch reproduces `out_schema` exactly; passing it explicitly or letting it
infer produces a byte-identical table.

**`_run_native_streaming`'s `ExecutionResult` kwargs that match the dataclass
default (2)** -- `__mutmut_105` (`warnings=()` dropped), `__mutmut_108`
(`row_errors=()` dropped): `ExecutionResult.warnings` and `.row_errors` both
default to `()`, and this lane's real value for both is always `()` too (no
strategy on the allowlist warns; no per-row failure mode exists here), so the
explicit keyword and the default are the same value.

## What's now pinned that wasn't before

- `static_candidacy`: the `no_columns_configured` / `invalid_column_config` /
  `unsupported_strategy:{name}:{strategy}` reason codes exactly; the `"?"` default
  for a column missing `name`; the `redact_with_not_string:{name}` /
  `{code}:{name}` reason threading the real column name (not a dropped one); the
  `elif strategy == "truncate"` branch is proven to actually gate a rejecting
  config, not silently admit it; `sink_mode == "resident"` as a literal value.
- `peek_and_admit`: `admitted`/`column_order` exact values on all three decline
  paths (zero-row, unsupported-projection, non-utf8), and the
  `unsupported_projection:missing=...:extra=...` reason's computed column lists.
- `_resolve_truncate_keep` / `_resolve_strategy_cfg`: every key name and default
  value (`redact_with` -> `"REDACTED"`, `mask_char` -> `None`, `keep` resolution
  from `keep` then `from_end`) via direct, exact-value unit tests.
- `_schema_drift_reason`: the name-mismatch (`missing=`/`extra=`) branch, which no
  existing test reached (only the type-changed branch was covered before).
- `_mask_one_batch` / `_run_native_streaming`: the ledger's attempted/completed/
  rows-attempted/rows-completed arithmetic, `LedgerEntry.table` threading,
  per-column timing accumulation and the `boundary_conversion_ms` assembly-time
  computation -- pinned with a fixed clock (real wall-clock timing on a
  two-row batch is too fast to reliably distinguish a dropped `+=` or a `0.0`
  floor from a genuine near-zero reading) and, separately, two chained calls
  sharing one ledger/timing_acc/boundary_ms_box to prove accumulation (not
  overwrite) across batches. Also: `_execution_envelope`'s exact dict (every key
  and both `outputs_streamed` values), and `try_native_route`'s `attempted`/
  `table` fields on both the candidacy-decline and admission-decline reports,
  which the production suite's `.reason`-only assertions never distinguished from
  a dropped/flipped field.
- `_validate_ledger`: both raises' `.code` field exactly, and the `or`-chain
  triggering on any ONE of the four oracle/fallback counts independently (an
  `or`->`and` regression would require two counts nonzero at once before
  refusing to commit).

## Regenerate

```
# Temporarily point [tool.mutmut] at these two files + the three covering tests
# (see this file's own history for the exact block), then:
.venv/bin/mutmut run
.venv/bin/mutmut show decoy_engine.execution._native_route_exec.x_<func>__mutmut_<N>
```
