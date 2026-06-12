"""decoy_engine.unmask_pipeline (capability-gaps WS1, 2026-06-12).

The detokenization entry: given the pipeline config (which carries the
seed -- the secret -- plus per-column namespace/charset) and the masked
tables, invert every fpe column and report per-column reversibility.
The flagship cell is the full round trip: run_pipeline -> unmask_pipeline
recovers the source bytes exactly.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import unmask_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ExecutionError, run_pipeline
from decoy_engine.unmask import UnmaskResult

_ENGINE_VERSION = "ws1-test"


def _validated(cfg: dict) -> dict:
    return PipelineConfig.model_validate(cfg).model_dump()


def _config(tmp_path, columns: list[dict], seed: int = 42) -> dict:
    return _validated(
        {
            "version": 1,
            "global_settings": {"seed": seed},
            "sources": {
                "accounts": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "accounts.csv"),
                },
            },
            "tables": [{"name": "accounts", "columns": columns}],
            "targets": {
                "accounts": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "out.csv"),
                },
            },
        }
    )


def _fpe_col(name: str, namespace: str = "acct_ns", **provider_config) -> dict:
    return {
        "name": name,
        "strategy": "fpe",
        "namespace": namespace,
        "provider_config": {"charset": "digits", **provider_config},
    }


def _mask(tmp_path, cfg: dict, df: pd.DataFrame) -> dict[str, pa.Table]:
    df.to_csv(tmp_path / "accounts.csv", index=False)
    sources = {"accounts": pa.Table.from_pandas(df, preserve_index=False)}
    result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
    return dict(result.outputs)


class TestRoundTrip:
    def test_fpe_column_recovers_source_exactly(self, tmp_path):
        cfg = _config(tmp_path, [_fpe_col("acct")])
        source = ["123456789", "987654321", "000000042"]
        masked = _mask(tmp_path, cfg, pd.DataFrame({"acct": source}))
        masked_vals = masked["accounts"].column("acct").to_pylist()
        assert masked_vals != source  # actually masked

        result = unmask_pipeline(cfg, masked)
        assert isinstance(result, UnmaskResult)
        assert result.outputs["accounts"].column("acct").to_pylist() == source

    def test_nulls_stay_null(self, tmp_path):
        cfg = _config(tmp_path, [_fpe_col("acct")])
        masked = _mask(
            tmp_path, cfg, pd.DataFrame({"acct": ["123456789", None, "987654321"]})
        )
        result = unmask_pipeline(cfg, masked)
        vals = result.outputs["accounts"].column("acct").to_pylist()
        assert vals[0] == "123456789"
        assert vals[1] is None
        assert vals[2] == "987654321"

    def test_separators_round_trip(self, tmp_path):
        cfg = _config(tmp_path, [_fpe_col("acct")])
        source = ["123-45-6789", "987-65-4321"]
        masked = _mask(tmp_path, cfg, pd.DataFrame({"acct": source}))
        result = unmask_pipeline(cfg, masked)
        assert result.outputs["accounts"].column("acct").to_pylist() == source

    def test_luhn_valid_pan_round_trips(self, tmp_path):
        cfg = _config(tmp_path, [_fpe_col("pan", validate_luhn=True)])
        source = ["4532015112830366"]  # Luhn-valid
        masked = _mask(tmp_path, cfg, pd.DataFrame({"pan": source}))
        result = unmask_pipeline(cfg, masked)
        assert result.outputs["accounts"].column("pan").to_pylist() == source

    def test_wrong_seed_does_not_recover(self, tmp_path):
        cfg = _config(tmp_path, [_fpe_col("acct")], seed=42)
        source = ["123456789"]
        masked = _mask(tmp_path, cfg, pd.DataFrame({"acct": source}))
        wrong = _config(tmp_path, [_fpe_col("acct")], seed=43)
        result = unmask_pipeline(wrong, masked)
        assert result.outputs["accounts"].column("acct").to_pylist() != source


class TestReport:
    def test_per_column_statuses(self, tmp_path):
        # "notes" exists in the data but not in the config: the mask run
        # passes it through, and unmask reports it untouched.
        cfg = _config(
            tmp_path,
            [
                _fpe_col("acct"),
                {"name": "email", "strategy": "hash", "namespace": "email_ns"},
            ],
        )
        df = pd.DataFrame(
            {
                "acct": ["123456789"],
                "email": ["a@x.com"],
                "notes": ["hello"],
            }
        )
        masked = _mask(tmp_path, cfg, df)
        result = unmask_pipeline(cfg, masked)
        by_col = {(r.table, r.column): r for r in result.columns}
        assert by_col[("accounts", "acct")].status == "reversed"
        assert by_col[("accounts", "email")].status == "irreversible"
        assert by_col[("accounts", "email")].strategy == "hash"
        assert by_col[("accounts", "notes")].status == "untouched"
        # Irreversible and untouched columns pass through unchanged.
        out = result.outputs["accounts"]
        assert out.column("email").to_pylist() == masked["accounts"].column("email").to_pylist()
        assert out.column("notes").to_pylist() == ["hello"]

    def test_luhn_column_carries_caveat_detail(self, tmp_path):
        cfg = _config(tmp_path, [_fpe_col("pan", validate_luhn=True)])
        masked = _mask(tmp_path, cfg, pd.DataFrame({"pan": ["4532015112830366"]}))
        result = unmask_pipeline(cfg, masked)
        (entry,) = [r for r in result.columns if r.column == "pan"]
        assert entry.status == "reversed"
        assert "luhn" in entry.detail.lower()

    def test_configured_table_missing_from_inputs_reported(self, tmp_path):
        cfg = _config(tmp_path, [_fpe_col("acct")])
        result = unmask_pipeline(cfg, {})
        (entry,) = [r for r in result.columns if r.table == "accounts"]
        assert entry.status == "table_missing"
        assert result.outputs == {}

    def test_extra_input_table_passes_through(self, tmp_path):
        cfg = _config(tmp_path, [_fpe_col("acct")])
        masked = _mask(tmp_path, cfg, pd.DataFrame({"acct": ["123456789"]}))
        extra = pa.Table.from_pandas(pd.DataFrame({"x": [1, 2]}), preserve_index=False)
        masked["other"] = extra
        result = unmask_pipeline(cfg, masked)
        assert result.outputs["other"].equals(extra)
        (entry,) = [r for r in result.columns if r.table == "other"]
        assert entry.status == "untouched"


class TestErrors:
    def test_fpe_column_without_namespace_raises(self, tmp_path):
        # Hand-built config dict (the schema would normally catch this at
        # validate; unmask must not silently guess a namespace).
        cfg = {
            "global_settings": {"seed": 42},
            "tables": [
                {"name": "accounts", "columns": [{"name": "acct", "strategy": "fpe"}]}
            ],
        }
        tbl = pa.Table.from_pandas(
            pd.DataFrame({"acct": ["123456789"]}), preserve_index=False
        )
        with pytest.raises(ExecutionError) as exc:
            unmask_pipeline(cfg, {"accounts": tbl})
        assert exc.value.code == "fpe_requires_namespace"
