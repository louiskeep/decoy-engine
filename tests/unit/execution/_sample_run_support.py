"""Fixtures for sample-inside-a-run tests: a chain, an edgeless table and ad-hoc FK shapes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from decoy_engine import read_fixed_width
from decoy_engine.config import PipelineConfig
from decoy_engine.profile._lazy_csv import CsvLazySource
from decoy_engine.profile._readers import LazySource
from tests._declared_schema import declare_rest

VAR = "SAMPLE_SE2_TEST_SECRET"
NOTES = 40
WIDTH = 5  # every chain value fits: "I179" is the longest

Edge = tuple[str, tuple[str, ...], str, tuple[str, ...]]
CHAIN_EDGES: list[Edge] = [
    ("customers", ("cid",), "orders", ("cid",)),
    ("orders", ("oid",), "items", ("oid",)),
]


def chain() -> dict[str, pa.Table]:
    """customers -> orders -> items (text keys) plus `notes`, which has no relationship."""
    n = range(30)
    return {
        "customers": pa.table({"cid": [f"C{i}" for i in n], "name": [f"n{i}" for i in n]}),
        "orders": pa.table(
            {"oid": [f"O{i}" for i in range(90)], "cid": [f"C{i % 30}" for i in range(90)]}
        ),
        "items": pa.table(
            {"iid": [f"I{i}" for i in range(180)], "oid": [f"O{i % 90}" for i in range(180)]}
        ),
        "notes": pa.table(
            {"nid": [f"N{i}" for i in range(NOTES)], "body": [f"b{i}" for i in range(NOTES)]}
        ),
    }


def _write(path: Path, table: pa.Table, fmt: str) -> dict[str, Any]:
    desc: dict[str, Any] = {"type": "file", "format": fmt, "path": str(path)}
    if fmt == "parquet":
        pq.write_table(table, path)
    elif fmt in ("csv", "tab"):
        sep = "\t" if fmt == "tab" else ","
        pacsv.write_csv(table, path, write_options=pacsv.WriteOptions(delimiter=sep))
        desc.update(format="csv", has_header=True, delimiter=sep)
    else:  # fixed_width
        lines = ["".join(str(v).ljust(WIDTH) for v in row.values()) for row in table.to_pylist()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        desc["layout"] = {
            "columns": [
                {"name": n, "start": i * WIDTH, "width": WIDTH, "type": "str"}
                for i, n in enumerate(table.schema.names)
            ]
        }
    return desc


def build(
    tmp_path: Path,
    data: dict[str, pa.Table],
    relationships: list[Edge],
    *,
    fmt: str = "parquet",
    shuffle: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The validated config dump (no `subset:` block) and the lazy sources.

    Every relationship column is hashed under a namespace named for its parent column;
    ``shuffle`` maps a table to one column to shuffle."""
    keys: dict[str, dict[str, str]] = {name: {} for name in data}
    rels: list[dict[str, Any]] = []
    for parent, pcols, child, ccols in relationships:
        ns = f"{parent}_{'_'.join(pcols)}"
        for table, cols in ((parent, pcols), (child, ccols)):
            keys[table].update({c: ns for c in cols})
        rels.append(
            {
                "parent": {"table": parent, "columns": list(pcols)},
                "children": [{"table": child, "columns": list(ccols)}],
                "orphan_policy": "preserve",
                "namespace": ns,
            }
        )
    sources = {n: _write(tmp_path / f"{n}.{fmt}", t, fmt) for n, t in data.items()}
    tables = []
    for name in data:
        cols: list[dict[str, Any]] = [
            {"name": c, "strategy": "hash", "namespace": ns} for c, ns in keys[name].items()
        ]
        if name in (shuffle or {}):
            cols.append({"name": (shuffle or {})[name], "strategy": "shuffle"})
        elif not cols:
            cols.append({"name": data[name].schema.names[0], "strategy": "passthrough"})
        tables.append({"name": name, "columns": cols})
    raw: dict[str, Any] = {
        "version": 2,
        "global_settings": {"seed": 1, "mask_secret_ref": f"env:{VAR}"},
        "sources": sources,
        "tables": tables,
        "relationships": rels,
        "targets": {
            n: {"type": "file", "format": "parquet", "path": str(tmp_path / f"out_{n}.parquet")}
            for n in data
        },
    }
    cfg = PipelineConfig.model_validate(declare_rest(raw)).model_dump()
    return cfg, lazy_sources(cfg)


def lazy_sources(cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, desc in cfg["sources"].items():
        if desc["format"] == "csv":
            out[name] = CsvLazySource.from_descriptor(desc, table=name)
        elif desc["format"] == "parquet":
            out[name] = LazySource(Path(desc["path"]))
    return out


def resident_sources(cfg: dict[str, Any]) -> dict[str, pa.Table]:
    """Every declared source read into memory as a resident caller holds it: text columns."""
    out: dict[str, pa.Table] = {}
    for name, desc in cfg["sources"].items():
        if desc["format"] == "fixed_width":
            frame = read_fixed_width(desc["path"], desc["layout"])
            out[name] = pa.Table.from_pandas(frame, preserve_index=False)
        elif desc["format"] == "csv":
            out[name] = CsvLazySource.from_descriptor(desc, table=name).to_table()
        else:
            out[name] = pq.read_table(desc["path"])
    return out


def chain_job(
    tmp_path: Path, *, fmt: str = "parquet"
) -> tuple[dict[str, Any], dict[str, pa.Table], dict[str, Any]]:
    """The chain config, the resident tables and the lazy sources."""
    data = chain()
    cfg, lazy = build(tmp_path, data, CHAIN_EDGES, fmt=fmt, shuffle={"notes": "body"})
    return cfg, data, lazy
