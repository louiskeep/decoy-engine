"""Task 4.4 C5: `ShadowSnapshot` identity + the read-only fixture guard."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.execution.physical._shadow_snapshot import (
    capture_shadow_snapshot,
    snapshot_identity,
)
from tests.physical._shadow_helpers import write_read_only_fixture


def test_identity_is_stable_across_repeated_calls_on_the_same_table() -> None:
    table = pa.table({"a": pa.array([1, None, 3], type=pa.int64())})
    assert snapshot_identity(table) == snapshot_identity(table)


def test_identity_changes_with_row_order() -> None:
    forward = pa.table({"a": pa.array(["x", "y", "z"], type=pa.string())})
    reversed_ = pa.table({"a": pa.array(["z", "y", "x"], type=pa.string())})
    assert snapshot_identity(forward) != snapshot_identity(reversed_)


def test_identity_changes_with_null_positions() -> None:
    a = pa.table({"a": pa.array(["x", None, "z"], type=pa.string())})
    b = pa.table({"a": pa.array([None, "x", "z"], type=pa.string())})
    assert snapshot_identity(a) != snapshot_identity(b)


def test_identity_changes_with_schema() -> None:
    a = pa.table({"a": pa.array([1, 2], type=pa.int64())})
    b = pa.table({"a": pa.array([1, 2], type=pa.int32())})
    assert snapshot_identity(a) != snapshot_identity(b)


def test_shadow_snapshot_identity_matches_direct_call() -> None:
    table = pa.table({"a": pa.array([1, 2, 3], type=pa.int64())})
    snapshot = capture_shadow_snapshot({"t": table})
    assert snapshot.identity("t") == snapshot_identity(table)


def test_shadow_snapshot_copies_the_mapping_not_the_caller_reference() -> None:
    table = pa.table({"a": pa.array([1], type=pa.int64())})
    caller_sources = {"t": table}
    snapshot = capture_shadow_snapshot(caller_sources)
    caller_sources["u"] = table
    assert set(snapshot.tables) == {"t"}


def test_fixture_is_written_then_made_read_only(tmp_path: Path) -> None:
    table = pa.table({"a": pa.array([1, 2], type=pa.int64())})
    path = write_read_only_fixture(tmp_path, table, "fixture")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert not (mode & stat.S_IWUSR), "fixture must not be user-writable after capture"
    with pytest.raises(PermissionError):
        path.write_bytes(b"corrupt")
