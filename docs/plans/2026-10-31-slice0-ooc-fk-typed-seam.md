Status: plan only, revision 1. No implementation or release approval is granted by this document.

Summary: The out-of-core FK join rebuilds every child FK output value from Python scalars by type inference, which collapses `timestamp[ns]` keys to microseconds, and type-tags join tokens by Python class name, which makes equal instants in different timestamp units miss each other. One typed seam (explicit Arrow type carried from the schemas into every output array and a unit-free temporal join token) closes both.

Baselines: engine `origin/main` `a6e5c252` (2026-10-31), planning branch `plan/slice0`. Every probe ran in the `uv.lock` venv built by `scripts/dev/bootstrap.sh` (Python 3.10.20, pyarrow 24.0.0, pandas 2.3.3, duckdb 1.5.4). Probe scripts and outputs are reproduced inline in section 1; they are not committed.

Rules consulted: `DEFECT-CATALOG.md` and `OWNER-DECISIONS-2026-10-30.md` (decoy-platform `handoffs/queue`), the engine `CLAUDE.md`, `docs/compatibility-contract.md` 3.3 and 3.4, `tests/parity/SEMANTIC_DIFFERENCES.md` (named by `_join.py:313-314`; the worker reads it before adding a row).

# Slice 0, item 2: typed FK output in the out-of-core join (14.1), PLAN (revision 1)

Risk: R2 (wrong-value fix on one route; no persisted format, no public signature, no frozen contract changes; the determinism guarantee is restored, not altered, because the full-frame output is the reference). Gate: Codex.

Threat model: V1. In scope: ordinary data shapes (a nanosecond timestamp key, a parent and child stored at different units) producing wrong published values and false orphans. Out of scope: hostile Arrow extension types.

## 1. Reproduction on main (yes, on the real auto route)

Harness: parent `ts: timestamp[ns]` with 20 keys 1 ns apart, child `parent_ts` equal to them (second run: child row 0 moved 1 ns off every parent), parent key `passthrough`, child FK `passthrough`, `orphan_policy: preserve`, both tables 20 rows, `run_pipeline(cfg, sources, engine_version="0.1.0", **kw)`. `passthrough` is in the parent-key allowlist (`_compat.py:34`, `:529`), and `hash` on a naive timestamp is refused by canonicalization on every route (`timezone_naive_datetime`), so passthrough is the reachable shape.

| kw | `quality_metrics["execution"]` | child FK type | distinct child keys (parent has 20) |
|---|---|---|---|
| `execution_mode="full_frame"` | `full_frame` / `override_full_frame` | `timestamp[ns]` | 20 |
| `execution_mode="sequential"` | `sequential` / `pure_mask_fk` | `timestamp[ns]` | 20 |
| `execution_mode="out_of_core"` | `out_of_core` / `override_out_of_core` | `timestamp[us]` | 1 |
| `out_of_core_threshold_rows=10, use_byte_estimate_routing=False` | `out_of_core` / `out_of_core_large_fk` | `timestamp[us]` | 1 |
| defaults (`auto`) | `out_of_core` / `byte_estimate_bounded_out_of_core` | `timestamp[us]` | 1 |

The parent output keeps 20 distinct `timestamp[ns]` keys on every route, so on out-of-core the published child no longer references its parent (referential integrity broken) and the 1-ns-off orphan becomes indistinguishable from a match. The join itself is exact: DuckDB round-trips `timestamp[ns]` through the relation parquet and the result batch (probed: `pq.read_metadata` reports `timestamp[ns]`, `to_arrow_reader` yields `timestamp[ns]`). The loss is at the Python round trip: `_append_output_batch` (`_join.py:261-302`) calls `to_pylist()` (`pd.Timestamp`, exact) then `pa.array(component, from_pandas=True)` (`_join.py:302`), which infers `timestamp[us]`; and the fixed output type the streaming joiners cast to is hard-coded `pa.timestamp("us")` (`_batch_join.py:506-509`), so `_cast_chunks` (`_batch_join.py:527-540`) cannot restore what inference dropped.

Second shape, same class (found by the matrix): parent `timestamp[us]`, child `timestamp[ns]`, equal instants, `orphan_policy: warn`. full_frame: 0 orphans. out_of_core: 12 of 12 rows reported `orphan_fk` and published as orphans. Cause: `fk_join_key` falls to `\x00OBJ:{type(normalized).__qualname__}:{normalized!r}` (`_fk_keys.py:208`), and `as_py()` yields `datetime.datetime` for `us` and `pd.Timestamp` for `ns`, so equal instants mint different tokens on both sides (`_relation.py:396-405`, `_join.py:230-237`). The pandas oracle's dict treats them as equal.

