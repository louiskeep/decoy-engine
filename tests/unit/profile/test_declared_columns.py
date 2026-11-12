"""CONFIG-EXPLICIT E2a: the declared-columns boundary (AT-E2a-1 to AT-E2a-7)."""

from __future__ import annotations

import copy
import inspect
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import compile_plan, declared_columns
from decoy_engine.errors import PipelineValidationError
from decoy_engine.execution._output_projection import known_output_columns
from decoy_engine.fileaccess import ENTRY_POINTS
from decoy_engine.profile import (
    DeclarationRequest,
    PreparedDeclaredSources,
    _declared_columns,
    check_columns_declared,
    declaration_findings,
    prepare_declared_sources,
    profile_source,
    source_columns,
)
from decoy_engine.profile._declared_columns import DeclarationInput, arrow_columns
from tests._declared_schema import declare_rest

MARK_A, MARK_B = "zz_marker_a", "zz_marker_b"


def _col(name: str, strategy: str = "passthrough") -> dict[str, Any]:
    return {"name": name, "strategy": strategy}


def _config(sources: dict[str, Any], tables: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "global_settings": {"seed": 0},
        "sources": sources,
        "tables": tables,
        "relationships": [],
        **extra,
    }


def _csv(path: Path, **extra: Any) -> dict[str, Any]:
    return {"type": "file", "format": "csv", "path": str(path), "has_header": True, **extra}


def _inputs(table: str, *columns: str) -> list[DeclarationInput]:
    return [DeclarationInput(table, tuple(columns), "test")]


# AT-E2a-1 -----------------------------------------------------------------------------------


def _descriptor(kind: str, tmp: Path) -> dict[str, Any]:
    if kind == "headered":
        (tmp / "d.csv").write_text("id,email,age\n1,a,2\n", encoding="utf-8")
        return _csv(tmp / "d.csv")
    if kind == "headerless":
        (tmp / "d.csv").write_text("1,a,2\n", encoding="utf-8")
        return _csv(tmp / "d.csv", has_header=False, column_names=["id", "email", "age"])
    if kind == "names_file":
        (tmp / "d.csv").write_text("1,a,2\n", encoding="utf-8")
        (tmp / "h.csv").write_text("id,email,age\n", encoding="utf-8")
        return _csv(tmp / "d.csv", has_header=False, column_names_file=str(tmp / "h.csv"))
    if kind in ("tab", "pipe"):
        sep = {"tab": "\t", "pipe": "|"}[kind]
        (tmp / "d.csv").write_text(f"id{sep}email{sep}age\n1{sep}a{sep}2\n", encoding="utf-8")
        return _csv(tmp / "d.csv", delimiter=sep)
    if kind == "parquet_index":
        frame = pd.DataFrame({"email": ["a"], "age": [2]}, index=pd.Index([1], name="id_idx"))
        pq.write_table(pa.Table.from_pandas(frame), tmp / "d.parquet")
        return {"type": "file", "format": "parquet", "path": str(tmp / "d.parquet")}
    if kind == "parquet":
        pq.write_table(pa.table({"id": [1], "email": ["a"], "age": [2]}), tmp / "d.parquet")
        return {"type": "file", "format": "parquet", "path": str(tmp / "d.parquet")}
    (tmp / "d.txt").write_text("alice   030\n", encoding="utf-8")
    layout = {
        "columns": [
            {"name": "name", "start": 0, "width": 8, "type": "str"},
            {"name": "age", "start": 8, "width": 3, "type": "int"},
        ]
    }
    return {"type": "file", "format": "fixed_width", "path": str(tmp / "d.txt"), "layout": layout}


@pytest.mark.parametrize(
    "kind",
    [
        "headered",
        "headerless",
        "names_file",
        "tab",
        "pipe",
        "parquet",
        "parquet_index",
        "fixed_width",
    ],
)
def test_source_columns_match_profile(kind: str, tmp_path: Path) -> None:
    descriptor = _descriptor(kind, tmp_path)
    config = declare_rest(_config({"t": descriptor}, []))
    profiled = profile_source(config, seed=0).tables[0]
    assert source_columns(descriptor, table="t") == tuple(c.name for c in profiled.columns)
    for name in (
        "decoy_engine.profile._declared_columns.source_columns",
        "decoy_engine.profile._declared_columns.prepare_declared_sources",
    ):
        assert name in ENTRY_POINTS


