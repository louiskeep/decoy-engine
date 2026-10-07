Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, debugging, feature-dev, testing, code-review.

# C5c-a: deterministic Faker over integer columns with nulls

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. Cam's decision (2026-10-07) on this bug: "make it work". Branch `fix/c5c-a-nullable-int-faker` off engine main `228fe140`. Risk R2: an oracle behavior fix on every pandas route.

## 1. Problem

Deterministic Faker maps each source value to a fake value with `derive_index(mask_key, namespace, canonical(value))` (`generation/pool/_sampler.py:117-157`, `generation/pool/_canonicalize.py:62-119`). Canonicalization encodes an int as length-prefixed two's complement and raises `float_canonicalization_unsupported` for a float (`_canonicalize.py:87-89`).

The pandas adapter converts the source with `to_pandas_fk_safe` (`_fk_keys.py:492-547`). That keeps integers exact only for FK, group-anchor, top_code and group_key columns (`_pandas_adapter.py:209-218`). For every other column, plain `to_pandas()` widens an Arrow integer column that has any null to float64. So:
- deterministic Faker over a nullable integer column fails the whole job, on every route that runs the adapter (whole-frame, sequential `_sequential.py:~376`, and each chunk of the chunked oracle);
- on the chunked route, only chunks that contain a null fail. The oracle's result therefore depends on chunk boundaries (`native/_dispatch.py:426-436` comment).

**Why not fix the shared frame (Codex C5c round 1, HIGH).** Protecting the column as a nullable `Int64` in the shared frame changes what every other reader of that column sees: `when:` predicates, derived and case_when readers, group_by siblings and pandas metadata. Codex showed a job that succeeds today (a gate that never runs Faker) whose output changes. The fix must therefore stay at the Faker sampling boundary.

## 2. Decision: exact integers supplied at the sampling boundary

**2a. Which columns.** A column is an "exact-int Faker column" when all of these hold:
- its seed is deterministic Faker;
- its Arrow source type is a signed or unsigned integer;
- the converted frame column is float64 (it widened);
- no EARLIER work node writes that column, so the frame column still holds the converted source at Faker's turn. This uses the same write-set rule as C8-i's `when` admission (`native/_when_admission._earlier_writes`).

Anything else keeps today's behavior exactly. In particular, a column an earlier node writes still raises as it does now.

**2b. The adapter supplies the original values.** Each route that builds a `StrategyContext` and runs handlers (`_pandas_adapter.py:~234`, `_sequential.py:~307`, and the chunked oracle through the adapter, `_chunked_oracle.py:~349`) records, for each exact-int Faker column of the table it runs, a reference to that column's ORIGINAL Arrow array:
- `ctx.exact_int_sources: Mapping[(table, column), pa.ChunkedArray]`, read-only;
- no copy is made, because it is the source table's own column;
- on the chunked oracle it is the current RAW chunk's column, before any normalization.

**2c. Row positions under a `when:` gate.**
- When the gate runs a handler on `sub_df = df.loc[mask].copy()` (`_when_gate.py:212`), it passes the selected row positions `np.flatnonzero(mask)` through the context: `ctx.gate_positions`, set via `dataclasses.replace` for that one call. The mutable sinks stay shared.
- Without a gate, positions are `None`, meaning all rows in order.

**2d. The Faker handler** (`_strategies/_faker.py`, deterministic path only):
- when `(ctx.current_table, column)` is in `ctx.exact_int_sources`, it takes the original Arrow values at the given positions;
- it checks the null mask matches the frame column's `isna()` row for row, and raises a coded `GenerationError` if not, because that would be a wiring bug;
- it hands the sampler the non-null values as Python ints, so the canonical bytes are exactly those of the same value in a null-free int64 column;
- selection, null handling and write-back (`df[column] = [...]`) are otherwise unchanged.

The positional (non-deterministic) path never reads values and is untouched.

**2e. The compiled pool-index path** (`_sampler.py`, `pa.Array.from_pandas` with fallback to the reference) receives the same exact values. It must agree with the reference path, which test 1 pins.

**2f. No other change.** The frame, predicates, other strategies, unified admission and the native routes are untouched. The native routes still decline non-string deterministic Faker; C5c-ii opens them.

## 3. Acceptance tests (written first; never weakened)

1. **Works:** deterministic Faker over int8, int16, int32, int64, uint8 and uint64 columns with nulls completes on the whole-frame, sequential (an FK job with an unrelated nullable-int Faker column) and chunked oracle routes. Each non-null value maps to exactly the fake value the same value gets in a null-free copy of the column, and nulls stay null. Values above 2**53 (and uint64 above 2**63) key exactly. The compiled and reference index paths agree.
2. **Only failing jobs change:** these produce output byte-identical to main (tables, schema, pandas metadata, warnings, row errors, metrics minus timings):
   - a null-free int Faker column;
   - a nullable-int Faker column under a `when:` gate that selects ZERO rows, so Faker never runs (Codex's counterexample);
   - a `when:` predicate on another column that reads the nullable-int Faker column;
   - a derived or case_when column reading it;
   - a group_by sibling reading it;
   - a nullable `Int64`-metadata source.
3. **Gate positions:** under a gate that selects some rows and one that selects all rows, each selected value maps as in test 1 and unselected rows keep their source value. That includes a non-default pandas index (parquet metadata with non-range row labels), so positions, not labels, are used.
4. **Chunked stability:** a nullable int column with nulls in some chunks and not others gives the same output as one pass (the chunked oracle against whole-frame).
5. **Earlier writer:** a column that an earlier node writes still raises exactly as on main (code and message).
6. **Wiring guard:** a forced null-mask mismatch between the Arrow source and the frame raises the coded error, never a silent misalignment.
7. **Float sources unchanged:** a true float64 source (including whole-number floats) still raises `float_canonicalization_unsupported`.
8. **Testflight:** if any fingerprint moves, STOP and report.
9. **Sentries, plus mutation** on the column rule (2a), position passing (2c) and the handler branch (2d).

## 4. Failure modes

| Risk | Closed by |
|---|---|
| Output changes for jobs that work today | The frame is untouched; test 2 matrix |
| Positions misaligned under a gate or a non-default index | `flatnonzero` positions; test 3 |
| The column was already rewritten by an earlier node | 2a write-set rule; test 5 |
| Routes disagree | Every ctx-building site lists the same columns; tests 1 and 4 |
| A silent misalignment | Null-mask check; test 6 |
| A large integer loses precision | Original Arrow values, never the float64 frame; test 1 |

Rollback: revert the merge commit.
