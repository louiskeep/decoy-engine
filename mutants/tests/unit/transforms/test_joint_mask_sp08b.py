"""SP-08b joint_mask-2 tests: NDC, MCC, and customer-provided tables (TDD: tests first).

Tests cover:
  J2.1 - NDC table loads and joint_mask can apply it.
  J2.2 - MCC table loads and joint_mask can apply it.
  J2.3 - Customer-provided table pathway: customer:/path/to/table.parquet.
  J2.4 - Cross-domain: multiple joint_masks in one fixture all apply correctly.
  J2.5 - Integration through the real plan/run path (STRATEGY-WIRING GUARD).

Methodology: HMAC-SHA256-keyed row selection via ReferenceTable.keyed_row
(RFC 2104). Reuses SP-06 reference_tables loader (pyarrow Parquet I/O).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.joint_mask import (
    JointMaskConfig,
    apply_joint_mask,
    validate_joint_mask_config,
)

_JOB_SEED = b"\xde\xca\xfe" + b"\x00" * 29

# ── J2.1: NDC table ──────────────────────────────────────────────────────────


class TestNDCJointMask:
    """NDC drug/labeler/strength/dosage-form reference table."""

    def test_ndc_table_loads(self):
        """ndc_labeler_drug_strength reference table must load without error."""
        from decoy_engine.reference_tables import load_table

        tbl = load_table("ndc_labeler_drug_strength")
        assert tbl.row_count > 0, "NDC table must have at least one row."
        assert "labeler_name" in tbl.column_names or "drug_name" in tbl.column_names, (
            "NDC table must have labeler_name or drug_name column."
        )

    def test_ndc_joint_mask_applies(self):
        """joint_mask with ndc_labeler_drug_strength replaces all target columns."""
        from decoy_engine.reference_tables import load_table

        tbl = load_table("ndc_labeler_drug_strength")
        ndc_cols = [c for c in tbl.column_names if c != "id"][:2]
        assert len(ndc_cols) >= 2, "NDC table must have at least 2 non-id columns."

        n = 5
        df = pd.DataFrame(
            {
                "patient_id": [f"P{i:04d}" for i in range(n)],
                **{col: ["placeholder"] * n for col in ndc_cols},
            }
        )
        cfg_dict = {
            "columns": ndc_cols,
            "reference": "ndc_labeler_drug_strength",
            "key_by": "patient_id",
        }
        cfg = JointMaskConfig.from_dict(cfg_dict)
        result = apply_joint_mask(df, cfg, mode="mask", job_seed=_JOB_SEED)

        # Output must not contain placeholder values.
        for col in ndc_cols:
            for val in result[col]:
                assert val != "placeholder", (
                    f"joint_mask with NDC table must replace placeholder in column {col!r}."
                )

    def test_ndc_joint_mask_output_from_valid_table_rows(self):
        """Every output row must exist in the NDC reference table."""
        from decoy_engine.reference_tables import load_table

        tbl = load_table("ndc_labeler_drug_strength")
        ndc_cols = [c for c in tbl.column_names if c != "id"][:2]

        n = 3
        df = pd.DataFrame(
            {
                "patient_id": [f"P{i:04d}" for i in range(n)],
                **{col: ["x"] * n for col in ndc_cols},
            }
        )
        cfg = JointMaskConfig.from_dict(
            {"columns": ndc_cols, "reference": "ndc_labeler_drug_strength", "key_by": "patient_id"}
        )
        result = apply_joint_mask(df, cfg, mode="mask", job_seed=_JOB_SEED)

        # Build reference set: set of (col0, col1) tuples from the table.
        ref_rows = set()
        for i in range(tbl.row_count):
            row = tbl.row(i)
            ref_rows.add(tuple(str(row.get(c, "")) for c in ndc_cols))

        for i in range(n):
            out_tuple = tuple(str(result[c].iloc[i]) for c in ndc_cols)
            assert out_tuple in ref_rows, (
                f"Row {i} output tuple {out_tuple!r} not found in NDC reference table."
            )


# ── J2.2: MCC table ──────────────────────────────────────────────────────────


class TestMCCJointMask:
    """MCC merchant category code reference table."""

    def test_mcc_table_loads(self):
        """mcc_category_description reference table must load without error."""
        from decoy_engine.reference_tables import load_table

        tbl = load_table("mcc_category_description")
        assert tbl.row_count > 0, "MCC table must have at least one row."
        assert "mcc" in tbl.column_names or "category" in tbl.column_names, (
            "MCC table must have mcc or category column."
        )

    def test_mcc_joint_mask_applies(self):
        """joint_mask with mcc_category_description replaces all target columns."""
        from decoy_engine.reference_tables import load_table

        tbl = load_table("mcc_category_description")
        mcc_cols = [c for c in tbl.column_names if c != "id"]

        n = 4
        df = pd.DataFrame(
            {
                "merchant_id": [f"M{i:04d}" for i in range(n)],
                **{col: ["placeholder"] * n for col in mcc_cols},
            }
        )
        cfg = JointMaskConfig.from_dict(
            {"columns": mcc_cols, "reference": "mcc_category_description", "key_by": "merchant_id"}
        )
        result = apply_joint_mask(df, cfg, mode="mask", job_seed=_JOB_SEED)

        for col in mcc_cols:
            for val in result[col]:
                assert val != "placeholder"

    def test_mcc_output_rows_are_from_table(self):
        """Every output row must exist in the MCC reference table."""
        from decoy_engine.reference_tables import load_table

        tbl = load_table("mcc_category_description")
        mcc_cols = [c for c in tbl.column_names if c != "id"]

        n = 3
        df = pd.DataFrame(
            {
                "merchant_id": [f"M{i:04d}" for i in range(n)],
                **{col: ["x"] * n for col in mcc_cols},
            }
        )
        cfg = JointMaskConfig.from_dict(
            {"columns": mcc_cols, "reference": "mcc_category_description", "key_by": "merchant_id"}
        )
        result = apply_joint_mask(df, cfg, mode="mask", job_seed=_JOB_SEED)

        ref_rows = set()
        for i in range(tbl.row_count):
            row = tbl.row(i)
            ref_rows.add(tuple(str(row.get(c, "")) for c in mcc_cols))

        for i in range(n):
            out_tuple = tuple(str(result[c].iloc[i]) for c in mcc_cols)
            assert out_tuple in ref_rows, (
                f"Row {i} output tuple {out_tuple!r} not found in MCC reference table."
            )


# ── J2.3: Customer-provided table ─────────────────────────────────────────────


def _make_customer_table(tmp_path: Path, cols: list[str]) -> Path:
    """Write a minimal Parquet table with an id column + given cols."""
    data = {"id": pa.array([1, 2, 3], type=pa.int64())}
    for col in cols:
        data[col] = pa.array([f"{col}_A", f"{col}_B", f"{col}_C"])
    arrow_table = pa.table(data)
    out = tmp_path / "custom.parquet"
    pq.write_table(arrow_table, str(out))
    return out


class TestCustomerProvidedTable:
    """customer:/path/to/table.parquet reference pathway."""

    def test_customer_path_parses_and_loads(self, tmp_path):
        """reference: customer:/path/to/file.parquet loads the customer table."""
        parquet_path = _make_customer_table(tmp_path, ["product_code", "category"])
        ref_str = f"customer:{parquet_path}"

        cfg = JointMaskConfig.from_dict(
            {
                "columns": ["product_code", "category"],
                "reference": ref_str,
                "key_by": "order_id",
            }
        )
        assert cfg.table.row_count == 3

    def test_customer_table_masks_tuples_correctly(self, tmp_path):
        """Customer-provided table joint_masks a DataFrame correctly."""
        parquet_path = _make_customer_table(tmp_path, ["product_code", "category"])
        ref_str = f"customer:{parquet_path}"

        n = 5
        df = pd.DataFrame(
            {
                "order_id": [f"O{i:04d}" for i in range(n)],
                "product_code": ["old"] * n,
                "category": ["old"] * n,
            }
        )
        cfg = JointMaskConfig.from_dict(
            {
                "columns": ["product_code", "category"],
                "reference": ref_str,
                "key_by": "order_id",
            }
        )
        result = apply_joint_mask(df, cfg, mode="mask", job_seed=_JOB_SEED)

        valid_product_codes = {"product_code_A", "product_code_B", "product_code_C"}
        valid_categories = {"category_A", "category_B", "category_C"}
        for val in result["product_code"]:
            assert val in valid_product_codes, f"Unexpected product_code: {val!r}"
        for val in result["category"]:
            assert val in valid_categories, f"Unexpected category: {val!r}"

    def test_customer_table_nonexistent_file_raises(self):
        """customer:/nonexistent.parquet must raise PlanCompileError."""
        with pytest.raises(PlanCompileError, match="not found|customer"):
            validate_joint_mask_config(
                {
                    "columns": ["a", "b"],
                    "reference": "customer:/nonexistent/path/table.parquet",
                    "key_by": "id",
                }
            )

    def test_customer_same_key_same_output(self, tmp_path):
        """Same key_by value -> same reference row (deterministic)."""
        parquet_path = _make_customer_table(tmp_path, ["code", "desc"])
        ref_str = f"customer:{parquet_path}"

        df = pd.DataFrame(
            {
                "entity_id": ["E001", "E002", "E001"],  # first and last are same
                "code": ["x", "x", "x"],
                "desc": ["x", "x", "x"],
            }
        )
        cfg = JointMaskConfig.from_dict(
            {"columns": ["code", "desc"], "reference": ref_str, "key_by": "entity_id"}
        )
        result = apply_joint_mask(df, cfg, mode="mask", job_seed=_JOB_SEED)
        assert result["code"].iloc[0] == result["code"].iloc[2], (
            "Same key_by value must produce same output code."
        )
        assert result["desc"].iloc[0] == result["desc"].iloc[2], (
            "Same key_by value must produce same output desc."
        )


# ── J2.4: Cross-domain: multiple joint_masks together ─────────────────────────


class TestCrossDomainJointMask:
    """Multiple joint_masks in one fixture work independently."""

    def test_zip_and_mcc_together(self):
        """Fixture with both address and MCC joint_masks applies correctly."""
        from decoy_engine.reference_tables import load_table

        zip_cols = ["zip", "city", "state"]

        mcc_tbl = load_table("mcc_category_description")
        mcc_cols = [c for c in mcc_tbl.column_names if c != "id"]

        n = 3
        df = pd.DataFrame(
            {
                "patient_id": [f"P{i}" for i in range(n)],
                "merchant_id": [f"M{i}" for i in range(n)],
                **{c: ["x"] * n for c in zip_cols},
                **{c: ["x"] * n for c in mcc_cols},
            }
        )

        cfg_zip = JointMaskConfig.from_dict(
            {"columns": zip_cols, "reference": "us_zip5_city_state", "key_by": "patient_id"}
        )
        cfg_mcc = JointMaskConfig.from_dict(
            {"columns": mcc_cols, "reference": "mcc_category_description", "key_by": "merchant_id"}
        )
        result = apply_joint_mask(df, cfg_zip, mode="mask", job_seed=_JOB_SEED)
        result = apply_joint_mask(result, cfg_mcc, mode="mask", job_seed=_JOB_SEED)

        # Both sets of columns must be replaced.
        for col in zip_cols:
            assert any(v != "x" for v in result[col]), f"Column {col!r} not replaced."
        for col in mcc_cols:
            assert any(v != "x" for v in result[col]), f"Column {col!r} not replaced."


# ── J2.5: Integration through plan/run path ───────────────────────────────────


class TestJointMaskSP08bIntegration:
    """STRATEGY-WIRING GUARD: joint_mask with NDC/MCC through PandasExecutionAdapter."""

    def test_mcc_joint_mask_through_adapter(self):
        """MCC joint_mask through the real plan/run path returns valid MCC rows."""
        from decoy_engine.reference_tables import load_table

        mcc_tbl = load_table("mcc_category_description")
        mcc_cols = [c for c in mcc_tbl.column_names if c != "id"]

        from decoy_engine.execution import PandasExecutionAdapter
        from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
        from decoy_engine.providers_v2 import get_default_registry
        from decoy_engine.relationships._graph import RelationshipGraph
        from decoy_engine.relationships._namespace import NamespaceRegistry

        # joint_mask is a scalar handler; use the first mcc col as the plan column.
        primary_col = mcc_cols[0]
        col_seed = ColumnSeed(
            namespace=None,
            strategy="joint_mask",
            provider="joint_mask",
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=False,
            provider_config=(
                ("columns", mcc_cols),
                ("reference", "mcc_category_description"),
                ("key_by", "merchant_id"),
                ("mode", "mask"),
            ),
            coherent_with=(),
        )
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=b"\x11" * 8,
                per_table=(("t", TableSeed(per_column=((primary_col, col_seed),), per_group=())),),
            )
        )
        source_data: dict[str, list] = {"merchant_id": ["M001", "M002", "M003"]}
        for col in mcc_cols:
            source_data[col] = ["placeholder"] * 3
        src = pa.table(source_data)

        result = PandasExecutionAdapter().run_single(
            plan,
            src,
            registry=get_default_registry(),
            relationship_graph=RelationshipGraph(edges=(), ordering=()),
            namespace_registry=NamespaceRegistry(bindings=()),
        )
        out_col = result.output.column(primary_col).to_pylist()
        assert all(v != "placeholder" for v in out_col), (
            f"MCC joint_mask must replace all placeholder values, got {out_col!r}"
        )
