"""Cell-level corpus for the F06 format x route x policy matrix.

Every admitted cell of ``tests/fixtures/f06/route-format-matrix-v1.json`` is run on the real
route: one declared column of the cell's format, a corpus built from the format's own vectors plus
an invalid and a null cell, and the expected result derived from the catalog kernels
(``parse_value`` / ``format_value``), which is what every route must reproduce. A driver returns
the route identity the engine reported, so a cell that silently ran elsewhere cannot pass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa

from tests.acceptance._f06_support import PROTECTED_QUARANTINE, column, run

MATRIX = Path(__file__).resolve().parents[1] / "fixtures" / "f06" / "route-format-matrix-v1.json"
POLICIES = ("fail", "null", "quarantine")
INVALID_TEXT = "\x01not-a-value"
INVALID_BYTES = b"\xff\xff\xff\xff\xff\xff\xff\xff"


def admitted_cells() -> list[tuple[str, str, str]]:
    """``(route, "<format>|<direction>", policy)`` for every ``ok`` cell of the pinned matrix."""
    matrix = json.loads(MATRIX.read_text())
    return sorted(
        (route, key, policy)
        for key, row in matrix["formats"].items()
        for route, cells in row["routes"].items()
        for policy, verdict in cells.items()
        if verdict == "ok"
    )


@dataclass(frozen=True)
class Corpus:
    fields: dict[str, Any]  # logical_type / parse_format / output_format for the declared column
    cells: list[Any]  # the source cells, in order; None is a physical null
    binary: bool


def _descriptor(key: str) -> Any:
    from decoy_engine.declared_casting import descriptors

    format_id, direction = key.split("|")
    return next(d for d in descriptors() if (d.format_id, d.direction) == (format_id, direction))


def _widen(spec: dict[str, Any]) -> dict[str, Any]:
    """An integer contract at 64 bits: a narrower nullable column does not survive the pandas
    round trip some lanes require, which is a lane decision, not what this corpus is for."""
    if "bit_width" in spec:
        spec["bit_width"] = 64
    return spec


def build_corpus(key: str, *, invalid: bool = True) -> Corpus:
    """The declared column's contract fields and a source column that exercises it; ``invalid``
    adds a cell the contract rejects (a ``fail`` run is proven both with and without it)."""
    from decoy_engine.declared_casting import descriptor_for

    desc = _descriptor(key)
    spec = _widen(dict(desc.vectors[0][0]))
    if desc.direction == "parse":
        same = [v for v in desc.vectors if v[0] == desc.vectors[0][0]]
        fields: dict[str, Any] = {"logical_type": desc.logical, "parse_format": spec}
        lexemes = [lexeme for _, lexeme, _ in same]  # parse vectors are (spec, lexeme, value)
        binary = desc.physical_kind == "binary"
    else:
        inverse = descriptor_for(desc.inverse, "parse")
        parse = _widen(dict(inverse.vectors[0][0]))
        if "width" in desc.model.model_fields and "width" not in spec:
            spec.update(width=40, width_unit="code_points")
        if desc.logical in ("decimal", "currency"):
            parse.update({k: spec[k] for k in ("precision", "scale") if k in spec})
        fields = {"logical_type": desc.logical, "parse_format": parse, "output_format": spec}
        lexemes = [lex for s_, lex, _ in inverse.vectors if s_ == inverse.vectors[0][0]]
        binary = inverse.physical_kind == "binary"
    assert lexemes, f"{key}: no usable vector"
    cells: list[Any] = [lexemes[i % len(lexemes)] for i in range(4)]
    cells += ([INVALID_BYTES if binary else INVALID_TEXT] if invalid else []) + [None]
    return Corpus(fields, cells, binary)


def expected(corpus: Corpus, policy: str) -> dict[str, Any]:
    """What the route must publish, from the catalog kernels: ``rows`` (id -> cell), the ids to
    quarantine, the failing-cell count and the reason code of the first one (what ``fail`` raises)."""
    from decoy_engine.declared_casting import format_value, parse_value

    parse = corpus.fields["parse_format"]
    out_spec = corpus.fields.get("output_format")
    rows: dict[str, Any] = {}
    quarantined: list[str] = []
    first_failure: str | None = None
    failures = 0
    for i, cell in enumerate(corpus.cells):
        row_id = str(i)
        if cell is None:
            rows[row_id] = None
            continue
        parsed = parse_value(parse, cell)
        rendered = format_value(out_spec, parsed.value) if parsed.ok and out_spec else parsed
        if rendered.ok:
            rows[row_id] = rendered.value
            continue
        failures += 1
        first_failure = first_failure or str(rendered.reason)
        if policy == "quarantine":
            quarantined.append(row_id)
        else:
            rows[row_id] = None
    return {
        "rows": rows,
        "quarantined": quarantined,
        "first_failure": first_failure,
        "failures": failures,
    }


# Admitted by the route's capabilities but declined by its physical admission for a documented
# reason. A declined cell still runs: the engine falls back, and the fallback must publish what
# the contract promises. Each predicate is deliberately narrow so a broken route cannot hide here.
DECLINE_RULES = {
    "chunked": "a parse-only decimal/currency column is not chunk-stable (its Arrow precision is "
    "rebuilt per chunk), so the planner keeps the job off the chunked lane",
    "mixed": "the mixed route classifies its mask table with the chunked planner, so the same "
    "unrendered decimal/currency exclusion applies",
    "unified_slice": "the unified slice masks scalar string/integer columns; date32 and decimal128 "
    "columns decline to the full-frame lane",
}


def physical_decline(route: str, key: str) -> str | None:
    desc = _descriptor(key)
    unrendered = desc.direction == "parse" and desc.logical in ("decimal", "currency")
    if route in ("chunked", "mixed") and unrendered:
        return DECLINE_RULES[route]
    if route == "unified_slice" and desc.logical in ("decimal", "currency", "date"):
        return DECLINE_RULES[route]
    return None


def check_route(took: bool, declined: bool, what: str) -> None:
    """The route ran exactly when its physical admission did not decline the cell."""
    if declined:
        assert not took, f"{what} ran although the cell is documented as declined"
    else:
        assert took, f"{what} did not run"


def declared_column(corpus: Corpus, policy: str) -> dict[str, Any]:
    return column("c", error_policy=policy, **corpus.fields)


def source_cells(corpus: Corpus) -> pa.Array:
    return pa.array(corpus.cells, pa.binary() if corpus.binary else pa.string())


def _top(policy: str) -> dict[str, Any]:
    return {"protected_quarantine": PROTECTED_QUARANTINE} if policy == "quarantine" else {}


def _parquet(table: str, tmp_path: Path, data: pa.Table) -> tuple[dict[str, Any], dict[str, Any]]:
    """Source and target descriptors for ``table``; the profile pass reads the source file."""
    import pyarrow.parquet as pq

    path = str(tmp_path / f"{table}.parquet")
    pq.write_table(data, path)
    return (
        {"type": "file", "format": "parquet", "path": path},
        {"type": "file", "format": "parquet", "path": path + ".out"},
    )


# -- route drivers: each returns (result, table, route identity reported by the engine) --------


def _single(tmp_path: Path, corpus: Corpus, policy: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ids = pa.array([str(i) for i in range(len(corpus.cells))], pa.string())
    table = pa.table({"id": ids, "c": source_cells(corpus)})
    src, dst = _parquet("people", tmp_path, table)
    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {"people": src},
        "tables": [{"name": "people", "columns": [column("id"), declared_column(corpus, policy)]}],
        "targets": {"people": dst},
        "relationships": [],
        "namespaces": {},
        **_top(policy),
    }
    return cfg, {"people": table}


def drive_full_frame(
    tmp_path: Path, corpus: Corpus, policy: str, runtime: Any, declined: bool = False
) -> Any:
    cfg, sources = _single(tmp_path, corpus, policy)
    result = run(
        cfg,
        sources,
        execution_mode="full_frame",
        auto_chunk=False,
        unified_slice_enabled=False,
        quarantine_runtime=runtime,
    )
    assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
    assert "unified_slice_activation" not in result.quality_metrics
    return result, "people"


def drive_chunked(
    tmp_path: Path, corpus: Corpus, policy: str, runtime: Any, declined: bool = False
) -> Any:
    cfg, sources = _single(tmp_path, corpus, policy)
    result = run(
        cfg,
        sources,
        auto_chunk=True,
        chunk_size_rows=2,
        auto_chunk_threshold_rows=1,
        quarantine_runtime=runtime,
    )
    block = result.quality_metrics["auto_chunk"]
    check_route(block["mode"] == "chunked", declined, "the chunked lane")
    if declined:
        assert "declared, no output_format" in block["reason"], block["reason"]
    return result, "people"


def drive_unified_slice(
    tmp_path: Path, corpus: Corpus, policy: str, runtime: Any, declined: bool = False
) -> Any:
    cfg, sources = _single(tmp_path, corpus, policy)
    result = run(cfg, sources, auto_chunk=False, quarantine_runtime=runtime)
    check_route("unified_slice_activation" in result.quality_metrics, declined, "the unified slice")
    return result, "people"


def _relational(
    tmp_path: Path, corpus: Corpus, policy: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    n = len(corpus.cells)
    ids = pa.array([str(i) for i in range(n)], pa.string())
    parent = pa.table({"id": ids, "label": pa.array(["p"] * n, pa.string())})
    child = pa.table({"id": ids, "pid": ids, "c": source_cells(corpus)})
    psrc, pdst = _parquet("parent", tmp_path, parent)
    csrc, cdst = _parquet("child", tmp_path, child)
    key = column("id", "hash", namespace="ns", deterministic=True)
    fk = column("pid", "hash", namespace="ns", deterministic=True)
    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {"parent": psrc, "child": csrc},
        "tables": [
            {"name": "parent", "columns": [key, column("label")]},
            {
                "name": "child",
                "columns": [column("id"), fk, declared_column(corpus, policy)],
            },
        ],
        "targets": {"parent": pdst, "child": cdst},
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "remap",
                "namespace": "ns",
            }
        ],
        "namespaces": {},
        **_top(policy),
    }
    return cfg, {"parent": parent, "child": child}


def _drive_relational(mode: str, tmp_path: Path, corpus: Corpus, policy: str, runtime: Any) -> Any:
    cfg, sources = _relational(tmp_path, corpus, policy)
    result = run(cfg, sources, execution_mode=mode, quarantine_runtime=runtime)
    assert result.quality_metrics["execution"]["execution_mode"] == mode
    return result, "child"


def drive_sequential(
    tmp_path: Path, corpus: Corpus, policy: str, runtime: Any, declined: bool = False
) -> Any:
    return _drive_relational("sequential", tmp_path, corpus, policy, runtime)


def drive_out_of_core(
    tmp_path: Path, corpus: Corpus, policy: str, runtime: Any, declined: bool = False
) -> Any:
    return _drive_relational("out_of_core", tmp_path, corpus, policy, runtime)


def drive_mixed(
    tmp_path: Path, corpus: Corpus, policy: str, runtime: Any, declined: bool = False
) -> Any:
    from tests.parity import _f2_mixed_support as mx

    n = len(corpus.cells)
    child = pa.table(
        {
            mx.FK: pa.array([f"P{i % 5}" for i in range(n)], pa.string()),
            "email": pa.array([f"u{i}@example.com" for i in range(n)], pa.string()),
            "note": pa.array([f"note-{i}" for i in range(n)], pa.string()),
            "id": pa.array([str(i) for i in range(n)], pa.string()),
            "c": source_cells(corpus),
        }
    )
    columns = [*mx.payload_columns(), column("id"), declared_column(corpus, policy)]
    cfg = mx.mixed_config(tmp_path, child, child_columns=columns, coupled=False)
    result = mx.run(cfg, {mx.CHILD: child}, quarantine_runtime=runtime)
    check_route("mixed_route" in result.quality_metrics, declined, "the production mixed route")
    return result, mx.CHILD


DRIVERS = {
    "full_frame": drive_full_frame,
    "chunked": drive_chunked,
    "unified_slice": drive_unified_slice,
    "sequential": drive_sequential,
    "out_of_core": drive_out_of_core,
    "mixed": drive_mixed,
}
