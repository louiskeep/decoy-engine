"""FC-1 F2 (2026-06-02): tests for the unified `run_pipeline` entry.

`run_pipeline` is the V2 spine the platform job runner + the CLI both
call when the operator submits a mixed mask + generate config. Pre-FC-1
the engine ran mask and generate through two separate top-level
entries (`PandasExecutionAdapter.run` and `generate_tables`); FC-1
unifies them so one job can mask some tables and generate others.

These cells cover the three legitimate shapes (pure-mask, pure-generate,
mixed) plus the per-table-kind classification helper.
"""

from __future__ import annotations

import json

import pandas as pd
import pyarrow as pa

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import (
    ExecutionResult,
    classify_table_kinds,
    run_pipeline,
)

_ENGINE_VERSION = "fc-1-test"


def _faker_col(name: str, namespace: str) -> dict:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": namespace,
    }


def _generate_col(name: str, type_: str = "sequence", **params) -> dict:
    return {"name": name, "type": type_, **params}


def _validated_dump(cfg: dict) -> dict:
    """The contract: caller pre-validates; engine consumes the dump."""
    return PipelineConfig.model_validate(cfg).model_dump()


# --------------------------------------------------------------------------
# classify_table_kinds helper
# --------------------------------------------------------------------------


class TestClassifyTableKinds:
    def test_mask_only_config_classifies_every_table_as_mask(self):
        cfg = {
            "tables": [
                {"name": "customers", "columns": [_faker_col("email", "ns_a")]},
                {"name": "orders", "columns": [_faker_col("customer_id", "ns_a")]},
            ]
        }
        assert classify_table_kinds(cfg) == {"customers": "mask", "orders": "mask"}

    def test_generate_only_config_classifies_every_table_as_generate(self):
        cfg = {
            "tables": [
                {
                    "name": "employees",
                    "row_count": 5,
                    "generate_columns": [_generate_col("id", start=1)],
                },
            ]
        }
        assert classify_table_kinds(cfg) == {"employees": "generate"}

    def test_mixed_config_classifies_per_table(self):
        cfg = {
            "tables": [
                {"name": "customers", "columns": [_faker_col("email", "ns_a")]},
                {
                    "name": "employees",
                    "row_count": 5,
                    "generate_columns": [_generate_col("id", start=1)],
                },
            ]
        }
        assert classify_table_kinds(cfg) == {
            "customers": "mask",
            "employees": "generate",
        }


# --------------------------------------------------------------------------
# run_pipeline: pure-generate
# --------------------------------------------------------------------------


def _pure_generate_config() -> dict:
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {},
            "tables": [
                {
                    "name": "employees",
                    "row_count": 5,
                    "generate_columns": [
                        {"name": "employee_id", "type": "sequence", "start": 1000, "step": 1},
                    ],
                },
            ],
            "targets": {"employees": {"type": "file", "format": "csv", "path": "out.csv"}},
        }
    )


class TestRunPipelinePureGenerate:
    def test_generates_declared_rows(self):
        cfg = _pure_generate_config()
        result = run_pipeline(cfg, engine_version=_ENGINE_VERSION)
        assert isinstance(result, ExecutionResult)
        assert "employees" in result.outputs
        assert result.outputs["employees"].num_rows == 5

    def test_table_kinds_marks_generate(self):
        cfg = _pure_generate_config()
        result = run_pipeline(cfg, engine_version=_ENGINE_VERSION)
        assert result.table_kinds == {"employees": "generate"}

    def test_sources_argument_ignored_when_no_mask_tables(self):
        cfg = _pure_generate_config()
        result = run_pipeline(cfg, sources={}, engine_version=_ENGINE_VERSION)
        assert result.outputs["employees"].num_rows == 5


# --------------------------------------------------------------------------
# run_pipeline: pure-mask (FC-1 invariant: existing pure-mask path unchanged)
# --------------------------------------------------------------------------


def _pure_mask_config(tmp_path) -> dict:
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "customers.csv"),
                },
            },
            "tables": [
                {
                    "name": "customers",
                    "columns": [_faker_col("email", "customer_identity")],
                },
            ],
            "targets": {
                "customers": {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")},
            },
        }
    )


