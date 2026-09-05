Status: plan

# Q3 slice 2: widen the native lane to integer, boolean, and timestamp columns

Slice 1 shipped the single-pass streaming native lane for `passthrough` / `redact` /
`truncate` on `utf8` columns only. It stayed single-pass by admitting exactly the type whose
oracle output never depends on the column's null state. This slice adds integer, boolean, and
timestamp, which do have null-driven oracle behavior, by building the two things slice 1
deferred: a bounded-memory preflight that resolves each column's global null and empty state,
and a source-snapshot identity digest so the preflight verdict is still true when execution
reads the source a second time. The strategy set and the default-off posture are unchanged;
only the admitted column types widen.

The masking machinery already handles these types; the difficulty is entirely physical-schema
parity. The pandas oracle round-trips masked output through `pa.Table.from_pandas`, whose type
inference depends on data a single first-batch peek cannot see (documented in
`native/_kernels_scalar.py`). The correct behavior, confirmed by probing pandas 2.3.3 /
PyArrow 24.0.0 against the native kernels, is the matrix in section 3; several cells are safer
than a naive reading suggests (a nullable boolean re-infers to Arrow `bool`, `NaT` does not
alter a timestamp type, an empty column preserves its type), which is exactly why the preflight
must distinguish partial-null from all-null rather than collapsing both to "has a null".

## 1. What changes and what does not

Unchanged: the layer-2 seam (`maybe_run_native_route`), the reject-before-output closed world,
the literal streaming-sink predicate, the `native_route_enabled` runtime option (default False),
the invocation-scoped route ledger, the transactional stage/drain/validate/commit lifecycle, the
structured result and telemetry, and byte + physical-schema parity to the pandas oracle. This
slice adds no strategy, does not flip the default, and does not touch the frozen Part-1 or FK
gates. The integer-null compile guard (`check_null_bearing_int_unsupported`) and the execution
guard stay exactly as they are (section 6).

Changed: admission stops being schema-only for the added types. A column of an added type is
admitted only after a preflight pass has resolved that its global null/empty state lands on a
parity-safe matrix cell, and execution is gated on the source being byte-identical to the one
the preflight read.

New module: the preflight state accumulator, the matrix resolver, and the digest codec live in a
new leaf `execution/_native_route_preflight.py` (target <= 600 LOC). `_native_route.py` (381) and
`_native_route_exec.py` (544, only ~56 headroom) gain small call sites, not the bulk;
`_pipeline.py` (639, ceiling 645) receives no new logic.

## 2. The bounded preflight

Before any output, the lane runs one streaming pass over the runtime source
(`LazySource.iter_batches`) that accumulates, per column, `total_rows`, `null_count`, and the
physical Arrow type; it freezes the first preflight batch's schema and rejects any later batch
whose column names, order, or types drift from it. This is O(columns) state plus the one bounded
active batch, no materialization. From the accumulated counts each column resolves to exactly one
state:

- `empty`: `total_rows == 0`
- `no-null`: `null_count == 0 and total_rows > 0`
- `partial-null`: `0 < null_count < total_rows`
- `all-null`: `null_count == total_rows and total_rows > 0`

The preflight then resolves the admit/reroute decision and the oracle-equivalent output schema
per the matrix (section 3), and freezes that output schema before the first sink write. It is a
second read of the runtime source (execution being the first); that is the honest cost of exact
parity for global-state-dependent types. "Read exactly twice" in this plan means the
caller-supplied runtime `LazySource.iter_batches`, distinct from the pipeline's own bounded
profiling reads, which are unchanged.

## 3. The physical-parity admission matrix (normative)

Resolved from each column's `(state, type)`. Admit = native; Reroute = the whole table goes to
the oracle before output; "Oracle rejects" = the reroute lets the existing oracle guard raise its
own coded error. This table is normative and was validated by an oracle-vs-native probe on
pandas 2.3.3 / PyArrow 24.0.0; the build re-derives each cell with a parity test and may only
widen an Admit to a Reroute (never the reverse) if a probe on the pinned versions disagrees.

| strategy / type       | no-null | partial-null   | all-null        | empty  |
|-----------------------|:-------:|:--------------:|:---------------:|:------:|
| passthrough / integer | Admit   | Reroute (double) | Reroute (null) | Admit  |
| passthrough / boolean | Admit   | Admit          | Reroute (null)  | Admit  |
| passthrough / timestamp | Admit | Admit          | Admit           | Admit  |
| redact / int,bool,ts  | Admit   | Admit          | Reroute (null)  | Reroute (double) |
| truncate / integer    | Admit   | Oracle rejects | Oracle rejects  | Reroute (double) |
| truncate / boolean    | Admit   | Admit          | Reroute (null)  | Reroute (double) |
| truncate / timestamp  | Admit   | Admit          | Reroute (null)  | Reroute (double) |

