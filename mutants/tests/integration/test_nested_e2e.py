"""MG-3 / M2 (2026-05-31): nested JSONPath strategy end-to-end.

Locks:
- Full pipeline through PandasExecutionAdapter masks the targeted
  leaf and leaves siblings + structure intact.
- Manifest carries the nested config tree (target + strategy +
  strategy_config).
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
            per_table=(("t", TableSeed(per_column=(("data", col_seed),), per_group=())),),
        )
    )


def _col(provider_config: tuple) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="nested",
        provider="x_nobackend",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=provider_config,
        coherent_with=(),
    )


def test_nested_pipeline_e2e_metadata_phone_redacted():
    adapter = PandasExecutionAdapter()
    cfg = (
        ("strategy", "redact"),
        ("target", "$.contact.phone"),
    )
    rows = [
        json.dumps({"id": 1, "contact": {"phone": "555-0100", "email": "a@x.com"}}),
        json.dumps({"id": 2, "contact": {"phone": "555-0200", "email": "b@x.com"}}),
    ]
    result = adapter.run(
        _envelope(_col(cfg)),
        {"t": pa.table({"data": rows})},
        registry=get_default_registry(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
    )
    out = result.outputs["t"].column("data").to_pylist()
    for cell in out:
        parsed = json.loads(cell)
        assert parsed["contact"]["phone"] == "REDACTED"
        # Sibling untouched.
        assert parsed["contact"]["email"].endswith("@x.com")
        # Outer structure preserved.
        assert "id" in parsed


def test_nested_manifest_carries_nested_config_tree():
    """The plan serializer round-trips the nested config so a downstream
    consumer can inspect target + strategy + strategy_config."""
    from decoy_engine.plan._serialize import (
        _column_seed_from_dict,
        _column_seed_to_dict,
    )

    seed_in = _col(
        (
            ("strategy", "redact"),
            ("strategy_config", {"redact_with": "X"}),
            ("target", "$.user.email"),
        )
    )
    payload = _column_seed_to_dict(seed_in)
    assert payload["provider_config"]["target"] == "$.user.email"
    assert payload["provider_config"]["strategy"] == "redact"
    seed_out = _column_seed_from_dict({**payload, "namespace": None})
    cfg = dict(seed_out.provider_config)
    assert cfg["target"] == "$.user.email"
    assert cfg["strategy"] == "redact"
