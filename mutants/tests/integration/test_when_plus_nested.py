"""MG-3 combined cell (2026-05-31): `when:` + `nested` composition.

Composition order: when (row gate) FIRST, then nested (path traversal
on surviving rows). Locks the contract that a row excluded by `when`
keeps its full JSON shape unchanged, including the targeted leaf.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pyarrow as pa

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry


def _envelope(col_seed: ColumnSeed) -> SimpleNamespace:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x07" * 8,
            per_table=(("t", TableSeed(per_column=(("metadata", col_seed),), per_group=())),),
        )
    )


def _nested_seed(*, target: str, child_strategy: str, when: str | None) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="nested",
        provider="x_nobackend",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(
            ("strategy", child_strategy),
            ("target", target),
        ),
        coherent_with=(),
        when=when,
    )


def test_when_consent_denied_plus_nested_metadata_phone_redact():
    """Only rows where consent is denied get their nested $.phone
    leaf redacted. Granted rows keep the original cell verbatim."""
    adapter = PandasExecutionAdapter()
    rows = [
        json.dumps({"phone": "555-0100", "note": "ok"}),
        json.dumps({"phone": "555-0200", "note": "ok"}),
        json.dumps({"phone": "555-0300", "note": "ok"}),
    ]
    result = adapter.run(
        _envelope(
            _nested_seed(
                target="$.phone",
                child_strategy="redact",
                when="consent_status == 'denied'",
            )
        ),
        {
            "t": pa.table(
                {
                    "metadata": rows,
                    "consent_status": ["granted", "denied", "denied"],
                }
            )
        },
        registry=get_default_registry(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
    )
    out = result.outputs["t"].column("metadata").to_pylist()

    # Row 0: granted -> when gate rejects -> nested NEVER runs ->
    # cell is byte-identical to input.
    assert out[0] == rows[0]
    assert json.loads(out[0])["phone"] == "555-0100"

    # Rows 1 + 2: denied -> when gate passes -> nested redacts $.phone.
    for i in (1, 2):
        parsed = json.loads(out[i])
        assert parsed["phone"] == "REDACTED"
        # Sibling note preserved.
        assert parsed["note"] == "ok"
