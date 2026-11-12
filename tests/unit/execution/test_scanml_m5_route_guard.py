"""SCAN-ML M5 (AT-M5a): routes that run no post-scan refuse model post-scan up front.

`run_sequential`, `run_fk_out_of_core`, `run_mask_pipeline_chunked`, native `run_mask_chunked` and
`run_native_or_oracle_chunked` mask without running the post-scan, so a config that asks for a model
post-scan detector or `ner_findings: fail` would publish unchecked output there. Each refuses such a
config with `PostScanRouteError` (`route.post_scan_models_unsupported`) before a source is read;
regex-only `integrity: true` keeps today's silent skip. `run_pipeline`'s resident auto-chunk lane
runs the post-scan itself, so it must keep running there.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
from collections.abc import Iterator, Mapping
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.config import PipelineConfig
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _b6a_support as b6a

_GUARD_MODULE = "decoy_engine.verification.postmask._route_guard"
_CODE = "post_scan_models_unsupported"

MODEL_NER = {"post_scan": {"integrity": True, "detectors": ["regex", "ner"]}}
NER_FAIL = {"post_scan": {"integrity": True, "detectors": ["regex", "ner"], "ner_findings": "fail"}}
REFUSED = {"model_ner": MODEL_NER, "ner_findings_fail": NER_FAIL}
REGEX_ONLY = {"post_scan": {"integrity": True}}


def _guard_module() -> Any:
    assert importlib.util.find_spec(_GUARD_MODULE) is not None, (
        "the post-scan route guard is missing"
    )
    return importlib.import_module(_GUARD_MODULE)


def _error_class() -> type[Exception]:
    return _guard_module().PostScanRouteError


def _summary() -> str:
    spec = reasons.find("route", _CODE)
    assert spec is not None, "route.post_scan_models_unsupported is not registered"
    return spec.summary


class _PastGuardError(Exception):
    """Raised by the statement right after the guard: the entry went on with its existing body."""


def _past_guard(*_args: Any, **_kwargs: Any) -> Any:
    raise _PastGuardError


class _SpySource(Mapping[str, Any]):
    """A source mapping that records every read."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    def __getitem__(self, key: str) -> Any:
        self.reads.append(f"get:{key}")
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        self.reads.append("iter")
        return iter(())

    def __len__(self) -> int:
        self.reads.append("len")
        return 0

    def __contains__(self, key: object) -> bool:
        self.reads.append("contains")
        return False


class _SpyChunks:
    def __init__(self) -> None:
        self.reads = 0

    def __iter__(self) -> Iterator[pa.Table]:
        self.reads += 1
        return iter(())


