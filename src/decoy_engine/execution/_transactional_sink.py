"""Transactional sink protocol and reference file-based implementation.

A TransactionalSink lets run_sequential publish tables all-or-nothing: each
table is written during the run, and either committed (atomically on success)
or aborted (everything discarded) on any exception.

The file-based reference implementation (ParquetTransactionalSink) uses the
established atomic-rename pattern: stage output in a private temp directory,
then publish each file to the target with os.replace (POSIX rename(2), which
is atomic within one filesystem, per POSIX.1-2008). On abort, the staging
directory is removed before any data reaches the target. This is the standard
technique for crash-safe file publication on POSIX systems; see GNU Coreutils
mv(1), POSIX.1-2008 rename(2), and Python os.replace docs.

Limitation: the set of renames in commit() is not a single atomic operation.
A process crash between two rename calls would leave a partial set of files
in the target. This is acceptable for the reference implementation because
decoy jobs are re-runnable (the run is idempotent with the same seed) and
the target directory is job-scoped. A crash-safe variant would add a commit
sentinel file as the final rename.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

import pyarrow as pa
import pyarrow.parquet as pq


@runtime_checkable
class TransactionalSink(Protocol):
    """All-or-nothing table sink protocol.

    run_sequential calls write() for each masked table in FK-topological order,
    then calls commit() on success or abort() on any exception. Implementations
    must be safe to call abort() even when no write() calls were made (e.g. the
    loader itself raises before the first table is processed).
    """

    def write(self, table: str, data: pa.Table) -> None:
        """Accept one masked table; called in FK-topological order."""
        ...

    def commit(self) -> None:
        """Finalize all written tables; called only on a successful run."""
        ...

    def abort(self) -> None:
        """Discard all written tables; called on any exception during the run."""
        ...


class _CallableSinkAdapter:
    """Wraps a plain Callable[[str, pa.Table], None] as a TransactionalSink.

    Preserves the pre-existing non-transactional contract: write() calls the
    callable immediately (so partial output on abort is the documented
    behavior). commit() and abort() are no-ops.
    """

    def __init__(self, fn: object) -> None:
        self._fn = fn

    def write(self, table: str, data: pa.Table) -> None:
        self._fn(table, data)  # type: ignore[operator]

    def commit(self) -> None:
        pass

    def abort(self) -> None:
        pass


class ParquetTransactionalSink:
    """File-based transactional sink using atomic POSIX rename.

    On write(), each masked table is serialized as Parquet to a private staging
    directory (a sibling of the target directory, so staging and target share a
    filesystem). On commit(), each staged file is atomically published to the
    target directory via os.replace (POSIX rename within one filesystem). On
    abort(), the staging directory is removed so nothing partial reaches the
    target.

    The staging directory name starts with ``_decoy_stage_`` so callers can
    identify and clean up orphaned directories if needed.

    Args:
        target_dir: Directory where committed Parquet files are published.
            Created on commit() if it does not exist.
    """

    def __init__(self, target_dir: Path) -> None:
        self._target = target_dir
        self._staging: Path | None = None
        self._staged: list[tuple[str, Path]] = []

    def write(self, table: str, data: pa.Table) -> None:
        """Stage one masked table as Parquet in the private staging directory."""
        if self._staging is None:
            # Staging lives in target.parent to guarantee same-filesystem rename.
            self._target.parent.mkdir(parents=True, exist_ok=True)
            self._staging = Path(tempfile.mkdtemp(prefix="_decoy_stage_", dir=self._target.parent))
        dest = self._staging / f"{table}.parquet"
        pq.write_table(data, dest)
        self._staged.append((table, dest))

    def commit(self) -> None:
        """Atomically publish all staged files to the target directory."""
        self._target.mkdir(parents=True, exist_ok=True)
        for _table, src in self._staged:
            dest = self._target / src.name
            os.replace(src, dest)
        self._cleanup_staging()

    def abort(self) -> None:
        """Remove all staged files so nothing partial reaches the target."""
        self._cleanup_staging()

    def _cleanup_staging(self) -> None:
        if self._staging is not None:
            shutil.rmtree(self._staging, ignore_errors=True)
        self._staging = None
        self._staged.clear()
