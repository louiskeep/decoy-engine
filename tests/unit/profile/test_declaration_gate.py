"""CONFIG-EXPLICIT E2c: the declaration gate on profile_source, run_pipeline and
run_pipeline_isolated (AT-E2c-1, -2, -3, -5, and the missing-`has_header` guard).

Every refusal must come before a data row is read, staged, profiled or masked. The spies
below count the row readers, the adapter, the loader and the staging writer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_pipeline
from decoy_engine.errors import ConfigError, PipelineValidationError
from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.execution import _isolated_run as isolated_mod
from decoy_engine.execution._isolated_run import run_pipeline_isolated
from decoy_engine.profile import (
    DeclarationRequest,
    PreparedDeclaredSources,
    prepare_declared_sources,
    profile_source,
)
from decoy_engine.profile import _source as source_mod
from decoy_engine.profile._readers_csv import CsvFileSource

COLUMN = "source_file.undeclared_column"
TABLE = "source_file.undeclared_table"
MARK = "zz_marker_value"


def _col(name: str) -> dict[str, str]:
    return {"name": name, "strategy": "passthrough"}


def _csv(path: Path, **extra: Any) -> dict[str, Any]:
    return {"type": "file", "format": "csv", "path": str(path), "has_header": True, **extra}


def _config(
    sources: dict[str, Any], tables: dict[str, list[str]], tmp: Path | None = None
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": sources,
        "tables": [{"name": t, "columns": [_col(c) for c in cols]} for t, cols in tables.items()],
        "relationships": [],
    }
    if tmp is not None:
        config["targets"] = {
            t: {"type": "file", "format": "csv", "path": str(tmp / f"out_{t}.csv")} for t in tables
        }
    return config


def _write(tmp: Path, name: str, columns: list[str]) -> Path:
    path = tmp / f"{name}.csv"
    pd.DataFrame({c: [MARK] for c in columns}).to_csv(path, index=False)
    return path


def _table(*columns: str) -> pa.Table:
    return pa.table({c: [MARK] for c in columns})


class RowSpies:
    """Counts every reader that exposes a data row, and the adapter's `run`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        for name in ("_read", "to_frame", "sample_frame"):
            self._wrap(monkeypatch, CsvFileSource, name, f"CsvFileSource.{name}")
        self._wrap(monkeypatch, source_mod, "_profile_one_source", "_profile_one_source")
        self._wrap(monkeypatch, PandasExecutionAdapter, "run", "adapter.run")

    def _wrap(self, mp: pytest.MonkeyPatch, owner: Any, name: str, label: str) -> None:
        real = getattr(owner, name)

        def spy(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(label)
            return real(*args, **kwargs)

        mp.setattr(owner, name, spy)


def _assert_value_free(err: PipelineValidationError, code: str) -> None:
    assert err.code == code
    for text in (str(err), repr(err)):
        assert MARK not in text
        assert "zz_" not in text


# AT-E2c-1 -----------------------------------------------------------------------------------


def test_profile_source_refuses_undeclared_column_before_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, "t", ["id", "zz_secret"])
    config = _config({"t": _csv(path)}, {"t": ["id"]})
    spies = RowSpies(monkeypatch)
    with pytest.raises(PipelineValidationError) as caught:
        profile_source(config, seed=1)
    _assert_value_free(caught.value, COLUMN)
    assert "column 2 of 2" in str(caught.value)
    assert spies.calls == []


def test_profile_source_refuses_table_without_entry_before_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, "t", ["id"])
    other = _write(tmp_path, "other", ["id"])
    config = _config({"t": _csv(path), "other": _csv(other)}, {"t": ["id"]})
    spies = RowSpies(monkeypatch)
    with pytest.raises(PipelineValidationError) as caught:
        profile_source(config, seed=1)
    assert caught.value.code == TABLE
    assert spies.calls == []


# AT-E2c-2 -----------------------------------------------------------------------------------


