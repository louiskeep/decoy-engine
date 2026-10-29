"""F22 A7: the out-of-core route must clean up on BaseException exactly like the sequential one."""

from __future__ import annotations

from pathlib import Path

import pytest

from decoy_engine import publication as pub
from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import _runner
from tests.unit.execution import _r3_scenarios as sc
from tests.unit.publication import _support as sup

pytestmark = pytest.mark.filterwarnings("ignore")


class _Counting(ParquetTransactionalSink):
    def __init__(self, target: Path) -> None:
        super().__init__(target)
        self.aborts = 0

    def abort(self) -> None:
        self.aborts += 1
        super().abort()


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_ooc_base_exception_before_commit_aborts_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc_type: type[BaseException]
) -> None:
    cfg, tables = sc.fk_job(tmp_path)
    out = tmp_path / "sink_out"
    sink = _Counting(out)

    def interrupted(*_a: object, **_k: object) -> None:
        assert sup.staging_dirs(tmp_path), "staging must exist before the interrupt (non-vacuous)"
        raise exc_type()

    monkeypatch.setattr(_runner._validate_batch, "finish_validation", interrupted)
    with pytest.raises(exc_type):
        sc._go(cfg, tables, sink=sink, execution_mode="out_of_core")
    assert sink.aborts == 1
    assert not out.exists() and sup.staging_dirs(tmp_path) == []


def test_ooc_abort_failure_does_not_mask_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, tables = sc.fk_job(tmp_path)
    sink = _Counting(tmp_path / "sink_out")
    monkeypatch.setattr(sink, "abort", lambda: (_ for _ in ()).throw(OSError("abort boom")))

    def failing(*_a: object, **_k: object) -> None:
        raise RuntimeError("original failure")

    monkeypatch.setattr(_runner._validate_batch, "finish_validation", failing)
    with pytest.raises(RuntimeError, match="original failure"):
        sc._go(cfg, tables, sink=sink, execution_mode="out_of_core")


def test_ooc_commit_failure_is_a_typed_refusal_and_staging_is_aborted(tmp_path: Path) -> None:
    cfg, tables = sc.fk_job(tmp_path)
    out = tmp_path / "sink_out"
    out.mkdir()
    (out / "legacy.txt").write_text("occupant")
    with pytest.raises(pub.PublicationRefusedError) as exc:
        sc._go(cfg, tables, sink=ParquetTransactionalSink(out), execution_mode="out_of_core")
    assert exc.value.receipt.reason_code == "target_occupied_legacy"
    assert [p.name for p in out.iterdir()] == ["legacy.txt"] and sup.staging_dirs(tmp_path) == []


def test_abort_after_the_binding_never_runs_on_the_ooc_route(tmp_path: Path) -> None:
    cfg, tables = sc.fk_job(tmp_path)
    out = tmp_path / "sink_out"
    sink = _Counting(out)
    result = sc._go(cfg, tables, sink=sink, execution_mode="out_of_core")
    assert sink.aborts == 0 and result.publication_receipt.outcome == "published"  # type: ignore[union-attr]
    sup.oracle(out)
