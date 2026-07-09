"""Bounded S3 / GCS `ProfileSource` readers.

Pattern: HTTP ranged reads + Parquet footer metadata (Apache Arrow / AWS S3
GetObject Range / GCS objects.get Range; all vendor-standard). See: the S3
`Range` request header and GCS `download_as_bytes(start, end)` fetch only the
requested byte window, so a Parquet footer + one Arrow batch, or a CSV's first
`<= sample_rows` rows, can be read without transferring the whole object -- the
cloud analogue of the local footer/`nrows` readers in `_readers.py`.

Companion to `profile/_readers.py`. The engine never sees raw secrets:
`credentials_ref` is opaque and the SDKs walk their default credential chain.
Error bodies are wrapped so the raw SDK exception string (which can carry
endpoint URLs / request metadata) never reaches job logs, preserving the QA-7
F2 hygiene the eager loaders established.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.profile._readers import (
    ProfileSource,
    RowCount,
    estimate_csv_rows,
    head_frame_from_batches,
)

# One ranged read big enough to cover a CSV header + a bounded sample on a
# typical object, while still being a tiny fraction of a large file. read_csv
# drops a truncated trailing line, so an over-read never corrupts the sample.
_CSV_SAMPLE_WINDOW_BYTES = 1 << 20  # 1 MiB
# Rows to parse from the window when only the schema (column set + dtypes) is
# needed; bounded so schema inference never depends on the full window.
_CSV_ESTIMATE_ROWS = 1_000


# ---------------------------------------------------------------------
# Seekable ranged file: lets pyarrow read a Parquet footer + batches from a
# remote object via byte-range fetches instead of a whole-object download.
# ---------------------------------------------------------------------


class _RangeFileReader(io.RawIOBase):
    """A read-only seekable file backed by a `fetch(start, end_inclusive)`.

    pyarrow needs random access to read a Parquet footer (stored at the end of
    the file) and then decode selected row groups; this adapter serves those
    reads from byte-range requests so only the bytes pyarrow asks for cross the
    wire. `fetch` returns the inclusive `[start, end]` byte range.
    """

    def __init__(self, size: int, fetch: Callable[[int, int], bytes]) -> None:
        super().__init__()
        self._size = size
        self._fetch = fetch
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._size + offset
        else:  # pragma: no cover -- io module only defines the three above
            raise ValueError(f"invalid whence {whence!r}")
        return self._pos

    def readinto(self, buffer: Any) -> int:
        want = len(buffer)
        if want == 0 or self._pos >= self._size:
            return 0
        end_inclusive = min(self._pos + want, self._size) - 1
        data = self._fetch(self._pos, end_inclusive)
        n = len(data)
        buffer[:n] = data
        self._pos += n
        return n


def _read_parquet_row_count(size: int, fetch: Callable[[int, int], bytes]) -> RowCount:
    reader = _RangeFileReader(size, fetch)
    return RowCount(pq.ParquetFile(pa.PythonFile(reader, mode="r")).metadata.num_rows, exact=True)


def _read_parquet_schema(size: int, fetch: Callable[[int, int], bytes]) -> pa.Schema:
    reader = _RangeFileReader(size, fetch)
    return pq.ParquetFile(pa.PythonFile(reader, mode="r")).schema_arrow


def _read_parquet_sample(
    size: int, fetch: Callable[[int, int], bytes], sample_rows: int
) -> pd.DataFrame:
    reader = _RangeFileReader(size, fetch)
    parquet_file = pq.ParquetFile(pa.PythonFile(reader, mode="r"))
    return head_frame_from_batches(
        parquet_file.iter_batches(batch_size=sample_rows), sample_rows, parquet_file.schema_arrow
    )


def _csv_row_count_from_window(size: int, window: bytes) -> RowCount:
    """Estimate CSV rows from the object size and a leading byte window.

    Splits the window into header + data lines to measure mean row width, then
    scales to the full object size. Mirrors the local CSV estimate so cloud and
    local CSV counts are flagged and computed the same way.
    """
    lines = window.split(b"\n")
    if not lines:
        return RowCount(0, exact=False)
    header_bytes = len(lines[0]) + 1  # + the split-stripped newline
    # Drop the header and a possibly-truncated final fragment; keep whole lines.
    data_lines = [len(line) + 1 for line in lines[1:-1] if line]
    return estimate_csv_rows(size, header_bytes, data_lines)


def _csv_sample_from_window(window: bytes, size: int, sample_rows: int) -> pd.DataFrame:
    """Parse up to `sample_rows` rows from a leading CSV byte window.

    If the window is a prefix of a larger object it may end mid-row; the last
    fragment is dropped before parsing so `read_csv` never sees a torn line.
    """
    if len(window) < size:
        window = window.rsplit(b"\n", 1)[0] + b"\n"
    return pd.read_csv(io.BytesIO(window), nrows=sample_rows)


# ---------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------


def _s3_client(descriptor: dict[str, Any]) -> Any:
    """Construct a per-call boto3 S3 client (no shared global; QA Q1/Q10).

    Connect + read timeouts so a black-holed endpoint cannot hang the worker
    (QA-7 F2). Region + `endpoint_url` (MinIO/R2/moto) come from the descriptor.
    """
    import boto3
    from botocore.config import Config as BotoConfig

    client_kwargs: dict[str, Any] = {}
    region = descriptor.get("region")
    if isinstance(region, str) and region:
        client_kwargs["region_name"] = region
    endpoint_url = descriptor.get("endpoint_url")
    if isinstance(endpoint_url, str) and endpoint_url:
        client_kwargs["endpoint_url"] = endpoint_url
    client_kwargs["config"] = BotoConfig(
        connect_timeout=5,
        read_timeout=60,
        retries={"max_attempts": 1, "mode": "standard"},
    )
    return boto3.client("s3", **client_kwargs)


def _s3_wrapped(call: Callable[[], Any]) -> Any:
    """Run an S3 SDK call, wrapping SDK errors so no raw metadata leaks.

    Network-class errors surface only their exception type name; client errors
    surface only the error CODE (NoSuchKey, AccessDenied, ...). The raw request
    metadata / access key ID stays out of the message (QA-7 F2).
    """
    from botocore.exceptions import (
        ClientError,
        ConnectTimeoutError,
        EndpointConnectionError,
        ReadTimeoutError,
    )

    try:
        return call()
    except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError) as exc:
        raise RuntimeError(
            f"profile_source s3: transient network error ({type(exc).__name__})"
        ) from exc
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        raise RuntimeError(f"profile_source s3: client error {code}") from exc


def _s3_size(client: Any, bucket: str, key: str) -> int:
    head = _s3_wrapped(lambda: client.head_object(Bucket=bucket, Key=key))
    return int(head["ContentLength"])


def _s3_range(client: Any, bucket: str, key: str, start: int, end_inclusive: int) -> bytes:
    response = _s3_wrapped(
        lambda: client.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-{end_inclusive}")
    )
    # Q17: draining .read() does not release the socket; close the body so the
    # connection returns to the pool (multi-range parquet reads reuse it).
    with response["Body"] as stream:
        return bytes(stream.read())


def _s3_full(client: Any, bucket: str, key: str) -> bytes:
    response = _s3_wrapped(lambda: client.get_object(Bucket=bucket, Key=key))
    with response["Body"] as stream:
        return bytes(stream.read())


class S3ParquetSource:
    """Bounded `ProfileSource` for an S3 Parquet object (footer + batch reads)."""

    def __init__(self, descriptor: dict[str, Any]) -> None:
        self._descriptor = descriptor
        self._bucket = _require_str(descriptor, "bucket", "s3")
        self._key = _require_str(descriptor, "key", "s3")

    def _bind(self) -> tuple[Any, int, Callable[[int, int], bytes]]:
        client = _s3_client(self._descriptor)
        size = _s3_size(client, self._bucket, self._key)

        def fetch(start: int, end_inclusive: int) -> bytes:
            return _s3_range(client, self._bucket, self._key, start, end_inclusive)

        return client, size, fetch

    def row_count(self) -> RowCount:
        _, size, fetch = self._bind()
        return _read_parquet_row_count(size, fetch)

    def schema(self) -> pa.Schema:
        _, size, fetch = self._bind()
        return _read_parquet_schema(size, fetch)

    def sample_frame(self, sample_rows: int) -> pd.DataFrame:
        _, size, fetch = self._bind()
        return _read_parquet_sample(size, fetch, sample_rows)

    def to_frame(self) -> pd.DataFrame:
        client = _s3_client(self._descriptor)
        return pd.read_parquet(io.BytesIO(_s3_full(client, self._bucket, self._key)))


class S3CsvSource:
    """Bounded `ProfileSource` for an S3 CSV object (size estimate + window)."""

    def __init__(self, descriptor: dict[str, Any]) -> None:
        self._descriptor = descriptor
        self._bucket = _require_str(descriptor, "bucket", "s3")
        self._key = _require_str(descriptor, "key", "s3")

    def _size_and_window(self) -> tuple[int, bytes]:
        client = _s3_client(self._descriptor)
        size = _s3_size(client, self._bucket, self._key)
        end = min(_CSV_SAMPLE_WINDOW_BYTES, size) - 1
        window = _s3_range(client, self._bucket, self._key, 0, end) if size > 0 else b""
        return size, window

    def row_count(self) -> RowCount:
        size, window = self._size_and_window()
        return _csv_row_count_from_window(size, window)

    def schema(self) -> pa.Schema:
        size, window = self._size_and_window()
        # Reuse the sample parser so a window truncated mid-row is trimmed to
        # whole lines before pandas infers the schema.
        head = _csv_sample_from_window(window, size, _CSV_ESTIMATE_ROWS)
        return pa.Table.from_pandas(head, preserve_index=False).schema

    def sample_frame(self, sample_rows: int) -> pd.DataFrame:
        size, window = self._size_and_window()
        return _csv_sample_from_window(window, size, sample_rows)

    def to_frame(self) -> pd.DataFrame:
        client = _s3_client(self._descriptor)
        return pd.read_csv(io.BytesIO(_s3_full(client, self._bucket, self._key)))


def build_s3_profile_source(descriptor: dict[str, Any]) -> ProfileSource:
    fmt = descriptor.get("format")
    if fmt == "csv":
        return S3CsvSource(descriptor)
    if fmt == "parquet":
        return S3ParquetSource(descriptor)
    raise NotImplementedError(
        f"profile_source: unsupported s3 format {fmt!r}. V1 supports csv | parquet only."
    )


# ---------------------------------------------------------------------
# GCS
# ---------------------------------------------------------------------


def _gcs_blob(descriptor: dict[str, Any], client: Any) -> Any:
    bucket_name = _require_str(descriptor, "bucket", "gcs")
    object_name = _require_str(descriptor, "object", "gcs")
    return client.bucket(bucket_name).blob(object_name)


def _gcs_size(blob: Any) -> int:
    if blob.size is None:
        # size is populated by a metadata fetch, not an object download.
        blob.reload()
    return int(blob.size)


class GcsParquetSource:
    """Bounded `ProfileSource` for a GCS Parquet object (footer + batch reads)."""

    def __init__(self, descriptor: dict[str, Any]) -> None:
        self._descriptor = descriptor

    def _with_reads(self, consume: Callable[[int, Callable[[int, int], bytes]], Any]) -> Any:
        from google.cloud import storage

        # Q18: storage.Client owns an HTTP transport; close it via the context
        # manager once all ranged reads for this call have completed.
        with storage.Client() as client:
            blob = _gcs_blob(self._descriptor, client)
            size = _gcs_size(blob)

            def fetch(start: int, end_inclusive: int) -> bytes:
                return bytes(blob.download_as_bytes(start=start, end=end_inclusive))

            return consume(size, fetch)

    def row_count(self) -> RowCount:
        return self._with_reads(lambda size, fetch: _read_parquet_row_count(size, fetch))

    def schema(self) -> pa.Schema:
        return self._with_reads(lambda size, fetch: _read_parquet_schema(size, fetch))

    def sample_frame(self, sample_rows: int) -> pd.DataFrame:
        return self._with_reads(lambda size, fetch: _read_parquet_sample(size, fetch, sample_rows))

    def to_frame(self) -> pd.DataFrame:
        from google.cloud import storage

        with storage.Client() as client:
            data = _gcs_blob(self._descriptor, client).download_as_bytes()
        return pd.read_parquet(io.BytesIO(data))


class GcsCsvSource:
    """Bounded `ProfileSource` for a GCS CSV object (size estimate + window)."""

    def __init__(self, descriptor: dict[str, Any]) -> None:
        self._descriptor = descriptor

    def _size_and_window(self) -> tuple[int, bytes]:
        from google.cloud import storage

        with storage.Client() as client:
            blob = _gcs_blob(self._descriptor, client)
            size = _gcs_size(blob)
            if size == 0:
                return 0, b""
            end = min(_CSV_SAMPLE_WINDOW_BYTES, size) - 1
            window = bytes(blob.download_as_bytes(start=0, end=end))
        return size, window

    def row_count(self) -> RowCount:
        size, window = self._size_and_window()
        return _csv_row_count_from_window(size, window)

    def schema(self) -> pa.Schema:
        size, window = self._size_and_window()
        # Reuse the sample parser so a window truncated mid-row is trimmed to
        # whole lines before pandas infers the schema.
        head = _csv_sample_from_window(window, size, _CSV_ESTIMATE_ROWS)
        return pa.Table.from_pandas(head, preserve_index=False).schema

    def sample_frame(self, sample_rows: int) -> pd.DataFrame:
        size, window = self._size_and_window()
        return _csv_sample_from_window(window, size, sample_rows)

    def to_frame(self) -> pd.DataFrame:
        from google.cloud import storage

        with storage.Client() as client:
            data = _gcs_blob(self._descriptor, client).download_as_bytes()
        return pd.read_csv(io.BytesIO(data))


def build_gcs_profile_source(descriptor: dict[str, Any]) -> ProfileSource:
    fmt = descriptor.get("format")
    if fmt == "csv":
        return GcsCsvSource(descriptor)
    if fmt == "parquet":
        return GcsParquetSource(descriptor)
    raise NotImplementedError(
        f"profile_source: unsupported gcs format {fmt!r}. V1 supports csv | parquet only."
    )


def _require_str(descriptor: dict[str, Any], field: str, kind: str) -> str:
    value = descriptor.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"profile_source: {kind} source missing {field}, got {value!r}")
    return value


__all__ = [
    "GcsCsvSource",
    "GcsParquetSource",
    "S3CsvSource",
    "S3ParquetSource",
    "build_gcs_profile_source",
    "build_s3_profile_source",
]
