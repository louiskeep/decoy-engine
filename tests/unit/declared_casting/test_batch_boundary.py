"""The batch API is the one casting kernel every route uses: any partition of the same rows
gives the same typed rows, absolute positions, counts and quarantine set as one whole table."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from tests.acceptance._f06_support import (
    INT64,
    ISO_DATE,
    PROTECTED_QUARANTINE,
    column,
    config,
    text_table,
    validate,
    write_csv,
    zero_pad,
)

DATES = ["2026-02-01", "TBD", "bad", "2026-03-05", None, "2026-12-31", "2024-02-29"]
NUMS = ["042", "7", "8", "x", "5", None, "1000"]  # "x" fails parse, "1000" fails the width


def _run(tmp_path: Path, policy: str = "null", runtime: Any = None) -> Any:
    from decoy_engine.declared_casting import DeclaredCastRun
    from decoy_engine.plan._checks_declared_casting import compile_declared_casting_contract

    src = write_csv(tmp_path / "t.csv", ["id", "start", "n"], [["1", "2026-02-01", "1"]])
    cols = [
        column("id"),
        column(
            "start",
            logical_type="date",
            parse_format=ISO_DATE,
            sentinel_to_null={"id": "sentinel/exact/v1", "values": ["TBD"]},
            error_policy=policy,
            output_format={"id": "date/iso8601/v1", "width": 10, "width_unit": "code_points"},
        ),
        column(
            "n",
            logical_type="integer",
            parse_format=INT64,
            error_policy=policy,
            output_format=zero_pad(3),
        ),
    ]
    top = {"protected_quarantine": PROTECTED_QUARANTINE} if policy == "quarantine" else None
    cfg = validate(config(cols, source_path=src, table="t", top=top))
    return DeclaredCastRun(compile_declared_casting_contract(cfg), runtime)


def _table() -> pa.Table:
    return text_table(id=[str(i) for i in range(7)], start=DATES, n=NUMS)


def _through(
    run: Any, table: pa.Table, size: int, *, masked: Any = None
) -> tuple[pa.Table, list[int]]:
    """Cast then format in `size`-row batches; returns the formatted rows and absolute positions."""
    outs: list[pa.Table] = []
    positions: list[int] = []
    for start in range(0, table.num_rows, size):
        batch = run.cast_batch("t", table.slice(start, size), offset=start)
        out = batch.table if masked is None else masked(batch.table)
        formatted, kept = run.format_batch("t", out, batch)
        positions.extend(batch.positions if kept is None else [batch.positions[i] for i in kept])
        outs.append(formatted)
    return pa.concat_tables(outs), positions


@pytest.mark.parametrize("size", [1, 2, 3, 7, 100])
def test_any_partition_gives_the_whole_table_result(tmp_path: Path, size: int) -> None:
    whole_run = _run(tmp_path)
    whole, whole_pos = _through(whole_run, _table(), 100)
    run = _run(tmp_path)
    got, pos = _through(run, _table(), size)
    assert got.equals(whole) and got.schema.equals(whole.schema)
    assert pos == whole_pos
    assert run.partial_counts() == whole_run.partial_counts()


@pytest.mark.parametrize("size", [1, 2, 7])
def test_row_quarantine_positions_are_absolute_across_partitions(tmp_path: Path, size: int) -> None:
    from decoy_engine.declared_casting import (
        LocalProtectedQuarantineSink,
        ProtectedQuarantineRuntime,
        QuarantineAccessContext,
    )
    from decoy_engine.keyprovider import SecretKeyProvider

    def quarantined(sz: int, root: Path) -> tuple[list[int], list[int], dict[str, Any]]:
        sink = LocalProtectedQuarantineSink(root, key_provider=SecretKeyProvider(secret=b"q" * 32))
        runtime = ProtectedQuarantineRuntime(sink=sink, owner="o", run_id="r")
        run = _run(tmp_path, "quarantine", runtime)
        _, kept = _through(run, _table(), sz)
        evidence, token = run.finish()
        records = sink.open(token, QuarantineAccessContext(owner="o", run_id="r"))
        return kept, sorted(r.row_position for r in records), evidence

    whole = quarantined(100, tmp_path / "q-whole")
    got = quarantined(size, tmp_path / "q-part")
    assert got[0] == whole[0] and got[1] == whole[1]
    assert whole[0] == [0, 1, 4, 5]
    assert whole[1] == [2, 3, 6]  # bad date, bad integer, then the format-stage width overflow

    def stable(evidence: dict[str, Any]) -> dict[str, Any]:
        q = {
            k: v for k, v in evidence["quarantine"].items() if k in ("rows", "bytes", "protection")
        }
        return {**evidence, "quarantine": q}

    assert stable(got[2]) == stable(whole[2])  # ref / digest / times are per-artifact


def test_record_false_has_no_side_effects(tmp_path: Path) -> None:
    run = _run(tmp_path)
    batch = run.cast_batch("t", _table(), offset=0, record=False)
    assert batch.table.num_rows == 7
    counts = run.partial_counts()
    assert counts["total_failures"] == 0 and counts["total_substitutions"] == 0


def test_fail_policy_aborts_with_a_registered_code(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import DeclaredCastValueError

    run = _run(tmp_path, "fail")
    with pytest.raises(DeclaredCastValueError) as exc:
        _through(run, _table(), 2)
    # columns run in plan order within a batch: the first failure is deterministic for a partition
    assert exc.value.code == "declared_integer_invalid"