def _sequential_call(config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    from decoy_engine.execution import _sequential

    monkeypatch.setattr(_sequential, "declared_route_guard", _past_guard)
    loads: list[str] = []

    def loader(table: str) -> pa.Table:
        loads.append(table)
        raise AssertionError("a source was read")

    def call() -> Any:
        return _sequential.run_sequential(
            object(),
            object(),
            loader,
            registry=object(),
            relationship_graph=object(),
            namespace_registry=object(),
            config=config,
        )

    return call, loads


def _ooc_call(config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    from decoy_engine.execution.out_of_core import _runner

    monkeypatch.setattr(_runner, "declared_route_guard", _past_guard)
    sources = _SpySource()

    def call() -> Any:
        return _runner.run_fk_out_of_core(
            object(), sources, registry=object(), relationship_graph=object(), config=config
        )

    return call, sources.reads


def _patch_subset_check(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "decoy_engine.config._subset_run",
        "decoy_engine.execution._chunked",
        "decoy_engine.execution._chunked_oracle",
        "decoy_engine.execution.native._dispatch",
    ):
        monkeypatch.setattr(
            importlib.import_module(name), "refuse_unapplied_subset", _past_guard, raising=False
        )


def _chunked_call(entry: str, config: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    from decoy_engine.execution import _chunked
    from decoy_engine.execution.native import _chunked_entry, _dispatch

    _patch_subset_check(monkeypatch)
    fn = {
        "run_mask_pipeline_chunked": _chunked.run_mask_pipeline_chunked,
        "run_mask_chunked": _chunked_entry.run_mask_chunked,
        "run_native_or_oracle_chunked": _dispatch.run_native_or_oracle_chunked,
    }[entry]
    chunks = _SpyChunks()

    def call() -> Any:
        return fn(config, chunks, table="t", engine_version="v")

    return call, chunks


ENTRIES = (
    "run_sequential",
    "run_fk_out_of_core",
    "run_mask_pipeline_chunked",
    "run_mask_chunked",
    "run_native_or_oracle_chunked",
)


def _entry(entry: str, config: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    """`(call, reads)`: `reads()` is truthy once the entry touched a source."""
    if entry == "run_sequential":
        call, loads = _sequential_call(config, monkeypatch)
        return call, lambda: list(loads)
    if entry == "run_fk_out_of_core":
        call, reads = _ooc_call(config, monkeypatch)
        return call, lambda: list(reads)
    call, chunks = _chunked_call(entry, config, monkeypatch)
    return call, lambda: chunks.reads


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("config_name", sorted(REFUSED))
def test_entry_refuses_model_post_scan_before_reading_a_source(
    entry: str, config_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _error_class()
    call, reads = _entry(entry, REFUSED[config_name], monkeypatch)
    with pytest.raises(error) as caught:
        call()
    assert not reads()
    assert caught.value.code == _CODE
    assert caught.value.reason == f"route.{_CODE}"


@pytest.mark.parametrize("entry", ENTRIES)
def test_refusal_text_is_the_fixed_summary_with_no_chain(
    entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _error_class()
    call, _ = _entry(entry, NER_FAIL, monkeypatch)
    try:
        raise ValueError("an unrelated failure the caller is handling")
    except ValueError:
        with pytest.raises(error) as caught:
            call()
    assert str(caught.value) == _summary()
    assert caught.value.message == _summary()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert entry not in str(caught.value)


@pytest.mark.parametrize("entry", ENTRIES)
def test_regex_only_integrity_goes_on_with_the_existing_body(
    entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _guard_module()
    call, reads = _entry(entry, REGEX_ONLY, monkeypatch)
    with pytest.raises(_PastGuardError):
        call()
    assert not reads()


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"post_scan": {"integrity": False}},
        REGEX_ONLY,
        {"post_scan": {"integrity": True, "detectors": ("regex",), "ner_findings": "warn"}},
        # The plan's condition needs `integrity: true`; validation refuses this shape earlier.
        {"post_scan": {"integrity": False, "detectors": ["regex", "ner"]}},
    ],
)
def test_guard_admits_configs_without_a_model_post_scan(config: dict[str, Any]) -> None:
    assert _guard_module().refuse_unchecked_route(config, "sequential") is None


@pytest.mark.parametrize(
    "config",
    [
        MODEL_NER,
        NER_FAIL,
        {"post_scan": {"integrity": True, "detectors": ("regex", "classifier")}},
        {"post_scan": {"integrity": True, "ner_findings": "fail"}},
        {"post_scan": {"integrity": True, "detectors": "ner"}},
    ],
)
def test_guard_refuses_each_model_post_scan_shape(config: dict[str, Any]) -> None:
    module = _guard_module()
    with pytest.raises(module.PostScanRouteError) as caught:
        module.refuse_unchecked_route(config, "chunked")
    assert caught.value.route == "chunked"
    assert str(caught.value) == _summary()


def test_error_is_public_and_pickles_to_the_same_identity() -> None:
    import pickle

    import decoy_engine

    error = _error_class()
    assert getattr(decoy_engine, "PostScanRouteError", None) is error
    assert "PostScanRouteError" in decoy_engine.__all__
    back = pickle.loads(pickle.dumps(error("sequential")))  # noqa: S301 - our own bytes
    assert (type(back), back.route, str(back)) == (error, "sequential", _summary())


def test_reason_adapter_reports_the_registered_identity() -> None:
    from decoy_engine.reason_adapter import reason_from_exception

    record = reason_from_exception(_error_class()("out_of_core"))
    assert (record.namespace, record.code) == ("route", _CODE)


def _required_keyword(fn: Any) -> None:
    param = inspect.signature(fn).parameters.get("config")
    assert param is not None, f"{fn.__qualname__} takes no config"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, fn.__qualname__
    assert param.default is inspect.Parameter.empty, fn.__qualname__


def test_plan_level_entries_and_their_wrappers_require_a_keyword_config() -> None:
    from decoy_engine.execution import _pipeline_route_exec, _sequential
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
    from decoy_engine.execution.out_of_core import _runner
    from decoy_engine.execution.physical.drivers import OutOfCoreAdapter
    from decoy_engine.execution.physical.drivers._sequential import SequentialAdapter

    for fn in (
        _sequential.run_sequential,
        _runner.run_fk_out_of_core,
        _pipeline_route_exec.run_sequential_route,
        _pipeline_route_exec.run_out_of_core_route,
        PandasExecutionAdapter.run_sequential,
        SequentialAdapter.run,
        OutOfCoreAdapter.run,
    ):
        _required_keyword(fn)


# -- run_out_of_core_route forwards its config to the guarded core -----------------------------


def _tiny_ooc_job() -> dict[str, Any]:
    from tests.unit.execution.test_pipeline_route_exec_ooc_memory_preflight import (
        _edge,
        _graph,
        _tiny_plan,
        _tiny_registry,
    )

    return {
        "plan": _tiny_plan(),
        "sources": {
            "parent": pa.table({"id": ["p1", "p2"]}),
            "child": pa.table({"parent_id": pa.array([], type=pa.string())}),
        },
        "registry": _tiny_registry(),
        "graph": _graph(_edge("parent", "child")),
        "sink": None,
        "route_reason": "test",
        "table_kinds": {},
        "source_loader": None,
        "sources_resident": True,
        "budget_bytes": 512 * 1024 * 1024,
    }


def _spy_core(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from decoy_engine.execution import out_of_core

    received: list[Any] = []
    real = out_of_core.run_fk_out_of_core

    def spy(*args: Any, **kwargs: Any) -> Any:
        received.append(kwargs.get("config"))
        return real(*args, **kwargs)

    monkeypatch.setattr(out_of_core, "run_fk_out_of_core", spy)
    return received


def test_out_of_core_route_forwards_its_config_to_the_guarded_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution._pipeline_route_exec import run_out_of_core_route

    error = _error_class()
    received = _spy_core(monkeypatch)
    with pytest.raises(error):
        run_out_of_core_route(**_tiny_ooc_job(), config=NER_FAIL)
    assert received == [NER_FAIL]
    assert received[0] is NER_FAIL


def test_out_of_core_route_with_regex_only_integrity_runs_as_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.execution._pipeline_route_exec import run_out_of_core_route

    _guard_module()
    received = _spy_core(monkeypatch)
    result = run_out_of_core_route(**_tiny_ooc_job(), config=REGEX_ONLY)
    assert result.outputs["parent"].num_rows == 2
    assert received == [REGEX_ONLY]


# -- regex-only integrity on the chunked entries: output unchanged ------------------------------


def _chunk_cfg(post_scan: dict[str, Any]) -> dict[str, Any]:
    raw = {
        "version": 2,
        "global_settings": {"seed": 42},
        "post_scan": post_scan,
        "sources": {"t": {"type": "file", "format": "parquet", "path": "/dev/null"}},
        "tables": [{"name": "t", "columns": [support.redact_col("r")]}],
        "targets": {"t": {"type": "file", "format": "parquet", "path": "/dev/null"}},
    }
    return PipelineConfig.model_validate(raw).model_dump()


@pytest.mark.parametrize(
    "entry", ["run_mask_pipeline_chunked", "run_mask_chunked", "run_native_or_oracle_chunked"]
)
def test_chunked_entry_output_with_regex_only_integrity_equals_integrity_off(entry: str) -> None:
    from decoy_engine.execution import _chunked
    from decoy_engine.execution.native import _chunked_entry, _dispatch

    _guard_module()
    fn = {
        "run_mask_pipeline_chunked": _chunked.run_mask_pipeline_chunked,
        "run_mask_chunked": _chunked_entry.run_mask_chunked,
        "run_native_or_oracle_chunked": _dispatch.run_native_or_oracle_chunked,
    }[entry]
    src = pa.table({"r": pa.array([f"s{i}" for i in range(6)])})

    def run(config: dict[str, Any]) -> list[pa.Table]:
        return list(fn(config, [src.slice(0, 3), src.slice(3)], table="t", engine_version="v"))

    on = run(_chunk_cfg({"integrity": True}))
    off = run(_chunk_cfg({}))
    assert pa.concat_tables(on).equals(pa.concat_tables(off))


# -- run_pipeline's resident auto-chunk lane still runs the post-scan ---------------------------


def _pipeline_cfg(tmp_path: Any, post_scan: dict[str, Any]) -> tuple[dict[str, Any], pa.Table]:
    src = pa.table(
        {
            "h": pa.array([f"u{i}@x.example" for i in range(support.ROWS)]),
            "r": pa.array([f"s{i}" for i in range(support.ROWS)]),
        }
    )
    path = support.write_source(src, tmp_path / "s.parquet")
    raw = {
        "version": 2,
        "global_settings": {"seed": 42},
        "post_scan": post_scan,
        "sources": {"t": {"type": "file", "format": "parquet", "path": path}},
        "tables": [{"name": "t", "columns": [support.hash_col("h"), support.redact_col("r")]}],
        "targets": {"t": {"type": "file", "format": "parquet", "path": "/dev/null"}},
    }
    return PipelineConfig.model_validate(raw).model_dump(), src


@pytest.mark.parametrize("lane", ["dispatcher", "oracle"])
def test_run_pipeline_auto_chunk_lane_runs_model_post_scan_configs_as_on_main(
    lane: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _chunked_oracle, run_pipeline
    from decoy_engine.execution.native import _chunked_entry

    core = {
        "dispatcher": (_chunked_entry, "_mask_chunked"),
        "oracle": (_chunked_oracle, "_mask_pipeline_chunked"),
    }
    module, name = core[lane]
    assert hasattr(module, name), f"the guard-free {lane} core is missing"
    calls: list[str] = []
    real = getattr(module, name)

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["table"])
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, spy)
    extra = {"chunked_dispatcher_enabled": False} if lane == "oracle" else {}

    def run(post_scan: dict[str, Any]) -> Any:
        # A sink, so the resident decision is the post-scan's (the pinned chunked-sink case).
        cfg, src = _pipeline_cfg(tmp_path, post_scan)
        sink = b6a.RecordingSink()
        kwargs = support.run_kwargs(auto_chunk_threshold_rows=1, chunk_size_rows=2, **extra)
        result = run_pipeline(cfg, sources={"t": src}, sink=sink, **kwargs)
        assert sink.calls == []
        return result

    got = run(NER_FAIL["post_scan"])
    assert calls == ["t"]
    want = run({"integrity": True})
    # The legacy lane is always resident; the dispatcher lane is resident for the post-scan.
    reason = {"dispatcher": "post_scan_integrity", "oracle": "legacy_lane"}[lane]
    assert got.quality_metrics["auto_chunk"]["output"] == {"mode": "resident", "reason": reason}
    assert "post_scan_integrity" in got.quality_metrics
    assert got.outputs["t"].equals(want.outputs["t"])
