"""Phase F FR0: the public streamed entry point, its sink contract and its failure atomicity."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._transactional_sink import _CallableSinkAdapter
from decoy_engine.generation import _column_streams
from decoy_engine.generation import _plan_entry as plan_entry
from decoy_engine.generation._plan_entry import generate_tables, generate_tables_streamed
from decoy_engine.generation._streamed import is_bounded_sink
from tests.parity import _f0_fixtures as fx
from tests.parity._fr0_support import fingerprints, plan_of


def _relational_plan() -> Any:
    return plan_of(
        fx.config(
            [
                fx.table(
                    "child",
                    200,
                    [
                        {"name": "cid", "type": "sequence", "start": 1},
                        {
                            "name": "pid",
                            "type": "reference",
                            "reference_table": "parent",
                            "reference_column": "id",
                        },
                    ],
                ),
                fx.table(
                    "parent",
                    50,
                    [
                        {"name": "id", "type": "sequence", "prefix": "P"},
                        {"name": "who", "type": "faker", "faker_type": "first_name"},
                    ],
                ),
            ]
        )
    )


def _staging_dirs(parent: Path) -> list[Path]:
    return [p for p in parent.iterdir() if p.name.startswith("_decoy_stage_")]


# ---- the sink capability marker ---------------------------------------------------------------


def test_only_the_parquet_sink_carries_the_bounded_marker(tmp_path: Path) -> None:
    assert is_bounded_sink(ParquetTransactionalSink(tmp_path / "o"))
    assert not is_bounded_sink(_CallableSinkAdapter(lambda *a: None))
    assert not is_bounded_sink(lambda *a: None)
    assert not is_bounded_sink(None)


# ---- streaming ---------------------------------------------------------------------------------


def test_streamed_run_is_metadata_only_and_commits_one_file_per_table(tmp_path: Path) -> None:
    plan = _relational_plan()
    target = tmp_path / "out"
    result = generate_tables_streamed(
        plan, sink=ParquetTransactionalSink(target), budget_bytes=256 * 1024 * 1024, batch_rows=64
    )
    assert result.mode == "streamed" and result.reason == "eligible" and result.tables is None
    assert result.row_counts == {"parent": 50, "child": 200}
    assert sorted(p.name for p in target.glob("*.parquet")) == ["child.parquet", "parent.parquet"]
    assert (
        pq.ParquetFile(target / "child.parquet").metadata.num_row_groups == 4
    )  # 64 rows per batch
    assert _staging_dirs(tmp_path) == []
    resident = generate_tables(plan)
    committed = {p.stem: pq.read_table(p) for p in target.glob("*.parquet")}
    assert fingerprints(committed) == fingerprints(resident)


def test_streamed_commit_happens_once_after_every_table(tmp_path: Path) -> None:
    events: list[str] = []

    class Recording(ParquetTransactionalSink):
        def write_batches(self, table: str, batches: Any, *, schema: pa.Schema) -> None:
            events.append(f"write:{table}")
            super().write_batches(table, batches, schema=schema)

        def commit(self) -> None:
            events.append("commit")
            super().commit()

    generate_tables_streamed(_relational_plan(), sink=Recording(tmp_path / "o"))
    assert events == ["write:parent", "write:child", "commit"]


# ---- transactional abort ---------------------------------------------------------------------


class _BoomError(RuntimeError):
    pass


def test_failure_at_a_later_table_aborts_without_a_partial_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Any, offset: int, size: int) -> Any:
        raise _BoomError("child stream failed")

    monkeypatch.setattr(_column_streams.ReferenceStream, "next_batch", boom)
    target = tmp_path / "out"
    with pytest.raises(_BoomError, match="child stream failed"):
        generate_tables_streamed(_relational_plan(), sink=ParquetTransactionalSink(target))
    assert not target.exists(), "a partial commit reached the target"
    assert _staging_dirs(tmp_path) == [], "staging was not removed"


def test_writer_close_failure_aborts_and_preserves_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _transactional_sink as ts

    class FailingClose(pq.ParquetWriter):
        def close(self) -> None:
            super().close()
            raise OSError("close failed")

    monkeypatch.setattr(ts.pq, "ParquetWriter", FailingClose)
    target = tmp_path / "out"
    with pytest.raises(OSError, match="close failed"):
        generate_tables_streamed(_relational_plan(), sink=ParquetTransactionalSink(target))
    assert not target.exists() and _staging_dirs(tmp_path) == []


def test_commit_failure_aborts_and_preserves_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import _transactional_sink as ts

    def failing_replace(src: Any, dst: Any) -> None:
        raise OSError("commit failed")

    monkeypatch.setattr(ts.os, "replace", failing_replace)
    target = tmp_path / "out"
    with pytest.raises(OSError, match="commit failed"):
        generate_tables_streamed(_relational_plan(), sink=ParquetTransactionalSink(target))
    assert not target.exists() and _staging_dirs(tmp_path) == []


def test_budget_breach_after_the_first_batch_aborts_the_sink_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No post-start fallback: a reservation breach mid-run aborts, it never switches mode."""
    from decoy_engine.generation._budget_ledger import (
        GenerationBudgetExceeded,
        GenerationBudgetLedger,
    )

    real = GenerationBudgetLedger.reserve

    def refuse_child(self: Any, stage: str, n: int) -> Any:
        if stage.startswith("batch_buffers:child"):
            raise GenerationBudgetExceeded(code="generation_budget_exceeded", message=stage)
        return real(self, stage, n)

    monkeypatch.setattr(GenerationBudgetLedger, "reserve", refuse_child)
    target = tmp_path / "out"
    with pytest.raises(GenerationBudgetExceeded):
        generate_tables_streamed(
            _relational_plan(),
            sink=ParquetTransactionalSink(target),
            budget_bytes=256 * 1024 * 1024,
        )
    assert not target.exists() and _staging_dirs(tmp_path) == []


