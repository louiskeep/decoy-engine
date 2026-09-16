"""Task 4.4 C0: a recursive walk of the plan + snapshot object graph proves
no `KeyProvider` object and no secret bytes appear anywhere on either
`PhysicalPlan` or `PhysicalPlanInputs`.

The resolved `KeyProvider` and mask-key bytes live exclusively in the
runtime `ShadowContext` (`_shadow_context.py`); this test proves the frozen
plan/snapshot never carry them. A real `SecretKeyProvider` with a KNOWN
secret is used so the walk can search for the exact bytes value, not a
heuristic length threshold (which would risk false negatives against a
differently-sized secret and false positives against the unrelated 8-byte
`job_seed`, which is a legitimate, non-secret field already on `Plan`).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.keyprovider import KeyProvider, SecretKeyProvider

_ENGINE_VERSION = "shadow-no-secret-test"
_SECRET = bytes(range(1, 33))  # 32 known bytes, distinguishable from job_seed (8 bytes)


def _build_plan_and_inputs(tmp_path: Path):
    source = pa.table(
        {
            "h": pa.array(["a", "b", "c"], type=pa.string()),
            "r": pa.array(["x", "y", "z"], type=pa.string()),
            # Task 4.6 slice 1: a real, admitted faker column, so the walk
            # below also crawls a live `PoolBinding` -- not just KeyBinding.
            "f": pa.array(["alice", "bob", "carol"], type=pa.string()),
        }
    )
    path = tmp_path / "t.parquet"
    pq.write_table(source, path)
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {"name": "h", "strategy": "hash", "namespace": "n"},
                        {"name": "r", "strategy": "redact"},
                        {
                            "name": "f",
                            "strategy": "faker",
                            "provider": "person_first_name",
                            "deterministic": True,
                            "namespace": "ns_f",
                            "pool_size": 20,
                        },
                    ],
                }
            ],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version=_ENGINE_VERSION)
    plan = compile_physical_plan(inputs)
    return plan, inputs


def test_faker_node_binds_a_real_pool_binding(tmp_path: Path) -> None:
    """Guards the fixture above against silently going vacuous: the faker
    column must actually reach admission (a real `PoolBinding`), or the
    no-secret walk below would be crawling nothing new."""
    plan, _ = _build_plan_and_inputs(tmp_path)
    node = next(n for table in plan.tables for n in table.nodes if n.strategy == "faker")
    assert node.execution is not None
    assert node.execution.pool_binding is not None
    assert node.execution.pool_binding.provider == "person_first_name"
    assert node.execution.pool_binding.plan_pool_size == 20


def _walk(obj: Any, *, _seen: set[int] | None = None, _depth: int = 0) -> list[Any]:
    """Yield every leaf reachable through dataclass fields, mappings, lists,
    and tuples, bounded in depth so an opaque production object (a `Plan`,
    `Profile`, `pa.Table`, `pa.Schema`, `ProviderRegistry`, ...) is visited
    as ONE leaf rather than crawled cell-by-cell."""
    if _seen is None:
        _seen = set()
    if _depth > 12:  # pragma: no cover - defensive bound, never hit by these fixtures
        return [obj]
    leaves: list[Any] = []
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        oid = id(obj)
        if oid in _seen:
            return []
        _seen.add(oid)
        for f in dataclasses.fields(obj):
            leaves.extend(_walk(getattr(obj, f.name), _seen=_seen, _depth=_depth + 1))
        return leaves
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            leaves.extend(_walk(key, _seen=_seen, _depth=_depth + 1))
            leaves.extend(_walk(value, _seen=_seen, _depth=_depth + 1))
        return leaves
    if isinstance(obj, (list, tuple)):
        for item in obj:
            leaves.extend(_walk(item, _seen=_seen, _depth=_depth + 1))
        return leaves
    # Defensive (dennis LOW-1): a decoy_engine object that is NOT a dataclass
    # could still hold a `KeyProvider` or secret in a plain / `__slots__`
    # attribute and would otherwise be visited as one opaque leaf. Crawl our
    # own objects' attributes one level deeper (bounded by `_depth`), while
    # leaving FOREIGN objects (pyarrow tables/schemas, stdlib) opaque so the
    # walk does not descend huge production data structures. The object itself
    # is still yielded as a leaf so the `isinstance(..., KeyProvider)` scan
    # fires on it too.
    if (type(obj).__module__ or "").startswith("decoy_engine"):
        attrs: dict[str, Any] = {}
        if hasattr(obj, "__dict__"):
            attrs.update(vars(obj))
        for slot in getattr(type(obj), "__slots__", ()) or ():
            if hasattr(obj, slot):
                attrs[slot] = getattr(obj, slot)
        if attrs:
            oid = id(obj)
            if oid in _seen:
                return []
            _seen.add(oid)
            leaves.append(obj)
            for value in attrs.values():
                leaves.extend(_walk(value, _seen=_seen, _depth=_depth + 1))
            return leaves
    return [obj]


def test_plan_and_snapshot_carry_no_key_provider_or_secret_bytes(tmp_path: Path) -> None:
    plan, inputs = _build_plan_and_inputs(tmp_path)
    leaves = _walk(plan) + _walk(inputs)

    for leaf in leaves:
        assert not isinstance(leaf, KeyProvider), (
            f"a KeyProvider object reached the plan/snapshot graph: {leaf!r}"
        )
        assert not isinstance(leaf, SecretKeyProvider)
        if isinstance(leaf, (bytes, bytearray)):
            assert bytes(leaf) != _SECRET, "the resolved secret bytes leaked onto the plan/snapshot"


def test_shadow_context_is_the_only_place_the_secret_lives(tmp_path: Path) -> None:
    plan, inputs = _build_plan_and_inputs(tmp_path)
    provider = SecretKeyProvider(secret=_SECRET, key_version="v1")
    ctx = ShadowContext.from_key_provider(plan=inputs.plan, key_provider=provider)

    # ShadowContext itself legitimately carries a derived key (HKDF over the
    # secret, per `SecretKeyProvider.mask_key()` -- never the raw secret
    # bytes themselves, and never the provider object).
    assert isinstance(ctx.mask_key, bytes)
    assert not isinstance(ctx, KeyProvider)

    # The plan/snapshot built independently of `ctx` still carry nothing.
    leaves = _walk(plan) + _walk(inputs)
    for leaf in leaves:
        assert not isinstance(leaf, KeyProvider)
        if isinstance(leaf, (bytes, bytearray)):
            assert bytes(leaf) != _SECRET
            assert bytes(leaf) != ctx.mask_key
