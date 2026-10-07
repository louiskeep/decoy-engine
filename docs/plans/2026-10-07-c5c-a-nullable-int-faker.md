Status: plan

Rules consulted: 00-universal, development-loop, risk-and-exceptions, debugging, feature-dev, testing, code-review.

# C5c-a: deterministic Faker over integer columns with nulls

Program: `docs/plans/2026-09-30-rust-engine-program.md`, Phase C. Cam's decision (2026-10-07) on this bug: "make it work". Branch `fix/c5c-a-nullable-int-faker` off engine main `228fe140`. Risk R2: an oracle behavior fix on every pandas route.

## 1. Problem

Deterministic Faker maps each source value to a fake value with `derive_index(mask_key, namespace, canonical(value))` (`generation/pool/_sampler.py:117-157`, `generation/pool/_canonicalize.py:62-119`). Canonicalization encodes an int as length-prefixed two's complement and raises `float_canonicalization_unsupported` for a float (`_canonicalize.py:87-89`).

The pandas adapter converts the source with `to_pandas_fk_safe` (`_fk_keys.py:492-547`). That keeps integers exact only for FK, group-anchor, top_code and group_key columns (`_pandas_adapter.py:209-218`). For every other column, plain `to_pandas()` widens an Arrow integer column that has any null to float64. So:
- deterministic Faker over an integer column that WIDENED (default conversion, at least one null AND at least one non-null value reaching canonicalization) fails the whole job. That happens on every route that runs the adapter: whole-frame, sequential `_sequential.py:~376`, each chunk of the chunked oracle, and the generate-mask, multi-table and physical-fallback paths that reach the same adapter constructors;
- an all-null column, and a source carrying pandas nullable `Int64` metadata, already succeed today and are not touched;
- on the chunked route, only chunks that contain a null fail. The oracle's result therefore depends on chunk boundaries (`native/_dispatch.py:426-436` comment).

**Why not fix the shared frame (Codex C5c round 1, HIGH).** Protecting the column as a nullable `Int64` in the shared frame changes what every other reader of that column sees: `when:` predicates, derived and case_when readers, group_by siblings and pandas metadata. Codex showed a job that succeeds today (a gate that never runs Faker) whose output changes. The fix must therefore stay at the Faker sampling boundary.

## 2. Decision: exact integers supplied at the sampling boundary

**2a. Which columns.** A column is an "exact-int Faker column" when all of these hold:
- its seed is deterministic Faker;
- its Arrow source type is a signed or unsigned integer;
- the converted frame column is float64 (it widened);
- no work node that has ACTUALLY RUN before Faker's turn wrote that column, so the frame column still holds the converted source.

"Run before" is decided from the adapter's real ordered WorkNodes and dispatch semantics, including FK-resolution overrides (a node resolved by FK lookup never runs its handler). It uses the nodes' declared write sets, but neither native admission's name ordering nor its unknown-read policy (Codex round 1: `_earlier_writes` consumes config mappings, orders by name, and rejects unknown reads, none of which models the adapter). The check is computed where the adapter iterates its nodes.

Anything else keeps today's behavior exactly. In particular, a column an earlier node writes still raises as it does now.

**2b. The adapter supplies the original values** (and releases them): Each route that builds a `StrategyContext` and runs handlers (`_pandas_adapter.py:~234`, `_sequential.py:~307`, and the chunked oracle through the adapter, `_chunked_oracle.py:~349`) records, for each exact-int Faker column of the table it runs, a reference to that column's ORIGINAL Arrow array:
- `ctx.exact_int_sources: Mapping[(table, column), pa.ChunkedArray]`, read-only;
- no copy is made, because it is the source table's own column;
- on the chunked oracle it is the current RAW chunk's column, before any normalization;
- **lifetime (Codex round 1):** references are registered when a table's source is loaded and released together with that table's frame. On the sequential route with a sink (`_sequential.py:~495` evicts frames), a finished table's integer buffers must not stay reachable through the context. A chunk context holds only its current chunk.

**2c. Row positions under a `when:` gate.**
- When the gate runs a handler on `sub_df = df.loc[mask].copy()` (`_when_gate.py:212`), it passes the selected row positions through the context as `ctx.gate_positions`, set via `dataclasses.replace` for that one call. The mutable sinks stay shared.
- Positions are `np.flatnonzero(mask.to_numpy(dtype=bool, na_value=False))`. A nullable-boolean mask with `pd.NA` selects exactly what `df.loc[mask]` selects, with no truth-value error (Codex round 1 HIGH: plain `np.flatnonzero(mask)` raises on `pd.NA` and would break gated jobs that succeed today).
- The same positions feed the existing row-error remap.
- Without a gate, positions are `None`, meaning all rows in order.

