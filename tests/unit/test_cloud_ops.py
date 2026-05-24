"""Tests for cloud source/target graph ops (Item 63 — Sprint G).

All cloud connector calls are mocked so tests run without real S3 / GCS /
SFTP credentials. The pattern mirrors test_sql_run.py: exercise the op
module directly, skipping the graph runner.

Coverage:
  - Config validation: missing required fields, bad format, auth rules (SFTP).
  - source apply: mocked FileSource.open() yields CSV / Parquet bytes;
    result is pd.DataFrame (pandas engine) or pa.Table (duckdb engine);
    __preview_row_limit applied.
  - target apply: preview mode skips upload; normal mode calls sink.write();
    zero-row stub returned; SFTP close() called on error.
  - OPS registry: all 6 new kinds present with correct KIND / NATIVE_ENGINE /
    INPUT_ARITY / OUTPUT_KIND metadata.
"""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.graph.ops import (
    OPS,
    source_gcs,
    source_s3,
    source_sftp,
    target_gcs,
    target_s3,
    target_sftp,
)
from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

_CSV_BYTES = b"id,name,age\n1,alice,30\n2,bob,25\n3,carol,35\n"


def _parquet_bytes() -> bytes:
    table = pa.table({"id": [1, 2, 3], "name": ["alice", "bob", "carol"], "age": [30, 25, 35]})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def _mock_source(content: bytes) -> MagicMock:
    """FileSource mock whose open() yields content as one chunk."""
    src = MagicMock()
    src.open.return_value = iter([content])
    src.close = MagicMock()
    return src


def _mock_sink() -> MagicMock:
    """FileSink mock whose write() accepts (path, chunk_iterator)."""
    sink = MagicMock()
    sink.write.return_value = MagicMock()
    sink.close = MagicMock()
    return sink


# ---------------------------------------------------------------------------
# source.s3 — validation
# ---------------------------------------------------------------------------


class TestSourceS3Validation:
    def test_missing_bucket(self):
        with pytest.raises(ValidationError, match="bucket"):
            source_s3.validate_config({"path": "data/customers.csv"})

    def test_missing_path(self):
        with pytest.raises(ValidationError, match="path"):
            source_s3.validate_config({"bucket": "my-bucket"})

    def test_bad_format(self):
        with pytest.raises(ValidationError, match="format"):
            source_s3.validate_config({"bucket": "b", "path": "f.csv", "format": "excel"})

    def test_valid_minimal(self):
        source_s3.validate_config({"bucket": "b", "path": "data/file.csv"})

    def test_valid_parquet_inferred(self):
        source_s3.validate_config({"bucket": "b", "path": "data/file.parquet"})

    def test_valid_format_explicit(self):
        source_s3.validate_config({"bucket": "b", "path": "data/f", "format": "csv"})


# ---------------------------------------------------------------------------
# source.gcs — validation
# ---------------------------------------------------------------------------


class TestSourceGCSValidation:
    def test_missing_bucket(self):
        with pytest.raises(ValidationError, match="bucket"):
            source_gcs.validate_config({"path": "data.csv"})

    def test_missing_path(self):
        with pytest.raises(ValidationError, match="path"):
            source_gcs.validate_config({"bucket": "my-bucket"})

    def test_valid_minimal(self):
        source_gcs.validate_config({"bucket": "b", "path": "file.csv"})


# ---------------------------------------------------------------------------
# source.sftp — validation
# ---------------------------------------------------------------------------


class TestSourceSFTPValidation:
    def test_missing_host(self):
        with pytest.raises(ValidationError, match="host"):
            source_sftp.validate_config({"username": "u", "path": "/data/f.csv", "password": "p"})

    def test_missing_username(self):
        with pytest.raises(ValidationError, match="username"):
            source_sftp.validate_config({"host": "h", "path": "/data/f.csv", "password": "p"})

    def test_missing_path(self):
        with pytest.raises(ValidationError, match="path"):
            source_sftp.validate_config({"host": "h", "username": "u", "password": "p"})

    def test_missing_auth(self):
        with pytest.raises(ValidationError):
            source_sftp.validate_config({"host": "h", "username": "u", "path": "/f.csv"})

    def test_valid_with_password(self):
        source_sftp.validate_config(
            {"host": "h", "username": "u", "path": "/f.csv", "password": "p"}
        )

    def test_valid_with_private_key(self):
        source_sftp.validate_config(
            {"host": "h", "username": "u", "path": "/f.csv", "private_key": "---PEM---"}
        )


