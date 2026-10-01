"""Final-gate findings on `run_mask_chunked`: caller-registry Faker admission and
explicit (non-ambient) ingest-guard suppression."""

from __future__ import annotations

import asyncio
import datetime
from typing import Any

import pyarrow as pa

from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.execution.native._dispatch import NativeRouteEvidence
from decoy_engine.providers_v2 import get_default_registry
from tests.native._chunked_entry_support import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    TABLE,
    categorical,
    faker_col,
    key_provider,
    make_config,
    split,
    string_source,
)


class _DateAdapter:
    """A poolable adapter whose `person_first_name` values are dates, not strings."""

    backend_type = "test_date"
    backend_version = "1"

    def generate(self, provider: str, *, spec: Any, source_value: bytes | None = None) -> Any:
        return datetime.date(2000, 1, 1)

    def generate_batch(self, provider: str, *, spec: Any, count: int) -> list[datetime.date]:
        return [datetime.date(2000, 1, 1) + datetime.timedelta(days=i) for i in range(count)]

    def capability_matrix(self, provider: str) -> Any:
        return get_default_registry().get_capabilities(provider)


def _run(fn: Any, config: dict[str, Any], chunks: list[pa.Table], **kw: Any) -> list[pa.Table]:
    return list(
        fn(
            config,
            list(chunks),
            table=TABLE,
            engine_version=ENGINE_VERSION,
            key_provider=key_provider(),
            **kw,
        )
    )


@NEEDS_COMPANION
def test_caller_registry_non_string_provider_output_routes_to_the_oracle() -> None:
    default = get_default_registry()
    registry = default.override(
        "person_first_name", _DateAdapter(), default.get_capabilities("person_first_name")
    )
    source = pa.table({"f": pa.array(["a", "b", None, "c", "d"], pa.string())})
    config = make_config([faker_col("f")])
    expected = _run(run_mask_pipeline_chunked, config, split(source, 2), registry=registry)
    evidence: list[NativeRouteEvidence] = []
    actual = _run(
        run_mask_chunked,
        config,
        split(source, 2),
        registry=registry,
        route_evidence_sink=evidence,
    )
    assert [t.to_pydict() for t in actual] == [t.to_pydict() for t in expected]
    assert [t.schema for t in actual] == [t.schema for t in expected]
    assert evidence[0].native_admitted is False
    assert evidence[0].reroute_reason == "faker_provider_output_not_string:f:person_first_name"


def _int_null_categorical() -> tuple[dict[str, Any], pa.Table]:
    return (
        make_config([categorical("c")]),
        pa.table({"c": pa.array([1, None, 3], pa.int64())}),
    )


def _inner_code() -> str | None:
    config, source = _int_null_categorical()
    try:
        _run(run_mask_chunked, config, [source])
    except Exception as exc:
        return getattr(exc, "code", type(exc).__name__)
    return None


def _redact() -> dict[str, Any]:
    return {"name": "s", "strategy": "redact"}


class _ReentrantAdapter:
    """A custom adapter whose `run` re-enters `run_mask_chunked` on other input."""

    def __init__(self, *, via_task: bool) -> None:
        self.via_task = via_task
        self.inner: list[str | None] = []

    def run(self, *args: Any, **kwargs: Any) -> Any:
        if self.via_task:

            async def _in_task() -> str | None:
                return await asyncio.create_task(_coro())

            async def _coro() -> str | None:
                return _inner_code()

            self.inner.append(asyncio.run(_in_task()))
        else:
            self.inner.append(_inner_code())
        return PandasExecutionAdapter().run(*args, **kwargs)


def test_reentrant_run_mask_chunked_in_a_custom_adapter_still_runs_its_guards() -> None:
    adapter = _ReentrantAdapter(via_task=False)
    config = make_config([_redact()])
    source = string_source(4).select(["s"])
    _run(run_mask_chunked, config, [source], adapter=adapter)
    assert adapter.inner == ["null_bearing_int_unsupported"]


def test_task_created_inside_the_adapter_still_runs_guards() -> None:
    adapter = _ReentrantAdapter(via_task=True)
    config = make_config([_redact()])
    source = string_source(4).select(["s"])
    _run(run_mask_chunked, config, [source], adapter=adapter)
    assert adapter.inner == ["null_bearing_int_unsupported"]
