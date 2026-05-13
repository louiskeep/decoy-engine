"""Graph-level integration tests for source.s3 and target.s3 ops.

Tests the graph execution path (run_graph / validate_graph) using moto's
mock_aws, so no real AWS credentials or network are needed. The connector
SDK layer is covered separately in tests/connectors/test_s3.py; these
tests focus on the op contract as seen through the graph runner:

  - source.s3 reads CSV/Parquet from a mocked bucket and feeds downstream ops
  - target.s3 writes graph output back to the mocked bucket
  - Round-trip: source.s3 -> transform -> target.s3, verify object content
  - Validation errors for missing required config fields
  - Reference-pipeline shape: source.s3 -> mask -> target.s3
"""
from __future__ import annotations

import io
import os
import tempfile

import pandas as pd
import pytest
import yaml

from decoy_engine import run_graph, validate_graph
from decoy_engine.exceptions import PipelineValidationError

boto3 = pytest.importorskip("boto3")
mock_aws = pytest.importorskip("moto").mock_aws

BUCKET = "test-pipeline-bucket"
REGION = "us-east-1"
_S3_CREDS = {
    "region": REGION,
    "access_key_id": "test-key",
    "secret_access_key": "test-secret",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _yaml(d):
    return yaml.safe_dump(d)


def _input_df() -> pd.DataFrame:
    return pd.DataFrame({
        "id": [1, 2, 3, 4, 5],
        "name": ["Alice", "Bob", "Carol", "Dave", "Eve"],
        "score": [90, 75, 85, 60, 95],
        "dept": ["eng", "sales", "eng", "hr", "eng"],
    })


def _seed_csv(client, key: str, df: pd.DataFrame) -> None:
    buf = io.BytesIO()
    df.to_csv(buf, index=False)
    client.put_object(Bucket=BUCKET, Key=key, Body=buf.getvalue())


def _read_s3_csv(client, key: str) -> pd.DataFrame:
    body = client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    return pd.read_csv(io.BytesIO(body))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def work_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def input_csv(work_dir):
    """Five-row CSV on local disk (used for source.file nodes)."""
    path = os.path.join(work_dir, "input.csv")
    _input_df().to_csv(path, index=False)
    return path


@pytest.fixture
def aws_mocked():
    with mock_aws():
        yield


@pytest.fixture
def boto_client(aws_mocked):
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(Bucket=BUCKET)
    return client


# ---------------------------------------------------------------------------
# source.s3
# ---------------------------------------------------------------------------

class TestSourceS3:
    """source.s3 graph op — reads from a mocked S3 bucket."""

    def test_reads_csv_fully_into_pipeline(self, boto_client, work_dir):
        """A CSV object in S3 flows through the graph with all rows + columns."""
        input_key = "input/data.csv"
        output_path = os.path.join(work_dir, "out.csv")
        _seed_csv(boto_client, input_key, _input_df())

        cfg = _yaml({
            "mode": "graph",
            "nodes": [
                {"id": "src", "kind": "source.s3", "config": {
                    "bucket": BUCKET, "path": input_key, **_S3_CREDS,
                }},
                {"id": "tgt", "kind": "target.file",
                 "config": {"output_filename": output_path}},
            ],
            "edges": [{"from": "src", "to": "tgt"}],
        })
        validate_graph(cfg)
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(output_path)
        assert len(out) == 5
        assert set(out.columns) == {"id", "name", "score", "dept"}

    def test_downstream_transform_sees_s3_rows(self, boto_client, work_dir):
        """source.s3 feeds a filter op; only matching rows reach the target."""
        input_key = "input/filter_test.csv"
        output_path = os.path.join(work_dir, "out.csv")
        _seed_csv(boto_client, input_key, _input_df())

        cfg = _yaml({
            "mode": "graph",
            "nodes": [
                {"id": "src", "kind": "source.s3", "config": {
                    "bucket": BUCKET, "path": input_key, **_S3_CREDS,
                }},
                {"id": "filt", "kind": "filter",
                 "config": {"predicate": "score > 80"}},
                {"id": "tgt", "kind": "target.file",
                 "config": {"output_filename": output_path}},
            ],
            "edges": [{"from": "src", "to": "filt"}, {"from": "filt", "to": "tgt"}],
        })
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(output_path)
        assert len(out) == 3  # Alice (90), Carol (85), Eve (95)
        assert (out["score"] > 80).all()

    def test_drop_column_after_source_s3(self, boto_client, work_dir):
        """Column-drop transform works on data originating from S3."""
        input_key = "input/drop_test.csv"
        output_path = os.path.join(work_dir, "out.csv")
        _seed_csv(boto_client, input_key, _input_df())

        cfg = _yaml({
            "mode": "graph",
            "nodes": [
                {"id": "src", "kind": "source.s3", "config": {
                    "bucket": BUCKET, "path": input_key, **_S3_CREDS,
                }},
                {"id": "drop", "kind": "drop_column",
                 "config": {"columns": ["dept", "score"]}},
                {"id": "tgt", "kind": "target.file",
                 "config": {"output_filename": output_path}},
            ],
            "edges": [{"from": "src", "to": "drop"}, {"from": "drop", "to": "tgt"}],
        })
        result = run_graph(cfg)
        assert result["success"], result
        out = pd.read_csv(output_path)
        assert "dept" not in out.columns
        assert "score" not in out.columns
        assert {"id", "name"}.issubset(out.columns)
        assert len(out) == 5

    def test_missing_key_is_runtime_not_validation_error(self, boto_client, work_dir):
        """A non-existent S3 key can't be detected at validate time; fails at run."""
        output_path = os.path.join(work_dir, "out.csv")
        cfg = _yaml({
            "mode": "graph",
            "nodes": [
                {"id": "src", "kind": "source.s3", "config": {
                    "bucket": BUCKET, "path": "never/exists.csv", **_S3_CREDS,
                }},
                {"id": "tgt", "kind": "target.file",
                 "config": {"output_filename": output_path}},
            ],
            "edges": [{"from": "src", "to": "tgt"}],
        })
        validate_graph(cfg)  # key existence is a run-time concern
        result = run_graph(cfg)
        assert result["success"] is False
        failed = next(n for n in result["nodes"] if n["status"] == "error")
        assert failed["node_id"] == "src"

    @pytest.mark.parametrize("bad_config", [
        {"path": "data.csv"},    # missing bucket
        {"bucket": BUCKET},      # missing path
        {},                       # missing both
    ])
    def test_invalid_config_fails_validation(self, bad_config):
        """Missing required fields caught at validate_graph time."""
        cfg = _yaml({
            "mode": "graph",
            "nodes": [{"id": "src", "kind": "source.s3", "config": bad_config}],
            "edges": [],
        })
        with pytest.raises(PipelineValidationError):
            validate_graph(cfg)


# ---------------------------------------------------------------------------
# target.s3
# ---------------------------------------------------------------------------

class TestTargetS3:
    """target.s3 graph op — writes graph output to a mocked S3 bucket."""

    def test_writes_csv_to_bucket(self, boto_client, input_csv):
        """target.s3 uploads a CSV; the object is readable with the right rows."""
        output_key = "output/written.csv"
        cfg = _yaml({
            "mode": "graph",
            "nodes": [
                {"id": "src", "kind": "source.file",
                 "config": {"path": input_csv}},
                {"id": "tgt", "kind": "target.s3", "config": {
                    "bucket": BUCKET, "path": output_key, **_S3_CREDS,
                }},
            ],
            "edges": [{"from": "src", "to": "tgt"}],
        })
        validate_graph(cfg)
        result = run_graph(cfg)
        assert result["success"], result
        out = _read_s3_csv(boto_client, output_key)
        assert len(out) == 5
        assert set(out.columns) == {"id", "name", "score", "dept"}

    def test_filtered_rows_land_in_s3(self, boto_client, input_csv):
        """Only rows that pass the upstream filter are written to S3."""
        output_key = "output/filtered.csv"
        cfg = _yaml({
            "mode": "graph",
            "nodes": [
                {"id": "src", "kind": "source.file",
                 "config": {"path": input_csv}},
                {"id": "filt", "kind": "filter",
                 "config": {"predicate": "dept == 'eng'"}},
                {"id": "tgt", "kind": "target.s3", "config": {
                    "bucket": BUCKET, "path": output_key, **_S3_CREDS,
                }},
            ],
            "edges": [{"from": "src", "to": "filt"}, {"from": "filt", "to": "tgt"}],
        })
        result = run_graph(cfg)
        assert result["success"], result
        out = _read_s3_csv(boto_client, output_key)
        assert len(out) == 3  # Alice, Carol, Eve are in eng
        assert (out["dept"] == "eng").all()

    @pytest.mark.parametrize("bad_config", [
        {"path": "data.csv"},    # missing bucket
        {"bucket": BUCKET},      # missing path
        {},                       # missing both
    ])
    def test_invalid_config_fails_validation(self, bad_config):
        """Missing required fields caught at validate_graph time."""
        cfg = _yaml({
            "mode": "graph",
            "nodes": [
                {"id": "src", "kind": "source.file",
                 "config": {"path": "/tmp/x.csv"}},
                {"id": "tgt", "kind": "target.s3", "config": bad_config},
            ],
            "edges": [{"from": "src", "to": "tgt"}],
        })
        with pytest.raises(PipelineValidationError):
            validate_graph(cfg)


# ---------------------------------------------------------------------------
# End-to-end round-trips
# ---------------------------------------------------------------------------

class TestS3RoundTrip:
    """source.s3 -> [transform] -> target.s3 round-trips."""

    def test_passthrough_csv_roundtrip(self, boto_client):
        """Data read from S3 and written back to S3 preserves all rows/columns."""
        input_key = "roundtrip/input.csv"
        output_key = "roundtrip/output.csv"
        _seed_csv(boto_client, input_key, _input_df())

        cfg = _yaml({
            "mode": "graph",
            "nodes": [
                {"id": "src", "kind": "source.s3", "config": {
                    "bucket": BUCKET, "path": input_key, **_S3_CREDS,
                }},
                {"id": "tgt", "kind": "target.s3", "config": {
                    "bucket": BUCKET, "path": output_key, **_S3_CREDS,
                }},
            ],
            "edges": [{"from": "src", "to": "tgt"}],
        })
        result = run_graph(cfg)
        assert result["success"], result
        out = _read_s3_csv(boto_client, output_key)
        assert len(out) == 5
        assert list(out.columns) == ["id", "name", "score", "dept"]

    def test_reference_pipeline_shape_source_mask_target_s3(self, boto_client):
        """Reference-pipeline shape (Item 24): source.s3 -> mask -> target.s3.

        Mirrors the sprint G reference pipeline in templates/upload_csv_mask_to_s3.yaml.
        Verifies that the masking transform applies and the masked data lands in S3.
        """
        input_key = "ref/input.csv"
        output_key = "ref/output.csv"
        _seed_csv(boto_client, input_key, _input_df())

        cfg = _yaml({
            "mode": "graph",
            "nodes": [
                {"id": "src", "kind": "source.s3", "config": {
                    "bucket": BUCKET, "path": input_key, **_S3_CREDS,
                }},
                {"id": "msk", "kind": "mask", "config": {
                    "columns": {
                        "name": {"strategy": "redact", "redact_with": "MASKED"},
                    },
                }},
                {"id": "tgt", "kind": "target.s3", "config": {
                    "bucket": BUCKET, "path": output_key, **_S3_CREDS,
                }},
            ],
            "edges": [{"from": "src", "to": "msk"}, {"from": "msk", "to": "tgt"}],
        })
        result = run_graph(cfg)
        assert result["success"], result
        out = _read_s3_csv(boto_client, output_key)
        assert len(out) == 5
        assert (out["name"] == "MASKED").all()   # masking applied
        assert (out["score"] > 0).all()           # non-masked column unchanged
        assert set(out["dept"]).issubset({"eng", "sales", "hr"})  # dept preserved

    def test_sql_run_between_s3_source_and_target(self, boto_client):
        """sql_run (DuckDB) can be chained between S3 source and target."""
        input_key = "sql/input.csv"
        output_key = "sql/output.csv"
        _seed_csv(boto_client, input_key, _input_df())

        cfg = _yaml({
            "mode": "graph",
            "nodes": [
                {"id": "src", "kind": "source.s3", "config": {
                    "bucket": BUCKET, "path": input_key, **_S3_CREDS,
                }},
                {"id": "sql", "kind": "sql_run", "config": {
                    "sql": "SELECT dept, COUNT(*) AS n FROM df GROUP BY dept ORDER BY dept",
                }},
                {"id": "tgt", "kind": "target.s3", "config": {
                    "bucket": BUCKET, "path": output_key, **_S3_CREDS,
                }},
            ],
            "edges": [{"from": "src", "to": "sql"}, {"from": "sql", "to": "tgt"}],
        })
        result = run_graph(cfg)
        assert result["success"], result
        out = _read_s3_csv(boto_client, output_key)
        # 3 distinct depts: eng, hr, sales
        assert len(out) == 3
        assert set(out.columns) == {"dept", "n"}
        eng_row = out[out["dept"] == "eng"].iloc[0]
        assert eng_row["n"] == 3  # Alice, Carol, Eve
