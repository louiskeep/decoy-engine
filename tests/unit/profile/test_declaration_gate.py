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
from decoy_engine.execution import _pipeline_auto_chunk as auto_chunk
from decoy_engine.execution._chunked import run_mask_pipeline_chunked
from decoy_engine.execution._chunked_input import fixed_schema_chunks_from_resident
from decoy_engine.execution._isolated_run import run_pipeline_isolated
from decoy_engine.execution.native import _chunked_entry
from decoy_engine.execution.native._chunked_entry import run_mask_chunked
from decoy_engine.execution.native._dispatch import run_native_or_oracle_chunked
from decoy_engine.execution.physical.drivers._chunked import MaskPipelineChunkedAdapter
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
    assert err.__cause__ is None
    assert err.__context__ is None


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


# AT-E2d-1, -2, -4 and the capture guard: the three chunked entries -------------------------

ENTRIES = ("pipeline", "mask", "native_or_oracle")


class SpyChunks:
    """An iterable of chunks that counts how far the entry pulled it."""

    def __init__(self, *tables: pa.Table) -> None:
        self.tables = tables
        self.iters = 0
        self.nexts = 0
        self._it = iter(tables)

    def __iter__(self) -> SpyChunks:
        self.iters += 1
        return self

    def __next__(self) -> pa.Table:
        self.nexts += 1
        return next(self._it)


def _chunk_config(*columns: str, table: str = "t") -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": {table: {"type": "file", "format": "csv", "path": "unused.csv"}},
        "tables": [{"name": table, "columns": [_col(c) for c in columns]}],
        "relationships": [],
    }


def _run_entry(
    entry: str, config: dict[str, Any], chunks: Any, schema: pa.Schema | None = None, table="t"
) -> Any:
    if entry == "pipeline":
        return run_mask_pipeline_chunked(
            config, chunks, table=table, engine_version="e2d", source_schema=schema
        )
    if entry == "mask":
        return run_mask_chunked(config, chunks, table=table, engine_version="e2d")
    return run_native_or_oracle_chunked(config, chunks, table=table, engine_version="e2d")


def _schema(*columns: str) -> pa.Schema:
    return pa.schema([pa.field(c, pa.string()) for c in columns])


