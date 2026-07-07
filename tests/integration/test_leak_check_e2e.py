"""S2 (Sprint 2 honesty pack) leak_check integration test: full run_pipeline.

Mirrors tests/integration/test_quarantine_e2e.py's shape. Monkeypatches a
handler to a no-op to simulate the engine-bug class leak_check exists to
catch (S2 acceptance 1), and exercises quarantine composition end-to-end
(S2 acceptance 8).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import ValidatorFailedError
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution._strategies import _hash


def _write_source(tmp_path: Path, table: pa.Table, name: str = "t") -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _config(
    src_path: str, target_path: str, *, quarantine: dict[str, Any] | None = None
) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"job_name": "sp2-leak-check-test", "seed": 42},
        "sources": {"t": {"type": "file", "path": src_path, "format": "parquet"}},
        "targets": {"t": {"type": "file", "path": target_path, "format": "parquet"}},
        "tables": [
            {"name": "t", "columns": [{"name": "ssn", "strategy": "hash", "namespace": "ns1"}]}
        ],
        "relationships": [],
        "validators": [{"name": "leak_check"}],
    }
    if quarantine is not None:
        cfg["quarantine"] = quarantine
    return cfg


class TestLeakCheckCatchesEngineBugClassLeak:
    """A no-op handler (simulated engine bug) is caught by leak_check."""

    def test_no_op_handler_raises_validator_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _NoOpHandler:
            name = "hash"

            def run(self, df, column, plan, ctx):  # type: ignore[no-untyped-def]
                return df, []  # simulated bug: no-op, column passes through unmasked

        monkeypatch.setitem(
            __import__(
                "decoy_engine.execution._strategies", fromlist=["SCALAR_HANDLERS"]
            ).SCALAR_HANDLERS,
            "hash",
            _NoOpHandler(),
        )

        src = pa.table({"ssn": ["111-22-3333", "222-33-4444", "333-44-5555"]})
        src_path = _write_source(tmp_path, src)
        config = _config(src_path, str(tmp_path / "t.out.parquet"))
        sources = {"t": pq.read_table(src_path)}

        with pytest.raises(ValidatorFailedError) as exc_info:
            run_pipeline(config, sources, engine_version="0.1.0")

        report = exc_info.value.report
        finding = next(f for f in report.findings if f.column == "ssn")
        assert finding.table == "t"
        assert "1.00" in finding.detail


class TestLeakCheckQuarantineComposition:
    """Leak findings + validation_fail trigger remove exactly the leaked rows."""

    def test_leaked_rows_quarantined_clean_output_ships(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate a partial leak: row 1 passes through unmasked, others hash normally.
        real_run = _hash.HashStrategyHandler.run

        def _partial_leak_run(self, df, column, plan, ctx):  # type: ignore[no-untyped-def]
            masked_df, warnings = real_run(self, df.copy(), column, plan, ctx)
            # Restore the original value for row 0 across a large-enough sample
            # that the ratio exceeds the 2% default threshold.
            masked_df.loc[0, column] = df.loc[0, column]
            return masked_df, warnings

        monkeypatch.setattr(_hash.HashStrategyHandler, "run", _partial_leak_run)

        n = 40  # 1/40 = 2.5% > the 2% default max_identical_ratio
        src = pa.table({"ssn": [f"{100000000 + i}" for i in range(n)]})
        src_path = _write_source(tmp_path, src)
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(
            src_path,
            str(tmp_path / "t.out.parquet"),
            quarantine={"enabled": True, "output_path": qpath, "triggers": ["validation_fail"]},
        )
        sources = {"t": pq.read_table(src_path)}

        result = run_pipeline(config, sources, engine_version="0.1.0")

        out = result.outputs["t"]
        assert out.num_rows == n - 1
        assert src.column("ssn").to_pylist()[0] not in out.column("ssn").to_pylist()

        assert Path(qpath).exists()
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["ssn"] == src.column("ssn").to_pylist()[0]
        assert records[0]["_quarantine_trigger"] == "validation_fail"