# ---------------------------------------------------------------------------
# target.s3 — validation
# ---------------------------------------------------------------------------


class TestTargetS3Validation:
    def test_missing_bucket(self):
        with pytest.raises(ValidationError, match="bucket"):
            target_s3.validate_config({"path": "out.csv"})

    def test_missing_path(self):
        with pytest.raises(ValidationError, match="path"):
            target_s3.validate_config({"bucket": "b"})

    def test_valid(self):
        target_s3.validate_config({"bucket": "b", "path": "output/data.csv"})


# ---------------------------------------------------------------------------
# target.gcs — validation
# ---------------------------------------------------------------------------


class TestTargetGCSValidation:
    def test_missing_bucket(self):
        with pytest.raises(ValidationError, match="bucket"):
            target_gcs.validate_config({"path": "out.csv"})

    def test_missing_path(self):
        with pytest.raises(ValidationError, match="path"):
            target_gcs.validate_config({"bucket": "b"})

    def test_valid(self):
        target_gcs.validate_config({"bucket": "b", "path": "out.parquet"})


# ---------------------------------------------------------------------------
# target.sftp — validation
# ---------------------------------------------------------------------------


class TestTargetSFTPValidation:
    def test_missing_host(self):
        with pytest.raises(ValidationError, match="host"):
            target_sftp.validate_config({"username": "u", "path": "/out.csv", "password": "p"})

    def test_missing_path(self):
        with pytest.raises(ValidationError, match="path"):
            target_sftp.validate_config({"host": "h", "username": "u", "password": "p"})

    def test_missing_auth(self):
        with pytest.raises(ValidationError):
            target_sftp.validate_config({"host": "h", "username": "u", "path": "/f.csv"})

    def test_valid(self):
        target_sftp.validate_config(
            {"host": "h", "username": "u", "path": "/out.csv", "password": "p"}
        )


# ---------------------------------------------------------------------------
# source.s3 apply — mocked connector
# ---------------------------------------------------------------------------


class TestSourceS3Apply:
    def test_reads_csv_pandas(self):
        mock_src = _mock_source(_CSV_BYTES)
        with (
            patch("decoy_engine.connectors.s3.S3Config"),
            patch("decoy_engine.connectors.s3.S3FileSource", return_value=mock_src),
        ):
            result = source_s3.apply(
                inputs=[],
                config={"bucket": "b", "path": "data/customers.csv", "__engine": "pandas"},
                ctx=None,
            )
        assert isinstance(result, pd.DataFrame)
        assert list(result.columns) == ["id", "name", "age"]
        assert len(result) == 3

    def test_reads_csv_duckdb(self):
        mock_src = _mock_source(_CSV_BYTES)
        with (
            patch("decoy_engine.connectors.s3.S3Config"),
            patch("decoy_engine.connectors.s3.S3FileSource", return_value=mock_src),
        ):
            result = source_s3.apply(
                inputs=[],
                config={"bucket": "b", "path": "data/customers.csv", "__engine": "duckdb"},
                ctx=None,
            )
        assert isinstance(result, pa.Table)
        assert result.num_rows == 3
        assert result.column_names == ["id", "name", "age"]

    def test_reads_parquet_pandas(self):
        mock_src = _mock_source(_parquet_bytes())
        with (
            patch("decoy_engine.connectors.s3.S3Config"),
            patch("decoy_engine.connectors.s3.S3FileSource", return_value=mock_src),
        ):
            result = source_s3.apply(
                inputs=[],
                config={"bucket": "b", "path": "data/customers.parquet", "__engine": "pandas"},
                ctx=None,
            )
        assert len(result) == 3

    def test_preview_row_limit(self):
        mock_src = _mock_source(_CSV_BYTES)
        with (
            patch("decoy_engine.connectors.s3.S3Config"),
            patch("decoy_engine.connectors.s3.S3FileSource", return_value=mock_src),
        ):
            result = source_s3.apply(
                inputs=[],
                config={
                    "bucket": "b",
                    "path": "data/customers.csv",
                    "__engine": "pandas",
                    "__preview_row_limit": 2,
                },
                ctx=None,
            )
        assert len(result) == 2

    def test_open_called_with_path(self):
        mock_src = _mock_source(_CSV_BYTES)
        with (
            patch("decoy_engine.connectors.s3.S3Config"),
            patch("decoy_engine.connectors.s3.S3FileSource", return_value=mock_src),
        ):
            source_s3.apply(
                inputs=[],
                config={"bucket": "b", "path": "some/key.csv", "__engine": "pandas"},
                ctx=None,
            )
        mock_src.open.assert_called_once_with("some/key.csv")


