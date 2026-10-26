# Parity E1: engine progress contract

Status: plan, PRE-RELEASE (2026-10-24). Not build-ready until plan-gate review. Part of the CLI/platform
parity program (`decoy-platform/docs/plans/2026-10-24-cli-platform-parity-program.md`); source finding:
`decoy-platform/docs/audit/cli-platform-capability-parity-audit-2026-10-24.md` §6 and §4 ("Progress events
mid-run: none").

Rules consulted: 00-universal, development-loop, risk-and-exceptions, api-and-compatibility,
observability-and-resilience, architecture, reuse-first, testing, documentation; engine `CLAUDE.md`
(library code does not know its callers; validation never mutates; 600-LOC cap).

Risk: R2 (new public engine surface consumed by two products; touches every execution route; concurrency
on the chunked and FK routes). Additive: no existing kwarg changes meaning.

## 1. Guarantee and non-goals

Guarantee: a caller of `run_pipeline`, `run_mask_chunked`, `run_fk_out_of_core`, `run_sequential` and
`run_generation` can pass a progress sink and receive, during the run, a bounded stream of value-free
events: phase start/finish, per-table row counts, periodic rate samples, warnings and decline reasons as
they are decided, and a terminal event. Byte output of every route is unchanged whether or not a sink is
attached (the sink is observation only). A raising or slow sink never changes a run's result or exit.

Non-goals: cancellation (separate; today cooperative at platform phase boundaries), a wire format for the
platform's SSE (platform adapts events to its existing `JobStep`/throughput tables), progress for
`run_storm`/`run_subset` (follow-on once the contract settles), per-row progress (per-batch/chunk is the
granularity), Rich or terminal rendering (CLI-owned).

## 2. What exists (reuse first)

- `decoy_engine/context.py` already defines `Logger` (runtime-checkable) and an optional
  `StructuredEvents` protocol (`step`, `lineage`, `fidelity`, `quarantine`, `throughput_sample`) plus safe
  `emit_step`/`emit_throughput_sample`/`emit_quarantine`/`emit_fidelity` helpers that no-op when the
  logger lacks the method and swallow sink exceptions. The platform's `api/persistence/logger.py::JobLogger`
  already implements `StructuredEvents`.
- Nothing in `execution/` or `generation/` calls any `emit_*` helper today (grep: zero call sites), and
  `run_pipeline` accepts no logger or context. The protocol is a V1 graph-runner relic that survived the
  V2 spine. This plan revives it rather than inventing a second event surface.
- `run_mask_chunked` already has a `chunk_result_sink: list[Any] | None` side channel for per-chunk
  evidence (platform B3). `instrumentation/TimingCollector` is an opt-in after-the-fact timing surface.
- Route evidence already exists as decline codes and lane stamps in `quality_metrics` at the end of a run.

## 3. Established solutions

- dbt's structured event system (`--log-format json`, typed events with a stable `info.name`, data never
  in events) is the closest shape for a library consumed by both a CLI and a server; adopt its rule that
  every event has a name, a timestamp, a level and typed fields, and that the renderer is the consumer's.
- Python stdlib `logging` is the delivery vehicle for the narrative half (already the `Logger` protocol);
  structured events ride the optional protocol, as the existing `context.py` design intended.
- Arrow batch iteration gives natural event boundaries on the chunked, out-of-core and streamed
  generation routes; no new threading is introduced for progress.
- Remaining custom code: the event dataclasses and the per-route emit points. No scheduler, no bus.

## 4. Design

### 4.1 Public surface (additive)

- `decoy_engine.progress` (new module, well under 600 LOC): frozen dataclasses `PhaseEvent(name, status,
  table, rows_in, rows_out, elapsed_s)`, `RateSample(table, rows_done, rows_total_estimate, rows_per_sec)`,
  `RouteEvent(route, lane, reason_code)`, `WarningEvent(code, table, column)`, `TerminalEvent(status,
  error_class)`; and a `ProgressSink` protocol with one method `on_event(event) -> None`.
