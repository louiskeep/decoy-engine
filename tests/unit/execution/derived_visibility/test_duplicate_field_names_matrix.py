"""T10: the duplicate-field-name admissibility table, every public entry, default settings.

Plan `docs/plans/2026-10-31-slice0-derived-visibility.md` section 9. Every cell names its
section 4 table row, its expected outcome for the duplicate input, its unique control and its
label. There is no free Cartesian product: each cell is listed explicitly.

`D` (duplicate refused): `TransformError` with `code == "duplicate_source_field_names"`,
`reason_from_exception` equal to `runtime.duplicate_source_field_names`, a message that names
the table and a count and no column name, the shared-function spy holding exactly one raise
that is the propagated exception, and no handler, `apply_derived`, `to_pandas_fk_safe` or
`walk_dataframe` call on the duplicate table. CONFIG-FIRST cells are refused before any table
is read: the duplicate and its unique control raise the same refusal and the spy stays at zero.
"""

from __future__ import annotations

import os
import re
import sys
import textwrap
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import run_pipeline
from decoy_engine.execution import _guards
from decoy_engine.execution._transforms import TransformError
from decoy_engine.profile._readers import LazySource
from decoy_engine.reason_adapter import reason_from_exception
from tests._declared_schema import chunked_entry_kwargs, declare_rest
from tests._keyed_secret import THROWAWAY_SECRET
from tests.unit.execution._transform_testkit import ENGINE_VERSION, fk_config

CODE = "duplicate_source_field_names"
SECRET_ENV = "DECOY_TEST_THROWAWAY_SECRET"
_SHARED = "refuse_duplicate_field_names"
# Every table and column name of both tables; none may appear in an isolated result.
_NAMES = ("parent", "child", "cid", "parent_id", "qty", "other", "q2", "note")


# ---------------------------------------------------------------- fixtures


def _parent() -> pa.Table:
    return pa.table({"id": [f"p{i}" for i in range(6)], "note": [f"n{i}" for i in range(6)]})


def _child(*, dup: str | None) -> pa.Table:
    """`cid, parent_id, qty, <dup or other>, q2`; `dup` names the repeated field."""
    n = 12
    cols = {
        "cid": pa.array([f"c{i}" for i in range(n)]),
        "parent_id": pa.array([f"p{i % 6}" for i in range(n)]),
        "qty": pa.array([float(i) for i in range(n)]),
    }
    arrays = list(cols.values())
    names = list(cols)
    second = (
        pa.array([f"p{i % 6}" for i in range(n)])
        if dup == "parent_id"
        else pa.array([float(-i) for i in range(n)])
    )
    arrays.append(second)
    names.append(dup or "other")
    arrays.append(pa.array([0.0] * n))
    names.append("q2")
    return pa.Table.from_arrays(arrays, names=names)


_WORK_COLUMNS: dict[str, list[dict[str, Any]]] = {
    "derived": [
        {"name": "qty", "strategy": "passthrough"},
        {"name": "q2", "strategy": "derived", "provider_config": {"expression": "qty * 2"}},
    ],
    "redact": [{"name": "qty", "strategy": "redact"}],
    "transform": [],
}


def _config(tmp_path: Path, work: str, descriptor_child: pa.Table) -> dict[str, Any]:
    transforms = [{"op": "limit", "n": 10}] if work == "transform" else []
    cfg = fk_config(tmp_path, _parent(), descriptor_child, child_transforms=transforms)
    cfg["tables"][1]["columns"].extend(_WORK_COLUMNS[work])
    cfg["global_settings"]["mask_secret_ref"] = f"env:{SECRET_ENV}"
    return declare_rest(cfg)


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, THROWAWAY_SECRET.hex())


# ---------------------------------------------------------------- spies


@dataclass
class Spies:
    raises: list[BaseException]
    calls: int
    handler_tables: list[str]
    derived_calls: int
    dup_conversions: int
    dup_walks: int


def _replace_everywhere(monkeypatch: pytest.MonkeyPatch, original: Any, wrapper: Any) -> None:
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("decoy_engine"):
            continue
        for attr, value in list(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, attr, wrapper)


