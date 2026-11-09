"""A scan-set record keeps each table's source descriptor; export must not leak it."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from decoy_engine import records
from decoy_engine.config import _sources
from decoy_engine.profiling import scan_set_to_dict, scan_sources
from tests.unit.records._helpers import PRODUCER, T0, make, raw_of

SECRET_DIR = "/home/alice/secret"
S3_KEY = "customers/jane.doe/ssn_export.csv"


def _scan(tmp_path: Path) -> Any:
    names = tmp_path / "names.txt"
    names.write_text("id\n")
    data = tmp_path / "patients.csv"
    data.write_text("1\n2\n")
    source = {
        "type": "file",
        "format": "csv",
        "path": str(data),
        "has_header": False,
        "column_names_file": str(names),
    }
    return scan_sources({"patients": source})


def _record(scan: Any) -> dict[str, Any]:
    payload = scan_set_to_dict(scan, detail=False)
    return raw_of(make("scan-set", payload, labels={}))


def _file_record(tmp_path: Path) -> dict[str, Any]:
    scan = _scan(tmp_path)
    table = dataclasses.replace(
        scan.tables[0],
        descriptor={**scan.tables[0].descriptor, "path": f"{SECRET_DIR}/patients.csv"},
    )
    return _record(dataclasses.replace(scan, tables=(table,)))


def _s3_record(tmp_path: Path) -> dict[str, Any]:
    scan = _scan(tmp_path)
    descriptor = {
        "type": "s3",
        "format": "csv",
        "bucket": "acme-prod-bucket",
        "key": S3_KEY,
        "endpoint_url": "https://minio.internal.example:9000",
    }
    table = dataclasses.replace(scan.tables[0], descriptor=descriptor)
    return _record(dataclasses.replace(scan, tables=(table,)))


def _export(rec: dict[str, Any]) -> tuple[Any, str]:
    lines = list(records.export_lines([rec], producer=PRODUCER, exported_at=T0))
    header = next(iter(records.read_export(lines)))
    return header, b"".join(lines).decode()


def test_scan_set_path_fields_come_from_the_marker_registry() -> None:
    spec = records.kind_spec("scan-set")
    assert spec is not None
    expected = {f"/payload/tables/*/descriptor/{f}" for f in _sources.SOURCE_PATH_FIELDS}
    assert set(spec.path_fields) == expected
    assert {"path", "column_names_file", "key", "object"} <= set(_sources.SOURCE_PATH_FIELDS)
    assert set(_sources.FILE_SOURCE_PATH_FIELDS) <= set(_sources.SOURCE_PATH_FIELDS)
    assert set(spec.locator_fields) == {
        "/payload/tables/*/descriptor/bucket",
        "/payload/tables/*/descriptor/endpoint_url",
    }


def test_a_file_scan_exports_without_its_directory(tmp_path: Path) -> None:
    header, text = _export(_file_record(tmp_path))
    assert SECRET_DIR not in text and str(tmp_path) not in text
    assert "patients.csv" in text and "names.txt" in text
    assert header.paths_stripped is True and header.unstripped == ()


def test_an_s3_scan_exports_without_its_key_and_lists_the_locator(tmp_path: Path) -> None:
    rec = _s3_record(tmp_path)
    header, text = _export(rec)
    assert "jane.doe" not in text and S3_KEY not in text
    assert "ssn_export.csv" in text
    assert len(header.unstripped) == 1
    exported = [json.loads(line) for line in text.splitlines()][1]["record"]
    assert header.unstripped[0]["id"] == exported["id"]


def test_keep_paths_lists_nothing(tmp_path: Path) -> None:
    rec = _s3_record(tmp_path)
    lines = list(records.export_lines([rec], producer=PRODUCER, exported_at=T0, keep_paths=True))
    assert records.read_export(lines).__next__().unstripped == ()
