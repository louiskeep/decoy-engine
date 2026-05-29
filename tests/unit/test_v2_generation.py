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
        # ENG-2 M1 fix: V1 sequence ALWAYS returns strings (columns.py:305-319 wraps
        # every value through f"{prefix}{value_str}{suffix}"), even unformatted.
        cfg = PipelineConfig.model_validate(_generate_config(row_count=7)).model_dump()
        tables = generate_tables(cfg)
        assert set(tables) == {"customers"}
        t = tables["customers"]
        assert t.num_rows == 7
        assert t.column_names == ["id"]
        assert t.column("id").to_pylist() == [
            "1000", "1001", "1002", "1003", "1004", "1005", "1006",
        ]

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
        # `faker` is in the closed Literal (so it passes validation) but is not yet
        # implemented in this sub-commit of S6-ENG-2. Later sub-commits make this
        # unreachable; the test is updated then.
        cfg = _generate_config()
        cfg["tables"][0]["generate_columns"][0] = {"name": "x", "type": "faker"}
        cfg = PipelineConfig.model_validate(cfg).model_dump()
        with pytest.raises(ValueError, match="S6-ENG-2"):
            generate_tables(cfg)


# ----------------------------------------------------------------------------
# V1 parity oracle (Reading B)
# ----------------------------------------------------------------------------
# Dennis S6-ENG-2 plan: a shared helper runs V1 ``ColumnGenerator`` against the v2
# column dict shape (which mirrors V1's flat column dict per Q-S6-1) and returns
# the values list. Each parity test class asserts ``_v2_run == _v1_run`` for its
# generator under a fixed seed + ``derive_key=None``. ENG-4 adds the derive-key
# determinism tests; ENG-2 pins seed-only-path V1 equivalence.


def _v1_run(col: dict, n: int, seed: int = 42) -> list:
    """Run V1 ``ColumnGenerator`` against a single v2-shape column dict and return
    the values list (the parity oracle)."""
    from decoy_engine.generators.columns import ColumnGenerator

    cg = ColumnGenerator(seed=seed, derive_key=None)
    method = cg.generators[col["type"]]
    return method(n, col, "t", {}).tolist()


def _v2_run(col: dict, n: int, seed: int = 42) -> list:
    """Run v2 ``generate_tables`` on a single-column generate config and return the
    values list for that column."""
    cfg = {
        "version": 1,
        "mode": "generate",
        "global_settings": {"seed": seed},
        "sources": {},
        "tables": [{"name": "t", "row_count": n, "generate_columns": [col]}],
        "targets": {"t": {"type": "file", "format": "csv", "path": "o.csv"}},
    }
    cfg = PipelineConfig.model_validate(cfg).model_dump()
    return generate_tables(cfg)["t"].column(col["name"]).to_pylist()


class TestSequenceParityV1:
    """Reading B: v2 ``sequence`` is byte-identical to V1 ``_generate_sequence_column``."""

    def test_unformatted(self):
        # M1 oracle: V1 returns STRINGS even with no prefix/suffix/pad. The v2 spine
        # used to return ints here -- corrected in this sub-commit of S6-ENG-2.
        col = {"name": "id", "type": "sequence", "start": 1, "step": 1}
        assert _v2_run(col, 5) == _v1_run(col, 5)

    def test_formatted_pad(self):
        col = {
            "name": "acct",
            "type": "sequence",
            "start": 1,
            "step": 1,
            "prefix": "ACCT-",
            "pad_length": 4,
        }
        assert _v2_run(col, 3) == _v1_run(col, 3)

    def test_step_and_start(self):
        col = {"name": "n", "type": "sequence", "start": 100, "step": 7}
        assert _v2_run(col, 10) == _v1_run(col, 10)


class TestCategoricalParityV1:
    """Reading B: v2 ``categorical`` is byte-identical to V1 ``_generate_categorical_column``
    under the same seed (no ``derive_key``)."""

    def test_weighted(self):
        col = {
            "name": "tier",
            "type": "categorical",
            "categories": ["A", "B", "C"],
            "weights": [10, 1, 1],
        }
        assert _v2_run(col, 20) == _v1_run(col, 20)

    def test_uniform_no_weights(self):
        col = {
            "name": "flavor",
            "type": "categorical",
            "categories": ["red", "green", "blue"],
        }
        assert _v2_run(col, 15) == _v1_run(col, 15)

    def test_default_categories(self):
        # V1 default when `categories` omitted is ["Category A", "Category B"];
        # the v2 helper carries the same default (mirror V1).
        col = {"name": "x", "type": "categorical"}
        assert _v2_run(col, 5) == _v1_run(col, 5)