Key-type matrix on main (passthrough parent key, preserve, 12 rows, one null child key; full_frame versus forced out_of_core): `timestamp[ns]` values differ (the bug); `timestamp[us]` and `timestamp[ms]` values equal, type differs (`ns` on full_frame via pandas, `us` on out-of-core); `date32`, `string`, `int64`, `float64` equal in type and value; `int8`, `int16`, `int32`, `uint32` both routes widen to `int64` (the pandas oracle does the same, a documented DE-10 divergence, not this plan's); `decimal128(10,2)` and `uint64` are refused up front by out-of-core (`out_of_core_fk_key_dtype_unsupported`) while full_frame completes them (the hazard gate diverts these to sequential only when byte-estimate routing is on, `_pipeline_routing.py:588-590`). Classification: `timestamp[ns]` value loss and the mixed-unit false orphan are the two wrong-value defects; everything else is pre-existing typing divergence outside this plan.

## 2. Guarantee (invariants as properties)

- **P1 (typed output).** For every FK component on every out-of-core join surface, the output array's Arrow type is the type fixed from the schemas (`_resolve_output_types`), and for a temporal fixed type that type is the source column's own type (unit preserved, tz-naive). No output array is built by inference from Python values.
- **P2 (value exactness).** For every row, the published child FK value equals the parent's masked key value (matched), the `fk_key_value`-normalized source key (PRESERVE/WARN orphan) or the remap value (REMAP), compared as Arrow scalars at the source's precision. Two source keys that differ in the source differ in the output.
- **P3 (join equality is value equality).** Two FK key components mint the same join token if and only if the pandas oracle's dict treats them as the same key. For temporal values: same instant, any unit, naive only.
- **P4 (fail closed, same code).** Any value a fixed type cannot hold exactly raises `out_of_core_fk_key_dtype_unsupported`, as today, before the batch is emitted.
- **P5 (parity reference).** The reference for P1 and P2 is the full_frame route's output, not the whole-child test oracle `mask_child_fk`.

## 3. Design (one structural fix per class)

- **Typed construction seam.** One helper `fk_output_array(values: list[object], target: pa.DataType) -> pa.Array` in `_join.py` (or `_fk_keys.py` if `_join.py` would cross 600 LOC; it is 469 now). Rule: for temporal, string, binary and decimal targets construct with `pa.array(values, type=target, from_pandas=True)`, which pyarrow makes exact or raising (probed: `pd.Timestamp` ns into `timestamp[ns]` exact; `Decimal("1.234")` into `decimal128(4,2)` raises; `str` into `int64` raises); for numeric, boolean and null targets keep inference plus the existing `cast_fk_chunk` representability guard (`_join.py:379-421`), because explicit construction refuses exactly representable integers beyond 2**53 that the guard admits and silently truncates `2.5` into `int64` (probed). The helper is the only place that turns FK Python values into an Arrow array.
- **Call sites replaced (every inference site in the join family):** `_append_output_batch` (`_join.py:300-302`), which gains a required `output_types: tuple[pa.DataType, ...] | None` (None keeps inference for the whole-child oracle only); `ChildFkBatchJoiner._batch_remap_values` (`_batch_join.py:342-348`); the stream joiner's remap (`_stream_join.py:636-639`); the whole-child `_remap_values` (`_batch_join.py:563-565`). The two streaming joiners pass their `_output_types` (`_batch_join.py:141-146`, `_stream_join.py:188-193`).
- **Fixed type for temporal.** `_python_roundtrip_type` (`_batch_join.py:480-517`) returns `dtype` itself for a tz-naive timestamp (any unit) and `date32`; the tz-aware rejection stays. `fk_out_of_core_dtype_hazard` dry-runs this function (`_pipeline_routing_fk_dtype.py:55-60`), so the router inherits the change with no edit.
- **Temporal join token.** `fk_join_key` (`_fk_keys.py:173-208`) gains one branch before the `OBJ` fallback: naive `datetime.datetime` and `pd.Timestamp` encode as `\x00TS:{nanoseconds since epoch}`; `datetime.date` (not datetime) as `\x00DATE:{ordinal}`. A tz-aware value keeps today's path (and is refused up front by `_python_roundtrip_type`). Both sides of the join (`_relation.py:405`, `_join.py:237`) share `fk_join_key_tuple`, so one change covers both. Established pattern: canonical epoch integers are how Arrow itself compares timestamps across units (cast to the finer unit), cited in the docstring.
- **Not changed:** `mask_child_fk` (test oracle, `_join.py:61-174`, used only by tests), `_concat_fk_chunks`, `cast_fk_chunk`, the int widening to `int64`, the decimal and `uint64` refusals, the pandas routes.

## 4. Class table

| # | Class | Closing structure | Proving test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | Error text in the helper names Arrow types only, as today (`_join.py:324-332`); no key value is formatted | T6: a marker string key forced into an `int64` target; assert the marker is absent from the exception | Format `values[0]` into the message: T6 fails |
| 2 | Cleanup missing on an exit path | Unchanged owner: the joiner constructor releases its DuckDB connection on any raise (`_batch_join.py:168-175`); the helper raises before `output_chunks` is appended | T7 exit matrix: refusal in the helper mid-batch leaves `output_chunks` empty and the connection closed after `close()` | Append before constructing: T7 fails |
| 3 | State machine and concurrency | N/A: the join is single-threaded per edge and adds no state | | |
| 4 | Route parity and silent fallback | One helper used by every out-of-core join surface (resident batch joiner, sink-batch, sink-reorder stream joiner, REMAP minting); the fixed-type function is shared with the router hazard | T1 route matrix over the three out-of-core modes and the three gate paths (forced, large_fk, byte_estimate) against full_frame | Leave one call site on `pa.array(..., from_pandas=True)`: that cell of T1 fails |
| 5 | Referential integrity | P2 and P3; the temporal token; typed output | T1 (ns keys), T2 (mixed units, false orphans), T3 key-type matrix under PRESERVE, WARN, FAIL, REMAP; single and composite edges; null keys | Revert the `TS` token: T2 fails. Revert `_python_roundtrip_type` for timestamps: T1 fails |
| 6 | Untrusted producer | Explicit typed construction is strict (raises on loss); the numeric branch keeps the guarded cast | T4 helper corpus: each family with an unrepresentable value raises the one code; `2.5` never lands in an `int64` target silently | Construct numeric targets with `type=` directly: the `2**54` case fails or the `2.5` case silently truncates and T4 fails |
| 7 | TOCTOU on a path | N/A: relation parquet paths and temp dirs are unchanged | | |
| 8 | Weak tests and overclaimed evidence | T1 to T5 recorded red on `a6e5c252`; parity asserted against full_frame outputs with `Array.equals` (exact on values and nulls), never `to_pylist` equality | T8 non-vacuity: every new assertion fails on main with the recorded value; red baseline file `docs/plans/evidence/2026-10-31-slice0-ooc-red-baseline.txt` | Replace `Array.equals` with a `to_pylist` compare: the us/ns case passes vacuously and the sentry in T8 fails |
| 9 | Behavior-preserving refactor drift | Characterization first: the existing oracle battery (`tests/unit/execution/test_out_of_core_join_chunked.py`, `test_out_of_core_batch_join.py`, `tests/parity/test_out_of_core_fk_parity.py`) runs unchanged; `mask_child_fk` keeps inference | T5: existing suites green before and after; int, float, bool, string, binary, decimal cells byte-identical | Change the int widening or the bool rule: the existing battery fails |
| 10 | Frozen contract drift | No public signature or format changes; `fk_join_key` tokens for non-temporal types are byte-identical (snapshot) | T9 token snapshot over int, float, Decimal, str, bool, None and the new temporal cases | Change any existing token prefix: T9 fails |

## 5. Enumerations from the code

### 5.1 Exit paths

- Joiner construction: typing refusal before the connection exists (`_batch_join.py:141-146`), connection released on any raise after (`_batch_join.py:168-175`); stream joiner mirrors it (`_stream_join.py:180-200`).
- Per batch: FAIL anti-join raise before output (`_batch_join.py:242`, `:323-328`); helper refusal with `FK_KEY_DTYPE_UNSUPPORTED_CODE` (`_join.py:303-333`); `cast_fk_chunk` refusal (`_join.py:409-419`); `_unified_chunk_type` decimal refusal (`_join.py:433-448`).
- `join_batch` always unregisters the staging table (`_batch_join.py:269-270`).

### 5.2 Routes

- Route decision (`_pipeline_routing.py:510-535` forced modes; `:572`, `:605` byte-estimate bounded; `:634` large FK) and the hazard diversion to sequential (`:588-590`, only with byte-estimate routing).
- Out-of-core execution modes: resident (`out_of_core/_runner.py:426`, `ChildFkBatchJoiner`), sink-batch, sink-reorder (`_stream_join.py`, `_stream_driver.py:50`); the parity tests name them `resident`, `sink-batch`, `sink-reorder` (`tests/parity/test_phase_d_d1a_hash_disposition.py:42`).
- Unaffected: full_frame and sequential (pandas adapter, `_fk_keys.py:11-14`), mixed (generate plus mask).

### 5.3 FK shapes

- Single-column edge: the reproduction. Composite tuple: `fk_join_key_tuple` length-prefixed framing (`_fk_keys.py:211-220`), the `TS` token rides inside it; composite child masked as scalar seeds is already refused (`_batch_join.py:42-56`).
- Multi-parent merged relation needs one masked type across parents (`_multi_parent.py:23`, `_pipeline_routing_fk_dtype.py:73-77`); the temporal fixed type now keeps the unit, so two parents at different units are a type mismatch and stay refused.
- Chain-intermediate keys are diverted to sequential before out-of-core (`_pipeline_routing_fk_dtype.py:60-70`).
- Children in later streamed batches (`_stream_join.py` cursors) and null child keys (`_join.py:278-280`) keep their paths.

### 5.4 Fields

- `ChildFkBatchJoiner.output_types`, `.observed_types` (`_batch_join.py:177-193`); the stream joiner's `output_types` (`_stream_join.py:210-212`).
- `QualityWarning` for `orphan_fk` with `detail.orphan_rows` and the edge names (`_join.py:45-58`).
- `ExecutionError.code`, `.message` for the refusals above; `quality_metrics["execution"]["execution_mode"]` and `["route_reason"]` (`_pipeline_route_exec.py:91-112`).

### 5.5 Output surfaces

- The child table's FK column (data plane): type and values.
- `orphan_fk` warning counts (owner result) and the FAIL policy's `orphan_fk_violation_error` (`_join.py:131-132`).
- Refusal messages naming Arrow types only (`_join.py:324-332`, `:411-418`, `:442-447`, `_batch_join.py:522-527`).
- `quality_metrics["execution"]` route fields.

## 6. Reuse inventory

- `fk_key_value`, `fk_join_key`, `fk_join_key_tuple`, `_decimal_join_token` (`_fk_keys.py:76-220`); `cast_fk_chunk`, `_unified_chunk_type`, `_concat_fk_chunks` (`_join.py:337-458`); `_resolve_output_types`, `_fixed_component_type`, `_cast_chunks` (`_batch_join.py:367-540`); `FK_KEY_DTYPE_UNSUPPORTED_CODE` (no new reason code).
- Existing precedent for keeping Arrow scalars out of Python: `_external_sort_run.py:65-80` (`max_value` stays a `pa.Scalar` because `as_py` re-encodes `timestamp[ns]` at microseconds). The helper docstring cites it.
- Test harnesses: `tests/unit/execution/test_out_of_core_routing.py::_fk_ooc_config` (`:220-276`), `tests/parity/_d1a_hash_data.py::run_chain` and `mode_kwargs` for the three out-of-core modes, `tests/unit/execution/test_pipeline_routing_fk_dtype.py` corpus (`:85-122`).
- Sentries: `tests/sentry/test_module_size.py` (`_stream_join.py` is at its census entry 650, `_batch_join.py` 569, `_join.py` 469; the helper must not push `_stream_join.py` over its entry) and `tests/sentry/test_reason_registry.py`.

## 7. Frozen contracts touched

- Determinism guarantee (`docs/compatibility-contract.md` 3.3): the out-of-core child output for temporal keys changes from the wrong value to the full-frame value; no seed-protocol input changes (`fk_join_key` is not a derive input, `_fk_keys.py:3-6`).
- Public API (3.4): no signature changes; `_append_output_batch` is private.
- No persisted format is touched.

## 8. Acceptance tests, red first (hostile corpus)

Recorded red on `a6e5c252` before any fix, in the locked venv.

- T1 `tests/parity/test_out_of_core_fk_parity.py::test_ns_timestamp_fk_matches_full_frame[mode x gate]`: the section 1 harness; `Array.equals` between full_frame and out-of-core child FK columns, and `set(child) <= set(parent)` on the out-of-core output; three modes times forced, large_fk and byte_estimate gates.
- T2 `::test_mixed_unit_timestamp_keys_are_not_orphans`: parent `us`, child `ns`, equal instants; zero `orphan_fk` on both routes; FAIL policy completes.
- T3 `::test_fk_key_type_matrix[type x policy x shape]`: `timestamp[ns|us|ms]`, `date32`, `string`, `int8..int64`, `uint32`, `float64`, each with a null key, under PRESERVE, WARN, FAIL, REMAP, single and composite edges; expectation per cell is the full_frame output (or the recorded refusal code for `decimal128` and `uint64`, unchanged).
- T4 `tests/unit/execution/test_out_of_core_join_chunked.py::test_fk_output_array_is_exact_or_raises`: helper corpus from the class table, including `2**54` admitted into `float64`, `2**53+1` refused, `2.5` never silently into `int64`, `Decimal("1.234")` refused by `decimal128(4,2)`.
- T5 characterization: the existing join suites green before the change (command recorded).
- T6 marker test, T7 exit matrix, T8 red baseline, T9 token snapshot as named in the class table.
- Commands: `.venv/bin/python -m pytest tests/parity/test_out_of_core_fk_parity.py tests/unit/execution/test_out_of_core_join_chunked.py tests/unit/execution/test_out_of_core_batch_join.py tests/unit/execution/test_pipeline_routing_fk_dtype.py -q`; full gate `scripts/dev/gates.sh`.

## 9. Slices

- **Slice A (engine, R2, about 80 LOC):** `fk_output_array`, the four call sites, `_python_roundtrip_type` temporal branch, T1, T3, T4, T5 to T8. Ships alone.
- **Slice B (engine, R2, about 25 LOC):** the temporal join token in `fk_join_key`, T2, T9. Ships alone or stacked on A; the owner may defer it, in which case T2 stays recorded red as a known false-orphan shape.
- Rounds: at most two. The known third-round risk is a reviewer asking for Arrow-native (no Python) output assembly; that is a larger redesign of the oracle-parity rules in `_join.py:337-458` and is explicitly not this plan.

## 10. Pre-mortem

- The full-frame reference itself drifts types (`us` and `ms` sources come back as `ns` through pandas): T1 and T3 compare values with `Array.equals` after casting the full_frame column to the source unit, and assert the out-of-core type equals the source type for temporal keys; the plan states this reference rule up front so it is not relitigated.
- Explicit `type=` construction rejects a value the guarded cast admits: the numeric branch keeps the guard (T4 `2**54`).
- DuckDB on another version stores `timestamp[ns]` as microseconds: the relation footer is read as the authoritative masked type (`_batch_join.py:134-140`), so a `us` relation would type the output `us` and T1 would fail loudly rather than drift silently; the worker records the DuckDB version in the evidence.
- A reviewer flags the `TS` token as a determinism change: it is a join-key encoding, not a derive input (`_fk_keys.py:3-6`); T9 pins every existing token.
- `_stream_join.py` crosses its census entry: the helper lives in `_join.py`, the stream joiner only replaces one call.
- The matrix exposes further pre-existing divergences (int widening, decimal refusal): recorded in T3's expectation table as current behavior, not fixed here (DE-10 territory).

## 11. Open owner questions

- Q1: Is slice B (mixed-unit false orphans) in slice 0, or recorded as a known shape for a later round? Recommendation: include; it is 25 LOC and the matrix test already covers it.
- Q2: Should the router hazard also divert tz-naive timestamp keys when the relation unit and the child unit differ? Not needed once slice B lands; asked for the record.

## 12. Worker evidence rules

Name the class of every finding and fix the class, not the instance. Write the generated tests first and record them red (T8 file). Before pushing, attack your own diff: mutate each new mechanism once and show the test goes red. Run baselines with `uv.lock` in the same environment as the branch (`scripts/dev/bootstrap.sh`, `scripts/dev/gates.sh --baseline`) and state what you did not run. Never widen a threshold, skip a test or reduce a budget to make a refactor pass. If a defect of a class already in the catalog appears a second time, stop and report it rather than patch again.
