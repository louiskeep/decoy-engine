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
_MESSAGE = "subset: is applied only by run_pipeline; this entry does not apply it"
_LOADER_MESSAGE = "subset: cannot be applied to a source_loader run; pass sources instead"


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


def test_source_loader_run_refuses_with_a_fixed_text_before_any_read(tmp_path: Path) -> None:
    cfg = _dump(tmp_path, subset=True)
    sources = _RecordingSources(_tables(tmp_path))
    with pytest.raises(PipelineValidationError) as ei:
        run_pipeline(
            cfg,
            None,
            engine_version="t",
            source_loader=lambda name: sources[name],
            source_loader_schemas={n: t.schema for n, t in _tables(tmp_path).items()},
        )
    _assert_refusal(ei.value)
    assert str(ei.value) == _LOADER_MESSAGE
    assert sources.accesses == 0


def test_probe_peak_bytes_refuses_a_subset_config(tmp_path: Path) -> None:
    from decoy_engine.execution._probe import probe_peak_bytes

    cfg = _dump(tmp_path, subset=True)
    with pytest.raises(PipelineValidationError) as ei:
        probe_peak_bytes(cfg, _tables(tmp_path), reference_table="customers", target_rows=1000)
    _assert_refusal(ei.value)
    assert str(ei.value) == _MESSAGE


def test_refusal_text_is_fixed(tmp_path: Path) -> None:
    cfg = _dump(tmp_path, subset=True)
    renamed = copy.deepcopy(cfg)
    renamed["subset"]["seeds"][0]["table"] = "orders"
    texts = []
    for c in (cfg, renamed):
        with pytest.raises(PipelineValidationError) as ei:
            compile_plan(c, profile_source(c), decoy_engine_version="t")
        texts.append(str(ei.value))
        shown = str(ei.value) + repr(ei.value)
        for leak in ("customers", "orders", str(tmp_path)):
            assert leak not in shown
    assert texts[0] == texts[1] == _MESSAGE


def test_isolated_runs_a_subset_and_refuses_a_typed_request_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _isolated_run
    from decoy_engine.subset import SubsetRequest, subset_request_from_config
    from tests.unit.execution import _subset_run_support as sup

    # String keys: a child run with a sink takes the out-of-core route, which refuses int FK keys.
    cfg, data, _ = sup.job(tmp_path)
    request = subset_request_from_config(cfg)
    assert isinstance(request, SubsetRequest)
    writes: list[int] = []
    real = _isolated_run._write_payload

    def spy(*a: Any, **k: Any) -> Any:
        writes.append(1)
        return real(*a, **k)

    monkeypatch.setattr(_isolated_run, "_write_payload", spy)
    with pytest.raises(ValueError) as ei:
        run_pipeline_isolated(
            cfg,
            data,
            staging_dir=tmp_path / "stage",
            output_dir=tmp_path / "out",
            engine_version="t",
            subset_request=request,
        )
    assert writes == []
    assert "customers" not in str(ei.value) and repr(request) not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__context__ is None
    monkeypatch.setattr(_isolated_run, "_write_payload", real)
    for isolate in (False, True):
        out = run_pipeline_isolated(
            cfg,
            data,
            staging_dir=tmp_path / f"s{isolate}",
            output_dir=tmp_path / f"o{isolate}",
            isolate=isolate,
            engine_version="t",
            subset_request="config",
        )
        assert out.outcome == "completed", (isolate, out.error)
        assert out.outputs is not None and out.outputs["customers"].num_rows == 5
    skipped = run_pipeline_isolated(
        cfg,
        data,
        isolate=True,
        staging_dir=tmp_path / "sk",
        output_dir=tmp_path / "ok",
        engine_version="t",
        subset_request="skip",
    )
    assert skipped.outputs is not None and skipped.outputs["customers"].num_rows == 30


def test_governor_delegates_and_applies_the_subset(tmp_path: Path) -> None:
    cfg = _dump(tmp_path, subset=True)
    got = run_job_with_governor(
        cfg,
        _tables(tmp_path),
        budget_bytes=2**30,
        staging_dir=tmp_path / "gstage",
        engine_version="t",
    )
    assert got.result is not None and got.result.outputs is not None
    assert got.result.outputs["customers"].num_rows == 5


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
    est = decoy_engine.estimate_job(cfg, tmp_path)
    assert est.priceable is False
    assert est.route == "subset"
    assert est.notes == ("not applicable: subset",)
    from decoy_engine.execution.capacity import CapacityVerdict, estimate_job_capacity

    cap = estimate_job_capacity(cfg, tmp_path)
    assert cap.verdict is CapacityVerdict.NOT_APPLICABLE and cap.route == "subset"
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
