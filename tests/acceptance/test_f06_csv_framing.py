"""F06 r2: CSV readers must not destroy a declared sentinel before the casting boundary.

A declared column keeps its exact lexeme (``NA`` stays ``"NA"``, an empty field stays ``""``)
on every CSV reader; undeclared columns keep the legacy NA vocabulary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from tests.acceptance._f06_support import INT64, column, config, run, write_csv

SENTINEL = {"id": "sentinel/exact/v1", "values": ["NA"]}


def _cfg(tmp_path: Path) -> dict[str, Any]:
    src = write_csv(
        tmp_path / "t.csv",
        ["id", "age", "note"],
        [["1", "NA", "NA"], ["2", "042", "x"], ["3", "", ""]],
    )
    return config(
        [
            column("id"),
            column(
                "age",
                logical_type="integer",
                parse_format=INT64,
                sentinel_to_null=SENTINEL,
                error_policy="null",
            ),
            column("note"),
        ],
        source_path=src,
        table="t",
    )


def test_declared_columns_are_derived_from_the_config(tmp_path: Path) -> None:
    from decoy_engine._lexeme import lexeme_columns_by_table
    from tests.acceptance._f06_support import validate

    assert lexeme_columns_by_table(validate(_cfg(tmp_path))) == {"t": frozenset({"age"})}


def test_local_csv_profile_reader_keeps_declared_lexemes(tmp_path: Path) -> None:
    from decoy_engine._lexeme import profile_descriptors
    from decoy_engine.profile._readers import build_profile_source
    from tests.acceptance._f06_support import validate

    cfg = validate(_cfg(tmp_path))
    frame = build_profile_source(profile_descriptors(cfg)["t"]).to_frame()
    assert frame["age"].tolist() == ["NA", "042", ""]  # lexemes, not NaN / int 42
    assert frame["note"].isna().tolist() == [True, False, True]  # legacy NA vocabulary intact


def test_profile_source_receives_the_contract_aware_descriptors(tmp_path: Path) -> None:
    from decoy_engine.profile import profile_source
    from tests.acceptance._f06_support import validate

    profile = profile_source(validate(_cfg(tmp_path)), seed=7)
    assert profile.tables[0].name == "t"


def test_bounded_reader_keeps_declared_lexemes(tmp_path: Path) -> None:
    from decoy_engine.io.bounded_reader import ChunkBudget, iter_csv_chunks

    path = tmp_path / "b.csv"
    path.write_text("age,note\nNA,NA\n042,x\n,\n", encoding="utf-8")
    budget = ChunkBudget(chunk_budget_bytes=1 << 20, max_chunk_rows=100, max_row_bytes=1 << 16)
    (chunk,) = list(
        iter_csv_chunks(str(path), budget, table="t", lexeme_columns=frozenset({"age"}))
    )
    assert chunk.column("age").to_pylist() == ["NA", "042", ""]
    assert chunk.column("note").to_pylist() == [None, "x", None]
    legacy = next(iter_csv_chunks(str(path), budget, table="t"))
    assert legacy.column("age").to_pylist() == [None, "042", None]  # undeclared: unchanged


@pytest.mark.parametrize("reader", ["s3", "gcs"])
def test_cloud_csv_readers_honour_lexeme_columns(reader: str) -> None:
    from decoy_engine.profile import _cloud_readers as cr

    window = b"age,note\nNA,NA\n042,x\n"
    frame = cr._csv_sample_from_window(window, len(window), 10, lexeme_columns=frozenset({"age"}))
    assert frame["age"].tolist() == ["NA", "042"]
    assert frame["note"].isna().tolist() == [True, False]
    assert reader in ("s3", "gcs")


def test_end_to_end_csv_sentinel_is_counted_not_a_source_null(tmp_path: Path) -> None:
    """The declared ``NA`` sentinel must reach F06 as ``NA``: one sentinel_to_null, and the
    empty field is a parse failure under ``null`` (empty is a sentinel only if listed)."""
    from decoy_engine._lexeme import lexeme_columns_by_table, profile_descriptors
    from decoy_engine.io.bounded_reader import ChunkBudget, iter_csv_chunks
    from tests.acceptance._f06_support import validate

    cfg = _cfg(tmp_path)
    lex = lexeme_columns_by_table(validate(cfg))
    budget = ChunkBudget(chunk_budget_bytes=1 << 20, max_chunk_rows=100, max_row_bytes=1 << 16)
    chunks = list(
        iter_csv_chunks(cfg["sources"]["t"]["path"], budget, table="t", lexeme_columns=lex["t"])
    )
    table = pa.concat_tables(chunks)
    result = run(cfg, {"t": table})
    counts = result.quality_metrics["declared_casting"]["by_table"]["t"]["by_column"]["age"]
    assert counts["sentinel_to_null"] == 1
    assert counts["parse_null"] == 1  # the empty field
    assert profile_descriptors(validate(cfg))["t"]["lexeme_columns"] == ["age"]