# ---- the Plan-only boundary is preserved --------------------------------------------------------


def test_raw_config_is_rejected_before_anything_runs() -> None:
    with pytest.raises(TypeError, match="requires a compiled"):
        generate_tables_streamed({"tables": []}, sink=None)  # type: ignore[arg-type]


def test_plan_without_a_generation_payload_is_rejected() -> None:
    from decoy_engine.plan import compile_plan
    from decoy_engine.profile import Profile  # noqa: F401

    mask_only = compile_plan(
        {"version": 1, "global_settings": {}, "sources": {}, "tables": [], "targets": {}},
        fx._profile(),
        decoy_engine_version="fr0",
    )
    with pytest.raises(TypeError, match="no generation payload"):
        generate_tables_streamed(mask_only, sink=None)


def test_when_validation_runs_before_any_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    from decoy_engine.expressions import _when_parser

    real_validate = _when_parser.validate_plan_when
    monkeypatch.setattr(
        _when_parser, "validate_plan_when", lambda p: (order.append("when"), real_validate(p))[1]
    )
    real_run = plan_entry.run_generation
    monkeypatch.setattr(
        plan_entry, "run_generation", lambda *a, **k: (order.append("run"), real_run(*a, **k))[1]
    )
    generate_tables_streamed(plan_of(fx.cfg_sequence()), sink=None)
    assert order == ["when", "run"]


def test_pinned_statistical_inputs_are_not_reopened_from_disk(tmp_path: Path) -> None:
    """The statistical plan is declined to the legacy path but must still consume its pinned bytes."""
    copy = tmp_path / "snap.json"
    shutil.copy(fx.SNAPSHOT_PATH, copy)
    cfg = fx.cfg_statistical()
    for col in cfg["tables"][0]["generate_columns"]:
        col["snapshot_file"] = str(copy)
    plan = plan_of(cfg)
    copy.unlink()
    result = generate_tables_streamed(plan, sink=None)
    assert result.mode == "resident" and result.reason.startswith("legacy:")
    assert result.tables is not None and result.tables["stat"].num_rows == 60
    assert fingerprints(result.tables) == fingerprints(generate_tables(plan))
