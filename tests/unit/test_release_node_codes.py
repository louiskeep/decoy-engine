"""R2.2: stable codes for release-visible source/target ops.

Every release-visible source/target op's validate_config now tags
its raises with a stable code from
:mod:`decoy_engine.validation_result.CODES` so the platform layer
can route the failure to the right inspector field by code rather
than parsing the message text.

This file covers target.file plus all six cloud ops (s3/gcs/sftp x
source/target). source.file is covered by
test_source_file_has_header / test_source_file_parsing_controls,
mask by test_mask_op_validation.
"""

from __future__ import annotations

import pytest

from decoy_engine import VALIDATION_CODES
from decoy_engine.errors import ValidationError
from decoy_engine.graph.ops import (
    source_gcs,
    source_s3,
    source_sftp,
    target_file,
    target_gcs,
    target_s3,
    target_sftp,
)


class TestTargetFileCodes:
    def test_missing_output_filename(self):
        with pytest.raises(ValidationError) as exc:
            target_file.validate_config({})
        assert exc.value.code == VALIDATION_CODES.TARGET_FILE_MISSING_OUTPUT_FILENAME

    def test_unsupported_format(self):
        with pytest.raises(ValidationError) as exc:
            target_file.validate_config(
                {
                    "output_filename": "out.weird",
                    "format": "weird",
                }
            )
        assert exc.value.code == VALIDATION_CODES.TARGET_FILE_UNSUPPORTED_FORMAT

    def test_happy_path_passes(self):
        target_file.validate_config(
            {
                "output_filename": "out.csv",
                "format": "csv",
            }
        )


class TestSourceS3Codes:
    def test_missing_bucket(self):
        with pytest.raises(ValidationError) as exc:
            source_s3.validate_config({"path": "x.csv"})
        assert exc.value.code == VALIDATION_CODES.SOURCE_S3_MISSING_BUCKET

    def test_missing_path(self):
        with pytest.raises(ValidationError) as exc:
            source_s3.validate_config({"bucket": "b"})
        assert exc.value.code == VALIDATION_CODES.SOURCE_S3_MISSING_PATH

    def test_unsupported_format_routes_through_cloud_io_code(self):
        with pytest.raises(ValidationError) as exc:
            source_s3.validate_config(
                {
                    "bucket": "b",
                    "path": "x.weird",
                    "format": "weird",
                }
            )
        assert exc.value.code == VALIDATION_CODES.CLOUD_IO_UNSUPPORTED_FORMAT


class TestTargetS3Codes:
    def test_missing_bucket(self):
        with pytest.raises(ValidationError) as exc:
            target_s3.validate_config({"path": "x.csv"})
        assert exc.value.code == VALIDATION_CODES.TARGET_S3_MISSING_BUCKET

    def test_missing_path(self):
        with pytest.raises(ValidationError) as exc:
            target_s3.validate_config({"bucket": "b"})
        assert exc.value.code == VALIDATION_CODES.TARGET_S3_MISSING_PATH


class TestSourceGcsCodes:
    def test_missing_bucket(self):
        with pytest.raises(ValidationError) as exc:
            source_gcs.validate_config({"path": "x.csv"})
        assert exc.value.code == VALIDATION_CODES.SOURCE_GCS_MISSING_BUCKET

    def test_missing_path(self):
        with pytest.raises(ValidationError) as exc:
            source_gcs.validate_config({"bucket": "b"})
        assert exc.value.code == VALIDATION_CODES.SOURCE_GCS_MISSING_PATH


class TestTargetGcsCodes:
    def test_missing_bucket(self):
        with pytest.raises(ValidationError) as exc:
            target_gcs.validate_config({"path": "x.csv"})
        assert exc.value.code == VALIDATION_CODES.TARGET_GCS_MISSING_BUCKET

    def test_missing_path(self):
        with pytest.raises(ValidationError) as exc:
            target_gcs.validate_config({"bucket": "b"})
        assert exc.value.code == VALIDATION_CODES.TARGET_GCS_MISSING_PATH


class TestSourceSftpCodes:
    def test_missing_host(self):
        with pytest.raises(ValidationError) as exc:
            source_sftp.validate_config({"username": "u", "path": "x", "password": "p"})
        assert exc.value.code == VALIDATION_CODES.SOURCE_SFTP_MISSING_HOST

    def test_missing_username(self):
        with pytest.raises(ValidationError) as exc:
            source_sftp.validate_config({"host": "h", "path": "x", "password": "p"})
        assert exc.value.code == VALIDATION_CODES.SOURCE_SFTP_MISSING_USERNAME

    def test_missing_path(self):
        with pytest.raises(ValidationError) as exc:
            source_sftp.validate_config({"host": "h", "username": "u", "password": "p"})
        assert exc.value.code == VALIDATION_CODES.SOURCE_SFTP_MISSING_PATH

    def test_missing_auth(self):
        with pytest.raises(ValidationError) as exc:
            source_sftp.validate_config(
                {
                    "host": "h",
                    "username": "u",
                    "path": "x.csv",
                }
            )
        assert exc.value.code == VALIDATION_CODES.SOURCE_SFTP_MISSING_AUTH

    def test_happy_path_with_password_passes(self):
        source_sftp.validate_config(
            {
                "host": "h",
                "username": "u",
                "path": "x.csv",
                "password": "p",
            }
        )

    def test_happy_path_with_private_key_passes(self):
        source_sftp.validate_config(
            {
                "host": "h",
                "username": "u",
                "path": "x.csv",
                "private_key": "k",
            }
        )


class TestTargetSftpCodes:
    def test_missing_host(self):
        with pytest.raises(ValidationError) as exc:
            target_sftp.validate_config({"username": "u", "path": "x", "password": "p"})
        assert exc.value.code == VALIDATION_CODES.TARGET_SFTP_MISSING_HOST

    def test_missing_username(self):
        with pytest.raises(ValidationError) as exc:
            target_sftp.validate_config({"host": "h", "path": "x", "password": "p"})
        assert exc.value.code == VALIDATION_CODES.TARGET_SFTP_MISSING_USERNAME

    def test_missing_path(self):
        with pytest.raises(ValidationError) as exc:
            target_sftp.validate_config({"host": "h", "username": "u", "password": "p"})
        assert exc.value.code == VALIDATION_CODES.TARGET_SFTP_MISSING_PATH

    def test_missing_auth(self):
        with pytest.raises(ValidationError) as exc:
            target_sftp.validate_config(
                {
                    "host": "h",
                    "username": "u",
                    "path": "x.csv",
                }
            )
        assert exc.value.code == VALIDATION_CODES.TARGET_SFTP_MISSING_AUTH
