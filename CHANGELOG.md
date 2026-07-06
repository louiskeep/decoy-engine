# Changelog

All notable changes to the `decoy-engine` PyPI distribution land here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Engine versions are independent of `decoy-cli`; the CLI declares the
minimum engine version it was tested against via its
`decoy-engine>=X.Y` dependency pin.

## [Unreleased]

### Changed (S3 engine-efficiencies x S2 routing reconciliation, 2026-07-06)

Landed `feat/engine-efficiencies` (P0-P5 below) onto the integration branch
alongside the already-merged S2 FK-sequential routing. The two routing
layers compose in a fixed order inside `run_pipeline`, now split into
`execution/_pipeline_routing.py` to hold the 600-LOC orchestration cap:
S2's relationship routing (sequential vs. full_frame) decides first; S3's
auto-chunk routing (chunked vs. full_frame) only applies on jobs that did
not take the sequential early return, and independently excludes any
relationship-bearing job via the planner's own FK-edge gate, so the two
layers never overlap. Two silent-knob-ignoring gaps were closed as part of
the reconciliation: (1) an explicit non-`"pandas"` `substrate` (or
`DECOY_SUBSTRATE=polars`) now disqualifies sequential eligibility,
falling through to full_frame so `select_execution_adapter` is what
actually honors the request (previously the sequential path silently
always ran pandas regardless of a caller's `substrate="polars"` ask); (2)
`fpe_chunk_count` is now threaded into the sequential route's
`PandasExecutionAdapter` (previously silently reverted to its class
default). `explain_plan=True` also now surfaces a classification on the
sequential route (previously it silently stamped nothing for exactly the
relationship-route-deferred jobs where that surface is most informative).

### Changed (S2 FK-sequential default routing in run_pipeline, 2026-07-05)

`run_pipeline` now routes relationship-bearing pure-mask jobs (those with
`relationships`, no `generate_columns`, no validators/fidelity_report/vault_writer)
through the bounded-memory `run_sequential` path by default. Non-FK and mixed
generate+mask jobs take the full-frame path unchanged. Output is byte-identical;
the only difference is peak memory.

- **Default routing: `execution_mode="auto"`** (new kwarg in `run_pipeline`).
  Eligible pure-mask FK jobs route to `run_sequential` + `ParquetTransactionalSink`;
  all others route to full-frame. Disqualifiers are captured in telemetry as a
  stable `route_reason` token: `no_relationships`, `generate_plus_mask`,
  `validators_present`, `fidelity_report_requested`, `vault_writer_requested`.
  When `validators` or `fidelity_report` are present, the job takes full-frame and
  skips `run_sequential` entirely, since the sequential path cannot satisfy the
  post-mask compute requirements (all outputs must be resident at once). This
  exclusion also keeps the sequential path's row-error enforcement (Part 2 below)
  as the sole owner, with no double-processing.

- **Explicit overrides.** `execution_mode="full_frame"` forces full-frame regardless
  of eligibility. `execution_mode="sequential"` forces sequential and raises
  `ConfigError` (with the `route_reason` as context) if the job is not eligible,
  or if the FK table graph has a mutual cross-table cycle that cannot be ordered
  (see Fixed section below).

- **Transparent parameters.** `sink` and `source_loader` kwargs are passed through
  to `run_sequential` when the sequential route is taken. The default (neither
  supplied) routes to sequential-in-memory with byte-identical output; no existing
  caller breaks. The empty-outputs streamed path is only reachable when a `sink`
  is explicitly supplied.

- **Honest memory telemetry.** A new `quality_metrics["execution"]` dict records:
  `execution_mode` ("sequential" or "full_frame"), `route_reason` (the disqualifier
  or override used), `eviction` (per-table vs none), `outputs_streamed` (true only
  when a sink was provided), and `loaded_fully_in_memory` (false only when a lazy
  `source_loader` was supplied and no resident `sources` dict was provided).

### Added (S2 row-error draining on run_sequential, 2026-07-05)

`run_sequential` closes the S1 MEDIUM-2 precondition: it now drains strategy
errors (`format_error`, `mask_error`) from each masked table and
enforces the same fail-loud/quarantine rule as `run_pipeline` on the full-frame
path. This guarantees that the bounded-memory path (which is now the default for
eligible FK jobs, see Changed section above) delivers the same safety as full-frame.

- **Per-node drain, per-table fail-loud.** Row errors are drained and folded into
  a per-table index immediately after each node dispatch (inside the table's
  mask-node loop). This is critical for self-referential FK handling (see Fixed
  section below): a child node that references the same table must see the parent
  node's errors before it resolves the FK, which happens in the very next loop
  iteration. The fail-loud classification stays per-table (after all nodes of that
  table have dispatched), so any uncovered error raises `RowErrorsFailedError`
  before the table is written to the sink or evicted. A failing table therefore
  never publishes a leaked value; the exception propagates to `abort()` if using
  a transactional sink.

- **Quarantine-aware FK resolution (EXCLUDE-then-CASCADE).** A parent-key row that
  produced a `format_error` or `mask_error` must never leak its raw key value
  through a child FK, even though FK resolution reads from the full pre-filter
  parent frame. Row-errored parent-key rows are excluded from the parent key-map
  via a per-table error index (`key_error_rows`), and child rows that reference an
  excluded key are automatically cascade-quarantined (marked with a synthetic
  `RowError` on their own table's subsequent dispatch). Cascaded child errors
  drain and classify on the child table's own per-table drain, so they follow the
  same fail-loud/quarantine rule without separate code. This holds across
  self-referential, multi-hop, and composite-key chains.

- **Single quarantine JSONL write.** Unlike full-frame (which can write the JSONL
  incrementally), the sequential path writes one JSONL after every table has
  dispatched, avoiding truncation (`"w"` mode) and clobbering earlier tables'
  entries. The quarantine JSONL is durable only on a successful run (no uncovered
  errors); a fail-loud run publishes nothing.

- **Optional quarantine routing via `quarantine_config` kwarg** (default None).
  When `run_sequential(..., quarantine_config=...)` is supplied, covered rows
  (those whose `trigger` matches an enabled quarantine trigger) are filtered
  from that table's Arrow output before write and written to the quarantine JSONL.
  Uncovered rows raise `RowErrorsFailedError` before the write.

- **`ExecutionResult.row_errors` and `quality_metrics["row_errors"]`** now carry
  drained records on the sequential path (same surface as full-frame). No row-error
  records are silently dropped.

### Fixed (S2 round-3 FK-topology leak remediation, 2026-07-05)

- **Self-referential FK raw-key leak on the sequential path (BLOCKER).** A
  table that is its own FK parent (e.g. `employees.id` referenced by
  `employees.manager_id`) leaked the raw errored key into the child column on
  the default `execution_mode="auto"` sequential path, because
  `run_sequential` drained and folded row errors into the key-error index
  once PER TABLE, after the whole table's mask-node loop, so an intra-table
  FK-child node resolved before its own parent-key node's error was folded.
  Fixed by moving the drain + fold to PER NODE, inside the loop, mirroring
  full-frame `run()`. Full-frame and sequential now both close the
  self-referential case (the table empties: the failing parent row and its
  cascaded referrer are both quarantined) and are row-equivalent.
- **Cross-table FK cycle routing regression (functional, non-leak).** A
  mutual cross-table FK cycle (A references B, B references A) ran under
  full-frame before this program but, under the S2 `auto` router, was routed
  to `run_sequential`, which cannot order a cross-table cycle and raised
  `relationship_cycle`. Added `_has_cross_table_fk_cycle`; `auto` now falls
  back to full-frame for a cyclic table graph (`route_reason =
  "cross_table_cycle"`), restoring pre-S2 behavior. An explicit
  `execution_mode="sequential"` request on a cyclic graph now raises a clear
  `ConfigError` instead of the raw `relationship_cycle` error. A self-ref
  table (one table, not a cross-table cycle) is unaffected and still routes
  to `sequential` under `auto`.

**Accepted limitation (when-gated duplicate parent key), documented not
enforced.** When a `when` gate leaves a parent FK-key row unmasked AND that
same raw key value also appears on a different parent row that row-errored, a
child referencing that key value resolves (via the identity-map contract,
FK-resolution precedence 1) to the raw value carried by the when-gate-unmasked
parent row. This is NOT a quarantine escape: the raw value is present in the
child ONLY because the user's `when` gate deliberately left that duplicate
parent row unmasked, so it is ALREADY present in the parent output. Net-new
exposure is NIL. Enforcing "cascade even on a when-gated duplicate" would
break referential integrity: the child would point to null/quarantine while
the parent row survives with the raw key, producing a dangling reference for
a row the user intentionally chose to leave unmasked. The identity-map
contract (an unmasked parent key maps to itself, and children mirror it) is
the correct behavior; this case is documented and pinned by test, not
enforced. See `docs/relationships-memory-scaling.md` and
`docs/backlog/s2-fk-leak-remediation-r3-guide.md` section 5.

### Added (FK-RI transactional sink, 2026-06-30)

- **`TransactionalSink` protocol and `ParquetTransactionalSink` reference
  implementation** (`src/decoy_engine/execution/_transactional_sink.py`).
  Gives `run_sequential` (Option 2 FK-RI memory-scaling) an all-or-nothing
  output guarantee. Both are now exported from `decoy_engine.execution`.

  Three-method protocol (`write` / `commit` / `abort`):

  - `write(table, data)`: stages one masked Arrow table, called in
    FK-topological order by `run_sequential`.
  - `commit()`: publishes all staged tables; called once on a successful run.
  - `abort()`: discards all staged tables; called on any exception during the
    run. Must be best-effort and must not raise, so the original run exception
    always propagates.

  `ParquetTransactionalSink` file-based implementation:

  - Each `write()` call serializes the masked table as a Parquet file into a
    private staging directory (prefix `_decoy_stage_`) that is a sibling of
    the target directory, guaranteeing they share a filesystem.
  - `commit()` publishes the entire staging directory via a single
    `os.replace` call (POSIX rename(2)): either every Parquet file appears at
    the target path at once or nothing is published. This is a
    visibility-atomicity guarantee per POSIX.1-2008, not an fsync durability
    guarantee.
  - If `commit()` is called with no prior `write()` calls (nothing was staged),
    an empty target directory is created directly without a rename.
  - If the target directory already exists and is non-empty, `os.replace`
    raises `OSError` and commit fails closed; nothing is published and the
    staging directory remains intact for a subsequent `abort()`.
  - `abort()` removes the staging directory before any data reaches the target.
    If cleanup fails, the error is suppressed so the original run exception
    propagates unmasked.

  Back-compat: a plain `Callable[[str, pa.Table], None]` passed as the
  `run_sequential` sink is wrapped transparently by `_CallableSinkAdapter`,
  which preserves the pre-existing non-transactional contract (partial output
  on abort is documented and pinned by test).

  **Platform wiring is deferred.** `run_sequential` with a
  `ParquetTransactionalSink` is the safe path for job-runner use, but
  automatically routing production FK jobs through `run_sequential` is a
  platform job-runner change and is not part of this commit.

  Design doc: `docs/relationships-memory-scaling.md` sections 2 and 6.1.
### Fixed (Sprint 13 / coercion-13 S3, 2026-07-03, engine version 0.2.0)

Fail-closed fix for a live PII leak plus its same-class siblings (PO
GATE-1 Q4, coercion-13 sprint 13, root-caused by the Sprint F capability
registry's execution-verified audit).

- **`truncate` no longer silently passes the source value through on a bad
  config** (finding 0.4, CONFIRMED live on `main`). `TruncateHandler.run`
  (`execution/_strategies/_truncate.py`) and its polars twin
  (`execution/polars/_strategies/_truncate.py`) had three silent
  passthrough exits: a non-int/sub-1 `length`, an unrecognized `keep`,
  and an invalid `mask_char`. Each now raises `StrategyError` (codes
  `truncate_length_invalid`, `truncate_keep_invalid`,
  `truncate_mask_char_invalid`). A new plan-compile check,
  `check_truncate_config` (`plan/_checks_truncate.py`, compile-check
  ownership table row 22), rejects the same shapes before a run starts,
  and runs in `run_config_only_checks` too. BEHAVIOR CHANGE: a truncate
  column with an invalid `length`/`keep`/`mask_char` now fails the
  pipeline instead of emitting the unmasked source value. The valid
  config path is byte-identical (nulls preserved, head/tail, mask_char).
- **`bucketize` no longer silently passes the source column through on an
  unresolvable width** (GATE-1 Q4 sibling). `_resolve_width` returning
  `None` (an unknown/unresolved `preset`, or a missing/non-numeric/
  non-positive `width`) now makes `BucketizeStrategyHandler.run` raise
  `StrategyError(code="bucketize_width_unresolvable")` instead of
  passing the column through. New compile check
  `check_bucketize_config` (`plan/_checks_bucketize.py`, row 23).
- **`categorical` (mask) no longer silently corrupts output when
  `categories` is a string** (GATE-1 Q4 sibling). `list(cfg["categories"])`
  iterated the characters of a plain-string `categories` value, replacing
  the column with resampled single characters instead of the intended
  category set. Both `CategoricalStrategyHandler.run`
  (`execution/_strategies/_categorical.py`) and its polars twin now raise
  `StrategyError(code="categorical_categories_not_list")`. New compile
  check `check_categorical_categories` (`plan/_checks_categorical.py`,
  row 24; also rejects missing/empty categories as
  `categorical_categories_missing`). `from_profile: true` columns are
  exempt (the authoring layer resolves categories before the engine sees
  the config).
- **`joint_mask` investigated and found already safe**: the guide's
  concern that `tuple(cfg["columns"])` iterates characters when
  `columns` is a string does not reach a silent leak in practice --
  `validate_joint_mask_config` already checks each element against the
  reference table's real (multi-character) column names, so a string
  value fails loud (`joint_mask_column_not_in_reference`) on its first
  character before any row is masked. No new check added; a regression
  test locks the existing behavior in place
  (`tests/unit/transforms/test_joint_mask.py::test_string_columns_already_rejected`).
- **Engine version bumped 0.1.0 -> 0.2.0** so `decoy-platform` can pin a
  floor and guarantee `truncate` is only surfaced against a fail-closed
  engine (GATE-1 Q1).

### Added / Changed (Sprint 2 engine honesty pack, 2026-07-04)

Engine bumped to 0.3.0. Closes several silent-leak paths the validator and
quarantine framework (SP-05) did not yet cover, and adds five new post-mask
validators. Established methodology cited throughout: Delphix/Informatica
masking-verification pre/post comparisons, Great Expectations column
assertions, dbt relationship/aggregation tests, and the Spark
`badRecordsPath` / pandas `on_bad_lines` side-channel bad-row pattern.

- **`leak_check` validator**: compares masked output values against their
  source values per column and flags residual source values above a
  threshold. Fail-closed (no warn tier): a column tier catches a strategy
  that had NO effect at all (`identical_ratio == 1.0`); a cell tier (default
  `max_identical_ratio = 0.02`, TRANSFORMATIVE strategies only) catches
  partial per-row leaks and makes them quarantinable under the existing
  `validation_fail` trigger. `passthrough`, FK-child, and `when:`-gated
  columns are excluded by construction; a `params.exempt` knob covers
  legitimate coincidences the operator wants to allow. Requires the pipeline's
  pre-mask source tables; a leak_check scoped at a table with no source
  raises rather than silently skipping.
- **Four sibling validators** (`p5-j-validators-extended`): `regex_match`
  (whole-cell pattern match), `column_in_set` (allowed-value membership),
  `parent_window_respected` (child date within its parent's declared window;
  pairs with `windowed_date`), and `reconciliation_holds` (parent aggregate
  reconciles with child rows under an absolute tolerance; pairs with
  `derived_aggregate`). The validator registry grows from 6 to 11 entries.
- **`ValidatorEntry.params`**: a free-form per-validator config dict,
  additive alongside the existing `columns` field. Each validator validates
  its own `params` at run time.
- **Per-row strategy-error channel** (`execution/_row_errors.py`): a new
  `RowError` / `RowErrorRecord` side channel, threaded through
  `StrategyContext` and drained by both the pandas and polars execution
  adapters after every node dispatch. `ExecutionResult.row_errors` carries
  the table-attributed records; `quality_metrics["row_errors"]` persists
  per-table/column/trigger counts (no cell values) to the evidence manifest.
- **BEHAVIOR CHANGE (pre-GA hard cutover, no flag):** `bucketize` and
  `date_shift` columns with a non-null cell that fails numeric/date coercion
  used to silently keep the ORIGINAL source value in the masked output
  (discovery 0.1, the sibling of the #13 bucketize/truncate/categorical
  fix). They now record a `format_error` row error instead. A `code_set`
  `chapter_preserve` value that cannot be masked (input chapter absent from
  the corpus, or a sole-member chapter bucket) now records a `mask_error`
  row error instead of killing the whole job with no row attribution.
  **On the full-frame `run_pipeline` path the job now either fails loud by
  default (`RowErrorsFailedError`, naming counts by table/column/trigger, no
  cell values) or, when quarantine is enabled with the matching trigger, the
  offending rows are removed into the quarantine JSONL and the job succeeds.
  On the chunked/streaming path (`run_mask_pipeline_chunked`), which has no
  quarantine machinery, the job fails CLOSED: any chunk with a row error
  raises `RowErrorsFailedError`. Either way, the previous silent
  keep-the-source-value behavior is gone on these paths.**
  
  **Note: `run_sequential` (the bounded-memory FK path from PR #29) does NOT
  yet drain or surface row errors.** FK jobs routed through `run_sequential`
  silently pass through rows that would otherwise quarantine or fail. Closing
  this gap (wiring row-error draining into `run_sequential`) is a precondition
  of Sprint S2, which makes `run_sequential` the default path for eligible FK
  jobs.
- **Quarantine generalized to row errors** (`quarantine.apply_quarantine`):
  now accepts an optional `row_errors` tuple alongside the `ValidationReport`
  and builds one normalized worklist so a row that fails both a validator
  and a strategy row error is deduplicated exactly once. Existing
  `validation_fail`-only callers (the default `row_errors=()`) get
  byte-identical behavior. `quarantine.triggers` now accepts `format_error`
  and `mask_error` in addition to `validation_fail`.
- **`fpe` degenerate-charset compile check** (`check_fpe_charset_config`,
  `PlanCompileError` code `fpe_charset_degenerate`): a resolved fpe charset
  with fewer than 2 distinct characters used to silently pass the whole
  column through unmasked (the last known #13-class whole-column
  passthrough, discovery 0.1). Rejected at compile time; `FpeStrategyHandler`
  also raises `StrategyError` on the same shape as an execution-time
  backstop.
- **Public faker-provider accessor** (follow-up #11):
  `decoy_engine.list_generate_faker_providers(locale=None) -> tuple[str, ...]`,
  the sorted, flat, authoritative list of generate-kind Faker provider names
  (reflection + the existing denylist + registered custom providers). Closes
  the acceptance gap behind the platform's hand-maintained `GEN_FAKER`
  catalog; platform/web consumption is separate follow-up work in the
  platform lane.
- `validate()` (the validator-framework entry point) gained an additive,
  keyword-only `sources` parameter carrying the pipeline's pre-mask source
  tables, threaded from `run_pipeline`'s `caller_sources`. All pre-existing
  validators accept and ignore it.

### Added (Sprint G FK-aware subsetting core, 2026-07-03)

- **FK-aware row subsetting** (`src/decoy_engine/subset/`, 11 modules; Sprint
  G SS1-SS5). Pulls a referentially-intact slice of a multi-table Parquet
  dataset: select a seed (deterministic sample, filter predicate, or explicit
  key list) from one or more root tables, then close the seed over the
  declared `relationships` graph so every surviving child row has a
  surviving parent, and every parent a surviving child needs is pulled in
  too ("2% of `customers`, plus every `order`/`order_item` that belongs to
  them, nothing orphaned"). Narrow public surface: `run_subset_preflight`,
  `plan_subset` (dry-run/estimate), `run_subset` (materialize), plus the
  config adapters `relationships_from_config` / `subset_inputs_from_config`
  and the frozen dataclass types, all under `decoy_engine.subset`. Not
  re-exported from the top-level `decoy_engine` package this sprint; SS6's
  CLI will import the subpackage directly.

  - **SS1 preflight** (`_preflight.py`): fail-closed FK-validity pre-check
    over the declared `relationships` and the actual Parquet schemas/key
    columns, before any row is selected. Reuses
    `validation/post/_checks/_fk_validity.py`'s per-edge classification
    semantics (null keys neither match nor orphan; a non-null child key
    absent from the parent key set is a source orphan) via a schema/key-only
    anti-join adapter, since `run_fk_validity` itself needs a compiled `Plan`
    and masked outputs that do not exist yet at this stage. Fails on:
    non-Parquet source ("convert to Parquet for subsetting"), column-type
    mismatch (a float FK key is rejected outright), a half-declared
    composite key, and a dangling/reserved target column.
  - **SS2 seed selection** (`_seed.py`): `sample` (deterministic bottom-k
    HMAC-digest selection over the job seed, chosen over
    `pl.DataFrame.sample(seed=...)` for cross-polars-version stability),
    `filter` (a structured, AND-ed predicate; no string-eval surface), or
    `keys` (an explicit key list), over one or more root tables.
  - **SS3 closure engine** (`_closure.py`), the novel core of the sprint: a
    downward (cascade) + upward (parent-completeness) fixpoint walk over the
    declared FK edges, run to a monotone no-growth exit. Pattern: semi-naive
    Datalog fixpoint evaluation / Kleene-Knaster-Tarski monotone fixpoint
    over a finite powerset lattice (graph reachability closure); termination
    follows from monotonicity, not from the schema being acyclic, so a
    self-reference or a mutual cycle terminates the same way an acyclic
    schema does. An independent `verify_closure` no-orphan re-check runs
    after the fixpoint in both `plan_subset` (dry-run) and `run_subset`.
  - **SS4 fan-out policy + dry-run** (`_policy.py`): per-edge traversal
    direction (`both`/`downward`/`upward`/`none`; disabling upward traversal
    on an edge requires an explicit `allow_dangling=True`, since it can
    orphan a child FK); a fan-out budget that is both a total-output-row cap
    AND a per-table cap expressed as a multiple of the global seed row
    total. A budget breach hard-fails BEFORE any materialization and never
    truncates, since truncating a surviving set would re-introduce the exact
    orphans the feature exists to prevent. `plan_subset` is a first-class
    dry-run: it returns the exact projected per-table row counts with no
    write and no read of any non-key column.
  - **SS5 materialization + manifest** (`_materialize.py`, `_manifest.py`):
    semi-joins each source Parquet to its surviving row-index set and writes
    one filtered Parquet per table (row-index filtering, not a re-derived
    key join, so the dry-run estimate and the materialized counts are equal
    by construction). Writes a `subset-manifest.json` evidence artifact
    (counts, edges traversed and their direction, budget outcome, preflight
    summary) with NO raw key values and NO raw filter-predicate literals: a
    `filter`-mode seed keeps its predicate's `column` and `op` in the
    manifest, but the literal value is replaced with a `value_redacted`
    boolean (dennis review, MEDIUM-1: the first cut of `_seed_spec_public`
    wrote the raw predicate value, e.g. a `filter email ==
    "victim@example.com"` seed, straight into the shareable manifest).
  - **`subset:` on `PipelineConfig`** (`config/_subset.py`): additive,
    optional (`None` by default; unset means unchanged full-source masking
    behavior). Enforces subset-then-mask ordering structurally: there is no
    config syntax for the reverse order, and pointing a subset job's sources
    at the same config's mask targets is a validation-time error.

  **Scope for this build:** Parquet input only (a non-Parquet source is a
  validation-time and preflight-time reject, not a degraded full-load
  fallback); file/batch sources only (DB sources are deferred, not
  designed-for); manual `relationships` declaration only (no automatic
  FK/schema inference for subsetting); polymorphic FKs are unsupported (no
  clean `PlanRelationship` representation for a column whose parent table
  varies row to row). Built against the current full-frame FK execution
  path; `_materialize.py` marks where the `_sequential.py` per-table
  eviction plug-in point (once `feat/fk-ri-memory-scaling` merges) would
  compose in, but that path is not built here. A `decoy subset` CLI (SS6)
  and a platform UI (SS7) are follow-ons in other repos, not shipped in this
  engine sprint.

### Added (engine-efficiencies P0-P5, 2026-07-01)

Cross-cutting job-performance work on `feat/engine-efficiencies`
(`docs/job-performance-sprints.md`). Scope is `run_pipeline` mask-kind
execution only; the FK memory-scaling and out-of-core stacks live on their
own branches and are not part of this batch.

- **`run_pipeline` adapter-selection routing** (P1/P4,
  `src/decoy_engine/execution/_pipeline.py`,
  `src/decoy_engine/execution/_substrate.py`). Mask-kind work now routes
  through `select_execution_adapter()` at the public entrypoint via four new
  keyword-only knobs: `substrate` (default `"pandas"`; `"polars"` opts a
  scalar no-FK job into the Polars-native route, FK/composite work still
  falls back to the pandas oracle; `None` defers to the `DECOY_SUBSTRATE`
  env var), `fpe_chunk_count` (default 4), `max_workers` (default 4,
  polars-adapter only), and `fallback_to_pandas` (default `True`,
  polars-adapter only). The default call with no knobs supplied is
  byte-identical to the pre-P1 hardcoded pandas path. All four knobs are
  validated fail-closed before any profiling or plan compilation: an unknown
  substrate raises `ExecutionError(code="invalid_substrate")`, a bad count or
  bool knob raises `code="invalid_execution_knob"`. `require_bool` and
  `require_positive_int` are now public in `execution/_substrate.py`. When
  any knob is non-default, the resolved adapter identity and every knob
  value are stamped under `quality_metrics["execution_adapter"]`; the
  all-default path stamps nothing, so golden and compat-corpus fixtures stay
  byte-identical.

- **Auto-chunk routing for eligible single-table mask jobs** (P3,
  `src/decoy_engine/execution/_pipeline.py`,
  `src/decoy_engine/execution/_chunked.py`). `run_pipeline` gained
  `auto_chunk` (default `True`), `chunk_size_rows` (default 50,000), and
  `auto_chunk_threshold_rows` (default 100,000). When `auto_chunk` is on and
  the job is a single mask table with only chunk-safe value-keyed scalar
  strategies, no FK edges, no generate tables, pandas substrate, and a
  source at or above the threshold, the mask stage streams through
  `run_mask_pipeline_chunked` in `chunk_size_rows`-row slices instead of one
  full-frame adapter call. This is a memory win only, never a semantic
  change: output is byte-identical to the full-frame path, enforced by
  fail-closed eligibility (date_shift requires an explicit `date_format`,
  bucketize requires a null-free numeric source, `when`-bearing/composite/
  join-group-fpe/generate/relationship/non-pandas/below-threshold jobs all
  fall back to full-frame) plus a strict chunk concatenation that raises
  `code="chunked_schema_mismatch"` on any per-chunk schema disagreement
  instead of silently promoting it away. A routed run, or any run with a
  non-default auto-chunk knob, stamps `quality_metrics["auto_chunk"]`; the
  all-default non-routed path stamps nothing. `auto_chunk=False` is the kill
  switch back to the full-frame path.

- **Observe-only execution-mode planner** (P2,
  `src/decoy_engine/execution/_planner.py`: `classify_job`,
  `ExecutionPlan`). Classifies a job into one of `polars_native`, `chunked`,
  `sequential_relationship`, `out_of_core_relationship`, or
  `pandas_fallback`, recording why every faster mode was rejected (the
  `sequential_relationship`/`out_of_core_relationship` modes can only be
  detected as FK-edge candidates on this branch; the FK stack that would
  resolve them lives elsewhere). Surfaced via `run_pipeline(explain_plan=True)`
  into `quality_metrics["execution_plan"]`; default `False` stamps nothing.
  The planner does not route execution on its own; the one exception is the
  `chunked` mode, which `run_pipeline`'s `auto_chunk` knob routes as
  described above, using the same classification the explain surface
  reports so the two can never disagree.

- **Opt-in job-level performance gates** (P0,
  `tests/perf/test_job_performance_gates.py`). Benchmarks scalar, faker-
  heavy, FPE-heavy, and text-redaction-heavy jobs plus the P3 auto-chunk
  memory route at the `run_pipeline` public entrypoint, each timing
  assertion paired with a determinism (byte-identical output) and masking-
  sanity check. Test-only; gated behind `pytest.mark.benchmark`, which is
  excluded from the default test run (`addopts = "-m 'not benchmark and
  not testflight'"`).

- **Faker/provider pool parallel-readiness** (P5,
  `src/decoy_engine/providers_v2/_faker_adapter.py`,
  `src/decoy_engine/generation/synthesize.py`,
  `src/decoy_engine/generation/pool/_cache.py`). Removes the shared
  mutable Faker/RNG state and the lock that serialized it: seeded batch
  builds now construct a fresh `Faker` instance per call
  (`_faker_adapter.py`) instead of reseeding a shared one, and the
  no-locale default instance used by unseeded generation is cached
  thread-local rather than process-global (`synthesize.py`), removing the
  `_FAKER_CALL_LOCK` critical section. `PoolCache.get`/`put`/`stats`/`clear`
  are now internally locked so concurrent pool builds can share one cache
  without corrupting LRU order or the bytes-accounting total; the lock
  wraps only the cache bookkeeping, never a pool build. Output is unchanged:
  a fresh Faker instance seeded the same way produces the same sequence as
  a reseeded shared one, so existing faker output snapshots are unaffected.

### Added (SP-10 derived strategy, 2026-06-28)

- **`derived` mask and generate strategy** (`src/decoy_engine/transforms/derived.py`,
  `src/decoy_engine/execution/_strategies/_derived.py`, SP-10 / P5.S.derived).
  Registered in `SCALAR_HANDLERS`; technique class `pseudonymisation`;
  `distribution_behavior: mixed`. Also wired as a generate type in
  `generation/synthesize.py` (`_derived_generate`). Computes a column value
  from other columns in the same row via the SP-06 Lark closed-grammar
  evaluator. Deterministic by construction: same row context, same output, no
  RNG involved. Works in mask mode (`strategy: derived`, params under
  `provider_config:`) and generate mode (`type: derived`, params at the top
  level of the column config).

  Config surface:

  - `expression` (str, required): a closed-grammar expression. Column references
    are bare identifiers. Permitted forms: arithmetic (`+`, `-`, `*`, `/`, `//`),
    comparison (`==`, `!=`, `<`, `>`, `<=`, `>=`, `in`), logical (`and`, `or`,
    `not`), string (`concat(a, b)`), date (`days_between(start, end)`), ternary
    (`value if condition else other`), and literals. Anything outside the grammar
    raises `ValidationError` at config-parse time.
  - `bounds` (dict, optional): `{min: float, max: float}`. Clips numeric output
    after evaluation; non-numeric results pass through unchanged.
    `min > max` raises `PlanCompileError(derived_bounds_inverted)` at
    config-parse.
  - `null_propagation` (str, default `explicit_null`): `explicit_null` outputs
    `None` when any referenced column is `None`/`NaN`; `sentinel` replaces
    `None`/`NaN` with `""` before evaluation; `default` replaces with `0`.

  Validation timing:

  - Expression syntax: `compile_expr` at config-parse time (closed grammar;
    `ValidationError` before any row data is touched).
  - Column-ref existence: `check_derived_column_refs` in `plan/_checks.py` at
    plan-compile time. Raises `PlanCompileError(derived_missing_column_ref)`.
  - Cyclic references (direct and transitive, via DFS): same check, same
    timing. Raises `PlanCompileError(derived_cyclic_ref)`.

  Row-level evaluation errors (for example `ZeroDivisionError`, `TypeError`)
  fail the job with a diagnosable message naming the column and row index.
  No row is silently skipped.

  **Security.** The SP-06 Lark grammar is the sole security boundary: no
  `eval()`, `exec()`, or `__import__` on this path. A column value that looks
  like an expression string is treated as data and is never re-evaluated.

  **Carry-forwards (SP-10b, not yet built):** `case_when`, `derived_aggregate`,
  `grouped_series`, `windowed_date`; FK extensions (`cardinality`,
  `composite_depth`, `null_m2m`); layer-2/3 features (`conditioned_on`,
  `group_key`, `reconciliation_pass`). Forward-reference detection at
  plan-compile time (a derived column in generate mode that references a
  sibling declared later currently fails at evaluation time, not plan-compile).

### Added (SP-11 HIPAA pack tightening, 2026-06-28)

- **HIPAA Safe Harbor pack tightened posture** (`src/decoy_engine/disguises/hipaa.yaml`,
  SP-11). Three previously-weak field rules replaced with strategies that meet the
  45 CFR 164.514(b)(2) Safe Harbor standard by construction:

  - **NPI (National Provider Identifier):** `fpe` strategy now carries
    `checksum: npi`. Masked NPIs are valid by construction per the CMS NPPES
    check-digit spec (Luhn applied to prefix 80840 + 9-digit body). Masked
    values pass downstream NPI validators; deterministic so provider-level FK
    joins survive masking.

  - **ICD-10-CM diagnosis codes:** `code_set: icd10` with `chapter_preserve: true`
    replaces the prior `truncate(3)` strategy. Output is always a real CMS FY2024
    ICD-10-CM code in the same chapter as the input. A cardiovascular I-code masks
    to another I-code; a respiratory J-code stays in J. The HMAC-keyed selection
    is deterministic so provider-level analytics survive masking.

  - **ZIP / us_zip:** `geo_generalize` with `cascade: [zip5, zip3, state, suppress]`
    and `k_threshold: 20000` replaces the prior `truncate(3)` strategy. Implements
    the HIPAA Safe Harbor cascade (45 CFR 164.514(b)(2)(i)(B)): ZIP3 prefixes with
    population below 20,000 (HHS restricted list) cascade to state or suppress.
    Default `k_threshold: 20000` matches the Safe Harbor population floor.

  Pack `version` bumped to `2026-06-28`. CLI compliance templates that pin the pack
  version via a drift guard must be regenerated (SP-16/SP-19 lane).

  **Honest coverage limits (not auto-applied by this pack):**
  - Address columns use `faker: street_address` for standalone columns. For datasets
    where address, city, zip, and state appear together, use `joint_mask:
    us_zip5_city_state` in the recipe YAML; `joint_mask` requires a dataset-specific
    `key_by` column and cannot be set pack-wide.
  - Free-text columns (clinical notes, claim descriptions, etc.) have no registered
    STORM detector; configure `text_mask` manually in the recipe YAML.
  - HCPCS and NDC columns have no registered STORM detectors; configure
    `code_set: hcpcs` or `code_set: ndc` manually in the recipe YAML.
  - LOINC code_set corpus is not shipped (SP-09 carry-forward).
  - No strategy in this pack provides differential-privacy-grade guarantees (M11
    absent). `k-anonymity` (geo_generalize) and FPE are pseudonymisation techniques.

### Added (SP-09 code_set strategy, 2026-06-28)

- **`code_set` mask strategy** (`src/decoy_engine/transforms/code_set.py`,
  `src/decoy_engine/execution/_strategies/_code_set.py`, SP-09 /
  P5.S.code_set.1/2/3-corpus_source). Registered in `SCALAR_HANDLERS`;
  technique class `anonymisation`; `distribution_behavior: coarsens`. Replaces
  a code column value with a different code drawn from a named corpus (ICD-10,
  HCPCS, NDC, MCC, or a customer-supplied file). Output is always a real corpus
  code and always differs from the input.

  Config surface (`strategy: code_set` on a column; parameters under
  `provider_config:`):

  - `code_set` (str, required): corpus name. Shipped: `icd10`, `hcpcs`, `ndc`,
    `mcc`. Customer corpora: any name with `corpus_source: customer:<path>`.
  - `chapter_preserve` (bool, default `false`): restrict candidates to the same
    chapter bucket as the input. For ICD-10 the chapter is the first letter.
  - `corpus_source` (str, default `shipped`): `shipped` or
    `customer:<absolute_path>`. A customer corpus must have a `code` column
    (string) and, when `chapter_preserve: true`, a `chapter` column.

  Two modes:

  - **MASK mode** (default): `HMAC-SHA256(salt, input) % candidate_count` over
    the full corpus sorted ascending by `code` (RFC 2104). Candidate set
    excludes the input code, so output is never equal to input
    (domain-exclusion idiom, same primitive as `fpe` and `joint_mask`). Same
    input, same job_seed, same corpus version always produce the same output.
  - **GEN mode**: `derive_index` keyed on column namespace and row index
    (HKDF+HMAC, SEED_PROTOCOL_VERSION-covered). Two columns with different
    namespaces sharing the same job_seed produce decorrelated output sequences.
    Needs a namespace on the column.

  `chapter_preserve` fail-closed behavior (both raise `PlanCompileError`,
  execution-time, pre-mutation, before any data is changed):

  - Input's chapter absent from corpus (`code_set_chapter_absent`): no
    cross-chapter fallback; falling back would silently return a code from a
    different chapter.
  - Chapter bucket has only the input code (`code_set_sole_member_bucket`): no
    valid alternative exists; returning the input would violate output != input.

  Shipped corpora (in `src/decoy_engine/codesets/`):

  | Name | Rows | Source | License |
  |---|---|---|---|
  | `icd10` | 65 | CMS ICD-10-CM | US public domain |
  | `hcpcs` | 32 | CMS HCPCS | US public domain |
  | `ndc` | 38 | FDA NDC | US public domain |
  | `mcc` | 62 | ISO 18245 | See NOTICE |

  The `chapter` column in `ndc.parquet` is a Decoy-defined therapeutic bucket
  (A/B/C/D). NDC has no native chapter structure; this column is not a source
  attribute.

  **Cross-version keyed-access caveat.** MASK mode selects at position
  `HMAC(...) % candidate_count` over the code-sorted corpus. Deterministic
  within a corpus version. NOT stable if corpus row count changes (rows added
  or removed remap the modular index). Inherited from the SP-06 corpus-sort
  pattern.

  **Carry-forwards (not yet built):** additional shipped corpora LOINC, CIP,
  NUCC, UPC/EAN; CPT and MedDRA bring-your-own-corpus workflow documentation;
  an out-of-corpus-input `QualityWarning` signal (out-of-corpus inputs are
  currently silently remapped to a real code). HIPAA-pack wiring shipped in SP-11.

### Added (SP-08 joint_mask + geo_generalize, 2026-06-28)

- **`joint_mask` mask strategy** (`src/decoy_engine/transforms/joint_mask.py`,
  `src/decoy_engine/execution/_strategies/_joint_mask.py`, SP-08 /
  P5.S.joint_mask.1). Compound reference-tuple masking: replaces a set of
  logically coupled columns (for example `zip`, `city`, `state`) with a
  consistent tuple drawn from a reference table. Consistency holds because the
  output is a real reference-table row, never assembled field-by-field; no
  per-column replacement can produce a city/state pair that does not exist in
  the source data.

  Config surface (`strategy: joint_mask` on a column; parameters under
  `provider_config:`):

  - `columns` (list, required): target output column names. Every name must
    appear in the reference table (the `id` column is excluded).
  - `reference` (str, required): name of the shipped or customer-provided
    reference table. Ships with `us_zip5_city_state` and
    `vehicle_make_model_year`.
  - `key_by` (str, required): source column whose value drives HMAC-keyed row
    selection in mask mode. Not used in gen mode.
  - `mode` (str, default `mask`): `mask` (HMAC-keyed) or `gen` (seeded
    random).

  Two modes:

  - **MASK mode**: calls `ReferenceTable.keyed_row(str(key_by_value))`, which
    selects a row by reducing `HMAC-SHA256(job_seed, key_value)` modulo
    `row_count` over the `id`-sorted row order (RFC 2104). Same key value and
    seed always select the same row. Null `key_by` values fall back to a
    seeded random row.
  - **GEN mode**: draws row indices from `numpy.default_rng` seeded from the
    job seed, independently of source column values. Deterministic for the same
    seed and DataFrame length.

  Config validation runs at execution time, before any data is mutated
  (fail-closed). Raises `PlanCompileError` on missing `columns`, missing
  `key_by`, unknown `reference`, or a column name absent from the reference
  table. Error codes: `joint_mask_columns_missing`, `joint_mask_key_by_missing`,
  `joint_mask_reference_missing`, `joint_mask_reference_not_found`,
  `joint_mask_reference_invalid`, `joint_mask_column_not_in_reference`.

  **Cross-version keyed-access caveat (inherited from SP-06
  `ReferenceTable.keyed_row`).** `keyed_row` selects at position
  `HMAC(...) % row_count` in the `id`-sorted table. Deterministic within a
  single table version. NOT stable if `row_count` changes (rows added or
  removed): a given `key_by` value will select a different row after a
  row-count change. Do not assume cross-version key stability for `joint_mask`
  outputs.

- **`geo_generalize` mask strategy** (`src/decoy_engine/transforms/geo_generalize.py`,
  `src/decoy_engine/execution/_strategies/_geo_generalize.py`, SP-08 /
  P5.S.geo_generalize.1). HIPAA Safe Harbor geographic generalization for ZIP
  columns (45 CFR 164.514(b)(2)). For each row, attempts cascade levels in
  order until one satisfies the k-threshold; if no level does, the value is
  suppressed.

  Config surface (`strategy: geo_generalize`; parameters under
  `provider_config:`):

  - `type` (str, required): `zip` is the only supported type in SP-08.
    The lat/lng to H3 path requires the `h3` dependency and is deferred to
    SP-08b.
  - `cascade` (list, required): ordered list of generalization levels to
    attempt. Must include `suppress` as a terminator. Supported levels for
    `type: zip`: `zip5`, `zip3`, `state`, `suppress`.
  - `k_threshold` (int, default `20000`): minimum in-dataset record count for
    a generalization level to be retained. The default matches the HIPAA Safe
    Harbor population threshold for geographic units per
    45 CFR 164.514(b)(2)(i)(B).

  Cascade logic (applied to each row):

  - `zip5`: retain the full 5-digit ZIP when at least `k_threshold` records in
    the dataset share it.
  - `zip3`: generalize to the 3-digit prefix, but skip entirely for any prefix
    in the HHS-restricted list. The restricted list is the regulatory lever:
    it covers every geographic unit with population below 20,000 per the
    Census-based determination (45 CFR 164.514(b)(2)(i)(B)). The canonical 17
    restricted 3-digit prefixes ship as
    `reference_tables/data/us_zip3_population.parquet`, loaded via
    `load_table("us_zip3_population")`.
  - `state`: generalize to the 2-letter state abbreviation derived from the
    ZIP5 via the `us_zip5_city_state` reference table. Retained when at least
    `k_threshold` records in the dataset share the same state.
  - `suppress`: emit an empty string (`""`). Terminates the cascade.
    `suppress` must appear as the final level; omitting it raises
    `PlanCompileError(geo_generalize_missing_suppress)`.

  In-dataset counts (ZIP5, ZIP3, state) are computed once before the cascade
  loop. The restricted-ZIP3 check uses the shipped HHS list, not the
  in-dataset count: a restricted prefix is skipped regardless of how many
  records share it.

  Cascade decisions are recorded in a frozen `CascadeEvidence`
  dataclass (a `decisions` tuple, one label for each input row: `zip5`, `zip3`, `state`,
  or `suppressed`). Evidence is surfaced in `ExecutionResult.warnings` under
  `code="geo_generalize_cascade"` when at least one row was generalized past
  `zip5`, providing an auditable record of what happened to each row.

  Config validation runs at execution time, pre-mutation, fail-closed. Raises
  `PlanCompileError` on unsupported `type`, empty `cascade`, or missing
  `suppress`. Error codes: `geo_generalize_unsupported_type`,
  `geo_generalize_invalid_cascade`, `geo_generalize_missing_suppress`.

- **`us_zip3_population.parquet`** added to
  `src/decoy_engine/reference_tables/data/`. Loaded by `geo_generalize` via
  `load_table("us_zip3_population")`. Contains the 17 HHS-restricted 3-digit
  ZIP prefixes (geographic units with population below 20,000 per
  45 CFR 164.514(b)(2)(i)(B)). Schema: `id` (int64) + `zip3` (str), following
  the SP-06 schema convention.

**SP-08b carry-forwards (not yet built):**

- `geo_generalize` lat/lng to H3 generalization (requires the `h3` dependency).
- Additional `joint_mask` reference tables: NDC drug codes, MCC merchant
  category codes, and customer-provided reference path.
- D5 distribution-preserving strategies: entity-scoped `date_shift` and
  `bucket_perturb`.
- HIPAA-pack default wiring: automatic `geo_generalize` on ZIP columns shipped in SP-11.

### Added (SP-07 text_mask strategy, 2026-06-28)

- **`text_mask` mask strategy** (`src/decoy_engine/transforms/text_mask.py`,
  `src/decoy_engine/execution/_strategies/_text_mask.py`, SP-07 /
  P5.S.text_mask.1/2/3). Span-level PII masking for free-text columns: scans
  each cell with the STORM detector library (`iter_spans`) and masks only the
  PII-bearing spans, leaving surrounding prose intact. Registered in
  `SCALAR_HANDLERS` and available in all V2 mask pipelines. STORM is the single
  source of truth: any detector added to `_SPAN_DETECTORS` is automatically
  available to `text_mask` in the same release without a separate wiring step.

  Config surface (`provider_config:` keys):

  - `detectors` (list or null): detector IDs to run; null or absent runs all
    built-in span detectors. Unknown IDs are skipped silently.
  - `per_detector_strategy` (dict): per-detector strategy overrides. Keys are
    detector IDs; values are `fpe`, `faker`, `date_shift`, `redact`, or
    `passthrough`. Unspecified detectors fall back to `DETECTOR_DEFAULTS`.
  - `unmatched_span_policy` (str, default `redact`): controls text in each cell
    that no detector matched. See policy detail below.
  - `token` (str, default `[REDACTED]`): replacement token for the `redact`
    unmatched policy and for per-span `redact` strategy dispatch.
  - `min_days` / `max_days` (int, defaults -365 / 365): date-shift offset range
    for spans dispatched to the `date_shift` strategy.

  **TIER-1 vs TIER-2 detector reachability.** The `DETECTOR_DEFAULTS` table
  holds entries for 26 detector IDs, divided into two tiers based on whether
  `iter_spans` can reach them under the built-in path:

  - TIER 1 (11, fire automatically via `iter_spans`): `email`, `ssn`,
    `us_phone`, `us_zip`, `pan`, `iban`, `ipv4`, `icd10`, `npi`, `url`,
    `street_address`. These 11 detectors produce spans on every `mask_cell`
    call under the built-in path.
  - TIER 2 (15, NER/custom-only, NOT reached by the built-in path):
    `person_name`, `first_name`, `last_name`, `address`, `iso_date`, `us_date`,
    `eu_date`, `fax_number`, `cvv`, `mrn`, `health_plan_id`, `license_num`,
    `vehicle_id`, `device_id`, `biometric_id`. `iter_spans` never emits spans
    with TIER-2 IDs because name-hint-only regexes are intentionally excluded
    from `_SPAN_DETECTORS`. TIER-2 defaults in `DETECTOR_DEFAULTS` are active
    only when spans are injected via the `extra_spans=` parameter on `mask_cell`
    (for example, NER spans from `storm.ner.iter_ner_spans`).

  Under the built-in path alone, person names, free-text addresses, and dates
  are NOT masked, regardless of any `per_detector_strategy` settings for TIER-2
  detector IDs.

  **`unmatched_span_policy` and the passthrough leak caveat.** Text not covered
  by any detected span is controlled by this policy:

  - `redact` (default, safe): replace unmatched text with `token`. Treats all
    unmatched content as potentially undetected PII. Under this policy, TIER-2
    values (names, dates) that the built-in detectors miss are tokenized rather
    than leaking.
  - `passthrough` (operator opt-in, risk): pass unmatched text through unchanged.
    The engine emits a WARNING per cell stating that only the 11 TIER-1 detectors
    ran and that names, addresses, and dates not supplied via `extra_spans=` ride
    through in the clear. Use only when surrounding prose is known safe.
  - `replace_with_token`: replace unmatched text with the fixed sentinel
    `[UNMATCHED]`, distinguishable from per-span redaction tokens.

  **Cross-cell determinism.** Each matched span is keyed by
  `HMAC-SHA256(job_seed, matched_text)` (RFC 2104). The key depends only on the
  matched value, not on surrounding cell text, column name, or row index. The
  same SSN in two different cells always produces the same masked SSN.

  **Raw-value isolation.** `matched_text` is consumed only to derive HMAC key
  material and drive the strategy. It is never written to logs or evidence. A
  sentry test enforces this invariant.

  **Overlap resolution.** When two detected spans overlap, the leftmost span
  wins; ties on start position resolve to the longer match
  (leftmost-then-longest). An earlier spec draft described this as
  "longer-match-wins", which is imprecise: the primary sort key is start
  position, not span length.

  **Carry-forwards (not yet in SP-07).** Automatic NER wiring via a `ner:`
  config key on the column (intended to drive `storm.ner.iter_ner_spans` into
  the `extra_spans=` path to reach TIER-2 name/date spans) is designed in
  `storm/ner.py` but not yet wired into the column handler. Deferred to
  SP-16/SP-19. The `extra_spans=` injection path on `mask_cell` is available
  today for callers that supply spans directly. HIPAA-pack default wiring is
  SP-11. The `decoy text-mask explain` CLI subcommand is deferred to
  SP-16/SP-19.

### Added (SP-06 expression parser + reference tables, 2026-06-28)

- **Closed-vocabulary expression parser** (`src/decoy_engine/expressions/`,
  SP-06 / P5.INFRA.2). The `expressions.py` module is promoted to a package.
  The existing `safe_eval` / `BASE_GLOBALS` / `MASK_GLOBALS` / `make_mask_globals`
  API moves to `_safe_eval.py` and is re-exported without change; all callers on
  the `formula` strategy path are unaffected.

  A new Lark-backed closed-grammar parser ships in `_lark_parser.py` and
  `grammar.lark` (Pattern: Lark EBNF parser generator, lark-parser/lark, MIT).
  This is the expression evaluator that will power the `derived`, `case_when`,
  and `derived_aggregate` strategies (SP-10, not yet built).

  Public API:
  - `compile_expr(expr_string) -> CompiledExpression`: parses and validates once
    per column at pipeline-compile time. Raises `ValidationError` for any
    expression outside the closed set.
  - `evaluate(compiled, row_context) -> value`: evaluates a compiled
    expression against one row's values. `CompiledExpression` is immutable
    and safe to share across threads.

  Permitted operator set (the grammar is the complete and only security
  boundary):

  - Arithmetic: `+ - * / //`
  - Comparison: `== != < > <= >= in`
  - Logical: `and or not`
  - String: `concat(a, b)` (exactly two arguments)
  - Date: `days_between(start, end)` (returns integer days; accepts
    `datetime.date` or ISO-8601 strings)
  - Ternary: `value if condition else other`
  - Literals: integers, floats, double-quoted strings, `True`, `False`, `None`
  - Column references: bare identifiers (no dots, no dunders)

  Anything outside that set, including function calls other than `concat` and
  `days_between`, attribute access (`.`), subscript syntax (`[]`), `import`,
  and dunder identifiers, raises `ValidationError` at compile time before any
  row data is touched. There is no `eval()`, `exec()`, or dynamic code
  execution on this path.

  Two safety bounds are applied before the parser is invoked:
  - Maximum expression length: 4096 characters.
  - Maximum parenthesis nesting depth: 50 levels.
  String literal escape sequences are validated at compile time. Single-quoted
  strings are not in the grammar; use double quotes.

- **Reference-table loader** (`src/decoy_engine/reference_tables/`,
  SP-06 / P5.INFRA.3). A new package that loads static Parquet datasets for
  use by the `code_set` and `joint_mask` strategies (SP-08/09, not yet built).

  Public API:
  - `load_table(name, path=None) -> ReferenceTable`: loads a shipped table by
    name or a customer-provided Parquet at an explicit path. Raises
    `FileNotFoundError` when no shipped table exists for `name` and no `path`
    is given; raises `ValueError` when the file is unreadable or lacks the
    required `id` column.
  - `ReferenceTable.row(index) -> dict`: random access by zero-based row index.
  - `ReferenceTable.keyed_row(key_value) -> dict`: HMAC-SHA256-keyed
    deterministic row selection (see cross-version caveat below).
  - `ReferenceTable.row_count`: total rows.
  - `ReferenceTable.column_names`: column names in load order.

  Schema convention: every table must have an `id` column of type `int64`;
  enforced at load and raises `ValueError` otherwise. Rows are sorted ascending
  by `id` at load time. Domain columns follow (for example `zip`, `city`,
  `state` for the US ZIP table). Shipped tables carry `decoy_table_version`
  Parquet file-level metadata.

  Two public-domain tables ship in `data/`:
  - `us_zip5_city_state` v1.0: US 5-digit ZIP codes with city and state
    (USPS/Census ACS source; minimal 50-row foundation slice).
  - `vehicle_make_model_year` v1.0: vehicle make, model, and year
    (NHTSA vPIC source; minimal 50-row foundation slice).

  Customer-provided pathway: pass a Parquet file path as `load_table(name,
  path=Path(...))`. The file must follow the schema convention (`id` int64
  column plus domain columns). A version mismatch between the file's
  `decoy_table_version` metadata and the engine's expected version is logged as
  a WARNING; the table is still used.

  Keyed-access semantics and cross-version caveat: `keyed_row` reduces an
  HMAC-SHA256 digest of the key value modulo `row_count` to select a position
  in the `id`-sorted row order. Access is deterministic within a single table
  version. Adding or removing rows changes `row_count` and remaps the modular
  index; a given `key_value` will select a different row in a table with a
  different row count. Cross-version key stability is NOT guaranteed. This
  constraint must be revisited before `joint_mask` and `code_set` (SP-08/09)
  make cross-version stability assumptions.

### Added (SP-05 validator framework + quarantine_rows, 2026-06-27)

- **Job-level validator framework** (`src/decoy_engine/validators/`, SP-05 /
  P5.INFRA.4). A new top-level `validators:` block in the pipeline config
  declares which validators run after all column passes complete. Six built-in
  validators ship: `luhn`, `npi`, `iban`, `vin` (all delegate to
  `checksums.validate` from SP-04), `fk_intact` (every non-null child FK value
  resolves to a parent PK), and `no_orphan_children` (every child row has a
  non-null FK value). The two FK validators implement the SDV HMA1 parent-first
  DAG pattern (sdv-dev/SDV, MIT). `validate(outputs, config)` is the single
  entry point; it returns a frozen `ValidationReport` and never mutates output.
  The `ValidationReport` and per-finding `ValidatorFinding` are persisted to the
  evidence manifest under `quality_metrics["validation"]["validators"]`.
  Fail-closed by default: any validator failure raises `ValidatorFailedError`
  (exported from `decoy_engine.errors`) and fails the job. A warn-only override
  is deferred to a later sprint.

- **`quarantine_rows` config block** (`src/decoy_engine/quarantine.py`, SP-05 /
  P5.B). A new top-level `quarantine:` block accepts `enabled`, `output_path`,
  and `triggers`. When enabled with the `validation_fail` trigger, rows that fail
  validation are written to a JSONL file at `output_path` (one JSON object per
  distinct quarantined row, with `_quarantine_trigger`, `_quarantine_reason`, and
  `_source_table` metadata fields) and removed from the main output. The job
  continues and completes successfully. Deduplication: a row failing multiple
  validators appears once in the JSONL file; `total_quarantined` counts distinct
  rows removed, while `counts_by_trigger` tallies per finding and may sum higher.
  Quarantine state is persisted to the evidence manifest under
  `quality_metrics["quarantine"]` as a `QuarantineSummary`.

  Three fail-closed guards prevent silent data loss:

  1. `enabled: true` with an empty or whitespace `output_path` raises at config
     validation (caught by Pydantic `QuarantineConfig`) and again as a backstop
     in `apply_quarantine` for callers that bypass Pydantic. There is no silent
     row-drop.
  2. An unwired trigger (`format_error` or `mask_error`) raises at config
     validation. These are reserved names for future wiring; using one now would
     appear to quarantine rows but do nothing, a silent no-op. Rejected up front.
  3. A misconfigured FK validator (missing parent table or unknown parent column
     in the `relationships:` block) raises at `validate()` call time rather than
     mass-flagging every row.

  `format_error` and `mask_error` are reserved trigger names. They are not wired
  in SP-05 and will be rejected at config validation if used.

### Added (BF1 distribution-fidelity surfacing, 2026-06-26)

- **Opt-in fidelity report on the run path** (BF1, engine slice). The
  already-built `decoy_engine.quality` metrics
  (`compute_quality_report` -> diagnostic + value-identity fidelity +
  shape fidelity) are now wired into `run_pipeline` behind a new
  default-OFF kwarg `fidelity_report: bool = False`, with an optional
  `now_iso` passthrough for deterministic `generated_at` stamping. When
  ON, a per-mask-table `quality-report/v1` block is attached under
  `ExecutionResult.quality_metrics["fidelity_reports"]` (the free-form
  dict already plumbed to the platform manifest). It is REPORT-ONLY: a
  low fidelity score never fails the job. First slice is mask-kind
  tables, marginal-only (no joint columns); generate-kind tables are
  skipped. SECURITY: only the assembled, aggregate-only report is
  emitted (column names + kinds + scores); the intermediate
  distribution snapshots that carry category labels / raw values are
  consumed but never attached, pinned by a guard test. Default-OFF
  leaves the hot path byte-for-byte unchanged, so golden / compat-corpus
  fixtures do not move; no persisted-format or seed-protocol bump.
### Added (internal module hygiene, 2026-06-26)

- **`decoy_engine.identifiers` sub-import namespace** (F9). The identifier
  families (Ein/Mrn/Ndc/Npi/Ssn adapters, domains, and validators, plus
  `IdentifierError`/`IdentifierFormatError`) are now addressable via a focused
  module: `from decoy_engine.identifiers import EinValidator`. The top-level
  `decoy_engine` package keeps all 21 symbols as module bindings for backward
  compatibility, but they are no longer part of `decoy_engine.__all__`; the
  canonical import path is `decoy_engine.identifiers`. `BundlePool`,
  `PoolCache`, `CompositeAddress`, and `composite_city_state_zip` are similarly
  removed from `__all__` while keeping their top-level bindings.

### Changed (internal module splits, 2026-06-26)

No behavior or API change. Modules that exceeded the 600-LOC orchestration cap
were decomposed into private helpers. All external import paths are preserved.

- **`generators/columns.py`** (1333 LOC) split into `_distribution.py`
  (distribution-snapshot sampler methods, F11a) and `_formula.py` (formula
  column evaluation methods, F11a); both are private mixins folded into
  `ColumnGenerator` via multiple inheritance.
- **`storm/detectors.py`** (1356 LOC) split: regex catalog extracted to
  `_patterns.py` and detector validation helpers extracted to `_validators.py`
  (F11b).
- **`storm/profiler.py`** (999 LOC) split: column-shape classification helpers
  extracted to `_classification.py` and distribution-snapshot builders extracted
  to `_distributions.py` (F11c).
- **`plan/_compile.py`** (845 LOC) split: seed-envelope builder extracted to
  `_seed_envelope.py` and relationship/namespace graph builders extracted to
  `_graph.py` (F11d). `_compile.py` now sits below the 600-LOC cap and its
  allowlist entry has been removed.
- **`generation/synthesize.py`**: the one type-only function-body import
  (`collections.abc.Iterator`) hoisted to a `TYPE_CHECKING` block (F12). All
  other deferred imports in that module are real runtime imports and were left
  in place.

### Added (BF3 generation completeness, 2026-06-26)

- **Cross-column `formula` references in v2 generation**. A generate
  `formula` column carrying `references: [...]` (e.g. an `email` column
  built from `first_name`/`last_name` siblings) is now computed instead
  of returning all-null placeholders plus a "not yet supported"
  UserWarning. `generate_tables` runs a single declared-order in-memory
  post-pass (`_fill_referenced_formula_columns`) after every sibling
  column is finalized, delegating to the existing
  `ColumnGenerator.fill_referenced_formula_column`. It reuses the same v6
  per-row family derivation as the inline formula path, so there is NO
  `SEED_PROTOCOL_VERSION` / persisted-format change. `null_probability`
  applies to the computed values; a reference to a missing column logs a
  warning and yields nulls. LIMITATION: the post-pass is a single
  declared-order pass -- a referenced formula that reads a
  LATER-declared referenced formula sees that sibling's null placeholder
  (no multi-pass dependency resolver).

### Added (capability gaps, 2026-06-12)

- **Chunked mask execution** (WS4). New public API
  `run_mask_pipeline_chunked(config, chunks, *, table, engine_version)`
  streams one table through the engine chunk-by-chunk for inputs too
  large for memory. The contract is byte parity with a full-frame run,
  honest because chunked mode only admits VALUE-KEYED strategies (hash,
  fpe, redact, truncate, text_redact, date_shift, bucketize,
  passthrough -- each output cell a pure function of its input cell +
  config + seed). `check_chunked_compatibility` rejects shuffle,
  composite/nested, faker/categorical (pool state; deferred),
  relationship configs, and generate tables with typed codes. The plan
  compiles once from a first-chunk profile; every chunk runs the
  standard pandas adapter, so parity holds by construction.

- **Multi-parent FK support** (WS5). A child column-tuple may now declare
  FK relationships to multiple parent tables (polymorphic/shared-domain
  keys). The child resolves through each parent's source->masked map in
  DECLARED CONFIG ORDER, first hit wins; a row is an orphan only when
  absent from every parent map. Per-edge orphan policies on a shared
  child tuple must agree (new error `orphan_policy_conflict`).
  BEHAVIOR CHANGE: the S2-era `multi_parent_fk_unsupported` rejection is
  gone -- configs it used to reject now compile and run.

- **NER-backed text_redact** (WS2). New opt-in `ner` key on text_redact's
  provider_config (`ner: true` or `ner: {model: ..., entities: [...]}`)
  detects person names and locations via spaCy NER -- the two categories
  the regex span catalog deliberately cannot cover -- and merges those
  spans into the same leftmost-longest overlap resolution as the regex
  detectors (`iter_spans` gains an additive `extra_spans` kwarg). New
  optional extra `decoy-engine[ner]`; the model installs separately via
  `python -m spacy download en_core_web_sm`. New compile check row 13
  (`text_redact_ner_available`) rejects an ner-enabled config when spacy
  or the model is missing on this host (checks_passed grows 12 -> 13).
  Off by default; the no-ner path is byte-identical to before.

- **`statistical` generate type** (WS3). Samples synthetic columns from a
  `distribution-snapshot/v1` artifact (the existing quality/snapshot
  schema is the fitted model): histogram inverse-CDF for numeric
  (Devroye), weighted top-k for categorical with
  `other_mode: redistribute|emit`, year-bin sampling for datetime, and
  `condition_on` declared-pair conditional sampling from the snapshot's
  joint contingency tables (synthpop-style). Categorical columns require
  the explicit `allow_real_categories: true` disclosure opt-in (snapshot
  top_values carry real source values; DP is out of scope for v1).
  Per-row seeded (chunk-safe), pure-Python sampling (bit-stable).
  New compile check `statistical_columns` (row 12) validates config +
  artifact at validate time; `checks_passed` grows 11 -> 12.

- **`decoy_engine.unmask_pipeline` detokenization API** (WS1): inverts
  fpe columns of a masked output under the same config; per-column
  reversibility report. See the fpe re-keying entry under Changed.

- **Mimesis backend adoption completed** (closes the S7 evaluation that was
  built but never run). With the `mimesis` extra installed, five person
  providers (`person_name`, `person_first_name`, `person_last_name`,
  `person_full_name`, `person_email`) now bind to MimesisAdapter, 17-55x
  faster than Faker with checks 1-6 parity green. Without the extra,
  behavior is byte-identical to before. The other 6 candidates were
  rejected with evidence (speed or length/distribution parity); see
  `docs/mimesis-adoption-2026-06-12.md`. The extra is now pinned
  `mimesis>=19.0,<20` (evaluated on 19.1.0), and a seeded CI tripwire
  re-runs gating parity for adopted providers.

### Fixed (audit remediation, 2026-06-12)

Findings from the 2026-06-11 full-codebase audit. Behavior changes are
called out explicitly.

- **STORM residual-PII oracle is now source-aware** (audit C1, Critical;
  + H6). A column whose mask silently failed (output positionally
  identical to source) previously reported `severity='info'` on
  faker/formula/categorical/reference/date_shift strategies (a real
  leak shipped green). Detector-flagged columns are now compared
  positionally against the source frames and severity escalates to
  `fail` (substitution strategies at >=50% identity, value-reuse
  strategies at full identity, unconfigured columns at >=50% on a
  high-confidence hit). BEHAVIOR CHANGE: pipelines with partially-failed
  masks or verbatim-preserved unconfigured PII columns now exit 4 at
  `decoy storm integrity`. Shuffle's detector-hit baseline moved
  warning -> info (expected outcome) with a full-identity fail backstop.
  `ResidualPIIFinding` gains additive `source_identity_rate` +
  `source_compared` fields (schema stays `storm-post-mask/v1`).
- **text_redact null preservation** (audit H1): `pd.NA`/`pd.NaT` no
  longer leak into output as the literal strings `'<NA>'`/`'NaT'`.
- **composite_custom slot mapping** (audit H2): non-alphabetical bundle
  declarations no longer write every generated value into the wrong
  column on the pool/sampler path. Duplicate bundle column names are
  rejected (`composite_custom_duplicate_columns`). First composite
  pandas<->polars parity coverage added.
- **Pool build race** (audit H3): concurrent cache misses on the same
  deterministic identity now build exactly once (per-identity locks);
  divergent pool instances can no longer break determinism under the
  platform's async runner.
- **New compile check row 11, `non_poolable_provider_with_pool_backend`**
  (audit H5): `strategy: faker` on a poolable=False provider (e.g.
  `uuid`) is rejected at plan compile instead of crashing at run.
  BEHAVIOR CHANGE: `checks_passed` grows 10 -> 11 (no-profile 7 -> 8);
  consumers asserting the exact check set must update.
- **New public API `run_config_only_checks(config)`**: the profile-free
  compile-check subset for config-only callers (`decoy validate`).
- **Disguises carry a required dated `version`** (product rule: a
  disguise is the canonical legal artifact for its regulation; derived
  templates pin the version). All 8 bundles stamped `2026-06-12`.
  BEHAVIOR CHANGE: third-party disguise YAMLs without `version` no
  longer load.
- **HIPAA Safe-Harbor item Q is now honestly covered** (audit M2):
  biometric_id name hints gained photo/face terms (photo, photo_url,
  face_id, headshot, ...) so photo path/URL columns route to redact;
  the disguise states explicitly that image FILE CONTENT is out of
  scope. Stale header comments that disagreed with the disguise's own
  field_rules were corrected.
- **Relationship graph dedupes duplicate edges** (audit M1): indegree
  and parents_of/children_of bookkeeping no longer inflate when a
  relationship is declared twice.
- **Stable dtype labels across pandas majors** (audit M5/BL-2): pandas-3
  default-inference labels (`str`, `datetime64[us]`) normalize to their
  historical values (`object`, `datetime64[ns]`) in ColumnProfile and
  distribution snapshots, so USER-HELD snapshot baseline digests minted
  under pandas 2.x remain valid. pandas is now capped `>=1.5.0,<4`.
- **numexpr fallback surfaced** (audit L1): the silent numexpr -> python
  engine fallback on extension-array dtypes is logged through the
  engine logger instead of an unmonitored RuntimeWarning.
- **Capability matrix lists all 34 providers** (audit M3/BL-9): the
  generator walks the live registry instead of the Faker-only _CATALOG.
- New Hypothesis property suite `tests/property/test_mask_invariants.py`
  (9 properties x 400 examples) pinning null-preservation, determinism,
  namespace isolation, and per-strategy structural invariants.

### Fixed (remediation batch 1, 2026-06-26)

Targeted correctness and hardening fixes from the F-series findings register
(`docs/remediation-source.md`). Behavior changes are called out explicitly.

- **Typed `MaskKeyDerivationError` for FPE and date-shift key failures** (F15).
  `transforms/fpe.py` and `transforms/date_shift.py` previously raised a bare
  `RuntimeError` when the per-column key derivation failed, which escaped an
  upstream `except DecoyError` handler. Both now raise
  `MaskKeyDerivationError(DecoyError, code="mask.key_derivation_failed")`, so
  the failure is catchable at the engine boundary like every other typed engine
  error. The `.strategy` attribute names the originating strategy (`"fpe"` or
  `"date_shift"`). `MaskKeyDerivationError` is exported from
  `decoy_engine.errors`. BEHAVIOR CHANGE: callers catching bare `RuntimeError`
  on these paths must update to `DecoyError` or `MaskKeyDerivationError`.

- **Deterministic shuffle binds the column name into its derivation source**
  (F4). Before this fix, two shuffle columns sharing a namespace derived their
  permutation from `derive(job_seed, namespace, b"")`, so both received the
  same permutation and permuted in lockstep. That re-links values across columns
  that masking is meant to decouple: a privacy regression. The source is now
  `derive(job_seed, namespace, column_name.encode("utf-8"))`, so each column
  draws a distinct permutation. BEHAVIOR CHANGE: deterministic-shuffle output
  shifts for all columns. This fix bundles into the upcoming
  `SEED_PROTOCOL_VERSION` v6 bump (not yet bumped); do not assume v6 has landed.

- **`vault: true` fails at compile when `cryptography` is not installed**
  (F14a). A vaulted column without the `vault` extra (`cryptography` package)
  previously reached vault-write time hours into a run before failing. The plan
  compiler now rejects it immediately with
  `PlanCompileError(code="vault_requires_cryptography")`. Install the extra with
  `pip install 'decoy-engine[vault]'`. BEHAVIOR CHANGE: configs with
  `vault: true` that previously ran until vault-write now fail at compile.

- **NER model version mismatch raises at run time** (F14b). When
  `text_redact` is configured with `ner: true`, the spaCy model version is
  stamped into the plan at compile time. If the installed model version differs
  at run time, the engine now raises
  `StrategyError(code="ner_model_version_mismatch")` before any redaction
  runs, rather than silently producing different redactions for the same config
  and seed. Pin the model version or recompile the plan after a model update.
  Plans compiled before this version have no stamped version and skip the guard.

### Security / Changed (vault hardening F13, 2026-06-26)

- **Vault format bumped to `decoy-vault/v2`; per-chunk streaming encryption**
  (F13). BEHAVIOR CHANGE: vault files written by this engine use the new
  `decoy-vault/v2` format (magic `DCYVAULT2\n`) and are not readable by any
  prior engine version. v1 vault files are not readable by this engine. This is
  a pre-GA hard cutover: no vaults exist in the wild, so no migration is
  required at this point. The forever-readable rule begins at the first
  in-the-wild v2 vault.

  Format change: the file now contains an unencrypted JSON header (`format`,
  `seed_protocol_version`, `ambiguous_dropped`, `chunk_rows`, `chunk_count`)
  followed by a sequence of length-prefixed Fernet tokens, one per bounded
  chunk of up to 65 536 sorted entries.

  Privacy fix: `VaultWriter.write` now serializes and encrypts one bounded
  chunk at a time (F13). The previous implementation serialized the entire
  source-value table into a single Parquet buffer before encrypting it. That
  created a window where the full plaintext source-value table sat in heap
  as one unencrypted blob. The new path drops each chunk's plaintext
  immediately after encrypting it; the full-table plaintext blob is never
  materialized.

- **New typed error `vault_protocol_version_mismatch`** (F13). `load_vault`
  reads the unencrypted v2 header before any decryption attempt. If the
  header's `seed_protocol_version` does not match the running
  `SEED_PROTOCOL_VERSION`, it raises
  `VaultError(code="vault_protocol_version_mismatch")` with a message naming
  both versions. Previously a cross-version vault would surface as an opaque
  `vault_key_mismatch` (because the protocol version byte is mixed into the
  derived vault key). The new code is distinct from `vault_key_mismatch` (wrong
  seed, correct version). Cross-version unmask remains unsupported; F13 makes
  the error diagnosable. `unmask_pipeline` surfaces this code in its per-column
  error list alongside the existing vault error codes.

- **Single shared seed validator** (F5). `plan/_seed.py` is a new internal
  module containing `_normalize_job_seed` and `_normalize_job_seed_int`. The
  pipeline profile path (`execution/_pipeline.py`), the plan compiler
  (`plan/_compile.py`), and generation (`generation/synthesize.py`) all route
  through it. Previously the profile path accepted a bool seed
  (`isinstance(True, int)` is True in Python), so `seed: true` in YAML would
  seed `random.Random(True) == random.Random(1)` on the profile path while
  later being rejected by the compiler, producing a non-deterministic profile.
  Now rejected uniformly across all paths. BEHAVIOR CHANGE: a non-numeric seed
  passed to the public `generate_tables` now raises `PlanCompileError`
  (code `seed_not_numeric`) instead of `ValueError`; callers catching
  `ValueError` on that path must update to `PlanCompileError` or `DecoyError`.
  BEHAVIOR CHANGE: a config with no `seed` (or `seed: null`) now defaults to
  `0` on the profile path too, so seedless profiling is deterministic and the
  former "called without a seed" warning no longer fires; callers that relied
  on seedless runs drawing fresh entropy must set an explicit random seed.

- **Shared-state RNG removed from bare `MASK_GLOBALS`** (F16a). The three RNG
  bindings (`randint`, `choice`, `random`) are no longer present in the base
  `MASK_GLOBALS` scope. They were bound to the module-global `random._random`
  instance, so two formula strategies in the same job shared process-global
  random state: column B's output depended on column A's execution order and
  was non-deterministic across runs. The only supported RNG path is
  `make_mask_globals(rng)`, which binds a per-formula isolated
  `random.Random(formula_seed)`. BEHAVIOR CHANGE: a formula that calls
  `randint`, `choice`, or `random` against the bare scope now raises
  `InvalidExpression` (undefined name) instead of silently reading shared state.

### Fixed (generation determinism v6 rewrite, 2026-06-26)

Resolves findings F2 and F3 from `docs/remediation-source.md`. References the
F4 shuffle fix (shipped earlier on its own branch) that also rides the v6 bump.

- **`SEED_PROTOCOL_VERSION` bumped 5 to 6** (F2/F3). BEHAVIOR CHANGE: all
  synthetic-generation output and all masked output shift at v6. This is a
  pre-GA hard cutover; no plans or vaults exist in the wild, so no migration is
  required at this point. A v5 vault over a synthetic column cannot be unmasked
  under v6 (the regenerated seed diverges). The explicit cross-version vault
  protocol guard (error on mismatch instead of silently returning wrong values)
  is deferred to the vault-hardening work (F13); see
  `docs/compatibility-contract.md`.

- **Generate-path seed widened from 32 bits to 256 bits** (F2). The legacy
  `synthetic_column_seed` helper truncated every HKDF-derived key to 4 bytes
  (`int.from_bytes(b[:4], "big")`), leaving a 32-bit keyspace. The replacement
  `GenDeriveContext` (`generators/derivation.py`) resolves a full 32-byte
  column root via `derive_key("gen:" + fingerprint)`, consuming all 256 bits.
  `GenDeriveContext` is the public replacement; `synthetic_column_seed` is
  removed.

- **Per-row `seed + i` arithmetic replaced with per-family HMAC derivation**
  (F3). The old `column_seed + i` per-row loop meant column A (base `S`) and
  column B (base `S+1`) produced row-shift-identical seed sequences. The new
  `row_int(family, i)` method on `GenDeriveContext` derives each row's integer
  via a version-mixed HMAC keyed to the column root and RNG family, so adjacent
  columns never share seeds under any row shift.

- **Three RNG families now draw from disjoint sub-keys** (F3). `py`
  (`random.Random`), `np` (`numpy.random.default_rng`), and `faker`
  (`Faker.seed_instance`) each receive a distinct family key derived from the
  column root. Before v6, all three were seeded from the same truncated integer.

- **Generation mixes the protocol version byte into its HMAC** (F2/F3). The
  new `_gen_hmac` helper in `generators/derivation.py` mirrors the mask-path
  envelope in `determinism/_derive.py`: both mix `SEED_PROTOCOL_VERSION` into
  the HMAC input. The protocol version is now the single compatibility knob
  across both determinism roots; a bump re-keys both masked output and
  synthetic-generation output together.

- **V2 null-injection path unified to V1 numpy-vectorized mask** (F2/F3).
  `generation/synthesize.py` (`_apply_null_probability`) previously used a
  per-row Python `random.Random` reseed loop, which converged to the correct
  null fraction but did not produce the same null pattern as V1 (which uses
  `numpy.random.default_rng(column_seed).random(n) < null_prob`). Both engines
  now use the same numpy vectorized draw seeded from `GenDeriveContext.base_int
  ("np")`, so null-probability columns are byte-identical across the two
  generation engines.

- **New tests** (F6): subprocess byte-identity gate for `generate_tables`
  (`tests/unit/generation/test_synthesize_determinism.py`), cross-column seed
  independence tests, and a full `GenDeriveContext` contract suite
  (`tests/unit/generators/test_gen_derive_context.py`).

### Added

- **Generated engine capability matrix** (`docs/capability-matrix.md`, emitted by
  `scripts/gen_capability_matrix.py`). Reads the live registries (mask + generate
  strategies, synthetic providers, connectors + capabilities, STORM detectors,
  disguises) and writes a correct-by-construction reference. A `tests/sentry/
  test_capability_matrix.py` drift guard fails CI when a registry changes without
  the matrix being regenerated, so a new capability cannot ship without its docs.

### Added (F1 compatibility-corpus expansion, 2026-06-26)

- **`distribution-snapshot/v1` added to the cross-version compatibility corpus**
  (F1). The corpus (`tests/integration/compat_corpus/`) previously covered only
  `decoy-vault/v2`. It now also freezes a synthetic `distribution-snapshot/v1`
  artifact and verifies it through the real `load_spec` reader (numeric,
  categorical, and conditioned-joint branches) on every CI run. A schema-version
  tamper bite-test confirms the guard fires for this artifact kind. Every
  corpus artifact now stamps `seed_protocol_version`. Corpus version bumped to 2.

### Added (BF4 post-mask tests, 2026-06-26)

- **TDD synthetic fixture test suites for post-mask check runners** (BF4).
  Two pure-engine test modules exercise the behavioral contracts of the
  residual-PII scanner and FK-preservation checker, both shipped as part of
  Reframe-A. `tests/unit/storm/test_bf4_residual_pii.py` (12 test scenarios,
  374 LOC) covers failed-hash detection (S1), successful masking (S2),
  unconfigured PII columns (S3), redact failures (S4), multi-column mixtures
  (S5), non-PII columns (S6), and a security invariant asserting that report
  findings never leak raw cell values. `tests/unit/storm/test_bf4_fk_preservation.py`
  (15 test scenarios, 417 LOC) validates consistent masking (F1), orphan
  detection on inconsistent hashing (F2), null FK handling (F3), multi-child
  independence (F4), namespace routing (F5), composite FK tuples (F6), missing-
  parent error gracefully handled (F7), and the security invariant that findings
  carry no raw key material.

### Changed

- **Repository visibility flipped to public** (2026-06-02). Aligns
  with the OSS launch plan (memory: `OSS CLI launch` PO lock
  2026-06-01: "publish free Apache-2.0 decoy-cli + decoy-engine on
  PyPI"). Trigger for the flip: the `release-smoke.yml` workflow in
  the sibling `decoy` CLI repo needs to clone the engine from
  `git+https://github.com/louiskeep/decoy-engine@main` during the
  pre-publish window; cross-repo `git clone` of a private repo from
  inside a public-workflow runner fails with `could not read Username`
  (no TTY for the auth prompt). Making the engine public unblocks
  the cross-repo clone without introducing a PAT secret.
- Pre-flip pre-flight (working-tree only, 2026-06-02): LICENSE +
  NOTICE present and correct (Apache-2.0); no tracked secrets
  (AKIA*, sk_live_, password=, api_key=, private_key=); no tracked
  .env / credentials files; fixture CSVs are faker-generated
  synthetic data; logs are gitignored. Git history was not scanned
  for redacted secrets; if any historical leak surfaces post-flip,
  `git filter-repo` + force-push + immediate credential rotation is
  the recovery path.

### Added

- OSS.3 packaging metadata: PyPI Trove classifiers (Python 3.10/3.11/3.12,
  Apache-2.0 license, Topic taxonomy), keywords (data-masking,
  synthetic-data, faker, mimesis, pandas, polars, etc.), and the
  `[project.urls]` block (Homepage, Repository, Documentation, Issues,
  Changelog) surfaced on the PyPI sidebar.
- This `CHANGELOG.md` itself.

### Added (BF2 field-recognition harness, 2026-06-26)

Groundwork for a future ML column classifier (ML2+, gated and not built
here). Both additions are off the public run path and are intentionally
NOT re-exported from `decoy_engine.__init__`.

- **Regex-detector baseline harness + labeled fixtures** (`storm/eval/`,
  BF2/ML0). Five deterministic synthetic datasets with per-column
  ground-truth labels: `hipaa` (mrn, icd10, npi, health_plan_id),
  `pci` (pan, cvv, iban), `account_order` (account_id, order_id),
  `claim` (claim_id, service_date, amount), and `cryptic_header` (real
  PII under opaque column names). Identifier values (PAN, NPI, IBAN) are
  constructed with their real checksums so the structural detectors
  actually fire; the checksum-digit generators in `fixtures.py` are
  independent of `storm/detectors.py` to avoid "cheating" by sharing
  code under test. `run_baseline()` runs the registered detector set over
  all fixtures and returns a `HarnessReport` with per-field-type recall,
  precision, review-burden, and false-negative lists. Pinned
  misses at overall recall 0.8462 (11 of 13 PII columns): name-hint-gated
  health detectors (mrn, health_plan_id) miss entirely under opaque
  headers; content detectors (ssn, email, pan) stay header-agnostic and
  fire correctly; account_id is a confirmed false positive (the mrn
  detector claims generic account/acct identifiers by design). Read-only
  over the detector set; no run-path change.

- **Deterministic column feature builder** (`storm/features/`, BF2/ML1).
  `build_column_features(series, col_name)` produces a `ColumnFeatures`
  artifact: header tokens, inferred dtype, null/distinct/unique rates,
  char-class fractions, stdlib Shannon entropy (raw and normalized),
  per-detector regex weak signals (including checksum-gated rates for pan,
  iban, ipv4, icd10, npi), standalone checksum pass rates (no regex gate),
  and a `ShapeSignature` (dominant value mask, length stats). Reuses the
  profiler's four coarse classifiers (alphabet, casing, value-set-size,
  numeric-range) and the detector regex constants and validators so a
  detector change flows through automatically. Deterministic: content
  features sample `iloc[:200]` (matching the profiler's head-sample
  convention, never a random draw). `ColumnFeatures` is a separate
  artifact from `StormProfile`/`FieldStats` by design so it never
  crosses the persisted-format compatibility boundary.

### Added (ML-foundation measurement substrate, 2026-06-27)

Measurement gates for a future ML column classifier (ML2.2+). Scaffolding
is off the public run path and intentionally NOT re-exported from
`decoy_engine.__init__`.

- **Extended harness with F2, confusion matrix, and aggregate metrics**
  (`storm/eval/harness.py`, ML0/§A.1, §A.7). `run_baseline()` now
  computes per-type precision, recall, and F2 (β=2, recall-weighted per
  Presidio SpanEvaluator conventions). Aggregate metrics: macro-F2,
  weighted-F2 (corpus-prevalence weighted), balanced_accuracy (macro-average
  recall), entity-type confusion matrix (truth rows x predicted columns),
  and enumerated FP/FN lists identifying which columns false-positive or
  false-negative. This is the foundational evidence artifact proving where
  the regex detectors miss. The baseline report is frozen at
  `docs/v2/ml/baseline-report.json` with a regression-test gate
  (`tests/snapshots/test_ml_baseline_golden.py`).

- **StratifiedGroupKFold split scaffolding** (`storm/eval/split.py`,
  ML0/§A.3). Held-out split utility guarded against data leakage: group
  = the unique PII value string, so the same value cannot appear in both
  train and test. Prevents a future model memorising strings instead of
  learning column-shape patterns. `make_split_inputs()` converts labeled
  fixtures to `(X, y, groups)` for sklearn's `StratifiedGroupKFold`.
  Requires the optional `[ml]` extra (`pip install 'decoy-engine[ml]'`,
  pins scikit-learn >= 1.4, < 3). The regex baseline has no training phase
  and does not use this utility; it is scaffolding for ML2.2.

- **Confidence bands and per-column latency benchmark** (`storm/eval/bands.py`,
  ML0/§A.4). Three operational confidence bands for STORM field-recognition
  suggestions: high (precision >= 0.95), review (0.70 <= precision < 0.95),
  low (precision < 0.70). Thresholds calibrate to the regex baseline
  precision (not probabilistic model outputs; calibration deferred to ML2.2).
  Includes per-column latency micro-benchmark (target: < 50ms dev-tier budget).

- **Privacy test for baseline artifact** (`tests/privacy/`,
  test_no_raw_values_in_baseline_report.py, ML0/§B.4). Asserts no raw PII
  cell values in the frozen baseline report or feature dicts, a guard
  against accidental training-data leakage into version-controlled artifacts.

### Added (ML3 field classification and provenance, 2026-06-27)

Production column-type classification and manifest integrity features built
on the ML1/ML2 foundation. Gated by the `[ml]` optional extra; off by default.

- **ML3.1: `classify_fields()` public function** (`storm/model_pack/classify.py`).
  Entry point for LightGBM-backed field-type classification: loads the model pack
  via `ModelPackLoader`, builds ML1 aggregate column features, and returns per-
  column predictions with calibrated confidence scores and operational confidence
  bands (high/review/low). Output contains metadata only; no raw cell values are
  included (privacy invariant per ml-benchmarking-and-privacy.md §B.4). Returns
  `None` (never raises) when ML is disabled (`DECOY_ML_DISABLED=1`) or the pack
  is missing/corrupt, so callers can fall back to the deterministic regex baseline.
  Deterministic: given the same `DataFrame` and pack, always returns identical
  results. The platform's HTTP classify-fields endpoint and review UI consume this
  function (ML3.3, frontend lane).

- **ML3.2: HMAC-SHA256 provenance signing** (`storm/model_pack/provenance.py`).
  New functions `sign_manifest()` and `verify_manifest()` bind manifest integrity:
  canonical-JSON payload (all fields except `manifest_hmac` itself) is signed with
  HMAC-SHA256, binding the weights file hash, eval report hash, feature schema
  version, and pack identity. Uses stdlib `hmac` + `hashlib` (established keyed-hash
  primitive used throughout the engine). The `ModelPackLoader` enforces signature
  verification when a signing key is configured via `DECOY_PACK_SIGNING_KEY` env
  var (hex-encoded 32 bytes): unsigned packs rejected, tampered manifests detected
  via constant-time comparison. Without a key, packs are accepted with a warning
  (forward compatibility for development/testing). Production signing-key source is
  escalated (see Sprint C hand-off); key management not configured in this module.

### Added (SP-01 perf guard coverage, 2026-06-27)

- **PERF.BASE.3 guard coverage restored for the V2 baseline** (`tests/perf/`,
  SP-01). The guard suite deleted in b9b73e1 (when the engine became V2-only)
  is restored against the V2 substrate. `test_baseline_schema.py` (6 tests)
  pins `meta.schema_version`, the `["pandas", "polars"]` substrates
  declaration, 11-strategy x {small, medium} coverage, all required top-level
  and substrate timing fields, and the p95 >= p50 sanity invariant against
  `tests/perf_fixtures/engine-v2-baseline.json`.
  `test_baseline_reproducibility.py` (2 tests) runs a subprocess dual-run on
  mid-band cells (date_shift, hash) and asserts the polars p50 values land
  within 3x of each other: a harness-sanity check that a broken or
  non-deterministic benchmark script surfaces in CI. The 3x bound is
  deliberately loose and is NOT the regression gate. The throughput regression
  gate is `scripts/compare_baselines.py`, which flags any cell more than 5%
  slower than the committed baseline JSON. `docs/v2/perf/engine-v2-baseline-report.md`
  is the accompanying baseline report (human-readable gate table and caveats).
  These tests run under `pytest -m "not benchmark"` (the `perf` marker is not
  excluded from the CI regression gate).

### Added (SP-04 checksums + FPE valid-by-construction, 2026-06-27)

- **`decoy_engine.checksums` check-digit registry** (SP-04 / P5.INFRA.1).
  New module `src/decoy_engine/checksums.py` exposes a uniform pair of
  functions for seven structured-identifier schemes:
  `validate(scheme, value) -> bool` and `calc_check_digit(scheme, body) -> str`.
  Schemes and backing implementations: `luhn` (python-stdnum 2.2, Luhn 1954),
  `npi` (hand-rolled per CMS NPPES check-digit spec; enforces the 1/2
  leading-digit NPPES allocation rule), `iban` (python-stdnum 2.2 stdnum.iban,
  ISO 13616 / ISO 7064 mod-97), `vin` (hand-rolled per NHTSA 49 CFR Part 565 /
  ISO 3779), `isbn13` (python-stdnum 2.2 stdnum.isbn via GS1 EAN algorithm),
  `ean13` (python-stdnum 2.2 stdnum.ean), `gtin` (python-stdnum 2.2 stdnum.ean;
  covers all four GTIN lengths 8/12/13/14). `python-stdnum >= 2.2` is now a core
  dependency declared in `pyproject.toml`.

- **FPE `checksum:` parameter: valid-by-construction masked identifiers**
  (SP-04 / P5.INFRA.1). `transforms/fpe.py` and
  `execution/_strategies/_fpe.py` accept a new `checksum: <scheme>` config key.
  After the Feistel permutation rewrites the value body, the engine recomputes
  the check digit in place. The masked value is valid for the named scheme by
  construction, in both the forward (mask) and inverse (unmask) directions.
  Determinism is preserved: the same input, key, and scheme always produce the
  same output. `checksum:` takes priority over `validate_luhn:` when both are set.

  Schemes valid-by-construction in FPE mode: `luhn`, `npi`, `vin`, `isbn13`,
  `ean13`, `gtin`. Scheme-specific constraints applied at permutation time:
  NPI output pins the 1/2 NPPES leading digit; VIN constrains the permutation
  to the VIN alphabet (A-Z excluding I/O/Q, plus digits 0-9); ISBN-13 pins
  the 978/979 GS1 prefix.

  Three fail-closed behaviors (no silent passthrough of unmasked data):

  1. `iban` in FPE mode: `checksum: iban` raises
     `PlanCompileError(fpe_checksum_iban_unsupported)` at plan-compile and
     `FpeChecksumError` at runtime. Per-country BBAN structure enforced by
     `stdnum.iban.validate` cannot be satisfied by a format-preservation
     permutation. `checksums.validate("iban", ...)` and
     `checksums.calc_check_digit("iban", ...)` still work for validation-only
     use cases; only FPE checksum mode is unsupported for IBAN.

  2. Unknown scheme: a `checksum` value not in the known-scheme set (for example
     a typo) raises `PlanCompileError(fpe_checksum_unknown_scheme)` at compile.
     There is no silent fallback to plain FPE.

  3. Incompatible charset: a column whose configured charset cannot represent
     the scheme's required alphabet (for example `checksum: vin` with a
     digits-only charset, missing the letter characters VIN requires) raises
     `PlanCompileError(fpe_checksum_charset_incompatible)` at compile. This
     prevents a silent no-op where values would pass through unmasked because
     they fail the per-scheme minimum-body-length guard at runtime.

  `FpeChecksumError` (new typed error) is exported from `decoy_engine.errors`.

## [0.1.0] - 2026-06-02

The first publishable cut of the engine. Not yet pushed to the real
PyPI index; first publish lands with OSS.7.

### Added

- **FC-1 (mixed mask + generate)**: a single PipelineConfig can now
  declare both mask-kind tables (with `columns:`) and generate-kind
  tables (with `generate_columns:`) in one config. The top-level
  `mode:` discriminator is gone; per-table kind is inferred from
  `columns` vs `generate_columns` presence. The new
  `decoy_engine.run_pipeline` entry sequences generate -> merge ->
  mask in one call and returns an `ExecutionResult` whose
  `table_kinds: dict[str, "mask" | "generate"]` carries the per-table
  classification for manifest stamping.
- **FC-2 (self-FK end-to-end verification)**: golden fixture
  `tests/fixtures/golden/self_fk/` (50-row employees table with
  manager_id self-FK + 5 root nodes + 1 orphan) plus 4 e2e cells +
  1 invariant cell + the degenerate-case `parent_col == child_col`
  cycle-rejection pin. No engine source code change; the verification
  doc's trace proved correct.
- `classify_table_kinds(config)` top-level export: returns
  `{table_name: "mask" | "generate"}` for every table in the config.
  Used by the platform's preview helper to slice mask sources + cap
  generate row_counts independently.

### Fixed (from QA review docs/qa/review-2026-06-02-fc1-mixed-mode-engine.md)

- Finding 1 (HIGH): `_topo_sort` in `generation/synthesize.py` used
  recursive Python DFS. Reference chains >~1000 generate tables hit
  the default recursion limit and crashed with `RecursionError`.
  Replaced with iterative DFS that uses an explicit (node, parent
  iterator) work stack.
- Finding 2 (HIGH): the validator at
  `config/_pipeline.py::_reference_graph_valid` admitted a
  generate-child -> mask-parent reference (the engine docstring said
  runtime resolution was V2.1), but `synthesize.py::generate_tables`
  raised a plain `ValueError` on this case at runtime, which the
  platform's typed-exception handler did not catch -> the job hung
  in `running` forever. Post-fix the validator rejects at submit time
  with a "deferred to V2.1" message.

[Unreleased]: https://github.com/louiskeep/decoy-engine/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/louiskeep/decoy-engine/releases/tag/v0.1.0