# ---------------------------------------------------------------------------
# source.gcs apply — mocked connector
# ---------------------------------------------------------------------------


class TestSourceGCSApply:
    def test_reads_csv_pandas(self):
        mock_src = _mock_source(_CSV_BYTES)
        with (
            patch("decoy_engine.connectors.gcs.GCSConfig"),
            patch("decoy_engine.connectors.gcs.GCSFileSource", return_value=mock_src),
        ):
            result = source_gcs.apply(
                inputs=[],
                config={"bucket": "b", "path": "data.csv", "__engine": "pandas"},
                ctx=None,
            )
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 3

    def test_reads_csv_duckdb(self):
        mock_src = _mock_source(_CSV_BYTES)
        with (
            patch("decoy_engine.connectors.gcs.GCSConfig"),
            patch("decoy_engine.connectors.gcs.GCSFileSource", return_value=mock_src),
        ):
            result = source_gcs.apply(
                inputs=[],
                config={"bucket": "b", "path": "data.csv", "__engine": "duckdb"},
                ctx=None,
            )
        assert isinstance(result, pa.Table)
        assert result.num_rows == 3


# ---------------------------------------------------------------------------
# source.sftp apply — mocked connector
# ---------------------------------------------------------------------------


class TestSourceSFTPApply:
    _BASE_CFG = {
        "host": "h",
        "username": "u",
        "password": "p",
        "path": "/data.csv",
        "__engine": "pandas",
    }

    def test_reads_csv_pandas(self):
        mock_src = _mock_source(_CSV_BYTES)
        with (
            patch("decoy_engine.connectors.sftp.SFTPConfig"),
            patch("decoy_engine.connectors.sftp.SFTPFileSource", return_value=mock_src),
        ):
            result = source_sftp.apply(inputs=[], config=self._BASE_CFG, ctx=None)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 3
        mock_src.close.assert_called_once()

    def test_close_called_on_error(self):
        mock_src = MagicMock()
        mock_src.open.side_effect = RuntimeError("network failure")
        mock_src.close = MagicMock()
        with (
            patch("decoy_engine.connectors.sftp.SFTPConfig"),
            patch("decoy_engine.connectors.sftp.SFTPFileSource", return_value=mock_src),
            pytest.raises(OpError, match="sftp"),
        ):
            source_sftp.apply(inputs=[], config=self._BASE_CFG, ctx=None)
        mock_src.close.assert_called_once()


# ---------------------------------------------------------------------------
# target.s3 apply — mocked connector
# ---------------------------------------------------------------------------


class TestTargetS3Apply:
    def _df(self) -> pd.DataFrame:
        return pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})

    def test_preview_skips_upload(self):
        df = self._df()
        with patch("decoy_engine.connectors.s3.S3FileSink") as MockSink:
            result = target_s3.apply(
                inputs=[df],
                config={
                    "bucket": "b",
                    "path": "out.csv",
                    "__preview_row_limit": 5,
                    "__engine": "pandas",
                },
                ctx=None,
            )
        MockSink.assert_not_called()
        assert result is df

    def test_uploads_csv_returns_empty_stub(self):
        df = self._df()
        mock_sink = _mock_sink()
        with (
            patch("decoy_engine.connectors.s3.S3Config"),
            patch("decoy_engine.connectors.s3.S3FileSink", return_value=mock_sink),
        ):
            result = target_s3.apply(
                inputs=[df],
                config={"bucket": "b", "path": "out.csv", "__engine": "pandas"},
                ctx=None,
            )
        mock_sink.write.assert_called_once()
        write_path = mock_sink.write.call_args[0][0]
        assert write_path == "out.csv"
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 0
        assert list(result.columns) == ["id", "name"]

    def test_uploads_parquet(self):
        df = self._df()
        mock_sink = _mock_sink()
        with (
            patch("decoy_engine.connectors.s3.S3Config"),
            patch("decoy_engine.connectors.s3.S3FileSink", return_value=mock_sink),
        ):
            target_s3.apply(
                inputs=[df],
                config={"bucket": "b", "path": "out.parquet", "__engine": "pandas"},
                ctx=None,
            )
        mock_sink.write.assert_called_once()


