"""Gate round 1: the stored form owns the id, one value-free vocabulary, one read deriver,
mutation-resistant decode checks, and errors that keep no cause."""

from __future__ import annotations

import json
from typing import Any

import pytest

from decoy_engine import records
from decoy_engine.records import RecordError, canonical_bytes
from decoy_engine.records import _joblog as joblog
from decoy_engine.records._payloads import EvidencePayload
from decoy_engine.records._read_model import read_model_of
from tests.unit.records._helpers import (
    INVOCATION,
    PRODUCER,
    T0,
    corpus,
    make,
    raw_of,
    sample_payload,
    seal,
)


def _clean(error: BaseException) -> None:
    # A validator or JSON error would hold the caller's text (a path, a cell) in its input.
    assert error.__cause__ is None and error.__context__ is None


# -- H1 -----------------------------------------------------------------------------------------


def test_a_none_label_is_dropped_before_the_id_is_computed() -> None:
    env = make(labels={"pipeline": None, "status": "ok"})
    assert env.labels.pipeline is None
    got = records.decode(records.encode(env))
    assert isinstance(got, records.DecodedRecord) and got.id == env.id
    assert "pipeline" not in got.raw["labels"]
    assert env.id == make(labels={"status": "ok"}).id


def test_producer_and_links_are_hashed_in_their_stored_form() -> None:
    other = make()
    env = make(links=[{"rel": "baseline", "id": other.id}])
    assert records.decode(records.encode(env)).id == env.id


# -- H2 -----------------------------------------------------------------------------------------


def test_a_later_enum_value_under_the_same_payload_tag_still_reads() -> None:
    raw = raw_of(make("evidence", corpus("evidence-cli-local-1.json")))
    raw["payload"]["output_fingerprints"]["claims"]["format"] = "orc"
    got = records.decode(canonical_bytes(seal(raw)))
    assert isinstance(got, records.DecodedRecord)
    assert got.view.output_fingerprints["claims"].format == "orc"


def test_a_later_enum_value_is_still_checked_for_free_text() -> None:
    raw = raw_of(make("evidence", corpus("evidence-cli-local-1.json")))
    raw["payload"]["output_fingerprints"]["claims"]["format"] = "a b/c"
    with pytest.raises(RecordError) as caught:
        records.decode(canonical_bytes(seal(raw)))
    assert caught.value.code == "record_invalid"


def test_the_schema_and_producer_tags_stay_exact_on_read() -> None:
    model = read_model_of(EvidencePayload)
    for name in ("schema_version", "producer"):
        assert str(model.model_fields[name].annotation).startswith("typing.Literal")
    raw = raw_of(make("evidence", corpus("evidence-cli-local-1.json")))
    raw["payload"]["producer"] = "someone-else"
    with pytest.raises(RecordError):
        records.decode(canonical_bytes(seal(raw)))


def test_there_is_one_deriver() -> None:
    from decoy_engine import _value_free
    from decoy_engine.run_result import _model

    assert not hasattr(_model, "_derive_read") and not hasattr(_model, "_read_annotation")
    assert _model.derive_read is _value_free.derive_read


# -- H3 -----------------------------------------------------------------------------------------


def test_the_envelope_uses_the_shared_value_free_types() -> None:
    from decoy_engine.records import _envelope
    from decoy_engine.run_result import _model

    for name in ("InvocationId", "RelPath", "UtcTimestamp", "SafeName", "SafeRef", "Version"):
        assert getattr(_envelope, name) is getattr(_model, name)


@pytest.mark.parametrize(
    "labels",
    [
        {"tables": ["a/b"]},
        {"tables": ["x=y"]},
        {"status": "has space"},
        {"route": "a;b"},
        {"pipeline": "/abs/p.yaml"},
        {"pipeline": "../up/p.yaml"},
    ],
)
def test_labels_hold_names_and_refs_only(labels: dict[str, Any]) -> None:
    with pytest.raises(RecordError) as caught:
        make(labels=labels)
    assert caught.value.code == "record_invalid"


def test_invocation_id_is_a_uuid4() -> None:
    with pytest.raises(RecordError):
        make(invocation_id="11111111-2222-1333-8444-555555555555")
    assert make(invocation_id=INVOCATION).invocation_id == INVOCATION


def test_sample_run_names_are_safe_names() -> None:
    with pytest.raises(RecordError) as caught:
        make(payload=sample_payload(root_table="a/b"))
    assert caught.value.code == "record_invalid"
    with pytest.raises(RecordError):
        make(payload=sample_payload(table_rows={"x;y": 1}))


# -- M1 -----------------------------------------------------------------------------------------


