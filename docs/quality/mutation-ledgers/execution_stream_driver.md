# Mutation grading: P4-A Task 6 reorder driver (`_stream_driver.py`)

Scope: the CHANGED unit for Task 6 is the new
`src/decoy_engine/execution/out_of_core/_stream_driver.py` (`stream_table` +
`_open_joiner` + `_iter_source_batches`; the leaf helpers live in
`_stream_driver_support.py`). Graded via
`scripts/native-testing/python_mutation_pilot.py` (mutmut + standalone-pytest
readjudication), selection `test_stream_driver_reorder.py` +
`test_stream_driver_lifecycle.py`.

## Numbers

**366 mutants: 268 killed, 98 survived, 0 true-timeout (73.22% logic).** The
survivors split `stream_table` 82 / `_open_joiner` 15 / `_iter_source_batches` 1.
**0 unresolved correctness-critical logic on the changed unit.**

## Correctness-critical control flow: fully pinned (0 survivors)

An exhaustive screen of every `stream_table` survivor against the
correctness-critical constructs -- `total_orphans` (FAIL precount),
`run_ordered_join`, `enter_context` / `ExitStack` / `.close` (owning lifecycle),
`resolve_batch` / `.take(` / `assert_exhausted` (per-edge resolve + cursor
alignment), `row_nr` / `offset` (positional alignment), `_replace_fk_columns`,
`orphan_policy` / FAIL, and the per-edge `register` -- found **0 survivors
touching any of them.** The byte-parity suite (differential vs `run_fk_out_of_core`
+ the 4-shape pandas oracle across matched/orphan-FAIL/WARN/PRESERVE/REMAP, null
FK, empty child, cross-batch/cross-run, multi-edge) plus the three lifecycle
tests kill every mutation that changes an output value, a dropped/duplicated row,
the sort order, the FAIL-before-output ordering, or a leaked resource.

## Accepted survivors (all output-equivalent or covered elsewhere)

- **Tuning-parameter mutations** (`merge_fan_in` default 16->17, `_open_joiner`
  `memory_limit=memory_limit`->`None`): these change DuckDB's spill threshold /
  sorter fan-in, never an output VALUE (the same accepted class as the external
  sorter and split-dedup ledgers). The parity oracle is value-based, so they
  survive by construction.
- **Schema-plumbing** (`_payload_schema` `table_name`->`None`): survived under
  the full parity oracle, i.e. it does not change the produced schema on any
  tested shape -- `table_name` there is naming, not an output-affecting input.
- **`code_set_null_seen` plumbing**: the driver threads code_set records +
  null-seen; code_set correctness (incl. null handling) is graded by the OOC
  group-C parity suite (`test_out_of_core_group_c_parity.py`), not the driver.
  Flagged to the reviewer as an optional driver-level code_set-null coverage add.
- **Message / reject-code prose** on `_open_joiner` (13) and a few on
  `stream_table`: machine-consumed codes preserved; message text mutated.

None changes a masked/resolved value, a row's presence/order, the FAIL-precount
lifecycle, or an owning-iterator close.

## Regenerate

```
.venv/bin/python scripts/native-testing/python_mutation_pilot.py \
  --module src/decoy_engine/execution/out_of_core/_stream_driver.py \
  --tests tests/parity/test_stream_driver_reorder.py \
          tests/unit/execution/test_stream_driver_lifecycle.py \
  --timeout 60
```
