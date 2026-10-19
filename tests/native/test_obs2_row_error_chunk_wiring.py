"""OBS-2c: the chunked legs attach a PARTIAL_BEFORE_ABORT summary to the escaping
`RowErrorsFailedError`, with the RUNNING row offset (not `base_row_offset`) and the loop's
chunk index. Both legs stop at the first error-bearing chunk."""

from __future__ import annotations

import json
from typing import Any

import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution._row_error_summary import RowErrorCompleteness
from tests.native._chunked_date_shift_support import (
    FORCE,
    date_value,
    ds_col,
    make_config,
    passthrough,
    run_outcome,
    source,
    with_force,
)
from tests.native._chunked_entry_support import NEEDS_COMPANION, force_oracle

_BAD = "not-a-date"


def _good(n: int, start: int = 0) -> list[str | None]:
    return [date_value(start + i) for i in range(n)]


def _legs(chunks: list[Any], **kw: Any) -> tuple[Any, Any]:
    columns = [ds_col(), passthrough("p")]
    native = run_outcome(make_config(columns), chunks, **kw)
    forced = run_outcome(
        make_config([*columns, force_oracle(FORCE)]), [with_force(c) for c in chunks], **kw
    )
    assert native.ev[0].native_admitted is True and forced.ev[0].native_admitted is False
    return native, forced


@NEEDS_COMPANION
@pytest.mark.parametrize("base_row_offset", [0, 7, 5_000])
def test_failing_later_chunk_carries_running_offset_not_base_offset(base_row_offset: int) -> None:
    # Unequal chunk lengths (3, 2, 4): the failing chunk is index 1, starting at base + 3.
    chunks = [source(_good(3)), source([date_value(1), _BAD]), source(_good(4))]
    native, forced = _legs(chunks, base_row_offset=base_row_offset)
    for leg in (native, forced):
        err = leg.error
        assert isinstance(err, RowErrorsFailedError)
        s = err.summary
        assert s is not None
        assert s.completeness is RowErrorCompleteness.PARTIAL_BEFORE_ABORT
        assert (s.total_observed, s.covered, s.uncovered) == (1, 0, 1)
        item = s.sample[0]
        assert (item.column, item.trigger, item.row_index) == ("d", "format_error", 1)
        assert item.chunk_index == 1
        assert item.chunk_row_offset == base_row_offset + 3
        assert err.summaries_by_table is None
    assert native.error.summary == forced.error.summary


@NEEDS_COMPANION
def test_failure_in_the_first_chunk_and_hostile_reason_absent() -> None:
    native, forced = _legs([source([_BAD, date_value(1)]), source(_good(2))], base_row_offset=9)
    for leg in (native, forced):
        s = leg.error.summary
        assert s.sample[0].chunk_index == 0 and s.sample[0].chunk_row_offset == 9
        blob = json.dumps(s.to_dict())
        for rec in leg.error.records:
            assert rec.reason not in blob
        assert set(s.to_dict()) == {
            "table",
            "by_column",
            "total_observed",
            "covered",
            "uncovered",
            "sample",
            "sample_limit",
            "omitted",
            "completeness",
        }