def test_a_stale_payload_digest_is_corrupt_even_when_the_id_matches() -> None:
    raw = raw_of(make())
    raw["payload"]["sample_size"] = 51  # the digest still names the old payload
    raw["id"] = records.record_id(raw)
    with pytest.raises(RecordError) as caught:
        records.decode(canonical_bytes(raw))
    assert caught.value.code == "record_corrupt"
    _clean(caught.value)


def test_a_duplicate_key_is_corrupt_even_when_the_last_value_verifies() -> None:
    text = records.encode(make()).decode("utf-8")
    assert text.startswith("{")
    doubled = '{"kind":"not-the-kind",' + text[1:]
    # Without the duplicate check json keeps the last "kind", which is the real one and verifies.
    assert records.decode(text).id == json.loads(doubled)["id"]
    with pytest.raises(RecordError) as caught:
        records.decode(doubled)
    assert caught.value.code == "record_corrupt"


# -- M2 -----------------------------------------------------------------------------------------


def _failures() -> list[Any]:
    def bad_payload() -> None:
        make(payload=sample_payload(sample_size="secret-/home/u"))

    def bad_label() -> None:
        make(labels={"status": "/home/u/secret"})

    def bad_producer() -> None:
        make(producer={"name": "/home/u"})

    def naive_time() -> None:
        make(created_at=T0.replace(tzinfo=None))

    def not_json() -> None:
        records.decode(b'{"secret": /home/u')

    def bad_utf8() -> None:
        records.decode(b"\xff\xfe")

    def bad_envelope() -> None:
        raw = raw_of(make())
        raw["labels"] = {"status": "/home/u/secret"}
        records.decode(canonical_bytes(seal(raw)))

    def bad_view() -> None:
        raw = raw_of(make())
        raw["payload"]["sample_size"] = "/home/u/secret"
        records.decode(canonical_bytes(seal(raw)))

    def bad_header() -> None:
        records.read_job_log(b'{"log_schema": "decoy-job-log/v1", "pipeline": 1}\n')

    def bad_log_text() -> None:
        records.read_job_log(b"\xff")

    def bad_export_header() -> None:
        list(records.read_export([b'{"type":"header","exported_at":1}']))

    def bad_log_bytes() -> None:
        list(
            records.export_lines(
                [], producer=PRODUCER, exported_at=T0, keep_paths=True, logs=[(INVOCATION, b"\xff")]
            )
        )

    def bad_log_owner() -> None:
        list(records.export_lines([], producer=PRODUCER, exported_at=T0, logs=[("inv-1", b"x")]))

    return [
        bad_payload, bad_label, bad_producer, naive_time, not_json, bad_utf8, bad_envelope,
        bad_view, bad_header, bad_log_text, bad_export_header, bad_log_bytes, bad_log_owner,
    ]  # fmt: skip


@pytest.mark.parametrize("call", _failures(), ids=lambda f: f.__name__)
def test_a_record_error_keeps_no_cause_or_context(call: Any) -> None:
    with pytest.raises(RecordError) as caught:
        call()
    _clean(caught.value)
    assert "secret" not in str(caught.value) and "/home" not in str(caught.value)


# -- M3 -----------------------------------------------------------------------------------------


def test_withheld_logs_have_their_own_header_field() -> None:
    log = b'{"x":1}\n'
    lines = list(
        records.export_lines([], producer=PRODUCER, exported_at=T0, logs=[(INVOCATION, log)])
    )
    header = next(records.read_export(lines))
    assert header.withheld_logs == (INVOCATION,) and header.unstripped == ()
    assert len(lines) == 1


def test_a_log_owner_must_be_an_invocation_id() -> None:
    with pytest.raises(RecordError) as caught:
        list(
            records.export_lines(
                [], producer=PRODUCER, exported_at=T0, keep_paths=True, logs=[("../x", b"a")]
            )
        )
    assert caught.value.code == "record_invalid"


def test_a_log_line_with_a_bad_owner_is_a_bad_line() -> None:
    lines = list(
        records.export_lines(
            [], producer=PRODUCER, exported_at=T0, keep_paths=True, logs=[(INVOCATION, b"a")]
        )
    )
    wrapper = json.loads(lines[1])
    wrapper["invocation_id"] = "../x"
    got = list(records.read_export([lines[0], json.dumps(wrapper)]))
    assert isinstance(got[1], records.BadLine) and got[1].code == "record_invalid"


def test_the_joblog_header_uses_the_shared_types() -> None:
    from decoy_engine.run_result import _model

    assert joblog.InvocationId is _model.InvocationId
