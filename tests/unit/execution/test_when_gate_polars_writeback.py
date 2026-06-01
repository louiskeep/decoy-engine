"""QA-3 F13 (2026-05-31): polars when_gate writeback survives handler reorder.

Pre-fix `run_with_when_gate_polars` used `sub_pdf[column].values` to copy
the handler-returned subset back into the original frame's masked rows
positionally. That assumed the handler preserved subset row order. No
current handler reorders, but the contract is implicit; this cell pins
the safety net so a future polars-native handler that internally sorts
cannot silently misalign the writeback.

We construct a fake handler that DELIBERATELY reverses the subset's
row order, then verify the writeback still lands on the right
destination rows in the original frame.
"""

from __future__ import annotations

import polars as pl

from decoy_engine.execution._when_gate import run_with_when_gate_polars
from decoy_engine.plan._types import ColumnSeed


class _ReversingHandler:
    """Reverses the subset's rows in-place, mutates one column.

    Used to verify that the when_gate carries the right positional
    anchor across handler calls. If the writeback in _when_gate were
    naive (column-values positional copy), this would scramble the
    output.
    """

    name = "reversing_redact"

    def run(self, frame: pl.DataFrame, column: str, plan: ColumnSeed, ctx) -> tuple[pl.DataFrame, list]:
        reversed_frame = frame.reverse()
        reversed_frame = reversed_frame.with_columns(
            pl.lit("REDACTED").alias(column)
        )
        return reversed_frame, []


class _FakeCtx:
    pass


def _seed(when: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="reversing_redact",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(),
        when=when,
    )


def test_when_gate_polars_writeback_label_aligned_under_reorder():
    frame = pl.DataFrame(
        {
            "id": [10, 20, 30, 40],
            "score": [5, 15, 25, 35],
            "v": ["a", "b", "c", "d"],
        }
    )
    # Mask matches rows 1 + 2 + 3 (score > 10); row 0 falls out.
    out, _ = run_with_when_gate_polars(
        _ReversingHandler(),
        frame,
        "v",
        _seed(when="score > 10"),
        _FakeCtx(),
    )
    # The "REDACTED" writes must land on rows 1, 2, 3 (the originally
    # mask-true rows) regardless of how the handler reordered them.
    v = out["v"].to_list()
    assert v[0] == "a"  # row 0 untouched (mask was False)
    assert v[1] == "REDACTED"
    assert v[2] == "REDACTED"
    assert v[3] == "REDACTED"
    # Other columns must be untouched.
    assert out["id"].to_list() == [10, 20, 30, 40]
    assert out["score"].to_list() == [5, 15, 25, 35]
