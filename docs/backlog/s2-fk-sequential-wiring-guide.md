# S2 (A2) Implementation Guide: wire FK-sequential into `run_pipeline`

**Tech-lead-authored (Opus). Build agent: Sonnet. Implement strictly from this guide.**
Program: "Finish Open-Ended Surfaces". Sprint S2. Engine track.
Worktree: `/home/cam/vscode/engine-integration`, branch `program/finish-open-ended` (builds on S1 head `135045e5`).
Repo docstrings use ASCII only (no em-dash, no `->`/`<->` arrow glyphs in prose): the `source_hygiene` sentry gate rejects them. Keep every comment/docstring you write ASCII-clean.

---

## 0. What you are building, in one paragraph

`run_pipeline` (the ONLY public execution entry point) today always masks via the full-frame
`PandasExecutionAdapter.run`, which loads every table's pandas frame at once and holds every output
frame at once. The engine already has a bounded-memory alternative, `run_sequential` (masks one table
at a time in FK-topological order, evicting each wide frame after building the narrow parent key-map),
plus a `ParquetTransactionalSink` (all-or-nothing publish via one atomic directory rename). Neither is
reachable from the public entry point. This sprint adds a routing heuristic inside `run_pipeline` so
that relationship-bearing **pure-mask** jobs default to the sequential path, with an explicit override
to force full-frame, and honest per-config memory telemetry. **Blocking precondition:** `run_sequential`
does not currently drain per-row strategy errors, so the S1 honesty-pack fail-loud/quarantine guarantee
does NOT hold on it. Making `run_sequential` the default FK path without fixing that reopens the
silent-PII passthrough for FK jobs. Fixing the row-error gap is part 2 and is mandatory.

Read these before you touch code (you will edit the first two):
- `src/decoy_engine/execution/_pipeline.py` (`run_pipeline`, ~L100-337; the D8 quarantine/fail-loud block ~L266-327)
- `src/decoy_engine/execution/_sequential.py` (`run_sequential`, the per-table loop L150-213)
- `src/decoy_engine/execution/_pandas_adapter.py` (reference drain pattern: `run()` L199-234, esp. the `drain_row_errors` call L216-221)
- `src/decoy_engine/execution/_row_errors.py` (`RowError`, `RowErrorRecord`, `drain_row_errors`)
- `src/decoy_engine/execution/_transactional_sink.py` (`TransactionalSink`, `ParquetTransactionalSink`)
- `src/decoy_engine/quarantine.py` (`apply_quarantine`, `_write_jsonl`, `quarantine_manifest`)
- `src/decoy_engine/errors.py` (`RowErrorsFailedError`)
- `tests/integration/test_when_gate_row_error_leak.py` (the leak-closure acceptance pattern to mirror)
- `tests/unit/execution/test_sequential_eviction.py` + `test_transactional_sink.py` (harness + `build_fk_relational` fixture)

---

## 1. Ground truth verified in the code (do not re-derive; build on these)

1. **`run_pipeline` owns compute, not I/O.** It receives `sources: dict[str, pa.Table]` already loaded
   in memory and returns `outputs: dict[str, pa.Table]` in memory. Callers (platform runner, CLI, unmask,
   subset) read `result.outputs` and write targets themselves. `config["targets"]` paths are the
   caller's concern, not `run_pipeline`'s. **This layering must be preserved for existing callers.**
2. **`run_pipeline` is always pandas.** It hardcodes `adapter = PandasExecutionAdapter()` (L206). The
   polars substrate never reaches here, so `run_sequential` (pandas-only) is always applicable when the
   route selects it. You do NOT need a substrate guard.
3. **Row-error draining already works in `run()` but NOT in `run_sequential`.** `run()` calls
   `drain_row_errors(ctx.row_errors, table=node.table)` after each node dispatch (L216-221) and returns
   them on `ExecutionResult.row_errors`. `run_sequential` never drains, so `ctx.row_errors` accumulates
   silently across all tables and is dropped on return (its `ExecutionResult` at L225-231 omits
   `row_errors`). This is dennis's S1 MEDIUM-2 / LOW-1. **This is the gap you must close.**
