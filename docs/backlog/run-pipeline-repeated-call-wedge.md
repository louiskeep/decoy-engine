# Repeated `run_pipeline` calls in one process wedge nondeterministically

Status: open, unconfirmed, needs triage. Observed 2026-07-30 while building the
TEST-2 per-strategy throughput profiler (`scripts/strategy_throughput_probe.py`).

## Symptom

A single long-lived Python process that calls `run_pipeline` in a loop over
several jobs (different strategies, one masked column each) wedges: an early
call completes and prints, then a later call hangs with the process near-idle
(low CPU, not compute-bound), and `timeout` cannot always reap it (SIGTERM is
deferred, consistent with a stall inside a C extension holding the GIL). The
failing call is nondeterministic across runs (sometimes the 2nd strategy,
sometimes later).

Running each job in its own fresh interpreter always completes cleanly. The
throughput probe works around the wedge by spawning one subprocess per strategy
(`multiprocessing` spawn), which is why it is reliable.

## What is NOT yet known

- Whether this is a genuine engine resource leak (an unreleased per-call
  handle: a thread pool, a DuckDB connection, a pyarrow buffer, a temp file)
  or an interaction artifact of repeated `run_pipeline` + pyarrow in one
  long-lived interpreter. This has not been root-caused.
- The exact trigger. It reproduced across mixed strategy loops but has not been
  minimized to a single repeated call.

## Blast radius

Likely bounded in production: the platform runs the engine in a per-job
isolated worker (`decoy-platform api/jobs/runner.py`, isolated-worker path), so
a per-call leak would not accumulate across jobs. The exposed callers are
**direct library / CLI users** that loop `run_pipeline` many times in one
process. Not a masking-correctness or determinism issue.

## Next step

Minimize to the smallest repeated-call reproducer (same config twice in one
process, then vary strategy), then bisect whether a specific strategy or a
shared adapter resource accumulates. If confirmed, promote to the remediation
register with `file:line` evidence.
