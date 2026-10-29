"""F29 acceptance A10: an owner reports retry and checkpoint state truthfully.

A retry *classification* alone never emits a scheduled retry; only the owner of a schedule or of
a durable checkpoint reports one. The engine has no checkpoint store, so an engine route that
reports nothing is `unsupported`, and `committed` needs its opaque reference.
"""

from __future__ import annotations

import pytest

from decoy_engine import progress
from decoy_engine.progress import CheckpointInfo, RetryInfo
from decoy_engine.reason_adapter import reason_from_exception
from decoy_engine.reasons import Phase, RetryClass
from decoy_engine.sdk import PermanentError, TransientError


def _collect() -> tuple[list[progress.ProgressEvent], progress.CallbackSink]:
    seen: list[progress.ProgressEvent] = []
    return seen, progress.CallbackSink(seen.append)


def test_a_retry_classification_alone_emits_no_scheduled_retry() -> None:
    seen, sink = _collect()
    with progress.use_progress(sink) as ch, ch.run_scope():
        record = reason_from_exception(TransientError("blip"), phase=Phase.PUBLISH)
    assert record.retry_class is RetryClass.RETRY_OPERATION
    assert [e for e in seen if e.kind == "retry"] == []
    assert (
        reason_from_exception(PermanentError("gone")).retry_class is RetryClass.ENVIRONMENT_REPAIR
    )


def test_the_owner_reports_attempts_and_budget_exhaustion() -> None:
    seen, sink = _collect()
    with progress.use_progress(sink) as ch:
        assert ch.retry_report(RetryInfo("operation", 1, 0, "running"), table="t")
        assert ch.retry_report(RetryInfo("operation", 1, 0, "scheduled", delay_s=2.0), table="t")
        assert ch.retry_report(RetryInfo("operation", 2, 1, "running"), table="t")
        assert ch.retry_report(RetryInfo("operation", 3, 2, "exhausted"), table="t")
    states = [(e.retry.attempt, e.retry.state, e.retry.delay_s) for e in seen if e.kind == "retry"]
    assert states == [
        (1, "running", None),
        (1, "scheduled", 2.0),
        (2, "running", None),
        (3, "exhausted", None),
    ]


def test_attempt_numbers_do_not_go_backwards_within_a_scope() -> None:
    seen, sink = _collect()
    with progress.use_progress(sink) as ch:
        assert ch.retry_report(RetryInfo("attempt", 2, 1, "running"))
        assert not ch.retry_report(RetryInfo("attempt", 1, 0, "running"))
        # A different scope has its own counter (a job attempt versus a governor route attempt).
        assert ch.retry_report(RetryInfo("route", 1, 0, "running"))
        assert ch.rejected_reports == 1
    assert [e.retry.scope for e in seen if e.kind == "retry"] == ["attempt", "route"]


def test_checkpoint_states_and_the_committed_reference_requirement() -> None:
    seen, sink = _collect()
    with progress.use_progress(sink) as ch:
        ch.checkpoint_report(CheckpointInfo("unsupported"))
        ch.checkpoint_report(CheckpointInfo("none"))
        ch.checkpoint_report(CheckpointInfo("writing", sequence=1, rows=10))
        ch.checkpoint_report(
            CheckpointInfo("committed", reference="ckpt-1", sequence=1, rows=10, bytes=80)
        )
        ch.checkpoint_report(CheckpointInfo("failed", sequence=2))
    assert [e.checkpoint.state for e in seen if e.kind == "checkpoint"] == [
        "unsupported",
        "none",
        "writing",
        "committed",
        "failed",
    ]
    with pytest.raises(ValueError):
        CheckpointInfo("committed", sequence=1)  # no reference
    with pytest.raises(ValueError):
        CheckpointInfo("committed", reference="/var/ckpt", sequence=1)  # a path is not opaque


def test_the_checkpoint_sequence_never_regresses() -> None:
    seen, sink = _collect()
    with progress.use_progress(sink) as ch:
        assert ch.checkpoint_report(CheckpointInfo("committed", reference="c2", sequence=2))
        assert not ch.checkpoint_report(CheckpointInfo("committed", reference="c1", sequence=1))
        assert ch.checkpoint_report(CheckpointInfo("committed", reference="c3", sequence=3))
        assert ch.rejected_reports == 1
    assert [e.checkpoint.sequence for e in seen if e.kind == "checkpoint"] == [2, 3]


def test_an_engine_route_without_a_store_never_claims_a_committed_checkpoint(tmp_path) -> None:
    import pandas as pd
    import pyarrow as pa

    from decoy_engine.config import PipelineConfig
    from decoy_engine.execution import run_pipeline

    df = pd.DataFrame({"zip": ["90210", "10001"]})
    df.to_csv(tmp_path / "c.csv", index=False)
    cfg = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"c": {"type": "file", "format": "csv", "path": str(tmp_path / "c.csv")}},
            "tables": [
                {
                    "name": "c",
                    "columns": [
                        {"name": "zip", "strategy": "truncate", "provider_config": {"length": 3}}
                    ],
                }
            ],
            "targets": {"c": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}},
        }
    ).model_dump()
    seen, sink = _collect()
    run_pipeline(
        cfg,
        {"c": pa.Table.from_pandas(df, preserve_index=False)},
        engine_version="f29-a10",
        progress=sink,
    )
    # No owner reported anything: the engine has no checkpoint store, so there is no claim at all.
    assert [e for e in seen if e.kind in ("checkpoint", "retry")] == []
    assert seen[-1].kind == "terminal" and seen[-1].status == "ok" and seen[-1].reason is None


def test_consumer_fixture_one_failed_attempt_and_one_success_without_double_counting() -> None:
    """The consumer contract: counts come from each attempt's final samples, grouped by attempt."""
    import pyarrow as pa

    attempts: dict[int, list[progress.ProgressEvent]] = {}
    for attempt, fail in ((1, True), (2, False)):
        seen, sink = _collect()
        try:
            with progress.use_progress(sink, interval_s=3600) as ch, ch.run_scope():
                ch.retry_report(RetryInfo("attempt", attempt, attempt - 1, "running"), table="t")
                it = ch.count_rows("t", [pa.table({"a": [1, 2]}), pa.table({"a": [3]})])
                next(it)
                if fail:
                    it.close()
                    raise RuntimeError("attempt 1 failed")
                list(it)
        except RuntimeError:
            pass
        attempts[attempt] = seen

    def final_rows(events: list[progress.ProgressEvent]) -> int:
        return sum(e.rows_done for e in events if isinstance(e, progress.RateSample) and e.final)

    assert final_rows(attempts[1]) == 2  # the failed attempt only counts what it consumed
    assert final_rows(attempts[2]) == 3
    # Totals are per attempt; summing attempts would double-count, so consumers key by attempt.
    assert {a: next(e.sequence for e in evs) for a, evs in attempts.items()} == {1: 1, 2: 1}
    terminals = {a: evs[-1] for a, evs in attempts.items()}
    assert terminals[1].status == "error" and terminals[1].reason is not None
    assert terminals[2].status == "ok" and terminals[2].reason is None
