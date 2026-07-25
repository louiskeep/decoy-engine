"""CodSpeed instrumented microbenchmarks.

Distinct from the two other perf-flavored suites in this repo:

- ``tests/perf/`` (PERF.BASE, marker ``perf``): wall-clock + memory BUDGET
  regressions that fail a PR outright; runs in the default regression gate.
- ``tests/benchmark/`` (marker ``benchmark``): manual, label-triggered
  wall-clock spikes that post a comment on the PR; informational, not a gate.
- ``tests/codspeed/`` (this directory, marker ``codspeed``): CodSpeed's
  instrumented (cache-simulation-based) measurement, which is reproducible
  across noisy CI runners in a way wall-clock timing is not. Opt-in: needs
  the ``[perf]`` extra (``pytest-codspeed``) and only produces uploaded,
  tracked measurements under the CodSpeed GitHub Action with
  ``CODSPEED_TOKEN`` set (.github/workflows/codspeed.yml). Without the
  token it is still a normal pytest suite -- `pytest tests/codspeed/ -m
  codspeed` runs every ``benchmark(...)`` call once as an ordinary
  function call and asserts on the result, same as any other test.

Each module benchmarks one real hot path picked from the engine's own
profiling/perf-test history (see the module docstrings), kept at a modest
input size so a run stays fast: these are regression-shape microbenchmarks,
not throughput/scale probes (that is tests/perf/'s job).
"""