Notes that fix the earlier draft:

- A nullable boolean is `object` only inside pandas; `pa.Table.from_pandas` re-infers Arrow
  `bool`, so partial-null boolean passthrough is exact (Admit), not drift.
- `NaT` does not change a timestamp's Arrow type; units `s/ms/us/ns`, UTC, a DST-aware zone, and a
  fixed offset all survive, so partial-null and all-null timestamp passthrough are exact.
- An empty column preserves integer width, boolean, and timestamp unit/timezone under
  passthrough, so empty passthrough is exact for all three.
- Native `truncate` accepts integer, boolean, and timestamp (`truncate_array` converts each
  non-null scalar with `str(value)`); its output matched the oracle for the admitted cells.
- The all-null Reroute cells are where the oracle infers `null` while native pins a concrete type
  or `string`; the empty Reroute cells for redact/truncate are where the oracle infers `double`
  while native pins `string`. Both are drifts the harness does not allow, hence Reroute.

Integer covers all signed and unsigned widths; timestamp covers all units, tz-aware and
tz-naive. The reject-before-output list from slice 1 keeps every entry and gains: a column whose
`(strategy, type, state)` is not an Admit cell.

## 4. Source-snapshot identity digest

`LazySource` reopens the Parquet file on every read, so preflight and execution are not pinned to
one file generation; a source mutated between them could present the same footer with different
values and commit outside the parity contract. Footer metadata (row count, schema, size, mtime)
is not content identity and is not the check.

The lane computes a versioned, domain-separated digest over the logical stream during the
preflight pass and recomputes it during execution, then compares the two after the execution
iterator is fully drained and before `commit()` or a `sink=None` resident return; a mismatch
aborts, discards any staged artifact, and raises a coded error, never committing. The codec is
specified so the encoding is injective (a cryptographic hash does not rescue ambiguous framing):

- Algorithm: BLAKE2b, keyed with a fixed domain-separation constant and a format-version byte.
- The hash is updated directly from Arrow buffers / memoryviews per batch; `to_pylist()`,
  `combine_chunks()`, and whole-stream IPC materialization are forbidden in the digest path.
- Per column, framed with explicit lengths: the field name, the Arrow type (including timestamp
  unit and timezone and integer signedness/width), then the validity bitmap and the value
  buffers, each length-prefixed. Length prefixes make concatenation unambiguous.
- The running total is `total_rows` and the frozen column order, so a row permutation, a
  validity-only change, a timezone change, or a reordered schema all change the digest.
- The digest is partition-independent: it is a fold over the logical column stream, not over
  batch boundaries, so preflight and execution reading the same file at different batch sizes
  produce the same digest. Batch partitioning is explicitly not part of identity.

Memory is O(columns) digest state plus the bounded active batch, not literal O(1). As defense in
depth the per-batch guards still fire, and the first execution batch is checked against the
frozen preflight schema (not only later batches): an integer column the preflight cleared as
null-free that presents a null at execution aborts rather than emitting a divergent batch.

A private spool of the source is a stronger anti-mutation guarantee but creates a
raw-PII-at-rest, disk-budget, permission, and cleanup obligation; the digest is preferred unless
the benchmarks in section 6 show it is too costly, at which point the spool is the fallback.

## 5. Failure modes

1. A column whose cell is Reroute (partial/all-null int passthrough, all-null bool/ts passthrough,
   all-null redact, empty redact/truncate, and any probe-failing cell) reroutes to the oracle
   before output. Not an error.
2. A truncate integer column with a null: if the null is in the profile sample the existing
   compile guard raises `PlanCompileError` before native is reached; otherwise the native
   preflight reroutes and the existing execution guard raises `ExecutionError`. Both paths are
   preserved unchanged (section 6).
3. `native_route_enabled=False` or a non-`auto` execution mode: native is never selected.
4. A source whose digest changes between preflight and commit, or a per-batch guard trip: abort,
   discard staged output, coded error, no oracle retry.
5. Parity divergence native vs oracle: a gate failure caught before ship.

## 6. Interaction with the existing integer-null guards

