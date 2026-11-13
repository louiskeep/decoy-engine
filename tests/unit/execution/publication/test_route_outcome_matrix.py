"""`test_route_outcome_matrix` (c1b part): every engine route that binds output through the
reference sink returns a receipt, a retry is `already_committed` and a different result under
the same `operation_id` is `operation_conflict` with the bound unit untouched (plan 4.2, 7.2;
class 4). The isolated parent and fallback and `publish_tables` are c2's and a3's rows.

Each route is proven to be the one that ran (its own telemetry), and the receipt is read where
the route returns it: `run_sequential` and `run_fk_out_of_core` (spied where the pipeline calls
them), `run_pipeline`'s result on the full-frame streamed and mixed routes, and
`GenerationResult` on streamed generation; `sink.receipt` must be the same receipt.

Red before c1b (start commit `acb2c4aa`): the sink takes no `request`, its commit returns
`None` and a retry fails `OSError` 39 on every route.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.publication import PublicationRefused, PublicationRequest
from tests.unit.publication._publish_support import snapshot, stages_left

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX durable publish")

ROUTES = ["sequential", "out_of_core", "full_frame_streamed", "mixed", "streamed_generation"]


def _key_provider() -> Any:
    from decoy_engine.keyprovider import SecretKeyProvider

    return SecretKeyProvider(secret=bytes(range(32)), key_version="v1")


def _fk(tmp: Path, mode: str, seed: str, sink: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Sequential or out-of-core over a parent/child FK job; returns the route's own result."""
    import tests.acceptance.test_f06_route_parity as f06
    from decoy_engine.execution import _pipeline_route_exec, out_of_core
    from tests.acceptance._f06_support import run

    owner, name = (
        (_pipeline_route_exec, "run_sequential")
        if mode == "sequential"
        else (out_of_core, "run_fk_out_of_core")
    )
    real = getattr(owner, name)
    seen: list[Any] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(real(*args, **kwargs))
        return seen[-1]

    sources = f06._fk_sources()
    labels = pa.array([f"{seed}{c}" for c in "abcde"], pa.string())
    sources["parent"] = sources["parent"].set_column(1, "label", labels)
    with monkeypatch.context() as patch:
        patch.setattr(owner, name, spy)
        result = run(
            f06._fk_cfg(tmp, endpoints=False),
            sources,
            execution_mode=mode,
            sink=sink,
            key_provider=_key_provider(),
        )
    assert result.quality_metrics["execution"]["execution_mode"] == mode  # the route ran
    assert len(seen) == 1
    return seen[0].publication_receipt


def _full_frame_streamed(tmp: Path, seed: str, sink: Any) -> Any:
    from decoy_engine.execution import run_pipeline
    from tests.unit.execution import _auto_chunk_support as support

    src = pa.table(
        {
            "r": pa.array([f"s{i}" for i in range(support.ROWS)]),
            "p": pa.array([f"{seed}-{i}" for i in range(support.ROWS)]),
        }
    )
    path = support.write_source(src, tmp / f"s-{seed}.parquet")
    cfg = support.make_cfg([support.redact_col("r"), support.pass_col("p")], path=path)
    result = run_pipeline(cfg, sources={support.TABLE: src}, sink=sink, **support.run_kwargs())
    execution = result.quality_metrics["execution"]
    assert (execution["execution_mode"], execution["outputs_streamed"]) == ("full_frame", True)
    return result.publication_receipt


def _mixed(tmp: Path, seed: str, sink: Any) -> Any:
    from tests.parity import _f2_mixed_support as mx

    rows = 20 if seed == "A" else 23  # `note` is redacted: only the row count changes output
    child = mx.child_table(pa.array([str(i % 5) for i in range(rows)], pa.string()))
    (tmp / seed).mkdir(exist_ok=True)
    cfg = mx.mixed_config(tmp / seed, child)
    result = mx.run(cfg, {mx.CHILD: child}, sink=sink)
    execution = result.quality_metrics["execution"]
    assert (execution["execution_mode"], execution["outputs_streamed"]) == ("mixed", True)
    return result.publication_receipt


