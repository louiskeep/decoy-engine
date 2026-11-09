"""A1: the export codec."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, tzinfo
from typing import Any

import pytest

from decoy_engine import records
from decoy_engine.records import BadLine, ExportedLog, ExportedRecord, RecordError, canonical_bytes
from tests.unit.records._helpers import (
    INVOCATION,
    PRODUCER,
    SHA_B,
    T0,
    corpus,
    make,
    raw_of,
    sample_payload,
    seal,
)


def _lines(recs: list[dict[str, Any]], **kw: Any) -> list[bytes]:
    return list(records.export_lines(recs, producer=PRODUCER, exported_at=T0, **kw))


def _read(lines: list[bytes]) -> list[Any]:
    return list(records.read_export(lines))


def _chain() -> list[dict[str, Any]]:
    evidence = raw_of(make("evidence", corpus("evidence-cli-local-1.json")))
    sample = make(links=[{"rel": "evidence", "id": evidence["id"]}])
    return [evidence, raw_of(sample)]


def test_roundtrip_keeps_envelopes_and_rejects_a_tampered_line_alone() -> None:
    """A1-export-roundtrip."""
    chain = _chain()
    lines = _lines(chain, keep_paths=True)
    items = _read(lines)
    assert [type(i).__name__ for i in items] == [
        "ExportHeaderRead",
        "ExportedRecord",
        "ExportedRecord",
    ]
    assert [i.record.raw for i in items[1:]] == chain
    assert all(i.source_id is None for i in items[1:])
    assert items[0].paths_stripped is False and items[0].record_count == 2

    broken = lines[:]
    broken[1] = broken[1].replace(b'"row_counts"', b'"row_countz"')
    got = _read(broken)
    assert isinstance(got[1], BadLine) and got[1].code == "record_corrupt" and got[1].lineno == 2
    assert isinstance(got[2], ExportedRecord)


def test_paths_are_reduced_to_file_names_and_ids_follow() -> None:
    chain = _chain()
    items = _read(_lines(chain))
    header, evidence, sample = items
    payload = evidence.record.raw["payload"]
    assert payload["pipeline_path"] == "claims.yaml"
    assert payload["input_fingerprints"]["médecins"]["path"] == "médecins.csv"
    assert payload["output_fingerprints"]["ledger"]["path"] == "ledger.txt"
    assert header.paths_stripped is True
    assert evidence.source_id == chain[0]["id"] and evidence.record.id != chain[0]["id"]
    assert records.record_id(evidence.record.raw) == evidence.record.id
    assert evidence.record.raw["payload_digest"] == records.payload_digest(payload)
    # Closure: the linked record points at the new id, so its own id moved as well.
    assert sample.record.raw["links"][0]["id"] == evidence.record.id
    assert sample.source_id == chain[1]["id"]
    assert sample.record.raw["payload"]["scratch_dir"] == "sample-1"
    assert header.external_links == 0


def test_links_outside_the_export_are_counted_and_kept() -> None:
    sample = make(links=[{"rel": "baseline", "id": SHA_B}])
    header, rec = _read(_lines([raw_of(sample)], keep_paths=True))
    assert header.external_links == 1
    assert rec.record.raw["links"] == [{"rel": "baseline", "id": SHA_B}]


def test_refs_follow_the_new_ids() -> None:
    chain = _chain()
    header, evidence, _ = _read(_lines(chain, refs={"evidence/latest": chain[0]["id"]}))
    assert header.refs == {"evidence/latest": evidence.record.id}


def _newer_and_opaque() -> list[dict[str, Any]]:
    newer = raw_of(make(labels={"status": "ok"}))
    newer["payload"]["later_field"] = "/home/user/x.csv"
    opaque = raw_of(make(labels={"status": "ok"}))
    opaque.update(kind="future-thing", payload_schema="future-thing/v1", payload={"p": "/a/b.csv"})
    return [seal(newer), seal(opaque)]


def test_newer_and_opaque_records_are_copied_byte_for_byte() -> None:
    """A1-export-newer."""
    recs = _newer_and_opaque()
    header, *items = _read(_lines(recs))
    for original, item in zip(recs, items, strict=True):
        assert canonical_bytes(item.record.raw) == canonical_bytes(original)
        assert item.record.id == original["id"] and item.source_id is None
    assert [u["id"] for u in header.unstripped] == [r["id"] for r in recs]
    assert header.paths_stripped is False


def test_opaque_record_links_are_remapped_though_its_payload_is_not() -> None:
    chain = _chain()
    opaque = raw_of(make(links=[{"rel": "evidence", "id": chain[0]["id"]}]))
    opaque.update(kind="future-thing", payload_schema="future-thing/v1", payload={"p": "/a/b.csv"})
    opaque = seal(opaque)
    _, evidence, _, copy_ = _read(_lines([chain[0], chain[1], opaque]))
    assert copy_.record.raw["links"][0]["id"] == evidence.record.id
    assert copy_.record.raw["payload"] == {"p": "/a/b.csv"}
    assert copy_.source_id == opaque["id"]


def test_a_tampered_input_is_refused() -> None:
    bad = raw_of(make())
    bad["payload"]["ttl_seconds"] = 5
    with pytest.raises(RecordError) as caught:
        _lines([bad])
    assert caught.value.code == "record_corrupt"


def test_logs_travel_with_their_hash() -> None:
    log = b'{"log_schema":"decoy-job-log/v1"}\n{"kind":"phase"}\n'
    lines = _lines([], keep_paths=True, logs=[("11111111-2222-4333-8444-555555555555", log)])
    header, item = _read(lines)
    assert isinstance(item, ExportedLog) and item.verified and item.content.encode() == log
    tampered = lines[1].replace(b"phase", b"phasf")
    assert _read([lines[0], tampered])[1].verified is False


def test_non_utf8_log_is_refused() -> None:
    with pytest.raises(RecordError):
        _lines([], keep_paths=True, logs=[(INVOCATION, b"\xff\xfe")])


def test_a_naive_export_time_is_refused() -> None:
    # Read as host time, the same input would give different header bytes per machine.
    with pytest.raises(RecordError) as caught:
        list(records.export_lines([], producer=PRODUCER, exported_at=T0.replace(tzinfo=None)))
    assert caught.value.code == "record_invalid"


def test_a_bad_producer_is_a_value_free_record_error() -> None:
    """The producer comes from the caller; a failure names the location, never the value."""
    secret = "SECRET-PRODUCER-VALUE"
    with pytest.raises(RecordError) as caught:
        list(records.export_lines([], producer={"name": 7, "version": secret}, exported_at=T0))
    assert caught.value.code == "record_invalid"
    assert secret not in repr(caught.value) and secret not in str(caught.value)
    assert caught.value.__context__ is None and caught.value.__cause__ is None


class _NoOffset(tzinfo):
    """A zone object that reports no offset: Python treats such a time as naive."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None