- `run_pipeline(..., progress: ProgressSink | None = None)`; same kwarg on `run_mask_chunked`,
  `run_fk_out_of_core`, `run_sequential`, `run_generation`/`generate_tables_streamed`. `None` means today's
  behaviour exactly.
- Adapter `StructuredEventsSink(logger)` maps the new events onto the existing `StructuredEvents`
  methods so `JobLogger` works unchanged on day one; `emit_*` helpers become the implementation of that
  adapter, not a parallel path.
- Events are value-free by construction: table and column names and counts only; never cell values,
  expression text, paths beyond the declared table name, or `str(exc)` (observability rule: log where and
  how much, never what).

### 4.2 Emit points (one per existing boundary, no new phases)

| Route | Emit at |
|---|---|
| `run_pipeline` | context built (profile/compile done), route decided (`RouteEvent` with decline reasons), per-table mask start/finish, finalize/post-validation start/finish, publish, terminal |
| auto-chunk / `run_mask_chunked` | per chunk: `RateSample` (rows done, running rows/s), reusing the `chunk_result_sink` evidence fold |
| sequential FK | per table start/finish in FK order |
| out-of-core FK | per batch pass: `RateSample`; per stage: `PhaseEvent` (stage, join, emit) |
| unified slice / full frame | per table start/finish (no finer boundary exists; documented) |
| generation core | per batch written to the sink: `RateSample`; per table start/finish |

Rate samples are throttled in the engine to at most one per table per `progress_interval_s` (default 1.0,
kwarg) so a slow sink cannot be hammered; the throttle is time-based, not row-based, to keep byte output
independent of chunk size.

### 4.3 Design notes

- Principle: deep module, simple interface (one method, typed events) over a dozen callbacks. One place
  per fact: event types live in `progress.py`; route reasons reuse the existing decline-code constants.
- Established pattern followed: the repo's own `context.py` optional-protocol design, extended to the V2
  routes.
- Failure isolation: `on_event` is wrapped once in `_emit()` that catches and counts exceptions, surfaces
  the count once as a `WarningEvent(code="progress_sink_failed")` at terminal, never raises.

## 5. Slices and acceptance

1. **E1a types + sink + throttle + `run_pipeline` phase events.** Tests: unit on dataclasses and throttle;
   golden e2e proves byte-identical outputs with and without a sink (`tests/integration/golden`); a
   raising sink does not change result or outputs; events carry no values (sentry test feeds a config whose
   column names are secrets-shaped and asserts no cell value string appears in any event).
2. **E1b chunked + multi-table lane rate samples**, folding the `chunk_result_sink` evidence. Tests: rate
   sample count bounded by elapsed/interval; totals equal final row counts.
3. **E1c FK routes (sequential, out-of-core)** per-table and per-batch events. Parity tests unchanged.
4. **E1d generation core** per-batch samples on resident and streamed materialisation; pinned generation
   emits the same events (fingerprint unchanged).
5. **E1e `StructuredEventsSink` adapter + CHANGELOG + `docs/execution-backends.md` note + API reference.**
   Platform `JobLogger` receives `step`/`throughput_sample` from the adapter with no platform change.

Fail-before proof: a test that asserts today's `run_pipeline` emits zero `throughput_sample` calls to a
`StructuredEvents` logger, flipped to expect samples after E1b.

## 6. Failure modes considered

Sink raises (swallowed, counted); sink blocks (throttled, documented as caller responsibility; the CLI
writes to a local file, the platform to its DB in its existing best-effort manner); event volume on 100M
rows (bounded by time throttle, not rows); thread safety on the Rayon-backed kernels (events are emitted
from the Python driver loop only, never from inside a kernel call).

## 7. Done when

- All five slices merged; golden and parity suites green; the no-sink path is byte-identical and within
  noise on the 10M benchmark.
- `decoy-platform` runs unchanged (adapter) and the CLI plan C4 can build on the stream.
- Reference docs updated; this plan archived per the hub lifecycle.
