"""Streamed column facts equal the whole-table pandas values, whatever the batch layout."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.detection.detectors import run_all_detectors
from decoy_engine.profiling import column_distinct
from decoy_engine.verification.postmask import from_table
from decoy_engine.verification.postmask._column_facts import column_facts
from decoy_engine.verification.postmask.policy_validation import check_policy_validation
from decoy_engine.verification.postmask.residual_pii import _source_identity

LIMIT = "256MB"
COLUMNS = ["email", "code", "num", "same", "const"]


def _pair(n: int) -> tuple[pa.Table, pa.Table]:
    ks = range(n)
    src_email = [f"user{k}@corp.com" for k in ks]
    out_email = [
        None if k % 11 == 5 else src_email[k] if k % 3 == 0 else f"u{k}@m.org" if k % 2 else f"x{k}"
        for k in ks
    ]
    src_code = [f"c{k % 50}" for k in ks]
    out_code = [src_code[k] if k % 4 else f"d{k % 7}" for k in ks]
    # Nulls only from row 7 on, so a batch of 7 sees none in its first batch.
    src_num = [10_000 + k for k in ks]
    out_num = [None if k % 13 == 8 else 10_000 + k + (k % 5 == 0) for k in ks]
    same = [f"s{k % 9}" for k in ks]
    const = ["z"] * n
    src = pa.table(
        {"email": src_email, "code": src_code, "num": pa.array(src_num, pa.int64())}
        | {"same": same, "const": const}
    )
    out = pa.table(
        {"email": out_email, "code": out_code, "num": pa.array(out_num, pa.int64())}
        | {"same": same, "const": const}
    )
    return src, out


def _chunked(table: pa.Table, sizes: list[int]) -> pa.Table:
    """The same rows split at uneven offsets, cycling through `sizes`."""
    parts, at, i = [], 0, 0
    while at < table.num_rows:
        parts.append(table.slice(at, sizes[i % len(sizes)]))
        at += sizes[i % len(sizes)]
        i += 1
    return pa.Table.from_batches([b for p in parts for b in p.to_batches()])


def _frames(table: pa.Table) -> pd.DataFrame:
    # Integers with nulls become Python ints, so a null in one batch cannot retype the column.
    return pd.DataFrame(
        {c: table[c].to_pandas(integer_object_nulls=True) for c in table.column_names}
    )


def _run(src: pa.Table, out: pa.Table, batch_rows: int, tmp_path: Path):
    return column_facts(
        from_table("t", src),
        from_table("t", out),
        COLUMNS,
        batch_rows=batch_rows,
        scratch=tmp_path,
        memory_limit=LIMIT,
    )


def _oracle(src: pa.Table, out: pa.Table):
    sf, of = {"t": _frames(src)}, {"t": _frames(out)}
    cfg = {"tables": [{"name": "t", "columns": [{"name": c, "strategy": "x"} for c in COLUMNS]}]}
    policy = {f.column: f for f in check_policy_validation(sf, of, cfg)}
    return sf, of, policy


@pytest.mark.parametrize("batch_rows", [7, 100_000])
@pytest.mark.parametrize("n", [10, 10_000, 250_000])
def test_facts_equal_whole_table_pandas(n: int, batch_rows: int, tmp_path: Path) -> None:
    if n == 250_000 and batch_rows == 7:
        pytest.skip("35,714 detector passes per column; covered by the uneven-layout case")
    src, out = _pair(n)
    facts = _run(src, out, batch_rows, tmp_path)
    sf, of, policy = _oracle(src, out)
    assert set(facts) == set(COLUMNS)
    for col in COLUMNS:
        f = facts[col]
        rate, identical, src_distinct = _source_identity(sf, "t", col, of["t"][col])  # type: ignore[misc]
        assert (f.source_rows, f.output_rows) == (n, n)
        assert f.identical_rows == identical
        assert f.identical_rows / f.comparable_rows == rate
        assert f.bytes_identical == policy[col].bytes_identical
        assert f.source_distinct == src_distinct == policy[col].source_distinct
        assert f.output_distinct == policy[col].output_distinct
        expected = [
            (m.detector_id, m.match_rate, m.confidence)
            for m in run_all_detectors(of["t"][col], col)
        ]
        assert [(d.detector_id, d.match_rate, d.confidence) for d in f.detectors] == expected
    assert facts["email"].detectors, "the email column must fire so the rate check has teeth"


def test_batch_layout_changes_nothing(tmp_path: Path) -> None:
    """Nulls only in the second batch, one batch against uneven batches on both sides."""
    src, out = _pair(100)
    whole = _run(src, out, 100_000, tmp_path)
    split = column_facts(
        from_table("t", _chunked(src, [7, 3, 11])),
        from_table("t", _chunked(out, [5, 13, 2, 9])),
        COLUMNS,
        batch_rows=4,
        scratch=tmp_path,
        memory_limit=LIMIT,
    )
    assert split == whole
    assert whole["num"].comparable_rows < 100  # nulls exist, so the second-batch case is real
    # Five-digit ints read as zip codes only while they are rendered without a ".0" suffix.
    assert [d.detector_id for d in whole["num"].detectors] == ["us_zip"]


def test_unaligned_row_counts_skip_identity(tmp_path: Path) -> None:
    src, out = _pair(30)
    facts = column_facts(
        from_table("t", src.slice(0, 20)),
        from_table("t", out),
        COLUMNS,
        scratch=tmp_path,
        memory_limit=LIMIT,
    )
    f = facts["code"]
    assert (f.source_rows, f.output_rows) == (20, 30)
    assert (f.comparable_rows, f.identical_rows, f.bytes_identical) == (0, 0, False)


def test_without_source_only_output_facts(tmp_path: Path) -> None:
    _, out = _pair(30)
    facts = column_facts(
        None, from_table("t", out), ["email"], scratch=tmp_path, memory_limit=LIMIT
    )
    assert facts["email"].source_rows == 0
    assert facts["email"].output_distinct == len(set(out["email"].drop_null().to_pylist()))


def test_column_distinct_counts_non_null_values(tmp_path: Path) -> None:
    table = pa.table({"anything; DROP": ["a", "b", "a", None, "c", None]})
    reader = pa.RecordBatchReader.from_batches(table.schema, table.to_batches(max_chunksize=2))
    assert column_distinct(reader, scratch=tmp_path, memory_limit=LIMIT) == 3
