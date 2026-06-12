"""Chunked mask execution (capability-gaps WS4, 2026-06-12).

`run_mask_pipeline_chunked` masks a table chunk-by-chunk for inputs too
large to hold in memory. The non-negotiable gate is BYTE PARITY: for
any chunking of the rows, concatenated chunked output equals the
full-frame `run_pipeline` output exactly. That holds because chunked
mode only admits VALUE-KEYED strategies (every output cell is a pure
function of its input cell + config + seed, never of row position or
neighboring rows); `check_chunked_compatibility` rejects everything
else at compile time.
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_mask_pipeline_chunked
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.plan import PlanCompileError

_ENGINE_VERSION = "ws4-test"


def _config(tmp_path, columns: list[dict], table: str = "accounts") -> dict:
    cfg = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {table: {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}},
        "tables": [{"name": table, "columns": columns}],
        "targets": {table: {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")}},
    }
    return PipelineConfig.model_validate(cfg).model_dump()


_SAFE_COLUMNS = [
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
        }
    )


def _chunks(df: pd.DataFrame, size: int) -> list[pa.Table]:
    return [
        pa.Table.from_pandas(df.iloc[i : i + size], preserve_index=False)
        for i in range(0, len(df), size)
    ]


class TestChunkParity:
    @pytest.mark.parametrize("chunk_size", [1, 7, 33, 100, 250])
    def test_chunked_equals_full_frame(self, tmp_path, chunk_size: int) -> None:
        df = _frame(100)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, _SAFE_COLUMNS)

        full = run_pipeline(
            cfg,
            sources={"accounts": pa.Table.from_pandas(df, preserve_index=False)},
            engine_version=_ENGINE_VERSION,
        ).outputs["accounts"]

        out_chunks = list(
            run_mask_pipeline_chunked(
                cfg,
                _chunks(df, chunk_size),
                table="accounts",
                engine_version=_ENGINE_VERSION,
            )
        )
        chunked = pa.concat_tables(out_chunks)
        assert chunked.to_pylist() == full.to_pylist()

    def test_nulls_preserved(self, tmp_path) -> None:
        df = _frame(10)
        df.loc[3, "ssn"] = None
        df.loc[5, "notes"] = None
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, _SAFE_COLUMNS)
        out = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg, _chunks(df, 4), table="accounts", engine_version=_ENGINE_VERSION
                )
            )
        )
        assert out.column("ssn").to_pylist()[3] is None
        assert out.column("notes").to_pylist()[5] is None

    def test_deterministic_across_calls(self, tmp_path) -> None:
        df = _frame(30)
        df.to_csv(tmp_path / "in.csv", index=False)
        cfg = _config(tmp_path, _SAFE_COLUMNS)
        a = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg, _chunks(df, 7), table="accounts", engine_version=_ENGINE_VERSION
                )
            )
        )
        b = pa.concat_tables(
            list(
                run_mask_pipeline_chunked(
                    cfg, _chunks(df, 7), table="accounts", engine_version=_ENGINE_VERSION
                )
            )
        )
        assert a.equals(b)


class TestChunkedCompatibility:
    def test_shuffle_rejected(self, tmp_path) -> None:
        cfg = _config(tmp_path, [{"name": "ssn", "strategy": "shuffle"}])
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(cfg, [], table="accounts", engine_version=_ENGINE_VERSION)
            )
        assert exc.value.code == "strategy_not_chunk_safe"
        assert "shuffle" in str(exc.value)

    def test_faker_rejected_v1(self, tmp_path) -> None:
        cfg = _config(
            tmp_path,
            [
                {
                    "name": "email",
                    "strategy": "faker",
                    "provider": "person_email",
                    "deterministic": True,
                    "namespace": "e_ns",
                }
            ],
        )
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(cfg, [], table="accounts", engine_version=_ENGINE_VERSION)
            )
        assert exc.value.code == "strategy_not_chunk_safe"

    def test_relationships_rejected(self, tmp_path) -> None:
        cfg = _config(tmp_path, [{"name": "ssn", "strategy": "hash", "namespace": "n"}])
        cfg["relationships"] = [
            {
                "parent": {"table": "accounts", "columns": ["ssn"]},
                "children": [{"table": "accounts", "columns": ["ssn"]}],
                "orphan_policy": "preserve",
                "namespace": "n",
            }
        ]
        with pytest.raises(PlanCompileError) as exc:
            list(
                run_mask_pipeline_chunked(cfg, [], table="accounts", engine_version=_ENGINE_VERSION)
            )
        assert exc.value.code == "chunked_relationships_unsupported"

    def test_generate_tables_rejected(self, tmp_path) -> None:
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "synth",
                    "row_count": 5,
                    "generate_columns": [{"name": "n", "type": "sequence", "start": 1}],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            list(run_mask_pipeline_chunked(cfg, [], table="synth", engine_version=_ENGINE_VERSION))
        assert exc.value.code == "chunked_generate_unsupported"

    def test_unknown_table_rejected(self, tmp_path) -> None:
        cfg = _config(tmp_path, [{"name": "ssn", "strategy": "hash", "namespace": "n"}])
        with pytest.raises(PlanCompileError) as exc:
            list(run_mask_pipeline_chunked(cfg, [], table="nope", engine_version=_ENGINE_VERSION))
        assert exc.value.code == "chunked_table_unknown"

    def test_empty_iterator_yields_nothing(self, tmp_path) -> None:
        cfg = _config(tmp_path, [{"name": "ssn", "strategy": "hash", "namespace": "n"}])
        assert (
            list(
                run_mask_pipeline_chunked(cfg, [], table="accounts", engine_version=_ENGINE_VERSION)
            )
            == []
        )
