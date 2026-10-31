"""`post_scan_integrity: true` runs the Post-scan integrity checks inside `run_pipeline` (E3b).

The config key existed on `PipelineConfig` but nothing in `execution/` honoured
it; these tests pin that it now produces `quality_metrics["post_scan_integrity"]`,
never changes output bytes, never fails the run, and routes an FK job to the
full-frame finalize branch where the whole output is resident.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline

_V = "run-pre-scan-test"


def _cfg(tmp_path, *, post_scan_integrity: bool, strategy: str = "hash"):
    df = pd.DataFrame({"email": [f"user{i}@example.com" for i in range(6)]})
    src = tmp_path / "c.csv"
    df.to_csv(src, index=False)
    raw = {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": {"t": {"type": "file", "format": "csv", "path": str(src)}},
        "tables": [
            {"name": "t", "columns": [{"name": "email", "strategy": strategy, "namespace": "e"}]}
        ],
        "targets": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}},
    }
    if post_scan_integrity:
        raw["post_scan"] = {"integrity": True}
    return PipelineConfig.model_validate(raw).model_dump(), {
        "t": pa.Table.from_pandas(df, preserve_index=False)
    }


def test_post_scan_integrity_stamps_a_report_and_keeps_output_bytes(tmp_path) -> None:
    cfg_off, sources = _cfg(tmp_path, post_scan_integrity=False)
    cfg_on, _ = _cfg(tmp_path, post_scan_integrity=True)
    off = run_pipeline(cfg_off, sources=sources, engine_version=_V)
    on = run_pipeline(cfg_on, sources=sources, engine_version=_V)
    assert on.outputs["t"].equals(off.outputs["t"])
    assert "post_scan_integrity" not in off.quality_metrics
    report = on.quality_metrics["post_scan_integrity"]
    assert isinstance(report, dict) and "fail_count" in report


def test_post_scan_integrity_is_report_only_even_when_values_leak(tmp_path) -> None:
    cfg, sources = _cfg(tmp_path, post_scan_integrity=True, strategy="passthrough")
    result = run_pipeline(cfg, sources=sources, engine_version=_V)
    report = result.quality_metrics["post_scan_integrity"]
    assert isinstance(report.get("fail_count"), int)
    assert result.outputs["t"].num_rows == 6


def test_fk_job_with_post_scan_integrity_takes_the_full_frame_branch(tmp_path) -> None:
    customers = pd.DataFrame({"id": [f"C{i}" for i in range(4)]})
    orders = pd.DataFrame({"customer_id": ["C1", "C2", "C1"]})
    cp, op = tmp_path / "c.csv", tmp_path / "o.csv"
    customers.to_csv(cp, index=False)
    orders.to_csv(op, index=False)
    raw = {
        "version": 2,
        "global_settings": {"seed": 42},
        "post_scan": {"integrity": True},
        "sources": {
            "customers": {"type": "file", "format": "csv", "path": str(cp)},
            "orders": {"type": "file", "format": "csv", "path": str(op)},
        },
        "tables": [
            {
                "name": "customers",
                "columns": [{"name": "id", "strategy": "hash", "namespace": "id_ns"}],
            },
            {
                "name": "orders",
                "columns": [{"name": "customer_id", "strategy": "hash", "namespace": "id_ns"}],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "preserve",
                "namespace": "id_ns",
            }
        ],
        "targets": {
            "customers": {"type": "file", "format": "csv", "path": str(tmp_path / "c_out.csv")},
            "orders": {"type": "file", "format": "csv", "path": str(tmp_path / "o_out.csv")},
        },
    }
    cfg = PipelineConfig.model_validate(raw).model_dump()
    sources = {
        "customers": pa.Table.from_pandas(customers, preserve_index=False),
        "orders": pa.Table.from_pandas(orders, preserve_index=False),
    }
    routes: list[str] = []

    class Sink:
        def on_event(self, event):
            if event.kind == "route":
                routes.append(event.route)

    result = run_pipeline(cfg, sources=sources, engine_version=_V, progress=Sink())
    assert "post_scan_integrity" in result.quality_metrics
    assert routes == ["full_frame"]


def test_post_scan_integrity_on_a_chunked_sink_job_still_produces_the_report(tmp_path) -> None:
    # Codex HIGH (parity E3b): a `post_scan_integrity: true` job on the streamed chunked sink
    # route silently skipped the Post-scan integrity report. The streamed output leaves
    # `result.outputs` empty, so `compute_post_scan_integrity` saw no mask tables and
    # stamped nothing -- a silent no-op of the requested check. `decide_output_mode`
    # now declines streaming for post_scan_integrity (like the other whole-output consumers:
    # validators, quarantine, fidelity, post_validation), keeping the output resident
    # so the report is produced. Masked values are chunk-invariant, so the resident
    # chunked output is Post-scan-correct.
    from tests.unit.execution import _auto_chunk_support as support
    from tests.unit.execution import _b6a_support as b6a

    src = pa.table(
        {
            "h": pa.array([f"u{i}@x.example" for i in range(support.ROWS)]),
            "r": pa.array([f"s{i}" for i in range(support.ROWS)]),
        }
    )
    path = support.write_source(src, tmp_path / "s.parquet")
    raw = {
        "version": 2,
        "global_settings": {"seed": 42},
        "post_scan": {"integrity": True},
        "sources": {"t": {"type": "file", "format": "parquet", "path": path}},
        "tables": [{"name": "t", "columns": [support.hash_col("h"), support.redact_col("r")]}],
        "targets": {"t": {"type": "file", "format": "parquet", "path": "/dev/null"}},
    }
    cfg = PipelineConfig.model_validate(raw).model_dump()
    sink = b6a.RecordingSink()
    result = b6a.run_streamed(cfg, src, sink, auto_chunk_threshold_rows=1, chunk_size_rows=2)

    # The whole point: the requested check ran, instead of a silent no-op.
    report = result.quality_metrics.get("post_scan_integrity")
    assert isinstance(report, dict) and "fail_count" in report
    # Root cause: streaming is declined for post_scan_integrity, so the run stays resident.
    assert result.quality_metrics["auto_chunk"]["output"] == {
        "mode": "resident",
        "reason": "post_scan_integrity",
    }
    assert result.quality_metrics["execution"]["outputs_streamed"] is False
    assert sink.calls == []  # a resident run never touches the streaming sink
    assert set(result.outputs) == {"t"}


def test_generate_mask_job_with_post_scan_integrity_declines_the_mixed_route(tmp_path) -> None:
    # A generate+mask job mixed-admitted under `auto` routes to the chunked "mixed"
    # lane, which never runs the post-mask Post-scan integrity report. With `post_scan_integrity: true` it
    # must decline mixed and take full-frame instead, exactly as the sequential and
    # out-of-core routes decline (review finding, parity E2a).
    import pyarrow as _pa

    from tests.parity._f2_mixed_support import CHILD, child_table, run

    keys = _pa.array([str(i) for i in range(5)] * 8, _pa.string())
    child = child_table(keys)
    base_cfg = mixed_config_helper(tmp_path, child)

    def routes_for(cfg) -> list[str]:
        captured: list[str] = []

        class Sink:
            def on_event(self, event):
                if event.kind == "route":
                    captured.append(event.route)

        result = run(cfg, {CHILD: child}, progress=Sink())
        return captured, result

    without_routes, _ = routes_for(base_cfg)
    assert "mixed" in without_routes, without_routes  # the route exists for this job

    integrity_cfg = {**base_cfg, "post_scan": {"integrity": True}}
    integrity_cfg = PipelineConfig.model_validate(integrity_cfg).model_dump()
    with_routes, integrity_result = routes_for(integrity_cfg)
    assert "mixed" not in with_routes, with_routes
    assert "post_scan_integrity" in integrity_result.quality_metrics


def mixed_config_helper(tmp_path, child):
    from tests.parity._f2_mixed_support import mixed_config

    return mixed_config(tmp_path, child)
