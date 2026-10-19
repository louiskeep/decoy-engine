"""OBS-2d and the shared carrier: `strategy_branch_calls` and `decline_records` folding.

`strategy_branch_calls` is a table-level cumulative per-strategy snapshot, folded by keeping
the last chunk's copy. Expected counts are hand-derived from the branch rule (one activation
per column per chunk), never read back from the field under test.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.errors import DecoyError
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._chunked_output_sink import OutputEvidenceAccumulator
from decoy_engine.execution._pipeline_auto_chunk import _without_elapsed
from decoy_engine.execution.native._chunked_evidence import (
    ColumnPlan,
    aggregate_chunked_route_evidence,
    chunk_route_evidence,
)
from decoy_engine.execution.native._route_decline_codes import (
    RouteDeclineCode as C,
)
from decoy_engine.execution.native._route_decline_codes import column_decline
from tests.native._b8_support import run_one
from tests.native._chunked_entry_support import (
    NEEDS_COMPANION,
    faker_col,
    force_oracle,
    make_config,
    passthrough,
    redact,
    split,
    truncate,
)
from tests.native._chunked_group_key_support import GB, gk_col, gk_source


def _strings(n: int, *names: str) -> pa.Table:
    return pa.table(
        {name: pa.array([f"value{i}" for i in range(n)], pa.string()) for name in names}
    )


def _snapshots(run: Any) -> list[dict[str, int]]:
    return [r.quality_metrics["chunked_route"]["strategy_branch_calls"] for r in run.sink]


def _fold_both(run: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    acc = OutputEvidenceAccumulator()
    for res in run.sink:
        acc.append(res)
    return aggregate_chunked_route_evidence(run.sink), acc.route_evidence()


# ---------------------------------------------------------------------------
# Hand-counted branch activations.
# ---------------------------------------------------------------------------


def test_two_columns_one_strategy_across_chunks_count_once_per_branch() -> None:
    # 11 rows in chunks of 4 is 3 chunks; 2 redact columns x 3 chunks = 6 activations, 1 truncate
    # column x 3 chunks = 3. Each chunk's snapshot is the running total.
    run = run_one(
        make_config([redact("a"), redact("b"), truncate("t")]),
        split(_strings(11, "a", "b", "t"), 4),
    )
    assert _snapshots(run) == [
        {"redact": 2, "truncate": 1},
        {"redact": 4, "truncate": 2},
        {"redact": 6, "truncate": 3},
    ]
    resident, streamed = _fold_both(run)
    # Summing the snapshots would give 2 + 4 + 6 = 12; the fold keeps the last.
    assert resident["strategy_branch_calls"] == {"redact": 6, "truncate": 3}
    assert streamed["strategy_branch_calls"] == resident["strategy_branch_calls"]
    # Per-column `calls` is a different, unchanged number: one per chunk.
    assert {c["column"]: c["calls"] for c in resident["columns"]} == {"a": 3, "b": 3, "t": 3}
    assert resident == streamed
    assert _without_elapsed(resident)["strategy_branch_calls"] == {"redact": 6, "truncate": 3}


def test_snapshot_matches_the_legacy_kernel_calls_and_the_pinned_faker_count() -> None:
    run = run_one(make_config([redact("a"), redact("b")]), split(_strings(8, "a", "b"), 4))
    assert run.ev[0].kernel_calls == {"redact": 4}
    assert _fold_both(run)[0]["strategy_branch_calls"] == {"redact": 4}


@NEEDS_COMPANION
def test_an_active_faker_column_counts_one_activation_per_chunk() -> None:
    run = run_one(make_config([faker_col("f")]), split(_strings(12, "f"), 4))
    assert _fold_both(run)[0]["strategy_branch_calls"] == {"faker": 3}


@NEEDS_COMPANION
def test_an_idle_group_key_chunk_is_still_a_branch_activation() -> None:
    # The count is a branch activation, not a compiled call: the empty middle chunk ran no
    # kernel but still counts, so 3 chunks give 3. A strategy the table never configures is
    # absent from the mapping rather than present with zero.
    chunks = [gk_source(["a", "b"]), gk_source([], pa.string()), gk_source(["a", "c"])]
    run = run_one(make_config([passthrough(GB), gk_col(), passthrough("p")]), chunks)
    assert run.ev[0].native_admitted is True
    resident, streamed = _fold_both(run)
    # Two passthrough columns (g, p) x 3 chunks also activate their branch.
    expected = {"group_key": 3, "passthrough": 6}
    assert resident["strategy_branch_calls"] == expected == streamed["strategy_branch_calls"]
    assert "hash" not in resident["strategy_branch_calls"]


def test_an_oracle_routed_table_reports_no_branch_calls() -> None:
    run = run_one(make_config([redact("r"), force_oracle("c")]), split(_strings(8, "r", "c"), 4))
    assert run.ev[0].native_admitted is False
    resident, streamed = _fold_both(run)
    assert resident["strategy_branch_calls"] == {} == streamed["strategy_branch_calls"]
    assert resident["native_admitted"] is False


def test_the_payload_is_json_safe_and_additive() -> None:
    run = run_one(make_config([redact("a")]), split(_strings(8, "a"), 4))
    resident, _ = _fold_both(run)
    assert json.loads(json.dumps(resident, allow_nan=False)) == resident
    assert {
        "table",
        "native_admitted",
        "reroute_reason",
        "pandas_read_passthrough",
        "columns",
    } <= set(resident)
    assert set(resident) - {
        "table",
        "native_admitted",
        "reroute_reason",
        "pandas_read_passthrough",
        "columns",
    } == {"decline_records", "strategy_branch_calls"}


# ---------------------------------------------------------------------------
# Fold contract on synthetic payloads (both folders in lockstep).
# ---------------------------------------------------------------------------


def _chunk(
    calls: dict[str, int], declines: tuple[Any, ...] = (), table: str = "t"
) -> ExecutionResult:
    evidence = chunk_route_evidence(
        table=table,
        native_admitted=not declines,
        reroute_reason=None,
        decline_records=declines,
        strategy_branch_calls=calls,
        columns=[ColumnPlan("c", "redact", "arrow_python")],
        elapsed_ms={},
    )
    return ExecutionResult(
        outputs={},
        timings=(),
        boundary_conversion_ms=0.0,
        warnings=(),
        quality_metrics={"chunked_route": evidence},
    )


def _folds(results: list[ExecutionResult]) -> tuple[dict[str, Any], dict[str, Any]]:
    acc = OutputEvidenceAccumulator()
    for r in results:
        acc.append(r)
    return aggregate_chunked_route_evidence(results), acc.route_evidence()


def test_the_last_snapshot_wins_in_emission_order_for_both_folders() -> None:
    results = [
        _chunk({"redact": 1}),
        _chunk({"redact": 3, "hash": 1}),
        _chunk({"redact": 4, "hash": 2}),
    ]
    for folded in _folds(results):
        assert folded["strategy_branch_calls"] == {"redact": 4, "hash": 2}


def test_the_fold_copies_so_later_mutation_cannot_change_the_payload() -> None:
    live = {"redact": 2}
    results = [_chunk(live)]
    resident, streamed = _folds(results)
    live["redact"] = 99
    live["hash"] = 1
    results[0].quality_metrics["chunked_route"]["strategy_branch_calls"]["redact"] = 77
    assert resident["strategy_branch_calls"] == {"redact": 2}
    assert streamed["strategy_branch_calls"] == {"redact": 2}
    # Mutating a returned payload does not reach the accumulator's retained state either.
    acc = OutputEvidenceAccumulator()
    acc.append(_chunk({"redact": 2}, (column_decline(C.OTHER, "t", "c"),)))
    first = acc.route_evidence()
    first["strategy_branch_calls"]["redact"] = 5
    first["decline_records"][0]["code"] = "tampered"
    again = acc.route_evidence()
    assert again["strategy_branch_calls"] == {"redact": 2}
    assert again["decline_records"][0]["code"] == "other"


def test_decline_records_are_taken_from_the_first_chunk_not_summed() -> None:
    rec = column_decline(C.FALLBACK_POLICY_NOT_NATIVE, "t", "c")
    results = [_chunk({}, (rec,)), _chunk({}, (rec,)), _chunk({}, (rec,))]
    for folded in _folds(results):
        assert folded["decline_records"] == [rec.to_payload()]


def test_chunks_that_disagree_on_decline_records_raise_in_both_folders() -> None:
    a = column_decline(C.FALLBACK_POLICY_NOT_NATIVE, "t", "c")
    b = column_decline(C.STRATEGY_VETOED_CHUNKED, "t", "c")
    results = [_chunk({}, (a,)), _chunk({}, (b,))]
    with pytest.raises(DecoyError) as resident:
        aggregate_chunked_route_evidence(results)
    assert resident.value.code == "chunked_route_evidence_inconsistent"
    acc = OutputEvidenceAccumulator()
    acc.append(results[0])
    with pytest.raises(DecoyError) as streamed:
        acc.append(results[1])
    assert streamed.value.code == "chunked_route_evidence_inconsistent"


def test_the_empty_head_defaults_carry_both_fields() -> None:
    empty = aggregate_chunked_route_evidence([])
    assert empty["decline_records"] == [] and empty["strategy_branch_calls"] == {}
    assert OutputEvidenceAccumulator().route_evidence() == empty


def test_the_table_stays_the_consistency_key() -> None:
    results = [_chunk({}, table="one"), _chunk({}, table="two")]
    with pytest.raises(DecoyError) as exc:
        aggregate_chunked_route_evidence(results)
    assert exc.value.code == "chunked_route_evidence_mixed_tables"
