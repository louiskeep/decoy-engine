"""S15-CLOUD-TGT-S3GCS: schema cells for V2 cloud targets.

Schema acceptance + rejection at the PipelineConfig choke-point. The
write path (atomic-move pattern via moto / mocked GCS) is exercised on
the platform side in tests/test_v2_runner.py since _materialize_output
lives in api/jobs/v2_runner.py.

Symmetric with tests/unit/test_v2_cloud_sources.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from decoy_engine.config import PipelineConfig


def _base_config_with_target(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "global_settings": {"seed": 0},
        "sources": {
            "t": {"type": "file", "format": "csv", "path": "/tmp/in.csv"},
        },
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "c",
                        "strategy": "faker",
                        "provider": "person_email",
                        "namespace": "t_c",
                        "deterministic": True,
                    },
                ],
            },
        ],
        "targets": {"t": target},
        "relationships": [],
        "namespaces": {"t_c": {"declared_by": ["t.c"]}},
    }


class TestCloudTargetSchema:
    def test_target_descriptor_accepts_s3_variant(self):
        cfg = _base_config_with_target({
            "type": "s3", "format": "csv",
            "bucket": "my-bucket", "key": "out/customers.csv",
        })
        PipelineConfig.model_validate(cfg)

    def test_target_descriptor_accepts_gcs_variant(self):
        cfg = _base_config_with_target({
            "type": "gcs", "format": "parquet",
            "bucket": "my-bucket", "object": "out/customers.parquet",
        })
        PipelineConfig.model_validate(cfg)

    def test_target_descriptor_rejects_unknown_type(self):
        cfg = _base_config_with_target({
            "type": "sftp", "format": "csv",
            "host": "h", "path": "/x",
        })
        with pytest.raises(Exception) as exc:
            PipelineConfig.model_validate(cfg)
        assert "sftp" in str(exc.value).lower() or "discriminator" in str(exc.value).lower()

    def test_s3_target_credentials_ref_optional(self):
        cfg_no_creds = _base_config_with_target({
            "type": "s3", "format": "csv", "bucket": "b", "key": "k",
        })
        PipelineConfig.model_validate(cfg_no_creds)

        cfg_with_creds = _base_config_with_target({
            "type": "s3", "format": "csv", "bucket": "b", "key": "k",
            "credentials_ref": "aws-prod-writeonly",
        })
        PipelineConfig.model_validate(cfg_with_creds)

    def test_gcs_target_credentials_ref_optional(self):
        cfg_no_creds = _base_config_with_target({
            "type": "gcs", "format": "csv", "bucket": "b", "object": "o",
        })
        PipelineConfig.model_validate(cfg_no_creds)

        cfg_with_creds = _base_config_with_target({
            "type": "gcs", "format": "csv", "bucket": "b", "object": "o",
            "credentials_ref": "gcp-prod-writeonly",
        })
        PipelineConfig.model_validate(cfg_with_creds)

    def test_s3_target_rejects_empty_bucket(self):
        cfg = _base_config_with_target({
            "type": "s3", "format": "csv", "bucket": "", "key": "k",
        })
        with pytest.raises(Exception):
            PipelineConfig.model_validate(cfg)

    def test_s3_target_rejects_extra_field(self):
        cfg = _base_config_with_target({
            "type": "s3", "format": "csv", "bucket": "b", "key": "k",
            "session_token": "leaked-secret",
        })
        with pytest.raises(Exception):
            PipelineConfig.model_validate(cfg)
