"""Acceptance test 0 (plan 2026-10-01-dispatcher-auto-chunk, revision 5): the
output-delta record between today's chunked lane, the dispatcher lane and the
forced full frame, for every planner-admitted source shape.

The recorded fixture (`auto_chunk_fixtures/output_delta.json`) holds each cell's
output column set, per-column type, field nullability, field-metadata presence
and schema-metadata keys, or the exception type and code. The test does two
things against the live code:

1. The live record equals the fixture, so a pyarrow or pandas upgrade that
   changes today's lane, or a B1 change, surfaces as a fixture diff for review.
2. Every cell where the dispatcher lane differs from today's lane is one of
   guarantee 3's differences (b) to (d), and nothing else. The set of allowed
   differences is closed: do not add to it without a new plan gate.

The dispatcher lane here is B1's `run_mask_chunked` called as plan Design 4
calls it (`_auto_chunk_support.b1_as_the_lane`), so this module passes before
and after the B2 implementation. Cells run with `use_byte_estimate_routing=False`
because default byte-estimate routing crashes on date/time columns on main (plan
Known issues); re-record them by review when that defect is fixed.

Regenerate the fixture with `DECOY_RECORD_OUTPUT_DELTA=1` in the companion venv.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from tests.unit.execution import _auto_chunk_matrix as matrix
from tests.unit.execution import _auto_chunk_support as support

FIXTURE = Path(__file__).parent / "auto_chunk_fixtures" / "output_delta.json"
STRING_OUTPUT_COLUMNS = ("h", "r")


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module", autouse=True)
def _maybe_record(tmp_path_factory: pytest.TempPathFactory) -> None:
    if os.environ.get("DECOY_RECORD_OUTPUT_DELTA") != "1":
        return
    mp = pytest.MonkeyPatch()
    path = str(tmp_path_factory.mktemp("record") / "src.parquet")
    record = {cell: matrix.record_cell(cell, mp, path) for cell in matrix.cell_ids()}
    FIXTURE.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")


def test_fixture_covers_exactly_the_matrix() -> None:
    assert sorted(_fixture()) == sorted(matrix.cell_ids())


def _check_masked_column(
    name: str, d: pa.Table, l_: pa.Table, *, string_output: bool, faker_native_all_null: bool
) -> None:
    d_field, l_field = d.schema.field(name), l_.schema.field(name)
    assert d.column(name).to_pylist() == l_.column(name).to_pylist(), f"{name}: values differ"
    assert d_field.nullable, f"{name}: masked field must be nullable"
    assert d_field.metadata is None, f"{name}: masked field must carry no field metadata"
    if string_output:
        # hash, truncate, redact: always `string`; today's lane leaves `null` only when
        # every chunk is all-null.
        assert d_field.type == pa.string()
        assert l_field.type in (pa.string(), pa.null())
    elif faker_native_all_null:
        assert d_field.type == pa.string()
        assert l_field.type in (pa.string(), pa.null())
    else:
        assert d_field.type == l_field.type, f"{name}: masked type differs from today's lane"


@pytest.mark.parametrize("route", ["native", "oracle"])
@pytest.mark.parametrize("cell", matrix.cell_ids())
def test_cell_matches_record_and_differences_are_guarantee_3(
    cell: str, route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if route == "native" and not support.COMPANION_PRESENT:
        pytest.skip("compiled companion not installed; the companion-present job covers this")
    src, lanes = matrix.run_cell(cell, monkeypatch, str(tmp_path / "src.parquet"))
    recorded = _fixture()[cell]
    for lane, (rec, _table) in lanes.items():
        if lane.startswith(matrix.LIVE_PREFIX):
            continue
        assert rec == recorded[lane], f"{cell}: live {lane} record differs from the fixture"
    # The shipped lane (no patch) equals the fixture lane: record and table.
    if f"disp_{route}" in lanes:
        live_rec, live = lanes[f"{matrix.LIVE_PREFIX}{route}"]
        fix_rec, fix = lanes[f"disp_{route}"]
        assert live_rec == fix_rec, f"{cell}: shipped lane record differs from disp_{route}"
        assert (live is None) == (fix is None)
        if live is not None and fix is not None:
            assert live.equals(fix, check_metadata=True), f"{cell}: shipped table differs"

    typ, role, nulls = cell.split("|")
    (l_rec, legacy), (d_rec, disp) = lanes["legacy"], lanes[f"disp_{route}"]
    if legacy is None or disp is None:
        # An exception on either lane must be the same exception on the other: a lane
        # change never turns a failure into a success or the reverse.
        assert legacy is None and disp is None
        assert l_rec == d_rec
        return
    assert l_rec["mode"] == "chunked", (
        f"{cell}: today's lane did not route; the cell proves nothing"
    )
    assert d_rec["mode"] == "chunked"

    # (a) same columns in the same order, same row count.
    assert disp.column_names == legacy.column_names
    assert disp.num_rows == legacy.num_rows == support.ROWS

    # (d) no schema metadata.
    assert disp.schema.metadata is None

    string_output_x = role in matrix.STRING_OUTPUT_ROLES
    faker_x = role == "faker"
    # Guarantee 3 (b)'s route-dependent cells: an all-null Faker source outputs `string` on the
    # dispatcher lane while today's lane leaves `null`. For a string source only the native leg
    # pins (C5b-i); C5c-ii pins a bool/int/uint source on BOTH dispatcher legs, so `bool` (the
    # only all-null numeric faker cell -- int/uint are non-nullable in this matrix) is pinned on
    # the oracle route too.
    faker_native_all_null = (
        faker_x
        and nulls == "all"
        and ((route == "native" and typ in ("string", "large_string")) or typ == "bool")
    )
    for name in disp.column_names:
        if name in STRING_OUTPUT_COLUMNS or (name == "x" and string_output_x):
            _check_masked_column(
                name, disp, legacy, string_output=True, faker_native_all_null=False
            )
        elif name == "x" and faker_x:
            _check_masked_column(
                name, disp, legacy, string_output=False, faker_native_all_null=faker_native_all_null
            )
        else:
            # (c) a passthrough column present in today's output equals the source exactly.
            assert disp.column(name).equals(src.column(name)), f"{name}: passthrough values/type"
            assert disp.schema.field(name).equals(src.schema.field(name), check_metadata=True)
