"""F22 5.2 and 8: the `decoy-publication/v1` wire shape is frozen.

Plan `docs/plans/2026-10-30-f22-publication-replan.md` 7.2 row `test_descriptor_schema_snapshot`
(slice a2; class 10). A field added, removed, renamed or retyped in the one declaration turns this
red until the snapshot below changes in the same diff; a v1 change after GA is a new schema.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Any

from tests.unit.publication import _doc

# Hand-written canonical bytes: keys sorted, no whitespace, ASCII.
GOLDEN = (
    b'{"artifacts":[{"path":"accounts.parquet","role":"table","rows":2,"sha256":"'
    + b"1" * 64
    + b'","size":512},{"path":"customers.parquet","role":"table","rows":3,"sha256":"'
    + b"2" * 64
    + b'","size":640}],"engine_version":"0.22.0","operation_id":"job-42.attempt:1",'
    b'"result_id":"cb1459e733731d16cbdbcd6ede3761700f4849c4912b98a1658d042192ca2722",'
    b'"schema":"decoy-publication/v1","unit":"immutable_directory/v1","vault":{"entry_count":7,'
    b'"sha256":"' + b"3" * 64 + b'","size":2048}}'
)
GOLDEN_RESULT_ID = "cb1459e733731d16cbdbcd6ede3761700f4849c4912b98a1658d042192ca2722"
GOLDEN_DIGEST = "0a1630b1ffa7dc9fd4401795c7ef37bf970ac41199241b450ec5af4f7bdab69a"

DECLARATION = (
    ("schema", "exact:decoy-publication/v1"),
    ("unit", "one_of:immutable_directory/v1"),
    ("operation_id", "fullmatch:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}"),
    ("result_id", "fullmatch:[0-9a-f]{64}"),
    (
        "artifacts",
        "list:object:"
        "(path=rel_path,role=one_of:table,rows=count,sha256=fullmatch:[0-9a-f]{64},size=count)",
    ),
    ("vault", "nullable:object:(sha256=fullmatch:[0-9a-f]{64},size=count,entry_count=count)"),
    ("engine_version", "fullmatch:[0-9][0-9A-Za-z.+!_-]{0,63}"),
)


def _codec() -> Any:
    from decoy_engine.publication import _descriptor

    return _descriptor


def test_descriptor_schema_snapshot() -> None:
    codec = _codec()
    assert codec.SCHEMA == "decoy-publication/v1"
    assert codec.DESCRIPTOR_NAME == "_decoy_publication.json"
    assert codec.RESULT_DOMAIN == b"decoy-publication-result/v1\x00"
    assert codec.describe_declaration() == DECLARATION
    # The typed model and the declaration name the same fields in the same order.
    assert tuple(f.name for f in dataclasses.fields(codec.Descriptor)) == tuple(
        name for name, _ in DECLARATION
    )
    assert tuple(f.name for f in dataclasses.fields(codec.Artifact)) == (
        "path",
        "role",
        "rows",
        "sha256",
        "size",
    )
    assert tuple(f.name for f in dataclasses.fields(codec.DescriptorVault)) == (
        "sha256",
        "size",
        "entry_count",
    )


def test_golden_document_bytes_and_ids() -> None:
    codec = _codec()
    assert _doc.encode(_doc.doc()) == GOLDEN  # non-vacuity: the golden is the hand-built document
    assert hashlib.sha256(GOLDEN).hexdigest() == GOLDEN_DIGEST
    got = codec.decode_descriptor(GOLDEN)
    assert got.result_id == GOLDEN_RESULT_ID
    assert codec.encode_descriptor(got) == GOLDEN
    assert codec.descriptor_digest(GOLDEN) == GOLDEN_DIGEST
    assert codec.compute_result_id(got.artifacts) == GOLDEN_RESULT_ID
