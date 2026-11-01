"""REMAP minting under resource exhaustion: the same error object, and no key in any engine frame.

Two properties meet here. The typed FK seam (`_join.fk_output_array`) lets `MemoryError`,
`pyarrow.ArrowMemoryError` included, propagate as the very object raised instead of turning it
into the `out_of_core_fk_key_dtype_unsupported` refusal. The secret-material census requires
that no `decoy_engine` frame a traceback keeps holds the mask key. Every REMAP minting route
(the whole-child oracle `_remap_values`, the shared `mint_remap_values`, both joiners'
`_batch_remap_values`) reaches the seam with the key live, so an allocation failure there is the
case where both must hold at once. The failure is injected at the seam's value build only (a
`pa` stand-in in `_join` whose `array` raises), so every other Arrow call runs for real.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.out_of_core import _batch_join
from decoy_engine.execution.out_of_core import _join as join_mod
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge
from tests.fileaccess.census_support import leaks

CANARY = b"REMAP-OOM-CANARY-" + b"k" * 15

_SEED = ColumnSeed(
    namespace="cust",
    strategy="hash",
    provider="hash",
    backend_type="faker",
    backend_version="v",
    cardinality_mode="reuse",
    deterministic=True,
    provider_config=(),
    coherent_with=(),
)

_EDGE = RelationshipEdge(
    parent_table="customers",
    parent_columns=("customer_id",),
    child_table="orders",
    child_columns=("customer_id",),
    namespace="cust",
    orphan_policy=OrphanPolicy.REMAP,
)

_PLAN = SimpleNamespace(
    seed_envelope=SeedEnvelope(
        job_seed=b"\x00" * 8,
        per_table=(
            ("customers", TableSeed(per_column=(("customer_id", _SEED),), per_group=())),
            ("orders", TableSeed(per_column=(("customer_id", _SEED),), per_group=())),
        ),
    )
)

_KEYS = {
    "int": pa.array([1, 2, None], pa.int64()),
    "timestamp_ns": pa.array(
        [dt.datetime(2026, 1, 1), None, dt.datetime(2026, 1, 2)], pa.timestamp("ns")
    ),
}


class _ArrowWithExhaustedBuild:
    """`pa` for the seam module: the value build runs out of memory, everything else is real."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def array(self, *_a: Any, **_k: Any) -> Any:
        raise self._exc

    def __getattr__(self, name: str) -> Any:
        return getattr(pa, name)


def _whole_child(key: pa.Array) -> Any:
    return _batch_join._remap_values(_PLAN, _EDGE, pa.table({"customer_id": key}), mask_key=CANARY)


def _shared(key: pa.Array) -> Any:
    return _batch_join.mint_remap_values([key], [_SEED], CANARY)


def _batch_joiner(key: pa.Array) -> Any:
    joiner = object.__new__(_batch_join.ChildFkBatchJoiner)
    joiner._edge, joiner._remap_seeds, joiner._job_seed = _EDGE, (_SEED,), CANARY
    return joiner._batch_remap_values(pa.record_batch({"customer_id": key}))


def _stream_joiner(key: pa.Array) -> Any:
    joiner = object.__new__(StreamFkJoiner)
    joiner._edge, joiner._remap_seeds, joiner._job_seed = _EDGE, (_SEED,), CANARY
    return joiner._batch_remap_values(pa.record_batch({"__decoy_src_0": key}))


_ROUTES = {
    "whole_child": _whole_child,
    "shared": _shared,
    "batch_joiner": _batch_joiner,
    "stream_joiner": _stream_joiner,
}


@pytest.mark.parametrize("key_kind", sorted(_KEYS))
@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize("exc_type", [pa.ArrowMemoryError, MemoryError], ids=lambda t: t.__name__)
def test_exhaustion_in_remap_minting_propagates_unchanged_and_keeps_no_key(
    route: str, key_kind: str, exc_type: type[BaseException], monkeypatch: pytest.MonkeyPatch
) -> None:
    exc = exc_type("injected: allocation failed")
    monkeypatch.setattr(join_mod, "pa", _ArrowWithExhaustedBuild(exc))
    with pytest.raises(MemoryError) as raised:
        _ROUTES[route](_KEYS[key_kind])
    assert raised.value is exc, (type(raised.value), getattr(raised.value, "code", None))
    assert leaks(raised.value, CANARY) == []


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_the_routes_mint_with_the_key_when_nothing_fails(route: str) -> None:
    # The premise of the cells above: each route really reaches the seam with this key.
    (minted,) = _ROUTES[route](_KEYS["int"])
    assert len(minted) == 3
