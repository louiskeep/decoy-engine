"""S6-ENG-1: the v2 generation config contract + the generation op spine.

Reading B (parity-frozen vs V1 DataGenerator). S6-ENG-1 ships the spine + the
``sequence`` generator and the gate "a single-column generate config produces
row_count rows on the v2 path"; the per-generator V1-parity tests land in S6-ENG-2.
Also pins the mask contract is unchanged (mask still requires a source; a table is
mask XOR generate).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from decoy_engine.config import PipelineConfig
from decoy_engine.generation.synthesize import generate_tables


def _generate_config(row_count: int = 5) -> dict:
    return {
        "version": 1,
        "mode": "generate",
        "global_settings": {"seed": 42},
        "sources": {},
        "tables": [
            {
                "name": "customers",
                "row_count": row_count,
                "generate_columns": [
                    {"name": "id", "type": "sequence", "start": 1000, "step": 1},
                ],
            }
        ],
        "targets": {"customers": {"type": "file", "format": "csv", "path": "out.csv"}},
    }


def _mask_config() -> dict:
    return {
        "version": 1,
        "global_settings": {"seed": 0},
        "sources": {"customers": {"type": "file", "format": "csv", "path": "in.csv"}},
        "tables": [
            {
                "name": "customers",
                "columns": [
                    {
                        "name": "email",
                        "strategy": "faker",
                        "provider": "person_email",
                        "namespace": "ns",
                        "deterministic": True,
                    }
                ],
            }
        ],
        "targets": {"customers": {"type": "file", "format": "csv", "path": "o.csv"}},
    }


class TestGenerateConfigContract:
    def test_generate_config_validates(self):
        cfg = PipelineConfig.model_validate(_generate_config()).model_dump()
        assert cfg["mode"] == "generate"
        assert cfg["sources"] == {}
        assert cfg["tables"][0]["row_count"] == 5
        assert cfg["tables"][0]["generate_columns"][0]["type"] == "sequence"

    def test_generate_column_carries_flat_params(self):
        # extra="allow" mirror-V1: per-type params (start/step) ride flat.
        cfg = PipelineConfig.model_validate(_generate_config()).model_dump()
        col = cfg["tables"][0]["generate_columns"][0]
        assert col["start"] == 1000 and col["step"] == 1

    def test_generate_table_requires_row_count(self):
        cfg = _generate_config()
        del cfg["tables"][0]["row_count"]
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(cfg)

    def test_table_cannot_be_both_mask_and_generate(self):
        cfg = _generate_config()
        cfg["tables"][0]["columns"] = [{"name": "x", "strategy": "faker"}]
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(cfg)

    def test_generate_mode_rejects_mask_columns(self):
        # mode generate but a table declares mask columns (no generate_columns).
        cfg = _generate_config()
        cfg["tables"][0] = {
            "name": "customers",
            "columns": [{"name": "x", "strategy": "faker"}],
        }
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(cfg)


class TestMaskContractUnchanged:
    def test_mask_config_still_validates(self):
        out = PipelineConfig.model_validate(_mask_config()).model_dump()
        assert out["mode"] == "mask"  # default when omitted

    def test_mask_mode_still_requires_sources(self):
        cfg = _mask_config()
        cfg["sources"] = {}
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(cfg)

    def test_mask_table_still_requires_columns(self):
        cfg = _mask_config()
        cfg["tables"][0]["columns"] = []
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate(cfg)


class TestGenerationOpSpine:
    def test_produces_row_count_rows(self):
        # THE S6-ENG-1 GATE: a single-column generate config produces row_count rows.
        cfg = PipelineConfig.model_validate(_generate_config(row_count=7)).model_dump()
        tables = generate_tables(cfg)
        assert set(tables) == {"customers"}
        t = tables["customers"]
        assert t.num_rows == 7
        assert t.column_names == ["id"]
        assert t.column("id").to_pylist() == [1000, 1001, 1002, 1003, 1004, 1005, 1006]

    def test_zero_rows(self):
        cfg = PipelineConfig.model_validate(_generate_config(row_count=0)).model_dump()
        tables = generate_tables(cfg)
        assert tables["customers"].num_rows == 0

    def test_sequence_formatting(self):
        cfg = _generate_config(row_count=3)
        cfg["tables"][0]["generate_columns"][0] = {
            "name": "acct",
            "type": "sequence",
            "start": 1,
            "step": 1,
            "prefix": "ACCT-",
            "pad_length": 4,
        }
        cfg = PipelineConfig.model_validate(cfg).model_dump()
        vals = generate_tables(cfg)["customers"].column("acct").to_pylist()
        assert vals == ["ACCT-0001", "ACCT-0002", "ACCT-0003"]

    def test_unsupported_generator_type_raises(self):
        cfg = _generate_config()
        cfg["tables"][0]["generate_columns"][0] = {"name": "x", "type": "faker"}
        cfg = PipelineConfig.model_validate(cfg).model_dump()
        with pytest.raises(ValueError, match="S6-ENG-2"):
            generate_tables(cfg)
