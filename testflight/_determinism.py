"""Cross-run determinism invariant for the test-flight suite (TH-2.2 / P1-5).

Extracted from _invariants.py to keep both modules within the ~600-line
soft cap (see the module-split note in _invariants.py's docstring).

check_determinism asserts three things about the SAME-process double run
(`_runner.run_pipeline_twice`):

  1. Table sets and per-table arrow SCHEMAS match, not only cell values.
     to_pydict() silently loses arrow dtype -- int32 and int64 both render
     as plain Python ints -- so a dtype-only regression (e.g. a column that
     should stay int64 quietly downcasts to int32 on one code path) shipped
     green before this change even though the two runs disagreed on schema.
  2. Per-table cell values match via to_pydict() (unchanged from before).
  3. The FULL quality_metrics block matches -- not only the fidelity_reports
     sub-block -- EXCLUDING known wall-clock timing keys (elapsed_ms,
     timing_per_phase, total_ms, max_ms). Those measure real CPU time and
     vary run-to-run even for a perfectly deterministic pipeline (see
     decoy_engine.validators._registry and instrumentation.timing); comparing
     them verbatim would make the gate flaky rather than a correctness
     signal, so they are stripped before comparison, not the tolerance of any
     invariant.

Cross-process caveat (still true after this change): both calls run inside
ONE Python process with ONE PYTHONHASHSEED, so a hash-seed-dependent
ordering bug that only differs ACROSS processes is invisible here. That gap
is covered separately by the committed fingerprint check
(`testflight/_fingerprint.py`), which compares today's run against a
committed golden fingerprint recorded by a prior (necessarily different)
process invocation.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

# Keys whose values are wall-clock/CPU timing measurements, not pipeline
# output -- excluded from the quality_metrics equality check so real
# scheduler jitter cannot manufacture a flaky determinism failure. Sourced
# from decoy_engine.validators._types.ValidationReport.elapsed_ms,
# decoy_engine.instrumentation.timing (total_ms/max_ms rollups), and the
# quality_summary.timing_per_phase block in execution/_pipeline_finalize.py.
_TIMING_KEYS: frozenset[str] = frozenset({"elapsed_ms", "timing_per_phase", "total_ms", "max_ms"})


def _strip_timing(obj: Any) -> Any:
    """Recursively drop known timing keys/values before determinism comparison."""
    if isinstance(obj, dict):
        return {k: _strip_timing(v) for k, v in obj.items() if k not in _TIMING_KEYS}
    if isinstance(obj, list):
        return [_strip_timing(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_strip_timing(v) for v in obj)
    return obj


def check_determinism(
    job_name: str,
    result_a: Any,
    result_b: Any,
) -> None:
    """Assert two same-process pipeline runs produce equal output and metrics.

    For every table in result_a.outputs, asserts:
      - the arrow schema matches result_b's (TH-2.2 / P1-5b).
      - result_a.outputs[t].to_pydict() == result_b.outputs[t].to_pydict().

    Also asserts the full quality_metrics block is equal, minus known timing
    keys (TH-2.2 / P1-5c) -- not only the fidelity_reports sub-block, so a
    drift anywhere else in quality_metrics (execution telemetry, validation
    findings, quarantine summary, quality_summary, ...) is now caught too.

    Args:
        job_name: Job name for error messages.
        result_a: First ExecutionResult.
        result_b: Second ExecutionResult.

    Raises:
        AssertionError: If any table schema/values or quality_metrics differ.
    """
    tables_a: dict[str, pa.Table] = result_a.outputs
    tables_b: dict[str, pa.Table] = result_b.outputs

    assert set(tables_a) == set(tables_b), (
        f"[{job_name}] determinism: output table sets differ between runs: "
        f"A={set(tables_a)} B={set(tables_b)}"
    )

    for table_name in tables_a:
        schema_a = tables_a[table_name].schema
        schema_b = tables_b[table_name].schema
        assert schema_a.equals(schema_b, check_metadata=False), (
            f"[{job_name}] determinism: table '{table_name}' arrow schema differs "
            f"between runs (a dtype-only drift is invisible to a to_pydict() "
            f"value comparison alone). A={schema_a} B={schema_b}"
        )

        dict_a = tables_a[table_name].to_pydict()
        dict_b = tables_b[table_name].to_pydict()
        assert dict_a == dict_b, (
            f"[{job_name}] determinism: table '{table_name}' differs between runs. "
            f"Columns with mismatches: "
            + ", ".join(col for col in dict_a if dict_a.get(col) != dict_b.get(col))
        )

    qm_a = _strip_timing(result_a.quality_metrics)
    qm_b = _strip_timing(result_b.quality_metrics)
    mismatched_keys = sorted(k for k in set(qm_a) | set(qm_b) if qm_a.get(k) != qm_b.get(k))
    assert qm_a == qm_b, (
        f"[{job_name}] determinism: quality_metrics differ between runs "
        f"(timing keys excluded; full block compared, not only fidelity_reports). "
        f"Top-level keys with mismatches: {mismatched_keys}"
    )
