Status: plan

# Q3 slice 2: widen the native lane to integer, boolean, and timestamp columns

Slice 1 shipped the single-pass streaming native lane for `passthrough` / `redact` /
`truncate` on `utf8` columns only. It stayed single-pass by admitting exactly the type whose
oracle output never depends on the column's null state. This slice adds the integer, boolean,
and timestamp types, which do have null-driven oracle behavior, by building the two things
slice 1 deliberately deferred: a bounded-memory preflight that resolves each column's global
null and empty state, and a source-snapshot identity contract so the preflight verdict is still
true when execution reads the source a second time. The strategy set and the default-off
posture are unchanged; only the admitted column types widen.

Why the extra machinery. The pandas oracle round-trips masked output through
`pa.Table.from_pandas`, whose type inference depends on data that a single first-batch peek
cannot see (documented in `native/_kernels_scalar.py`): an all-null column infers `null`, an
empty column infers `double`, and a null-bearing integer under `passthrough` promotes to
`double` while the native kernel keeps `int64`. The int-null guard (`reject_null_bearing_int`,
strategies `{truncate, hash, categorical}`) rejects a null-bearing integer outright after
scanning the whole column. None of these can be decided from one batch, so the lane must learn
the global state before it commits.

## 1. What changes and what does not

Unchanged: the layer-2 seam (`maybe_run_native_route`), the reject-before-output closed world,
the literal streaming-sink predicate, the `native_route_enabled` runtime option (default False),
the invocation-scoped route ledger, the transactional stage/drain/validate/commit lifecycle,
the structured result and telemetry, and byte + physical-schema parity to the pandas oracle
(harness `tests/parity/native/_fixtures.py`, only null-typed normalization allowed, no broad
`PhysicalDiff`). This slice does not add `faker`, `hash`, or any new strategy, does not flip the
default, and does not touch the frozen Part-1 or FK gates.

Changed: admission stops being schema-only. A column of an added type is admitted only after a
preflight pass has resolved that its global null/empty state lands on a parity-safe matrix cell,
and execution is gated on the source being identical to the one the preflight read.

## 2. The bounded preflight

Before any output, the lane runs one streaming pass over the source
(`LazySource.iter_batches`) that accumulates, per column, only `is_empty`, `has_null`, and the
physical Arrow type. This is O(columns) memory, no materialization, and it is the same
per-column-state prepass the out-of-core route already uses in spirit. From that state the lane
resolves the oracle-equivalent output schema and the admit/reroute decision per the matrix in
section 3, and freezes the output schema before the first sink write.

The preflight is a second read of the source (the first being execution). That is the honest
cost of exact parity on a streaming route for types whose oracle output is global-state
dependent; slice 1 avoided it by admitting only the type that needed no such state.

## 3. The physical-parity admission matrix

Resolved from the preflight's `(is_empty, has_null, type)` per column, exhaustive over
`{passthrough, redact, truncate} x {int (all widths, signed + unsigned), bool, timestamp
(all units, tz-aware and tz-naive)} x {non-empty-no-null, non-empty-has-null, all-null,
empty}`. Any cell not proven byte + physical identical to the oracle reroutes the whole table
to the oracle before output. The build derives every cell empirically (an oracle-vs-native
probe) and pins it with a parity test; the cells below are the design's starting position and
the plan-gate's checkpoints, not a substitute for that per-cell verification.

- `redact` is type-agnostic in its VALUE output: it emits `pa.string()` from any input, so a
  non-empty, not-all-null int / bool / timestamp column redacts to the identical string column
  the oracle produces. `redact` admits on all three added types for the non-empty-no-null and
  non-empty-has-null cells. The all-null and empty cells reroute: the oracle infers `null` /
  `double` there while native pins `string`, a drift the harness does not allow.
- `passthrough` preserves type, so it is parity-safe only where the oracle also preserves it:
  the non-empty-no-null cell for every added type (verify timestamp tz-aware and tz-naive both
  hold; NaT is not a value-null promotion the way integer NaN is). The non-empty-has-null cell
  reroutes for integer (oracle -> `double`) and boolean (oracle -> `object`); timestamp
  has-null is admitted only if the probe confirms no drift, else it reroutes. all-null and
  empty reroute.
- `truncate` emits `pa.string()`. A null-bearing integer reroutes because the oracle rejects it
  (`reject_null_bearing_int`), so the reroute lets the oracle raise the identical
  `ExecutionError`. A non-null integer, and boolean and timestamp columns, are admitted only
  where the probe proves the native stringify-then-truncate matches the oracle's; any cell that
  does not is rerouted, not forced. all-null and empty reroute.

The reject-before-output list from slice 1 keeps every existing entry and gains: a column whose
`(strategy, type, null/empty)` cell is not an admitted matrix cell.

