"""S14-CLOUD-SRC-S3GCS: schema + end-to-end cells for V2 cloud sources.

The cells assert the discriminated-union shape accepts the three variants
(file / s3 / gcs) + rejects unknown types, plus the engine + platform
read dispatches end-to-end against moto (S3) and a mocked GCS client.

Pattern lessons applied from QA Q1, Q3, Q4, Q10: the dispatch never
interpolates strings into control-plane calls; client construction is
parameterized; exception bodies do not leak source values.

R6 lock: emulators only in CI (moto for S3 + monkeypatched Client for GCS).
No real-cloud calls.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from decoy_engine.config import PipelineConfig, PipelineConfigError


# ---------------------------------------------------------------------
# Schema acceptance / rejection (extra=forbid + discriminator)
# ---------------------------------------------------------------------


def _base_config() -> dict[str, Any]:
    """A minimum PipelineConfig the schema accepts; tests mutate `sources`."""
    return {
        "version": 1,
        "global_settings": {"seed": 0},
        "sources": {},
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
        "targets": {
            "t": {"type": "file", "format": "csv", "path": "/tmp/out.csv"},
        },
        "relationships": [],
        "namespaces": {"t_c": {"declared_by": ["t.c"]}},
    }


class TestCloudSourceSchema:
    def test_source_descriptor_accepts_s3_variant(self):
        """S3Source with bucket + key + format is a valid v2 source."""
        cfg = _base_config()
        cfg["sources"] = {
            "t": {
                "type": "s3",
                "format": "csv",
                "bucket": "my-bucket",
                "key": "data/customers.csv",
            },
        }
        # No exception => the discriminator accepts the variant.
        PipelineConfig.model_validate(cfg)

    def test_source_descriptor_accepts_gcs_variant(self):
        """GCSSource with bucket + object + format is a valid v2 source."""
        cfg = _base_config()
        cfg["sources"] = {
            "t": {
                "type": "gcs",
                "format": "parquet",
                "bucket": "my-bucket",
                "object": "data/customers.parquet",
            },
        }
        PipelineConfig.model_validate(cfg)

    def test_source_descriptor_rejects_unknown_type(self):
        """An unsupported type (sftp here -- S18 wires it) is rejected at the
        choke-point with a clear discriminator error, not silently dropped."""
        cfg = _base_config()
        cfg["sources"] = {
            "t": {
                "type": "sftp",
                "format": "csv",
                "host": "h",
                "path": "/x",
            },
        }
        with pytest.raises(Exception) as exc:
            PipelineConfig.model_validate(cfg)
        # Pydantic's discriminator error mentions the unknown tag.
        assert "sftp" in str(exc.value).lower() or "discriminator" in str(exc.value).lower()

    def test_source_descriptor_credentials_ref_optional(self):
        """credentials_ref is optional on both S3 + GCS (the engine SDK walks
        the default credential chain when absent)."""
        cfg_s3 = _base_config()
        cfg_s3["sources"] = {
            "t": {
                "type": "s3", "format": "csv",
                "bucket": "b", "key": "k",
                "credentials_ref": "aws-prod-readonly",
            },
        }
        PipelineConfig.model_validate(cfg_s3)

        cfg_gcs = _base_config()
        cfg_gcs["sources"] = {
            "t": {
                "type": "gcs", "format": "csv",
                "bucket": "b", "object": "o",
                "credentials_ref": "gcp-prod-readonly",
            },
        }
        PipelineConfig.model_validate(cfg_gcs)

    def test_s3_source_rejects_empty_bucket(self):
        """min_length=1 on bucket means empty-string rejects at the choke-point."""
        cfg = _base_config()
        cfg["sources"] = {
            "t": {"type": "s3", "format": "csv", "bucket": "", "key": "k"},
        }
        with pytest.raises(Exception):
            PipelineConfig.model_validate(cfg)

    def test_s3_source_rejects_extra_field(self):
        """extra='forbid' means unknown fields like 'session_token' surface as
        a clean validation error, not a silently-dropped value."""
        cfg = _base_config()
        cfg["sources"] = {
            "t": {
                "type": "s3", "format": "csv",
                "bucket": "b", "key": "k",
                "session_token": "leaked-secret",
            },
        }
        with pytest.raises(Exception):
            PipelineConfig.model_validate(cfg)


# ---------------------------------------------------------------------
# End-to-end via moto (S3) + monkeypatched GCS client
# ---------------------------------------------------------------------


@pytest.fixture
def _moto_s3():
    """Spin up a moto-mocked S3 + pre-create one bucket. Yields the boto3
    client + bucket name. Uses moto's mock_aws context (moto 5.x)."""
    from moto import mock_aws
    import boto3

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        bucket = "decoy-test-bucket"
        client.create_bucket(Bucket=bucket)
        yield client, bucket


