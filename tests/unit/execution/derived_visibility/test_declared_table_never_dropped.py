"""A table declared under `tables:` is never silently dropped from the run.

A loader-only table (no `sources:` entry, no caller table) was profiled as nothing, so the plan
held no work for it and the adapter echoed the raw input. Two gates now refuse that shape: one
before any read, one after the plan is built.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import classify_table_kinds
from decoy_engine.errors import ConfigError
from decoy_engine.execution import _guards, run_pipeline
from decoy_engine.plan import compile_plan
from decoy_engine.profile import profile_source
from decoy_engine.reason_adapter import reason_from_exception

pytestmark = pytest.mark.filterwarnings("ignore")

_EMAIL = "secret.person@example.com"


def _table() -> pa.Table:
    return pa.table({"id": ["a", "b"], "email": [_EMAIL, "z@w.com"]})


def _config(*, source_dir: Path | None = None, names: tuple[str, ...] = ("t",)) -> dict:
    cfg: dict = {
        "version": 1,
        "tables": [
            {
                "name": name,
                "columns": [
                    {"name": "id", "strategy": "passthrough"},
                    {
                        "name": "email",
                        "strategy": "faker",
                        "provider": "person_email",
                        "deterministic": True,
                        "namespace": "n",
                    },
                ],
            }
            for name in names
        ],
    }
    if source_dir is not None:
        cfg["sources"] = {
            n: {"type": "file", "format": "parquet", "path": str(source_dir / f"{n}.parquet")}
            for n in names
        }
    return cfg


def test_loader_table_without_source_entry_is_refused_before_any_read() -> None:
    table = _table()
    loads: list[str] = []

    def loader(name: str) -> pa.Table:
        loads.append(name)
        return table

    with pytest.raises(ConfigError) as caught:
        run_pipeline(
            _config(),
            {},
            engine_version="0.1.0",
            source_loader=loader,
            source_loader_schemas={"t": table.schema},
        )
    record = reason_from_exception(caught.value)
    assert (record.namespace, record.code) == ("config", "loader_table_undeclared")
    assert loads == []
    assert _EMAIL not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_dropped_declared_table_is_refused_after_the_plan() -> None:
    cfg = _config(names=("a", "b"))
    profile = profile_source(_config(names=("a",)), seed=1, supplied_sources={"a": _table()})
    plan = compile_plan(_config(names=("a",)), profile, decoy_engine_version="0.1.0")
    with pytest.raises(ConfigError) as caught:
        _guards.refuse_dropped_declared_tables(classify_table_kinds(cfg), plan, {"a", "b"})
    record = reason_from_exception(caught.value)
    assert (record.namespace, record.code) == ("config", "declared_table_dropped")
    assert _EMAIL not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_declared_table_nobody_supplies_keeps_running() -> None:
    result = run_pipeline(_config(), {}, engine_version="0.1.0")
    assert result.outputs == {}


def test_loader_table_with_source_entry_still_masks(tmp_path: Path) -> None:
    table = _table()
    pq.write_table(table, tmp_path / "t.parquet")
    result = run_pipeline(
        _config(source_dir=tmp_path),
        {},
        engine_version="0.1.0",
        source_loader=lambda n: table,
        source_loader_schemas={"t": table.schema},
    )
    assert _EMAIL not in result.outputs["t"].column("email").to_pylist()


def test_run_pipeline_refuses_a_dropped_table_when_the_first_gate_is_bypassed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pins the post-plan guard's call site: with the pre-read gate gone, the loader-only table
    # still profiles as nothing, so only the construction guard stands between it and an echo.
    monkeypatch.setattr(_guards, "refuse_undeclared_loader_tables", lambda *a, **k: None)
    table = _table()
    loads: list[str] = []

    def loader(name: str) -> pa.Table:
        loads.append(name)
        return table

    with pytest.raises(ConfigError) as caught:
        run_pipeline(
            _config(),
            {},
            engine_version="0.1.0",
            source_loader=loader,
            source_loader_schemas={"t": table.schema},
        )
    record = reason_from_exception(caught.value)
    assert (record.namespace, record.code) == ("config", "declared_table_dropped")
    assert loads == []
    assert _EMAIL not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