**2d. The Faker handler** (`_strategies/_faker.py`, deterministic path only):
- when `(ctx.current_table, column)` is in `ctx.exact_int_sources`, it takes the original Arrow values at the given positions;
- it checks the null mask matches the frame column's `isna()` row for row, and raises a coded `GenerationError` if not, because that would be a wiring bug;
- it builds a FULL-LENGTH, explicitly `object`-typed sampling Series: Python `int` for valid rows and `None` for null rows, aligned to the frame column's index. The dtype is never inferred, because inference would widen to float again. The sampler still does its own null filtering. `PoolSampler.sample` requires a full-length Series, and a values-only sequence raises `source_length_mismatch` (Codex round 1);
- the canonical bytes are therefore exactly those of the same value in a null-free int64 column;
- selection, null handling and write-back (`df[column] = [...]`) are otherwise unchanged.

The positional (non-deterministic) path never reads values and is untouched.

**2e. The compiled pool-index path** (`_sampler.py`, `pa.Array.from_pandas` with fallback to the reference) receives the same exact values. Test 1 asserts the exact values at BOTH kernel boundaries, including uint64 above 2**63 (where the compiled path may fall back). It requires evidence that the compiled path actually ran for admitted widths, not two fallback runs.

**2f. Known limitation, kept.** A gate that selects only SOME rows of an integer column, with a string-output provider, leaves a column of mixed strings and numbers. Output conversion then raises `ArrowTypeError`, as it already does for any partially gated type-changing column today. This slice adds no output coercion, so that case still fails: with a new error in place of the canonicalization error. Test 3b pins it.

**2g. No other change.** The frame, predicates, other strategies, unified admission and the native routes are untouched. The native routes still decline non-string deterministic Faker; C5c-ii opens them.

## 3. Acceptance tests (written first; never weakened)

1. **Works:** deterministic Faker over int8, int16, int32, int64, uint8 and uint64 columns with nulls completes on the whole-frame, sequential (an FK job with an unrelated nullable-int Faker column) and chunked oracle routes. Each non-null value maps to exactly the fake value the same value gets in a null-free copy of the column, and nulls stay null. Values above 2**53 (and uint64 above 2**63) key exactly. The compiled and reference index paths agree.
2. **Only failing jobs change:** these produce output byte-identical to main (tables, schema, pandas metadata, warnings, row errors, metrics minus timings):
   - a null-free int Faker column;
   - a nullable-int Faker column under a `when:` gate that selects ZERO rows, so Faker never runs (Codex's counterexample);
   - a `when:` predicate on another column that reads the nullable-int Faker column;
   - a derived or case_when column reading it;
   - a group_by sibling reading it;
   - a nullable `Int64`-metadata source.
3. **Gate positions:**
   - (a) With a numeric-output provider, under a gate that selects some rows and one that selects all rows, each selected value maps as in test 1 and unselected rows keep their source value. This includes a non-default pandas index (parquet metadata with non-range row labels), a duplicate index, a nonzero chunk offset, and a nullable-boolean predicate with `pd.NA`.
   - (b) With a string-output provider under a partial gate, the mixed-type output rejection (`ArrowTypeError`) is preserved and pinned. An all-rows gate succeeds.
   - (c) An unrelated strategy (string redact) under a nullable-boolean `pd.NA` predicate still succeeds, byte-identical to main.
4. **Chunked stability:** a nullable int column with nulls in some chunks and not others gives the same output as one pass (the chunked oracle against whole-frame).
5. **Earlier writer:**
   - a column that an earlier-RUN node writes keeps main's behavior exactly, whether that is success or the existing error;
   - an FK-delayed node and a multi-column writer are both handled from the adapter's real order.
6. **Wiring guard:** a forced null-mask mismatch between the Arrow source and the frame raises the coded error, never a silent misalignment.
7. **Float sources unchanged:** a true float64 source (including whole-number floats) still raises `float_canonicalization_unsupported`.
8. **Testflight:** if any fingerprint moves, STOP and report.
8b. **More coverage:** empty and all-null columns (unchanged from main), multi-table and generate-mask jobs, and reference release between sequential tables (the buffers are no longer reachable after eviction). Native routes still decline deterministic non-string Faker, and C5c-i's positional outcomes are unchanged.
8c. **Test migration:** `tests/.../test_when_gate_mutation_kills.py` uses a non-dataclass `_Ctx` and asserts gated context identity. It moves to real `StrategyContext` fixtures, and gated identity assertions become shared-sink identity plus position assertions. No-gate and preflight identity tests stay as they are, and so does nested row-error remap coverage.
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

## 5. Review log

- **Codex plan gate, round 1: REVISE** (1 HIGH, 5 MEDIUM, 1 LOW). All folded into rev 2:
  - **HIGH:** nullable-boolean-safe positions;
  - **MEDIUM:** the adapter-order writer rule;
  - **MEDIUM:** a full-length object sampling Series, plus compiled evidence;
  - **MEDIUM:** reference lifetime;
  - **MEDIUM:** the partial-gate mixed-type limitation, stated and pinned;
  - **MEDIUM:** test migration;
  - **LOW:** facts qualified and coverage completed.
