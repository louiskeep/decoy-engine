"""F22 5.2: one field declaration drives the descriptor writer and its strict reader.

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 7.2 row
`test_descriptor_canonical_and_strict` (slice a2; classes 1, 2, 4, 10). Every malformed stored
record is refused with a closed check token, so none can drive judge or adoption (a3 maps any
refusal to `publication_target_occupied`). Each cell breaks exactly one rule of a document the
reader otherwise accepts; the token pins which rule refused it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable
from typing import Any

import pytest

from tests.unit.publication import _doc

PREFIX = "descriptor_refused:"


def _codec() -> Any:
    from decoy_engine.publication import _descriptor

    return _descriptor


def _with(**over: Any) -> Callable[[], bytes]:
    return lambda: _doc.encode(_doc.doc(**over))


def _drop(key: str) -> Callable[[], bytes]:
    def make() -> bytes:
        document = _doc.doc()
        del document[key]
        return _doc.encode(document)

    return make


def _art(index: int = 1, drop: str | None = None, **over: Any) -> Callable[[], bytes]:
    def make() -> bytes:
        document = _doc.doc()
        entry = document["artifacts"][index]
        entry.update(over)
        if drop is not None:
            del entry[drop]
        return _doc.encode(_doc.resign(document))

    return make


def _vault(drop: str | None = None, **over: Any) -> Callable[[], bytes]:
    def make() -> bytes:
        document = _doc.doc()
        document["vault"].update(over)
        if drop is not None:
            del document["vault"][drop]
        return _doc.encode(document)

    return make


def _swap(old: bytes, new: bytes) -> Callable[[], bytes]:
    def make() -> bytes:
        raw = _doc.encode(_doc.doc())
        assert raw.count(old) == 1  # non-vacuity: the edit lands exactly once
        return raw.replace(old, new)

    return make


def _duplicate_top() -> bytes:
    raw = _doc.encode(_doc.doc())
    # Same key, same value: only the duplicate-key hook can tell this from the valid document.
    return b'{"engine_version":"0.22.0",' + raw[1:]


def _duplicate_in_artifact() -> bytes:
    raw = _doc.encode(_doc.doc())
    return raw.replace(b'{"path":"accounts.parquet"', b'{"rows":2,"path":"accounts.parquet"', 1)


def _unsorted() -> bytes:
    document = _doc.doc()
    document["artifacts"].reverse()
    return _doc.encode(_doc.resign(document))


def _duplicate_path() -> bytes:
    document = _doc.doc()
    document["artifacts"][0]["path"] = "customers.parquet"
    document["artifacts"].sort(key=lambda a: a["sha256"])
    return _doc.encode(_doc.resign(document))


def _pretty() -> bytes:
    return json.dumps(_doc.doc(), sort_keys=True, indent=1).encode("ascii")


def _forged_result_id() -> bytes:
    return _doc.encode(_doc.doc(result_id="f" * 64))


CELLS: dict[str, tuple[Callable[[], bytes], str]] = {
    # version
    "schema_missing": (_drop("schema"), "schema_missing"),
    "schema_next": (_with(schema="decoy-publication/v2"), "schema_unknown"),
    "schema_newline": (_with(schema="decoy-publication/v1\n"), "schema_unknown"),
    "schema_not_str": (_with(schema=1), "schema_unknown"),
    # fields
    "top_unknown_field": (_with(extra=1), "unknown_field"),
    "top_missing_field": (_drop("engine_version"), "missing_field"),
    "artifact_unknown_field": (_art(schema_digest="0" * 64), "unknown_field"),
    "artifact_missing_field": (_art(drop="rows"), "missing_field"),
    "vault_unknown_field": (_vault(anchor="x"), "unknown_field"),
    "vault_missing_field": (_vault(drop="size"), "missing_field"),
    # exact primitive types: bool is never an int
    "artifact_size_bool": (_art(size=True), "wrong_type"),
    "artifact_rows_bool": (_art(rows=False), "wrong_type"),
    "vault_size_bool": (_vault(size=True), "wrong_type"),
    "vault_entry_count_bool": (_vault(entry_count=True), "wrong_type"),
    "artifact_size_numeric_string": (_art(size="640"), "wrong_type"),
    "artifact_size_float": (_art(size=640.0), "wrong_type"),
    "operation_id_int": (_with(operation_id=42), "wrong_type"),
    "artifacts_not_list": (_with(artifacts={}), "wrong_type"),
    "vault_not_object": (_with(vault=[]), "wrong_type"),
    # bounded counts
    "artifact_size_negative": (_art(size=-1), "out_of_range"),
    "vault_entry_count_oversized": (_vault(entry_count=2**63), "out_of_range"),
    # closed tokens
    "unit_unknown": (_with(unit="managed_dataset/v1"), "bad_token"),
    "unit_newline": (_with(unit="immutable_directory/v1\n"), "bad_token"),
    "role_unknown": (_art(role="sidecar"), "bad_token"),
    "role_newline": (_art(role="table\n"), "bad_token"),
    # fullmatch shapes: a trailing newline passes a `$`-anchored match
    "operation_id_newline": (_with(operation_id="job-42.attempt:1\n"), "bad_token"),
    "operation_id_space": (_with(operation_id="job 42"), "bad_token"),
    "operation_id_empty": (_with(operation_id=""), "bad_token"),
    "operation_id_too_long": (_with(operation_id="a" * 129), "bad_token"),
    "artifact_sha256_newline": (_art(sha256="2" * 64 + "\n"), "bad_token"),
    "artifact_sha256_upper": (_art(sha256="A" * 64), "bad_token"),
    "vault_sha256_newline": (_vault(sha256="3" * 64 + "\n"), "bad_token"),
    "result_id_newline": (
        lambda: _doc.encode(_doc.doc(result_id=_doc.doc()["result_id"] + "\n")),
        "bad_token",
    ),
    "engine_version_newline": (_with(engine_version="0.22.0\n"), "bad_token"),
    # JSON
    "duplicate_key_top": (_duplicate_top, "duplicate_key"),
    "duplicate_key_artifact": (_duplicate_in_artifact, "duplicate_key"),
    "nan": (_swap(b'"size":640', b'"size":NaN'), "non_finite"),
    "infinity": (_swap(b'"size":640', b'"size":Infinity'), "non_finite"),
    "minus_infinity": (_swap(b'"size":640', b'"size":-Infinity'), "non_finite"),
    "not_json": (lambda: b'{"schema":', "not_json"),
    "not_utf8": (lambda: b'{"schema":"\xff"}', "not_json"),
    "not_object": (lambda: b"[]", "not_object"),
    # paths
    "path_absolute": (_art(path="/etc/customers.parquet"), "bad_path"),
    "path_dotdot": (_art(path="../customers.parquet"), "bad_path"),
    "path_inner_dotdot": (_art(path="a/../customers.parquet"), "bad_path"),
    "path_empty_component": (_art(path="a//customers.parquet"), "bad_path"),
    "path_empty": (_art(path=""), "bad_path"),
    "path_nul": (_art(path="a\x00b"), "bad_path"),
    "path_reserved": (_art(path="_decoy_publication.json"), "bad_path"),
    # document rules
    "artifacts_unsorted": (_unsorted, "artifacts_order"),
    "artifacts_duplicate_path": (_duplicate_path, "artifacts_order"),
    "result_id_forged": (_forged_result_id, "result_id_mismatch"),
    "not_canonical": (_pretty, "not_canonical"),
}


def test_the_base_document_is_accepted() -> None:
    codec = _codec()
    raw = _doc.encode(_doc.doc())
    got = codec.decode_descriptor(raw)
    # Non-vacuity for every cell below: the unedited document decodes, so a refusal is the edit's.
    assert got.result_id == _doc.doc()["result_id"]
    assert [a.path for a in got.artifacts] == ["accounts.parquet", "customers.parquet"]
    assert got.vault is not None and got.vault.size == 2048


@pytest.mark.parametrize("cell", sorted(CELLS))
def test_descriptor_canonical_and_strict(cell: str) -> None:
    codec = _codec()
    make, token = CELLS[cell]
    raw = make()
    assert raw != _doc.encode(_doc.doc())  # non-vacuity: the cell changed the document
    with pytest.raises(ValueError) as caught:
        codec.decode_descriptor(raw)
    assert str(caught.value) == PREFIX + token


def test_refusal_carries_no_value_and_no_chained_parse_error() -> None:
    codec = _codec()
    raw = b'{"schema":"VALMARK'
    with pytest.raises(ValueError) as caught:
        codec.decode_descriptor(raw)
    assert b"VALMARK" in raw  # non-vacuity: the marker is in the refused bytes
    assert "VALMARK" not in str(caught.value)
    # Raised outside the parse handler: a JSONDecodeError's `doc` would hold the bytes.
    assert caught.value.__context__ is None and caught.value.__cause__ is None


def _artifacts(codec: Any) -> list[Any]:
    return [
        codec.Artifact(role="table", path="customers.parquet", size=640, sha256="2" * 64, rows=3),
        codec.Artifact(role="table", path="accounts.parquet", size=512, sha256="1" * 64, rows=2),
    ]


def test_writer_round_trips_through_the_reader() -> None:
    codec = _codec()
    built = codec.build_descriptor(
        operation_id="job-42.attempt:1",
        artifacts=_artifacts(codec),
        vault=codec.DescriptorVault(sha256="3" * 64, size=2048, entry_count=7),
        engine_version="0.22.0",
    )
    raw = codec.encode_descriptor(built)
    assert raw == _doc.encode(_doc.doc())  # writer output is the hand-built canonical document
    assert codec.decode_descriptor(raw) == built
    assert codec.descriptor_digest(raw) == hashlib.sha256(raw).hexdigest()


def test_writer_sorts_and_escapes() -> None:
    codec = _codec()
    arts = [*_artifacts(codec)]
    arts.append(
        codec.Artifact(role="table", path="données/t.parquet", size=1, sha256="4" * 64, rows=0)
    )
    built = codec.build_descriptor(operation_id="op-1", artifacts=arts, vault=None)
    raw = codec.encode_descriptor(built)
    assert raw.isascii() and b"donn\\u00e9es/t.parquet" in raw
    assert [a.path for a in built.artifacts] == sorted(a.path for a in arts)
    assert codec.decode_descriptor(raw) == built


@pytest.mark.parametrize(
    ("field", "value", "token"),
    [
        ("size", True, "wrong_type"),
        ("rows", -1, "out_of_range"),
        ("sha256", "2" * 64 + "\n", "bad_token"),
        ("path", "../x.parquet", "bad_path"),
        ("role", "sidecar", "bad_token"),
    ],
)
def test_writer_refuses_through_the_same_declaration(field: str, value: Any, token: str) -> None:
    codec = _codec()
    good = codec.build_descriptor(operation_id="op-1", artifacts=_artifacts(codec), vault=None)
    codec.encode_descriptor(good)  # non-vacuity: the unedited descriptor encodes
    bad_art = dataclasses.replace(good.artifacts[0], **{field: value})
    bad = dataclasses.replace(good, artifacts=(bad_art, good.artifacts[1]))
    with pytest.raises(ValueError) as caught:
        codec.encode_descriptor(bad)
    assert str(caught.value) == PREFIX + token


def test_writer_refuses_a_bad_operation_id_and_duplicate_paths() -> None:
    codec = _codec()
    arts = _artifacts(codec)
    with pytest.raises(ValueError) as caught:
        codec.encode_descriptor(
            codec.build_descriptor(operation_id="op-1\n", artifacts=arts, vault=None)
        )
    assert str(caught.value) == PREFIX + "bad_token"
    with pytest.raises(ValueError) as caught:
        codec.build_descriptor(operation_id="op-1", artifacts=[arts[0], arts[0]], vault=None)
    assert str(caught.value) == PREFIX + "artifacts_order"
    assert arts[0] != arts[1]  # non-vacuity: only the repeated entry is refused


def test_result_id_is_the_artifacts_only() -> None:
    codec = _codec()
    arts = _artifacts(codec)
    one = codec.build_descriptor(operation_id="op-1", artifacts=arts, vault=None)
    two = codec.build_descriptor(
        operation_id="op-2",
        artifacts=list(reversed(arts)),
        vault=codec.DescriptorVault(sha256="3" * 64, size=1, entry_count=1),
    )
    assert one.result_id == two.result_id == codec.compute_result_id(arts)
    assert one.result_id == _doc.result_id(
        [_doc.artifact("accounts.parquet", rows=2, sha256="1" * 64, size=512), _doc.artifact()]
    )
    other = codec.compute_result_id([dataclasses.replace(arts[0], rows=4), arts[1]])
    assert other != one.result_id  # non-vacuity: a measured change moves the id
