"""V1P-4.1: the Avro/ORC identities are registered and pinned append-only (AT-Q1)."""

from __future__ import annotations

import json
from pathlib import Path

from decoy_engine import io as engine_io
from decoy_engine import reasons

_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "f29" / "reason-manifest-v1.7-avro-orc.json"
)
_OWNERS = {
    "avro_container_malformed": engine_io.AvroMalformed,
    "avro_schema_unsupported": engine_io.AvroSchemaUnsupported,
    "avro_block_too_large": engine_io.AvroBlockTooLarge,
    "orc_file_malformed": engine_io.OrcMalformed,
    "orc_type_unsupported": engine_io.OrcTypeUnsupported,
    "target_column_type_unsupported": engine_io.TargetColumnTypeUnsupported,
    "target_schema_mismatch": engine_io.TargetSchemaMismatch,
}


def test_identities_are_registered_and_pinned() -> None:
    pinned = json.loads(_FIXTURE.read_text())
    live = reasons.manifest()
    entries = [
        e
        for e in live["entries"]
        if (e["namespace"], e["code"]) in {("runtime", c) for c in _OWNERS}
    ]
    assert pinned["entries"] == sorted(entries, key=lambda e: e["code"])
    for code, cls in _OWNERS.items():
        spec = reasons.find("runtime", code)
        assert spec is not None and spec.owner == cls.__name__
        assert cls.code == code
        assert spec.since == pinned["registry_version"]
