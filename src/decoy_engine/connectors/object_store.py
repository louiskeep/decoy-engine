"""Stub."""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO, Literal, Protocol

from decoy_engine.connectors.gcs import GCSConfig
from decoy_engine.connectors.s3 import S3Config

Version = str
PutResult = Literal["won", "rejected", "conflict", "unknown"]


class ObjectStore(Protocol):
    def put_new(self, key: str, fd: BinaryIO) -> Version: ...
    def read_small(self, key: str, limit: int) -> tuple[bytes, Version] | None: ...
    def put_if(self, key: str, data: bytes, expected: Version | None) -> PutResult: ...
    def delete_created(self, key: str, version: Version) -> None: ...
    def iter_object(self, key: str) -> Iterator[bytes]: ...
    def check_region(self) -> None: ...


class S3ObjectStore:
    def __init__(self, config: S3Config) -> None:
        self._config = config

    def put_new(self, key: str, fd: BinaryIO) -> Version:
        return ""

    def read_small(self, key: str, limit: int) -> tuple[bytes, Version] | None:
        return None

    def put_if(self, key: str, data: bytes, expected: Version | None) -> PutResult:
        return "unknown"

    def delete_created(self, key: str, version: Version) -> None:
        return None

    def iter_object(self, key: str) -> Iterator[bytes]:
        return iter(())

    def check_region(self) -> None:
        return None


class GCSObjectStore:
    def __init__(self, config: GCSConfig) -> None:
        self._config = config

    def iter_object(self, key: str) -> Iterator[bytes]:
        return iter(())
