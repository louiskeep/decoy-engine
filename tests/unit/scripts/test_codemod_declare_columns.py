"""AT-E2a-9: the `declare-columns` and `source-schema` modes of the CONFIG-EXPLICIT codemod."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
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
    from decoy_engine.profile import declaration_findings
    from decoy_engine.profile._declared_columns import DeclarationInput

    config = {"sources": {"s": {}}, "tables": []}
    (finding,) = declaration_findings(config, [DeclarationInput("s", ("x", "y"), "t")])
    assert finding.undeclared == ("x", "y")
    rec = _record(tmp_path, table, "s", "table", list(finding.undeclared), [])
    codemod.main(["declare-columns", "--record", str(rec), str(table)])
    tables = ast.literal_eval(table.read_text().split("=", 1)[1].strip())["tables"]
    assert [t["name"] for t in tables] == ["s"]
    assert [c["name"] for c in tables[0]["columns"]] == ["x", "y"]


def test_declare_columns_reads_a_local_helper_module(
    codemod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "helper_cfg.py").write_text(ONE.replace("CONFIG", "HELPER"), encoding="utf-8")
    test = tmp_path / "test_uses_helper.py"
    test.write_text("from helper_cfg import HELPER\n", encoding="utf-8")
    rec = _record(tmp_path, test, "t", "column", ["b"], ["a"])
    assert codemod.main(["declare-columns", "--record", str(rec), str(test)]) == 0
    assert '"name": "b", "strategy": "passthrough"' in (tmp_path / "helper_cfg.py").read_text()


def test_table_record_matches_only_the_config_naming_the_source(
    codemod: ModuleType, tmp_path: Path
) -> None:
    both = tmp_path / "both.py"
    both.write_text(
        'A = {"sources": {"other": {}}, "tables": []}\nB = {"sources": {"s": {}}, "tables": []}\n',
        encoding="utf-8",
    )
    rec = _record(tmp_path, both, "s", "table", ["x"], [])
    codemod.main(["declare-columns", "--record", str(rec), str(both)])
    ns: dict = {}
    exec(both.read_text(), ns)  # noqa: S102
    assert ns["A"]["tables"] == []
    assert [t["name"] for t in ns["B"]["tables"]] == ["s"]


def test_declare_columns_merges_only_the_manifest_run(codemod: ModuleType, tmp_path: Path) -> None:
    one = tmp_path / "one.py"
    one.write_text(ONE, encoding="utf-8")
    out = tmp_path / "rec.jsonl"
    current = _record(tmp_path, one, "t", "column", ["b"], ["a"])
    stale = current.read_text(encoding="utf-8").replace('["b"]', '["zz_stale"]')
    current.rename(tmp_path / "rec.jsonl.run2.gw0")
    (tmp_path / "rec.jsonl.run1.gw7").write_text(stale, encoding="utf-8")
    out.write_text(json.dumps({"run": "run2"}) + "\n", encoding="utf-8")
    assert codemod.main(["declare-columns", "--record", str(out), str(one)]) == 0
    text = one.read_text()
    assert text.count("passthrough") == 1
    assert "zz_stale" not in text


def test_probe_run_under_xdist_ignores_stale_worker_files(
    codemod: ModuleType, tmp_path: Path
) -> None:
    import subprocess
    import sys

    (tmp_path / "test_probe_target.py").write_text(
        "import pyarrow as pa\n"
        "from decoy_engine.profile import DeclarationRequest, prepare_declared_sources\n"
        "CONFIG = {'sources': {}, 'tables': [{'name': 't', 'columns': "
        "[{'name': 'a', 'strategy': 'hash'}]}]}\n"
        "SCHEMA = pa.schema([('a', pa.string()), ('zz_fresh', pa.string())])\n\n"
        "def test_one():\n"
        "    prepare_declared_sources(DeclarationRequest.for_supplied(CONFIG, {'t': SCHEMA}))\n\n"
        "def test_two():\n"
        "    prepare_declared_sources(DeclarationRequest.for_supplied(CONFIG, {'t': SCHEMA}))\n",
        encoding="utf-8",
    )
    out = tmp_path / "probe.jsonl"
    stale_row = {
        "node": "n", "table": "t", "kind": "column", "undeclared": ["zz_stale"],
        "declared": ["a"], "frame": str(tmp_path / "test_probe_target.py") + ":1",
    }  # fmt: skip
    (tmp_path / "probe.jsonl.oldrun.gw7").write_text(json.dumps(stale_row) + "\n", encoding="utf-8")
    # An outer xdist run leaves its own worker variables behind; the inner run must start clean.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTEST_XDIST", "DECOY_DECLARE_PROBE"))
    }
    env["DECOY_DECLARE_PROBE"] = str(out)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "scripts" / "dev" / "codemods"), env.get("PYTHONPATH", "")]
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-p", "declare_columns_probe", "-n", "2", "-q",
         "-p", "no:cacheprovider", str(tmp_path / "test_probe_target.py")],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout[-2000:]
    target = tmp_path / "test_probe_target.py"
    assert codemod.main(["declare-columns", "--record", str(out), str(target)]) == 0
    text = target.read_text()
    assert "zz_fresh" in text
    assert "zz_stale" not in text


def test_shared_helper_literal_is_edited_once(
    codemod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "helper_cfg.py").write_text(ONE.replace("CONFIG", "HELPER"), encoding="utf-8")
    rows = []
    for name, undeclared in (("test_one.py", ["b", "c"]), ("test_two.py", ["b"])):
        test = tmp_path / name
        test.write_text("from helper_cfg import HELPER\n", encoding="utf-8")
        rows.append(
            json.dumps(
                {
                    "node": f"{name}::n",
                    "table": "t",
                    "kind": "column",
                    "undeclared": undeclared,
                    "declared": ["a"],
                    "frame": f"{test}:1",
                }
            )
        )
    rec = tmp_path / "rec.jsonl"
    rec.write_text("\n".join(rows) + "\n", encoding="utf-8")
    codemod.main(["declare-columns", "--record", str(rec), str(tmp_path)])
    helper = ast.literal_eval((tmp_path / "helper_cfg.py").read_text().split("=", 1)[1].strip())
    names = [c["name"] for c in helper["tables"][0]["columns"]]
    assert names == ["a", "b", "c"]


def test_two_undeclared_tables_in_one_config_stay_separate(
    codemod: ModuleType, tmp_path: Path
) -> None:
    both = tmp_path / "both.py"
    both.write_text('CFG = {"sources": {"s1": {}, "s2": {}}, "tables": []}\n', encoding="utf-8")
    rows = [
        {"node": "n", "table": t, "kind": "table", "undeclared": u, "declared": [],
         "frame": f"{both}:1"}
        for t, u in (("s1", ["a"]), ("s2", ["b"]))
    ]  # fmt: skip
    rec = tmp_path / "rec.jsonl"
    rec.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    codemod.main(["declare-columns", "--record", str(rec), str(both)])
    tables = ast.literal_eval(both.read_text().split("=", 1)[1].strip())["tables"]
    assert {t["name"]: [c["name"] for c in t["columns"]] for t in tables} == {
        "s1": ["a"],
        "s2": ["b"],
    }


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
        "    run_mask_pipeline_chunked(cfg, chunks, table=name, engine_version='1')\n"
        "    run_mask_pipeline_chunked(cfg, chunks, table='t', **kw)\n",
        encoding="utf-8",
    )
    assert codemod.main(["source-schema", str(src)]) == 0
    out = capsys.readouterr().out
    assert f"hand {src}:6" in out
    assert f"hand {src}:8" in out
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
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
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
        probe.pytest_sessionfinish(_Session("no"))
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(calls) == 2
    assert [(r["table"], r["kind"], r["undeclared"], r["declared"]) for r in rows] == [
        ("t", "column", ["zz"], ["a"])
    ]
    # The real check is back after the session.
    with pytest.raises(PipelineValidationError):
        prepare_declared_sources(DeclarationRequest.for_supplied(config, {"t": schema}))


def _load_probe() -> ModuleType:
    path = REPO / "scripts" / "dev" / "codemods" / "declare_columns_probe.py"
    spec = importlib.util.spec_from_file_location("declare_columns_probe_xdist", path)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    return probe


class _Session:
    def __init__(self, dist: str) -> None:
        self.config = type("C", (), {"option": type("O", (), {"dist": dist})()})()


def test_probe_writes_per_worker_files_under_xdist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "probe.jsonl"
    out.write_text("stale\n", encoding="utf-8")
    monkeypatch.setenv("DECOY_DECLARE_PROBE", str(out))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    probe = _load_probe()
    probe.pytest_configure(None)
    run = os.environ["DECOY_DECLARE_PROBE_RUN"]
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    probe.pytest_sessionfinish(_Session("load"))
    assert (tmp_path / f"probe.jsonl.{run}.gw1").is_file()
    assert out.read_text() == "stale\n"
    # The controller records nothing: OUT holds only the manifest naming this run.
    monkeypatch.delenv("PYTEST_XDIST_WORKER")
    probe.pytest_sessionfinish(_Session("load"))
    assert [json.loads(x) for x in out.read_text().splitlines()] == [{"run": run}]
    probe.pytest_sessionfinish(_Session("no"))
    assert out.read_text() == ""


def test_missing_record_is_hand(
    codemod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A declared column the source lacks is for a person to fix; the file is left alone."""
    cfg = tmp_path / "cfg.py"
    text = 'CFG = {"sources": {"t": {}}, "tables": []}\n'  # a table record would add an entry
    cfg.write_text(text, encoding="utf-8")
    rec = _record(tmp_path, cfg, "t", "missing", ["a"], [])
    codemod.main(["declare-columns", "--record", str(rec), str(cfg)])
    assert "hand t::n t" in capsys.readouterr().out
    assert cfg.read_text() == text
