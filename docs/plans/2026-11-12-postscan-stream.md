Status: plan (revision 3, 2026-11-12, not built). Engine only. Roadmap row 60 (CLI `docs/ROADMAP.md`).

# POSTSCAN-STREAM: post-scan integrity on every run route, with output key checks

Repo `louiskeep/decoy-engine`, plan branch `plan/postscan-stream`. Base: engine `origin/main` `4bb95063`; CLI `origin/main` `19cc7b1d`. Citations are checked at those heads.

Risk: R2. It blocks before output commits, uses the commit choke point every engine sink uses, and changes routing for relationship and `post_scan.integrity: true` jobs. No public API removal, schema major or lock change.

Rules consulted: `~/dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`, `development-loop.md` (PLAN), `testing.md` (Test depth, escalation triggers), `cross-cutting-concerns.md`, `catalogs/recurring-defects.md` (classes 1 to 11), engine `CLAUDE.md` and `CODEMAP.md` ("Where things go").

Also read: CLI `docs/ROADMAP.md` (rows 57, 58, 60 to 63); F22 rev 11 (5.6, 5.7, slices); SUB-RUN (4.2, 4.3, 10); SCAN-ML (`plan/scan-ml` 4.5, 11, 13; M5 is #508); DELIV (4, 7.4); `docs/compatibility-contract.md` 3.1; `docs/reasons.md`.

## 1. Guarantee and non-goals

The change promises:

1. Every `run_pipeline` route runs the post-scan gate before its output becomes visible: the six
   `ROUTES` (`declared_casting/_eligibility.py:26`; full-frame resident and streamed lanes both),
   stream mode, and `run_pipeline_isolated` (in the child, or in-process on the fallback).
2. When relationships are declared, every declared primary key in the output is unique and
   non-null and every complete child key (MATCH SIMPLE) is in the parent's output keys, measured
   against the same facts over the run's inputs. A breach the run introduced blocks: nothing
   commits and the caller gets no outputs.
3. A required gate that cannot run refuses the commit (fail closed).
4. Results hold counts, table names, column names and reason codes only. No key value, row value
   or DuckDB message text reaches the report, an exception, a log line or a reason record.
5. One function decides "post-scan blocks delivery": `post_scan_verdict` (4.8), covering key
   breaches and every finding whose severity blocks. The verdict lands in
   `quality_metrics["post_scan_integrity"]` (tag `post-scan-integrity/v2`) on every route and, when
   blocking, in one `PostScanBlockedError` carrying the report (4.10).
6. The column suite (residual PII, policy validation) gives exact counts on every route, equal to
   the resident pandas runner today.

Named exemptions (gate not run):

- `generate_tables_streamed` and `generate_tables_streamed_from_generation_plan`
  (`generation/_plan_entry.py:254`, `:326`): direct primitives that never reach `execute_route`.
  The CLI calls `run_pipeline` instead. V2-LOG POSTSCAN-GEN-STREAMED.
- Direct mask primitives (`run_sequential`, `run_fk_out_of_core`, the chunked entries): M5 keeps
  refusing model asks; regex and key checks there are V2-LOG. CLI `--chunked` reaches `_chunked.py`
  until RUN-STREAM C2 moves it to stream mode, which this plan covers. Safe meanwhile through a
  dependency on row 61: the delivery plan refuses `decoy run --chunked` when post-scan integrity is
  requested, and CLI delivery treats a requested post-scan with no report as a block.
- F22 `publish_tables`: it publishes tables a caller already holds from a gated run.

Non-goals (V2-LOG, section 15): NER post-scan on streamed routes; exact per-row attribution; a
`tables[].primary_key` field (Q3); streaming A1 post-validation; the composite-key bug in
`validation/post/_checks/_pk_uniqueness.py`; `validators/_fk_validators.py` on the key core.

## 2. Measured today (`4bb95063`)

| Route | Post-scan today | Where |
|---|---|---|
| full_frame | Whole-table `to_pandas()` of each mask source and output, `run_post_mask` in pandas, report only, no PK check, FK via `set(...)` and `isin` | `execution/_pipeline_finalize.py:470-503`, called at `execution/_pipeline_full_frame.py:308`; `publish.commit()` at `:316` |
| sequential | Declined when integrity is asked; the route needs relationships at all (`:130-131`) | `execution/_pipeline_routing.py:140-142` |
| chunked output lane | Output forced resident | `execution/_chunked_output_sink.py:124-130` |
| out_of_core, mixed | Declined | `execution/_pipeline_routing.py:328-329` |
| unified_slice | Declined; its return path skips line 308 | `execution/_unified_slice_admission.py:252-257`; `execution/_pipeline_full_frame.py:156-162` |
| stream mode | Refused `runtime.streaming_unavailable` | `execution/pipeline/_stream.py:187-188` |
| isolated | Child runs `run_pipeline`; the parent publishes with `atomic_commit`, not `observed_commit`; a child failure becomes `crashed` and drops `quality_metrics` | `execution/_isolated_run.py:330`, `:655`, `:575-590`; `execution/_isolated_worker.py:222-228` |
| streamed generation | Not a route: only the public `generate_tables_streamed*` entries reach `generation/_streamed.py:524` | `generation/_plan_entry.py:254`, `:326` |

Commit sites: `observed_commit` (`execution/_publication_observer.py:99-111`) is called by
`_sequential.py:603`, `out_of_core/_runner.py:306` and `OutputPublish.commit`
(`_chunked_output_sink.py:164`, used by full-frame `:316`, mixed `_pipeline_mixed_route.py:300`
and streamed generation). The isolated parent uses `atomic_commit`.

The pre-scan key core: `_check` (`profiling/_scan_integrity.py:304`) loads key columns as Arrow
batches (`_BATCH_ROWS` 100,000) into DuckDB (`MEMORY_LIMIT` 512MB, spill under
`protected_scratch_dir`) and counts parent null-key rows, duplicate excess, and per child null-key
rows and `NOT EXISTS` orphans (`_child_facts`, `:358`). Keys are declared only in `relationships:`.

## 3. Established solutions

Searched 2026-11-12: DuckDB larger-than-memory joins and aggregates; dbt `unique`, `not_null`, `relationships`; Great Expectations compound uniqueness; Polars streaming anti joins.

- DuckDB spills joins and aggregates to `temp_directory` past `memory_limit`
  ([memory management](https://duckdb.org/2024/07/09/memory-management)). Taken: key checks and
  exact distinct counts run in DuckDB through `connect_duckdb`.
- dbt generic tests compile to queries that return failing rows
  ([data tests](https://docs.getdbt.com/docs/build/data-tests)); `_check` already has them.
- Great Expectations checks a composite key as one tuple
  ([compound unique](https://greatexpectations.io/expectations/expect_compound_columns_to_be_unique)).
  Taken.
- Polars streaming joins: spill behavior not settled. Rejected; Polars stays off the mask path.
- A detector rate is hits over non-null values, so per-batch sums are exact
  (`detection/_scoring.py:175`). Taken; no sampling.

Custom code left: batch readers, the source-versus-output verdict and the hit collector. No library compares a masked output with its input for these types.

## 4. Behavior

### 4.1 PS-1 interfaces (fixed; the builder makes no design choice)

In `profiling/_scan_integrity.py`, exported from `profiling/__init__.py`:

```python
@dataclass(frozen=True)
class KeyBatches:  # public rename of _Keys (:104-110), fields unchanged
    schema: pa.Schema                                # key columns labelled k0..kn, declared order
    batches: Callable[[], Iterator[pa.RecordBatch]]  # re-callable: each call starts a new read
    bounded: bool = True

@dataclass(frozen=True)
class ChildKeyFacts: rows: int; null_key_rows: int; orphan_rows: int; orphan_distinct_keys: int

@dataclass(frozen=True)
class KeyFacts:
    status: Literal["checked", "key_type_mismatch"]
    parent_rows: int; parent_null_key_rows: int; parent_duplicate_excess: int
    children: tuple[ChildKeyFacts, ...]  # one per `children` argument, same order
    bounded: bool

def key_facts(parent: KeyBatches, children: Sequence[KeyBatches] = (), *,
              scratch: Path, memory_limit: str) -> KeyFacts: ...
```

Rules:

- `children=()` gives the parent facts only (PS-2a checks each primary key once).
- A type mismatch (`_comparable` differs) returns `status="key_type_mismatch"`, zero counts and
  `children=()`, and opens no connection. Pre-scan maps it to `SCAN_KEY_TYPE_MISMATCH` as today
  (`:389-395`); post-scan to `post_scan.key_type_mismatch`. From PS-2b0, `_comparable` compares
  type families (4.2.1), so mixed integer widths and string encodings are measured, not refused.
- `batches()` is called once per load; a self-reference passes two `KeyBatches` over one table and
  reads it twice. Stated, not optimized.
- `scratch` is caller-owned; `key_facts` spills under it and closes the connection in `finally`.
  Exceptions propagate. Callers split them with one shared rule, the predicate pre-scan already
  applies, which PS-2a makes public as `decoy_engine.profiling.passes_through(exc)` (on main,
  `profiling/scan_set.py`:
  `_PASS_THROUGH` at `:71` is `FileAccessDenied`, `FileAccessReadError`, `FileAccessUnavailable`,
  `PipelineValidationError`; `_is_coded` at `:338` adds `ScanSourceError` and every family the
  reason adapter registers, such as `runtime.lazy_source_changed`). A coded error propagates
  unchanged; any other is stripped with `safe_error_class` (`_progress_events.py:85`). Post-scan
  calls that predicate and never keeps a second list. No second sanitizer.
- `_check` becomes `_open_keys`, then `key_facts`, then a map to `RelationshipFacts` (each
  `ChildKeyFacts` plus the child's table and columns becomes a `ChildFacts`; `exact=True`).

`LazySource.open_batches` (`profile/_readers.py:257`) and `CsvLazySource.open_batches`
(`profile/_lazy_csv.py:214`) gain `columns: Sequence[str] | None = None`, forwarded to the existing
projection (pyarrow `iter_batches(columns=)`, CSV `_chunks(batch_rows, columns)`).

`columns` narrows batches only. `OpenedLazyBatches.facts` remains the whole-file footer facts so
`check_opened` still compares the whole file (`execution/_chunked_input.py:248-252`).
`OpenedLazyBatches` gains a `columns` argument passed to `arrow_adapter.iter_batches`; CSV builds each batch with the projected text schema instead of whole-file `facts.schema`
(`profile/_lazy_csv.py:91`). `_stream_input.py` imports `decoy_engine.profile` lazily or under
`TYPE_CHECKING`, following `profiling/_scan_integrity.py:204`; it does not import the unexported
`OpenedLazyBatches` from that package; the callback uses `Any` at this private concrete seam.

In `verification/postmask/_stream_input.py`:

```python
@dataclass(frozen=True)
class TableBatches:
    name: str
    schema: pa.Schema
    num_rows: int
    read: Callable[[Sequence[str], int], Iterator[pa.RecordBatch]]  # (columns, batch_rows), re-callable
    bounded: bool = True
    resident: pa.Table | None = None  # set only by from_table (M4 model passes read it)

def from_table(name: str, table: pa.Table) -> TableBatches: ...
def from_lazy(name: str, source: LazySource, *,
              on_open: Callable[[Any], None] | None = None) -> TableBatches: ...
def missing_columns(table: TableBatches, columns: Sequence[str]) -> tuple[str, ...]: ...
def key_batches(table: TableBatches, columns: Sequence[str]) -> KeyBatches: ...  # labels k0..kn
```

PS-1 exports these names through `verification.postmask`; execution callers never import the
private module. PS-2a exports its report/key adapter through the same package API.

- `from_lazy` covers Parquet and CSV `LazySource`: each `read` opens with
  `open_batches(batch_rows, columns=...)`, calls `on_open(opened)` before the first batch (the
  gate passes `check_opened`, 4.3) and closes on every exit. `key_batches` needs the columns to
  exist (callers check `missing_columns` first).
- Post-scan input kinds are what `run_pipeline` holds: `pa.Table` and `LazySource`. Fixed width
  and cloud are pre-scan descriptor kinds and keep today's pre-scan handling (`:248-257`,
  `skipped_cloud`). Staged Parquet is PS-10; loader baselines are scratch Parquet (PS-11).

### 4.2 Key checks

Run whenever relationships are declared (Q1), from the parsed `RelationshipConfig`s
(`parse_relationship_config`). Primary keys: each distinct `(parent.table, tuple(parent.columns))`;
foreign keys: each child end. `key_facts` runs over the inputs (baseline) and the output. Per key:

| Fact | Blocks when | Code |
|---|---|---|
| PK duplicate excess | output > baseline | `post_scan.primary_key_duplicates_introduced` |
| PK null-key rows (any part null) | output > baseline | `post_scan.primary_key_nulls_introduced` |
| FK orphan rows (complete tuple, no parent tuple) | output > baseline | `post_scan.foreign_key_orphans_introduced` |
| FK null-key rows | never; reported | none |
| declared key column absent from output | always | `post_scan.key_column_missing` |
| output key types not comparable (4.2.1) | the source side was comparable (4.2.2) | `post_scan.key_type_mismatch` |
| uncoded read, DuckDB or spill error measuring the output | always | `post_scan.check_error` (reused) |

The "output > baseline" rows compare against a measured baseline only; 4.2.2 says what an
unmeasured one does. An uncoded failure inside a key measurement never raises: the `KeyResult`
gets status `error` (output side) or baseline `error` (source side), code
`post_scan.check_error` and the stripped class name only. A coded error (4.1,
`profiling.passes_through`) propagates unchanged out of `check_keys`. Turning a blocking verdict
or a check error into a raised error is the gate's job (4.5, PS-7), not `_keys.py`'s.

Children that share one `(child.table, child.columns)` across relationships are checked once
against the union of every referenced parent's keys. One `key_facts` call chains all parent
`KeyBatches` and checks them with `_comparable`; each parent primary key is still checked alone
with `children=()`. This preserves declared-order first-hit resolution and "orphan only if absent
from all parents" (`relationships/_graph.py:174-204`) without changing PS-1's interface.

The baseline keeps valid runs valid: `preserve` and `warn` keep source orphans, `remap` gives them
fresh values that stay orphans, inherited duplicates are not the run's fault. A generate table has
baseline 0. `orphan_policy: fail` aborts earlier. Shapes: single column; composite (one tuple);
nullable FK (a tuple with a null part is counted, never an orphan); self-reference (two reads);
multi-parent child tuple (one union check). Column-key cycles are refused while building the graph
(`relationships/_graph.py:302-308`); cross-table cycles that remain routable take full-frame and
are checked normally (`execution/_pipeline_routing.py:404`, `:427`).

#### 4.2.1 Comparable key types (PS-2b0)

Today `_comparable` (`profiling/_scan_integrity.py:286-296`) compares exact Arrow type text and
folds only `large_string` to `string` and `large_binary` to `binary`. An `int32` child against an
`int64` parent, or a dictionary-encoded string against a plain string, is a mismatch. Both
pre-scan and post-scan measure through the same `key_facts`, so one change covers both: the Arrow
side decides comparability (`_comparable`), and the DuckDB side compares the values (`_load` at
`:309-316`, the join in `_child_facts` at `:419-427`). `_keys.py` adds no comparison of its own;
its multi-parent union (`_union`) feeds every parent through one `parent` table. Where `_keys.py`
needs a comparability answer without counting (the `not_run` baseline, 4.2.2), it calls the one
public predicate `profiling.keys_comparable(parent: KeyBatches, children: Sequence[KeyBatches])
-> bool`, built on `_comparable`; it reads schemas only and opens no connection.

`_comparable` returns one token per key column, by family:

| Family | Arrow types | Token |
|---|---|---|
| integer | every signed and unsigned width, 8 to 64 bits | `integer` |
| string | `string`, `large_string`, and a dictionary whose value type is one of them | `string` |
| other | every other type | today's text (with the `large_binary` fold) |

Exactness, checked against DuckDB 1.5.4 with pyarrow 24:

- No Arrow cast touches a key value. `uint64` to `int64` would raise or, unchecked, wrap.
- DuckDB scans an Arrow dictionary of strings as `VARCHAR`, also when each batch carries its own
  dictionary, so the string family needs no decoding step.
- DuckDB compares `INTEGER` with `BIGINT`, and `UBIGINT` with `BIGINT` (through `HUGEINT`),
  exactly: `2**63 + 5` stored as `uint64` never matches `-(2**63) + 5` stored as `int64`.
- The one unsafe step is `_load` today: it creates each table from the first schema it sees, and
  an `INSERT` of a wider batch then fails on overflow (`ConversionException`) or narrows. The
  multi-parent union hits this when parents differ in width: its declared schema is
  `parts[0].schema` (the first parent's, not the widened one), so an `int32` first parent fixes
  an `INTEGER` column and a later `int64` parent's value above that range fails. So `key_facts`
  computes one DuckDB type per label from every schema passed to the call and creates `parent`
  and `child` with it:
  the declared type when all are identical (today's behavior, unchanged); `VARCHAR` for the string
  family; `BIGINT` for the integer family when no member is `uint64`; `HUGEINT` when one is. Every
  insert then widens without loss.

The family rule has one owner. One function in `_scan_integrity.py` classifies an Arrow type as
the `integer` family, the `string` family or neither; `_comparable` and the DuckDB type choice
both read it, and neither keeps its own type list. A test guards that every comparable pair of
different types inside a family gets a widened DuckDB type and never falls back to the parent's
declared type.

A pair that is comparable but not identical outside the two families (today only the
`large_binary` to `binary` fold) keeps the parent's declared DuckDB type, as before this slice.

Left out, staying `key_type_mismatch`: floats (equality on floats is not a safe key rule),
decimals (scale differs), dates and timestamps (unit and time zone change the value),
dictionaries of non-string values, and integer against string.

Pre-scan effect: a relationship whose ends differ only inside a family is now `checked` with exact
counts, instead of `key_type_mismatch` with the `SCAN_KEY_TYPE_MISMATCH` warning. Integer against
CSV text stays a mismatch (`tests/unit/profiling/test_scan_integrity.py:108-116` is unchanged).

#### 4.2.2 Baseline status (PS-2b)

A zero baseline is a measured zero only when the source side was counted. Each `KeyResult` gains
`baseline`, the state of its source side:

| `baseline` | Meaning | Counts in `source` |
|---|---|---|
| `measured` | the source side was counted | counts |
| `no_source` | a table of the key is not in the inputs (generated), so its baseline is zero by definition (4.2) | zeros |
| `key_column_missing` | a source table lacks a declared key column | `null` |
| `key_type_mismatch` | the source key types are not comparable (4.2.1) | `null` |
| `error` | an uncoded failure while counting the source; `baseline_error_class` holds the class name | `null` |
| `not_run` | not counted: the output side ended in `error` or `key_column_missing`, or is a type mismatch over comparable source types | `null` |

`_keys.py` always takes the source key types through `profiling.keys_comparable` (4.2.1), even
when the output side is a type mismatch; that reads schemas only and opens no connection.
Introduced codes compare output with the measured counts, or with zero when `baseline` is not
`measured`. `post_scan_verdict` (4.8) then decides
per key by code and `baseline`, never by `status == "checked"`:

| Output `status` | `baseline` | Key `codes` | Verdict |
|---|---|---|---|
| `error` | `not_run` | `check_error` | block |
| `key_column_missing` | `not_run` | `key_column_missing` | block |
| `key_type_mismatch` | `key_type_mismatch` | `key_type_mismatch` | warn: nothing is measurable on either side and the run did not break it |
| `key_type_mismatch` | `not_run`, `no_source`, `key_column_missing`, `error` | `key_type_mismatch` | block |
| `checked` | `measured`, `no_source` | each introduced code (output above baseline) | block |
| `checked` | `key_column_missing` | each introduced code (output above zero) | block |
| `checked` | `error` | `check_error`, plus each introduced code (output above zero) | block on an introduced code; `check_error` alone warns |
| `checked` | `key_type_mismatch` | `key_type_mismatch`, plus each introduced code (output above zero) | block on an introduced code; `key_type_mismatch` alone warns |

So when the output is measured, an output count above zero blocks on its introduced code
whatever made the baseline unmeasured; a source-side `key_type_mismatch` or `check_error` then
rides as a warning. Warn-and-do-not-block holds only where nothing can be measured on either
side (the mismatch over mismatch row). A multi-parent child whose source parents differ in type
takes the same rows. A warning code stays on its key and is listed in `verdict.warnings`;
it never blocks. No reason code is added: the codes of the 4.2 table express every row.

### 4.3 Baselines and identity (F6, F16, F20)

- Resident inputs: the gate reads the sources the route masked from, after the pre-mask cast
  (full-frame `step.sources`; unified slice `caller_sources` after `cast_run.pre_mask`).
- `bind_gate` takes its own `capture_source_facts(ctx.caller_sources, ctx.table_kinds)` snapshot at
  bind time, before route reads. It does not depend on a route snapshot: the chunk route skips one
  when its lane graph has edges (`execution/_pipeline_chunk_route.py:89-92`), and mixed captures
  its own only at `_mixed_static.py:186-189`. This costs one footer read per lazy table; CSV facts
  are cached (`profile/_lazy_csv.py:167-170`).
- Parquet `LazySource` inputs: re-read through `from_lazy` with `on_open` calling
  `check_opened(h, facts[name], table=name)` against the gate's bind-time snapshot. A
  changed file refuses with `runtime.lazy_source_changed`, the check chunked and mixed already
  trust (row count, schema with metadata, null counts, row-group layout).
- `CsvLazySource` inputs: re-read through `from_lazy`; its identity check refuses a change.
- Loader inputs (`source_loader`): the baseline is taken in the read the route already does
  (PS-11). Until PS-11, and for any loader the tee cannot wrap, the run keeps the resident route.
- A mask table in `transform_bearing_mask_tables` takes its baseline after transforms. PS-11 tees
  key and cast columns from the route's own transformed read into scratch Parquet in
  `execution/pipeline/_post_scan.py`; until PS-11 these tables stay resident under
  `route.post_scan_keys_resident`. This matches the resident preparation order
  (`execution/_pipeline_sources.py:99-124`) and sequential transform order (`:127-165`).
- SUB-RUN: with `subset:` the inputs are the post-subset scratch sources (`field_key=
  "engine.scratch"`, SUB-RUN 4.2), which outlive the gate (`SubsetStageScope` wraps `run_pipeline`).
  Orphans kept across `excluded_edges` are in the baseline (AT-24). SUB-RUN refuses a subset with
  a `source_loader`.

F20 answer: every run path reads CSV as text (`profile/_lazy_csv.py:134`,
`profile/_csv_reader.py:506-507`), and so does pre-scan. Output keys differ from text only where
declared casting types a key column, so the baseline reads through the same cast: resident routes
use the cast sources; a streamed re-read applies `CastRun.cast_batch(table, batch, record=False)`
(`declared_casting/_boundary.py:210`) over the key and cast columns. Quarantined rows drop from both
sides alike. Each route's `open_cast_run` registers the run on `active_gate()` through
`gate.attach_cast_run`; `cast_batch(record=False)` remains valid after `finish()` because it uses a
fresh ledger (`declared_casting/_boundary.py:210-236`).

### 4.4 Column suite (exact, F14)

Runs under `post_scan.integrity: true`, as today. `verification/postmask/_column_facts.py`
computes per configured column, in one pass over aligned batches: row counts, comparable and
identical rows (the positional comparison of `residual_pii.py:241-247`), `bytes_identical`, a
constant-source flag, and exact distinct counts through `profiling.column_distinct(batches, *,
scratch, memory_limit) -> int`, implemented in `profiling/_scan_integrity.py` and exported from
`profiling/__init__.py` (DuckDB `count(DISTINCT ...)` over a `RecordBatchReader`).

Detector rates are exact: `detection/_scoring.py` gains `counting_scope()`, a context variable to
which `_evaluate` reports `(detector_id, hits, n, threshold, name_hint)` before its threshold test.
The suite runs `run_all_detectors` per batch inside the scope, sums hits and `n`, and decides with
the same threshold and `_confidence_bucket`. `sample_misses` stays empty here (it holds values).

Batch boundaries must not change any fact. A pandas frame built per batch takes its dtype from
that batch (an integer column with a null becomes float), so comparisons run in Arrow or through a
dtype mapping derived from the schema, never from one batch's values. The mapping is the central
`internal/pandas_compat.exact_int_types_mapper`.

Amendment (PS-4 review round 3): the FK key-fact reader in `fk_preservation._whole()` reads a
float key column (NumPy or nullable `Float32`/`Float64`) as `Int64` only when it has at least one
non-null value and every non-null value is whole and inside the `Int64` range. An all-null float
column keeps its float dtype: it carries no evidence that it was an integer column, so an integer
parent against it stays `key_type_mismatch`. Every other float column also keeps its dtype.

Recorded semantic change (PS-5 review): an integer column that holds a null is read as whole
numbers ("10005") on every route. The old runner converted with plain `to_pandas()`, which turns
such a column into floats ("10005.0"), so digit detectors (`us_zip`, `npi` and the like) never
matched it. They can now fire there. This is the safer direction and the only reading that does
not depend on which batch a null falls in. AT-19 pins the difference against plain `to_pandas()`
and AT-22 carves it out by name.

`residual_pii.py` and `policy_validation.py` keep their decision tables and take these facts.
`run_post_mask(source_frames, output_frames, *, config, keys=None)` stays the public adapter.
When `keys is None`, it computes key facts from those frames through `from_table` and `_keys.py`,
so every caller retains FK findings while `compute_post_scan_integrity` remains on main through
PS-7 (`execution/_pipeline_finalize.py:470-503`).
Model passes (SCAN-ML M4) need resident values; the model predicate keeps those runs resident and
the passes read `TableBatches.resident`.

### 4.5 The gate

`execution/pipeline/_post_scan.py`:

```python
class PostScanGate:
    required: bool  # post_scan_gate_applies(config), see 4.7
    ran: bool
    def run_resident(self, outputs: Mapping[str, pa.Table], sources: Mapping[str, pa.Table],
                     table_kinds: Mapping[str, str]) -> None: ...
    def before_commit(self, sink: Any) -> None: ...  # called by observed_commit
def bind_gate(ctx: PipelineRunContext) -> ContextManager[PostScanGate]: ...  # context variable
def active_gate() -> PostScanGate | None: ...
```

1. `execute_route` binds one gate per run (precedent `bind_duckdb_run`,
   `_pipeline_route_dispatch.py:113`).
2. Full-frame: at `_pipeline_full_frame.py:308`, `gate.run_resident(...)` runs when there is no sink or `post_scan_output_readable` says the sink is unreadable; otherwise the readable staged output is checked at commit. `compute_post_scan_integrity` is deleted (F2).
3. Unified slice (PS-12, F13): `_render_and_seal` runs `gate.run_resident(outputs, caller_sources,
   ...)` between `cast_run.post_mask` and `cast_run.finish`; with no cast run, on
   `unified_slice_result.outputs` before the return at `:162`. Relationships already decline this
   route (`_unified_slice_admission.py:259`).
4. Commit: `observed_commit(sink)` calls `active_gate().before_commit(sink)` before the publication
   evidence `try` and before `sink.commit()` (`execution/_publication_observer.py:99-110`), so a
   gate block is not attributed as a failed commit.
   Not required, or already ran: return. Sink has `staged_tables()` (4.6): run over the staged
   files. Otherwise raise `post_scan.check_error`; the commit does not happen (F1). Routing keeps
   such runs resident (4.7); this is the backstop.
5. After the route returns, whenever the gate has not run, `execute_route` runs it over
   `result.outputs` regardless of `ctx.sink`, then raises `post_scan.check_error` if a required gate
   still has not run. This covers mixed with a sink while streaming is off, which returns outputs
   resident (`execution/_pipeline_mixed_route.py:184-205`, `:288-300`).
6. After step 5, `execute_route` writes `gate.report` to
   `result.quality_metrics["post_scan_integrity"]` whenever the gate ran, on every route. The
   isolated worker sends it through the existing envelope `quality_metrics` field
   (`execution/_isolated_worker.py:236`).
7. Exactly once: `ran` is set when a run completes; later calls are no-ops.
8. Errors: caught per part. A coded error (`profiling.passes_through`, 4.1) propagates unchanged;
   any other is kept as `safe_error_class(type(exc).__name__)`. Key checks already return such a
   failure as a `check_error` result (4.2). The gate raises `PostScanBlockedError` for a blocking
   verdict, or `post_scan.check_error` for a gate that cannot run, after the `except` block, so
   the raise carries no `__cause__` and no `__context__` (AT-31).
9. Blocking: `post_scan_verdict` blocking raises `PostScanBlockedError` before `sink.commit()`; the
   route's failure path aborts the sink (`out_of_core/_runner.py:314-316`, `_sequential.py`,
   `OutputPublish.__exit__`).
10. Memory: DuckDB `memory_limit` is the smaller of `MEMORY_LIMIT` and
   `resolve_ooc_memory_limit(...).memory_limit`. Spill goes to the F25 `ScratchArena` once S2a has
   merged, else `fileaccess.protected_scratch_dir`.

Isolated (PS-9, F4). The gate runs in the child's `run_pipeline` (`_isolated_worker.py:217`) and
in `_run_in_process`. A block must not read as a crash:

- `_isolated_worker._run` catches `PostScanBlockedError` before its generic handler and returns
  `{"outcome": "blocked", "reason": ..., "error": <safe text>, "quality_metrics":
  {"post_scan_integrity": exc.report}}`.
- `IsolatedRunOutcome` (`_isolated_common.py:86`) gains `"blocked"`; `_result_from_envelope` and
  `_run_in_process` keep report and reason with `outputs=None` and commit nothing, through one
  helper in `_isolated_reason.py` (`_isolated_run.py` grows at most 8 lines).
- `run_job_with_governor` treats `blocked` as terminal, no reroute (an unknown outcome raises
  today, `_governor.py:396-406`); `GovernorResult.outcome` gains `blocked`.

### 4.6 Staged output and the F22 seam (PS-10, F21)

`ParquetTransactionalSink.staged_tables() -> Mapping[str, Callable[[], int]]`: before commit, each
staged table maps to an opener returning a new read-only descriptor of its staged file through the
public `OutputTxn.open_file(component)` (`fileaccess/_output.py:247`); names are recorded in
`write` and `write_batches`. `from_staged` reads by row group; schema and row count come from the
footer. Main's sink is the base, so this row is off F22's critical path. A sink without
`staged_tables` (`_CallableSinkAdapter`, `_transactional_sink.py:107`, third-party) is unreadable:
routing keeps the resident route (4.7, F5).

Contract F22 c1a keeps (to post on the F22 PRs): "`ParquetTransactionalSink` and
`StagedParquetWriter` keep `staged_tables() -> Mapping[str, Callable[[], int]]`, each opener
returning a new read-only descriptor of that table's staged Parquet file before commit, and every
route still commits them through `observed_commit`."

### 4.7 Routing (F5, F9)

`config/_post_scan.py`, exported through `config/__init__.py`, holds the one model predicate `post_scan_model_setting(config) -> str | None` (`post_scan.detectors` other than `regex`, or `post_scan.ner_findings: fail`), the one
readability decision `post_scan_output_readable(sink_kind) -> bool`, and
`post_scan_resident_reason(config, *, output_readable: bool,
input_baseline_readable: bool) -> ReasonRef | None`:

| Slice state | Returns |
|---|---|
| PS-8 to PS-10 | `route.post_scan_integrity_requested` when integrity is asked; else `route.post_scan_keys_resident` when keys are declared and either output or post-transform/loader baseline is unreadable |
| PS-11 | the same, but the tee makes supported loader and transform baselines readable |
| PS-12 on | `route.post_scan_models_resident` when the model setting is set; else the keys clause |

`post_scan_output_readable` owns the canonical `"None"` token (`physical/_snapshot.py:44-48`), the PS-10 engine staged-sink token, and any F22 staged-writer token; all other class tokens are unreadable. `sink is None` is readable in every slice because sequential and out-of-core return resident outputs (`execution/_sequential.py:542-546`,
`execution/out_of_core/_runner.py:305-313`); false for a sink without `staged_tables()`; and true
for a sink with `staged_tables()` from PS-10. Every estimator, compiler and runtime site calls this
function: runtime passes the sink kind, the compiler passes `inputs.sink_class_token` (`execution/physical/_inputs.py:206`), and estimators pass the canonical no-sink token. A guard fails if a site
constructs readability itself. The same predicate receives baseline readability from the source
kind; until PS-11 a loader or transform-bearing mask table passes false, and PS-11 passes true only
when the tee covers every key and cast column. Runtime uses the real sources, the compiler combines
`inputs.source_loader_present` (`physical/_inputs.py:204`) with config transforms, and estimators'
contract has no loader. `decide_execution_route` takes
`post_scan_resident: ReasonRef | None` in place of the bool.

`config/_post_scan.py` also holds the one applicability predicate
`post_scan_gate_applies(config) -> bool`, beside `post_scan_integrity_requested`: true when a
relationship is declared or `post_scan.integrity` is true. PS-7 adds it and `PostScanGate.required`
is set from it at bind time. It has three callers: this gate, the DELIV row-7 missing-report rule
and the DELIV chunked gate (slice CR). The two DELIV callers are in the CLI and swap to it in DELIV
C5, once the engine release carrying PS-7 is pinned; until then CR uses
`post_scan_integrity_requested` plus its own relationship check, as the DELIV plan states. The
engine sentry in the table in 6 covers the engine side now; DELIV C5 adds the CLI side.

`route.post_scan_keys_resident` keeps one meaning in every slice: keys must be checked and this
route cannot read its output back. At PS-12 `route.post_scan_integrity_requested` retires with
`replaced_by=("route", "post_scan_models_resident")`. M5 (#508): if it merges before PS-8, PS-8
moves `_asks_for_models` and the `ner_findings` test from `_route_guard.py` into
`post_scan_model_setting`; if PS-8 merges first, #508 rebases onto it. One copy either way.

### 4.8 Verdict, report and exception (F8, F10)

`quality_metrics["post_scan_integrity"]` gains `keys` (per key: `table`, `columns`, `kind`,
`parent`, `status`, `baseline` (4.2.2), `bounded`, `codes`, `source` and `output` counts, and
`error_class` or `baseline_error_class` when that side failed) and `verdict: {blocking, codes,
warnings}`. Fields are additive under `post-scan-integrity/v2`. Every reason code in the report
(`keys[].codes`, `verdict.codes`, `verdict.warnings`) crosses the wire as a mapping
`{"namespace": ..., "code": ...}`, never as a hand-built dotted string. One public serializer
builds that mapping: `reason_ref_payload(ref: ReasonRef) -> dict[str, str]` in
`reasons/_identity.py`, beside `ReasonRef` (`:27-31`), exported through `decoy_engine.reasons`.
PS-2b adds it and moves `phase._reason_wire` (`phase.py:470-476`, which keeps its
`require_known` check and `PhaseContractError`) and `_keys.py` onto it; `_blocked.py` calls it
too. Until PS-2b, PS-2a's `asdict(c.spec.ref)` in `_keys.py` stays as it is.

`PostScanBlockedError(DecoyError)` is SCAN-ML M4's class (4.5 there): the value-free report in a field, the registry's fixed summary plus `_blocked.py`'s safe location as `str(error)`, raised with
an empty chain. Whichever of M4 and PS-2b builds first defines it in
`verification/postmask/_blocked.py`; the other uses it.

`post_scan_verdict(report) -> PostScanVerdict` in `_blocked.py`, exported through
`verification.postmask`, is the one blocking decision; cross-package callers use that public API. The
gate calls it and raises on `blocking`. The report blocks when any of these holds: a key code
that the 4.2.2 table classes as blocking, read from each key's `codes` and `baseline` (an `error`
or mismatch result blocks or warns by its code, whatever its status); a finding of severity
`fail` in `residual_pii`, `policy_validation` or `fk_preservation`;
a finding of severity `error`, or a set `pass_failed_with`; an M4 NER block. Severities are the
`Severity` literals (`verification/postmask/types.py:20`: `info`, `warning`, `fail`, `error`);
`info` and `warning` never block. An `fk_preservation` `fail` is the same fact as
`foreign_key_orphans_introduced` and adds no second code. An `fk_preservation` `error` derived from
a key status also adds no code; the key code stands.

`_blocked.py` also owns the one value-free block-location formatter. It uses the existing residual
`detector_id` or the fixed id `policy_validation`, and formats table, column and detector id. No
caller or exception builds that message independently.

One code goes on the record, by this order (the first blocking code present wins, so a
warning-only `check_error` or `key_type_mismatch` never becomes the reason of a block);
`verdict.codes` lists every blocking code in the same order, and `verdict.warnings` every warning-only key code:

1. `post_scan.check_error` (also an `error` finding or `pass_failed_with`)
2. `post_scan.key_column_missing`
3. `post_scan.key_type_mismatch`
4. `post_scan.primary_key_nulls_introduced`
5. `post_scan.primary_key_duplicates_introduced`
6. `post_scan.foreign_key_orphans_introduced`
7. `post_scan.finding_blocked` (a column-suite `fail`)
8. `post_scan.ner_blocked` (M4)

Inability to check comes first, then structure, then data. An NER block and a key block make one
exception with both codes in `verdict.codes`. M4's block predicate feeds the verdict instead of
raising its own exception.

`fk_preservation` (frozen v2 field) derives from the keys facts and the 4.2.2 classes: `fail`
when orphans were introduced, `warning` when all output orphans are inherited or the key carries
only warning codes, `info` when none, `error` when the key blocks without counts (`error`,
`key_column_missing`, or a blocking `key_type_mismatch`). A warning-only key never derives an
`error` finding, so it cannot block through severity. A composite fills the additive
`parent_columns` and `child_columns` tuples;
`parent_column` and `child_column` hold the single name, or the names joined by `", "` for display.
The rate thresholds (`fk_preservation.py:165-195`) and the dead `orphan_policy == "skip"` branch go.
PS-4 records this pre-GA semantic change in `docs/compatibility-contract.md` 3.1 and updates on
purpose `tests/unit/detection_verification/test_bf4_fk_preservation.py`,
`test_postmask_runner.py` and `test_postmask_batch_b.py`. Per-table blame stays in the findings for
display; V1 delivery does not act on it (4.10).

### 4.9 Generated relationship parents (F15, PS-3)

A relationship whose parent table is generated needs a key the plan can show is unique. Accept
`row_count <= 1`; a `sequence` with non-zero `step`; `unique: true`; a composite with one such
column; or a `unique_together` entry whose names, as a set, are exactly the declared parent key
columns. Otherwise `check_generated_parent_keys(config)` (`plan/checks/_relationship_keys.py`)
refuses with `plan.relationship_parent_not_unique`, naming table, columns and the fix ("set
`unique: true` on the key column or use a `sequence`"). The rule is unconditional whenever keys
are declared. `check_generate_columns` calls it last, so
`decoy validate` and `decoy run` refuse alike (`plan/_compile.py:212`, `:532`); `_compile.py` does
not grow. No generated output changes for accepted configs; the census fixtures below move to
unique keys of the same Arrow type and regenerate exact expected values, never loosen assertions.

Fixture census from `origin/main`, complete for tests and shipped examples/docs snippets:

- `tests/parity/_f0_fixtures.py:727-795`: categorical parent key, five rows. Add `unique: true` and
  regenerate `_f0_expected.py` and refreeze evidence.
- `tests/parity/test_phase_f_f2_mixed_parity.py:40`, `:84-98`, `:162-170`: categorical integer
  parents with 12 rows. Replace them with integer sequences using the same start.
- `tests/parity/test_phase_f_f2_mixed_sweep.py:58-59`, `:340-365`, `:839-853`, `:1013-1018`:
  categorical, faker, formula and date parent keys. Replace each with a provably unique key of the
  same Arrow type, using a sequence or enough categories plus `unique: true`, then regenerate the
  exact matrix expectations.
- `tests/parity/test_phase_f_f2_mixed_admission.py:176-201`, `:415-431`, `:473-486`: categorical
  parent keys. Use same-type unique categories and regenerate exact values.

- `tests/parity/test_numeric_typing_readers.py`
  (`test_relationship_parent_that_is_a_reference_column_keeps_the_root_text`): a `reference`
  parent key. Add `unique: true`; assertions unchanged. The first census missed it because the
  parity conftest lifts `check_generate_columns` through `lenient_generate_checks`. The lenient
  path (`tests/_lenient_checks.py`) keeps `check_generated_parent_keys` on, so a lifted test cannot
  skip this rule.

`unique: true` is trusted on every type that carries it: config validation accepts it on
`sequence`, `categorical`, `faker` and `reference` only (`generate_unique_unsupported` otherwise)
and generation enforces it on all four (`generation/_unique.py`). A type that gains `unique` must
be enforced by generation before validation accepts it.

The one-row categorical at `tests/physical/test_shadow_mixed_fk.py:227-233` is accepted by the
`row_count <= 1` rule and does not migrate. No config in `examples/complex_healthcare_claims.yaml`
or `examples/runnable_demo.py` uses its generated `audit_log` as a relationship parent
(`examples/complex_healthcare_claims.yaml:240-284`; `examples/runnable_demo.py:212-233`), and the
current docs snippets keep relationship and generation examples separate
(`docs/recipes.md:109-180`; `docs/relationships.md:85-114`). Census: four refused fixture files,
zero shipped example or current-doc migrations.

### 4.10 Contract for CLI delivery (row 61)

The verdict carries `blocking`, `codes` (4.8 order), `warnings` and the value-free report (keys, column
findings with `table`, `child_table`, `severity`). On a block the engine raises
`PostScanBlockedError` (`reason` is the first code, `report` the full report) and nothing has
committed. It is raised inside `observed_commit` before `sink.commit()` on a sink route, in
`execute_route` after the route returns on a no-sink route, and in the isolated child (or the
in-process fallback), which surfaces as `IsolatedRunResult` outcome `blocked` with the same reason
and report. CLI delivery keeps no severity rule: it maps the exception or a `blocked` outcome to
"nothing delivered, exit 4". V1 withholds the whole run in strict and best-effort modes alike
(Q4), so the gate takes no delivery mode. A requested post-scan with no report is a block on the
CLI side; no engine route returns without one (4.5 steps 4 to 6). A column-suite block message
names the table, column and detector id, and contains no data value. The release note and
`docs/relationships.md` state that `integrity: true` now withholds all output on a blocking
finding.

## 5. Failure modes

| Failure | Result | Code (owner module) |
|---|---|---|
| Output PK duplicates above baseline | block, nothing commits | `post_scan.primary_key_duplicates_introduced` (new, `verification.postmask._keys`) |
| Output PK nulls above baseline | block | `post_scan.primary_key_nulls_introduced` (new, same) |
| Output FK orphans above baseline | block | `post_scan.foreign_key_orphans_introduced` (new, same) |
| Declared key column missing from output | block | `post_scan.key_column_missing` (new, same) |
| Output key types not comparable, source side comparable | block | `post_scan.key_type_mismatch` (new, same) |
| Source and output key types both not comparable | warn, no block (4.2.2) | `post_scan.key_type_mismatch` (new, same) |
| Source key types not comparable, output measured | block only on an output count above zero (4.2.2) | the introduced code, with `post_scan.key_type_mismatch` as a warning |
| Uncoded read, DuckDB or spill error measuring the output | `error` result, block, class name only | `post_scan.check_error` (reused) |
| Uncoded error measuring the source | baseline `error`; block only on an output count above zero (4.2.2) | `post_scan.check_error` (reused) as a warning, plus the introduced code |
| Coded engine error inside a key check (`passes_through`) | propagates unchanged | the error's own code |
| Column-suite check raises (`error` finding, `pass_failed_with`) | block, type name only | `post_scan.check_error` (reused) |
| Column-suite finding of severity `fail` | block | `post_scan.finding_blocked` (new, `verification.postmask._blocked`) |
| Required gate cannot run at commit | commit refused | `post_scan.check_error` (reused) |
| Required gate did not run after a route | refused after the route | `post_scan.check_error` (reused) |
| Source file changed before the re-read | refuse | `runtime.lazy_source_changed` (reused) |
| Keys declared, output unreadable | resident route | `route.post_scan_keys_resident` (new, `config._post_scan`) |
| Model detectors or `ner_findings: fail` | resident route (PS-12) | `route.post_scan_models_resident` (new; retires `route.post_scan_integrity_requested`) |
| Generated parent key not provably unique | refused at plan time | `plan.relationship_parent_not_unique` (new, `plan.checks._relationship_keys`) |
| Isolated child blocked | outcome `blocked`, report kept, nothing committed | the blocking `post_scan` code |

Retry classes: the six new `post_scan` codes are `CONFIGURATION_REPAIR` (fix the strategy, keys
or source); the plan code takes the plan default `CONFIGURATION_REPAIR`; route codes keep `NEVER`;
`check_error` stays `UNKNOWN`. Files: `reasons/post_scan/_part2.py` (new), `reasons/plan/_part3.py`,
`reasons/route/_added.py`, `_retired.py`. Every new or retired code uses `since=UNRELEASED`;
release stamping owns the version. Code-adding slices: PS-2a, PS-2b, PS-3, PS-8, PS-12.

## 6. Choke points, guards and red mutations

| Rule | Choke point | Guard (walks the real artifact) | Mutation that turns it red |
|---|---|---|---|
| No commit without the gate | `observed_commit` | AST sentry over `execution/` and `generation/`: every sink `.commit()` sits inside `observed_commit`; `OutputPublish.commit` is the one wrapper; `atomic_commit` only in `_isolated_run.py` `_commit` and `_run_in_process` (named, until F22 c2 deletes it); aliases and `getattr` resolved | call `sink.commit()` directly in `_sequential.py`; add an `atomic_commit` call elsewhere |
| Gate ran on every route | gate binding plus post-condition | route-coverage sentry (AT-17), keyed on the imported `ROUTES` plus `stream` and `isolated` | drop the `mixed` entry |
| One key core | `profiling.key_facts`, `profiling.keys_comparable` | import sentry: no module under `verification/` or `execution/pipeline/_post_scan.py` imports `duckdb` or `connect_duckdb`; AST sentry: no module there calls `_comparable` or compares (`==`, `!=`, `in`) a `.type`, `.types` or `.schema` value of a key | add `import duckdb` to `_keys.py`; compare `own.schema != parent.schema` in `_keys.py` |
| One reason-identity serializer | `reasons._identity.reason_ref_payload` | AST sentry over `phase.py` and `verification/postmask/`: no dict literal or `dict(...)` call holds both `namespace` and `code` keys, and no `asdict` call takes a `ReasonRef` or `.spec.ref`; the identity mapping comes only from `reason_ref_payload` | build `{"namespace": ..., "code": ...}` inline in `_blocked.py` |
| One pass-through rule | `profiling.passes_through` | AST sentry over `verification/postmask/` and `execution/pipeline/_post_scan.py`: every broad handler (`except Exception`, `except BaseException`, bare `except`) opens with `if passes_through(<bound name>): raise`, and no handler there catches `DecoyError` or a subclass | delete that line from the source-side handler in `_keys.py` |
| One model predicate | `config._post_scan.post_scan_model_setting` | AST sentry: no other module reads `ner_findings` or compares `detectors` with `"regex"` | inline the test in `_route_guard.py` |
| One applicability predicate | `config._post_scan.post_scan_gate_applies` | AST sentry: `PostScanGate.required` is assigned only from a `post_scan_gate_applies` call, and no other function under `src/decoy_engine/` both reads `relationships` and `post_scan.integrity` to decide whether the gate runs (ratchet baseline of zero, by module) | set `required` from a local `bool(config.relationships) or integrity` expression in `_post_scan.py` |
| One route readability decision | `post_scan_output_readable` plus `post_scan_resident_reason` in `config._post_scan` | AST sentry inventories compiler, estimator and runtime sites and fails on a literal output/baseline decision | pass `True` in `_job_estimate.py` |
| One blocking decision | `verification.postmask._blocked.post_scan_verdict`; `_blocked.severity_counts` owns the tally used by `runner.py` | AST sentry: outside `_blocked.py`, nothing under `src/decoy_engine/` constructs `PostScanBlockedError` or compares a post-scan `severity` with `"fail"` or `"error"` | raise `PostScanBlockedError` from `_keys.py` |
| Value-free results | facts types hold ints, names and `ReasonRef` only | AT-14 canary | add a key value to a finding |
| No whole-table read in the gate | `TableBatches` | AT-15 spies | read a staged file with `pq.read_table` |

Each sentry is shown red on its seeded mutation in the slice record. The blocking-decision,
pass-through, key-type comparison and reason-identity serializer sentries land in PS-2b.

## 7. Acceptance tests

Red first: each slice's first commit adds its tests, red on the base.

| AT | Slice | Input and expected result | Red mutation |
|---|---|---|---|
| AT-1 | PS-1 | Parquet and CSV files with nulls, duplicates and orphans: `key_facts` counts equal pre-scan `relationship_facts` on the same files | `_complete` joins with `OR` |
| AT-2 | PS-1 | One string-column table as `pa.Table`, Parquet `LazySource`, CSV `LazySource`: `from_*` reads give identical concatenated tables and row counts, with and without `columns`; `check_opened` passes on a projected open while facts remain whole-file | project `facts.schema` with the batch |
| AT-3 | PS-1 | Existing pre-scan scan-integrity tests pass unchanged; `children=()` gives parent facts; a mismatch returns `key_type_mismatch` and opens no connection | swap null-key rows and duplicate excess |
| AT-4 | PS-2a | Masking collapses two parent keys: `primary_key_duplicates_introduced`; source duplicates alone: no block | compare output with 0 |
| AT-5 | PS-2a | A nulled PK part blocks; composite `(a, b)` with `b` null counts as null | check `k0` only |
| AT-6 | PS-2a | Child keys remapped outside the parent block; source orphans under `preserve`, `warn`, `remap` do not | drop the baseline |
| AT-7 | PS-2a | Child `(1, 2)` against parents `(1, 3)` and `(4, 2)` is an orphan | join on `k0` only |
| AT-8 | PS-2a | A child with any null part: not an orphan, counted, no block | remove the completeness filter |
| AT-9 | PS-2a | `employees.manager_id -> employees.id`: both ends read, two `batches()` calls | reuse one read |
| AT-10 | PS-2a | Two relationships over one parent: PK checked once; verdict independent of config order | stop after the first edge |
| AT-11 | PS-2a | Missing output key column: `key_column_missing`; `int64` parent with `string` child: `key_type_mismatch` | return `checked` |
| AT-12 | PS-2a | An uncoded failure at `_before_fetch` (a `RuntimeError` carrying a canary): `check_keys` raises nothing; the key's `KeyResult` has status `error`, codes `check_error`, `error_class` the safe class name, no canary anywhere. A coded error raised in a read (`runtime.lazy_source_changed` from `check_opened`, `FileAccessReadError`, `ScanSourceError`) propagates as the same object; `_keys.py` decides through `profiling.passes_through` | wrap every exception into `check_error` |
| AT-13 | PS-2b | Breaches of several kinds plus an NER fail stub: reason is the first code in 4.8 order; `verdict.codes` has all, in order | sort codes alphabetically |
| AT-32 | PS-2b | Verdict cells: residual and policy `fail` block with one value-free table/column/detector-id message; warning/info do not; error or `pass_failed_with` gives `check_error`; FK fail/error derived from a key adds no duplicate code. The verdict reads codes, not status: a result with status `error` and `check_error` blocks; status `key_type_mismatch` blocks or warns by its `baseline` (4.2.2); a `checked` result with no codes passes | block on key codes only; skip results whose status is not `checked` |
| AT-35 | PS-2b0 | Type families in `key_facts` and pre-scan: the integer cell is parametrized over every pair of `int8`, `int16`, `int32`, `int64`, `uint8`, `uint16`, `uint32` and `uint64` (parent and child), each with values at that type's minimum and maximum; a typed-empty, schema-bearing load (zero-row `int32` parent and `uint64` child, so the column types come only from the schemas) gives zero counts without error and asserts the created column type of both tables is `HUGEINT` (a `uint64` end is present); `uint64` parent holding `2**63 + 5` and `1` against an `int64` child holding `-(2**63) + 5` and `1`, values above the signed 64-bit maximum and below zero so a wrap or a narrow shows as a wrong count (exactly one orphan, the negative value; no error); a dictionary string parent whose two batches carry different dictionaries against a `large_string` child; a union-shaped parent, as `_keys.py` builds it (declared schema of the `int32` parent first, then the batches of an `int64` parent, which also rides as a child), with a child value above the `int32` range, red under the old first-schema load; all give exact counts equal to the oracle: the same data with both ends as `decimal128(38, 0)` (plain `string` for the string family). The oracle ends have identical types, so it takes the declared-type path and never calls the family rule or the DuckDB type choice under test. `int64` against `string`, `float64` against `int64`, and a dictionary of `int64` against `int64` stay `key_type_mismatch` | create the union table from the first schema; cast `uint64` to `int64` in Arrow |
| AT-36 | PS-2b0 | Pre-scan: Parquet `int32` child and `int64` parent give `checked` with exact counts and no `SCAN_KEY_TYPE_MISMATCH` warning; `test_type_mismatch_gives_no_counts` (integer against CSV text) passes unchanged | keep exact type text in `_comparable` |
| AT-37 | PS-2b | Baseline status cells for the 4.2.2 verdict table (the output `error` row is AT-12 plus AT-32, the output mismatch over a comparable source and the `measured` rows are AT-11 and AT-4 to AT-8, verdict reading is AT-32): an output `key_column_missing` result (baseline `not_run`) blocks with `key_column_missing`; source and output both `int64` against `string`: `key_type_mismatch` warns, no block; source comparable and output not: block; source `int64` against `string` with the output cast comparable: one output orphan blocks on `foreign_key_orphans_introduced` with `key_type_mismatch` in `verdict.warnings`, none passes with the warning; the same for a child whose two source parents differ in type; the PS-2a test `test_a_source_side_type_mismatch_gives_a_zero_baseline` (`tests/unit/detection_verification/postscan/test_keys.py`) is kept, still asserts the block, and gains `baseline == "key_type_mismatch"`; a coded error (a changed lazy source, a `FileAccessReadError`) raised while counting the source propagates as the same object; source table missing the key column with one output orphan: block, with none: pass; an uncoded source-side error with output orphans: block on `foreign_key_orphans_introduced` with `check_error` in `verdict.warnings` and `baseline_error_class` set, with none: pass with the warning; a generated child: `no_source`, any orphan blocks; `source` counts are `null` unless `baseline` is `measured` or `no_source`; every code in the report is a `{namespace, code}` mapping | read an unmeasured baseline's zero as measured; drop the `passes_through` line from the source-side handler |
| AT-38 | PS-2a or PS-2b0, whichever merges second (hand-over in section 8) | Through `check_keys`: a child against an `int32` parent and an `int64` parent (in that declared order) holding a value above the `int32` range gives exact counts and no `check_error` | create the union table from `parts[0].schema` |
| AT-39 | PS-2b | `_load` width guard: a `KeyBatches` whose declared schema is `int32` and whose batch is `int64` (a value above the `int32` range carrying a canary) raises the guard's coded error before any INSERT; `profiling.passes_through` passes it; the canary is nowhere in `str(exc)`, `__cause__` or `__context__`; a batch equal to the declared types loads as before | drop the guard and let DuckDB's `ConversionException` reach the caller |
| AT-40 | PS-2b | `runner.py` pass-through: on each post-scan exception path `runner.py` owns, a coded error (a `FileAccessReadError`) propagates as the same object, and a non-pass-through `RuntimeError` carrying a canary becomes an `error` finding whose message names the class only; the canary appears in no finding, report field or captured log line | drop the `passes_through` line from one `runner.py` handler; put `{exc}` in the message |
| AT-14 | PS-2a, PS-6, PS-8 | Canary `CANARY-7f3a` duplicated, orphaned, in a column hit and in a forced DuckDB error: absent from the report, `str(exc)`, `ReasonRecord`, logs and the `quality_metrics` dump | add a value to a finding |
| AT-23 | PS-3 | Generated parent faker key without `unique` is refused before any read; `row_count <= 1`, `unique: true`, a sequence, and `unique_together` exactly equal to composite parent columns are accepted; pair supersets and subsets are refused | accept a non-unique faker key |
| AT-23b | PS-3 | The five-file fixture census is complete (four parity fixture files plus `test_numeric_typing_readers.py`), taken with the lenient generate checks off: grep census and affected suites are red before migration, then green with same-Arrow-type unique keys and regenerated exact values; shipped examples and current docs have zero refused configs | omit `_f0_fixtures.py` from the census |
| AT-21 | PS-4 | FK findings: introduced orphans `fail`, inherited only `warning`, none `info`; a key that carries only warning codes (a mismatch over a mismatch baseline, a lone source-side `check_error`) derives `warning`; only a key that blocks without counts (`error`, `key_column_missing`, a blocking `key_type_mismatch`) derives `error`; a composite fills the tuples; tag stays v2; the three named fixtures change as listed; `compute_post_scan_integrity` retains FK findings when `keys=None` | keep the 1% threshold |
| AT-19 | PS-5 | Column facts on tables of 10, 10,000 and 250,000 rows over batches of 7 and 100,000: identity counts, `bytes_identical`, distinct counts and every detector rate equal the resident pandas values exactly. Batch boundaries change no result: an integer column whose nulls sit only in the second batch, plus a string column, give the same facts in one batch and in uneven batches as the whole table | average per-batch rates instead of summing hits; compare per-batch pandas frames whose dtype depends on the batch |
| AT-22 | PS-6 | `run_post_mask` adapter equals the old runner on every existing fixture, with one named carve-out: an integer column holding a null is read as whole numbers (4.4), and a dedicated test pins the finding that moves; a column near `min_rate` escalates exactly as on the resident path | read only the first batch |
| AT-31 | PS-7 | Unit: `run_resident` stamps `keys` and `verdict`; a second call is a no-op; `before_commit` on a required, unrun gate with a sink lacking `staged_tables` raises `check_error`. Every error the gate raises (a blocking verdict, a key `check_error` result, a gate that cannot run, a part that fails with an uncoded error) has `__cause__` and `__context__` `None` and no canary in `str(exc)`; a coded error from a part propagates as the same object | reset `ran` in `before_commit`; raise inside `except` |
| AT-16 | PS-8, PS-10 | A blocking verdict commits nothing. At PS-8 sequential and out-of-core no-sink cells block at step 5; full-frame also blocks. Sink cells for sequential, out-of-core, chunked and mixed land in PS-10: no output table, sink evidence aborted, temp files gone | run the gate after `sink.commit()` |
| AT-20 | PS-8, PS-10 | Gate runs once; every successful cell has the report in result `quality_metrics`, and each blocked cell carries it in the exception or existing isolated envelope field. Includes full-frame no sink, mixed with a sink and streaming off, a cross-table cycle, and from PS-10 streamed full-frame and out-of-core; PS-8 forced streaming refuses, PS-10 introduced orphan blocks | omit the step-6 report copy |
| AT-25 | PS-8 | A no-sink run whose gate never ran raises `check_error` after the route | delete the post-condition |
| AT-26 | PS-9 | Isolated child (subprocess and in-process fallback) with an introduced orphan: outcome `blocked`, reason `foreign_key_orphans_introduced`, report present, output dir absent; governor returns terminal `blocked`; bool and unknown outcome tokens are rejected | map the block to `crashed` |
| AT-15 | PS-10, PS-12 | One cell per route (sequential, chunked, out_of_core, mixed, unified_slice, stream, isolated; full_frame asserts no second copy): no `pq.read_table`, `ParquetFile.read`, `materialize_source` or whole-table `to_pandas` in the gate; Arrow peak under 4 times the batch bytes; DuckDB `memory_limit` at most 512MB; at an 8MB limit spill files appear | read staged output with `read_table` |
| AT-27 | PS-10 | A Parquet input rewritten between routing and the gate re-read refuses `lazy_source_changed`; CSV the same | skip `check_opened` |
| AT-28 | PS-10 | `_CallableSinkAdapter` with keys declared: run stays resident with `post_scan_keys_resident`, the full-frame gate runs before commit and blocks an introduced orphan | skip the resident gate when a sink exists |
| AT-29 | PS-10 | Declared-cast key column (`"01"` and `"1"` cast to `int64`) on a streamed route: baseline counts equal resident; `open_cast_run` attaches the exact run and re-read works after `finish()` | re-read without `cast_batch` |
| AT-30 | PS-11 | `source_loader` on the sequential route: the tee baseline equals the resident baseline; the loader is called once per table; a loader table without the key columns keeps the run resident | call the loader twice |
| AT-33 | PS-11 | A transform-bearing parent with `filter` under `preserve` has the same post-transform baseline and verdict on resident and sequential routes | compare sequential output with the raw re-read |
| AT-34 | PS-2a | The DMP multi-parent fixture checks one shared child tuple against the union of both parent key sets; a child found in either parent is not orphaned, and each parent PK is checked separately | check each edge independently |
| AT-17 | PS-12 | Route-coverage sentry: `GATE_TESTS` keyed on every `ROUTES` entry plus `stream` and `isolated` names a test that proves the gate ran and stamped `keys` | delete an entry |
| AT-18 | PS-12 | `integrity: true` with regex detectors takes sequential and out_of_core when eligible; model asks stay resident with `post_scan_models_resident`; stream mode no longer refuses for regex; the unified slice runs the gate on its return | leave `_unified_slice_admission.py:256` on the old predicate |
| AT-24 | PS-8 or SUB-RUN E2, whichever merges second | `subset:` with an excluded edge leaving orphan children: no block; the same run with a key remapped outside the parent: block; the baseline is read from the `engine.scratch` sources | read the pre-subset sources |

## 8. Slices

Sizes are hand-written lines, source plus tests. "Today": buildable from main, no open decision.

| Slice | Content | Size | Tier | Waits on | Start |
|---|---|---|---|---|---|
| PS-1 | 4.1 interfaces: `KeyBatches`, `KeyFacts`, `ChildKeyFacts`, `key_facts`; `_check` on it; projected `open_batches`; `_stream_input.py`; registry rename; AT-1 to AT-3 | 210 + 230 | R1 | none | building now |
| PS-2a | `_keys.py`: declared keys, unioned multi-parent facts, baseline/output facts, per-key codes and report, `error` results, coded errors through the public `profiling.passes_through`; AT-4 to AT-12, AT-14 keys, AT-34, AT-38 if it merges second | 190 + 250 | R2 | PS-1 | after PS-1 |
| PS-2b0 | `profiling/` only: `_comparable` families, the typed one-type-per-label `_load`, public `keys_comparable` (4.2.1); AT-35, AT-36, AT-38 if it merges second | 45 + 140 | R2 | PS-1 (on main) | today, beside PS-2a |
| PS-2b | `_blocked.py`: `post_scan_verdict` on codes and `baseline`, `verdict.warnings`, `severity_counts`, code order, exception, reason, blocking and pass-through sentries, the key-core comparison sentry; `baseline` and its error class in `_keys.py` through `keys_comparable` (4.2.2); `reasons.reason_ref_payload` with `phase._reason_wire` and `_keys.py` moved onto it, and its sentry (4.8); the `_load` width guard and the matching rule on `KeyBatches`; `profiling.passes_through` on every post-scan exception path in `runner.py` (both below); AT-13, AT-32, AT-37, AT-39, AT-40 | 195 + 370 | R2 | PS-2a, PS-2b0 | after both |
| PS-3 | Generated-parent rule plus the four fixture migrations in the same slice, one plan code; AT-23, AT-23b | 70 + 340 | R2 | none | today |
| PS-4 | FK finding from the keys facts (4.8), composite tuples, `run_post_mask(keys=)`, contract note, fixtures, pandas FK checker deleted; AT-21 | 90 + 200 (about 250 deleted) | R2 | PS-2b (`baseline`, 4.2.2) | after PS-2b |
| PS-5 | `_column_facts.py`, `profiling/_scan_integrity.py` public `column_distinct`, `_scoring.py` `counting_scope`; AT-19 | 190 + 230 | R2 | PS-1 | after PS-1 |
| PS-6 | `residual_pii.py`, `policy_validation.py`, `runner.py` on facts; adapter; AT-22, AT-14 suite part | 150 + 220 | R2 | PS-4, PS-5; rebases with M4 (section 10) | after PS-5 |
| PS-7 | `execution/pipeline/_post_scan.py` gate core, not yet wired: `post_scan_gate_applies` in `config/_post_scan.py` with its sentry, `PostScanGate`, `run_resident` (keys via PS-2a, verdict via PS-2b, column suite via `run_post_mask`), `before_commit`, exactly-once, error stripping, memory limit; physical-seam entry; AT-31 | 200 + 230 | R2 | PS-2b, PS-4 | after PS-4 |
| PS-8 | Wiring: bind and source-facts snapshot, resident fallback and report copy, `observed_commit` hook, central readability/model predicates, routing sites, reason, M5 adoption, sentries; AT-16, AT-20, AT-25, AT-14 | 140 + 330 | R2 | PS-7, PS-3 | after PS-7 |
| PS-9 | Isolated `blocked` outcome (4.5), governor terminal, `atomic_commit` sentry case; AT-26 | 60 + 180 | R2 | PS-8 | after PS-8 |
| PS-10 | `staged_tables()` on main's sink, `from_staged`, commit gate, identity checks, cast attachment and readable-sink cells; AT-15, AT-16, AT-20, AT-27 to AT-29 | 170 + 290 | R2 | PS-8 | after PS-8 |
| PS-11 | Loader and post-transform tee to scratch Parquet; loader and transform-bearing runs leave resident routing; AT-30, AT-33 | 90 + 180 | R2 | PS-10 | after PS-10 |
| PS-12 | Lift model routing, retire/add route code, lift stream refusal, gate unified slice; AT-15, AT-17, AT-18; release note names integrity withholding and the hold; relationships/reasons/CODEMAP docs | 120 + 280 | R2 | PS-6, PS-9, PS-11 | after those |

Total about 4,980 lines (about 1,830 source). PS-1 and PS-3 can start today in parallel; PS-5 after PS-1, beside PS-2a.

PS-7 and PS-8 block on key type families (4.2.1) and the baseline status (4.2.2), so both land
first. PS-2b0 touches only `profiling/` and needs only PS-1, so it starts beside PS-2a. PS-2b
waits on PS-2a and PS-2b0; PS-4 and PS-7 wait on PS-2b, and PS-8 on PS-7. The chain is acyclic.

AT-38 hand-over: AT-38 needs both `check_keys` (PS-2a) and the family rule with the typed `_load`
(PS-2b0) on main. Whichever of the two PRs merges second merges main into its branch first and
adds AT-38 in that same PR, before its gate. It is not left to PS-2b or any later slice.

PS-2b also closes two error paths found in the PS-2b0 reviews:

- `profiling/_scan_integrity._load` compares each batch's column types with the declared column
  types (`keys.schema`, and the DuckDB types created from it) before the INSERT. A batch wider
  than declared raises a value-free coded error that `profiling.passes_through` passes unchanged;
  it names the table and key label only. Today DuckDB's `ConversionException` would be raised
  instead, and its text can carry the value. The same rule is stated on `KeyBatches`: every
  batch's column types are the declared schema's types or narrower in the same family (AT-39).
- `verification/postmask/runner.py` applies `profiling.passes_through(exc)` (PS-2a) on every
  post-scan exception path it owns: a coded error propagates unchanged, any other keeps today's
  class-name-only finding. No exception text reaches a finding, the report or a log line (AT-40).

PS-3 is not split: its estimated fixture/test diff is about 340 lines, below the about-400 threshold, and the rule plus every migration must merge together so main never goes red.

Merge train and named `POSTSCAN-STREAM` release hold: only sink runs are held from PS-8 through
PS-11. No-sink runs remain readable on every route. During the hold a sink relationship job can
be refused in three ways: a lazy transform-bearing job when sequential is ineligible
(`execution/_pipeline_routing.py:175-191`), stream mode with relationships
(`execution/pipeline/_stream.py:177-188`; `_pipeline_generate_mask.py:220-225`), or explicit
`sequential`/`out_of_core` when routing makes it ineligible (`_pipeline_routing.py:302-326`). A
large auto job can also stay resident and hit `FK_FULL_FRAME_OOM_RISK_REJECTED`. PS-8, PS-10 and
PS-11 merge on one stacked train, one full suite runs on top, and no daily engine release is cut
between them. The release notes name the hold. PS-10 gates staged sinks; PS-11 restores loader and
transform-bearing streaming; PS-12 completes guarantee 1.

## 9. Test scope and full-suite triggers

```text
Tiers:      T0 per edit; T1 impact-selected per commit and slice head; T2 on the exact head
            with the section 6 and 7 mutations
Named:      tests/unit/detection_verification/postscan/, tests/unit/execution/post_scan/,
            tests/unit/profiling/test_key_type_families.py (PS-2b0),
            tests/sentry/post_scan/, pre-scan scan-integrity tests, reason registry tests
Triggers:   reason registry (PS-2a, PS-2b, PS-3, PS-8, PS-12); frozen field meaning (PS-4); routing
            (PS-8, PS-12); public execution types (PS-9). Full suite once per trigger head,
            diffed against the saved main baseline (decoy-vm-t3); Mac first, else vm-run.
```

## 10. Design notes and seams

- One choke point for publication (`observed_commit`), one key core (`key_facts`), one model
  predicate, one readability registry, one blocking decision (`post_scan_verdict`) and exception, one route list (`ROUTES`);
  typed inputs (`TableBatches`, the bound gate) over long parameter lists. A baseline, not a blind
  "keys must be unique" rule, because `preserve` and `warn` allow inherited breaches.
- SCAN-ML: its shared-file rules ("`residual_pii.py` by M4 only", "`config/_post_scan.py` by M1
  only") need amending for PS-6 and PS-8; M4 raises only through `post_scan_verdict`, and the
  second to merge rebases.
- F22: c1b edits `_pipeline_full_frame.py`, `_publication_observer.py`, `_chunked_output_sink.py`
  as PS-8 and PS-10 do (mechanical conflicts); `test_route_outcome_matrix` and AT-17 name each
  other; after c2 `StagedParquetWriter` keeps `staged_tables()` and the `atomic_commit` case goes.
- DELIV (row 61): 4.10. The vault is safe on a block: the CLI commits it only after `run_pipeline`
  returns. SUB-RUN: both plans add one file to `execution/pipeline/` (5 to 7 modules).
- SAMPLE (row 63): PS-8 depends on its head-mode contract that files staged for head-mode tables
  count as staged sources for the gate.

## 11. Placement

New files (CODEMAP "Where things go"):

| File | Directory | CODEMAP row | Sits beside |
|---|---|---|---|
| `_stream_input.py`, `_keys.py`, `_column_facts.py`, `_blocked.py` | `src/decoy_engine/verification/postmask/` | Directory Map `verification/` row (`CODEMAP.md:106`) | `runner.py`, `residual_pii.py` |
| `_post_scan.py` | `src/decoy_engine/execution/pipeline/` | "Pipeline spine and routes" (`CODEMAP.md:238`) | `_stream.py`, `_route_summary.py` |
| `_relationship_keys.py` | `src/decoy_engine/plan/checks/` | "Plan compile and checks" (`CODEMAP.md:237`) | `_generate_columns.py` |
| `_part2.py` | `src/decoy_engine/reasons/post_scan/` | "Reason codes" (`CODEMAP.md:250`) | `_part1.py` |
| tests for `verification/postmask/` | `tests/unit/detection_verification/postscan/` (parent holds 26 entries) | "Unit tests" (`CODEMAP.md:263`) | `tests/unit/detection_verification/test_bf4_fk_preservation.py` |
| tests for `execution/pipeline/_post_scan.py` | `tests/unit/execution/post_scan/` (parent holds 233 entries) | "Unit tests" (`CODEMAP.md:263`) | `tests/unit/execution/derived_visibility/` |
| `test_key_type_families.py` (PS-2b0) | `tests/unit/profiling/` | "Unit tests" (`CODEMAP.md:263`), mirroring `profiling/` | `test_scan_integrity.py` |
| `2026-11-12-postscan-ps2b0-build.md` (PS-2b0) | `docs/records/` | "Plans, decisions, records" (`CODEMAP.md:257`) | other build records |
| `postscan-ps2b0-key-type-families.md` (PS-2b0) | `changelog.d/` | "Changelog fragments" (`CODEMAP.md:259`) | other slice fragments |
| sentries | `tests/sentry/post_scan/` (parent holds 80) | "Sentry test" (`CODEMAP.md:254`) | `tests/sentry/profiling/` |

Changed modules (layer, size now, after; no ceiling or pin rises; only the DS-9 registry rows grow):

| Module | Layer | Now | After |
|---|---|---|---|
| `verification/postmask/_stream_input.py`, `_keys.py`, `_column_facts.py`, `_blocked.py` | verification | 0 | about 140, 260, 190, 130 |
| `execution/pipeline/_post_scan.py` | execution | 0 | about 300 (gate plus loader tee) |
| `plan/checks/_relationship_keys.py` | plan | 0 | about 80 |
| `detection/detectors.py` | detection | existing | +2 names: re-exports `counting_scope` and `_confidence_bucket` through its existing `_scoring` import block, so the column suite imports the package's public module (PS-5 deviation, accepted) |
| `profiling/_scan_integrity.py`, `profiling/__init__.py`, `profiling/scan_set.py` | profiling | 427, existing, 527 | about 490 (PS-5 `column_distinct`, PS-2b0 families, typed `_load` and `keys_comparable`), export swap plus `passes_through` and `keys_comparable`, about 527 (PS-2a makes `_is_coded` with `_PASS_THROUGH` the public `passes_through`) |
| `profile/_readers.py`, `profile/_lazy_csv.py`; `detection/_scoring.py` | profile; detection | 568, 246; 192 | about 575, 250; 220 |
| `reasons/_identity.py`, `reasons/__init__.py`; `phase.py` | reasons; top level | 58, existing; 578 | about 68 (`reason_ref_payload`), one export; about 576 (`_reason_wire` calls the helper). Both stay under the 600 goal, so neither needs a census entry |
| `verification/postmask/__init__.py`, `residual_pii.py`, `policy_validation.py`, `fk_preservation.py`, `runner.py`, `types.py` | verification | existing, 398, 221, 337, 190, 148 | export swap, about 360, 200, 90, 210, 160 |
| `config/_post_scan.py`, `config/__init__.py`; `plan/checks/_generate_columns.py` | config; plan | 135, existing; 515 | about 175, export swap; 517 |
| `execution/_pipeline_full_frame.py`, `_pipeline_finalize.py`, `_pipeline_route_dispatch.py`, `_publication_observer.py`, `_transactional_sink.py` | execution | 317, 503, 151, 158, 438 | about 330, 470, 170, 165, 460 |
| `execution/_chunked_output_sink.py` | execution | 598 | at most 600 (call swap only) |
| `execution/_sequential.py` | execution | 660 | at most 660 (ceiling 680; the tee lives in `_post_scan.py`) |
| `execution/_isolated_run.py` | execution | 659 | at most 667 (ceiling 672; SUB-RUN E2 lowers it) |
| `execution/_isolated_worker.py`, `_isolated_common.py`, `_isolated_reason.py` | execution | 271, 388, 52 | about 285, 389, 75 |
| `execution/_governor.py`, `_unified_slice_admission.py`, `_pipeline_routing.py` | execution | 588, 571, 486 | about 596, 571, 490 |
| `execution/_pipeline_routing_signals.py` | execution | 613 | at most 613 (census entry, ceiling 633; swap only) |
| `execution/_pipeline_context.py` | execution | 594 | at most 596 |
| `execution/_pipeline_mixed_route.py`, `_pipeline_generate_mask.py` | execution | 314, 394 | call-site swaps only |
| `execution/capacity.py`, `_job_estimate.py`, `_transforms_admission.py` | execution | 541, 210, 394 | readability call-site swaps only |
| `execution/physical/_compiler.py`, `_shadow_generation.py` | physical | 444, 483 | readability call-site swaps only |
| `generation/_streamed.py` | generation | 594 | unchanged (exemption) |

`verification/postmask/` goes from 6 modules (7 with M5) to 11, under 25. `execution/pipeline/` goes from 5 to 6 (7 with SUB-RUN). `execution/` top level stays at its freeze.

Registries:

- Physical-seam sentry: `execution/pipeline/_post_scan.py` joins `GUARDED_MODULES`
  (`tests/sentry/test_physical_seam_disconnection.py:64`) in PS-7.
- Physical-seam exact-diff registry: in each of PS-7 through PS-12, every changed or added file
  below `src/decoy_engine/execution/` outside `physical/` is added to `permitted_non_physical` for
  that slice (`tests/sentry/test_physical_seam_disconnection.py:286-1001`); no broader allowlist.
- File-access registry (`tests/sentry/file_access_registry.toml`): the `_scan_integrity.py` entry
  at `:1354` moves from `_check` to `key_facts` (PS-1); new entries for `column_distinct` (PS-5),
  the gate's staged-file reads through `txn.open_file` (PS-10) and the loader tee's scratch Parquet
  writes and reads (PS-11), each with disposition, rule and owner `cam`.
- Reason codes: section 5 (retry class, owner module, file, registry version). PS-2a, PS-2b, PS-3,
  PS-8 and PS-12 add each new producer site to
  `tests/fixtures/f29/producer-inventory.json` (`tests/sentry/_baseline_sources.py:138-146`).
- Sentry manifest: PS-2b registers the blocking, pass-through, key-type comparison and
  reason-identity serializer sentries,
  PS-8 the commit/readability/import sentries, and PS-12 route coverage in
  `scripts/dev/sentry_registry.toml`; each is covered by
  `tests/sentry/test_sentry_manifest.py` (CODEMAP "Sentry test", `CODEMAP.md:254`).

## 12. Recurring-defect coverage

Catalog check: no new regex validator; wire enums and report tag remain checked tokens; AT-31 proves empty exception chains on every gate raise and AT-12 that key checks keep coded errors whole; v2 report reads stay additive and AT-26 rejects wrong-typed or unknown isolated outcomes; sentries normalize direct, alias and `getattr` forms; imports use public APIs; sentries run without hidden-success flags; staged, foreign and no-sink inputs keep separate trust paths; final heads rerun section 6 mutations; no forwarding helper is allowlisted; no limit, pin or ceiling rises (`MEMORY_LIMIT` and `_BATCH_ROWS` stay fixed).

## 13. Pre-mortem

- "Valid runs started failing." The baseline covers inherited breaches (AT-6); non-unique
  generated parents are refused at plan time and all four fixtures migrate in PS-3 (AT-23b).
- "Big FK jobs got refused." Only sink runs during the PS-8 to PS-11 train, which no release cuts.
- "Runs got slower." Key checks read key columns only; exact distinct counts add one DuckDB pass
  per configured column. PS-10's record times the 10M laptop demo with and without the gate.
- "F22 moved the sink." The `staged_tables()` contract (4.6) is posted on the F22 PRs.
- "Report numbers moved." The column suite is exact (AT-19, AT-22) except that an integer column
  holding a null is now read as whole numbers, so digit detectors can report a finding there that
  the old runner missed (4.4; named in the release note); FK severities move on purpose (AT-21).
- "`integrity: true` runs stopped writing." A column-suite `fail` now blocks before commit (row 61
  ruling); today it only reports. The release note and `docs/relationships.md` name it and the
  `POSTSCAN-STREAM` hold.

## 14. Recorded decisions

Taken as recommended under the standing rule; the owner may overturn.

- Q1: key checks run whenever relationships are declared, with or without `post_scan.integrity`.
- Q2: an introduced key breach, or any finding `post_scan_verdict` calls blocking, blocks before commit.
- Q3: no `tables[].primary_key` field in V1; keys are declared only in `relationships:` (V2-LOG).
- Q4 (row 61): a post-scan block withholds the whole run in strict and best-effort delivery; the
  gate takes no delivery mode.
- Q5 (PS-5 review): integer columns holding a null are read as whole numbers by the column suite,
  not as floats; residual-PII findings on such columns may appear where the old runner had none.

## 15. V2-LOG entries (backlog-ready)

- **POSTSCAN-GEN-STREAMED.** Run the gate on `generate_tables_streamed` and
  `generate_tables_streamed_from_generation_plan`, which commit through `OutputPublish` with no
  bound gate, so their output is not key-checked. Fix: bind a gate in `run_generation` from the
  `GenerationPlan` relationships. Not a V1 path: the CLI generates through `run_pipeline`.
- **POSTSCAN-PRIMITIVES.** Regex and key post-scan on the direct mask primitives
  (`run_sequential`, `run_fk_out_of_core`, the chunked entries); M5 refuses only model asks there.
- **POSTSCAN-NER-STREAM.** NER post-scan on streamed routes (M4 model passes over batches); M5's
  guard summary is reworded then.
- **POSTSCAN-ATTRIBUTION.** Exact per-row introduced orphans when quarantine drops rows.
- **CONFIG-PK.** `tables[].primary_key` for tables no relationship references (Q3).
- **POSTVAL-STREAM.** Stream the A1 post-validation suite.
- **PK-COMPOSITE-BUG.** `validation/post/_checks/_pk_uniqueness.py` checks composite keys per column.
- **FK-VALIDATORS-CORE.** Move `validators/_fk_validators.py` onto `key_facts`.
- **POSTSCAN-PER-TABLE-DELIVERY.** Under best-effort delivery, drop only the tables a post-scan
  finding names (and their FK children) instead of the whole run (Q4). Needs per-table outcomes
  from the verdict and a CLI delivery change.
- **SENTRY-SPELLINGS.** Commit and import sentries cover direct, aliased and `getattr` calls;
  dispatch through other containers is not covered. A future off-thread commit would also lose
  the context-variable gate binding; current commit sites use no thread handoff.
- **POSTSCAN-MASKED-PARENT-WARNING.** Warn at plan time when a masked relationship parent key uses
  a non-injective strategy (`cardinality_mode: reuse`, `allow_collisions`, or faker without a
  unique pool). V1 keeps the exact post-scan block; `plan/_seed_envelope.py:179-219` shows the
  collision-bearing options.
- **POSTSCAN-COLUMN-WAIVER.** Add a per-column waiver for heuristic detector false positives.
  V1 keeps `integrity: true` fail-closed and documents that output is withheld.
- **POSTSCAN-NULL-KEY-TYPE.** An all-null key column read as Arrow `null` is an "other" type, so
  it blocks over a comparable source (a false block); let `null` match any family.

CLI follow-ups: `src/decoy/_native_gate.py:236-250` still says post-scan integrity disqualifies
native; SCAN-ML-4 (`run --chunked` skips regex post-scan) closes when RUN-STREAM C2 lands.

## 16. Plan contract

### Class table

| # | Class | Applies? | Closing structure | Proof and mutation |
|---|---|---|---|---|
| 1 | Value-bearing output | Yes: key and column values pass through the gate | facts types hold ints, names and `ReasonRef` only; errors kept as type names via `safe_error_class` | AT-12, AT-14; mutation: add a value to a finding |
| 2 | Cleanup on an exit path | Yes: DuckDB connection, spill dir, staged files on block | `finally` close in `key_facts`; scratch owned by the arena or `protected_scratch_dir`; route abort paths unchanged | AT-16; mutation: skip `conn.close()` and assert the spill dir is gone |
| 3 | State machine and concurrency | Yes: gate `required` and `ran` per run | context variable per run; exactly-once flag; commit refusal; post-condition | AT-20, AT-25, AT-31; mutation: reset the flag in `before_commit` |
| 4 | Route parity and silent fallback | Yes: the reason for this row | gate inside `observed_commit`; unreadable output keeps the resident route; coverage sentry on `ROUTES` | AT-15 to AT-18, AT-28; mutation: drop a route entry |
| 5 | Referential integrity | Yes | `key_facts` PK and MATCH SIMPLE FK over output, baseline over inputs; type families compared exactly; a baseline status so an unmeasured zero is never read as measured | AT-4 to AT-11, AT-35, AT-37; mutation: join on `k0` only |
| 6 | Untrusted producer | Yes: staged files, foreign sinks, loaders | schema and counts from the file; a foreign sink is never read, the run stays resident | AT-28, AT-30; mutation: trust the route's row count |
| 7 | Check then use (TOCTOU) | Yes: inputs re-read after masking | `check_opened` against the gate's bind-time snapshot; CSV identity check | AT-27; mutation: skip the check |
| 8 | Weak tests and overclaimed evidence | Yes: memory tests can pass on tiny data | AT-15 forces an 8MB DuckDB limit and asserts spill files; AT-19 uses multi-batch tables | mutation: a whole read turns AT-15 red |
| 9 | Behavior-preserving refactor drift | Yes: `_check` split, runner adapter | AT-1, AT-3, AT-22 against the old outputs | mutation in AT-1 |
| 10 | Frozen contract drift | Yes: report tag, `fk_preservation` meaning, registry | additive fields; one recorded semantic change; one retirement with `replaced_by`; manifest fixtures | AT-21; registry tests; mutation: bump the tag |

### Enumerations

#### Exit paths

- Gate passes: report stamped, commit continues (`execution/_publication_observer.py:103`).
- Gate blocks: `PostScanBlockedError` before commit; the route aborts the sink
  (`execution/out_of_core/_runner.py:315`).
- Gate errors: `post_scan.check_error`, blocks (`reasons/post_scan/_part1.py:15`).
- Gate skipped for a foreign sink: `warning.post_scan_output_unreadable`, run continues.
- Gate never ran: `post_scan.check_error` raised after the route
  (`execution/_pipeline_route_dispatch.py:35`).
- Source changed: `runtime.lazy_source_changed` (`reasons/runtime/_part1.py:216`).

#### Routes

- `full_frame`, `sequential`, `chunked`, `out_of_core`, `mixed`, `unified_slice`
  (`declared_casting/_eligibility.py:26`).
- Streamed generation (`generation/_streamed.py:524`).
- Isolated worker (`execution/_isolated_worker.py:113`, `_stage_outputs`).
- Stream mode (`execution/pipeline/_stream.py:187`).
- Direct primitives: out of scope (section 1).

#### FK shapes

- Single column.
- Composite tuple (`profiling/_scan_integrity.py:300`, `_complete`).
- Nullable FK (MATCH SIMPLE).
- Self-reference.
- Cross-table cycle (each edge alone).
- Generated parent with a mask child: baseline 0.

#### Fields

- `keys[]`: `table`, `columns`, `kind` (`primary`, `foreign`), `parent` (foreign only), `bounded`,
  `codes`.
- `keys[].status`: `checked`, `key_type_mismatch`, `key_column_missing`,
  `skipped_unreadable_output` (extends `Status` at `profiling/_scan_integrity.py:40`).
- `keys[].source` and `keys[].output`: `rows`, `null_key_rows`, `duplicate_excess` or
  `orphan_rows`, `orphan_distinct_keys`.
- `sample` per table: `rows`, `of_rows`.
- `verdict`: `blocking`, `codes`.

#### Output surfaces

- `quality_metrics["post_scan_integrity"]` (stamped today at `execution/_pipeline_finalize.py:503`).
- `PostScanBlockedError` message and attributes.
- `ReasonRecord`s.
- Publication evidence state (`execution/_publication_observer.py:110`).
- RunResult `Check` (`run_result/_model.py:182`, through RESULT row 62).
- Log lines: none added that carry data.

### Reuse inventory

`profiling/_scan_integrity.py` (`_check`, `_child_facts`, `_comparable`, `_labels`,
`_before_fetch`, `_open_keys`, `_load`; public `keys_comparable` on `_comparable`);
`profiling/scan_set.py` pass-through rule
(`_PASS_THROUGH`, `_is_coded`, public as `passes_through`); `reasons/_identity.py` `ReasonRef`
and the new `reason_ref_payload`, which `phase._reason_wire` (`phase.py:470`) moves onto;
`parse_relationship_config`; `out_of_core/_duckdb.py`
(`connect_duckdb`, `DuckDBRunRecorder`, the `bind_duckdb_run` pattern); `resolve_ooc_memory_limit`;
`_chunked_input.py` (`capture_source_facts`, `check_opened`, `facts_match`);
`LazySource.open_batches` and the `CsvLazySource` identity check; `CastRun.cast_batch`;
`OutputTxn.open_file`, `protected_scratch_dir`, F25 `ScratchArena`; `safe_error_class`;
`observed_commit`; `_evaluate`, `_confidence_bucket`; SCAN-ML M5 `refuse_unchecked_route`;
`check_generate_columns`; the `test_f06_route_matrix.py` pattern; `registry_renumber.py`.

### Frozen contracts

`post-scan-integrity/v2` (additive fields plus the recorded `fk_preservation` change, compat 3.1);
the reason registry (append-only, one retirement with `replaced_by`); `run_post_mask` (one keyword
added); `CheckId` (unchanged); `IsolatedRunOutcome` and `GovernorResult.outcome` (one value added,
pre-GA).

## Amendments 2026-11-13 (PS-8 build, tech-lead rulings)


- 4.2, generated parent. The baseline of an edge whose parent is generated is "inherited": the count of input child key values that match no output parent key. The gate then measures introduced orphans as output orphans minus that count, both against the output parent keys. "A generate table has baseline 0" still holds for a generated child. A source orphan under `preserve` or `warn` therefore does not block, and a mask that breaks a matched child key still does.
- 4.3, declared casting. Until PS-10, a job with declared columns and declared keys stays on the resident route (`route.post_scan_keys_resident`), because the bounded routes cast per chunk and the gate has no cast re-read yet.
- PS-10 note: lift that clause when the cast attachment lands (AT-29), and give the two B-6 publish cells in `test_b6a_publish.py` their sink back once a staged sink is readable.
- Route types: routing takes `ReasonCode | None` (the type route reasons already have), not `ReasonRef`.

