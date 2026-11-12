Status: plan (revision 2, 2026-11-12, not built). Engine only. Roadmap row 60 (CLI `docs/ROADMAP.md`).

# POSTSCAN-STREAM: post-scan integrity on every run route, with output key checks

Repo `louiskeep/decoy-engine`, plan branch `plan/postscan-stream`. Base: engine `origin/main`
`a001f4d7` (#510). Code citations are checked at that head.

Risk: R2. It adds a check that blocks a run before its output commits, puts that check inside the
commit choke point every engine sink uses, and changes routing for relationship and
`post_scan.integrity: true` jobs. No public API removal, no schema major, no lock change.

Rules consulted: `~/dev-rules/README.md`, `00-universal.md`, `risk-and-exceptions.md`,
`development-loop.md` (PLAN), `testing.md` (Test depth, escalation triggers),
`cross-cutting-concerns.md`, `catalogs/recurring-defects.md` (classes 1 to 11), engine `CLAUDE.md`
and `CODEMAP.md` ("Where things go").

Also read: CLI `docs/ROADMAP.md` (rows 57, 58, 60 to 63); F22 rev 11 (5.6, 5.7, slices); SUB-RUN
(4.2, 4.3, 10); SCAN-ML (`plan/scan-ml` 4.5, 11, 13; M5 is #508); DELIV (4, 7.4);
`docs/compatibility-contract.md` 3.1; `docs/reasons.md`.

## Revision 2

Dennis r1 was NO-GO (8 V1, 9 DESIGN, 4 V2-LOG). Changes by finding: F1 gate inside
`observed_commit`, no-sink post-condition, relationship jobs resident until the streamed gate
(4.5, 8); F2 resident call only without a sink; F3 streamed generation exempt (1, 15); F4
child-side gate, outcome `blocked`; F5 unreadable sink stays resident; F6 Parquet identity via
`check_opened`, loader tee (4.3); F7 PS-1 interfaces (4.1), staged Parquet in PS-10; F8 FK
severity from the keys verdict; F9 one model predicate (4.7); F10 one exception and code order
(4.8); F11, F12 Placement (11); F13 unified-slice call (4.5); F14 exact counts (4.4); F15
plan-time uniqueness (4.9); F16 SUB-RUN (4.3); F21 `staged_tables()` (4.6); F17 to F20 text,
V2-LOG (15), F20 answer (4.3); main claims corrected (2); decisions (14); slices re-cut (8). Row
61 addendum: one blocking decision for all severities (4.8), CLI delivery contract (4.10), Q4.

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

## 2. Measured today (`a001f4d7`)

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

Corrections to the first revision (Dennis r1 section 1):

- Parquet `LazySource` has no file-identity check (`profile/_readers.py:214-330`); only
  `CsvLazySource` does (`profile/_lazy_csv.py:185-233`). Parquet relies on the routing snapshot:
  `capture_source_facts` (`execution/_chunked_input.py:123`) and `check_opened` (`:248-252`).
- CSV has column projection (`profile/_lazy_csv.py:204`). `Status` is at
  `profiling/_scan_integrity.py:41`. `resolve_ooc_memory_limit` returns an `OutOfCoreBudget`
  (`out_of_core/_budget.py:410`) whose `memory_limit` is the string used.
- F22: #488, #489, #496 are a1a, a1b, a2; c1a, c1b, c2 are unbuilt; rev 11 is on main. SCAN-ML M4
  has no code and `PostScanBlockedError` is on no ref. REG-RELEASE E2 is open as #506.

The pre-scan key core: `_check` (`profiling/_scan_integrity.py:304`) loads key columns as Arrow
batches (`_BATCH_ROWS` 100,000) into DuckDB (`MEMORY_LIMIT` 512MB, spill under
`protected_scratch_dir`) and counts parent null-key rows, duplicate excess, and per child null-key
rows and `NOT EXISTS` orphans (`_child_facts`, `:358`). Keys are declared only in `relationships:`.

## 3. Established solutions

Searched 2026-11-12: DuckDB larger-than-memory joins and aggregates; dbt `unique`, `not_null`,
`relationships`; Great Expectations compound uniqueness; Polars streaming anti joins.

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

Custom code left: the batch readers, the source-versus-output verdict and the hit collector. No
library compares a masked output with its input for these types.

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
  (`:321-324`); post-scan to `post_scan.key_type_mismatch`.
- `batches()` is called once per load; a self-reference passes two `KeyBatches` over one table and
  reads it twice. Stated, not optimized.
- `scratch` is caller-owned; `key_facts` spills under it and closes the connection in `finally`.
  Exceptions propagate; callers strip them with `safe_error_class` (`_progress_events.py:85`), as
  pre-scan does (`profiling/scan_set.py:296-303`). No second sanitizer.
- `_check` becomes `_open_keys`, then `key_facts`, then a map to `RelationshipFacts` (each
  `ChildKeyFacts` plus the child's table and columns becomes a `ChildFacts`; `exact=True`).

`LazySource.open_batches` (`profile/_readers.py:257`) and `CsvLazySource.open_batches`
(`profile/_lazy_csv.py:214`) gain `columns: Sequence[str] | None = None`, forwarded to the existing
projection (pyarrow `iter_batches(columns=)`, CSV `_chunks(batch_rows, columns)`).

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
              on_open: Callable[[OpenedLazyBatches], None] | None = None) -> TableBatches: ...
def missing_columns(table: TableBatches, columns: Sequence[str]) -> tuple[str, ...]: ...
def key_batches(table: TableBatches, columns: Sequence[str]) -> KeyBatches: ...  # labels k0..kn
```

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
| output parent and child key types not comparable | always | `post_scan.key_type_mismatch` |
| read, DuckDB or spill error in key checks | always | `post_scan.check_error` (reused) |

The baseline keeps valid runs valid: `preserve` and `warn` keep source orphans, `remap` gives them
fresh values that stay orphans, inherited duplicates are not the run's fault. A generate table has
baseline 0. `orphan_policy: fail` aborts earlier. Shapes: single column; composite (one tuple);
nullable FK (a tuple with a null part is counted, never an orphan); self-reference (two reads);
cross-table cycles refused before the run (`relationships/_graph.py`).

### 4.3 Baselines and identity (F6, F16, F20)

- Resident inputs: the gate reads the sources the route masked from, after the pre-mask cast
  (full-frame `step.sources`; unified slice `caller_sources` after `cast_run.pre_mask`).
- Parquet `LazySource` inputs: re-read through `from_lazy` with `on_open` calling
  `check_opened(h, facts[name], table=name)` against the run's `capture_source_facts` snapshot. A
  changed file refuses with `runtime.lazy_source_changed`, the check chunked and mixed already
  trust (row count, schema with metadata, null counts, row-group layout).
- `CsvLazySource` inputs: re-read through `from_lazy`; its identity check refuses a change.
- Loader inputs (`source_loader`): the baseline is taken in the read the route already does
  (PS-11). Until PS-11, and for any loader the tee cannot wrap, the run keeps the resident route.
- SUB-RUN: with `subset:` the inputs are the post-subset scratch sources (`field_key=
  "engine.scratch"`, SUB-RUN 4.2), which outlive the gate (`SubsetStageScope` wraps `run_pipeline`).
  Orphans kept across `excluded_edges` are in the baseline (AT-24). SUB-RUN refuses a subset with
  a `source_loader`.

F20 answer: every run path reads CSV as text (`profile/_lazy_csv.py:134`,
`profile/_csv_reader.py:506-507`), and so does pre-scan. Output keys differ from text only where
declared casting types a key column, so the baseline reads through the same cast: resident routes
use the cast sources; a streamed re-read applies `CastRun.cast_batch(table, batch, record=False)`
(`declared_casting/_boundary.py:210`) over the key and cast columns. Quarantined rows drop from both
sides alike.

### 4.4 Column suite (exact, F14)

Runs under `post_scan.integrity: true`, as today. `verification/postmask/_column_facts.py`
computes per configured column, in one pass over aligned batches: row counts, comparable and
identical rows (the positional comparison of `residual_pii.py:241-247`), `bytes_identical`, a
constant-source flag, and exact distinct counts through `profiling.column_distinct(batches, *,
scratch, memory_limit) -> int` (DuckDB `count(DISTINCT ...)` over a `RecordBatchReader`).

Detector rates are exact: `detection/_scoring.py` gains `counting_scope()`, a context variable to
which `_evaluate` reports `(detector_id, hits, n, threshold, name_hint)` before its threshold test.
The suite runs `run_all_detectors` per batch inside the scope, sums hits and `n`, and decides with
the same threshold and `_confidence_bucket`. `sample_misses` stays empty here (it holds values).

`residual_pii.py` and `policy_validation.py` keep their decision tables and take these facts.
`run_post_mask(source_frames, output_frames, *, config, keys=None)` stays the public adapter.
Model passes (SCAN-ML M4) need resident values; the model predicate keeps those runs resident and
the passes read `TableBatches.resident`.

### 4.5 The gate

`execution/pipeline/_post_scan.py`:

```python
class PostScanGate:
    required: bool  # keys declared, or post_scan.integrity true
    ran: bool
    def run_resident(self, outputs: Mapping[str, pa.Table], sources: Mapping[str, pa.Table],
                     table_kinds: Mapping[str, str]) -> None: ...
    def before_commit(self, sink: Any) -> None: ...  # called by observed_commit
def bind_gate(ctx: PipelineRunContext) -> ContextManager[PostScanGate]: ...  # context variable
def active_gate() -> PostScanGate | None: ...
```

1. `execute_route` binds one gate per run (precedent `bind_duckdb_run`,
   `_pipeline_route_dispatch.py:113`).
2. Full-frame: at `_pipeline_full_frame.py:308`, `gate.run_resident(...)` runs only when
   `ctx.publish.active_sink is None`; `compute_post_scan_integrity` is deleted (F2).
3. Unified slice (PS-12, F13): `_render_and_seal` runs `gate.run_resident(outputs, caller_sources,
   ...)` between `cast_run.post_mask` and `cast_run.finish`; with no cast run, on
   `unified_slice_result.outputs` before the return at `:162`. Relationships already decline this
   route (`_unified_slice_admission.py:259`).
4. Commit: `observed_commit(sink)` calls `active_gate().before_commit(sink)` before `sink.commit()`.
   Not required, or already ran: return. Sink has `staged_tables()` (4.6): run over the staged
   files. Otherwise raise `post_scan.check_error`; the commit does not happen (F1). Routing keeps
   such runs resident (4.7); this is the backstop.
5. No sink: after the route returns, `execute_route` runs the gate over `result.outputs` if it has
   not run, then raises `post_scan.check_error` if a required gate still has not run.
6. Exactly once: `ran` is set when a run completes; later calls are no-ops.
7. Errors: caught per part, kept as `safe_error_class(type(exc).__name__)`, the coded error raised
   after the `except` block (no cause, no context).
8. Blocking: `post_scan_verdict` blocking raises `PostScanBlockedError` before `sink.commit()`; the
   route's failure path aborts the sink (`out_of_core/_runner.py:314-316`, `_sequential.py`,
   `OutputPublish.__exit__`).
9. Memory: DuckDB `memory_limit` is the smaller of `MEMORY_LIMIT` and
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

`config/_post_scan.py` holds the one model predicate `post_scan_model_setting(config) -> str |
None` (`post_scan.detectors` other than `regex`, or `post_scan.ner_findings: fail`) and the helper
`post_scan_resident_reason(config, *, output_readable: bool) -> ReasonRef | None`:

| Slice state | Returns |
|---|---|
| PS-8 to PS-11 | `route.post_scan_integrity_requested` when integrity is asked; else `route.post_scan_keys_resident` when keys are declared and the output cannot be read back for the check |
| PS-12 on | `route.post_scan_models_resident` when `post_scan_model_setting` is set; else the keys clause |

`output_readable` is false for every streamed route in PS-8; from PS-10 true when the sink has
`staged_tables()`, from PS-11 also when the loader can be teed. Estimator and capacity sites
(`capacity.py:364`, `:426`, `_job_estimate.py:161`, `physical/_compiler.py:437`,
`physical/_shadow_generation.py:422`, `_transforms_admission.py:101`) pass `True` (they price the
engine sink). Run-time sites (`_pipeline_routing_signals.py:579`, `_chunked_output_sink.py:124`,
`_unified_slice_admission.py:256`) pass the real value. `decide_execution_route` takes
`post_scan_resident: ReasonRef | None` in place of the bool.

`route.post_scan_keys_resident` keeps one meaning in every slice: keys must be checked and this
route cannot read its output back. At PS-12 `route.post_scan_integrity_requested` retires with
`replaced_by=("route", "post_scan_models_resident")`. M5 (#508): if it merges before PS-8, PS-8
moves `_asks_for_models` and the `ner_findings` test from `_route_guard.py` into
`post_scan_model_setting`; if PS-8 merges first, #508 rebases onto it. One copy either way.

### 4.8 Verdict, report and exception (F8, F10)

`quality_metrics["post_scan_integrity"]` gains `keys` (per key: `table`, `columns`, `kind`,
`parent`, `status`, `bounded`, `codes`, and `source` and `output` counts) and `verdict: {blocking,
codes}`. Fields are additive under `post-scan-integrity/v2`.

`PostScanBlockedError(DecoyError)` is SCAN-ML M4's class (4.5 there): the value-free report in a
field, the registry's fixed summary as `str(error)`, raised with an empty chain. Whichever of M4 and
PS-2b builds first defines it in `verification/postmask/_blocked.py`; the other uses it.

`post_scan_verdict(report) -> PostScanVerdict` in `_blocked.py` is the one blocking decision; the
gate calls it and raises on `blocking`. The report blocks when any of these holds: a key code
(4.2); a finding of severity `fail` in `residual_pii`, `policy_validation` or `fk_preservation`;
a finding of severity `error`, or a set `pass_failed_with`; an M4 NER block. Severities are the
`Severity` literals (`verification/postmask/types.py:20`: `info`, `warning`, `fail`, `error`);
`info` and `warning` never block. An `fk_preservation` `fail` is the same fact as
`foreign_key_orphans_introduced` and adds no second code.

One code goes on the record, by this order (first present wins); `verdict.codes` lists every
blocking code in the same order:

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

`fk_preservation` (frozen v2 field) derives from the keys facts: `fail` when orphans were
introduced, `warning` when all output orphans are inherited, `info` when none, `error` when
unchecked. A composite fills the additive `parent_columns` and `child_columns` tuples;
`parent_column` and `child_column` hold the single name, or the names joined by `", "` for display.
The rate thresholds (`fk_preservation.py:165-195`) and the dead `orphan_policy == "skip"` branch go.
PS-4 records this pre-GA semantic change in `docs/compatibility-contract.md` 3.1 and updates on
purpose `tests/unit/detection_verification/test_bf4_fk_preservation.py`,
`test_postmask_runner.py` and `test_postmask_batch_b.py`. Per-table blame stays in the findings for
display; V1 delivery does not act on it (4.10).

### 4.9 Generated relationship parents (F15, PS-3)

A relationship whose parent table is generated needs a key the plan can show is unique: a
`sequence` with non-zero `step`, `unique: true`, a composite with one such column, or
`unique_together`. Otherwise `check_generated_parent_keys(config)` (`plan/checks/_relationship_keys.py`)
refuses with `plan.relationship_parent_not_unique`, naming table, columns and the fix ("set `unique:
true` on the key column or use a `sequence`"). `check_generate_columns` calls it last, so
`decoy validate` and `decoy run` refuse alike (`plan/_compile.py:212`, `:532`); `_compile.py` does
not grow. No generated output changes.

### 4.10 Contract for CLI delivery (row 61)

The verdict carries `blocking`, `codes` (4.8 order) and the value-free report (keys, column
findings with `table`, `child_table`, `severity`). On a block the engine raises
`PostScanBlockedError` (`reason` is the first code, `report` the full report) and nothing has
committed. It is raised inside `observed_commit` before `sink.commit()` on a sink route, in
`execute_route` after the route returns on a no-sink route, and in the isolated child (or the
in-process fallback), which surfaces as `IsolatedRunResult` outcome `blocked` with the same reason
and report. CLI delivery keeps no severity rule: it maps the exception or a `blocked` outcome to
"nothing delivered, exit 4". V1 withholds the whole run in strict and best-effort modes alike
(Q4), so the gate takes no delivery mode. A requested post-scan with no report is a block on the
CLI side; no engine route returns without one (4.5 steps 4 and 5).

## 5. Failure modes

| Failure | Result | Code (owner module) |
|---|---|---|
| Output PK duplicates above baseline | block, nothing commits | `post_scan.primary_key_duplicates_introduced` (new, `verification.postmask._keys`) |
| Output PK nulls above baseline | block | `post_scan.primary_key_nulls_introduced` (new, same) |
| Output FK orphans above baseline | block | `post_scan.foreign_key_orphans_introduced` (new, same) |
| Declared key column missing from output | block | `post_scan.key_column_missing` (new, same) |
| Output key types not comparable | block | `post_scan.key_type_mismatch` (new, same) |
| Read, DuckDB or spill error in key checks | block, type name only | `post_scan.check_error` (reused) |
| Column-suite check raises (`error` finding, `pass_failed_with`) | block, type name only | `post_scan.check_error` (reused) |
| Column-suite finding of severity `fail` | block | `post_scan.finding_blocked` (new, `verification.postmask._blocked`) |
| Required gate cannot run at commit | commit refused | `post_scan.check_error` (reused) |
| Required gate did not run (no sink) | refused after the route | `post_scan.check_error` (reused) |
| Source file changed before the re-read | refuse | `runtime.lazy_source_changed` (reused) |
| Keys declared, output unreadable | resident route | `route.post_scan_keys_resident` (new, `config._post_scan`) |
| Model detectors or `ner_findings: fail` | resident route (PS-12) | `route.post_scan_models_resident` (new; retires `route.post_scan_integrity_requested`) |
| Generated parent key not provably unique | refused at plan time | `plan.relationship_parent_not_unique` (new, `plan.checks._relationship_keys`) |
| Isolated child blocked | outcome `blocked`, report kept, nothing committed | the blocking `post_scan` code |

Retry classes: the six new `post_scan` codes are `CONFIGURATION_REPAIR` (fix the strategy, keys
or source); the plan code takes the plan default `CONFIGURATION_REPAIR`; route codes keep `NEVER`;
`check_error` stays `UNKNOWN`. Files: `reasons/post_scan/_part2.py` (new), `reasons/plan/_part3.py`,
`reasons/route/_added.py`, `_retired.py`. Registry version: `since=UNRELEASED` if #506 has merged
when the slice builds, else the next free minor via `scripts/dev/registry_renumber.py` with its
manifest fixture. Code-adding slices: PS-2a, PS-2b, PS-3, PS-8, PS-12.

## 6. Choke points, guards and red mutations

| Rule | Choke point | Guard (walks the real artifact) | Mutation that turns it red |
|---|---|---|---|
| No commit without the gate | `observed_commit` | AST sentry over `execution/` and `generation/`: every sink `.commit()` sits inside `observed_commit`; `OutputPublish.commit` is the one wrapper; `atomic_commit` only in `_isolated_run.py` `_commit` and `_run_in_process` (named, until F22 c2 deletes it); aliases and `getattr` resolved | call `sink.commit()` directly in `_sequential.py`; add an `atomic_commit` call elsewhere |
| Gate ran on every route | gate binding plus post-condition | route-coverage sentry (AT-17), keyed on the imported `ROUTES` plus `stream` and `isolated` | drop the `mixed` entry |
| One key core | `profiling.key_facts` | import sentry: no module under `verification/` or `execution/pipeline/_post_scan.py` imports `duckdb` or `connect_duckdb` | add `import duckdb` to `_keys.py` |
| One model predicate | `config._post_scan.post_scan_model_setting` | AST sentry: no other module reads `ner_findings` or compares `detectors` with `"regex"` | inline the test in `_route_guard.py` |
| One blocking decision | `verification.postmask._blocked.post_scan_verdict` | AST sentry: outside `_blocked.py`, nothing under `src/decoy_engine/` constructs `PostScanBlockedError` or compares a post-scan `severity` with `"fail"` or `"error"` | raise `PostScanBlockedError` from `_keys.py` |
| Value-free results | facts types hold ints, names and `ReasonRef` only | AT-14 canary | add a key value to a finding |
| No whole-table read in the gate | `TableBatches` | AT-15 spies | read a staged file with `pq.read_table` |

Each sentry is shown red on its seeded mutation in the slice record. The blocking-decision sentry lands in
PS-2b.

## 7. Acceptance tests

Red first: each slice's first commit adds its tests, red on the base.

| AT | Slice | Input and expected result | Red mutation |
|---|---|---|---|
| AT-1 | PS-1 | Parquet and CSV files with nulls, duplicates and orphans: `key_facts` counts equal pre-scan `relationship_facts` on the same files | `_complete` joins with `OR` |
| AT-2 | PS-1 | One table as `pa.Table`, Parquet `LazySource`, CSV `LazySource`: `from_*` reads give identical concatenated tables and row counts, with and without `columns` | drop the last partial batch |
| AT-3 | PS-1 | Existing pre-scan scan-integrity tests pass unchanged; `children=()` gives parent facts; a mismatch returns `key_type_mismatch` and opens no connection | swap null-key rows and duplicate excess |
| AT-4 | PS-2a | Masking collapses two parent keys: `primary_key_duplicates_introduced`; source duplicates alone: no block | compare output with 0 |
| AT-5 | PS-2a | A nulled PK part blocks; composite `(a, b)` with `b` null counts as null | check `k0` only |
| AT-6 | PS-2a | Child keys remapped outside the parent block; source orphans under `preserve`, `warn`, `remap` do not | drop the baseline |
| AT-7 | PS-2a | Child `(1, 2)` against parents `(1, 3)` and `(4, 2)` is an orphan | join on `k0` only |
| AT-8 | PS-2a | A child with any null part: not an orphan, counted, no block | remove the completeness filter |
| AT-9 | PS-2a | `employees.manager_id -> employees.id`: both ends read, two `batches()` calls | reuse one read |
| AT-10 | PS-2a | Two relationships over one parent: PK checked once; verdict independent of config order | stop after the first edge |
| AT-11 | PS-2a | Missing output key column: `key_column_missing`; `int64` parent with `string` child: `key_type_mismatch` | return `checked` |
| AT-12 | PS-2a | DuckDB failure at `_before_fetch`: `check_error`, message is the type name, `__cause__` and `__context__` are `None` | raise inside `except` |
| AT-13 | PS-2b | Breaches of several kinds plus an NER fail stub: reason is the first code in 4.8 order; `verdict.codes` has all, in order | sort codes alphabetically |
| AT-32 | PS-2b | `post_scan_verdict` on reports: one `residual_pii` `fail` blocks with `finding_blocked`; only `warning` and `info` findings do not block; an `error` finding or a set `pass_failed_with` blocks with `check_error`; an FK `fail` with its orphan code gives one code | block on key codes only |
| AT-14 | PS-2a, PS-6, PS-8 | Canary `CANARY-7f3a` duplicated, orphaned, in a column hit and in a forced DuckDB error: absent from the report, `str(exc)`, `ReasonRecord`, logs and the `quality_metrics` dump | add a value to a finding |
| AT-23 | PS-3 | Generated parent `id` of type `faker` without `unique`: refused before any read with `plan.relationship_parent_not_unique` naming the fix; with `unique: true`, a `sequence` or `unique_together`: accepted, output bytes unchanged | accept a non-unique faker key |
| AT-21 | PS-4 | FK findings: introduced orphans `fail`, inherited only `warning`, none `info`, unchecked `error`; a composite fills the tuples; tag stays `post-scan-integrity/v2`; the three named fixtures change as listed | keep the 1% threshold |
| AT-19 | PS-5 | Column facts on tables of 10, 10,000 and 250,000 rows over batches of 7 and 100,000: identity counts, `bytes_identical`, distinct counts and every detector rate equal the resident pandas values exactly | average per-batch rates instead of summing hits |
| AT-22 | PS-6 | `run_post_mask` adapter equals the old runner on every existing fixture; a column near `min_rate` escalates exactly as on the resident path | read only the first batch |
| AT-31 | PS-7 | Unit: `run_resident` stamps `keys` and `verdict`; a second call is a no-op; `before_commit` on a required, unrun gate with a sink lacking `staged_tables` raises `check_error` | reset `ran` in `before_commit` |
| AT-16 | PS-8, PS-10 | A blocking verdict commits nothing on full-frame, sequential and out-of-core (from PS-10 also chunked and mixed): no output table, sink evidence aborted, temp files gone. At PS-8, sequential and out-of-core relationship jobs route resident with `post_scan_keys_resident` | run the gate after `sink.commit()` |
| AT-20 | PS-8, PS-10 | Gate runs once (full-frame no sink, full-frame streamed, out-of-core with sink); an active sink skips `run_resident`; at PS-8 a forced streamed lane refuses at commit with `check_error`; at PS-10 an introduced orphan on the full-frame streamed lane blocks | call `run_resident` unconditionally |
| AT-25 | PS-8 | A no-sink run whose gate never ran raises `check_error` after the route | delete the post-condition |
| AT-26 | PS-9 | Isolated child (subprocess and in-process fallback) with an introduced orphan: outcome `blocked`, reason `foreign_key_orphans_introduced`, report present, output dir absent; the governor returns terminal `blocked` | map the block to `crashed` |
| AT-15 | PS-10, PS-12 | One cell per route (sequential, chunked, out_of_core, mixed, unified_slice, stream, isolated; full_frame asserts no second copy): no `pq.read_table`, `ParquetFile.read`, `materialize_source` or whole-table `to_pandas` in the gate; Arrow peak under 4 times the batch bytes; DuckDB `memory_limit` at most 512MB; at an 8MB limit spill files appear | read staged output with `read_table` |
| AT-27 | PS-10 | A Parquet input rewritten between routing and the gate re-read refuses `lazy_source_changed`; CSV the same | skip `check_opened` |
| AT-28 | PS-10 | `_CallableSinkAdapter` with keys declared: run stays resident with `post_scan_keys_resident`, the gate runs and blocks an introduced orphan | treat an unreadable sink as zero rows |
| AT-29 | PS-10 | Declared-cast key column (`"01"` and `"1"` cast to `int64`) on a streamed route: baseline counts equal the resident route's | re-read without `cast_batch` |
| AT-30 | PS-11 | `source_loader` on the sequential route: the tee baseline equals the resident baseline; the loader is called once per table; a loader table without the key columns keeps the run resident | call the loader twice |
| AT-17 | PS-12 | Route-coverage sentry: `GATE_TESTS` keyed on every `ROUTES` entry plus `stream` and `isolated` names a test that proves the gate ran and stamped `keys` | delete an entry |
| AT-18 | PS-12 | `integrity: true` with regex detectors takes sequential and out_of_core when eligible; model asks stay resident with `post_scan_models_resident`; stream mode no longer refuses for regex; the unified slice runs the gate on its return | leave `_unified_slice_admission.py:256` on the old predicate |
| AT-24 | PS-8 or SUB-RUN E2, whichever merges second | `subset:` with an excluded edge leaving orphan children: no block; the same run with a key remapped outside the parent: block; the baseline is read from the `engine.scratch` sources | read the pre-subset sources |

## 8. Slices

Sizes are hand-written lines, source plus tests. "Today": buildable from main, no open decision.

| Slice | Content | Size | Tier | Waits on | Start |
|---|---|---|---|---|---|
| PS-1 | 4.1 interfaces: `KeyBatches`, `KeyFacts`, `ChildKeyFacts`, `key_facts`; `_check` on it; `open_batches(columns=)`; `_stream_input.py` (`from_table`, `from_lazy`, `missing_columns`, `key_batches`); registry entry rename; AT-1 to AT-3 | 150 + 230 | R1 | none | today |
| PS-2a | `verification/postmask/_keys.py`: declared keys, baseline and output facts, per-key codes, the `keys` report section; five key codes; AT-4 to AT-12, AT-14 keys part | 170 + 230 | R2 | PS-1 | after PS-1 |
| PS-2b | `_blocked.py`: `post_scan_verdict` (key codes plus finding severities, 4.8), the code order, the `verdict` section, `PostScanBlockedError` (M4 reuses both), `post_scan.finding_blocked`, blocking-decision sentry; AT-13, AT-32 | 90 + 170 | R2 | PS-2a | after PS-2a |
| PS-3 | Generated-parent uniqueness plan check (4.9), one plan code; AT-23 | 60 + 120 | R2 | none | today |
| PS-4 | FK finding from the keys facts (4.8), composite tuples, `run_post_mask(keys=)`, contract note, fixtures, pandas FK checker deleted; AT-21 | 90 + 200 (about 250 deleted) | R2 | PS-2a | after PS-2a |
| PS-5 | `_column_facts.py`, `profiling.column_distinct`, `detection/_scoring.py` `counting_scope`; AT-19 | 190 + 230 | R2 | PS-1 | after PS-1 |
| PS-6 | `residual_pii.py`, `policy_validation.py`, `runner.py` on facts; adapter; AT-22, AT-14 suite part | 150 + 220 | R2 | PS-4, PS-5; rebases with M4 (section 10) | after PS-5 |
| PS-7 | `execution/pipeline/_post_scan.py` gate core, not yet wired: `PostScanGate`, `run_resident` (keys via PS-2a, verdict via PS-2b, column suite via `run_post_mask`), `before_commit`, exactly-once, error stripping, memory limit; physical-seam entry; AT-31 | 200 + 230 | R2 | PS-2b, PS-4 | after PS-4 |
| PS-8 | Wiring: bind in `execute_route`, full-frame call (no active sink), `compute_post_scan_integrity` deleted, `observed_commit` hook, post-condition, `config/_post_scan.py` predicates, routing call sites, `route.post_scan_keys_resident`, M5 adoption, commit and import sentries; AT-16, AT-20, AT-25, AT-14 end to end | 110 + 300 | R2 | PS-7, PS-3 | after PS-7 |
| PS-9 | Isolated `blocked` outcome (4.5), governor terminal, `atomic_commit` sentry case; AT-26 | 60 + 180 | R2 | PS-8 | after PS-8 |
| PS-10 | `staged_tables()` on main's sink, `from_staged`, commit-time gate over staged files, `check_opened` and CSV re-reads, cast baseline, `output_readable` for readable sinks; AT-15 streamed cells, AT-16 and AT-20 streamed cells, AT-27 to AT-29 | 150 + 270 | R2 | PS-8 | after PS-8 |
| PS-11 | Loader tee (key and cast columns to scratch Parquet in the route's own read, read back with `from_lazy`); loader runs leave the resident route; AT-30 | 70 + 150 | R2 | PS-10 | after PS-10 |
| PS-12 | Lift: model predicate in routing, keyword rename, route code retire and add, stream refusal lifted, unified-slice gate call; AT-15 remaining cells, AT-17, AT-18; docs (`docs/relationships.md`, `docs/reasons.md`, CODEMAP rows) | 120 + 280 | R2 | PS-6, PS-9, PS-11 | after those |

Total about 4,300 lines (about 1,580 source). PS-1 and PS-3 can start today in parallel; PS-5 after
PS-1, beside PS-2a.

Merge train and release hold (F1): from PS-8 until PS-10 and PS-11, relationship jobs stay
resident, so one above `full_frame_reject_rows` is refused (`FK_FULL_FRAME_OOM_RISK_REJECTED`)
instead of running sequential or out of core. PS-8, PS-10 and PS-11 merge on one train (stacked
PRs, one full suite on the top) and no engine release is cut between them. Every slice leaves main
fail-closed. After PS-8 full-frame and no-sink runs are gated; PS-10 staged sinks; PS-11 loader
runs stream again; PS-12 covers guarantee 1 fully.

Commits per slice: red tests, code, docs and record. Conventional Commits, at most 72 characters,
no body unless it explains why, final line `cam`.

## 9. Test scope and full-suite triggers

```text
Tiers:      T0 per edit; T1 impact-selected per commit and slice head; T2 on the exact head
            with the section 6 and 7 mutations
Named:      tests/unit/detection_verification/postscan/, tests/unit/execution/post_scan/,
            tests/sentry/post_scan/, pre-scan scan-integrity tests, reason registry tests
Triggers:   reason registry (PS-2a, PS-2b, PS-3, PS-8, PS-12); frozen field meaning (PS-4); routing
            (PS-8, PS-12); public execution types (PS-9). Full suite once per trigger head,
            diffed against the saved main baseline (decoy-vm-t3); Mac first, else vm-run.
```

## 10. Design notes and seams

- One choke point for publication (`observed_commit`), one key core (`key_facts`), one model
  predicate, one blocking decision (`post_scan_verdict`) and exception, one route list (`ROUTES`);
  typed inputs (`TableBatches`, the bound gate) over long parameter lists. A baseline, not a blind
  "keys must be unique" rule, because `preserve` and `warn` allow inherited breaches.
- SCAN-ML: its shared-file rules ("`residual_pii.py` by M4 only", "`config/_post_scan.py` by M1
  only") need amending for PS-6 and PS-8; the second to merge rebases.
- F22: c1b edits `_pipeline_full_frame.py`, `_publication_observer.py`, `_chunked_output_sink.py`
  as PS-8 and PS-10 do (mechanical conflicts); `test_route_outcome_matrix` and AT-17 name each
  other; after c2 `StagedParquetWriter` keeps `staged_tables()` and the `atomic_commit` case goes.
- DELIV (row 61): 4.10. The vault is safe on a block: the CLI commits it only after `run_pipeline`
  returns. SUB-RUN: both plans add one file to `execution/pipeline/` (5 to 7 modules).

## 11. Placement

New files (CODEMAP "Where things go"):

| File | Directory | CODEMAP row | Sits beside |
|---|---|---|---|
| `_stream_input.py`, `_keys.py`, `_column_facts.py`, `_blocked.py` | `src/decoy_engine/verification/postmask/` | Directory Map `verification/` row (`CODEMAP.md:106`) | `runner.py`, `residual_pii.py` |
| `_post_scan.py` | `src/decoy_engine/execution/pipeline/` | "Pipeline spine and routes" (`CODEMAP.md:238`) | `_stream.py`, `_route_summary.py` |
| `_relationship_keys.py` | `src/decoy_engine/plan/checks/` | "Plan compile and checks" (`CODEMAP.md:237`) | `_generate_columns.py` |
| `_part2.py` | `src/decoy_engine/reasons/post_scan/` | "Reason codes" | `_part1.py` |
| tests | `tests/unit/detection_verification/postscan/`, `tests/unit/execution/post_scan/` (parents hold 26 and 233 entries) | "Unit tests" | `tests/unit/execution/derived_visibility/` |
| sentries | `tests/sentry/post_scan/` (parent holds 80) | "Sentry test" (`CODEMAP.md:254`) | `tests/sentry/profiling/` |

Changed modules (layer, size now, after; no ceiling, pin or allowlist rises):

| Module | Layer | Now | After |
|---|---|---|---|
| `verification/postmask/_stream_input.py`, `_keys.py`, `_column_facts.py`, `_blocked.py` | verification | 0 | about 140, 220, 190, 90 |
| `execution/pipeline/_post_scan.py` | execution | 0 | about 300 (gate plus loader tee) |
| `plan/checks/_relationship_keys.py` | plan | 0 | about 60 |
| `profiling/_scan_integrity.py` | profiling | 368 | about 440 |
| `profile/_readers.py`, `profile/_lazy_csv.py`; `detection/_scoring.py` | profile; detection | 568, 246; 192 | about 575, 250; 220 |
| `verification/postmask/residual_pii.py`, `policy_validation.py`, `fk_preservation.py`, `runner.py`, `types.py` | verification | 398, 221, 337, 190, 148 | about 360, 200, 90, 210, 160 |
| `config/_post_scan.py`; `plan/checks/_generate_columns.py` | config; plan | 135; 515 | about 175; 517 |
| `execution/_pipeline_full_frame.py`, `_pipeline_finalize.py`, `_pipeline_route_dispatch.py`, `_publication_observer.py`, `_transactional_sink.py` | execution | 317, 503, 151, 158, 438 | about 330, 470, 170, 165, 460 |
| `execution/_chunked_output_sink.py` | execution | 598 | at most 600 (call swap only) |
| `execution/_sequential.py` | execution | 660 | at most 660 (ceiling 680; the tee lives in `_post_scan.py`) |
| `execution/_isolated_run.py` | execution | 659 | at most 667 (ceiling 672; SUB-RUN E2 lowers it) |
| `execution/_isolated_worker.py`, `_isolated_common.py`, `_isolated_reason.py` | execution | 271, 388, 52 | about 285, 389, 75 |
| `execution/_governor.py`, `_unified_slice_admission.py`, `_pipeline_routing.py` | execution | 588, 571, 486 | about 596, 571, 490 |
| `execution/_pipeline_routing_signals.py` | execution | 613 | at most 613 (census entry, ceiling 633; swap only) |
| `execution/_pipeline_context.py` | execution | 594 | at most 596 |
| `generation/_streamed.py` | generation | 594 | unchanged (exemption) |

`verification/postmask/` goes from 6 modules (7 with M5) to 11, under 25. `execution/pipeline/`
goes from 5 to 6 (7 with SUB-RUN). `execution/` top level stays at its freeze.

Registries:

- Physical-seam sentry: `execution/pipeline/_post_scan.py` joins `GUARDED_MODULES`
  (`tests/sentry/test_physical_seam_disconnection.py:64`) in PS-7.
- File-access registry (`tests/sentry/file_access_registry.toml`): the `_scan_integrity.py` entry
  at `:1354` moves from `_check` to `key_facts` (PS-1); new entries for `column_distinct` (PS-5),
  the gate's staged-file reads through `txn.open_file` (PS-10) and the loader tee's scratch Parquet
  writes and reads (PS-11), each with disposition, rule and owner `cam`.
- Reason codes: section 5 (retry class, owner module, file, registry version).

## 12. Recurring-defect coverage

Classes 1 to 10: the class table in section 16, each with its proof and mutation. Class 11: no
limit, pin or ceiling rises (`MEMORY_LIMIT`, `_BATCH_ROWS` unchanged).

## 13. Pre-mortem

- "Valid runs started failing." The baseline covers inherited breaches (AT-6); non-unique
  generated parents are refused at plan time with the fix named (AT-23).
- "Big FK jobs got refused." Only inside the PS-8 to PS-11 train, which no release cuts.
- "Runs got slower." Key checks read key columns only; exact distinct counts add one DuckDB pass
  per configured column. PS-10's record times the 10M laptop demo with and without the gate.
- "F22 moved the sink." The `staged_tables()` contract (4.6) is posted on the F22 PRs.
- "Report numbers moved." The column suite is exact (AT-19, AT-22); FK severities move on purpose
  (AT-21).
- "`integrity: true` runs stopped writing." A column-suite `fail` now blocks before commit (row 61
  ruling); today it only reports. The release note names it.

## 14. Recorded decisions

Taken as recommended under the standing rule; the owner may overturn.

- Q1: key checks run whenever relationships are declared, with or without `post_scan.integrity`.
- Q2: an introduced key breach, or any finding `post_scan_verdict` calls blocking, blocks before commit.
- Q3: no `tables[].primary_key` field in V1; keys are declared only in `relationships:` (V2-LOG).
- Q4 (row 61): a post-scan block withholds the whole run in strict and best-effort delivery; the
  gate takes no delivery mode.

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
  dispatch through other containers is not covered.

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
| 5 | Referential integrity | Yes | `key_facts` PK and MATCH SIMPLE FK over output, baseline over inputs | AT-4 to AT-11; mutation: join on `k0` only |
| 6 | Untrusted producer | Yes: staged files, foreign sinks, loaders | schema and counts from the file; a foreign sink is never read, the run stays resident | AT-28, AT-30; mutation: trust the route's row count |
| 7 | Check then use (TOCTOU) | Yes: inputs re-read after masking | `check_opened` against the routing snapshot; CSV identity check | AT-27; mutation: skip the check |
| 8 | Weak tests and overclaimed evidence | Yes: memory tests can pass on tiny data | AT-15 forces an 8MB DuckDB limit and asserts spill files; AT-19 uses multi-batch tables | mutation: a whole read turns AT-15 red |
| 9 | Behavior-preserving refactor drift | Yes: `_check` split, runner adapter | AT-1, AT-3, AT-22 against the old outputs | mutation in AT-1 |
| 10 | Frozen contract drift | Yes: report tag, `fk_preservation` meaning, registry | additive fields; one recorded semantic change; one retirement with `replaced_by`; manifest fixtures | AT-21; registry tests; mutation: bump the tag |

### Enumerations

#### Exit paths

- Pass: commit continues (`execution/_publication_observer.py:103`). Block: `PostScanBlockedError`,
  sink aborted (`execution/out_of_core/_runner.py:315`). Error: `post_scan.check_error`
  (`reasons/post_scan/_part1.py:15`).
- Gate cannot run at commit (`execution/_publication_observer.py:99`); no-sink gate never ran
  (`execution/_pipeline_route_dispatch.py:35`); source changed (`execution/_chunked_input.py:251`);
  isolated `blocked` (`execution/_isolated_worker.py:222`).

#### Routes

- The six `ROUTES` (`declared_casting/_eligibility.py:26`), stream mode
  (`execution/pipeline/_stream.py:187`), isolated child and fallback (`execution/_isolated_run.py:304`).
- Exempt: streamed generation entries (`generation/_plan_entry.py:254`); direct primitives.

#### FK shapes

- Single column; composite tuple (`profiling/_scan_integrity.py:300`); nullable FK; self-reference.
- Generated parent (4.9); subset with excluded edges (AT-24); cross-table cycle refused earlier.

#### Fields

- `keys[]`: `table`, `columns`, `kind`, `parent`, `bounded`, `codes`, `status`, and `source` and
  `output` counts (`rows`, `null_key_rows`, `duplicate_excess` or `orphan_rows`,
  `orphan_distinct_keys`).
- `verdict`: `blocking`, `codes` (4.8 order), from `post_scan_verdict`; `fk_preservation[]` adds
  `parent_columns`, `child_columns`, `source_orphan_count`.

#### Output surfaces

- `quality_metrics["post_scan_integrity"]` (`execution/_pipeline_finalize.py:503`);
  `PostScanBlockedError` (`reason`, `report`); `ReasonRecord`s and publication evidence
  (`execution/_publication_observer.py:110`).
- `IsolatedRunResult` and `GovernorResult` outcome `blocked`; RunResult
  `Check(id="post_scan_integrity")` (`run_result/_model.py:182`). No new log line carries data.

### Reuse inventory

`profiling/_scan_integrity.py` (`_check`, `_child_facts`, `_comparable`, `_labels`,
`_before_fetch`, `_open_keys`); `parse_relationship_config`; `out_of_core/_duckdb.py`
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
