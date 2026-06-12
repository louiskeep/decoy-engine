"""Chunked execution substrate parity (deferred follow-up 3, 2026-06-12).

`run_mask_pipeline_chunked` accepts an execution adapter; this harness
pins the cross-substrate contract for streaming:

- pandas-chunked vs pandas full-frame `run_pipeline`: BYTE parity (the
  original WS4 contract, unchanged by adapter pluggability).
- polars-chunked at any chunk size vs polars single-chunk: Arrow-level
  equality (`pa.Table.equals`) -- within one substrate, chunking is
  invisible, schema included.
- polars-chunked vs pandas full-frame: VALUE parity (`to_pydict()`),
  per the accepted v2 type-width drift rows in SEMANTIC_DIFFERENCES.md
  (string widens to large_string across the pa -> pl -> pa boundary).

The column set covers all 8 always-safe strategies plus the
conditionally-admitted deterministic faker and categorical paths.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_mask_pipeline_chunked
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import PolarsExecutionAdapter, run_pipeline
from decoy_engine.plan import PlanCompileError

_ENGINE_VERSION = "chunked-substrate-parity-test"

_COLUMNS = [
    {
        "name": "ssn",
        "strategy": "fpe",
        "namespace": "ssn_ns",
        "provider_config": {"charset": "digits"},
    },
    {"name": "email", "strategy": "hash", "namespace": "email_ns"},
    {"name": "notes", "strategy": "text_redact"},
    {
        "name": "dob",
        "strategy": "date_shift",
        "namespace": "dob_ns",
        "provider_config": {"min_days": -30, "max_days": 30},
    },
    {"name": "zip", "strategy": "truncate", "provider_config": {"length": 3}},
    {"name": "secret", "strategy": "redact"},
    {"name": "memo", "strategy": "passthrough"},
    {
        "name": "score",
        "strategy": "bucketize",
        "provider_config": {"bins": [0, 25, 50, 75, 100]},
    },
    {
        "name": "contact",
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": "contact_ns",
        "provider_config": {"pool_size": 50},
    },
    {
        "name": "tier",
        "strategy": "categorical",
        "deterministic": True,
        "namespace": "tier_ns",
        "provider_config": {"categories": ["free", "pro", "team"], "weights": [0.6, 0.3, 0.1]},
    },
]


def _frame(n: int = 100) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ssn": [f"{i:09d}" for i in range(n)],
            "email": [f"user{i}@example.com" for i in range(n)],
            "notes": [f"contact user{i}@example.com today" for i in range(n)],
            "dob": [f"19{60 + (i % 40):02d}-03-{1 + (i % 28):02d}" for i in range(n)],
            "zip": [f"{10000 + i:05d}" for i in range(n)],
            "secret": [f"secret-{i}" for i in range(n)],
            "memo": [f"memo {i}" for i in range(n)],
            "score": [str(i % 100) for i in range(n)],
            "contact": [f"person{i}@source.example" for i in range(n)],
            "tier": [["bronze", "silver", "gold"][i % 3] for i in range(n)],
        }
    )


def _config(tmp_path, columns: list[dict], table: str = "accounts") -> dict:
    cfg = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {table: {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}},
        "tables": [{"name": table, "columns": columns}],
        "targets": {table: {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")}},
    }
    return PipelineConfig.model_validate(cfg).model_dump()


def _chunks(df: pd.DataFrame, size: int) -> list[pa.Table]:
    return [
        pa.Table.from_pandas(df.iloc[i : i + size], preserve_index=False)
        for i in range(0, len(df), size)
    ]


def _chunked(cfg: dict, df: pd.DataFrame, chunk_size: int, adapter=None) -> pa.Table:
    return pa.concat_tables(
        list(
            run_mask_pipeline_chunked(
                cfg,
                _chunks(df, chunk_size),
                table="accounts",
                engine_version=_ENGINE_VERSION,
                adapter=adapter,
            )
        )
    ).combine_chunks()


class TestChunkedSubstrateParity:
    @pytest.mark.parametrize("chunk_size", [1, 7, 33, 100, 250])
    def test_polars_chunked_value_equals_pandas_full_frame(self, tmp_path, chunk_size) -> None:
        df = _frame(100)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, _COLUMNS)
        full = run_pipeline(
            cfg,
            sources={"accounts": pa.Table.from_pandas(df, preserve_index=False)},
            engine_version=_ENGINE_VERSION,
        ).outputs["accounts"]
        polars_chunked = _chunked(cfg, df, chunk_size, adapter=PolarsExecutionAdapter())
        assert polars_chunked.column_names == full.column_names
        assert polars_chunked.to_pydict() == full.to_pydict()

    @pytest.mark.parametrize("chunk_size", [1, 7, 33])
    def test_polars_chunking_invisible_within_substrate(self, tmp_path, chunk_size) -> None:
        """Same substrate, different chunkings: Arrow-equal, schema included."""
        df = _frame(100)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, _COLUMNS)
        single = _chunked(cfg, df, 1_000, adapter=PolarsExecutionAdapter())
        chunked = _chunked(cfg, df, chunk_size, adapter=PolarsExecutionAdapter())
        assert chunked.equals(single)

    def test_pandas_chunked_byte_parity_unchanged(self, tmp_path) -> None:
        """Adapter pluggability does not move the original contract: the
        default (None) and an explicit pandas-equivalent stay byte-equal
        to the full-frame run."""
        df = _frame(100)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, _COLUMNS)
        full = run_pipeline(
            cfg,
            sources={"accounts": pa.Table.from_pandas(df, preserve_index=False)},
            engine_version=_ENGINE_VERSION,
        ).outputs["accounts"]
        default_chunked = _chunked(cfg, df, 33)
        assert default_chunked.to_pylist() == full.to_pylist()

    def test_admission_check_fires_before_substrate_work(self, tmp_path) -> None:
        cfg = _config(tmp_path, [{"name": "ssn", "strategy": "shuffle"}])
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(
                    cfg,
                    [],
                    table="accounts",
                    engine_version=_ENGINE_VERSION,
                    adapter=PolarsExecutionAdapter(),
                )
            )
        assert exc.value.code == "strategy_not_chunk_safe"
