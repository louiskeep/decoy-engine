"""Staging, atomic-commit, and result-envelope IO for the isolated-run primitive.

Split out of `_isolated_run.py` (which owns spawn/classify orchestration) to
stay under the ~600 LOC orchestration cap (CLAUDE.md, `graph/runner.py`
threshold) -- these are pure IO/filesystem helpers with no subprocess
lifecycle logic of their own, shared by both the isolated (subprocess) path
and the `isolate=False` in-process fallback so the two modes commit with
identical discipline (dennis review HIGH-2).

Same same-parent-directory staging + single-rename discipline
`ParquetTransactionalSink.commit` uses, reused here rather than re-derived
(spec §12 ruling 3).
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.errors import DecoyError
from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.scratch import ArenaLike, PublicationStage, ensure_publication_stage

__all__ = [
    "CommitError",
    "atomic_commit",
    "read_envelope",
    "read_staged_row_errors",
    "resolve_staging_output_dir",
    "sibling_staging_dir",
    "signal_name",
    "stage_tables",
]

_STAGING_DIR_PREFIX = "_decoy_isolated_stage_"


class CommitError(DecoyError):
    """The atomic staging -> output_dir rename failed (dennis review MED-1).

    Raised by `atomic_commit` so both callers (the isolated path's commit
    step and the in-process fallback's own commit step) can catch it and
    turn a raw `OSError` -- most commonly a pre-existing non-empty target,
    which `os.replace` on a directory refuses with ENOTEMPTY -- into a
    classified `crashed` `IsolatedRunResult` instead of letting it escape as
    an unhandled exception with staging orphaned outside `work_root`.
    """


def sibling_staging_dir(
    output_dir: Path, *, scratch_arena: ArenaLike | None = None
) -> tuple[Path, PublicationStage | None]:
    """A scratch dir in `output_dir`'s own parent (same filesystem), so the
    commit's `os.replace` is a single atomic rename, never a cross-device
    copy -- the same discipline `ParquetTransactionalSink.commit` uses.

    F25 T12: with an injected arena this is a registered destination-side
    publication stage; without one it is the same owned `_decoy_isolated_stage_*`
    sibling directory as before. Returns `(stage_dir, capability)`; the caller
    releases the capability when the staged output is discarded.
    """
    stage_path, cap = ensure_publication_stage(
        scratch_arena,
        output_dir,
        retention_owner="isolated_commit",
        fallback_prefix=_STAGING_DIR_PREFIX,
    )
    return Path(stage_path), cap


def stage_tables(outputs: dict[str, pa.Table], staging_dir: Path) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)
    for table, data in outputs.items():
        pq.write_table(  # type: ignore[no-untyped-call, unused-ignore]
            data, staging_dir / f"{table}.parquet"
        )


def atomic_commit(staging_dir: Path, output_dir: Path) -> None:
    """Single-rename commit (spec §12 ruling 3), wrapped per MED-1: a
    pre-existing non-empty target makes `os.replace` raise (ENOTEMPTY on
    Linux); that must surface as a classified failure, not propagate as a
    raw `OSError` that also leaves staging orphaned outside `work_root`.
    """
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(staging_dir, output_dir)
    except OSError as exc:
        raise CommitError(
            f"could not commit staged output to {output_dir} (a pre-existing "
            f"non-empty target is the common cause): {exc}"
        ) from exc


def resolve_staging_output_dir(
    work_root: Path, output_dir: Path | None, *, scratch_arena: ArenaLike | None = None
) -> tuple[Path, PublicationStage | None]:
    """Where the child stages outputs before commit.

    When `output_dir` is given, staging must share its filesystem so the
    commit step's `os.replace` is a single atomic rename (never a copy) --
    the same same-parent-directory discipline `ParquetTransactionalSink`
    uses for exactly this reason. Without an `output_dir` there is nothing
    to rename into, so staging just lives under this run's own scratch root
    (already registered when the run opened the work root, F25 T11/T12).

    Returns `(staging_dir, capability)`; the capability is `None` when staging
    lives under the already-registered work root.
    """
    if output_dir is not None:
        return sibling_staging_dir(output_dir, scratch_arena=scratch_arena)
    return work_root / "staging_output", None


def read_staged_row_errors(staging_output_dir: Path) -> tuple[RowErrorRecord, ...]:
    """Read back `row_errors.json` staged by the worker (dennis review MED-4).

    Missing (the common case: no row errors this run) or unreadable/corrupt
    both degrade to an empty tuple rather than failing the whole commit --
    row_errors is additive telemetry, not load-bearing for `outputs`.
    """
    path = staging_output_dir / "row_errors.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    return tuple(RowErrorRecord(**item) for item in raw)


def read_envelope(result_path: Path) -> dict[str, Any] | None:
    """Read the worker's result envelope from `result_path` (dennis review HIGH-1).

    Missing (the child died before writing it -- a harder rlimit trip, an
    external SIGKILL) or corrupt (a partial write raced with a kill) both
    return `None` rather than raising, so the caller falls through to
    `classify_abnormal_exit`'s outside classification exactly as it did for
    the old "no parseable last stdout line" case (MED-5's missing/corrupt
    envelope coverage).
    """
    try:
        text = result_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        envelope: dict[str, Any] = json.loads(text)
        return envelope
    except json.JSONDecodeError:
        return None


def signal_name(signal_number: int | None) -> str | None:
    """`signal.Signals(n).name`, guarded (dennis review LOW-3).

    Real-time signal NUMBERS (e.g. SIGRTMIN+5) are valid `os.kill` targets
    but most have no individual `signal.Signals` enum member -- the
    constructor raises `ValueError` for them. That must not turn a clean
    abnormal-exit classification into an unhandled driver exception; fall
    back to a bare numeric label instead.
    """
    if not signal_number:
        return None
    try:
        return signal.Signals(signal_number).name
    except ValueError:
        return f"signal {signal_number}"