def _has_dup(names: Any) -> bool:
    names = list(names)
    return len(set(names)) != len(names)


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> Spies:
    import decoy_engine.execution._strategies._derived as derived_handler_mod
    from decoy_engine.execution import _fk_keys
    from decoy_engine.execution._strategies import SCALAR_HANDLERS
    from decoy_engine.profile import _walk

    s = Spies([], 0, [], 0, 0, 0)
    shared = getattr(_guards, _SHARED, None)
    if shared is not None:

        def shared_spy(*args: Any, **kwargs: Any) -> Any:
            s.calls += 1
            try:
                return shared(*args, **kwargs)
            except BaseException as exc:
                s.raises.append(exc)
                raise

        monkeypatch.setattr(_guards, _SHARED, shared_spy)

    for cls in {type(h) for h in SCALAR_HANDLERS.values()}:
        real_run = cls.run

        def run_spy(
            self: Any, df: Any, column: str, plan: Any, ctx: Any, _r: Any = real_run
        ) -> Any:
            if _has_dup(df.columns):
                s.handler_tables.append(f"DUP:{getattr(ctx, 'current_table', '?')}")
            else:
                s.handler_tables.append(getattr(ctx, "current_table", "?"))
            return _r(self, df, column, plan, ctx)

        monkeypatch.setattr(cls, "run", run_spy)

    import decoy_engine.execution.out_of_core._batch_join
    import decoy_engine.execution.out_of_core._relation
    import decoy_engine.execution.out_of_core._stream_join  # noqa: F401
    from decoy_engine.execution.out_of_core import _mask as ooc_mask

    real_mask_column = ooc_mask.mask_column

    def mask_column_spy(*a: Any, **k: Any) -> Any:
        s.handler_tables.append("ooc")
        return real_mask_column(*a, **k)

    _replace_everywhere(monkeypatch, real_mask_column, mask_column_spy)

    real_apply = derived_handler_mod.apply_derived

    def apply_spy(*a: Any, **k: Any) -> Any:
        s.derived_calls += 1
        return real_apply(*a, **k)

    monkeypatch.setattr(derived_handler_mod, "apply_derived", apply_spy)

    real_convert = _fk_keys.to_pandas_fk_safe

    def convert_spy(table: Any, cols: Any) -> Any:
        if _has_dup(table.schema.names):
            s.dup_conversions += 1
        return real_convert(table, cols)

    _replace_everywhere(monkeypatch, real_convert, convert_spy)

    real_walk = _walk.walk_dataframe

    def walk_spy(df: Any, *a: Any, **k: Any) -> Any:
        if _has_dup(df.columns):
            s.dup_walks += 1
        return real_walk(df, *a, **k)

    _replace_everywhere(monkeypatch, real_walk, walk_spy)
    return s


def assert_d(exc: BaseException, s: Spies, *, any_work_allowed: bool = False) -> None:
    assert type(exc) is TransformError, repr(exc)
    assert exc.code == CODE
    reason = reason_from_exception(exc)
    assert (reason.namespace, reason.code) == ("runtime", CODE)
    text = str(exc)
    assert text == exc.message and exc.args == (text,)
    assert "'child'" in text and "1 duplicated field name" in text
    for column in ("qty", "parent_id", "cid", "other", "q2"):
        assert column not in text
    assert len(s.raises) == 1 and s.raises[0] is exc
    assert not [t for t in s.handler_tables if t.startswith("DUP:") or t == "child"]
    if not any_work_allowed:
        assert s.handler_tables == []
        assert s.derived_calls == 0
    assert s.dup_conversions == 0 and s.dup_walks == 0


# ---------------------------------------------------------------- E1 run_pipeline


def _sources(kind: str, tmp_path: Path, child: pa.Table) -> tuple[Any, Any]:
    parent = _parent()
    if kind in ("res", "desc"):
        return {"parent": parent, "child": child}, None
    if kind == "lazy":
        paths = {}
        for name, table in (("parent", parent), ("child", child)):
            path = tmp_path / f"lazy_{name}.parquet"
            pq.write_table(table, path)
            paths[name] = LazySource(path=path)
        return paths, None
    tables = {"parent": parent, "child": child}
    return {}, (lambda name: tables[name])


def _e1(tmp_path: Path, route: str, kind: str, work: str, *, dup: str | None) -> Any:
    clean = _child(dup=None)
    if kind == "desc":
        descriptor, runtime = (_child(dup=dup) if dup else clean), clean
    else:
        descriptor, runtime = clean, _child(dup=dup)
    cfg = _config(tmp_path, work, descriptor)
    sources, loader = _sources(kind, tmp_path, runtime)
    declare_rest(cfg, sources or {"child": runtime, "parent": _parent()})
    return run_pipeline(
        cfg,
        sources,
        engine_version=ENGINE_VERSION,
        execution_mode=route,
        source_loader=loader,
        source_loader_schemas=None
        if loader is None
        else {n: loader(n).schema for n in cfg["sources"]},
    )