class Loader:
    def __init__(self, table: pa.Table) -> None:
        self.table = table
        self.calls = 0

    def __call__(self, name: str) -> pa.Table:
        self.calls += 1
        return self.table


def _run_case(case: str, tmp: Path) -> tuple[dict[str, Any], dict[str, Any], Loader | None, str]:
    """(config, run_pipeline kwargs, loader, expected code) for one refusal shape."""
    narrow = _write(tmp, "narrow", ["id"])
    wide = _write(tmp, "wide", ["id", "zz_secret"])
    if case == "descriptor_undeclared":
        return _config({"t": _csv(wide)}, {"t": ["id"]}, tmp), {}, None, COLUMN
    if case == "supplied_undeclared":
        config = _config({"t": _csv(narrow)}, {"t": ["id"]}, tmp)
        return config, {"sources": {"t": _table("id", "zz_secret")}}, None, COLUMN
    if case == "descriptor_undeclared_supplied_declared":
        config = _config({"t": _csv(wide)}, {"t": ["id"]}, tmp)
        return config, {"sources": {"t": _table("id")}}, None, COLUMN
    if case == "supplied_without_entry":
        config = _config({"t": _csv(narrow)}, {"t": ["id"]}, tmp)
        return config, {"sources": {"t": _table("id"), "extra": _table("id")}}, None, TABLE
    if case == "loader_schema_undeclared":
        config = _config({"t": _csv(narrow)}, {"t": ["id"]}, tmp)
        loader = Loader(_table("id"))
        schemas = {"t": _table("id", "zz").schema}
        return config, {"source_loader": loader, "source_loader_schemas": schemas}, loader, COLUMN
    raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "descriptor_undeclared",
        "supplied_undeclared",
        "descriptor_undeclared_supplied_declared",
        "supplied_without_entry",
        "loader_schema_undeclared",
    ],
)
def test_run_pipeline_refuses_before_rows(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, kwargs, loader, code = _run_case(case, tmp_path)
    spies = RowSpies(monkeypatch)
    with pytest.raises(PipelineValidationError) as caught:
        run_pipeline(config, engine_version="e2c-test", **kwargs)
    _assert_value_free(caught.value, code)
    assert spies.calls == []
    assert loader is None or loader.calls == 0
    assert not list(tmp_path.glob("out_*.csv"))


def test_loader_drift_is_refused_before_profile_or_mask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow = _write(tmp_path, "narrow", ["id"])
    config = _config({"t": _csv(narrow)}, {"t": ["id"]}, tmp_path)
    loader = Loader(_table("id", "zz_drift"))
    spies = RowSpies(monkeypatch)
    with pytest.raises(PipelineValidationError) as caught:
        run_pipeline(
            config,
            engine_version="e2c-test",
            execution_mode="full_frame",
            source_loader=loader,
            source_loader_schemas={"t": _table("id").schema},
        )
    _assert_value_free(caught.value, COLUMN)
    assert loader.calls == 1
    assert "adapter.run" not in spies.calls


def test_a_loader_needs_its_schemas_and_the_reverse(tmp_path: Path) -> None:
    narrow = _write(tmp_path, "narrow", ["id"])
    config = _config({"t": _csv(narrow)}, {"t": ["id"]}, tmp_path)
    loader = Loader(_table("id"))
    with pytest.raises(ValueError, match="source_loader_schemas"):
        run_pipeline(config, engine_version="e2c-test", source_loader=loader)
    assert loader.calls == 0
    with pytest.raises(ValueError, match="source_loader"):
        run_pipeline(
            config,
            engine_version="e2c-test",
            source_loader_schemas={"t": _table("id").schema},
        )


@pytest.mark.parametrize(
    ("schemas", "refusal"),
    [
        ({}, ValueError),
        ({"other": pa.schema([("id", pa.string())])}, PipelineValidationError),
    ],
)
def test_a_loader_table_without_a_schema_is_refused_before_the_loader_runs(
    schemas: dict[str, pa.Schema], refusal: type[Exception], tmp_path: Path
) -> None:
    narrow = _write(tmp_path, "narrow", ["id"])
    config = _config({"t": _csv(narrow)}, {"t": ["id"]}, tmp_path)
    loader = Loader(_table("id", "zz_secret"))
    with pytest.raises(refusal):
        run_pipeline(
            config,
            engine_version="e2c-test",
            execution_mode="full_frame",
            source_loader=loader,
            source_loader_schemas=schemas,
        )
    assert loader.calls == 0


# AT-E2c-3 -----------------------------------------------------------------------------------


class Staging:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        for name in ("_run_in_process", "_run_isolated", "_write_payload"):
            real = getattr(isolated_mod, name)

            def spy(*args: Any, _real: Any = real, _name: str = name, **kwargs: Any) -> Any:
                self.calls.append(_name)
                return _real(*args, **kwargs)

            monkeypatch.setattr(isolated_mod, name, spy)
        write = isolated_mod.pq.write_table

        def spy_write(*args: Any, **kwargs: Any) -> Any:
            self.calls.append("pq.write_table")
            return write(*args, **kwargs)

        monkeypatch.setattr(isolated_mod.pq, "write_table", spy_write)


@pytest.mark.parametrize("isolate", [True, False])
@pytest.mark.parametrize("case", ["supplied_undeclared", "descriptor_undeclared"])
def test_run_pipeline_isolated_refuses_before_staging(
    case: str, isolate: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow = _write(tmp_path, "narrow", ["id"])
    wide = _write(tmp_path, "wide", ["id", "zz_secret"])
    if case == "supplied_undeclared":
        config = _config({"t": _csv(narrow)}, {"t": ["id"]}, tmp_path)
        sources = {"t": _table("id", "zz_secret")}
    else:
        config = _config({"t": _csv(wide)}, {"t": ["id"]}, tmp_path)
        sources = {"t": _table("id")}
    spies = RowSpies(monkeypatch)
    staging = Staging(monkeypatch)
    with pytest.raises(PipelineValidationError) as caught:
        run_pipeline_isolated(config, sources, isolate=isolate, engine_version="e2c-test")
    _assert_value_free(caught.value, COLUMN)
    assert staging.calls == []
    assert spies.calls == []


# AT-E2c-5 -----------------------------------------------------------------------------------


def test_profile_source_reuses_the_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, "t", ["id"])
    config = _config({"t": _csv(path)}, {"t": ["id"]}, tmp_path)
    built: list[int] = []
    real_init = PreparedDeclaredSources.__init__

    def counting(self: PreparedDeclaredSources, *args: Any, **kwargs: Any) -> None:
        built.append(1)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(PreparedDeclaredSources, "__init__", counting)
    run_pipeline(config, engine_version="e2c-test")
    assert built == [1]

    other = _config({"t": _csv(path)}, {"t": ["id"]}, tmp_path)
    prepared = prepare_declared_sources(DeclarationRequest.for_descriptors(other))
    with pytest.raises(ValueError, match="declared"):
        profile_source(config, seed=1, declared=prepared)
    assert profile_source(other, seed=1, declared=prepared).tables


# Regression guard (green on the E2a base) ---------------------------------------------------


@pytest.mark.parametrize("route", ["profile_source", "run_pipeline"])
def test_missing_has_header_refuses_before_rows(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, "t", ["id"])
    descriptor = _csv(path)
    del descriptor["has_header"]
    config = _config({"t": descriptor}, {"t": ["id"]}, tmp_path)
    spies = RowSpies(monkeypatch)
    with pytest.raises(ConfigError):
        if route == "profile_source":
            profile_source(config, seed=1)
        else:
            run_pipeline(config, engine_version="e2c-test")
    assert spies.calls == []