## 4. Source-snapshot identity

`LazySource` reopens the Parquet file on every read, so the preflight pass and the execution
pass are not pinned to one file generation; a source mutated between them could present the same
schema with newly-arrived nulls and commit outside the parity contract. Footer metadata
(row count, schema, size, mtime) is not content identity and is rejected as the check.

The lane computes a collision-resistant digest over the logical stream (schema, per-column
validity bitmap, values, and row order) during the preflight pass, and recomputes it during the
execution pass; before commit (and before returning a `sink=None` resident result) it compares
the two. A mismatch aborts, discards any staged artifact, and raises a coded error; it never
commits. On a stable source (the normal case) the digests match and nothing aborts. This is
O(1) memory and one extra hash over the data already being read. As defense in depth, the
per-batch guards still fire: an integer column that the preflight cleared as null-free but that
presents a null at execution aborts rather than emitting a divergent batch.

The digest closes the passthrough integer/boolean case specifically: on an immutable source the
preflight null verdict is trustworthy so a null-free integer passthrough is safe; on a mutated
source the abort is a fail-closed outcome for an abnormal condition, not a parity divergence on
a normal input.

## 5. Failure modes

1. A column whose cell is not admitted (null-bearing int/bool passthrough, null-bearing int
   truncate, all-null, empty, or any probe-failing cell) reroutes to the oracle before output.
   Not an error.
2. `native_route_enabled=False` or a non-`auto` execution mode: native is never selected.
3. A source whose digest changes between preflight and commit, or a per-batch guard trip
   (a null in a cleared column): abort, discard staged output, coded error, no oracle retry.
4. Parity divergence native vs oracle: a gate failure caught before ship.

## 6. Acceptance tests

Every test asserts against the pinned pandas oracle through the production entry (`run_pipeline`
with `native_route_enabled=True`). Physical-schema parity uses the existing harness on the
committed Parquet artifact, no new broad `PhysicalDiff`.

1. **The full admission matrix, parity through the production entry.** Parameterized over every
   `(strategy, type, null/empty)` cell: an admitted cell is byte + physical identical to both
   the chunked-oracle and full-frame-oracle; a rerouted cell produces the oracle result with the
   coded reason. Integer widths (signed + unsigned), boolean, and timestamp units with and
   without a timezone are all covered.
2. **Preflight resolves global state, not first-batch.** A source whose nulls appear only in a
   later batch (a null-free first batch) reroutes an integer/boolean passthrough (or a
   null-bearing-int truncate) exactly as an all-in-first-batch source does, proving the preflight
   saw the whole column.
3. **Source-snapshot digest.** A source whose content changes between the preflight and the
   execution pass (same row count, schema, size, restored mtime, different values or validity)
   aborts before commit with no artifact and no oracle retry.
4. **The native lane provably ran, once materialization is avoided.** For an admitted job the
   frozen Part-1 ledger counts are all zero and `resolve_resident_sources` is never called on
   the native path; the source is read exactly twice (preflight + execution) and no more.
5. **Bounded memory.** Peak RSS through the production entry with the streaming sink is flat in
   row count under the frozen ceiling, measured in fresh processes over tiers; the preflight
   pass does not materialize (its accumulator is O(columns)).
6. **Every slice-1 guarantee still holds**: `utf8` parity unchanged, the reject-before-output
   closed world, the transactional lifecycle, the precedence matrix, and the closed-world sentry.
7. **Mutation bar** on the changed units (preflight, matrix resolution, digest, the widened
   admission), with the ledger updated to the accurate score and honest survivor taxonomy.

Non-regression: the full suite stays green; no Part-1 gate or the FK byte-parity contract is
weakened; the module-size sentry passes (the preflight and digest live in the native modules or
a new leaf, not in `_pipeline.py`, whose 645 ceiling has no headroom).

## 7. Scope

In: the bounded preflight; the source-snapshot digest identity contract; the widened
physical-parity admission matrix for integer, boolean, and timestamp; the per-batch defensive
guards; and the acceptance suite, all in normal CI (no companion needed).

Out (later slices): `faker` (streaming or admission-aligned pool quality); keyed `hash` on the
lane plus publishing the Rust companion; flipping `native_route_enabled` default to True;
resident and `source_loader` input admission; the P4-C payload ports; and the per-value
row-error channel.

## 8. Program sequence (this is slice 2)

1. Slice 1 (done): the single-pass string lane.
2. This slice: integer, boolean, timestamp via the preflight and source-snapshot contract.
3. `faker` on the lane.
4. Keyed `hash` on the lane, plus publishing the Rust companion (now able to cover integer
   columns because this slice's preflight resolves their null state).
5. The default-on flip.
6. The P4-C payload ports.
