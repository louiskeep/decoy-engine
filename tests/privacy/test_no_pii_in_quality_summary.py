"""engine-v2 S10 privacy gate (R18): quality_summary never contains source PII.

The `sampled_values` evidence block is the only place the manifest carries raw
output values, and it must be SYNTHETIC ONLY -- read from the masked output of
non-passthrough columns, never from the source. This pins that property end to
end through the PostValidationRunner on an SSN-shaped fixture: no sampled value
equals any source SSN.
"""

from __future__ import annotations

from types import SimpleNamespace

import pyarrow as pa

from decoy_engine.execution import ExecutionResult
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from decoy_engine.validation.post import PostValidationRunner

_N = 1000


def test_sampled_values_never_contain_source_ssn() -> None:
    source_ssns = [f"{i:03d}-00-0000" for i in range(_N)]  # the raw PII
    masked_ssns = [f"{i:03d}-99-9999" for i in range(_N)]  # what masking produced
    seed = ColumnSeed(
        namespace="ssn_ns",
        strategy="hash",
        provider="person_email",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x00" * 8,
            per_table=(("t", TableSeed(per_column=(("ssn", seed),), per_group=())),),
        )
    )
    summary = PostValidationRunner().run(
        plan=plan,  # type: ignore[arg-type]
        execution_result=ExecutionResult(
            outputs={"t": pa.table({"ssn": masked_ssns})}, warnings=()
        ),
        sources={"t": pa.table({"ssn": source_ssns})},
        profile=SimpleNamespace(tables=()),  # type: ignore[arg-type]
        registry=get_default_registry(),
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=NamespaceRegistry(bindings=()),
        config={"post_validation": True},
    )
    assert summary is not None
    sampled = summary.sampled_values["t.ssn"]
    assert sampled, "sampled_values should carry synthetic spot-check rows"
    source_set = set(source_ssns)
    assert not (set(sampled) & source_set), "a source SSN leaked into sampled_values"
    # And leakage did not fire (masked output shares no value with the source).
    assert "leakage" not in summary.failed_checks