def _customers_source(tmp_path) -> dict[str, pa.Table]:
    """Write the source CSV to disk (so profile_source can read it) AND
    return the Arrow dict the adapter consumes."""
    df = pd.DataFrame({"email": ["a@x.com", "b@x.com", "c@x.com"]})
    df.to_csv(tmp_path / "customers.csv", index=False)
    return {"customers": pa.Table.from_pandas(df, preserve_index=False)}


class TestRunPipelinePureMask:
    def test_masks_declared_table(self, tmp_path):
        cfg = _pure_mask_config(tmp_path)
        sources = _customers_source(tmp_path)
        result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        assert "customers" in result.outputs
        assert result.outputs["customers"].num_rows == 3
        masked_emails = result.outputs["customers"].column("email").to_pylist()
        # Faker mask replaces every value.
        assert masked_emails != ["a@x.com", "b@x.com", "c@x.com"]

    def test_table_kinds_marks_mask(self, tmp_path):
        cfg = _pure_mask_config(tmp_path)
        sources = _customers_source(tmp_path)
        result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        assert result.table_kinds == {"customers": "mask"}


# --------------------------------------------------------------------------
# run_pipeline: mixed (the FC-1 motivating case)
# --------------------------------------------------------------------------


def _mixed_config(tmp_path) -> dict:
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "customers.csv"),
                },
            },
            "tables": [
                # Generate-kind: synthesizes new rows.
                {
                    "name": "employees",
                    "row_count": 5,
                    "generate_columns": [
                        {"name": "employee_id", "type": "sequence", "start": 1000, "step": 1},
                    ],
                },
                # Mask-kind: transforms the customers source.
                {
                    "name": "customers",
                    "columns": [_faker_col("email", "customer_identity")],
                },
            ],
            "targets": {
                "employees": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "employees.csv"),
                },
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "customers_out.csv"),
                },
            },
        }
    )


class TestRunPipelineMixed:
    def test_outputs_carry_both_kinds(self, tmp_path):
        cfg = _mixed_config(tmp_path)
        sources = _customers_source(tmp_path)
        result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        assert "employees" in result.outputs, "generate table missing from outputs"
        assert "customers" in result.outputs, "mask table missing from outputs"

    def test_employees_has_declared_row_count(self, tmp_path):
        cfg = _mixed_config(tmp_path)
        sources = _customers_source(tmp_path)
        result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        assert result.outputs["employees"].num_rows == 5

    def test_customers_preserves_source_row_count(self, tmp_path):
        cfg = _mixed_config(tmp_path)
        sources = _customers_source(tmp_path)
        result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        assert result.outputs["customers"].num_rows == 3

    def test_customers_email_is_masked(self, tmp_path):
        cfg = _mixed_config(tmp_path)
        sources = _customers_source(tmp_path)
        result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        masked = result.outputs["customers"].column("email").to_pylist()
        assert masked != ["a@x.com", "b@x.com", "c@x.com"]

    def test_table_kinds_dict_stamps_each_table(self, tmp_path):
        cfg = _mixed_config(tmp_path)
        sources = _customers_source(tmp_path)
        result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        assert result.table_kinds == {"employees": "generate", "customers": "mask"}


# --------------------------------------------------------------------------
# Determinism (re-run byte equality)
# --------------------------------------------------------------------------


class TestRunPipelineDeterminism:
    def test_pure_generate_two_runs_byte_equal(self):
        cfg = _pure_generate_config()
        r1 = run_pipeline(cfg, engine_version=_ENGINE_VERSION)
        r2 = run_pipeline(cfg, engine_version=_ENGINE_VERSION)
        assert r1.outputs["employees"].to_pydict() == r2.outputs["employees"].to_pydict()

    def test_mixed_two_runs_byte_equal(self, tmp_path):
        cfg = _mixed_config(tmp_path)
        sources = _customers_source(tmp_path)
        r1 = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        r2 = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        for name in r1.outputs:
            assert r1.outputs[name].to_pydict() == r2.outputs[name].to_pydict(), (
                f"{name} drifted across runs"
            )


# --------------------------------------------------------------------------
# BF1 (2026-06-26): opt-in distribution-fidelity surfacing
# --------------------------------------------------------------------------

_PINNED_NOW = "2026-06-26T00:00:00+00:00"

# A categorical value present in the source data. The fidelity report
# compares its distribution but must NEVER embed the label itself.
_SECRET_CATEGORY_VALUES = ("gold", "silver", "bronze")
_SECRET_EMAIL_VALUES = ("alice@x.com", "bob@x.com", "carol@x.com")


