"""AT-E2a-9: the `declare-columns` and `source-schema` modes of the CONFIG-EXPLICIT codemod."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pyarrow as pa
import pytest

from decoy_engine.errors import PipelineValidationError

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def codemod() -> ModuleType:
    path = REPO / "scripts" / "dev" / "codemods" / "config_explicit.py"
    spec = importlib.util.spec_from_file_location("config_explicit", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(
    tmp: Path, path: Path, table: str, kind: str, undeclared: list[str], declared: list[str]
) -> Path:
    out = tmp / "rec.jsonl"
    row = {
        "node": "t::n",
        "table": table,
        "kind": kind,
        "undeclared": undeclared,
        "declared": declared,
        "frame": f"{path}:1",
    }
    out.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return out


ONE = 'CONFIG = {"sources": {}, "tables": [{"name": "t", "columns": [{"name": "a", "strategy": "hash"}]}]}\n'
MULTI = """CONFIG = {
    "tables": [
        {
            "name": "t",
            "columns": [
                {"name": "a", "strategy": "hash"},
            ],
        }
    ]
}
"""


def test_declare_columns_codemod(
    codemod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # (a) one matching literal gets exactly one passthrough entry, in its own style.
    one = tmp_path / "one.py"
    one.write_text(ONE, encoding="utf-8")
    rec = _record(tmp_path, one, "t", "column", ["b"], ["a"])
    assert codemod.main(["declare-columns", "--record", str(rec), str(one)]) == 0
    assert f"declare-columns {one}:1" in capsys.readouterr().out
    assert one.read_text().count('"strategy": "passthrough"') == 1
    multi = tmp_path / "multi.py"
    multi.write_text(MULTI, encoding="utf-8")
    rec = _record(tmp_path, multi, "t", "column", ["b", "c"], ["a"])
    codemod.main(["declare-columns", "--record", str(rec), str(multi)])
    text = multi.read_text()
    assert text.count("passthrough") == 2
    assert '                {"name": "b", "strategy": "passthrough"},\n' in text
    ast.parse(text)
    # (e) a second run finds no matching literal (names no longer equal) and edits nothing.
    before = multi.read_text()
    codemod.main(["declare-columns", "--record", str(rec), str(multi)])
    assert multi.read_text() == before
    assert "hand t::n t" in capsys.readouterr().out

    # (b) two matching literals print hand and edit nothing.
    two = tmp_path / "two.py"
    two.write_text(ONE + ONE.replace("CONFIG", "OTHER"), encoding="utf-8")
    rec = _record(tmp_path, two, "t", "column", ["b"], ["a"])
    codemod.main(["declare-columns", "--record", str(rec), str(two)])
    assert "hand t::n t" in capsys.readouterr().out
    assert "passthrough" not in two.read_text()

    # (c) a table record adds one tables entry beside its sources.
    table = tmp_path / "table.py"
    table.write_text('CFG = {"sources": {"s": {}}, "tables": []}\n', encoding="utf-8")
    rec = _record(tmp_path, table, "s", "table", ["x", "y"], [])
    codemod.main(["declare-columns", "--record", str(rec), str(table)])
    tables = ast.literal_eval(table.read_text().split("=", 1)[1].strip())["tables"]
    assert [t["name"] for t in tables] == ["s"]
    assert [c["name"] for c in tables[0]["columns"]] == ["x", "y"]


def test_source_schema_mode(
    codemod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "test_x.py"
    src.write_text(
        "from decoy_engine import run_mask_pipeline_chunked\n\n"
        "def f(cfg, name, chunks, kw):\n"
        "    run_mask_pipeline_chunked(cfg, chunks, table='t', engine_version='1')\n"
        "    run_mask_pipeline_chunked(cfg, chunks, table=name, source_schema=S)\n"
        "    run_mask_pipeline_chunked(cfg, chunks, **kw)\n"
        "    run_mask_pipeline_chunked(cfg, chunks, table=name, engine_version='1')\n",
        encoding="utf-8",
    )
    assert codemod.main(["source-schema", str(src)]) == 0
    out = capsys.readouterr().out
    assert f"hand {src}:6" in out
    text = src.read_text()
    assert text.count("source_schema=") == 3
    assert "source_schema=declared_schema(cfg, 't')" in text
    assert "source_schema=declared_schema(cfg, name)" in text
    assert text.count("from tests._declared_schema import declared_schema") == 1
    ast.parse(text)
    # Second run: nothing left to insert, the import is not repeated.
    codemod.main(["source-schema", str(src)])
    assert src.read_text().count("declared_schema import") == 1
    assert src.read_text() == text


def test_probe_records_through_the_public_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.profile import DeclarationRequest, prepare_declared_sources

    out = tmp_path / "probe.jsonl"
    monkeypatch.setenv("DECOY_DECLARE_PROBE", str(out))
    path = REPO / "scripts" / "dev" / "codemods" / "declare_columns_probe.py"
    spec = importlib.util.spec_from_file_location("declare_columns_probe", path)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    calls: list[int] = []
    real = probe.declaration_findings
    monkeypatch.setattr(
        probe, "declaration_findings", lambda c, i: (calls.append(1), real(c, i))[1]
    )
    config = {
        "tables": [{"name": "t", "columns": [{"name": "a", "strategy": "hash"}]}],
        "sources": {},
    }
    schema = pa.schema([("a", pa.string()), ("zz", pa.string())])
    probe.pytest_sessionstart(None)
    try:
        for _ in range(2):
            prepare_declared_sources(DeclarationRequest.for_supplied(config, {"t": schema}))
    finally:
        probe.pytest_sessionfinish(None)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(calls) == 2
    assert [(r["table"], r["kind"], r["undeclared"], r["declared"]) for r in rows] == [
        ("t", "column", ["zz"], ["a"])
    ]
    # The real check is back after the session.
    with pytest.raises(PipelineValidationError):
        prepare_declared_sources(DeclarationRequest.for_supplied(config, {"t": schema}))
