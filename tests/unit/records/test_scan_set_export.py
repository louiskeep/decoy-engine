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
        "region": "eu-private-1",
        "credentials_ref": "vault/acme-prod-creds",
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
        "/payload/tables/*/descriptor/region",
        "/payload/tables/*/descriptor/credentials_ref",
    }


def test_a_file_scan_exports_without_its_directory(tmp_path: Path) -> None:
    header, text = _export(_file_record(tmp_path))
    assert SECRET_DIR not in text and str(tmp_path) not in text
    assert "patients.csv" in text and "names.txt" in text
    assert header.paths_stripped is True and header.unstripped == ()


def test_an_s3_scan_exports_without_key_bucket_or_endpoint(tmp_path: Path) -> None:
    rec = _s3_record(tmp_path)
    header, text = _export(rec)
    for leaked in (
        "jane.doe",
        S3_KEY,
        "acme-prod-bucket",
        "minio.internal",
        "eu-private-1",
        "acme-prod-creds",
    ):
        assert leaked not in text
    assert "ssn_export.csv" in text
    exported = [json.loads(line) for line in text.splitlines()][1]["record"]
    descriptor = exported["payload"]["tables"][0]["descriptor"]
    assert "bucket" not in descriptor and "endpoint_url" not in descriptor
    assert [(u.id, u.source_id) for u in header.unstripped] == [(exported["id"], rec["id"])]
    assert records.record_id(exported) == exported["id"]
    assert exported["payload_digest"] == records.payload_digest(exported["payload"])


def test_keep_paths_lists_nothing(tmp_path: Path) -> None:
    rec = _s3_record(tmp_path)
    lines = list(records.export_lines([rec], producer=PRODUCER, exported_at=T0, keep_paths=True))
    assert records.read_export(lines).__next__().unstripped == ()


def test_a_descriptor_with_an_unknown_key_is_withheld_from_a_default_export(tmp_path: Path) -> None:
    scan = _scan(tmp_path)
    descriptor = {**scan.tables[0].descriptor, "future_locator": "s3://private/value.csv"}
    table = dataclasses.replace(scan.tables[0], descriptor=descriptor)
    rec = _record(dataclasses.replace(scan, tables=(table,)))
    lines = list(records.export_lines([rec], producer=PRODUCER, exported_at=T0))
    header = next(iter(records.read_export(lines)))
    assert header.withheld_records == (rec["id"],) and header.record_count == 0
    assert b"s3://private" not in b"".join(lines)
    kept = list(records.export_lines([rec], producer=PRODUCER, exported_at=T0, keep_paths=True))
    item = list(records.read_export(kept))[1]
    assert records.canonical_bytes(item.record.raw) == records.canonical_bytes(rec)


def test_a_gcs_scan_drops_its_credentials_ref(tmp_path: Path) -> None:
    scan = _scan(tmp_path)
    descriptor = {
        "type": "gcs",
        "format": "csv",
        "bucket": "b",
        "object": "x/y/z.csv",
        "credentials_ref": "secret-ref",
    }
    table = dataclasses.replace(scan.tables[0], descriptor=descriptor)
    header, text = _export(_record(dataclasses.replace(scan, tables=(table,))))
    assert "secret-ref" not in text and "x/y" not in text and "z.csv" in text
    assert len(header.unstripped) == 1


def _with_descriptor(tmp_path: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    scan = _scan(tmp_path)
    table = dataclasses.replace(scan.tables[0], descriptor=descriptor)
    return _record(dataclasses.replace(scan, tables=(table,)))


def _fixed_width(width: Any) -> dict[str, Any]:
    return {
        "type": "file",
        "format": "fixed_width",
        "path": "/data/in.txt",
        "layout": {"columns": [{"name": "id", "start": 0, "width": width}]},
    }


def test_a_valid_fixed_width_descriptor_exports_its_projection(tmp_path: Path) -> None:
    header, text = _export(_with_descriptor(tmp_path, _fixed_width(4)))
    assert header.withheld_records == () and header.record_count == 1
    assert '"width":4' in text and "/data/in.txt" not in text


def test_a_coerced_descriptor_value_withholds_the_record(tmp_path: Path) -> None:
    """A width given as text validates in lax mode; export must not emit that text."""
    rec = _with_descriptor(tmp_path, _fixed_width("4111111111111111"))
    header, text = _export(rec)
    assert header.withheld_records == (rec["id"],) and header.record_count == 0
    assert "4111111111111111" not in text


def test_a_bool_given_as_text_withholds_the_record(tmp_path: Path) -> None:
    csv = {"type": "file", "format": "csv", "path": "/data/in.csv", "has_header": "true"}
    rec = _with_descriptor(tmp_path, csv)
    header, _ = _export(rec)
    assert header.withheld_records == (rec["id"],)
