"""SUB-STOP: every run entry refuses a config that sets `subset:` (no source read first).

The literal code string is used on purpose (never the new symbol), so a test run on
the base tree fails on an assertion, not an ImportError.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import decoy_engine
from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked, run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.errors import PipelineValidationError
from decoy_engine.execution import run_job_with_governor, run_pipeline_isolated
from decoy_engine.plan import compile_plan
from decoy_engine.profile import profile_source
from decoy_engine.subset import plan_subset, run_subset, subset_inputs_from_config
from tests._declared_schema import chunked_entry_kwargs, declare_rest, declared_schema

_VAR = "SUB_STOP_TEST_SECRET"
_CODE = "subset_run_unsupported"
_MESSAGE = (
    "subset: is not applied by a pipeline run yet; run the subset step first and "
    "point this run's sources at its output, or remove the subset: block"
)


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_VAR, "ab" * 32)


def _raw(tmp_path: Path, *, subset: bool) -> dict[str, Any]:
    cust = pa.table({"customer_id": list(range(100)), "name": [f"n{i}" for i in range(100)]})
    orders = pa.table({"order_id": list(range(300)), "customer_id": [i % 100 for i in range(300)]})
    pq.write_table(cust, tmp_path / "customers.parquet")
    pq.write_table(orders, tmp_path / "orders.parquet")
    col = {"name": "customer_id", "strategy": "hash", "namespace": "customer_identity"}
    raw: dict[str, Any] = {
        "version": 2,
        "global_settings": {"seed": 1, "mask_secret_ref": f"env:{_VAR}"},
        "sources": {
            "customers": {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / "customers.parquet"),
            },
            "orders": {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / "orders.parquet"),
            },
        },
        "tables": [
            {"name": "customers", "columns": [col]},
            {"name": "orders", "columns": [col]},
        ],
        "relationships": [
            {
                "parent": {"table": "customers", "columns": ["customer_id"]},
                "children": [{"table": "orders", "columns": ["customer_id"]}],
                "orphan_policy": "preserve",
                "namespace": "customer_identity",
            }
        ],
        "targets": {
            "customers": {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / "out_customers.parquet"),
            },
            "orders": {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / "out_orders.parquet"),
            },
        },
    }
    if subset:
        raw["subset"] = {
            "seeds": [
                {"table": "customers", "mode": "sample", "key_columns": ["customer_id"], "count": 5}
            ]
        }
    return raw


def _dump(tmp_path: Path, *, subset: bool) -> dict[str, Any]:
    return PipelineConfig.model_validate(declare_rest(_raw(tmp_path, subset=subset))).model_dump()


def _tables(tmp_path: Path) -> dict[str, pa.Table]:
    return {
        "customers": pq.read_table(tmp_path / "customers.parquet"),
        "orders": pq.read_table(tmp_path / "orders.parquet"),
    }


class _RecordingSources(Mapping[str, pa.Table]):
    """Counts every access so a refusal can prove no source was touched."""

    def __init__(self, inner: dict[str, pa.Table]) -> None:
        self._inner = inner
        self.accesses = 0

    def __getitem__(self, key: str) -> pa.Table:
        self.accesses += 1
        return self._inner[key]

    def __iter__(self) -> Iterator[str]:
        self.accesses += 1
        return iter(self._inner)

    def __len__(self) -> int:
        self.accesses += 1
        return len(self._inner)

    def __contains__(self, key: object) -> bool:
        self.accesses += 1
        return key in self._inner


class _PulledChunks:
    """One-chunk iterable that counts how many chunks were pulled."""

    def __init__(self, table: pa.Table) -> None:
        self._table = table
        self.pulls = 0

    def __iter__(self) -> Iterator[pa.Table]:
        self.pulls += 1
        yield self._table


def _assert_refusal(err: PipelineValidationError) -> None:
    assert err.path == "subset"
    assert err.code == _CODE


def test_run_pipeline_refuses_subset_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _dump(tmp_path, subset=True)
    sources = _RecordingSources(_tables(tmp_path))

    def _boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("context built")

    monkeypatch.setattr("decoy_engine.execution._pipeline_context.build_run_context", _boom)
    with pytest.raises(PipelineValidationError) as ei:
        run_pipeline(cfg, sources, engine_version="t")
    _assert_refusal(ei.value)
    assert sources.accesses == 0


def test_refusal_text_is_fixed(tmp_path: Path) -> None:
    cfg = _dump(tmp_path, subset=True)
    renamed = copy.deepcopy(cfg)
    renamed["subset"]["seeds"][0]["table"] = "orders"
    texts = []
    for c in (cfg, renamed):
        with pytest.raises(PipelineValidationError) as ei:
            run_pipeline(c, _tables(tmp_path), engine_version="t")
        texts.append(str(ei.value))
        shown = str(ei.value) + repr(ei.value)
        for leak in ("customers", "orders", str(tmp_path)):
            assert leak not in shown
        assert " 5 " not in shown
    assert texts[0] == texts[1] == _MESSAGE


@pytest.mark.parametrize("isolate", [True, False])
def test_isolated_refuses_before_spawn(tmp_path: Path, isolate: bool) -> None:
    cfg = _dump(tmp_path, subset=True)
    spawned: list[int] = []
    stage, out = tmp_path / "stage", tmp_path / "out"
    with pytest.raises(PipelineValidationError) as ei:
        run_pipeline_isolated(
            cfg,
            _tables(tmp_path),
            staging_dir=stage,
            output_dir=out,
            on_spawn=spawned.append,
            isolate=isolate,
            engine_version="t",
        )
    _assert_refusal(ei.value)
    assert spawned == []
    for d in (stage, out):
        assert not d.exists() or not any(d.iterdir())


def test_governor_refuses_before_any_rung(tmp_path: Path) -> None:
    cfg = _dump(tmp_path, subset=True)
    trips: list[Any] = []
    stage = tmp_path / "gstage"
    with pytest.raises(PipelineValidationError) as ei:
        run_job_with_governor(
            cfg,
            _tables(tmp_path),
            budget_bytes=2**30,
            staging_dir=stage,
            on_trip=trips.append,
            engine_version="t",
        )
    _assert_refusal(ei.value)
    assert trips == []
    assert not stage.exists() or not any(stage.iterdir())


def test_chunked_refuses_at_call_without_pulling(tmp_path: Path) -> None:
    cfg = _dump(tmp_path, subset=True)
    chunks = _PulledChunks(_tables(tmp_path)["customers"])
    with pytest.raises(PipelineValidationError) as ei:
        run_mask_pipeline_chunked(
            cfg,
            chunks,
            table="customers",
            source_schema=declared_schema(cfg, "customers"),
            engine_version="t",
        )
    _assert_refusal(ei.value)
    assert chunks.pulls == 0


def test_native_chunked_refuses_at_call_without_pulling(tmp_path: Path) -> None:
    cfg = _dump(tmp_path, subset=True)
    chunks = _PulledChunks(_tables(tmp_path)["customers"])
    with pytest.raises(PipelineValidationError) as ei:
        run_mask_chunked(cfg, chunks, table="customers", engine_version="t")
    _assert_refusal(ei.value)
    assert chunks.pulls == 0


def test_native_or_oracle_chunked_refuses_at_call_without_pulling(tmp_path: Path) -> None:
    from decoy_engine.execution.native import run_native_or_oracle_chunked

    cfg = _dump(tmp_path, subset=True)
    chunks = _PulledChunks(_tables(tmp_path)["customers"])
    with pytest.raises(PipelineValidationError) as ei:
        run_native_or_oracle_chunked(cfg, chunks, table="customers", engine_version="t")
    _assert_refusal(ei.value)
    assert chunks.pulls == 0


def test_compile_plan_refuses_subset(tmp_path: Path) -> None:
    cfg = _dump(tmp_path, subset=True)
    with pytest.raises(PipelineValidationError) as ei:
        compile_plan(cfg, profile_source(cfg), decoy_engine_version="t")
    _assert_refusal(ei.value)
    # estimate_job never raises by contract: the refusal becomes the unpriceable fallback.
    est = decoy_engine.estimate_job(cfg, tmp_path)
    assert est.priceable is False
    assert est.route == "unknown"
    assert est.notes == ("estimate unavailable (PipelineValidationError)",)
    control = decoy_engine.estimate_job(_dump(tmp_path, subset=False), tmp_path)
    assert control.route != "unknown"
    assert not any(n.startswith("estimate unavailable") for n in control.notes)


def test_no_subset_runs_unchanged(tmp_path: Path) -> None:
    cfg = _dump(tmp_path, subset=False)
    assert cfg["subset"] is None
    res = run_pipeline(cfg, _tables(tmp_path), engine_version="t")
    assert res.outputs["customers"].num_rows == 100
    assert res.outputs["orders"].num_rows == 300
    cust = _tables(tmp_path)["customers"]
    from decoy_engine.execution.native import run_native_or_oracle_chunked

    for fn in (run_mask_pipeline_chunked, run_mask_chunked, run_native_or_oracle_chunked):
        rows = sum(
            t.num_rows
            for t in fn(
                cfg,
                _PulledChunks(cust),
                table="customers",
                engine_version="t",
                **chunked_entry_kwargs(fn, cfg, "customers"),
            )
        )
        assert rows == 100
    plan = compile_plan(cfg, profile_source(cfg), decoy_engine_version="t")
    assert plan is not None
    out = run_pipeline_isolated(
        cfg,
        _tables(tmp_path),
        isolate=False,
        staging_dir=tmp_path / "s2",
        output_dir=tmp_path / "o2",
        engine_version="t",
    )
    assert out.outcome == "completed"


def test_subset_step_still_runs(tmp_path: Path) -> None:
    cfg = _dump(tmp_path, subset=True)
    sources, rels, seeds, policy = subset_inputs_from_config(cfg)
    kwargs = dict(
        sources=sources,
        relationships=rels,
        seeds=seeds,
        policy=policy,
        job_seed=b"s" * 32,
        engine_version="t",
    )
    assert plan_subset(**kwargs) is not None
    result = run_subset(output_dir=tmp_path / "subset_out", **kwargs)
    assert result is not None
    kept = pq.read_table(tmp_path / "subset_out" / "customers.parquet")
    assert kept.num_rows == 5