_ROUTE = {"A": "auto", "F": "full_frame", "S": "sequential", "O": "out_of_core"}


@dataclass(frozen=True)
class Cell:
    route: str
    kind: str
    work: str
    row: str
    label: str
    expect: str = "D"  # "D", or "config-first:<code>"
    control: str = "success"  # "success", "success-ooc", or "ooc-ineligible"
    dup: str = "qty"

    @property
    def id(self) -> str:
        return f"{self.route}-{self.kind}-{self.work}-row{self.row}-{self.label}"


def _cells(routes: str, kinds: tuple[str, ...], works: tuple[str, ...], **kw: Any) -> list[Cell]:
    return [Cell(_ROUTE[r], k, w, **kw) for r in routes for k in kinds for w in works]


E1_CELLS: list[Cell] = [
    *_cells("AFS", ("res",), ("derived", "redact"), row="7", label="FAIL-BEFORE"),
    *_cells("AFS", ("lazy",), ("derived", "redact"), row="7a", label="FAIL-BEFORE"),
    # Type and code are CHARACTERIZATION on main; the message and spy assertions are red there.
    *_cells("AFS", ("res", "lazy", "loader"), ("transform",), row="4", label="FAIL-BEFORE-msg"),
    *_cells("AS", ("loader",), ("derived",), row="13", label="FAIL-BEFORE"),
    *_cells("F", ("loader",), ("derived",), row="13", label="FAIL-BEFORE"),
    *_cells("FS", ("loader",), ("redact",), row="12a", label="FAIL-BEFORE"),
    *_cells("A", ("loader",), ("redact",), row="17", label="FAIL-BEFORE"),
    *_cells("FS", ("loader",), ("redact",), row="10-12", label="FAIL-BEFORE", dup="parent_id"),
    *_cells("AFS", ("desc",), ("derived", "redact"), row="2", label="FAIL-BEFORE"),
    *_cells("O", ("desc",), ("redact",), row="2", label="FAIL-BEFORE", control="success-ooc"),
    *_cells("O", ("desc",), ("derived",), row="2", label="FAIL-BEFORE", control="ooc-ineligible"),
    *_cells("AFS", ("desc",), ("transform",), row="2", label="FAIL-BEFORE"),
    *_cells(
        "O",
        ("res", "lazy"),
        ("derived",),
        row="7-7a",
        label="FAIL-BEFORE",
        control="ooc-ineligible",
    ),
    *_cells(
        "O", ("res", "lazy"), ("redact",), row="7-7a", label="FAIL-BEFORE", control="success-ooc"
    ),
    *_cells("O", ("loader",), ("redact",), row="17", label="FAIL-BEFORE", control="success-ooc"),
    *_cells(
        "O",
        ("loader",),
        ("derived",),
        row="1",
        label="CONFIG-FIRST",
        expect="config-first:out_of_core_ineligible",
        control="ooc-ineligible",
    ),
    *_cells(
        "O",
        ("res", "lazy", "loader", "desc"),
        ("transform",),
        row="1",
        label="CONFIG-FIRST",
        expect="config-first:per_table_transforms_present",
        control="config-first",
    ),
]


def _assert_success(result: Any, work: str, *, ooc: bool) -> None:
    child = result.outputs["child"]
    assert child.column_names == ["cid", "parent_id", "qty", "other", "q2"]
    if work == "derived":
        assert child.column("q2").to_pylist() == [float(2 * i) for i in range(12)]
    elif work == "redact":
        assert child.column("qty").to_pylist() != [float(i) for i in range(12)]
    else:
        assert child.num_rows == 10
    route_keys = set(result.quality_metrics)
    assert ("out_of_core_hash" in route_keys) is ooc


def _refusal_code(exc: BaseException) -> str:
    return reason_from_exception(exc).code


def _assert_ooc_ineligible(exc: BaseException) -> None:
    assert _refusal_code(exc) == "out_of_core_ineligible"
    assert "out_of_core_dynamic_output_type_unsupported" in str(exc)


