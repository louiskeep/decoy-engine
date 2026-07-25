"""MG-3 / M3 (2026-05-31): conditional `when:` end-to-end through the
PandasExecutionAdapter.

Locks the full pipeline: ColumnSeed.when -> runner gate -> handler
runs on matching rows -> writeback. The plan-compile rejection for
when + coherent_with is covered by the unit cell
`test_when_combined_with_coherent_with_rejected_at_compile`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pyarrow as pa

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry


def _seed_envelope(table: str, col: str, col_seed: ColumnSeed) -> SimpleNamespace:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x07" * 8,
            per_table=((table, TableSeed(per_column=((col, col_seed),), per_group=())),),
        )
    )


def _col(when: str | None) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="redact",
        provider="x_nobackend",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(),
        coherent_with=(),
        when=when,
    )


def test_when_pipeline_e2e_only_denied_rows_redacted():
    adapter = PandasExecutionAdapter()
    result = adapter.run(
        _seed_envelope("t", "email", _col("consent_status == 'denied'")),
        {
            "t": pa.table(
                {
                    "email": ["a@x.com", "b@x.com", "c@x.com"],
                    "consent_status": ["granted", "denied", "denied"],
                }
            )
        },
        registry=get_default_registry(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
    )
    out = result.outputs["t"]
    assert out.column("email").to_pylist() == ["a@x.com", "REDACTED", "REDACTED"]
    # Sidecar column untouched.
    assert out.column("consent_status").to_pylist() == ["granted", "denied", "denied"]


def test_when_manifest_carries_when_field():
    """The plan serializer round-trips the when field onto the manifest
    so a downstream consumer can audit which columns gate on what."""
    from decoy_engine.plan._serialize import (
        _column_seed_from_dict,
        _column_seed_to_dict,
    )

    seed_in = _col("flag == 1")
    payload = _column_seed_to_dict(seed_in)
    assert payload["when"] == "flag == 1"
    seed_out = _column_seed_from_dict({**payload, "namespace": None})
    assert seed_out.when == "flag == 1"


def test_when_none_round_trips_as_absent_key():
    """A column without when must not emit the key, keeping legacy
    manifests byte-identical."""
    from decoy_engine.plan._serialize import _column_seed_to_dict

    payload = _column_seed_to_dict(_col(None))
    assert "when" not in payload