# ---------------------------------------------------------------------------
# target.gcs apply — mocked connector
# ---------------------------------------------------------------------------


class TestTargetGCSApply:
    def test_preview_skips_upload(self):
        df = pd.DataFrame({"id": [1]})
        with patch("decoy_engine.connectors.gcs.GCSFileSink") as MockSink:
            result = target_gcs.apply(
                inputs=[df],
                config={"bucket": "b", "path": "out.csv", "__preview_row_limit": 1},
                ctx=None,
            )
        MockSink.assert_not_called()
        assert result is df

    def test_uploads_csv(self):
        df = pd.DataFrame({"id": [1, 2]})
        mock_sink = _mock_sink()
        with (
            patch("decoy_engine.connectors.gcs.GCSConfig"),
            patch("decoy_engine.connectors.gcs.GCSFileSink", return_value=mock_sink),
        ):
            result = target_gcs.apply(
                inputs=[df],
                config={"bucket": "b", "path": "out.csv", "__engine": "pandas"},
                ctx=None,
            )
        mock_sink.write.assert_called_once()
        assert len(result) == 0


# ---------------------------------------------------------------------------
# target.sftp apply — mocked connector
# ---------------------------------------------------------------------------


class TestTargetSFTPApply:
    _BASE_CFG = {
        "host": "h",
        "username": "u",
        "password": "p",
        "path": "/out.csv",
        "__engine": "pandas",
    }

    def test_preview_skips_upload(self):
        df = pd.DataFrame({"id": [1]})
        with patch("decoy_engine.connectors.sftp.SFTPFileSink") as MockSink:
            result = target_sftp.apply(
                inputs=[df],
                config={**self._BASE_CFG, "__preview_row_limit": 1},
                ctx=None,
            )
        MockSink.assert_not_called()
        assert result is df

    def test_uploads_csv_and_closes(self):
        df = pd.DataFrame({"id": [1, 2]})
        mock_sink = _mock_sink()
        with (
            patch("decoy_engine.connectors.sftp.SFTPConfig"),
            patch("decoy_engine.connectors.sftp.SFTPFileSink", return_value=mock_sink),
        ):
            result = target_sftp.apply(inputs=[df], config=self._BASE_CFG, ctx=None)
        mock_sink.write.assert_called_once()
        mock_sink.close.assert_called_once()
        assert len(result) == 0

    def test_close_called_on_write_error(self):
        df = pd.DataFrame({"id": [1]})
        mock_sink = _mock_sink()
        mock_sink.write.side_effect = RuntimeError("SFTP write failed")
        with (
            patch("decoy_engine.connectors.sftp.SFTPConfig"),
            patch("decoy_engine.connectors.sftp.SFTPFileSink", return_value=mock_sink),
            pytest.raises((OpError, RuntimeError)),
        ):
            target_sftp.apply(inputs=[df], config=self._BASE_CFG, ctx=None)
        mock_sink.close.assert_called_once()


# ---------------------------------------------------------------------------
# OPS registry — all 6 new kinds
# ---------------------------------------------------------------------------


class TestRegistry:
    @pytest.mark.parametrize(
        "kind,module,arity,output_kind",
        [
            ("source.s3", source_s3, (0, 0), "stream"),
            ("source.gcs", source_gcs, (0, 0), "stream"),
            ("source.sftp", source_sftp, (0, 0), "stream"),
            ("target.s3", target_s3, (1, 1), "sink"),
            ("target.gcs", target_gcs, (1, 1), "sink"),
            ("target.sftp", target_sftp, (1, 1), "sink"),
        ],
    )
    def test_kind_registered_with_correct_metadata(self, kind, module, arity, output_kind):
        assert kind in OPS, f"{kind!r} not found in OPS"
        op = OPS[kind]
        assert kind == op.KIND
        assert op.NATIVE_ENGINE == "duckdb"
        assert arity == op.INPUT_ARITY
        assert output_kind == op.OUTPUT_KIND
