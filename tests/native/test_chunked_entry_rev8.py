"""B1 rev8: the first-chunk ingest guard raises at the first `next()` on every
route, and a null-typed passthrough chunk keeps the first chunk's field."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.vault import VaultWriter
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    TABLE,
    key_provider,
    make_config,
    passthrough,
    truncate,
    vault_key,
)
from tests.native.test_chunked_entry_rev7 import _DelegatingAdapter


def _call(fn: Any, config: dict[str, Any], chunks: list[pa.Table], **kw: Any) -> Any:
    return fn(
        config,
        list(chunks),
        table=TABLE,
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        **kw,
    )


_ROUTES = ["native_eligible", "forced_oracle", "public_oracle"]


def _start(route: str, config: dict[str, Any], chunks: list[pa.Table], **kw: Any) -> Any:
    if route == "public_oracle":
        return _call(run_mask_pipeline_chunked, config, chunks, **kw)
    if route == "forced_oracle":
        return _call(run_mask_chunked, config, chunks, adapter=_DelegatingAdapter(), **kw)
    return _call(run_mask_chunked, config, chunks, **kw)


@pytest.mark.parametrize("route", _ROUTES)
def test_first_chunk_ingest_guard_raises_at_first_next_not_at_call(route: str) -> None:
    config = make_config([truncate("n", 2)])
    first = pa.table({"n": pa.array([1, None, 3], pa.int64())})
    sink: list[Any] = []
    vault = VaultWriter(vault_key())
    gen = _start(route, config, [first], chunk_result_sink=sink, vault_writer=vault)
    with pytest.raises(Exception) as info:
        next(gen)
    assert getattr(info.value, "code", None) == "null_bearing_int_unsupported"
    assert sink == []
    assert vault._entries == set()


def _field_chunks() -> list[pa.Table]:
    field = pa.field("p", pa.int64(), nullable=False, metadata={b"unit": b"cm"})
    first = pa.table({"p": pa.array([1, 2], pa.int64())}, schema=pa.schema([field]))
    later = pa.table({"p": pa.nulls(2)})
    return [first, later]


@pytest.mark.parametrize("route", ["native_eligible", "forced_oracle"])
def test_null_typed_passthrough_chunk_keeps_the_first_chunks_field(route: str) -> None:
    config = make_config([passthrough("p")])
    out = list(_start(route, config, _field_chunks()))
    assert len(out) == 2
    for chunk in out:
        field = chunk.schema.field("p")
        assert field.type == pa.int64()
        assert field.nullable is False
        assert field.metadata == {b"unit": b"cm"}


def test_routes_yield_identical_fields_for_a_null_typed_passthrough_chunk() -> None:
    config = make_config([passthrough("p")])
    native = list(_start("native_eligible", config, _field_chunks()))
    oracle = list(_start("forced_oracle", config, _field_chunks()))
    assert [t.schema for t in native] == [t.schema for t in oracle]
    assert [t.schema.field("p").metadata for t in native] == [
        t.schema.field("p").metadata for t in oracle
    ]
