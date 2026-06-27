"""BF1 (2026-06-26): golden determinism for the opt-in fidelity report.

`run_pipeline(..., fidelity_report=True)` is REPORT-ONLY and default-OFF.
This suite pins the two properties the surface must hold against the real
golden mask fixture (`relational_parent_child/customers.csv`) and the real
planning path (profile -> compile_plan -> adapter):

  1. Determinism: with `now_iso` pinned, the serialized report is
     byte-identical across two runs, so the manifest evidence it feeds is
     reproducible.
  2. Zero default ripple: with the flag OFF (the default), `quality_metrics`
     carries no fidelity report at all, which is why the existing golden /
     compat-corpus fixtures do not move.

It also re-asserts the security invariant on real fixture data: no source
value (the very PII the report is computed over) appears in the emitted,
aggregate-only report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline

GOLDEN = Path(__file__).resolve().parent.parent.parent / "fixtures" / "golden"
_VERSION = "bf1-golden-test"
_PINNED_NOW = "2026-06-26T00:00:00+00:00"


def _config() -> dict:
    src = str(GOLDEN / "relational_parent_child" / "customers.csv")
    raw = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {"customers": {"type": "file", "format": "csv", "path": src}},
        "tables": [
            {
                "name": "customers",
                "columns": [
                    {"name": "customer_id", "strategy": "passthrough"},
                    {"name": "name", "strategy": "passthrough"},
                    {
                        "name": "email",
                        "strategy": "faker",
                        "provider": "person_email",
                        "deterministic": True,
                        "namespace": "customer_identity",
                    },
                ],
            },
        ],
        "targets": {
            "customers": {"type": "file", "format": "csv", "path": "out.csv"},
        },
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _sources() -> tuple[dict[str, pa.Table], pd.DataFrame]:
    df = pd.read_csv(GOLDEN / "relational_parent_child" / "customers.csv", dtype=str)
    return {"customers": pa.Table.from_pandas(df, preserve_index=False)}, df


@pytest.mark.golden
class TestFidelityReportGolden:
    def test_flag_off_leaves_quality_metrics_clean(self):
        sources, _ = _sources()
        result = run_pipeline(_config(), sources=sources, engine_version=_VERSION)
        assert "fidelity_reports" not in result.quality_metrics

    def test_report_is_byte_stable_across_runs(self):
        sources, _ = _sources()
        r1 = run_pipeline(
            _config(),
            sources=sources,
            engine_version=_VERSION,
            fidelity_report=True,
            now_iso=_PINNED_NOW,
        )
        r2 = run_pipeline(
            _config(),
            sources=sources,
            engine_version=_VERSION,
            fidelity_report=True,
            now_iso=_PINNED_NOW,
        )
        j1 = json.dumps(r1.quality_metrics["fidelity_reports"]["customers"], sort_keys=True)
        j2 = json.dumps(r2.quality_metrics["fidelity_reports"]["customers"], sort_keys=True)
        assert j1 == j2

    def test_no_source_value_leaks_into_report(self):
        sources, df = _sources()
        result = run_pipeline(
            _config(),
            sources=sources,
            engine_version=_VERSION,
            fidelity_report=True,
            now_iso=_PINNED_NOW,
        )
        serialized = json.dumps(result.quality_metrics["fidelity_reports"]["customers"])
        for value in (*df["email"].tolist(), *df["name"].tolist(), *df["customer_id"].tolist()):
            assert str(value) not in serialized, f"source value {value!r} leaked into report"