def test_a_zone_without_an_offset_is_refused() -> None:
    with pytest.raises(RecordError):
        list(
            records.export_lines([], producer=PRODUCER, exported_at=T0.replace(tzinfo=_NoOffset()))
        )


@pytest.mark.parametrize("lines", [[], [b""], [b"\n", b"  \n"]])
def test_an_export_with_no_header_is_refused(lines: list[bytes]) -> None:
    with pytest.raises(RecordError):
        _read(lines)


@pytest.mark.parametrize(
    "first",
    [
        b"",
        b"[]",
        b'{"type":"record"}',
        b'{"type":"header","export_schema":"decoy-library-export/v2"}',
    ],
)
def test_a_file_without_a_current_header_is_refused(first: bytes) -> None:
    with pytest.raises(RecordError):
        _read([first or b"\n", b'{"type":"record"}\n'])


def test_unknown_and_malformed_lines_are_flagged_alone() -> None:
    lines = _lines(_chain(), keep_paths=True)
    extra = [b"{not json\n", b'{"type":"future"}\n', b'{"type":"record","record":3}\n']
    got = _read([lines[0], *extra, lines[1]])
    assert [type(g).__name__ for g in got] == [
        "ExportHeaderRead",
        "BadLine",
        "BadLine",
        "BadLine",
        "ExportedRecord",
    ]


def test_header_is_the_first_line_and_json_lines_are_canonical() -> None:
    lines = _lines(_chain())
    assert json.loads(lines[0])["type"] == "header"
    assert all(
        line.endswith(b"\n") and canonical_bytes(json.loads(line)) + b"\n" == line for line in lines
    )


def test_sample_payload_helper_is_used() -> None:
    assert sample_payload()["sample_size"] == 50


def test_sec1_logs_stay_out_without_keep_paths_and_are_listed() -> None:
    """SEC-1: a log line can name paths; it is never exported unstripped."""
    log = b'{"log_schema":"decoy-job-log/v1","pipeline":"/home/u/p.yaml"}\n'
    lines = _lines([], logs=[(INVOCATION, log)])
    items = _read(lines)
    assert len(items) == 1
    assert items[0].withheld_logs == (INVOCATION,) and items[0].unstripped == ()
    kept = _read(_lines([], keep_paths=True, logs=[(INVOCATION, log)]))
    assert kept[0].withheld_logs == ()
    assert isinstance(kept[1], ExportedLog)


def test_sec2_label_pipeline_path_is_reduced_and_the_id_follows() -> None:
    """SEC-2."""
    rec = raw_of(make(labels={"pipeline": "proj/pipelines/claims.yaml"}))
    header, item = _read(_lines([rec]))
    assert item.record.raw["labels"]["pipeline"] == "claims.yaml"
    assert item.source_id == rec["id"] and records.record_id(item.record.raw) == item.record.id
    assert header.paths_stripped is True
    kept = _read(_lines([rec], keep_paths=True))[1]
    assert kept.record.raw["labels"]["pipeline"] == rec["labels"]["pipeline"]


def test_sec3_newer_envelope_is_unverified_and_never_read_as_an_export_record() -> None:
    """SEC-3."""
    from decoy_engine.records import _codec

    raw = raw_of(make())
    raw["record_schema"] = "decoy-record/v2"
    got = records.decode(canonical_bytes(raw))
    assert isinstance(got, records.RecordNeedsNewer) and got.verified is False
    for bad in ({"id": "../../x"}, {"id": "sha256:" + "A" * 64}, {"kind": "../k"}):
        with pytest.raises(RecordError):
            _codec.decode(canonical_bytes({**raw, **bad}))
    header = _lines([], keep_paths=True)[0]
    line = json.dumps({"type": "record", "source_id": None, "record": raw}).encode()
    bad_line = _read([header, line])[1]
    assert isinstance(bad_line, BadLine)