4. **`run_pipeline`'s D8 block (L266-327) handles validators AND row-errors together** through one
   `apply_quarantine` pass, then a fail-closed remainder (`RowErrorsFailedError` / `ValidatorFailedError`).
   `apply_quarantine(outputs, report, quarantine_config, *, row_errors=...)` removes bad rows from the
   in-memory outputs and writes the JSONL. `_write_jsonl` opens the file in `"w"` mode (truncates).
5. **`validators` default to `[]`** (`config/_pipeline.py:86`, opt-in). Most FK mask jobs have no
   validators, so excluding validator-configured jobs from the sequential path (see the predicate) is a
   safe, rarely-hit fallback, not a blanket disqualifier.
6. **`ParquetTransactionalSink` streams then commits.** `run_sequential(sink=<transactional>)` calls
   `write(table, out)` per table in FK-topo order, then `commit()` on success or `abort()` on any
   exception (abort is best-effort, never masks the original). With a sink, `res.outputs == {}` (outputs
   are streamed, not accumulated). Byte-parity of sink output vs `run()` output is pinned by
   `test_file_sink_commit_value_parity`.
7. **The parent key-map is built from the full pre-eviction pandas frame** (`_sequential.py:184-186`),
   independent of the Arrow output. Filtering quarantined rows out of a table's Arrow **output** does not
   affect any child's FK resolution (children resolve against `parent_map_cache`, built from full frames).
   This is what makes per-table quarantine on the sequential path byte-identical to full-frame + end-of-run
   quarantine. **Preserve this order: build parent maps BEFORE any output filtering.**
8. **Module sizes now:** `_sequential.py` 280, `_pipeline.py` 337, `quarantine.py` 251. The `module_size`
   sentry cap is **600 LOC** (`tests/sentry/test_module_size.py`), enforced as a ratchet. Your additions
   stay well under; do not add any file to the allowlist.

---

## PART 1 - Routing heuristic in `run_pipeline`

### 1.1 The predicate (pure, unit-testable)

Add a module-level helper in `_pipeline.py`:

```python
def _sequential_eligible(
    profile: Any,
    *,
    has_generate_table: bool,
    validators: list[Any],
    fidelity_report: bool,
    vault_writer: Any,
) -> tuple[bool, str]:
    """Decide whether a mask job may take the bounded-memory sequential path.

    Returns (eligible, reason). `reason` is a stable telemetry token; when
    eligible it is "pure_mask_fk", otherwise it names the disqualifier.

    The sequential path streams/evicts table by table, so any run_pipeline
    post-mask step that needs every masked output resident at once
    disqualifies it: job-level validators (compare positionally against all
    sources), the fidelity report, and the token-vault collection. Pure-generate
    and mixed generate+mask jobs are disqualified because generate tables are
    not masked table-by-table through this path.
    """
    if not profile.relationships:
        return False, "no_relationships"
    if has_generate_table:
        return False, "generate_plus_mask"
    if validators:
        return False, "validators_present"
    if fidelity_report:
        return False, "fidelity_report_requested"
    if vault_writer is not None:
        return False, "vault_writer_requested"
    return True, "pure_mask_fk"
```

Notes:
- `profile.relationships` is the authoritative FK set (already computed at `_pipeline.py:164`). Use it,
  not a raw scan of `config["relationships"]`.
- The exclusions of `validators` / `fidelity_report` / `vault_writer` are load-bearing: they let the
  sequential branch skip `run_pipeline`'s entire post-mask block (see 1.4), so there is exactly one owner
  of the row-error quarantine on the sequential path (`run_sequential`, Part 2) and no double-processing.

### 1.2 The override mechanism: a kwarg, `execution_mode`

Add a keyword-only parameter to `run_pipeline`:

```python
from typing import Literal
...
    execution_mode: Literal["auto", "sequential", "full_frame"] = "auto",
    sink: TransactionalSink | None = None,
    source_loader: Callable[[str], pa.Table] | None = None,
```

Semantics:
- `"auto"` (default): route sequential iff `_sequential_eligible` is True; else full-frame.
- `"full_frame"`: always full-frame, even when eligible. This is the explicit force-full override.
- `"sequential"`: force sequential. If the job is NOT eligible, raise `ConfigError` naming the
  disqualifier (fail-closed: never silently ignore an explicit request).

