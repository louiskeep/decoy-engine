# Mutation grading: `execution/_chunked_dgrn.py` -- 88.46% (0 unresolved)

Phase 4 slice 1 (DGRN -> `windowed_date`, `docs/plans/2026-08-31-p4-slice1-dgrn-windowed-date.md`).
`_chunked_dgrn.py` is a new module: the DGRN admission set (`CHUNK_DGRN_STRATEGIES`, kept
separate from `CHUNK_SAFE_STRATEGIES` so the FK-self-mask gate cannot fold `windowed_date`
in), the two-level domain guard (`validate_base_row_offset` at entry,
`validate_chunk_row_offset_range` per chunk), and the `windowed_date` + `when:` rejection
(`reject_windowed_date_when`).

Graded via `scripts/native-testing/python_mutation_pilot.py` + `scripts/tq_mutate.py`
(standalone-pytest-per-mutant, avoids mutmut's in-process false-timeout pathology on this
pandas/pyarrow-heavy suite), selection `tests/unit/execution/test_dgrn_windowed_date.py` +
`tests/unit/execution/test_chunked.py` + `tests/unit/execution/test_chunked_fk.py`, killed-bucket
readjudicated.

## Numbers

**69/78 = 88.46% LOGIC, 0 unresolved. 9 survivors, all ACCEPTED NON-CONTRACT (message
prose), 0 killable-and-undocumented residual.**

| Function | Total | Killed | Accepted non-contract |
|---|---|---|---|
| `validate_base_row_offset` | -- | all | 0 |
| `validate_chunk_row_offset_range` | -- | all | 0 |
| `reject_windowed_date_when` | -- | 38/47 | 9 |
| **Total** | **78** | **69** | **9** |

The machine fields asserted (and therefore killed): the coded error
(`chunked_row_offset_out_of_domain`, `chunked_windowed_date_when_not_supported`), the
`ExecutionError.message` / `PlanCompileError.message` NUMBERS (the actual offset value, the
actual bound, the actual offending TYPE name), the `PlanCompileError.path`, the offending
COLUMN NAMES (including the `', '.join(...)` separator over two names, and the `"?"`
placeholder when a column entry has no `name` key), and the `num_rows == 1` boundary
(pinned by a chunk that is both width-1 AND out of domain, so an early-return-on-width-1
mutant cannot hide behind the width-0 empty-chunk case). Free-text explanatory PROSE in
`reject_windowed_date_when`'s trailing "why" sentence (the `when` passes only matching
rows... paragraph) is left ACCEPTED NON-CONTRACT when the code/path/names around it are
already pinned -- same policy `execution_chunked.md`'s full-triage grade established for
this codebase's substrate modules: killable only via brittle full-message-equality, and the
sweep does not pursue that for pure prose.

## The 9 accepted non-contract survivors

All 9 are `reject_windowed_date_when` mutants that XX-wrap or upper-case ONE OF the four
trailing prose fragments of the "why" sentence (`"'windowed_date' with a 'when:'
predicate..."`, `"on the chunked route: \`when\` passes only matching rows to the"`,
`"handler, so the oracle enumerates the filtered subset"`, `"0..matches-1, not each row's
physical position -- a chunk's"`, `"durable row offset cannot reproduce that filtered
enumeration."`). Mutants 37, 39, 41, 43, 45 (XX-wrap) and 38, 42, 44, 46 (uppercase) --
mutant 40 (a case variant of a since-covered fragment) was killed by the "matching rows"
substring assertion added to close this gap; the remaining 9 target fragments no test reads.
The `code`, `path`, and column NAMES (including the two-column join separator) are all
independently pinned in `tests/unit/execution/test_dgrn_windowed_date.py::TestWhenRejection`,
so none of the 9 represents an unverified behavior -- only unverified wording.

## Candidate findings

None. No mutation exposed a wrong admission verdict, wrong coded error, wrong offset
arithmetic, or a chunked/full-frame divergence that current behavior does not already
intend.

## Regenerate

`python scripts/native-testing/python_mutation_pilot.py --module
src/decoy_engine/execution/_chunked_dgrn.py --tests
tests/unit/execution/test_dgrn_windowed_date.py tests/unit/execution/test_chunked.py
tests/unit/execution/test_chunked_fk.py --timeout 30 --readjudicate-killed`. `source_paths`
stays at the package root (mutmut 3.x quirk, see `_chunked.py`'s ledger).
