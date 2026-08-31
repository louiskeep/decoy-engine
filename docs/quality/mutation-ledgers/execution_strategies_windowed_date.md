# Mutation grading: `execution/_strategies/_windowed_date.py` -- LOGIC-100%

Phase 4 slice 1 (DGRN -> `windowed_date`, `docs/plans/2026-08-31-p4-slice1-dgrn-windowed-date.md`).
`WindowedDateStrategyHandler.run` forwards `ctx.row_offset` into `apply_windowed_date`; this
is the one line that makes the chunked route position-aware instead of always minting from
`i=0`.

Graded via `scripts/native-testing/python_mutation_pilot.py` + `scripts/tq_mutate.py`,
selection `tests/unit/transforms/test_windowed_date.py` +
`tests/unit/execution/test_dgrn_windowed_date.py`, killed-bucket readjudicated.

**17/17 = 100% LOGIC, 0 survivors, 0 unresolved.**

## Why 100% instead of the substrate 77.91% bar

The first grading pass (16/17) left one survivor: `df[column] = date_list` mutated to
`df[column] = None`. Every existing test at that point compared the
CHUNKED route's output against the FULL-FRAME route's output (both routes call this SAME
handler), so a handler that writes `None` on both sides still passes route-parity -- the
mutant is invisible to a parity-only suite by construction. Closed by
`TestHandlerWritesRealValues::test_run_single_output_matches_direct_apply_windowed_date_oracle`
in `test_dgrn_windowed_date.py`: it runs `WindowedDateStrategyHandler` through
`PandasExecutionAdapter.run_single` (the real dispatch path) and compares the result against
a DIRECT `apply_windowed_date(...)` call -- an oracle entirely outside the handler dispatch,
so a constant-write mutant cannot hide behind two routes agreeing with each other.

## Regenerate

`python scripts/native-testing/python_mutation_pilot.py --module
src/decoy_engine/execution/_strategies/_windowed_date.py --tests
tests/unit/transforms/test_windowed_date.py tests/unit/execution/test_dgrn_windowed_date.py
--timeout 30 --readjudicate-killed`.