@pytest.mark.parametrize("cell", E1_CELLS, ids=[c.id for c in E1_CELLS])
def test_e1_run_pipeline(cell: Cell, tmp_path: Path, spies: Spies) -> None:
    # The unique control first: it proves the fixture reaches its listed outcome.
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    if cell.control in ("success", "success-ooc"):
        result = _e1(control_dir, cell.route, cell.kind, cell.work, dup=None)
        # auto picks out_of_core for this all-OOC-compatible redact job (evidence B).
        ooc = cell.control == "success-ooc" or (cell.route == "auto" and cell.work == "redact")
        _assert_success(result, cell.work, ooc=ooc)
    else:
        with pytest.raises(Exception) as ctrl:
            _e1(control_dir, cell.route, cell.kind, cell.work, dup=None)
        if cell.control == "ooc-ineligible":
            _assert_ooc_ineligible(ctrl.value)
        else:
            assert _refusal_code(ctrl.value) == cell.expect.split(":", 1)[1]

    spies.raises.clear()
    spies.handler_tables.clear()
    spies.derived_calls = spies.dup_conversions = spies.dup_walks = 0
    dup_dir = tmp_path / "dup"
    dup_dir.mkdir()
    with pytest.raises(Exception) as exc:
        _e1(dup_dir, cell.route, cell.kind, cell.work, dup=cell.dup)
    if cell.expect.startswith("config-first:"):
        assert _refusal_code(exc.value) == cell.expect.split(":", 1)[1]
        # The spy records refusals; a clean declared descriptor is read (and passes) first.
        assert spies.raises == []
        return
    # A loader table is known only at its loader call, so an earlier unique table may have run.
    assert_d(exc.value, spies, any_work_allowed=cell.kind == "loader")
    assert not (dup_dir / "child_out.parquet").exists()


# ---------------------------------------------------------------- E2 / E3 run_pipeline_isolated


ISOLATED_CELLS: list[Cell] = [
    *_cells("AFS", ("res", "desc"), ("derived", "redact"), row="7/2", label="FAIL-BEFORE"),
    *_cells(
        "O", ("res", "desc"), ("redact",), row="7/2", label="FAIL-BEFORE", control="success-ooc"
    ),
    *_cells(
        "O", ("res", "desc"), ("derived",), row="7/2", label="FAIL-BEFORE", control="ooc-ineligible"
    ),
    *_cells("AFS", ("res",), ("transform",), row="4", label="CHARACTERIZATION-result"),
    *_cells("AFS", ("desc",), ("transform",), row="2", label="FAIL-BEFORE"),
    *_cells(
        "O",
        ("res", "desc"),
        ("transform",),
        row="1",
        label="CONFIG-FIRST",
        expect="config-first:per_table_transforms_present",
        control="config-first",
    ),
]

_SITECUSTOMIZE = textwrap.dedent(
    """
    import os, sys
    _LOG = os.environ.get("DECOY_T10_SPY")
    if _LOG:
        def _log(line):
            with open(_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\\n")
        from decoy_engine.execution import _fk_keys
        _real_convert = _fk_keys.to_pandas_fk_safe
        def _convert(table, cols):
            _log("convert")
            return _real_convert(table, cols)
        _fk_keys.to_pandas_fk_safe = _convert
        import decoy_engine.execution
        from decoy_engine.execution import _guards
        from decoy_engine.execution._strategies import SCALAR_HANDLERS
        for _m in list(sys.modules.values()):
            if getattr(_m, "__name__", "").startswith("decoy_engine"):
                for _k, _v in list(vars(_m).items()):
                    if _v is _real_convert:
                        setattr(_m, _k, _convert)
        _shared = getattr(_guards, "refuse_duplicate_field_names", None)
        if _shared is not None:
            def _shared_spy(*a, **k):
                try:
                    return _shared(*a, **k)
                except BaseException:
                    _log("shared-raise")
                    raise
            _guards.refuse_duplicate_field_names = _shared_spy
        import decoy_engine.execution.out_of_core._batch_join
        import decoy_engine.execution.out_of_core._relation
        import decoy_engine.execution.out_of_core._stream_join
        from decoy_engine.execution.out_of_core import _mask as _ooc
        _real_mask = _ooc.mask_column
        def _mask_spy(*a, **k):
            _log("handler")
            return _real_mask(*a, **k)
        for _m in list(sys.modules.values()):
            if getattr(_m, "__name__", "").startswith("decoy_engine"):
                for _k, _v in list(vars(_m).items()):
                    if _v is _real_mask:
                        setattr(_m, _k, _mask_spy)
        for _cls in {type(h) for h in SCALAR_HANDLERS.values()}:
            def _make(real):
                def run(self, df, column, plan, ctx):
                    _log("handler")
                    return real(self, df, column, plan, ctx)
                return run
            _cls.run = _make(_cls.run)
    """
)


