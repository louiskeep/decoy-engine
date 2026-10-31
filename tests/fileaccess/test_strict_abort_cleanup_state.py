"""A strict-policy table sink's abort records an explicit cleanup state, not `unknown`.

The rooted stage has no path to look at and its discard swallows removal errors, so the engine
cannot claim `confirmed`; it ran the discard, so the state is `attempted` (F29's state for "cleanup
ran, outcome not confirmed"). The ambient sink keeps `confirmed`/`failed` from its stage path.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa

from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.reasons import CleanupState
from tests.fileaccess.support import RootRelative


def test_a_strict_abort_records_attempted_and_leaves_no_stage(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    policy = RootRelative(root)
    try:
        sink = ParquetTransactionalSink(str(root / "out"), file_access=policy)
        sink.write("t", pa.table({"a": [1, 2]}))
        assert sink.publication_evidence.cleanup is CleanupState.UNKNOWN  # staged, not cleaned
        sink.abort()
        assert sink.publication_evidence.cleanup is CleanupState.ATTEMPTED
        assert list(root.iterdir()) == []  # the discard did remove the stage
    finally:
        policy.close()


def test_an_ambient_abort_still_confirms_from_the_stage_path(tmp_path: Path) -> None:
    sink = ParquetTransactionalSink(str(tmp_path / "out"))
    sink.write("t", pa.table({"a": [1]}))
    sink.abort()
    assert sink.publication_evidence.cleanup is CleanupState.CONFIRMED