`check_null_bearing_int_unsupported` runs at plan compile, before `maybe_run_native_route`, and
rejects a null-bearing integer under `{truncate, hash, categorical}` when the null is visible in
the 10,000-row profile sample. This slice does not weaken or bypass it. Consequently the matrix's
"truncate / integer / partial-null / all-null = Oracle rejects" cells split by where the null
first appears:

- Null in the profile sample: `PlanCompileError` (unchanged, native never entered).
- Null only after the sample and after the first native batch: the native preflight sees it,
  reroutes, and the existing execution guard raises `ExecutionError`.

The acceptance tests assert the correct one of these per case; neither is weakened to make the
native matrix uniform.

## 7. Acceptance tests

Every test asserts against the pinned pandas oracle through the production entry (`run_pipeline`
with `native_route_enabled=True`). Exact-physical-cell tests pass `allowed_physical_diffs=()` so
the default null-typed normalization cannot silently absorb an all-null drift; a test that is
deliberately consuming that normalization says so.

1. **The full normative matrix, parity through the production entry.** Parameterized over every
   `(strategy, type, state)` cell in section 3: an Admit cell is byte + physical identical (with
   `allowed_physical_diffs=()`) to both the chunked-oracle and full-frame-oracle; a Reroute cell
   produces the oracle result with the coded reason; an "Oracle rejects" cell raises the correct
   existing error. Integer signed + unsigned widths including boundary values (`-2**63`,
   `2**64-1`), boolean, and timestamp units with and without timezone, including negative epoch,
   fractional precision, a DST-aware zone, and a fixed offset.
2. **Preflight resolves global state, not first-batch.** A source whose only null is beyond both
   the 10,000-row profile sample and the first 50,000-row native batch reroutes a partial-null
   integer/boolean passthrough (or raises the execution guard for integer truncate) exactly as an
   all-in-first-batch source does.
3. **Partial-null vs all-null are distinguished.** A `[value, null]` column and a `[null, null]`
   column of the same type resolve to different states and different admit/reroute decisions.
4. **Source-snapshot digest.** A `LazySource` subclass that rewrites the same Parquet path between
   its two `iter_batches` calls (fixed-width same-size values, restored mtime, changed values or
   validity) aborts before commit: exactly two runtime iterator calls, coded failure, no final
   artifact, staging cleaned, no third or oracle read. Plus codec unit tests: ambiguous byte
   concatenations, a validity-only change, a row permutation, a timezone change, and two
   different batch partitions of the same data (which must digest equal).
5. **The native lane provably ran without materialization.** For an Admit job the frozen Part-1
   ledger counts are all zero, `resolve_resident_sources` is never called on the native path, and
   the runtime source's `iter_batches` is called exactly twice.
6. **Bounded memory + read cost.** Peak RSS through the production entry with the streaming sink
   is flat in row count under the frozen ceiling, measured in fresh processes over tiers (the
   preflight accumulator is O(columns)). Add cold-cache and warm-cache wall-time and read-byte
   benchmarks for the two-read design, so its cost is measured, not assumed.
7. **Every slice-1 guarantee still holds**: `utf8` parity unchanged, the reject-before-output
   closed world, the transactional lifecycle, the precedence matrix, the closed-world sentry.
8. **Mutation bar** on the changed units (preflight accumulator + state resolution, matrix
   resolver, digest codec, widened admission), ledger updated to the accurate score with an honest
   survivor taxonomy.

Non-regression: the full suite stays green; no Part-1 gate or the FK byte-parity contract is
weakened; no new broad `PhysicalDiff`; the module-size sentry passes (new logic in the leaf
module, `_pipeline.py` untouched).

## 8. Scope

In: the preflight state accumulator with the four-state resolution and schema-drift rejection;
the normative admission matrix; the versioned source-snapshot digest codec; the per-batch
defensive guards and first-batch schema check; the new leaf module; and the acceptance suite, all
in normal CI (no companion needed).

Out (later slices): `faker`; keyed `hash` plus publishing the Rust companion (now able to cover
integer columns because this slice's preflight resolves their null state); the default-on flip;
resident and `source_loader` input admission; the P4-C payload ports; the per-value row-error
channel.

## 9. Program sequence (this is slice 2)

1. Slice 1 (done): the single-pass string lane.
2. This slice: integer, boolean, timestamp via the preflight and source-snapshot digest.
3. `faker` on the lane.
4. Keyed `hash` on the lane, plus publishing the Rust companion.
5. The default-on flip.
6. The P4-C payload ports.