def _fidelity_mask_config(tmp_path) -> dict:
    """Mask `email` (faker) and pass `plan` through, so the output keeps
    a real categorical column the snapshot can capture labels for."""
    return _validated_dump(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "customers": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "customers.csv"),
                },
            },
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        _faker_col("email", "customer_identity"),
                        {"name": "plan", "strategy": "passthrough"},
                    ],
                },
            ],
            "targets": {
                "customers": {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")},
            },
        }
    )


def _fidelity_source(tmp_path) -> dict[str, pa.Table]:
    df = pd.DataFrame(
        {
            "email": [*_SECRET_EMAIL_VALUES, "dave@x.com", "erin@x.com"],
            "plan": ["gold", "silver", "gold", "bronze", "silver"],
        }
    )
    df.to_csv(tmp_path / "customers.csv", index=False)
    return {"customers": pa.Table.from_pandas(df, preserve_index=False)}


class TestRunPipelineFidelityReportFlag:
    def test_flag_off_attaches_no_fidelity_report(self, tmp_path):
        """Default-OFF: zero ripple. quality_metrics carries no report key."""
        cfg = _fidelity_mask_config(tmp_path)
        sources = _fidelity_source(tmp_path)
        result = run_pipeline(cfg, sources=sources, engine_version=_ENGINE_VERSION)
        assert "fidelity_reports" not in result.quality_metrics

    def test_flag_on_attaches_report_per_mask_table(self, tmp_path):
        cfg = _fidelity_mask_config(tmp_path)
        sources = _fidelity_source(tmp_path)
        result = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            fidelity_report=True,
            now_iso=_PINNED_NOW,
        )
        reports = result.quality_metrics["fidelity_reports"]
        assert set(reports) == {"customers"}
        report = reports["customers"]
        assert report["schema_version"] == "quality-report/v1"
        assert report["generated_at"] == _PINNED_NOW
        assert "grade" in report
        assert "overall_score" in report

    def test_report_only_low_score_does_not_fail_job(self, tmp_path):
        """A masked email column tanks fidelity; the job still succeeds."""
        cfg = _fidelity_mask_config(tmp_path)
        sources = _fidelity_source(tmp_path)
        result = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            fidelity_report=True,
            now_iso=_PINNED_NOW,
        )
        # Outputs are present and complete despite any low fidelity score.
        assert result.outputs["customers"].num_rows == 5
        assert isinstance(result.quality_metrics["fidelity_reports"]["customers"]["grade"], str)

    def test_generate_table_gets_no_fidelity_report(self, tmp_path):
        """First slice is mask-only; generate tables are skipped."""
        cfg = _mixed_config(tmp_path)
        sources = _customers_source(tmp_path)
        result = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            fidelity_report=True,
            now_iso=_PINNED_NOW,
        )
        reports = result.quality_metrics["fidelity_reports"]
        assert "customers" in reports  # mask table
        assert "employees" not in reports  # generate table skipped


class TestRunPipelineFidelityReportSecurity:
    """The one rule that must not regress: the emitted report is
    aggregate-only and label-free. Category labels / raw values live in
    the intermediate snapshots, which are consumed but never attached."""

    def test_report_contains_no_category_labels_or_raw_values(self, tmp_path):
        cfg = _fidelity_mask_config(tmp_path)
        sources = _fidelity_source(tmp_path)
        result = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            fidelity_report=True,
            now_iso=_PINNED_NOW,
        )
        report = result.quality_metrics["fidelity_reports"]["customers"]
        serialized = json.dumps(report)
        # The source genuinely contains these labels (so the snapshot
        # captured them); the assembled report must not echo any of them.
        for secret in (*_SECRET_CATEGORY_VALUES, *_SECRET_EMAIL_VALUES):
            assert secret not in serialized, f"raw value {secret!r} leaked into fidelity report"

    def test_report_is_json_serializable(self, tmp_path):
        cfg = _fidelity_mask_config(tmp_path)
        sources = _fidelity_source(tmp_path)
        result = run_pipeline(
            cfg,
            sources=sources,
            engine_version=_ENGINE_VERSION,
            fidelity_report=True,
            now_iso=_PINNED_NOW,
        )
        report = result.quality_metrics["fidelity_reports"]["customers"]
        # No custom encoder needed: the report must round-trip as plain JSON.
        assert json.loads(json.dumps(report)) == report