# AT-E2a-2 -----------------------------------------------------------------------------------

_GOLDEN_KEY = ["member_id", "plan_id", "effective_date"]
_ALL_DECLARED: list[tuple[str, dict[str, Any], tuple[str, ...]]] = [
    (
        "listed",
        {"tables": [{"name": "t", "columns": [_col("a"), _col("b", "hash")]}]},
        ("a", "b"),
    ),
    (
        "drop_column",
        {
            "tables": [
                {
                    "name": "t",
                    "columns": [_col("a")],
                    "transforms": [{"op": "drop_column", "columns": ["b"]}],
                }
            ]
        },
        ("a", "b"),
    ),
    (
        "composite",
        {
            "tables": [
                {"name": "enrollments", "columns": [_col(c) for c in _GOLDEN_KEY]},
                {"name": "claims", "columns": []},
            ],
            "relationships": [
                {
                    "parent": {"table": "enrollments", "columns": _GOLDEN_KEY},
                    "children": [{"table": "claims", "columns": _GOLDEN_KEY}],
                    "orphan_policy": "fail",
                }
            ],
        },
        tuple(_GOLDEN_KEY),
    ),
    (
        "provider_less_faker",
        {"tables": [{"name": "t", "columns": [_col("a", "faker")]}]},
        ("a",),
    ),
]


@pytest.mark.parametrize(("label", "extra", "columns"), _ALL_DECLARED, ids=lambda v: str(v)[:20])
def test_all_declared_passes(label: str, extra: dict[str, Any], columns: tuple[str, ...]) -> None:
    config = _config({}, **extra)
    table = "claims" if label == "composite" else "t"
    assert check_columns_declared(config, _inputs(table, *columns)) is None


# AT-E2a-3 and AT-E2a-4 ----------------------------------------------------------------------


def test_undeclared_column_refused_value_free() -> None:
    config = _config({}, [{"name": "events", "columns": [_col("id")]}])
    with pytest.raises(PipelineValidationError) as caught:
        check_columns_declared(config, _inputs("events", MARK_A, MARK_B, "id"))
    err = caught.value
    assert err.code == "source_file.undeclared_column"
    assert str(err).startswith("source_file.undeclared_column: source events column 1 of 3 ")
    assert "(2 undeclared)" in str(err)
    for text in (str(err), repr(err)):
        assert MARK_A not in text
        assert MARK_B not in text
    assert err.__cause__ is None
    assert err.__context__ is None


def test_undeclared_tables_and_order() -> None:
    tables = [
        {"name": "a", "columns": [_col("x")]},
        {"name": "b", "columns": [_col("x")]},
        {"name": "c", "columns": [_col("fk"), _col("other", "")]},
        {"name": "d", "columns": [_col("v")]},
        {"name": "gen", "generate_columns": [{"name": "g"}], "row_count": 3},
    ]
    scalar_fk = {
        "parent": {"table": "a", "columns": ["x"]},
        "children": [{"table": "d", "columns": ["fk"]}],
        "orphan_policy": "preserve",
        "namespace": "ns",
    }
    config = _config({}, tables, relationships=[scalar_fk])
    # An unlisted table is refused before any column check, in input order.
    inputs = [
        DeclarationInput("a", ("x", "zz"), "t"),
        DeclarationInput("ghost", ("q",), "t"),
        DeclarationInput("ghost2", None, "t"),
    ]
    for _ in range(2):
        with pytest.raises(PipelineValidationError) as caught:
            check_columns_declared(config, inputs)
        assert str(caught.value).startswith("source_file.undeclared_table: source ghost has no")
        assert "(1 columns)" in str(caught.value)
    # First undeclared position, not the first input.
    with pytest.raises(PipelineValidationError) as caught:
        check_columns_declared(config, _inputs("a", "x", "zz"))
    assert "column 2 of 2 (1 undeclared)" in str(caught.value)
    # An entry with an empty strategy is undeclared.
    with pytest.raises(PipelineValidationError) as caught:
        check_columns_declared(config, _inputs("c", "fk", "other"))
    assert "column 2 of 2 (1 undeclared)" in str(caught.value)
    # A scalar FK child left out of `columns` is undeclared; only composite children count.
    with pytest.raises(PipelineValidationError) as caught:
        check_columns_declared(config, _inputs("d", "v", "fk"))
    assert "column 2 of 2 (1 undeclared)" in str(caught.value)
    # A generate table has no source columns to declare.
    assert check_columns_declared(config, _inputs("gen", "anything")) is None


