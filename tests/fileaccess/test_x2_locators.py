"""AT-X2: a file locator never crosses in an error or log line under a strict policy, including
the descriptor path (`/proc/self/fd/<n>`) the library is handed. Authorized cell content may
appear (D5); only locators may not.

Each site runs first under the ambient policy as a positive control, proving the unsanitized
diagnostic does name the locator, then under a rooted policy whose files sit beneath a
directory carrying a sentinel."""

from __future__ import annotations

import errno
import json
import logging
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import fileaccess
from decoy_engine.data_discovery import DiscoverySqlError, run_discovery_sql
from decoy_engine.fileaccess.adapters import duckdb as duckdb_adapter
from tests.fileaccess.x_support import (
    LOCATOR_SENTINEL,
    XWorld,
    assert_no_locator,
    rendered,
)


def _snapshot_config(path: Path) -> dict[str, Any]:
    return {
        "tables": [
            {
                "name": "t",
                "generate_columns": [
                    {"name": "c", "type": "statistical", "snapshot_file": str(path)}
                ],
            }
        ]
    }


# -- snapshots ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [b"{not json", b'{"schema_version": "zzz"}', b"[1, 2]"],
    ids=["malformed", "schema", "not_object"],
)
def test_snapshot_parse_diagnostics_name_no_locator(
    xworld: XWorld,
    content: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from decoy_engine.plan._generation import read_and_pin_snapshots

    path = xworld.sub / "snap.json"
    path.write_bytes(content)
    ambient_pinned, ambient_failures = read_and_pin_snapshots(_snapshot_config(path))
    assert LOCATOR_SENTINEL in str(next(iter(ambient_failures.values())))  # positive control
    with fileaccess.scope(xworld.policy):
        pinned, failures = read_and_pin_snapshots(_snapshot_config(path))
    (failure,) = failures.values()
    assert_no_locator(xworld, rendered(failure, caplog))


def test_snapshot_size_error_names_no_locator(
    xworld: XWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.plan import _generation
    from decoy_engine.plan._errors import PlanCompileError

    path = xworld.sub / "big.json"
    path.write_bytes(b'{"schema_version": "x", "pad": "' + b"a" * 64 + b'"}')
    monkeypatch.setattr(_generation, "EMBEDDED_SNAPSHOT_MAX_BYTES", 8)
    with pytest.raises(PlanCompileError) as control:
        _generation.read_and_pin_snapshots(_snapshot_config(path))
    assert LOCATOR_SENTINEL in str(control.value) + control.value.path  # positive control
    with fileaccess.scope(xworld.policy), pytest.raises(PlanCompileError) as info:
        _generation.read_and_pin_snapshots(_snapshot_config(path))
    assert_no_locator(xworld, [*rendered(info.value), info.value.path])


@pytest.mark.parametrize(
    "content", [b"{not json", b'{"schema_version": "zzz"}', b"[1]"], ids=["bad", "schema", "list"]
)
def test_load_snapshot_diagnostics_name_no_locator(xworld: XWorld, content: bytes) -> None:
    from decoy_engine.generation.statistical._spec import StatisticalSpecError, _load_snapshot

    path = xworld.sub / "s.json"
    path.write_bytes(content)
    with pytest.raises(StatisticalSpecError) as control:
        _load_snapshot(str(path))
    assert LOCATOR_SENTINEL in str(control.value)
    with fileaccess.scope(xworld.policy), pytest.raises(StatisticalSpecError) as info:
        _load_snapshot(str(path))
    assert_no_locator(xworld, rendered(info.value))


def _read_failure(world: XWorld, path: Path):  # type: ignore[no-untyped-def]
    """A read failure whose text names the file, as a library's can."""

    def boom(*a: Any, **k: Any) -> bytes:
        raise OSError(errno.EIO, f"read failed on {path}")

    return boom


def test_a_read_failure_text_that_names_the_file_is_redacted(
    xworld: XWorld,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.generation.statistical._spec import StatisticalSpecError, _load_snapshot
    from decoy_engine.generators.derivation import _snapshot_content_digest
    from decoy_engine.plan._generation import read_and_pin_snapshots

    path = xworld.sub / "s.json"
    path.write_text("{}")
    monkeypatch.setattr(fileaccess, "read_all", _read_failure(xworld, path))
    # ambient controls: the raw diagnostic names the file
    with pytest.raises(StatisticalSpecError) as c1:
        _load_snapshot(str(path))
    assert LOCATOR_SENTINEL in str(c1.value)
    with pytest.raises(RuntimeError) as c2:
        _snapshot_content_digest(str(path))
    assert LOCATOR_SENTINEL in str(c2.value)
    with fileaccess.scope(xworld.policy):
        with pytest.raises(StatisticalSpecError) as e1:
            _load_snapshot(str(path))
        with pytest.raises(RuntimeError) as e2:
            _snapshot_content_digest(str(path))
        _, failures = read_and_pin_snapshots(_snapshot_config(path))
    for exc in (e1.value, e2.value, *failures.values()):
        assert_no_locator(xworld, rendered(exc))
        assert exc.__cause__ is None and exc.__context__ is None


# -- discovery SQL ---------------------------------------------------------------------------


class _FakeRelation:
    def __init__(self, fetch_error: Exception | None, rows: int = 1) -> None:
        self.description = [("a",)]
        self._fetch_error = fetch_error

    def fetchmany(self, n: int) -> list[tuple[int]]:
        if self._fetch_error is not None:
            raise self._fetch_error
        return [(1,)]


class _FakeConnection:
    def __init__(self, execute_error: Exception | None, fetch_error: Exception | None) -> None:
        self._execute_error = execute_error
        self._fetch_error = fetch_error

    def execute(self, sql: str) -> _FakeRelation:
        if self._execute_error is not None:
            raise self._execute_error
        return _FakeRelation(self._fetch_error)


def _patch_session(
    monkeypatch: pytest.MonkeyPatch,
    errors: tuple[Exception | None, Exception | None],
    texts: list[str],
) -> None:
    from contextlib import contextmanager

    @contextmanager
    def session(handles: Any):  # type: ignore[no-untyped-def]
        # The library's own text names the file it was handed: the descriptor path under a
        # strict policy, the original path otherwise.
        for handle in handles.values():
            texts.append(handle.library_path())
        yield _FakeConnection(*errors)

    monkeypatch.setattr(duckdb_adapter, "untrusted_session", session)


@pytest.mark.parametrize("stage", ["execute", "fetch"])
def test_discovery_errors_at_execute_and_fetch_name_no_locator(
    xworld: XWorld,
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = xworld.sub / "t.parquet"
    pq.write_table(pa.table({"a": [1]}), table)
    texts: list[str] = []

    def failure() -> duckdb.Error:
        shown = texts[-1] if texts else str(table)
        return duckdb.IOException(f"IO Error: cannot read {shown} or {table}")

    _patch_session(monkeypatch, (None, None), texts)
    # ambient control: the unsanitized text names the path
    ambient = duckdb.IOException(f"IO Error: cannot read {table}")
    _patch_session(monkeypatch, (ambient, None) if stage == "execute" else (None, ambient), texts)
    if stage == "execute":
        with pytest.raises(DiscoverySqlError) as control:
            run_discovery_sql("select 1", {"t": str(table)})
        assert LOCATOR_SENTINEL in str(control.value)
    else:
        with pytest.raises(duckdb.Error) as control_fetch:
            run_discovery_sql("select 1", {"t": str(table)})
        assert LOCATOR_SENTINEL in str(control_fetch.value)
    texts.clear()
    handle_text = f"IO Error: cannot read /proc/self/fd/7 or {table}"
    err = duckdb.IOException(handle_text)
    _patch_session(monkeypatch, (err, None) if stage == "execute" else (None, err), texts)
    with fileaccess.scope(xworld.policy), pytest.raises(DiscoverySqlError) as info:
        run_discovery_sql("select 1", {"t": str(table)})
    assert_no_locator(xworld, rendered(info.value))
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert "discovery.tables.t" in str(info.value)


# -- corpora ---------------------------------------------------------------------------------


def _corpus(path: Path, columns: dict[str, list[Any]], meta: dict[str, str] | None = None) -> None:
    table = pa.table(columns)
    if meta:
        table = table.replace_schema_metadata(meta)
    pq.write_table(table, path)


@pytest.mark.parametrize(
    "case",
    ["no_code_column", "empty", "duplicate", "partial_provenance"],
)
def test_customer_corpus_diagnostics_name_no_locator(
    xworld: XWorld,
    case: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from decoy_engine.plan._errors import PlanCompileError
    from decoy_engine.transforms._codeset_loader import load_corpus

    path = xworld.sub / "corpus.parquet"
    if case == "no_code_column":
        _corpus(path, {"x": [1]})
    elif case == "empty":
        _corpus(path, {"code": pa.array([], pa.string())})
    elif case == "duplicate":
        _corpus(path, {"code": ["a", "a"]})
    else:
        _corpus(path, {"code": ["a", "b"]}, {"decoy_corpus": "mine", "source": "s"})
    with pytest.raises(PlanCompileError) as control:
        load_corpus("mine", path)
    assert LOCATOR_SENTINEL in str(control.value)
    with fileaccess.scope(xworld.policy), pytest.raises(PlanCompileError) as info:
        load_corpus("mine", path)
    assert_no_locator(xworld, rendered(info.value, caplog))


def test_customer_corpus_version_pin_diagnostic_names_no_locator(xworld: XWorld) -> None:
    from decoy_engine.plan._errors import PlanCompileError
    from decoy_engine.transforms._codeset_loader import _get_corpus_record

    path = xworld.sub / "corpus.parquet"
    _corpus(path, {"code": ["a", "b"]})
    with pytest.raises(PlanCompileError) as control:
        _get_corpus_record("mine", path, is_shipped=False, expected_source_version="X")
    assert LOCATOR_SENTINEL in str(control.value)
    with fileaccess.scope(xworld.policy), pytest.raises(PlanCompileError) as info:
        _get_corpus_record("mine", path, is_shipped=False, expected_source_version="X")
    assert_no_locator(xworld, rendered(info.value))


def test_customer_corpus_without_provenance_logs_no_locator(
    xworld: XWorld,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from decoy_engine.transforms._codeset_loader import load_corpus

    path = xworld.sub / "corpus.parquet"
    other = xworld.sub / "corpus_two.parquet"  # a second file: the first load is cached
    _corpus(path, {"code": ["a", "b"]})
    _corpus(other, {"code": ["a", "b", "c"]})
    with caplog.at_level(logging.WARNING):
        load_corpus("mine", path)
        assert any(LOCATOR_SENTINEL in r.getMessage() for r in caplog.records)  # control
        caplog.clear()
        with fileaccess.scope(xworld.policy):
            load_corpus("mine", other)
    logged = [logging.Formatter().format(r) for r in caplog.records]
    assert logged, "the warning was expected"
    assert_no_locator(xworld, logged)


# -- model packs and name hints --------------------------------------------------------------


def test_model_pack_diagnostics_name_no_locator(
    xworld: XWorld,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from decoy_engine.storm.model_pack.loader import ModelPackLoader, ModelPackLoadError

    pack = xworld.sub / "pack"
    pack.mkdir()
    with pytest.raises(ModelPackLoadError) as control:
        ModelPackLoader(pack).load()
    assert LOCATOR_SENTINEL in str(control.value)
    (pack / "manifest.json").write_text("{not json")
    with fileaccess.scope(xworld.policy):
        loader = ModelPackLoader(pack, file_access=xworld.policy)
        with pytest.raises(fileaccess.FileAccessDenied) as missing:
            ModelPackLoader(xworld.sub / "nopack", file_access=xworld.policy).load()
        with pytest.raises(ModelPackLoadError) as corrupt:
            loader.load()
        with caplog.at_level(logging.DEBUG):
            assert loader.load_with_fallback() is None
    for exc in (corrupt.value,):
        assert_no_locator(xworld, rendered(exc, caplog))
    assert LOCATOR_SENTINEL not in "".join(r.getMessage() for r in caplog.records)
    assert missing.value is not None


def test_model_pack_refusals_log_no_locator(
    xworld: XWorld,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import hashlib

    from decoy_engine.storm.model_pack import loader as pack_loader

    pack = xworld.sub / "pack"
    pack.mkdir()
    weights = b"weights"
    manifest = {
        "format_version": pack_loader.PACK_FORMAT,
        "feature_schema_version": pack_loader.FEATURE_SCHEMA_VERSION,
        "sha256": hashlib.sha256(weights).hexdigest(),
    }
    (pack / "model.joblib").write_bytes(weights)
    try:
        (pack / "manifest.json").write_text(json.dumps(manifest))
        monkeypatch.delenv("DECOY_PACK_SIGNING_KEY", raising=False)
        with fileaccess.scope(xworld.policy), caplog.at_level(logging.DEBUG):
            result = pack_loader.ModelPackLoader(
                pack, file_access=xworld.policy
            ).load_with_fallback()
        assert result is None
        assert caplog.records, "a refusal is logged"
        assert_no_locator(xworld, [r.getMessage() for r in caplog.records])
    finally:
        pass


def test_name_hint_diagnostics_name_no_locator(xworld: XWorld) -> None:
    from decoy_engine.storm.name_hints.loader import NameHintLoaderError, load_name_hint_terms

    folder = xworld.sub / "hints"
    folder.mkdir()
    with pytest.raises(NameHintLoaderError) as control:
        load_name_hint_terms(folder)
    assert LOCATOR_SENTINEL in str(control.value)
    with fileaccess.scope(xworld.policy):
        with pytest.raises(NameHintLoaderError) as missing:
            load_name_hint_terms(folder, file_access=xworld.policy)
        (folder / "manifest.yaml").write_text("files: []\n")
        with pytest.raises(NameHintLoaderError) as empty:
            load_name_hint_terms(folder, file_access=xworld.policy)
        (folder / "manifest.yaml").write_text("files: [a.yaml]\n")
        with pytest.raises(NameHintLoaderError) as unlisted:
            load_name_hint_terms(folder, file_access=xworld.policy)
    for exc in (missing.value, empty.value, unlisted.value):
        assert_no_locator(xworld, rendered(exc))


# -- other inventoried sites -----------------------------------------------------------------


def test_custom_provider_directory_warning_names_no_locator(
    xworld: XWorld,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from decoy_engine.internal.faker_setup import load_custom_providers

    not_a_dir = xworld.sub / "providers.txt"
    not_a_dir.write_text("x")
    with caplog.at_level(logging.DEBUG):
        load_custom_providers(not_a_dir)
        assert any(LOCATOR_SENTINEL in r.getMessage() for r in caplog.records)  # control
        caplog.clear()
        with fileaccess.scope(xworld.policy):
            load_custom_providers(not_a_dir)
    assert caplog.records
    assert_no_locator(xworld, [r.getMessage() for r in caplog.records])


def test_lockfile_diagnostics_name_no_locator(xworld: XWorld) -> None:
    from decoy_engine.quality.dp_provenance import ProvenanceError, assert_lock_matches_installed

    lock = xworld.sub / "uv.lock"
    lock.write_text("not = [valid")
    with pytest.raises(ProvenanceError) as control:
        assert_lock_matches_installed(lock)
    assert LOCATOR_SENTINEL in str(control.value)
    with fileaccess.scope(xworld.policy), pytest.raises(ProvenanceError) as info:
        assert_lock_matches_installed(lock)
    assert_no_locator(xworld, rendered(info.value))
    lock.write_text("version = 1\n")
    with pytest.raises(ProvenanceError) as control2:
        assert_lock_matches_installed(lock)
    assert LOCATOR_SENTINEL in str(control2.value)
    with fileaccess.scope(xworld.policy), pytest.raises(ProvenanceError) as info2:
        assert_lock_matches_installed(lock)
    assert_no_locator(xworld, rendered(info2.value))


def test_quarantine_alias_diagnostic_names_no_locator(xworld: XWorld) -> None:
    from decoy_engine.quarantine import guard_quarantine_not_aliasing_committed_table

    target = xworld.sub / "out" / "t.parquet"

    class Sink:
        def committed_table_path(self, table: str) -> str:
            return str(target)

    with pytest.raises(ValueError) as control:
        guard_quarantine_not_aliasing_committed_table(Sink(), ["t"], str(target))
    assert LOCATOR_SENTINEL in str(control.value)
    with fileaccess.scope(xworld.policy), pytest.raises(ValueError) as info:
        guard_quarantine_not_aliasing_committed_table(Sink(), ["t"], str(target))
    assert_no_locator(xworld, rendered(info.value))


def test_reference_table_diagnostics_use_the_position(xworld: XWorld) -> None:
    from decoy_engine.reference_tables import load_table

    path = xworld.sub / "ref.parquet"
    pq.write_table(pa.table({"x": [1]}), path)
    with pytest.raises(ValueError) as control:
        load_table("ref", path)
    assert LOCATOR_SENTINEL in str(control.value)
    with fileaccess.scope(xworld.policy), pytest.raises(ValueError) as info:
        load_table("ref", path)
    assert_no_locator(xworld, rendered(info.value))


def test_joint_mask_unknown_reference_names_no_locator(xworld: XWorld) -> None:
    from decoy_engine.plan._errors import PlanCompileError
    from decoy_engine.transforms.joint_mask import validate_joint_mask_config

    cfg = {
        "columns": ["a"],
        "reference": f"customer:{xworld.sub / 'nope.parquet'}",
        "key_by": "a",
    }
    with pytest.raises(PlanCompileError) as control:
        validate_joint_mask_config(cfg)
    assert LOCATOR_SENTINEL in str(control.value)
    with (
        fileaccess.scope(xworld.policy),
        pytest.raises((PlanCompileError, fileaccess.FileAccessDenied)) as info,
    ):
        validate_joint_mask_config(cfg)
    assert_no_locator(xworld, rendered(info.value))