@pytest.fixture
def child_spy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    site = tmp_path / "spy_site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
    log = tmp_path / "spy.log"
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", f"{site}{os.pathsep}{existing}" if existing else str(site))
    monkeypatch.setenv("DECOY_T10_SPY", str(log))
    return log


def _spy_lines(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").split() if log.exists() else []


def _isolated(
    tmp_path: Path, cell: Cell, *, dup: str | None, isolate: bool, child: pa.Table | None = None
) -> Any:
    from decoy_engine.execution import run_pipeline_isolated

    tmp_path.mkdir(parents=True, exist_ok=True)
    clean = _child(dup=None)
    if cell.kind == "desc":
        descriptor, runtime = (_child(dup=dup) if dup else clean), clean
    else:
        descriptor, runtime = clean, (child if child is not None else _child(dup=dup))
    cfg = _config(tmp_path, cell.work, descriptor)
    tables = {"parent": _parent(), "child": runtime}
    declare_rest(cfg, tables)
    spawned: list[int] = []
    result = run_pipeline_isolated(
        cfg,
        tables,
        engine_version=ENGINE_VERSION,
        execution_mode=cell.route,
        isolate=isolate,
        staging_dir=tmp_path / "staging",
        on_spawn=spawned.append,
    )
    return result, spawned


def _assert_value_free_result(result: Any) -> None:
    rendered = []
    for f in fields(result):
        value = getattr(result, f.name)
        if f.name == "reason" and value is not None:
            rendered.append(repr(value.to_payload()))
        rendered.append(repr(value))
    blob = "\n".join(rendered)
    for name in _NAMES:
        assert f"'{name}'" not in blob and f'"{name}"' not in blob, name


def _assert_isolated_control(result: Any, cell: Cell) -> None:
    if cell.control == "ooc-ineligible":
        assert result.outcome == "crashed"
        assert (result.reason.namespace, result.reason.code) == ("route", "out_of_core_ineligible")
    elif cell.control == "config-first":
        assert result.outcome == "crashed"
        assert result.reason.code == "per_table_transforms_present"
    else:
        assert result.outcome == "completed", result.error
        assert result.outputs["child"].column_names == ["cid", "parent_id", "qty", "other", "q2"]


def _assert_isolated_d(result: Any, *, isolated: bool) -> None:
    assert result.outcome == "crashed"
    assert result.outputs is None
    assert result.isolated is isolated
    assert (result.reason.namespace, result.reason.code) == ("runtime", CODE)
    assert result.error == "TransformError"
    _assert_value_free_result(result)


@pytest.mark.parametrize("cell", ISOLATED_CELLS, ids=[c.id for c in ISOLATED_CELLS])
def test_e2_isolated_in_process(cell: Cell, tmp_path: Path, spies: Spies) -> None:
    ctrl, _ = _isolated(tmp_path / "c", cell, dup=None, isolate=False)
    _assert_isolated_control(ctrl, cell)
    spies.raises.clear()
    spies.handler_tables.clear()
    spies.derived_calls = spies.dup_conversions = spies.dup_walks = 0
    spies.calls = 0
    result, _ = _isolated(tmp_path / "d", cell, dup="qty", isolate=False)
    if cell.expect.startswith("config-first:"):
        assert result.reason.code == "per_table_transforms_present"
        assert spies.raises == []
        return
    _assert_isolated_d(result, isolated=False)
    assert len(spies.raises) == 1
    assert spies.handler_tables == [] and spies.derived_calls == 0
    assert spies.dup_conversions == 0 and spies.dup_walks == 0


@pytest.mark.parametrize("cell", ISOLATED_CELLS, ids=[c.id for c in ISOLATED_CELLS])
def test_e3_isolated_default(cell: Cell, tmp_path: Path, spies: Spies, child_spy: Path) -> None:
    ctrl, ctrl_spawned = _isolated(tmp_path / "c", cell, dup=None, isolate=True)
    _assert_isolated_control(ctrl, cell)
    assert ctrl_spawned, "the unique control ran in a child"
    if cell.control in ("success", "success-ooc"):
        assert "handler" in _spy_lines(child_spy), "the child spy is live"
    child_spy.unlink(missing_ok=True)
    spies.raises.clear()
    spies.calls = 0
    result, spawned = _isolated(tmp_path / "d", cell, dup="qty", isolate=True)
    lines = _spy_lines(child_spy)
    if cell.expect.startswith("config-first:"):
        assert result.reason.code == "per_table_transforms_present"
        assert spies.raises == [] and "shared-raise" not in lines
        return
    _assert_isolated_d(result, isolated=True)
    # The parent does not check, so config-only refusals in the child win as in process (E2):
    # a caller table reaches the child as a LazySource, a descriptor is read only there.
    assert spawned and result.pid is not None and result.returncode == 0
    assert spies.raises == []
    assert lines.count("shared-raise") == 1
    assert "handler" not in lines and "convert" not in lines


def _unwritable_child(*, dup: bool) -> pa.Table:
    child = _child(dup="qty" if dup else None)
    interval = pa.array([pa.MonthDayNano([1, 2, 3])] * child.num_rows, pa.month_day_nano_interval())
    return child.append_column("iv", interval)


def test_e3_unwritable_duplicate_surfaces_the_serialization_failure(
    tmp_path: Path, spies: Spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CHARACTERIZATION of the narrowed contract (row 0): a duplicate table Parquet cannot write
    # fails at the parent's serialization, as on main, ahead of the child's refusal. It is
    # raised out of the call, not returned as a result (open finding, plan section 13).
    from decoy_engine.execution import _isolated_run

    cell = Cell("auto", "res", "redact", row="0", label="CHARACTERIZATION")
    spawned: list[object] = []
    monkeypatch.setattr(_isolated_run, "_spawn_and_classify", lambda *a, **k: spawned.append(a))
    with pytest.raises(pa.ArrowNotImplementedError) as exc:
        _isolated(tmp_path, cell, dup="qty", isolate=True, child=_unwritable_child(dup=True))
    err = exc.value
    expected = "Unhandled type for Arrow to Parquet schema conversion: month_day_nano_interval"
    assert str(err) == expected and err.args == (expected,)
    assert getattr(err, "__notes__", []) == []
    assert err.__cause__ is None and err.__context__ is None
    for name in (*_NAMES, "iv"):
        assert name not in str(err), name
    assert spawned == [] and spies.raises == [] and spies.calls == 0
    # The caller owns staging_dir (kept on every path); the unwritable table left no file.
    assert not (tmp_path / "staging" / "sources" / "child.parquet").exists()
    assert not (tmp_path / "staging" / "payload.json").exists()


def test_e3_unwritable_unique_control_still_fails_serialization(tmp_path: Path) -> None:
    # CHARACTERIZATION: the same failure without the duplicate; the parent has no refusal.
    cell = Cell("auto", "res", "redact", row="0", label="CHARACTERIZATION")
    with pytest.raises(pa.ArrowNotImplementedError):
        _isolated(tmp_path, cell, dup=None, isolate=True, child=_unwritable_child(dup=False))


@pytest.mark.parametrize("isolate", [False, True])
def test_isolated_loader_is_config_first(isolate: bool, tmp_path: Path, spies: Spies) -> None:
    # CHARACTERIZATION (CONFIG-FIRST): a source_loader is refused before any table.
    from decoy_engine.execution import run_pipeline_isolated

    cfg = _config(tmp_path, "redact", _child(dup=None))
    with pytest.raises(ValueError, match="source_loader"):
        run_pipeline_isolated(
            cfg,
            None,
            engine_version=ENGINE_VERSION,
            isolate=isolate,
            source_loader=lambda name: _child(dup="qty"),
        )
    assert spies.calls == 0


# ---------------------------------------------------------------- E4 / E5 chunked entries


def _passthrough(*names: str) -> list[dict[str, Any]]:
    """Passthrough entries for the columns of the chunks that the work column leaves out."""
    return [{"name": n, "strategy": "passthrough"} for n in names]


def _chunked_config(work: str) -> dict[str, Any]:
    from tests.native._chunked_entry_support import make_config, redact, truncate

    cols: dict[str, list[dict[str, Any]]] = {
        "redact": [redact("qty"), *_passthrough("s", "q2")],
        "truncate": [truncate("s"), *_passthrough("qty", "q2")],
        "derived": [
            {"name": "qty", "strategy": "passthrough"},
            {"name": "q2", "strategy": "derived", "provider_config": {"expression": "qty * 2"}},
        ],
        "transform": [redact("qty"), *_passthrough("s", "q2")],
    }
    cfg = make_config(cols[work])
    if work == "transform":
        cfg["tables"][0]["transforms"] = [{"op": "limit", "n": 1}]
    return cfg


def _chunk(names: list[str]) -> pa.Table:
    arrays = []
    for name in names:
        if name == "s":
            arrays.append(pa.array(["alpha", "beta"]))
        else:
            arrays.append(pa.array(["1", "2"]))
    return pa.Table.from_arrays(arrays, names=names)


_UNIQUE = ["s", "qty", "q2"]


def _dup_names(work: str) -> list[str]:
    return ["s", "qty", "s", "q2"] if work == "truncate" else ["s", "qty", "qty", "q2"]


def _chunks(position: str, work: str, *, dup: bool) -> list[pa.Table]:
    if not dup:
        return [_chunk(_UNIQUE), _chunk(_UNIQUE)]
    dup_chunk = _chunk(_dup_names(work))
    return [dup_chunk, dup_chunk] if position == "first" else [_chunk(_UNIQUE), dup_chunk]


def _entry(entry: str) -> Any:
    from decoy_engine import run_mask_chunked, run_mask_pipeline_chunked

    return run_mask_pipeline_chunked if entry == "E4" else run_mask_chunked


def _drive(entry: str, cfg: dict[str, Any], chunks: Any, sink: list[pa.Table]) -> None:
    from tests.native._chunked_entry_support import key_provider

    fn = _entry(entry)
    out = fn(
        cfg,
        chunks,
        table="t",
        engine_version=ENGINE_VERSION,
        key_provider=key_provider(),
        **chunked_entry_kwargs(fn, cfg, "t"),
    )
    for chunk in out:
        sink.append(chunk)


@dataclass(frozen=True)
class ChunkCell:
    entry: str
    work: str
    position: str
    row: str
    label: str
    expect: str = "D"

    @property
    def id(self) -> str:
        return f"{self.entry}-{self.work}-{self.position}-row{self.row}-{self.label}"


CHUNK_CELLS: list[ChunkCell] = [
    *[
        ChunkCell(e, w, "first", "15", "FAIL-BEFORE")
        for e in ("E4", "E5")
        for w in ("redact", "truncate")
    ],
    ChunkCell("E4", "redact", "later", "16", "FAIL-BEFORE"),
    ChunkCell("E4", "truncate", "later", "16-11", "FAIL-BEFORE"),
    *[ChunkCell("E5", w, "later", "16a", "FAIL-BEFORE") for w in ("redact", "truncate")],
    *[
        ChunkCell(e, w, p, "14", "CONFIG-FIRST", expect=f"config-first:{code}")
        for e in ("E4", "E5")
        for w, code in (
            ("derived", "strategy_not_chunk_safe"),
            ("transform", "per_table_transforms_present"),
        )
        for p in ("first", "later")
    ],
]


def _assert_chunk_d(exc: BaseException, s: Spies) -> None:
    assert type(exc) is TransformError, repr(exc)
    assert exc.code == CODE
    assert (reason_from_exception(exc).namespace, reason_from_exception(exc).code) == (
        "runtime",
        CODE,
    )
    assert str(exc) == "table 't' has 1 duplicated field name(s)"
    assert len(s.raises) >= 1 and s.raises[0] is exc


@pytest.mark.parametrize("cell", CHUNK_CELLS, ids=[c.id for c in CHUNK_CELLS])
def test_e4_e5_chunked(cell: ChunkCell, spies: Spies) -> None:
    cfg = _chunked_config(cell.work)
    control: list[pa.Table] = []
    if cell.expect.startswith("config-first:"):
        with pytest.raises(Exception) as ctrl:
            _drive(cell.entry, cfg, iter(_chunks(cell.position, cell.work, dup=False)), control)
        assert _refusal_code(ctrl.value) == cell.expect.split(":", 1)[1]
    else:
        _drive(cell.entry, cfg, iter(_chunks(cell.position, cell.work, dup=False)), control)
        assert len(control) == 2 and all(c.column_names == _UNIQUE for c in control)
        if cell.work == "redact":
            assert control[0].column("qty").to_pylist() != ["1", "2"]
        else:
            assert control[0].column("s").to_pylist() == ["alp", "bet"]
    spies.raises.clear()
    spies.calls = 0
    spies.handler_tables.clear()
    sink: list[pa.Table] = []
    with pytest.raises(Exception) as exc:
        _drive(cell.entry, cfg, iter(_chunks(cell.position, cell.work, dup=True)), sink)
    if cell.expect.startswith("config-first:"):
        assert _refusal_code(exc.value) == cell.expect.split(":", 1)[1]
        assert spies.calls == 0
        return
    _assert_chunk_d(exc.value, spies)
    assert not [t for t in spies.handler_tables if t.startswith("DUP:")]
    assert spies.dup_conversions == 0 and spies.dup_walks == 0
    # The first chunk's output was yielded only when the duplicate came later.
    assert len(sink) == (1 if cell.position == "later" else 0)


@pytest.mark.parametrize("entry", ["E4", "E5"])
@pytest.mark.parametrize("position", ["first", "later"])
def test_e4_e5_streaming_csv_reader_chunks(
    entry: str, position: str, tmp_path: Path, spies: Spies
) -> None:
    # FAIL-BEFORE: chunks from the engine's pyarrow.csv streaming reader (R13) keep the duplicate.
    from decoy_engine.io import ChunkBudget, iter_csv_chunks

    header = "s,qty,qty,q2" if position == "first" else "s,qty,q2"
    path = tmp_path / "dup.csv"
    rows = ["alpha,1,2,3", "beta,4,5,6"] if position == "first" else ["alpha,1,3", "beta,4,6"]
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    budget = ChunkBudget(chunk_budget_bytes=1 << 20, max_chunk_rows=1, max_row_bytes=1024)
    chunks: Any = iter_csv_chunks(str(path), budget, table="t")
    if position == "later":
        first = next(chunks)
        dup = pa.Table.from_arrays(
            [first.column(0), first.column(1), first.column(1), first.column(2)],
            names=["s", "qty", "qty", "q2"],
        )
        chunks = iter([first, dup])
    with pytest.raises(Exception) as exc:
        _drive(entry, _chunked_config("redact"), chunks, [])
    _assert_chunk_d(exc.value, spies)


# ---------------------------------------------------------------- T6d4, T6d5, T7c


def test_t6d4_transform_schema_check_keeps_type_and_code_with_a_value_free_message() -> None:
    # Type and code: CHARACTERIZATION; the message shape: FAIL-BEFORE.
    from decoy_engine.execution._transforms import check_transform_source_schema

    schema = pa.schema([("a", pa.int64()), ("a", pa.int64()), ("b", pa.string())])
    with pytest.raises(TransformError) as exc:
        check_transform_source_schema({"tables": []}, "t", schema)
    assert exc.value.code == CODE
    assert str(exc.value) == "table 't' has 1 duplicated field name(s)"


@pytest.mark.parametrize(
    ("names", "count"),
    [(["a", "a", "b"], 1), (["a", "a", "b", "b", "a"], 2), (["x y", "x y"], 1)],
)
def test_t6d5_shared_refusal_names_the_table_and_a_count_only(names: list[str], count: int) -> None:
    # NEW-SEAM.
    with pytest.raises(TransformError) as exc:
        getattr(_guards, _SHARED)("orders", names)
    expected = f"table 'orders' has {count} duplicated field name(s)"
    assert str(exc.value) == expected and exc.value.args == (expected,)
    assert exc.value.code == CODE
    rest = str(exc.value).replace("'orders'", "")
    for name in set(names):
        assert not re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", rest), name
    getattr(_guards, _SHARED)("orders", ["a", "b"])  # unique: no raise


def test_t7c_reused_identity_keeps_its_pinned_owner() -> None:
    # CHARACTERIZATION: the identity's pinned owner.
    from decoy_engine import reasons

    spec = reasons.find("runtime", CODE)
    assert spec is not None and spec.owner == "TransformError"


def test_t7c_shared_refusal_raises_the_pinned_owner_only() -> None:
    # NEW-SEAM: the shared function is a TransformError producer, never an ExecutionError.
    with pytest.raises(Exception) as exc:
        getattr(_guards, _SHARED)("t", ["a", "a"])
    assert type(exc.value) is TransformError
    from decoy_engine.execution._errors import ExecutionError

    assert not isinstance(exc.value, ExecutionError)