def _generation(seed: str, sink: Any) -> Any:
    from decoy_engine.generation._streamed import run_generation
    from tests.parity import _f0_fixtures as fx
    from tests.parity._fr0_support import prepared_config

    cfg = prepared_config(fx.FIXTURES_BY_ID["sequence"].run.builder())
    if seed != "A":
        cfg["tables"][0]["row_count"] = int(cfg["tables"][0].get("row_count") or 0) + 3
    result = run_generation(cfg, sink=sink)
    assert result.mode == "streamed"
    return result.publication_receipt


def _runner(route: str, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    def go(seed: str, sink: Any) -> Any:
        if route in ("sequential", "out_of_core"):
            return _fk(tmp, route, seed, sink, monkeypatch)
        if route == "full_frame_streamed":
            return _full_frame_streamed(tmp, seed, sink)
        if route == "mixed":
            return _mixed(tmp, seed, sink)
        return _generation(seed, sink)

    return go


@pytest.mark.parametrize("route", ROUTES)
def test_route_outcome_matrix(route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _runner(route, tmp_path, monkeypatch)
    leaf = tmp_path / "out"

    first = ParquetTransactionalSink(leaf, request=PublicationRequest("op-1"))
    receipt = run("A", first)
    assert receipt is not None and receipt == first.receipt
    assert (receipt.outcome, receipt.durable) == ("published", True)
    before = snapshot(leaf)

    retry = ParquetTransactionalSink(leaf, request=PublicationRequest("op-1"))
    again = run("A", retry)
    assert again is not None and again == retry.receipt
    assert (again.outcome, again.result_id) == ("already_committed", receipt.result_id)
    assert snapshot(leaf) == before

    conflict = ParquetTransactionalSink(leaf, request=PublicationRequest("op-1"))
    with pytest.raises(PublicationRefused) as info:
        run("B", conflict)
    refused = info.value.receipt
    assert (refused.outcome, info.value.code) == (
        "operation_conflict",
        "publication_operation_conflict",
    )
    assert refused.result_id != receipt.result_id  # non-vacuity: B really is another result
    assert conflict.receipt == refused
    assert snapshot(leaf) == before
    assert stages_left(tmp_path) == []


STREAMED = {"full_frame_streamed", "mixed", "streamed_generation"}


@pytest.mark.parametrize("route", ROUTES)
def test_descriptor_describes_every_staged_table(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sink hands its own full staged set to `publish_unit` on every route, so the bound
    descriptor describes exactly as many files as the sink staged; a streamed route's publish
    session reports the same set (`staged_tables()` on both)."""
    import json

    from decoy_engine.execution import _chunked_output_sink
    from decoy_engine.publication import DESCRIPTOR_NAME

    seen: list[list[str]] = []
    real = _chunked_output_sink.OutputPublish.commit

    def commit(self: Any) -> Any:
        seen.append(self.staged_tables())
        return real(self)

    monkeypatch.setattr(_chunked_output_sink.OutputPublish, "commit", commit)
    leaf = tmp_path / "out"
    sink = ParquetTransactionalSink(leaf, request=PublicationRequest("op-1"))
    receipt = _runner(route, tmp_path, monkeypatch)("A", sink)
    staged = sink.staged_tables()
    bound = json.loads((leaf / DESCRIPTOR_NAME).read_bytes())
    assert staged and sorted(staged) == sorted(
        p.name for p in leaf.glob("*.parquet")
    )  # non-vacuity
    assert len(bound["artifacts"]) == len(staged) == len(receipt.artifacts)
    assert sorted(a["path"] for a in bound["artifacts"]) == sorted(staged)
    if route in STREAMED:
        assert [sorted(s) for s in seen if s] == [sorted(staged)]
