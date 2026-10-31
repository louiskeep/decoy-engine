Status: plan only, revision 2 (Codex round 1 remediation). No implementation or release approval is granted by this document.

Summary: The out-of-core FK join rebuilds every child FK output value from Python scalars by type inference, which collapses `timestamp[ns]` keys to microseconds, and type-tags join tokens by Python class name, which makes equal instants miss each other whenever exactly one side is nanosecond-typed. One typed seam closes both as a single atomic slice: an explicit temporal unification rule (finest unit, safe cast, range refusal) fixes one Arrow type per FK component from the schemas, every output array (including the zero-row table and the REMAP input) is built against a fixed type, and the join token encodes a naive temporal value as exact integer nanoseconds since the epoch.

Baselines: engine `origin/main` `6ce435ce` (2026-10-31, F13 #339 merged), planning branch `plan/slice0` rebased onto it. Every probe ran in the `uv.lock` venv built by `scripts/dev/bootstrap.sh` (Python 3.10.20, pyarrow 24.0.0, pandas 2.3.3, duckdb 1.5.4). Probe scripts and outputs are reproduced inline in section 1; they are not committed.

Rules consulted: dev-rules `00-universal`, `risk-and-exceptions`, `development-loop` (R2 plan contract, Established solutions), `bugfix`, `testing`, `api-and-compatibility`, `data-and-migrations` (Arrow type semantics are a data contract), `architecture`; `DEFECT-CATALOG.md`, `OWNER-DECISIONS-2026-10-30.md` and `HOSTILE-PLAN-REVIEW.md` (decoy-platform `handoffs/queue`); the engine `CLAUDE.md`; `docs/compatibility-contract.md` 3.3 and 3.4; `tests/parity/SEMANTIC_DIFFERENCES.md` (named by `_join.py:313-314`; the worker reads it before adding a row).

# Slice 0, item 2: typed FK output in the out-of-core join (14.1), PLAN (revision 2)

Risk: R2 (wrong-value fix on one route; no persisted format, no public signature, no frozen contract changes; the determinism guarantee is restored, not altered, because the full-frame output is the reference). Gate: Codex.

Threat model: V1. In scope: ordinary data shapes (a nanosecond timestamp key; a parent and child stored at different units; a far date in a `timestamp[us]` column) producing wrong published values, false orphans, or an uncoded crash. Out of scope: hostile Arrow extension types.

## 1. Reproduction on main (yes)

Re-run on `6ce435ce` through the parity harness (`tests/parity/test_out_of_core_fk_parity.py::_run_oracle` is `PandasExecutionAdapter.run`, `::_run_ooc` is `run_fk_out_of_core`; one `passthrough` parent key `pk`, one `passthrough` child key `fk`, `THROWAWAY_KEY_PROVIDER`). Child FK output column, oracle versus out-of-core:

| Shape | Policy | Oracle (type, distinct, orphan rows) | Out-of-core (type, distinct, orphan rows) |
|---|---|---|---|
| parent `ns` 20 keys 1 ns apart, child equal | preserve | `timestamp[ns]`, 20, 0 | `timestamp[us]`, 1, 0 (value loss) |
| same, child shifted 1 ns | warn | `timestamp[ns]`, 20, 1 | `timestamp[us]`, 1, 1 (orphan indistinguishable from a match) |
| parent `us`, child `ns`, equal instants, 12 rows | warn | `timestamp[ns]`, 12, 0 | `timestamp[us]`, 12, 12 (all false orphans) |
| parent `ns`, child `us`, equal instants | warn | `timestamp[ns]`, 12, 0 | `timestamp[us]`, 12, 12 (all false orphans) |
| parent `ms`, child `s`, equal instants | warn | `timestamp[ns]`, 12, 0 | `timestamp[us]`, 12, 0 (values equal, type drifts) |
| zero-row child, parent `ns` | preserve | `timestamp[ns]`, 0 rows | `null`, 0 rows (type drifts) |
| zero-row parent, child `ns` | preserve | `timestamp[ns]`, 20, 0 | `timestamp[us]`, 1, 0 (value loss) |
| `datetime(9999,1,1)` and `datetime(1,1,1)` in `us` | preserve | `timestamp[us]`, 2 | `timestamp[us]`, 2 (works today; a range case for the fix) |
| `date32` | preserve | `date32`, 5 | `date32`, 5 (unaffected) |

Causes, unchanged by F13 and H-1: the join itself is exact (DuckDB round-trips `timestamp[ns]` through the relation parquet and the result batch); the loss is at the Python round trip in `_append_output_batch` (`_join.py:248-335`), which calls `to_pylist()` then `pa.array(component, from_pandas=True)` at `_join.py:302` (infers `timestamp[us]` for `pd.Timestamp`, probed), and the fixed output type is hard-coded `pa.timestamp("us")` in `_python_roundtrip_type` (`_batch_join.py:505-509`), so `_cast_chunks` (`_batch_join.py:527-535`) cannot restore what inference dropped. The false orphans come from `fk_join_key` falling to `\x00OBJ:{type(normalized).__qualname__}:{normalized!r}` (`_fk_keys.py:208`): `as_py()` yields `pd.Timestamp` for `ns` and `datetime.datetime` for `s`, `ms` and `us` (probed), so tokens differ exactly when one side is `ns`; equal instants at `s`/`ms`/`us` already match (same `datetime` repr). The oracle's dict treats all of them as equal (`{us_value: 1}.get(ns_value)` returns 1, probed). Both sides share `fk_join_key_tuple` (`_relation.py:396-405`, `_join.py:230-237`).

The revision 1 route table (`run_pipeline` with `execution_mode` and gate kwargs on `a6e5c252`, out-of-core on forced, large-FK and byte-estimate routes) was not re-run on `6ce435ce` by this revision; neither F13 nor H-1 touches routing or the join, and T1 is the end-to-end route proof the handoff requires. Classification: `timestamp[ns]` value loss, the ns-versus-other-unit false orphan, the zero-row null type and the not-yet-defined mixed-unit output type are the wrong-value defects this plan closes; int widening to `int64`, the `decimal128` and `uint64` refusals and the `us`/`ms` type drift to `ns` on the pandas routes are pre-existing typing divergences outside this plan (DE-10).

## 2. Established solutions

- Temporal unification: Arrow's own schema promotion picks the finer unit for differing timestamp units; `pa.unify_schemas([us, ns], promote_options="permissive")` yields `timestamp[ns]` on pyarrow 24.0.0 (probed), and Arrow's comparison and join kernels cast to the finer unit before comparing. The plan follows that rule exactly and cites it in `_fixed_component_type`'s docstring.
- Range safety: Arrow's safe cast refuses an out-of-range unit conversion (`datetime(9999,1,1)` from `timestamp[us]` into `timestamp[ns]` raises `ArrowInvalid`, probed; the unsafe cast wraps to 1815, which is the silent-drift shape this plan refuses). Typed construction `pa.array(values, type=target)` has the same exact-or-raise property (probed: a `pd.Timestamp` with a nanosecond part into `timestamp[ns]` is exact; `datetime(9999,1,1)` into `timestamp[ns]` raises). The plan maps both raises to the one existing code.
- Canonical instant: pandas `Timestamp.value` (integer nanoseconds since the epoch) and Arrow's int64-since-epoch storage are the established canonical form for an instant; Python `datetime` arithmetic over `timedelta` gives the same integer without an int64 bound (year 1 to 9999, negative epochs), so the token needs no range guard.
- Precedent in this repo for keeping Arrow scalars out of Python: `_external_sort_run.py:65-80` (`max_value` stays a `pa.Scalar` because `as_py` re-encodes `timestamp[ns]` at microseconds). The helper docstring cites it.

## 3. Guarantee (invariants as properties)

- **P1 (typed output).** For every FK component on every out-of-core join surface, and for the zero-row output table, the output array's Arrow type is the type fixed from the schemas by `_resolve_output_types`; no output array is built by inference from Python values. For temporal candidates the fixed type is the unified type of section 4.
- **P2 (value exactness).** For every row, the published child FK value equals the parent's masked key value (matched), the `fk_key_value`-normalized source key (PRESERVE/WARN orphan) or the remap value (REMAP), compared as Arrow scalars after a safe cast to the fixed type. Two source keys that differ in the source differ in the output.
- **P3 (join equality is value equality).** Two FK key components mint the same join token if and only if the pandas oracle's dict treats them as the same key. For naive temporal values: same instant at any unit; `date` and `datetime` never fold (Python: `date(2020,1,1) == datetime(2020,1,1)` is False, probed).
- **P4 (fail closed, same code).** Any value a fixed type cannot hold exactly, including a timestamp outside the unified unit's range, raises `out_of_core_fk_key_dtype_unsupported` before the batch is emitted; never a raw `ArrowInvalid` and never a wrapped value.
- **P5 (parity reference).** The reference for P1 and P2 is the full_frame route's output cast (safe) to the fixed type, not the whole-child test oracle `mask_child_fk`.

## 4. Design (one structural fix per class, one atomic slice)

- **Temporal unification rule (new, in `_fixed_component_type`, `_batch_join.py:413-453`).** Let the non-null candidates for one component be C. If every member of C is a tz-naive `timestamp[u]`, the fixed type is `timestamp[finest(u)]` with ns > us > ms > s. If C mixes `timestamp` with `date32`, with a tz-aware timestamp, or with a non-temporal type, or any member is tz-aware, the existing refusal applies (`FK_KEY_DTYPE_UNSUPPORTED_CODE`; tz-aware is already refused by `_python_roundtrip_type` at `_batch_join.py:505-508`). `date32` unifies only with `date32`. The rule lives in this one function because both joiners (`_batch_join.py:140-145`, `_stream_join.py:188-193`) and the router hazard dry run (`_pipeline_routing_fk_dtype.py:55-60`) resolve types through it; a mixed-unit edge is therefore never a hazard diversion.
- **Unit preservation.** `_python_roundtrip_type` (`_batch_join.py:479-517`) returns `dtype` itself for a tz-naive timestamp (any unit) and for `date32`; `_fk_key_value_roundtrip_type` (`_batch_join.py:456-477`) inherits it.
- **Range rule (P4).** Every write into a fixed temporal type goes through typed construction or Arrow's default safe cast; an out-of-range value raises `ArrowInvalid`, which the seam maps to `FK_KEY_DTYPE_UNSUPPORTED_CODE` with a message naming the two Arrow types only. This is data-dependent (like the `2**53` guard at `_join.py:379-421`), so it is a run-time refusal recorded in `SEMANTIC_DIFFERENCES.md`, not a routing hazard.
- **Typed construction seam.** One helper `fk_output_array(values: list[object], target: pa.DataType) -> pa.Array` in `_join.py` (469 LOC; it stays under 600). Rule: for temporal, string, binary and decimal targets construct with `pa.array(values, type=target, from_pandas=True)` (exact or raising, probed); for numeric, boolean and null targets keep inference plus the existing `cast_fk_chunk` representability guard (`_join.py:379-421`), because explicit construction refuses exactly representable integers beyond 2**53 that the guard admits and silently truncates `2.5` into `int64` (probed). The helper is the only place that turns FK Python values into an Arrow array, and the only place that maps `ArrowInvalid`, `ArrowTypeError` and `OverflowError` to the code (the block at `_join.py:303-333` moves into it).
- **Call sites replaced (every inference site in the join family):** `_append_output_batch` (`_join.py:302`), which gains a required `output_types: tuple[pa.DataType, ...] | None` (None keeps inference for the whole-child oracle only); `ChildFkBatchJoiner._batch_remap_values` (`_batch_join.py:347`); the stream joiner's remap (`_stream_join.py:639`); the whole-child `_remap_values` (`_batch_join.py:565`); `empty_output_table` (`_emit.py:204`), which types each FK component with `joiner.output_types[component]` from the `fk_components` map it already receives instead of `pa.array([], from_pandas=True)`.
- **Remap input type, separate from the output type.** Each joiner computes `_remap_input_types[idx] = _fk_key_value_roundtrip_type(child_key_types[idx])` at construction (the normalized child key's own type, unit preserved) and `_batch_remap_values` builds `fk_output_array(normalized, input_type)` before `mask_column`; `_remap_values` does the same from `source_child.schema`. The REMAP output candidate stays `_python_roundtrip_type(masked_output_type(seed, input_type))` as today (`_batch_join.py:397-406`), so a numeric child remapped through a string-producing strategy keeps a numeric strategy input and a string output type.
- **Temporal join token.** `fk_join_key` (`_fk_keys.py:173-208`) gains one branch before the `OBJ` fallback: a naive `datetime.datetime` (including `pd.Timestamp`) that is not NaT encodes as `\x00TS:{n}` where `n` is exact integer nanoseconds since 1970-01-01T00:00:00: `value.value` for a `pd.Timestamp`, else `(value - EPOCH)` as a `timedelta` expanded to `days * 86_400_000_000_000 + seconds * 1_000_000_000 + microseconds * 1_000` (Python int, no int64 bound, negative for pre-epoch instants); `datetime.date` that is not a datetime encodes as `\x00DATE:{toordinal()}`. NaT (`value != value`) and tz-aware values keep today's `OBJ` path (tz-aware is refused up front; NaT never arrives from Arrow, whose nulls are `None`, pinned by T9). Both sides of the join share `fk_join_key_tuple`, so one change covers both.
- **Multi-parent (accepted divergence, pinned).** `merge_parent_relations` (`_multi_parent.py:106-132`) keeps refusing members whose masked key types differ, including differing units, with the same code, and the router hazard keeps diverting such jobs to sequential when byte-estimate routing is on (`_pipeline_routing_fk_dtype.py:71-77`). Reason: the merged relation is a DuckDB UNION over member parquet files, so unifying units there means rewriting member relations on disk, a second mechanism for a shape the sequential route already runs; it is recorded as a `SEMANTIC_DIFFERENCES.md` row and pinned by T3.
- **Not changed:** `mask_child_fk` (test oracle, `_join.py:61-174`), `_concat_fk_chunks`, `cast_fk_chunk`, the int widening to `int64`, the decimal and `uint64` refusals, the pandas routes.

## 5. Class table

| # | Class | Closing structure | Proving test | Mutation that must turn it red |
|---|---|---|---|---|
| 1 | Value-bearing output | Error text in the helper and in the unification rule names Arrow types only, as today (`_join.py:324-332`, `_batch_join.py:413-453`); no key value and no `repr` of a scalar is formatted; the coded error is raised after the `except` block (not `from None`, which keeps `__context__`) because pyarrow's own text echoes the value | T6: a marker string key forced into an `int64` target, and a marker-dated far timestamp into an `ns` target; assert the marker is absent from `str(exc)`, `exc.args`, `__cause__`, `__context__` | Format `values[0]` into the message, keep `from exc`, or raise inside the `except` block: T6 fails |
| 2 | Cleanup missing on an exit path | Unchanged owner: the joiner constructor releases its DuckDB connection on any raise (`_batch_join.py:151-171`); the helper raises before `output_chunks` is appended; the range refusal raises before `_replace_fk_columns` | T7 exit matrix: refusal in the helper mid-batch (unrepresentable value, out-of-range timestamp) leaves `output_chunks` empty and the connection closed after `close()`; the stream joiner's staged state is unchanged | Append before constructing: T7 fails |
| 3 | State machine and concurrency | N/A: the join is single-threaded per edge and adds no state | | |
| 4 | Route parity and silent fallback | One helper used by every out-of-core join surface (resident batch joiner, sink-batch, sink-reorder stream joiner, REMAP minting, zero-row table); the unification rule is shared with the router hazard, so a mixed-unit edge is neither diverted nor refused by the router | T1 route matrix over the three out-of-core modes and the three gate paths (forced, large_fk, byte_estimate) against full_frame, for `ns` keys and for one mixed-unit direction each; T2 asserts `route_reason` is the out-of-core reason, never a hazard diversion | Leave one call site on `pa.array(..., from_pandas=True)`: that cell of T1 fails. Make the hazard treat mixed units as a reject: T2's route assertion fails |
| 5 | Referential integrity | P2 and P3; the temporal token; typed output; the unification rule | T1 (ns keys), T2 (mixed units, both directions, false orphans), T3 key-type matrix under PRESERVE, WARN, FAIL, REMAP; single and composite edges; null positions; multi-parent refusal pinned | Revert the `TS` token: T2 fails. Revert `_python_roundtrip_type` for timestamps: T1 fails. Pick the coarsest unit in the rule: T2's ns-child cell loses values and fails |
| 6 | Untrusted producer | Explicit typed construction is strict (raises on loss); the numeric branch keeps the guarded cast; the range rule refuses what the unified unit cannot hold | T4 helper corpus: each family with an unrepresentable value raises the one code; `2.5` never lands in an `int64` target silently; `datetime(9999,1,1)` from `us` into a unified `ns` type raises the code, never `ArrowInvalid`, and never wraps | Construct numeric targets with `type=` directly: the `2**54` case fails or the `2.5` case silently truncates and T4 fails. Use `safe=False`: the far-date cell publishes 1815 and T4 fails |
| 7 | TOCTOU on a path | N/A: relation parquet paths and temp dirs are unchanged | | |
| 8 | Weak tests and overclaimed evidence | T1 to T5 and T10 recorded red on `6ce435ce`; parity asserted against full_frame outputs with `Array.equals` after a safe cast to the fixed type (exact on values and nulls), never `to_pylist` equality | T8 non-vacuity: every new assertion fails on main with the recorded value; red baseline file `docs/plans/evidence/2026-10-31-slice0-ooc-red-baseline.txt` | Replace `Array.equals` with a `to_pylist` compare: the us/ns case passes vacuously and the sentry in T8 fails |
| 9 | Behavior-preserving refactor drift | Characterization first: the existing oracle battery (`tests/unit/execution/test_out_of_core_join_chunked.py`, `test_out_of_core_batch_join.py`, `tests/parity/test_out_of_core_fk_parity.py`) runs unchanged; `mask_child_fk` keeps inference; `merge_parent_relations` keeps its refusal | T5: existing suites green before and after; int, float, bool, string, binary, decimal cells byte-identical; T3's multi-parent cell | Change the int widening or the bool rule: the existing battery fails. Unify units in `merge_parent_relations`: T3's pinned refusal fails |
| 10 | Frozen contract drift | No public signature or format changes; `fk_join_key` tokens for non-temporal types are byte-identical (snapshot) | T9 token snapshot over int, float, Decimal, str, bool, None, NaT and the new temporal cases; T10 property extension | Change any existing token prefix: T9 fails |

## 6. Enumerations from the code

### 6.1 Exit paths

- Joiner construction: typing refusal (including the new mixed-family and tz-aware cases) before the connection exists (`_batch_join.py:140-151`), connection released on any raise after (`_batch_join.py:151-171`); stream joiner mirrors it with a lazily opened connection (`_stream_join.py:188-212`).
- Per batch: FAIL anti-join raise before output (`_batch_join.py:323-328`); helper refusal with `FK_KEY_DTYPE_UNSUPPORTED_CODE` (moved from `_join.py:303-333` into `fk_output_array`), now also for an out-of-range timestamp; `cast_fk_chunk` refusal (`_join.py:409-419`); `_unified_chunk_type` decimal refusal (`_join.py:424-448`).
- Zero-row stream: `empty_output_table` (`_emit.py:183-205`, callers `_runner.py:528`, `_stream_driver.py:474`, `_emit.py:144`) raises nothing new; it reads the joiner's already-resolved types.
- Multi-parent: `merge_parent_relations` refusal before any merged file exists (`_multi_parent.py:116-132`).

### 6.2 Routes

- Route decision (`_pipeline_routing.py:510-535` forced modes; `:572`, `:605` byte-estimate bounded; `:634` large FK) and the hazard diversion to sequential (`:588-590`, only with byte-estimate routing; the hazard dry-runs `_resolve_output_types` at `_pipeline_routing_fk_dtype.py:55-60`).
- Out-of-core execution modes: resident (`out_of_core/_runner.py`, `ChildFkBatchJoiner`), sink-batch, sink-reorder (`_stream_join.py`, `_stream_driver.py`); the parity tests name them `resident`, `sink-batch`, `sink-reorder` (`tests/parity/test_phase_d_d1a_hash_disposition.py`).
- Unaffected: full_frame and sequential (pandas adapter, `_fk_keys.py:11-14`), mixed (generate plus mask).

### 6.3 FK shapes

- Single-column edge: the reproduction. Composite tuple: `fk_join_key_tuple` length-prefixed framing (`_fk_keys.py:211-220`), the `TS` token rides inside it; a composite with one temporal and one integer member unifies per component; composite child masked as scalar seeds is already refused (`_batch_join.py:42-56`).
- Multi-parent merged relation needs one masked type across parents (`_multi_parent.py:23-29`, `_pipeline_routing_fk_dtype.py:71-77`); two parents at different units stay refused or diverted (section 4), pinned by T3.
- Chain-intermediate keys are diverted to sequential before out-of-core (`_pipeline_routing_fk_dtype.py:60-70`).
- Children in later streamed batches (`_stream_join.py` cursors), null child keys (`_join.py:278-280`), zero-row children and zero-row parents (`_emit.py:140-150`) keep their paths; the last two now carry the fixed type.

### 6.4 Fields

- `ChildFkBatchJoiner.output_types`, `.observed_types` (`_batch_join.py:177-193`); new private `_remap_input_types` on both joiners; the stream joiner's `output_types` (`_stream_join.py:210-212`).
- `QualityWarning` for `orphan_fk` with `detail.orphan_rows` and the edge names (`_join.py:45-58`).
- `ExecutionError.code`, `.message` for the refusals above; `quality_metrics["execution"]["execution_mode"]` and `["route_reason"]` (`_pipeline_route_exec.py:91-112`).

### 6.5 Output surfaces

- The child table's FK column (data plane): type and values, including the zero-row case.
- `orphan_fk` warning counts (owner result) and the FAIL policy's `orphan_fk_violation_error` (`_join.py:131-132`).
- Refusal messages naming Arrow types only (`_join.py:324-332`, `:411-418`, `:442-447`, `_batch_join.py:413-453`, `_multi_parent.py:122-131`).
- `quality_metrics["execution"]` route fields.

## 7. Reuse inventory

- `fk_key_value`, `fk_join_key`, `fk_join_key_tuple`, `_decimal_join_token` (`_fk_keys.py:76-220`); `cast_fk_chunk`, `_unified_chunk_type`, `_concat_fk_chunks` (`_join.py:337-458`); `_resolve_output_types`, `_fixed_component_type`, `_fk_key_value_roundtrip_type`, `_python_roundtrip_type`, `_cast_chunks` (`_batch_join.py:367-535`); `FK_KEY_DTYPE_UNSUPPORTED_CODE` (no new reason code).
- `pa.unify_schemas(..., promote_options="permissive")` as the cited rule (not called at run time: the candidates are a set of types, not schemas; the rule is three lines).
- Test harnesses: `tests/parity/test_out_of_core_fk_parity.py::_run_oracle`, `::_run_ooc`, `::_build_single_edge` (extended with timestamp kinds and units), `tests/parity/_d1a_hash_data.py::run_chain` and `mode_kwargs` for the three out-of-core modes, `tests/unit/execution/test_out_of_core_routing.py::_fk_ooc_config` (`:220-276`) for the gate paths, `tests/unit/execution/test_pipeline_routing_fk_dtype.py` corpus (`:85-122`, `naive_timestamp` cleared, `tz_timestamp` rejected), `tests/property/test_fk_keys_invariants.py::scalar_values` (`:111`) and its two-sided properties (`:197-219`, `:334-380`).
- Sentries: `tests/sentry/test_module_size.py` (`_stream_join.py` is at its census entry 650, `_batch_join.py` 569, `_join.py` 469; the helper lives in `_join.py` so `_stream_join.py` only replaces one call) and `tests/sentry/test_reason_registry.py`.

## 8. Frozen contracts touched

- Determinism guarantee (`docs/compatibility-contract.md` 3.3): the out-of-core child output for temporal keys changes from the wrong value to the full-frame value; no seed-protocol input changes (`fk_join_key` is not a derive input, `_fk_keys.py:3-6`).
- Public API (3.4): no signature changes; `_append_output_batch`, `fk_output_array` and `empty_output_table` are private.
- No persisted format is touched. `SEMANTIC_DIFFERENCES.md` gains two rows (run-time range refusal; multi-parent mixed units).

## 9. Acceptance tests, red first (hostile corpus)

Recorded red on `6ce435ce` before any fix, in the locked venv.

- T1 `tests/parity/test_out_of_core_fk_parity.py::test_ns_timestamp_fk_matches_full_frame[mode x gate]`: end to end through `run_pipeline` with the real gate; `Array.equals` between the full_frame child FK column (cast to the fixed type) and the out-of-core column, type equals the source unit, and `set(child) <= set(parent)` on the out-of-core output; three modes times forced, large_fk and byte_estimate gates.
- T2 `::test_mixed_unit_timestamp_keys_are_not_orphans[direction x policy x nulls x shape]`: directions `us->ns`, `ns->us`, `ms->s`, `s->ns`; policies PRESERVE, WARN, FAIL (completes), REMAP; null positions none, first, last, all-null child; single and composite (timestamp plus int) edges; zero `orphan_fk`, fixed type equals the finest unit, values equal full_frame after a safe cast; plus one direction crossed with the three modes and three gates, asserting `route_reason` is the out-of-core reason (no hazard diversion). The ns-only shifted-by-1-ns case keeps exactly one orphan.
- T3 `::test_fk_key_type_matrix[type x policy x shape]`: `timestamp[s|ms|us|ns]`, `date32`, `string`, `int8..int64`, `uint32`, `float64`, each with a null key, under PRESERVE, WARN, FAIL, REMAP, single and composite edges; expectation per cell is the full_frame output (or the recorded refusal code for `decimal128`, `uint64`, `date32` mixed with `timestamp`, tz-aware, and a multi-parent edge whose members differ in unit).
- T4 `tests/unit/execution/test_out_of_core_join_chunked.py::test_fk_output_array_is_exact_or_raises`: helper corpus from the class table, including `2**54` admitted into `float64`, `2**53+1` refused, `2.5` never silently into `int64`, `Decimal("1.234")` refused by `decimal128(4,2)`, `datetime(9999,1,1)` into `timestamp[ns]` refused with the code and no cause, `pd.Timestamp` with a nanosecond part into `timestamp[ns]` exact, a `datetime` into `timestamp[ns]` exact; and `empty_output_table` yields the fixed type for a zero-row child in both unit directions.
- T5 characterization: the existing join suites green before the change (command recorded).
- T6 marker test, T7 exit matrix, T8 red baseline, T9 token snapshot as named in the class table.
- T10 `tests/property/test_fk_keys_invariants.py`: `scalar_values()` extended with naive `datetime`s over the full year 1 to 9999 range, in-range `pd.Timestamp`s with nanosecond parts, and `date`s (NaT excluded from the strategy and pinned in T9), so the existing two-sided consistency and injectivity properties (`:197-219`, `:334-380`) cover temporal values; a new unit-pair property: for every unit pair in {s, ms, us, ns} squared and an instant representable in both, `fk_join_key` of the two `as_py()` values is equal, and for two distinct instants it differs; far dates at s, ms and us; negative epochs; `date` versus midnight `datetime` never equal; tz-aware stays on the `OBJ` path.
- Commands: `.venv/bin/python -m pytest tests/parity/test_out_of_core_fk_parity.py tests/unit/execution/test_out_of_core_join_chunked.py tests/unit/execution/test_out_of_core_batch_join.py tests/unit/execution/test_pipeline_routing_fk_dtype.py tests/property/test_fk_keys_invariants.py -q`; full gate `scripts/dev/gates.sh`.

## 10. Slices

- **One slice (engine, R2, about 120 LOC):** `fk_output_array`, the five call sites, the temporal unification rule in `_fixed_component_type`, `_python_roundtrip_type` unit preservation, `_remap_input_types`, the `TS`/`DATE` token, the two `SEMANTIC_DIFFERENCES.md` rows, T1 to T10. Ships alone and atomically: typed output without the token leaves mixed-unit false orphans, the token without typed output leaves ns value loss, and unit preservation without the unification rule turns every mixed-unit edge into a refusal (the review's blocker), so no narrower slice has a guarantee worth stating.
- Rounds: at most two. The known third-round risk is a reviewer asking for Arrow-native (no Python) output assembly; that is a larger redesign of the oracle-parity rules in `_join.py:337-458` and is explicitly not this plan.

## 11. Pre-mortem

- The full-frame reference itself drifts types (`us`, `ms` and `s` sources come back as `ns` through pandas, probed): T1, T2 and T3 compare values with `Array.equals` after a safe cast of the full_frame column to the fixed type, and assert the out-of-core type equals the fixed type; the reference rule is stated in P5 so it is not relitigated.
- A `us` parent holds a far date and the child is `ns`: the unified type is `ns` and the parent value is out of range; the run refuses with the code at the first batch that carries it (P4), never publishes 1815, and the row is in `SEMANTIC_DIFFERENCES.md`. The pandas oracle completes this shape (it keeps `us` values in a `datetime64[us]` frame), so this is a documented divergence, not silent drift; T4 pins it.
- Explicit `type=` construction rejects a value the guarded cast admits: the numeric branch keeps the guard (T4 `2**54`).
- DuckDB on another version stores `timestamp[ns]` as microseconds: the relation footer is read as the authoritative masked type (`_batch_join.py:134-140`), so a `us` relation would unify to the child's unit or type the output `us`, and T1 would fail loudly rather than drift silently; the worker records the DuckDB version in the evidence.
- A reviewer flags the `TS` token as a determinism change: it is a join-key encoding, not a derive input (`_fk_keys.py:3-6`); T9 pins every existing token.
- NaT: `fk_key_value(pd.NaT)` returns NaT (probed) and NaT is an instance of `datetime`, so a naive TS branch would mint a token for it; the branch excludes `value != value`, and T9 pins that NaT keeps the `OBJ` token and that an Arrow null arrives as `None`.
- `_stream_join.py` crosses its census entry: the helper lives in `_join.py`, the stream joiner only replaces one call and adds one tuple field.
- The matrix exposes further pre-existing divergences (int widening, decimal refusal): recorded in T3's expectation table as current behavior, not fixed here (DE-10 territory).

## 12. Open owner questions

- Q1: Should the router hazard divert tz-naive mixed-unit edges to sequential instead of unifying them out-of-core? Recommendation: no; unification is three lines, the hazard dry-runs the same function, and sequential is the slower route.
- Q2: Is the multi-parent mixed-unit refusal (section 4) acceptable for V1, or should a follow-up unify member relations? Recommendation: accept and pin; no customer shape has asked for it.

## 13. Review history

Codex round 1 on `c97bf0e9` (verdict NO-GO: 2 BLOCKER, 2 HIGH, 3 MEDIUM), verified against `6ce435ce`:

- BLOCKER unit preservation makes mixed units raise in `_fixed_component_type` before the token matters: confirmed in the code (`_batch_join.py:385-392` collects `{us, ns}`, `:413-453` refuses) and fixed with the exact temporal unification rule (finest unit, safe cast, range refusal) in section 4; P3 and T2 are consistent with it.
- BLOCKER slices A and B not independent: fixed by merging them into one atomic slice (section 10) with the reason stated.
- HIGH `empty_output_table` builds FK columns by inference: confirmed (`_emit.py:204`; a zero-row child yields a null-typed column while full_frame yields `timestamp[ns]`, probed). Fixed by typing the zero-row FK columns from the joiner's fixed types (section 4, T4), so P1 covers every output array.
- HIGH token protocol lacks range and property evidence: fixed. The token is specified as exact Python-int nanoseconds (section 4), with s/ms/us/ns, negative epochs, far dates, both directions, NaT and tz-aware cases in T10's property extension and T9's snapshot.
- MEDIUM remap helper's target type unspecified: fixed with `_remap_input_types` separate from `_output_types` (section 4), applied to both joiners and the whole-child `_remap_values`.
- MEDIUM multi-parent units: decided as an accepted, pinned refusal with the reason stated (section 4, T3, `SEMANTIC_DIFFERENCES.md` row).
- MEDIUM mixed-unit coverage too narrow: fixed. T2 crosses direction, policy, null position and shape, plus modes and gates for one direction.
- Missing canonical rulebooks and Established solutions: fixed (Rules consulted line, section 2).
- Self-attack of the same classes elsewhere: the false-orphan class is specifically ns versus any other unit (s/ms/us already agree, probed), which narrowed the token design; the `ms`/`s` mixed shape that works today would have become a refusal under unit preservation without the unification rule, so the rule covers every unit pair, not only ns; `ArrowInvalid` text echoes the offending value, so the helper raises the coded error after the `except` block (class 1 row).

## 14. Worker evidence rules

Name the class of every finding and fix the class, not the instance. Write the generated tests first and record them red (T8 file). Before pushing, attack your own diff: mutate each new mechanism once and show the test goes red. Run baselines with `uv.lock` in the same environment as the branch (`scripts/dev/bootstrap.sh`, `scripts/dev/gates.sh --baseline`) and state what you did not run. Never widen a threshold, skip a test or reduce a budget to make a refactor pass. If a defect of a class already in the catalog appears a second time, stop and report it rather than patch again.