class DeepSpies:
    """What must stay at zero when a chunked entry refuses: profile, compile and the adapter."""

    def __init__(self, mp: pytest.MonkeyPatch) -> None:
        import decoy_engine.plan as plan_mod
        from decoy_engine.execution import _chunked_profile

        self.calls: list[str] = []
        self._wrap(mp, _chunked_profile, "first_chunk_profile")
        self._wrap(mp, plan_mod, "compile_plan")
        self._wrap(mp, PandasExecutionAdapter, "run")

    def _wrap(self, mp: pytest.MonkeyPatch, owner: Any, name: str) -> None:
        real = getattr(owner, name)

        def spy(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return real(*args, **kwargs)

        mp.setattr(owner, name, spy)


def test_chunked_requires_source_schema() -> None:
    """AT-E2d-1: the pipeline entry has no default for `source_schema`."""
    chunks = SpyChunks(_table("id"))
    with pytest.raises(TypeError):
        run_mask_pipeline_chunked(  # type: ignore[call-arg]
            _chunk_config("id"), chunks, table="t", engine_version="e2d"
        )
    assert chunks.iters == 0
    assert chunks.nexts == 0


@pytest.mark.parametrize("entry", ENTRIES)
def test_chunked_refuses_undeclared_table_before_rows(
    entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _chunk_config("id")
    chunks = SpyChunks(_table("id"))
    spies = DeepSpies(monkeypatch)
    with pytest.raises(PipelineValidationError) as caught:
        _run_entry(entry, config, chunks, _schema("id"), table="ghost")
    assert caught.value.code == TABLE
    assert (chunks.iters, chunks.nexts, spies.calls) == (0, 0, [])


def test_pipeline_entry_refuses_undeclared_source_schema_before_any_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = SpyChunks(_table("id"))
    spies = DeepSpies(monkeypatch)
    with pytest.raises(PipelineValidationError) as caught:
        _run_entry("pipeline", _chunk_config("id"), chunks, _schema("id", "zz_secret"))
    _assert_value_free(caught.value, COLUMN)
    assert (chunks.iters, chunks.nexts, spies.calls) == (0, 0, [])


@pytest.mark.parametrize("entry", ENTRIES)
def test_chunked_refuses_undeclared_first_chunk_column_before_profile(
    entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunks = SpyChunks(_table("id", "zz_secret"), _table("id", "zz_secret"))
    spies = DeepSpies(monkeypatch)
    with pytest.raises(PipelineValidationError) as caught:
        _run_entry(entry, _chunk_config("id"), chunks, _schema("id"))
    _assert_value_free(caught.value, COLUMN)
    assert chunks.nexts == 1
    assert spies.calls == []


@pytest.mark.parametrize("entry", ENTRIES)
def test_chunked_refuses_a_column_that_first_appears_in_chunk_three(
    entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal comes before chunk three is masked: two chunks are masked, three are pulled.

    Masking is counted on both routes (the oracle's adapter call and the native chunk step), so
    a check placed after masking and before the yield would show three masked chunks.
    """
    from decoy_engine.execution.native import _chunked_entry

    masked: list[str] = []

    def count(owner: Any, name: str, label: str) -> None:
        real = getattr(owner, name)

        def spy(*args: Any, **kwargs: Any) -> Any:
            masked.append(label)
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, name, spy)

    count(PandasExecutionAdapter, "run", "oracle")
    count(_chunked_entry, "_mask_chunk_native", "native")
    chunks = SpyChunks(_table("id"), _table("id"), _table("id", "zz_late"))
    out = _run_entry(entry, _chunk_config("id"), chunks, _schema("id"))
    got: list[pa.Table] = []
    with pytest.raises(PipelineValidationError) as caught:
        for chunk in out:
            got.append(chunk)
    _assert_value_free(caught.value, COLUMN)
    assert len(got) == 2
    assert len(masked) == 2, masked
    assert chunks.nexts == 3


def test_pipeline_entry_refuses_a_chunk_column_outside_source_schema() -> None:
    """The column is declared in config but the caller's `source_schema` does not list it."""
    config = _chunk_config("id", "extra")
    chunks = SpyChunks(_table("id", "extra"))
    out = run_mask_pipeline_chunked(
        config, chunks, table="t", engine_version="e2d", source_schema=_schema("id", "extra")
    )
    assert len(list(out)) == 1
    chunks = SpyChunks(_table("id", "extra"))
    with pytest.raises(PipelineValidationError) as caught:
        run_mask_pipeline_chunked(
            config, chunks, table="t", engine_version="e2d", source_schema=_schema("id")
        )
    assert caught.value.code == MISSING
    assert "column extra" in str(caught.value)
    assert chunks.nexts == 0


def test_a_declared_chunked_run_still_masks() -> None:
    config = _chunk_config("id", "name")
    chunk = pa.table({"id": ["1", "2"], "name": ["a", "b"]})
    for entry in ENTRIES:
        out = list(_run_entry(entry, config, [chunk, chunk], _schema("id", "name")))
        assert [c.num_rows for c in out] == [2, 2]


def test_auto_chunk_legacy_passes_source_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-E2d-4: the resident legacy lane hands its table's schema to the gate."""
    from decoy_engine.providers_v2 import get_default_registry

    source = pa.table({"id": ["1"], "zz_secret": ["x"]})
    spies = DeepSpies(monkeypatch)
    with pytest.raises(PipelineValidationError) as caught:
        auto_chunk._run_legacy(
            _chunk_config("id"),
            source,
            table="t",
            engine_version="e2d",
            registry=get_default_registry(),
            adapter=None,
            vault_writer=None,
            chunk_size_rows=10,
            key_provider=None,
            chunk_results=[],
        )
    _assert_value_free(caught.value, COLUMN)
    assert spies.calls == []


def test_chunked_adapter_forwards_source_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return iter(())

    monkeypatch.setattr("decoy_engine.execution._chunked.run_mask_pipeline_chunked", spy)
    schema = _schema("id")
    MaskPipelineChunkedAdapter().run(
        _chunk_config("id"), [], table="t", engine_version="e2d", source_schema=schema
    )
    assert seen["source_schema"] is schema


def test_fixed_schema_capture_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `_FixedSchemaChunks` input still reaches admission with its captured schema, and a
    stored pandas index field is not an undeclared column."""
    frame = pd.DataFrame({"id": ["1", "2"]}, index=pd.Index([10, 20], name="ix"))
    resident = pa.Table.from_pandas(frame)
    assert resident.schema.names != ["id"]
    seen: dict[str, Any] = {}
    real = _chunked_entry._route_chunked

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen["captured"] = kwargs["captured_schema"]
        return real(*args, **kwargs)

    monkeypatch.setattr(_chunked_entry, "_route_chunked", spy)
    producer = fixed_schema_chunks_from_resident(resident, 1)
    out = list(run_mask_chunked(_chunk_config("id"), producer, table="t", engine_version="e2d"))
    assert sum(c.num_rows for c in out) == 2
    assert seen["captured"].equals(resident.schema, check_metadata=True)


# AT-E2b-2: a declared column the source lacks, on every public route ------------------------

MISSING = "source_file.declared_column_missing"


@pytest.mark.parametrize("route", ["run_pipeline", "isolated", "isolated_inproc"])
def test_descriptor_routes_refuse_missing_declared_before_rows(
    route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, "t", ["id"])
    config = _config({"t": _csv(path)}, {"t": ["id", "account_number"]}, tmp_path)
    spies = RowSpies(monkeypatch)
    staging = Staging(monkeypatch)
    with pytest.raises(PipelineValidationError) as caught:
        if route == "run_pipeline":
            run_pipeline(config, engine_version="e2b-test")
        else:
            # Descriptor-only: nothing is supplied, so preparation must read the header itself.
            run_pipeline_isolated(
                config, {}, isolate=route == "isolated", engine_version="e2b-test"
            )
    _assert_value_free(caught.value, MISSING)
    assert "account_number" in str(caught.value)
    assert spies.calls == []
    assert staging.calls == []
    assert not list(tmp_path.glob("out_*.csv"))


def test_direct_profile_source_applies_only_the_forward_rule(tmp_path: Path) -> None:
    """The reverse rule belongs to the execution choke point, which knows which columns the
    config creates. A direct profile of a config with a created column must not refuse it."""
    path = _write(tmp_path, "t", ["g"])
    config = _config({"t": _csv(path)}, {"t": ["g"]}, tmp_path)
    config["tables"][0]["columns"].append(
        {
            "name": "hh",
            "strategy": "group_key",
            "provider_config": {"group_by": "g", "length": 16},
        }
    )
    profile_source(config, seed=1)
    config["tables"][0]["columns"][0]["name"] = "absent"  # the forward rule still applies
    with pytest.raises(PipelineValidationError) as caught:
        profile_source(config, seed=1)
    assert caught.value.code == COLUMN


@pytest.mark.parametrize("case", ["supplied", "loader_schema", "loader_table"])
def test_run_pipeline_refuses_missing_declared_before_rows(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, "t", ["id", "account_number"])
    config = _config({"t": _csv(path)}, {"t": ["id", "account_number"]}, tmp_path)
    loader = Loader(_table("id"))
    kwargs: dict[str, Any]
    if case == "supplied":
        kwargs = {"sources": {"t": _table("id")}}
    elif case == "loader_schema":
        kwargs = {"source_loader": loader, "source_loader_schemas": {"t": _table("id").schema}}
    else:
        both = _table("id", "account_number").schema
        kwargs = {"source_loader": loader, "source_loader_schemas": {"t": both}}
    spies = RowSpies(monkeypatch)
    with pytest.raises(PipelineValidationError) as caught:
        run_pipeline(config, engine_version="e2b-test", execution_mode="full_frame", **kwargs)
    _assert_value_free(caught.value, MISSING)
    assert "adapter.run" not in spies.calls
    if case == "loader_schema":
        assert loader.calls == 0
    assert not list(tmp_path.glob("out_*.csv"))


@pytest.mark.parametrize("entry", ENTRIES)
def test_chunked_refuses_missing_declared_before_profile(
    entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunks = SpyChunks(_table("id"), _table("id"))
    spies = DeepSpies(monkeypatch)
    schema = _schema("id", "account_number")
    with pytest.raises(PipelineValidationError) as caught:
        _run_entry(entry, _chunk_config("id", "account_number"), chunks, schema)
    _assert_value_free(caught.value, MISSING)
    assert spies.calls == []


@pytest.mark.parametrize("entry", ENTRIES)
def test_chunked_refuses_a_later_chunk_missing_a_declared_column(
    entry: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunks = SpyChunks(_table("id", "x"), _table("id"))
    out = _run_entry(entry, _chunk_config("id", "x"), chunks, _schema("id", "x"))
    with pytest.raises(PipelineValidationError) as caught:
        list(out)
    assert caught.value.code == MISSING