# AT-E2b-1 ------------------------------------------------------------------------------------


def test_declared_column_missing_from_source_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared column the source lacks is refused from the header alone, value-free."""
    from decoy_engine.profile import _readers_csv

    path = tmp_path / "t.csv"
    path.write_text(f"id,email,age\n1,{MARK_A},{MARK_B}\n", encoding="utf-8")
    config = _config(
        {"t": _csv(path)},
        [{"name": "t", "columns": [_col(c) for c in ("id", "email", "age", "account_number")]}],
    )
    rows: list[str] = []
    for name in ("_read", "to_frame", "sample_frame"):
        real = getattr(_readers_csv.CsvFileSource, name)

        def spy(*a: Any, _real: Any = real, _n: str = name, **k: Any) -> Any:
            rows.append(_n)
            return _real(*a, **k)

        monkeypatch.setattr(_readers_csv.CsvFileSource, name, spy)
    with pytest.raises(PipelineValidationError) as caught:
        prepare_declared_sources(DeclarationRequest.for_descriptors(config))
    err = caught.value
    assert err.code == "source_file.declared_column_missing"
    assert str(err).startswith(
        "source_file.declared_column_missing: source t column account_number"
    )
    assert "(1 missing)" in str(err)
    for text in (str(err), repr(err)):
        assert MARK_A not in text
        assert MARK_B not in text
        assert str(path) not in text
    assert err.__cause__ is None
    assert err.__context__ is None
    assert rows == []


def test_missing_findings_order_and_scope() -> None:
    """Undeclared table, then undeclared column, then missing; mask tables only."""
    tables = [
        {"name": "a", "columns": [_col("x"), _col("y"), _col("z")]},
        {"name": "gen", "generate_columns": [{"name": "g"}], "row_count": 3},
        {
            "name": "d",
            "columns": [_col("k")],
            "transforms": [{"op": "drop_column", "columns": ["gone"]}],
        },
    ]
    config = _config({}, tables)
    # Missing columns are named in config order, each count includes all of them.
    found = declaration_findings(config, _inputs("a", "x"))
    assert [f.kind for f in found] == ["missing"]
    assert "column y is declared but the source has no such column (2 missing)" in found[0].detail
    assert found[0].undeclared == ("y", "z")
    # An undeclared source column is reported before a missing declared one.
    both = declaration_findings(config, _inputs("a", "x", "q"))
    assert [f.kind for f in both] == ["column", "missing"]
    # A table finding comes first of all.
    mixed = declaration_findings(
        config, [*_inputs("a", "x"), DeclarationInput("ghost", ("q",), "t")]
    )
    assert [f.kind for f in mixed] == ["table", "missing"]
    # A drop_column naming an absent column is refused; a generate table has no source.
    drop = declaration_findings(config, _inputs("d", "k"))
    assert drop[0].undeclared == ("gone",)
    assert declaration_findings(config, _inputs("gen", "anything")) == ()
    # An existence-only input has no columns to compare.
    assert declaration_findings(config, [DeclarationInput("a", None, "t")]) == ()
    # Duplicate table inputs are checked independently.
    twice = declaration_findings(config, [*_inputs("a", "x", "y", "z"), *_inputs("a", "x")])
    assert [f.kind for f in twice] == ["missing"]


# AT-E2a-5 -----------------------------------------------------------------------------------


def test_source_columns_reads_header_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "big.csv"
    rows = ["id,email"] + [f"{i},e{i}" for i in range(1000)]
    rows[5] = "only-one-field"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    seen: list[Any] = []
    real = _declared_columns.read_csv_source

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("nrows"))
        return real(*args, **kwargs)

    monkeypatch.setattr(_declared_columns, "read_csv_source", spy)
    assert source_columns(_csv(path), table="t") == ("id", "email")
    assert seen == [0]

    opened: list[Any] = []
    monkeypatch.setattr(_declared_columns, "read_csv_source", lambda *a, **k: opened.append(1))
    headerless = _csv(path, has_header=False, column_names=["a", "b"])
    assert source_columns(headerless, table="t") == ("a", "b")
    assert opened == []

    frame = pd.DataFrame({"a": [1]}, index=pd.Index(["k"], name="idx"))
    assert arrow_columns(pa.Table.from_pandas(frame).schema) == ("a",)


# AT-E2a-6 -----------------------------------------------------------------------------------


def _chunk(*names: str) -> pa.Table:
    return pa.table({n: ["v"] for n in names})


def test_prepared_capability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(TypeError):
        PreparedDeclaredSources(object(), {}, ())  # type: ignore[arg-type]
    path = tmp_path / "t.csv"
    path.write_text("id,email\n1,a\n", encoding="utf-8")
    config = _config({"t": _csv(path)}, [{"name": "t", "columns": [_col("id"), _col("email")]}])
    ok = pa.schema([("id", pa.string()), ("email", pa.string())])
    extra = pa.schema([("id", pa.string()), ("email", pa.string()), ("zz", pa.string())])

    prepared = prepare_declared_sources(DeclarationRequest.for_run(config, {"t": ok}))
    assert prepared.matches(config)
    with pytest.raises(PipelineValidationError):
        prepare_declared_sources(DeclarationRequest.for_run(config, {"t": extra}))
    wide = _config({"t": _csv(path)}, [{"name": "t", "columns": [_col("id")]}])
    # The supplied schema is fully declared here, so only the descriptor can refuse.
    only_id = pa.schema([("id", pa.string())])
    prepare_declared_sources(DeclarationRequest.for_supplied(wide, {"t": only_id}))
    with pytest.raises(PipelineValidationError):
        prepare_declared_sources(DeclarationRequest.for_run(wide, {"t": only_id}))

    # Loader schemas join the supplied ones; one table may not carry two schemas or a non-schema.
    DeclarationRequest.for_run(config, {"t": ok}, {"t": ok})
    with pytest.raises(ValueError, match="two different declared schemas"):
        DeclarationRequest.for_run(config, {"t": ok}, {"t": extra})
    with pytest.raises(TypeError, match="must be a pa.Schema"):
        DeclarationRequest.for_run(config, {}, {"t": object()})  # type: ignore[dict-item]
    with pytest.raises(PipelineValidationError):
        prepare_declared_sources(DeclarationRequest.for_run(config, {}, {"t": extra}))

    calls: list[int] = []
    real = _declared_columns.check_columns_declared

    def spy(cfg: Any, inputs: Any) -> None:
        calls.append(len(inputs))
        real(cfg, inputs)

    monkeypatch.setattr(_declared_columns, "check_columns_declared", spy)
    chunks = prepared.checked_chunks(
        "t", ("id", "email"), [_chunk("id", "email"), _chunk("id", "zz")]
    )
    assert next(chunks).column_names == ["id", "email"]
    with pytest.raises(PipelineValidationError):
        next(chunks)
    served: list[str] = []

    def raw(name: str) -> pa.Table:
        served.append(name)
        return _chunk("id", "email", "zz") if name == "bad" else _chunk("id", "email")

    loader = prepared.checked_loader(raw, ("t", "bad"))
    assert loader("t").column_names == ["id", "email"]
    with pytest.raises(PipelineValidationError):
        loader("bad")
    assert len(calls) == 5
    # A table with no declared schema is refused before the loader runs.
    with pytest.raises(ValueError, match="no entry in source_loader_schemas"):
        loader("other")
    assert served == ["t", "bad"]


def test_request_shapes_keep_duplicates_separate(tmp_path: Path) -> None:
    path = tmp_path / "t.csv"
    path.write_text("id\n1\n", encoding="utf-8")
    config = _config({"t": _csv(path)}, [{"name": "t", "columns": [_col("id")]}])
    schema = pa.schema([("id", pa.string())])
    run = prepare_declared_sources(DeclarationRequest.for_run(config, {"t": schema}))
    assert [i.origin for i in run.inputs] == ["descriptor", "supplied"]
    chunks = prepare_declared_sources(DeclarationRequest.for_chunks(config, "t", schema))
    assert [(i.table, i.columns is None) for i in chunks.inputs] == [
        ("t", True),
        ("t", True),
        ("t", False),
    ]
    sig = inspect.signature(DeclarationRequest.for_chunks)
    assert list(sig.parameters) == ["config", "table", "source_schema"]


# AT-E2a-7 -----------------------------------------------------------------------------------


def _fk(parent: str, pcols: list[str], child: str, ccols: list[str]) -> dict[str, Any]:
    return {
        "parent": {"table": parent, "columns": pcols},
        "children": [{"table": child, "columns": ccols}],
        "orphan_policy": "preserve",
        "namespace": "ns",
    }


def _corpus() -> list[tuple[dict[str, Any], dict[str, list[str]]]]:
    """Configs paired with source columns that the config does not declare, per table."""
    return [
        (
            _config(
                {},
                [
                    {"name": "p", "columns": [_col("k1"), _col("k2")]},
                    {"name": "c", "columns": []},
                ],
                relationships=[_fk("p", ["k1", "k2"], "c", ["k1", "k2"])],
            ),
            {},
        ),
        (
            _config(
                {},
                [
                    {"name": "p", "columns": [_col("id")]},
                    {"name": "c", "columns": [_col("pid"), _col("v")]},
                ],
                relationships=[_fk("p", ["id"], "c", ["pid"])],
            ),
            {},
        ),
        (
            _config(
                {},
                [{"name": "n", "columns": [_col("id"), _col("boss")]}],
                relationships=[_fk("n", ["id"], "n", ["boss"])],
            ),
            {},
        ),
        (
            _config(
                {},
                [
                    {
                        "name": "d",
                        "columns": [_col("a")],
                        "transforms": [{"op": "drop_column", "columns": ["b"]}],
                    }
                ],
            ),
            {},
        ),
        # A scalar FK child missing from `columns` is not declared by the relationship.
        (
            _config(
                {},
                [
                    {"name": "p", "columns": [_col("id")]},
                    {"name": "c", "columns": [_col("v")]},
                ],
                relationships=[_fk("p", ["id"], "c", ["pid"])],
            ),
            {"c": ["pid"]},
        ),
        # An entry with an empty strategy is not declared.
        (
            _config({}, [{"name": "e", "columns": [_col("a"), _col("b", "")]}]),
            {"e": ["b"]},
        ),
    ]


def _drops(config: dict[str, Any], name: str) -> set[str]:
    entry = next(t for t in config["tables"] if t["name"] == name)
    return {col for op in entry.get("transforms", []) for col in op["columns"]}


def test_declared_columns_agree_with_de03(tmp_path: Path) -> None:
    for config, extra in _corpus():
        config["sources"] = {}
        tables = [t["name"] for t in config["tables"]]
        # Sources hold every declared column plus the ones the config leaves out, so
        # profile_source sees what the plan would.
        for name in tables:
            declared = list(declared_columns(config, name))
            names = declared + extra.get(name, [])
            path = tmp_path / f"{name}.csv"
            path.write_text(",".join(names) + "\n" + ",".join("1" for _ in names) + "\n")
            config["sources"][name] = _csv(path)
        # The gate would refuse the columns the config leaves out, so profile a copy that
        # lists them and compile the original against that profile.
        profile = profile_source(declare_rest(copy.deepcopy(config)), seed=0)
        plan = compile_plan(config, profile, decoy_engine_version="0")
        for name in tables:
            assert set(declared_columns(config, name)) - _drops(config, name) == set(
                known_output_columns(plan, name)
            ), name