**Why a kwarg and not a config field (decision, do not revisit):** `execution_mode` is a resource policy
of the *invocation*, not a property of the *data transformation*. `config` is the frozen, profile-hashed,
compatibility-contract-governed data contract; adding a routing field would pollute the profile hash and
the frozen surface for something that must be **byte-output-neutral** (sequential and full-frame produce
identical bytes; only peak memory differs). It also matches the existing shape of `run_pipeline`'s other
runtime switches (`vault_writer`, `fidelity_report`, `now_iso`) which are all caller-runtime kwargs, not
config. The platform job runner will set `execution_mode` (and provide `sink` / `source_loader`) per run
based on box size and job size.

### 1.3 The three mask-execution paths and the output contract

| Path | Trigger | Outputs | Memory profile |
|------|---------|---------|----------------|
| **full-frame** | route == full_frame (default for non-FK, mixed, validator/fidelity/vault jobs, or forced) | in `result.outputs` | all frames + all outputs resident (today's behavior, byte-identical) |
| **sequential in-memory** | route == sequential AND `sink is None` | in `result.outputs`, byte-identical | pandas working set bounded to one table; Arrow inputs + Arrow outputs still resident |
| **sequential streamed** | route == sequential AND `sink` provided | `result.outputs == {}`; written to the sink | fully bounded if `source_loader` is also lazy; else inputs resident but outputs streamed |

**Contract safety:** the streamed path (empty `result.outputs`) is only reachable when the caller
*explicitly passes a `sink`*. Every existing caller passes no sink, so they get either full-frame
(unchanged) or sequential-in-memory (same `result.outputs`, byte-identical, lower pandas peak). No
existing caller breaks. The default flip (eligible FK jobs go sequential-in-memory instead of full-frame)
is byte-output-neutral and is proven by the equivalence test in 3.2.

`source_loader`: when the caller provides one (lazy, e.g. Parquet-backed load-on-demand), pass it straight
to `run_sequential` for the full bounded-memory win. When absent, synthesize `lambda t: caller_sources[t]`
from the in-memory `sources` dict; the pandas working set is still bounded but the Arrow inputs remain
fully resident (reflected honestly in telemetry, 1.5).

### 1.4 Where the branch goes in `run_pipeline`

Compute the route immediately after `graph` is built (after `_pipeline.py:177`), so the decision is made
once. Then branch **before** the existing `if has_mask_table:` block. Concretely:

1. After building `graph`, compute:
   ```python
   eligible, route_reason = _sequential_eligible(
       profile,
       has_generate_table=has_generate_table,
       validators=(config.get("validators") or []),
       fidelity_report=fidelity_report,
       vault_writer=vault_writer,
   )
   if execution_mode == "full_frame":
       route = "full_frame"
       route_reason = "override_full_frame"
   elif execution_mode == "sequential":
       if not eligible:
           raise ConfigError(
               f"execution_mode='sequential' requested but the job is not "
               f"sequential-eligible ({route_reason})."
           )
       route = "sequential"
   else:  # "auto"
       route = "sequential" if eligible else "full_frame"
   ```
2. **Generate tables first, unchanged** (the existing `if has_generate_table:` block at L182-188). On the
   sequential route `has_generate_table` is always False, so `generate_outputs` is `{}`; leave the block
   as-is.
3. **New early-return sequential branch**, placed where the `if has_mask_table:` block begins:
   ```python
   if has_mask_table and route == "sequential":
       loader = source_loader if source_loader is not None else (
           lambda t: caller_sources[t]
       )
       seq_result = run_sequential(
           adapter := PandasExecutionAdapter(),
           plan,
           loader,
           registry=resolved_registry,
           relationship_graph=graph,
           namespace_registry=ns_registry,
           sink=sink,
           quarantine_config=config.get("quarantine"),
       )
       quality_metrics = dict(seq_result.quality_metrics)
       quality_metrics["execution"] = _execution_telemetry(
           route="sequential",
           route_reason=route_reason,
           sink=sink,
           source_loader=source_loader,
       )
       return ExecutionResult(
           outputs=dict(seq_result.outputs),  # {} when a sink was provided
           timings=seq_result.timings,
           boundary_conversion_ms=seq_result.boundary_conversion_ms,
           warnings=seq_result.warnings,
           quality_metrics=quality_metrics,
           table_kinds=table_kinds,
           row_errors=seq_result.row_errors,
       )
   ```
   Import `run_sequential` at the top of `_pipeline.py`:
   `from decoy_engine.execution._sequential import run_sequential`. Import `ConfigError` from
   `decoy_engine.errors`, and `TransactionalSink` from `decoy_engine.execution._transactional_sink`
   (TYPE_CHECKING is fine for the annotation; the value is only passed through).
4. **The existing full-frame code path is otherwise untouched.** Leave the `if has_mask_table:` block
   (adapter.run + vault + fidelity), the validators block, and the D8 quarantine/fail-loud block exactly
   as they are. They now run only on the full-frame route (the sequential route returned early). Add the
   full-frame execution telemetry just before the final `return ExecutionResult(...)`:
   ```python
   quality_metrics["execution"] = _execution_telemetry(
       route="full_frame", route_reason=route_reason, sink=None, source_loader=None
   )
   ```
   (Set it on the same `quality_metrics` dict already assembled; it is additive.)

**Non-FK / mixed / validator jobs are byte-identical to today:** they take the full-frame branch, whose
code you did not change; the only addition is one telemetry key under `quality_metrics["execution"]`.
The no-regression test (3.3) pins this.

### 1.5 Honest per-config telemetry (dennis hot-spot)

Add a small helper in `_pipeline.py`:

```python
def _execution_telemetry(
    *, route: str, route_reason: str, sink: Any, source_loader: Any
) -> dict[str, Any]:
    """Per-config execution memory telemetry. Honest by construction: it never
    claims bounded input residency unless a lazy source_loader was actually
    supplied, and never claims streamed outputs unless a sink was supplied."""
    if route == "full_frame":
        return {
            "execution_mode": "full_frame",
            "route_reason": route_reason,
            "eviction": "none",
            "outputs_streamed": False,
            "loaded_fully_in_memory": True,
        }
    return {
        "execution_mode": "sequential",
        "route_reason": route_reason,
        "eviction": "per_table",
        "outputs_streamed": sink is not None,
        # Arrow inputs are only bounded when a lazy loader replaces the fully
        # materialized sources dict. Without one, all inputs are resident even
        # though the pandas working set is bounded to one table.
        "loaded_fully_in_memory": source_loader is None,
    }
```

**The honesty rule dennis will check:** `loaded_fully_in_memory` must be `True` on the sequential path
when `source_loader is None`, because `run_pipeline` was handed a fully materialized `sources` dict. Do
NOT report bounded input memory just because the route is sequential; only a caller-supplied lazy loader
bounds inputs. This is exactly the "telemetry honest per-config" DoD item. Assert both cases in the
telemetry test (3.4).

---

## PART 2 (MANDATORY BLOCKER) - close the `run_sequential` row-error gap

Because Part 1 makes `run_sequential` the default FK mask path reachable from the public entry point, the
S1 fail-loud/quarantine guarantee MUST hold there. Today it does not: `run_sequential` never drains
`ctx.row_errors`. Fix it so that the sequential path enforces the SAME D8 rule as `run_pipeline`, and does
so per-table BEFORE the sink `write`/`commit` and BEFORE frame eviction, so a failing table never stages
or commits a leaked value.

`run_sequential` owns row-error enforcement for the WHOLE sequential path (both sink and no-sink). Because
the predicate (Part 1) excludes validators from this path, there are no validator findings to reconcile
here; `run_sequential` only handles per-row `format_error` / `mask_error` records. `run_pipeline` does NOT
re-run its D8 block for the sequential route (it returned early in 1.4), so there is exactly one owner and
no double-processing.

### 2.1 Signature change

Add a keyword-only parameter to `run_sequential` (`_sequential.py`) and to the thin
`PandasExecutionAdapter.run_sequential` wrapper (`_pandas_adapter.py:299-331`, pass it through):

```python
    quarantine_config: dict[str, Any] | None = None,
```

Default `None` preserves every existing direct caller and test.

### 2.2 Up-front validation (fail fast)

Before the masking loop, resolve the quarantine policy once:

```python
q_cfg = quarantine_config or {}
q_enabled = bool(q_cfg.get("enabled", False))
q_triggers: list[str] = list(q_cfg.get("triggers") or [])
q_output_path: str = (q_cfg.get("output_path") or "").strip()
# Fail-closed backstop for raw-dict callers who bypass QuarantineConfig
# validation: if quarantine is enabled with a row-error trigger, it must name
# an output_path, or a quarantined row would be silently dropped.
if q_enabled and q_triggers and not q_output_path:
    raise ValueError(
        "quarantine enabled with triggers but no output_path; refusing to run "
        "(would silently drop quarantined rows)."
    )
```

Add accumulators alongside the existing `outputs` / `warnings` locals:

```python
all_row_errors: list[RowErrorRecord] = []
quarantine_entries: list[dict[str, Any]] = []
counts_by_trigger: dict[str, int] = {}
```

Import `RowErrorRecord` and `drain_row_errors` from `decoy_engine.execution._row_errors`, and
`RowErrorsFailedError` from `decoy_engine.errors` (lazy import inside the function is fine, matching
`run_pipeline`'s style).

### 2.3 The per-table sequence (inside the `for table in table_order:` loop)

Current loop order (`_sequential.py:152-210`): load, guard, to_pandas, snapshot parent cols, **mask nodes
(L170-181)**, **build outgoing parent maps (L184-186)**, convert to Arrow `out` (L189), write/collect,
evict frame + snapshots, release parent maps. Insert the row-error handling as follows. **Order is
load-bearing.**

1. **After the mask-node loop (immediately after L181), drain and clear** (this clears `ctx.row_errors`
   every table, fixing LOW-1 unbounded accumulation):
   ```python
   table_records = drain_row_errors(ctx.row_errors, table=table)
   all_row_errors.extend(table_records)
   ```
2. **Fail-loud decision BEFORE anything is written or evicted.** Classify uncovered records and raise
   immediately if any exist:
   ```python
   if table_records:
       uncovered = tuple(
           r for r in table_records
           if not (q_enabled and r.trigger in q_triggers)
       )
       if uncovered:
           raise RowErrorsFailedError(uncovered)
   ```
   The raise propagates to the existing `except BaseException:` handler (L215-223), which calls
   `_tsink.abort()`. With `ParquetTransactionalSink`, abort discards staging so nothing is published; the
   leaked value never reached the sink because we raised **before** `write`. Frame eviction has not
   happened either, which is irrelevant on the exception path.
3. **Build outgoing parent maps (existing L184-186), unchanged.** They read the FULL frame
   `frames[table]`, so they must run before any output filtering. This preserves byte-parity: children
   resolve against the full parent key-map exactly as in full-frame.
4. **Convert to Arrow (existing L189):** `out = pa.Table.from_pandas(frames[table], preserve_index=False)`.
5. **Quarantine-filter the covered case, before writing:**
   ```python
   if table_records:  # reaching here means all covered (uncovered raised above)
       filtered, entries, counts, _total = compute_quarantine(
           {table: out}, None, q_cfg, row_errors=table_records
       )
       out = filtered[table]
       quarantine_entries.extend(entries)
       for trig, n in counts.items():
           counts_by_trigger[trig] = counts_by_trigger.get(trig, 0) + n
   ```
   `compute_quarantine` is the pure (no-I/O) core extracted from `apply_quarantine` in 2.4. It removes the
   bad rows from `out` and returns their original row dicts as `entries` (JSONL is written once at the end,
   2.5, to avoid the truncating `_write_jsonl("w")` clobbering earlier tables).
6. **Write / collect (existing L191-194), unchanged** - now `out` is the filtered table, so a quarantined
   value never reaches the sink.
7. **Evict frame + snapshots, release parent maps (existing L198-210), unchanged.**

### 2.4 Extract the pure quarantine core (in `quarantine.py`)

`apply_quarantine` currently both computes+filters AND writes JSONL + builds the summary in one call, and
`_write_jsonl` truncates. Extract the compute+filter portion (current lines ~149-191) into a pure
function so the sequential path can call it per table and defer the single JSONL write:

```python
def compute_quarantine(
    outputs: dict[str, pa.Table],
    report: ValidationReport | None,
    quarantine_config: dict[str, Any],
    *,
    row_errors: tuple[RowErrorRecord, ...] = (),
) -> tuple[dict[str, pa.Table], list[dict[str, Any]], dict[str, int], int]:
    """Pure compute+filter: build the worklist, produce the quarantine entry
    dicts and per-trigger counts, and return the outputs with bad rows removed.
    No file I/O. Returns (filtered_outputs, entries, counts_by_trigger, total)."""
    # ... exactly the body of current apply_quarantine lines ~140-191,
    # returning the four values instead of writing/ summarizing.
```

Then rewrite `apply_quarantine` to call it and keep its public behavior **byte-identical** (the existing
quarantine test suite pins this):

```python
def apply_quarantine(outputs, report, quarantine_config, *, row_errors=()):
    filtered, entries, counts, total = compute_quarantine(
        outputs, report, quarantine_config, row_errors=row_errors
    )
    output_path = (quarantine_config.get("output_path") or "")
    if entries and not output_path.strip():
        raise ValueError(...)  # keep the existing fail-closed message
    if entries and output_path:
        _write_jsonl(output_path, entries)
    summary = QuarantineSummary(
        enabled=True, output_path=output_path,
        counts_by_trigger=counts, total_quarantined=total,
    )
    return filtered, summary
```

This is a pure extraction: no behavior change on the full-frame path. Run the existing
`tests/integration/test_quarantine_e2e.py` and `test_row_errors_e2e.py` to confirm byte-identical.

### 2.5 Finalize after the loop, before commit

Immediately before `_tsink.commit()` (currently L212-213), and mirroring `run_pipeline`'s manifest keys:

```python
quality_metrics: dict[str, Any] = {}
if all_row_errors:
    counts: dict[str, int] = {}
    for rec in all_row_errors:
        key = f"{rec.table}.{rec.column}[{rec.trigger}]"
        counts[key] = counts.get(key, 0) + 1
    quality_metrics["row_errors"] = counts
if quarantine_entries:
    _write_jsonl(q_output_path, quarantine_entries)  # single write, all tables
    quality_metrics["quarantine"] = quarantine_manifest(
        QuarantineSummary(
            enabled=True, output_path=q_output_path,
            counts_by_trigger=counts_by_trigger, total_quarantined=len(quarantine_entries),
        )
    )
```

Note `total_quarantined = len(quarantine_entries)` is correct here because each entry is one distinct
quarantined row (dedup happens per table inside `compute_quarantine`, and row indices are per-table).

### 2.6 Carry `row_errors` (and `quality_metrics`) out of `run_sequential`

Change the final `ExecutionResult(...)` (L225-231) to include:

```python
    quality_metrics=quality_metrics,
    row_errors=tuple(all_row_errors),
```

Yes, `ExecutionResult` MUST carry `row_errors` out of `run_sequential`. It already has the field
(default `()`), `run()` populates it, and `run_pipeline` reads `mask_result.row_errors` on the
full-frame path. The sequential branch returns it straight through (1.4) so the public result is
symmetric across paths.

### 2.7 JSONL record-order caveat (write it into the equivalence test, not the code)

Full-frame quarantines in outputs-dict + row-error order; sequential quarantines in FK-topo table order.
The set of JSONL records is identical but the **line order can differ**. Do NOT assert JSONL byte-equality
across paths. Assert main-output byte-equality and JSONL record-SET equality (parse each line, compare as
sets/multisets). Main outputs ARE byte-identical because filtering is per-table and independent of order.

---

## 3. Tests the build MUST add (and the DoD they satisfy)

Put integration tests under `tests/integration/`, predicate/telemetry units under
`tests/unit/execution/`. Use the `build_fk_relational` fixture (`tests/perf_fixtures/fk_relational.py`)
for equivalence, and hand-built small configs (mirror `test_when_gate_row_error_leak.py`) for the leak
tests.

### 3.1 FK sequential row-error leak closure (mirrors `test_when_gate_row_error_leak.py`) - THE blocker proof

New file `tests/integration/test_fk_sequential_row_error_leak.py`. Build a **two-table pure-mask FK job**
(parent + child, `relationships` declaring the FK) where the PARENT has a `bucketize` (or `date_shift`)
column with one uncoercible cell (`"badX"`), reusing the source shape from the S1 leak test. Route through
the sequential path via `run_pipeline` by passing a `ParquetTransactionalSink(tmp_path/"out")` (auto route
selects sequential because the job is pure-mask FK with no validators). Assert:

- **(a) leak closed:** read `out/parent.parquet`; the raw `"badX"` is ABSENT from the masked column.
- **(b) quarantine JSONL carries the real value:** the configured `output_path` JSONL has one record whose
  original column value is `"badX"`.
- **(c) innocent row preserved:** an unaffected row is still present; exactly one row removed
  (`num_rows == n-1`).
- **(d) fail-loud before commit:** with quarantine DISABLED (or its trigger omitted), `run_pipeline`
  raises `RowErrorsFailedError`, and the sink target directory was NOT committed (assert
  `not (tmp_path/"out").exists()` or it is empty) - proving nothing published before the raise. Assert the
  record's `row_index` is the correct full-table position of the bad cell (mirrors the S1 test's index
  assertion).

Add a `when:`-gated variant (parent column has a `when` predicate) to prove the S1 subset-index remap also
holds on the sequential path.

### 3.2 Sequential-vs-full-frame equivalence (DoD: equivalence test)

New test in `tests/unit/execution/test_pipeline_routing.py`. For a `build_fk_relational` pure-mask FK
config, call `run_pipeline` twice with the SAME config/sources: once `execution_mode="full_frame"`, once
`execution_mode="sequential"` (no sink, in-memory outputs). Assert `result.outputs[t].equals(...)` for
every table and matching `warnings`. Then a third run with `execution_mode="sequential"` + a
`ParquetTransactionalSink`; read the parquet files back and assert byte-equality with the full-frame
outputs (mirror `test_file_sink_commit_value_parity`).

### 3.3 Non-FK no-regression (DoD: no regression to non-FK jobs)

A job with `relationships: []` (or a mixed generate+mask job) through `run_pipeline` with default
`execution_mode="auto"` must take the full-frame branch and produce output byte-identical to a run pinned
before this sprint. Assert `result.quality_metrics["execution"]["execution_mode"] == "full_frame"` and
`route_reason == "no_relationships"` (or `"generate_plus_mask"`). Confirm the full existing
`test_run_pipeline.py` suite still passes unchanged.

### 3.4 Telemetry honesty (DoD: telemetry honest per-config; dennis hot-spot)

For the SAME eligible FK config:
- `execution_mode="full_frame"` -> `loaded_fully_in_memory is True`, `eviction == "none"`.
- `execution_mode="sequential"`, no `source_loader` -> `execution_mode=="sequential"`,
  `eviction=="per_table"`, `outputs_streamed is False`, **`loaded_fully_in_memory is True`** (inputs still
  resident; this is the honesty assertion).
- `execution_mode="sequential"` + a lazy `source_loader` + a sink -> `loaded_fully_in_memory is False`,
  `outputs_streamed is True`.
- `execution_mode="sequential"` forced on an ineligible job -> raises `ConfigError` naming the reason.

### 3.5 Regression pins for the quarantine extraction

Run `tests/integration/test_quarantine_e2e.py` and `test_row_errors_e2e.py` unchanged; they pin that
`apply_quarantine` is byte-identical after the `compute_quarantine` extraction. Add a direct unit test for
`compute_quarantine` (single-table dict, row_errors covering one row) asserting the returned filtered
table and entries.

---

## 4. Module-size (600 LOC ratchet) implications

- `_pipeline.py` 337 -> ~400 after `_sequential_eligible`, `_execution_telemetry`, the route decision, and
  the early-return branch. Under 600.
- `_sequential.py` 280 -> ~340-360 after the per-table row-error block, params, and finalize. Under 600.
- `quarantine.py` 251 -> ~260 after extracting `compute_quarantine` (net near-zero; it is a split, not
  growth). Under 600.

Do NOT add any file to `tests/sentry/test_module_size.py`'s ALLOWLIST. If `_sequential.py` unexpectedly
crosses 600 (it should not), extract the row-error/quarantine composition (steps 2.2/2.5 helpers) into a
new `execution/_sequential_row_errors.py` rather than growing the file. Flag it if you hit this.

## 5. Exports / public surface

No new public exports are required. `run_pipeline` gains kwargs (backward compatible).
`ParquetTransactionalSink` and `TransactionalSink` are already exported from
`decoy_engine.execution` (`__init__.py`), so the platform job runner can construct a sink and pass it in.
`compute_quarantine` stays private to `quarantine.py` (no `__all__` entry). Do not bump the frozen-surface
contract; do not touch `release.py` / `is_pre_ga`.

## 6. Version / docs (barry does docs; you make the code true)

Engine is already at `0.3.0` (S1). This sprint is additive and pre-GA; **no version bump** unless dennis
asks. Leave CHANGELOG/CODEMAP edits to the barry docs step, but make sure the new behavior is
self-documenting in docstrings (routing rule in `run_pipeline`; the row-error sequence in
`run_sequential`).

## 7. CI-gate mirror - every command must pass before the sprint counts as done

Run from the worktree root with the repo venv. These mirror `.github/workflows/ci.yml` + `docs.yml` and
the `decoy-ci-environment-gotchas` note:

```bash
# lint + format (pinned versions matter; a minor bump changes rules)
ruff check src tests testflight scripts
ruff format --check src tests testflight scripts

# types
mypy src/decoy_engine testflight

# full regression gate (sentry module_size + source_hygiene run inside this)
pytest tests -m "not benchmark" --tb=short

# focused fast loop while iterating
pytest tests/integration/test_fk_sequential_row_error_leak.py \
       tests/unit/execution/test_pipeline_routing.py \
       tests/unit/execution/test_sequential_eviction.py \
       tests/unit/execution/test_transactional_sink.py \
       tests/integration/test_when_gate_row_error_leak.py \
       tests/integration/test_quarantine_e2e.py \
       tests/integration/test_row_errors_e2e.py -q

# docs (treat warnings as errors); use the .[docs]-only install to avoid the
# S1 local gotcha where dev+geo extras trip extra toctree warnings CI does not see
pip install -e ".[docs]"
sphinx-build -b html -W --keep-going docs docs/_build/html

# no-extras environment run (import guards must hold without optional deps)
# fresh venv: pip install -e .   (no extras), then pytest with importorskip guards intact
```

Known pre-existing red (NOT yours; do not try to fix, note it in the ledger): `test_v2_cloud_sources.py`
moto/`pydantic_settings` failure (S1 carry-forward, reaches into the platform sibling).

## 8. STOP and escalate to Cam (do not guess) if:

1. **The streamed-output contract turns out to be load-bearing for an existing caller.** This guide keeps
   the empty-`result.outputs` streamed path strictly opt-in (only when a `sink` is passed), so no existing
   caller should hit it. If you find a caller that passes a sink AND relies on `result.outputs`, or a test
   that assumes `run_pipeline` always returns outputs even with a sink, STOP - the contract needs a
   product decision, not a guess.
2. **`apply_quarantine` cannot be extracted byte-identically** (a pinned quarantine test moves after the
   `compute_quarantine` split). The extraction must be pure; if behavior changes, hand back what differs.
3. **The sequential-vs-full-frame equivalence test does not go byte-identical** on the FK fixture after the
   row-error wiring. That means the parent-map-before-filter ordering (2.3 step 3) or the FK resolution
   was perturbed. Do not "adjust the assertion" - the byte-parity is the whole point of Option 2; hand
   back the diverging table + columns.
4. **A pure-mask FK job in the real corpus carries `validators` by default** such that the sequential path
   never triggers in practice (defeating the sprint). If you discover platform/CLI auto-injects
   `leak_check` into every FK job, STOP: the predicate's validator exclusion needs a product decision
   (either run validators post-hoc on the in-memory sequential outputs, or accept full-frame for
   validator jobs). Do not silently broaden the predicate to include validators - that would skip
   validators on streamed jobs (a correctness regression).
5. **The same fix fails 2-3 times** (byte-parity keeps drifting, or abort/commit ordering keeps leaking).
   The mental model is wrong; hand back what you tried and observed.

Escalation = append the blocker to the program ledger in
`~/.claude/plans/decoy-finish-open-ended-program.md` and stop the loop; that is a successful autonomous
outcome.