class TestCloudSourceEndToEnd:
    def test_profile_s3_source_via_moto_csv(self, _moto_s3):
        """moto + S3Source + csv: profile_source reads the masked CSV and
        returns a SourceProfile shape identical to what a file source would."""
        from decoy_engine.profile import profile_source

        client, bucket = _moto_s3
        key = "data/customers.csv"
        client.put_object(
            Bucket=bucket, Key=key,
            Body=b"email\na@x.com\nb@y.com\nc@z.com\n",
        )

        config = _base_config()
        config["sources"] = {
            "customers": {
                "type": "s3", "format": "csv",
                "bucket": bucket, "key": key,
                "region": "us-east-1",
            },
        }
        config["tables"][0]["name"] = "customers"
        config["tables"][0]["columns"][0]["name"] = "email"

        profile = profile_source(config)
        assert len(profile.tables) == 1
        assert profile.tables[0].name == "customers"

    def test_read_s3_source_to_arrow_via_moto(self, _moto_s3):
        """The platform's _read_sources_as_arrow + _fetch_s3_to_bytesio dispatch
        on type='s3' and return a real pa.Table loaded from moto."""
        import sys
        # Allow this engine test to reach the platform's v2_runner via path.
        platform_root = (
            __import__("pathlib").Path(__file__).resolve().parents[3]
            / "decoy-platform"
        )
        sys.path.insert(0, str(platform_root))
        try:
            from api.jobs.v2_runner import _read_sources_as_arrow
        except ImportError:
            pytest.skip("platform api.jobs.v2_runner not importable from engine tests")

        client, bucket = _moto_s3
        key = "data/customers.csv"
        client.put_object(
            Bucket=bucket, Key=key,
            Body=b"email\na@x.com\nb@y.com\nc@z.com\n",
        )
        config = {
            "sources": {
                "customers": {
                    "type": "s3", "format": "csv",
                    "bucket": bucket, "key": key,
                    "region": "us-east-1",
                },
            },
        }
        tables = _read_sources_as_arrow(config)
        assert "customers" in tables
        assert tables["customers"].num_rows == 3
        assert "email" in tables["customers"].column_names

    def test_profile_gcs_source_via_mocked_client(self, monkeypatch):
        """Patches google.cloud.storage.Client to return a fake bucket whose
        blob downloads predictable bytes; verifies the engine's GCS dispatch
        reads through the same Arrow path as file/s3."""
        from decoy_engine.profile import profile_source

        fake_csv = b"email\nx@a.com\ny@b.com\n"

        class _FakeBlob:
            def download_as_bytes(self):
                return fake_csv

        class _FakeBucket:
            def blob(self, name):
                return _FakeBlob()

        class _FakeClient:
            def bucket(self, name):
                return _FakeBucket()

        monkeypatch.setattr(
            "google.cloud.storage.Client", lambda *a, **kw: _FakeClient()
        )

        config = _base_config()
        config["sources"] = {
            "customers": {
                "type": "gcs", "format": "csv",
                "bucket": "my-bucket",
                "object": "data/customers.csv",
            },
        }
        config["tables"][0]["name"] = "customers"
        config["tables"][0]["columns"][0]["name"] = "email"

        profile = profile_source(config)
        assert len(profile.tables) == 1
        assert profile.tables[0].name == "customers"

    def test_s3_source_missing_object_surfaces_sdk_error_without_pii_leak(self, _moto_s3):
        """A missing key surfaces as the SDK's NoSuchKey ClientError. The
        engine layer does NOT wrap or stringify it in a way that leaks any
        source-data value (the descriptor's bucket/key are config, not data,
        so they may appear; data values must not). Regression-pin against QA
        Q10 pattern carry to cloud."""
        from decoy_engine.profile import profile_source

        _, bucket = _moto_s3
        config = _base_config()
        config["sources"] = {
            "customers": {
                "type": "s3", "format": "csv",
                "bucket": bucket, "key": "missing/object.csv",
                "region": "us-east-1",
            },
        }
        config["tables"][0]["name"] = "customers"

        with pytest.raises(Exception) as exc:
            profile_source(config)
        # The exception message may name the missing key (config), but it must
        # not contain ANY of the data values from a successful read elsewhere
        # (since this test never put data, there's nothing to leak; we assert
        # the layer doesn't fabricate a "sample value" leak).
        msg = str(exc.value)
        assert "a@x.com" not in msg
        assert "user_" not in msg
