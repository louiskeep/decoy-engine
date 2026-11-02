"""The current-main route corpus: one small job per execution route, with actual route
evidence, output digests and error behavior.

Each job returns a JSON-able record. The E1-0 characterization test compares the records
to `route_corpus_golden.json`, captured on the E1 base before any source change; the
post-port parity test (AT-E4) runs the same jobs under `LocalFileAccess` and a
`RootedFileAccess` over the same files and compares to the same golden. Wall-clock fields
are removed, and absolute paths are replaced by `<TMP>`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.profile._readers import LazySource
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _multi_table_support as mt
from tests.unit.execution._transform_testkit import (
    base_table,
    single_table_config,
)
from tests.unit.execution.test_b6a_output import _late_typed_job

GOLDEN = Path(__file__).parent / "route_corpus_golden.json"

Job = Callable[[Path, pytest.MonkeyPatch, dict[str, Any]], dict[str, Any]]


def _digest(table: pa.Table) -> str:
    """Schema plus rows, not file bytes: the Parquet footer carries the pyarrow version."""
    body = json.dumps(table.to_pylist(), default=repr, sort_keys=True)
    return hashlib.sha256((str(table.schema) + "\n" + body).encode()).hexdigest()


def _strip(value: Any, tmp: Path) -> Any:
    if isinstance(value, dict):
        return {
            k: _strip(v, tmp)
            for k, v in value.items()
            if not str(k).endswith(("_ms", "_seconds", "_s"))
        }
    if isinstance(value, (list, tuple)):
        return [_strip(v, tmp) for v in value]
    if isinstance(value, str):
        return value.replace(str(tmp), "<TMP>")
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value


def _record(result: Any, tmp: Path, published: Path | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "outputs": {n: _digest(t) for n, t in sorted(result.outputs.items())},
        "metrics": _strip(
            {
                k: v
                for k, v in result.quality_metrics.items()
                if k in ("execution", "auto_chunk", "chunked_route", "chunked_route_by_table")
            },
            tmp,
        ),
        "row_errors": len(result.row_errors),
    }
    if published is not None and published.is_dir():
        out["published"] = {p.name: _digest(pq.read_table(p)) for p in sorted(published.iterdir())}
    return out


def _std_source(tmp: Path, rows: int = support.ROWS) -> tuple[dict[str, Any], pa.Table]:
    src = pa.table(
        {
            "h": pa.array([f"u{i}@x.example" for i in range(rows)]),
            "r": pa.array([f"s{i}" for i in range(rows)]),
        }
    )
    path = support.write_source(src, tmp / "s.parquet")
    return support.make_cfg([support.hash_col("h"), support.redact_col("r")], path=path), src


def full_frame(tmp: Path, mp: pytest.MonkeyPatch, ctx: dict[str, Any]) -> dict[str, Any]:
    cfg, src = _std_source(tmp)
    return _record(support.run_full_frame(cfg, src, **ctx["kw"]), tmp)


def auto_chunk_resident(tmp: Path, mp: pytest.MonkeyPatch, ctx: dict[str, Any]) -> dict[str, Any]:
    cfg, src = _std_source(tmp)
    return _record(support.run_default(cfg, src, **ctx["kw"]), tmp)


def streamed_to_sink(tmp: Path, mp: pytest.MonkeyPatch, ctx: dict[str, Any]) -> dict[str, Any]:
    cfg, src = _std_source(tmp)
    out = tmp / "out"
    res = run_pipeline(
        cfg, sources={"t": src}, sink=ctx["sink"](out), **support.run_kwargs(**ctx["kw"])
    )
    return _record(res, tmp, out)


def streamed_lazy_source(tmp: Path, mp: pytest.MonkeyPatch, ctx: dict[str, Any]) -> dict[str, Any]:
    cfg, src = _std_source(tmp)
    out = tmp / "out"
    lazy = ctx["lazy"](Path(cfg["sources"]["t"]["path"]))
    res = run_pipeline(
        cfg, sources={"t": lazy}, sink=ctx["sink"](out), **support.run_kwargs(**ctx["kw"])
    )
    return _record(res, tmp, out)


def holdback_spill(tmp: Path, mp: pytest.MonkeyPatch, ctx: dict[str, Any]) -> dict[str, Any]:
    from decoy_engine.execution import _chunked_output_sink as mod

    mp.setattr(mod, "ROW_GROUP_ROWS", 20)
    cfg, src = _late_typed_job(tmp)
    out = tmp / "level_one" / "out"
    res = run_pipeline(
        cfg, sources={"t": src}, sink=ctx["sink"](out), **support.run_kwargs(**ctx["kw"])
    )
    record = _record(res, tmp, out)
    record["spilled"] = res.quality_metrics["auto_chunk"]["output"]["spilled_chunks"] > 0
    return record


def multi_table_dispatch(tmp: Path, mp: pytest.MonkeyPatch, ctx: dict[str, Any]) -> dict[str, Any]:
    cfg, sources = mt.build_job(
        tmp,
        {
            "big": (mt.std_columns("big_ns"), mt.string_table(mt.BIG, "b")),
            "small": (mt.std_columns("small_ns"), mt.string_table(mt.SMALL, "s")),
        },
    )
    out = tmp / "out"
    res = run_pipeline(cfg, sources=sources, sink=ctx["sink"](out), **mt.kw(**ctx["kw"]))
    return _record(res, tmp, out)


def transform_bearing_lazy(
    tmp: Path, mp: pytest.MonkeyPatch, ctx: dict[str, Any]
) -> dict[str, Any]:
    tbl = base_table()
    cfg = single_table_config(
        tmp, tbl, transforms=[{"op": "filter", "expression": "id > 2"}, {"op": "limit", "n": 4}]
    )
    lazy = ctx["lazy"](Path(cfg["sources"]["t"]["path"]))
    res = run_pipeline(cfg, sources={"t": lazy}, engine_version="e1-corpus", **ctx["kw"])
    return _record(res, tmp)


def native_when(tmp: Path, mp: pytest.MonkeyPatch, ctx: dict[str, Any]) -> dict[str, Any]:
    table = pa.table(
        {
            "val": pa.array([f"v{i}" for i in range(support.ROWS)]),
            "amount": pa.array(range(support.ROWS)),
        }
    )
    path = support.write_source(table, tmp / "w.parquet")
    cfg = support.make_cfg(
        [support.redact_col("val"), support.pass_col("amount")], table="tbl", path=path
    )
    for t in cfg["tables"]:
        for column in t["columns"]:
            if column["name"] == "val":
                column["when"] = "amount > 10"
    cfg = PipelineConfig.model_validate(cfg).model_dump()
    res = run_pipeline(
        cfg,
        sources={"tbl": table},
        **support.run_kwargs(**ctx["kw"]),
    )
    return _record(res, tmp)


def sequential_fk(tmp: Path, mp: pytest.MonkeyPatch, ctx: dict[str, Any]) -> dict[str, Any]:
    from tests.unit.execution.test_isolated_worker_streaming import _fk_config

    cfg, manifest = _fk_config(tmp)
    sources = {name: pq.read_table(path) for name, path in manifest.items()}
    out = tmp / "seq_out"
    res = run_pipeline(
        cfg,
        sources=sources,
        engine_version="e1-corpus",
        execution_mode="sequential",
        sink=ctx["sink"](out),
        **ctx["kw"],
    )
    return _record(res, tmp, out)


def out_of_core_fk(tmp: Path, mp: pytest.MonkeyPatch, ctx: dict[str, Any]) -> dict[str, Any]:
    from tests.unit.execution.test_isolated_worker_streaming import _fk_config

    cfg, manifest = _fk_config(tmp)
    sources = {name: ctx["lazy"](Path(path)) for name, path in manifest.items()}
    out = tmp / "ooc_out"
    res = run_pipeline(
        cfg,
        sources=sources,
        engine_version="e1-corpus",
        execution_mode="out_of_core",
        sink=ctx["sink"](out),
        **ctx["kw"],
    )
    return _record(res, tmp, out)


def generate_only(tmp: Path, mp: pytest.MonkeyPatch, ctx: dict[str, Any]) -> dict[str, Any]:
    raw = {
        "version": 1,
        "global_settings": {"seed": 11},
        "tables": [
            {
                "name": "people",
                "rows": 25,
                "columns": [
                    {"name": "id", "strategy": "sequence", "start": 1},
                    {"name": "email", "strategy": "faker", "provider": "person_email"},
                ],
            }
        ],
    }
    try:
        cfg = PipelineConfig.model_validate(raw).model_dump()
    except Exception as exc:
        return {"config_error": type(exc).__name__}
    res = run_pipeline(cfg, sources=None, engine_version="e1-corpus", **ctx["kw"])
    return _record(res, tmp)


JOBS: dict[str, Job] = {
    "full_frame": full_frame,
    "auto_chunk_resident": auto_chunk_resident,
    "streamed_to_sink": streamed_to_sink,
    "streamed_lazy_source": streamed_lazy_source,
    "holdback_spill": holdback_spill,
    "multi_table_dispatch": multi_table_dispatch,
    "transform_bearing_lazy": transform_bearing_lazy,
    "native_when": native_when,
    "sequential_fk": sequential_fk,
    "out_of_core_fk": out_of_core_fk,
    "generate_only": generate_only,
}


def default_ctx() -> dict[str, Any]:
    """How a job builds its sink and lazy source today: the no-policy constructors."""
    return {"sink": ParquetTransactionalSink, "lazy": LazySource, "kw": {}}


def env_key() -> str:
    """Route evidence differs with and without the compiled companion, so the golden has one
    section for each; outputs are equal in both."""
    return "companion" if support.COMPANION_PRESENT else "no_companion"


def load_golden() -> dict[str, Any]:
    document = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    return document.get(env_key(), {})  # type: ignore[no-any-return]


def write_golden(name: str, record: dict[str, Any]) -> None:
    document = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    document.setdefault(env_key(), {})[name] = record
    GOLDEN.write_text(json.dumps(document, sort_keys=True, indent=1) + "\n")
