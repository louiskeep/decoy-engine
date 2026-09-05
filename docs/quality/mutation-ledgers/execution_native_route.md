# Mutation grading: `_native_route.py` + `_native_route_exec.py` (native-route production seam)

Scope: the two modules behind the single-pass streaming native lane --
`_native_route.py` (`maybe_run_native_route`, `static_candidacy`,
`peek_and_admit`, the ledger/report dataclasses) and `_native_route_exec.py`
(`try_native_route` and its private helpers: `_resolve_truncate_keep`,
`_resolve_strategy_cfg`, `_schema_drift_reason`, `_mask_one_batch`,
`_validate_ledger`, `_masked_batches`, `_execution_envelope`,
`_execution_adapter_stamp`, `_run_native_streaming`). Graded with `mutmut`
(`only_mutate` scoped to the two files, selection = the three covering test
files below), readjudicated per-mutant with `scripts/tq_mutate.py` (fresh-
subprocess pytest re-run of every non-`killed` verdict, since mutmut's
in-process runner misfires `timeout` on this suite -- see tq-findings.md #8).

Covering tests: `tests/unit/execution/test_native_route_units.py` (direct
unit tests of the private helpers, including `maybe_run_native_route`),
`tests/parity/native/test_native_route_production_seam.py`, and
`tests/unit/execution/test_native_route_transactional_failures.py`.

## Numbers

**813 mutants total, 765 killed, 48 survived: 94.10% (765/813).** This
supersedes every earlier number recorded against this file (a 95.10%/641/674
claim from a prior pass, itself already marked stale after the P0/P1/P2
native-route-production-seam remediation added `_validate_ledger`'s three
invariant checks and the substrate/RSS/boundary-time fixes). That population
was never regenerated; this pass is the first full re-grade against the
current source, including `maybe_run_native_route` -- the routing-decision
gate a recent refactor moved out of `_pipeline.py` and into `_native_route.py`
-- which had no direct unit tests before this pass.

Of the 48 residual survivors, 22 are non-contract diagnostic prose (an
`ExecutionError.message` string changed while `.code` and every branch
stayed put) and 26 are genuine equivalents (no test, however written, could
ever distinguish the mutant from the real code). Each is listed below with
its own one-line justification; none is left un-investigated because it
happened to survive the run.

## Killed by new tests

Three real-logic gaps, closed by extending `test_native_route_units.py`
(no source change):

**`maybe_run_native_route`'s admission gate (3 mutants)** -- this function
had no direct unit test before this pass; every existing test reached it
only indirectly through `run_pipeline`. Three new tests call it directly:

- `test_maybe_run_native_route_declines_without_calling_try_native_route_when_no_mask_table`
  and `test_maybe_run_native_route_declines_without_calling_try_native_route_when_route_disabled`
  each monkeypatch `try_native_route` to raise if it is ever invoked, then
  call `maybe_run_native_route` with exactly one of `has_mask_table` /
  `native_route_enabled` False. Together they kill the `and`-to-`or`
  mutation on `if not (has_mask_table and native_route_enabled)`: an `or`
  would let either single-flag-True case fall through to
  `try_native_route` instead of declining, which the raising stub catches.
- `test_maybe_run_native_route_calls_try_native_route_with_every_kwarg_threaded`
  passes a distinct sentinel object for every keyword argument, stubs
  `try_native_route` to capture its kwargs, and asserts each one arrives
  unchanged (`is` identity where the type allows it). This kills the
  `source_loader=None` and `fidelity_report=None` overrides mutmut
  generated for two of the pass-through kwargs, and would catch the same
  class of regression on any other kwarg were one introduced later. It
  also pins the return value as a literal passthrough of whatever
  `try_native_route` returns.

**`_mask_one_batch`'s per-column memory-peak accumulation (1 mutant)** --
`mem_acc[key] = max(mem_acc.get(key, 0), delta_kb)` mutated to
`mem_acc.get(None, 0)` always misses (no key is ever `None`), so the mutant
silently drops the running max and keeps only the latest call's delta.
`test_mask_one_batch_accumulates_across_calls_and_floors_at_zero` already
drove two chained calls sharing one `mem_acc`; it gained a fixed `rss_kb`
sequence giving call 1 a 50kb delta and call 2 a smaller 10kb delta, then
asserts `mem_acc[key] == 50` after both calls -- the real code keeps the
peak, the mutant would show 10.

**`try_native_route`'s own default parameter values (3 mutants)** -- two
new tests pin literals a caller relies on when it omits these keywords:

- `test_try_native_route_resolved_substrate_default_is_exactly_pandas`
  stubs `static_candidacy` to capture its kwargs, calls `try_native_route`
  without `resolved_substrate`, and asserts the captured value is the
  literal string `"pandas"` -- catches both the `"XXpandasXX"` and
  `"PANDAS"` mutants mutmut generated for that default.
- `test_try_native_route_explain_plan_and_execution_plan_decision_default`
  stubs `static_candidacy` + `peek_and_admit` to admit and `_run_native_
  streaming` to capture its kwargs, calls `try_native_route` without
  `explain_plan` or `execution_plan_decision`, and asserts the captured
  values are `False` and `None`. The `explain_plan=True` default mutant
  would be unobservable through `_run_native_streaming`'s own `if
  explain_plan and execution_plan_decision is not None` gate alone (that
  gate is also false when `execution_plan_decision` defaults to `None`),
  so this asserts the literal value reaching the callee, not the gated
  behavior -- the earlier, weaker check a regression could still slip past.

## Non-contract diagnostic prose (accepted-not-killed) -- 22

Each of these DOES change an observable value (part of `str(exc)`), so a
test reading that string would catch it. They are accepted anyway because
`.message` is diagnostic prose outside this lane's frozen contract --
callers and every existing test key off `ExecutionError.code` only, never
`.message`. This is a materially weaker claim than equivalence (an
equivalent mutant cannot be killed by ANY test); mislabeling these as
equivalent would overstate the case.

**`_validate_ledger`'s six raises, message only (20)** -- every one of
these mutates or drops a `message=` argument (`ExecutionError.__init__`
defaults `message` to `""`, so dropping the kwarg is legal, not a crash)
while `code="native_route_ledger_invalid"` is untouched on all six raise
sites:

- attempted-vs-completed-calls raise: `mutmut_3` (message dropped to
  `None`), `mutmut_5` (message kwarg removed entirely).
- rejected-chunks raise: `mutmut_16`, `mutmut_18` (same two shapes),
  `mutmut_21`, `mutmut_22` (message text case-mangled).
- oracle/fallback-count raise: `mutmut_27`, `mutmut_29` (dropped),
  `mutmut_32`, `mutmut_33` (case-mangled).
- record-count-mismatch raise: `mutmut_36`, `mutmut_38` (dropped).
- duplicate-identity raise: `mutmut_44`, `mutmut_46` (dropped), `mutmut_49`,
  `mutmut_50` (case-mangled).
- foreign-table raise: `mutmut_54`, `mutmut_56` (dropped), `mutmut_59`,
  `mutmut_60` (case-mangled).

The `.code` mutations on these same six raises ARE on the contract and are
killed by `test_validate_ledger_raises_on_attempted_completed_mismatch`,
`test_validate_ledger_raises_on_any_single_nonzero_oracle_or_fallback_count`,
`test_validate_ledger_raises_on_rows_attempted_completed_mismatch`,
`test_validate_ledger_raises_on_nonzero_rejected_chunks`,
`test_validate_ledger_raises_on_record_count_mismatch`,
`test_validate_ledger_raises_on_duplicate_record_identity`, and
`test_validate_ledger_raises_on_foreign_table_record`.

**`_masked_batches`'s schema-drift raise, message only (2)** -- `mutmut_21`
(message forced to `None`) and `mutmut_23` (message kwarg dropped) on the
`code="native_chunk_schema_drift"` raise; the code is untouched by either.

## Genuine equivalents -- 26

No test, however written, could distinguish these from the real code.

**Boolean-default collapse under `bool()` (4)** -- two independent call
sites share this shape: `static_candidacy`'s `col.get("vault", False)` and
`_resolve_truncate_keep`'s `cfg.get("from_end", False)`. Each has two
survivors: the default swapped to `None` (`static_candidacy` `mutmut_107`,
`_resolve_truncate_keep` `mutmut_10`) and the default dropped entirely,
which is also `None` (`static_candidacy` `mutmut_109`,
`_resolve_truncate_keep` `mutmut_12`). `bool(None) == bool(False)`, so
every caller reads the same value either way. (The genuinely different
default, `col.get("vault", True)`, is a real behavior change and is killed
by `test_static_candidacy_admits_resident_when_vault_key_is_absent`.)

**`_masked_batches`'s `i > 0` -> `i >= 0` (1)** -- `mutmut_9`: at `i == 0`,
`batch` in the loop IS `first` (see `_rechain`), so
`_schema_drift_reason(expected_schema, batch.schema)` compares `first.schema`
against itself and always returns `None`. Running that comparison on the
first batch too changes nothing observable.

**Preconditions the source itself marks unreachable (10)** -- every one of
these sits on a branch already commented `# pragma: no cover -
precondition` (or the equivalent "already proved this" phrasing), because
an earlier function in the same call chain already established the
invariant the guard restates before this code ever reaches it:

- `try_native_route`'s "candidacy admitted with no table name" guard:
  `mutmut_36` (message forced to `None`), `mutmut_37`, `mutmut_38`
  (message text case-mangled) -- `static_candidacy` only ever sets
  `candidate=True` alongside a real `table` string.
- `try_native_route`'s "admitted a non-LazySource entry" guard: `mutmut_41`
  (message forced to `None`) -- `static_candidacy` only admits a table
  whose `caller_sources` entry already passed `isinstance(source,
  LazySource)`.
- `try_native_route`'s "admitted=True with no batch/iterator" guard:
  `mutmut_63` (`is None or ... is None` mutated to `and`), `mutmut_66`
  (message forced to `None`) -- `peek_and_admit`'s only `admitted=True`
  return sets `first_batch` and `rest` together, never one without the
  other, so `or` and `and` can never observably differ on a branch neither
  disjunct can ever satisfy for a real caller.
- `_run_native_streaming`'s "streaming=True but no sink" guard:
  `mutmut_41` (message forced to `None`), `mutmut_42`, `mutmut_43`,
  `mutmut_44` (message text case-mangled) -- `static_candidacy` never
  returns `sink_mode="streaming"` without a real `ParquetTransactionalSink`.

**`try_native_route` passes an unused `plan` through (1)** -- `mutmut_68`
(`plan=None` in the call to `_run_native_streaming`): that function's first
line is `del plan  # admission already resolved the declared-column set;
unused here`, so no value of `plan` is ever observable past that call.

**`_run_native_streaming`'s `committed` bookkeeping (3)** -- `committed` is
read only via `if not committed` inside the `except BaseException:` clause.
`mutmut_45` (the pre-`try` initial value `False` -> `None`) and `mutmut_57`,
`mutmut_58` (the post-`sink.commit()` value `True` -> `None`/`False`) all
collapse under `not`: `bool(None) == bool(False)`, and the post-commit
write is additionally dead code -- nothing in the `try` block after
`sink.commit()` can raise, so the `except` clause that reads `committed`
is unreachable once it has been set to `True`.

**`pa.Table.from_batches(..., schema=...)` (2)** -- `mutmut_68`
(`schema=None`), `mutmut_70` (the kwarg dropped, which is also `None`):
every batch in `masked` was already constructed by `_mask_one_batch` with
`schema=out_schema`, so pyarrow's schema-inference from the first batch
reproduces `out_schema` exactly; passing it explicitly or letting it infer
produces a byte-identical table.

**`_run_native_streaming`'s `ExecutionResult` kwargs matching the dataclass
default (2)** -- `mutmut_131` (`warnings=()` dropped), `mutmut_134`
(`row_errors=()` dropped): `ExecutionResult.warnings` and `.row_errors`
both default to `()`, and this lane's real value for both is always `()`
too (no strategy on the allowlist warns; no per-row failure mode exists
here), so the explicit keyword and the default are the same value.

**`StrategyTimingRecord`'s `mem_acc.get(..., default)` (3)** -- `mutmut_82`
(default `0` -> `None`), `mutmut_84` (default dropped, also `None`),
`mutmut_85` (default `0` -> `1`): `_mask_one_batch` writes a `mem_acc[key]`
entry in the same loop iteration where it writes the matching `timing_acc[
key]` entry, unconditionally, for every column it processes -- so by the
time `_run_native_streaming` iterates `timing_acc.items()` to build the
timing records, every key it looks up in `mem_acc` is guaranteed present.
The `.get` default can never be read; it is dead regardless of its value.

## Regenerate

```
# Temporarily point [tool.mutmut] at these two files + the three covering
# tests (see this file's own history for the exact block), then:
.venv/bin/python scripts/tq_mutate.py --run --report <path>.json
.venv/bin/mutmut show decoy_engine.execution._native_route_exec.x_<func>__mutmut_<N>
```
