"""DE-08 (HIGH data-safety finding): the quarantine JSONL sidecar must share
`TransactionalSink`'s commit-or-discard fate on the sequential FK path.

Before the fix, `run_sequential` (`src/decoy_engine/execution/_sequential.py`,
around lines 369-408 pre-fix) wrote the final quarantine JSONL straight to its
FINAL `output_path` (via `quarantine._write_jsonl`) BEFORE / independently of
`_tsink.commit()`. If the sink's commit() raised, table staging was correctly
discarded (`_tsink.abort()`), but the quarantine sidecar -- which contains raw,
uncoercible pre-mask cell values by definition (see `quarantine.py` module
docstring) -- had already been published and was left behind. That is raw PII
published outside the commit protocol.

Reproduces the exact FK shape from
`tests/integration/test_fk_sequential_row_error_leak.py` (a parent/child pair,
one uncoercible `bucketize` cell "badX" on the parent, `format_error` covered
by an enabled quarantine trigger), routed through `run_pipeline`'s
auto-eligible sequential path, but wraps a real `ParquetTransactionalSink`
with a fake whose `commit()` always raises -- simulating a late sink-side
commit failure (disk full, permission denied, etc) unrelated to the masking
itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._pipeline import run_pipeline

_AGE = ["10", "20", "30", "40", "50", "badX"]
_IDS = ["p0", "p1", "p2", "p3", "p4", "p5"]


class _CommitBoomSink:
    """Wraps a real `ParquetTransactionalSink`; `write()`/`abort()` delegate
    to it unchanged, but `commit()` always raises -- simulating a late
    sink-side commit failure (e.g. disk full) after every table has already
    staged successfully. Satisfies `_has_transactional_write_contract`
    (write/commit/abort callables) so `run_sequential` dispatches it as a
    transactional sink, not the non-transactional `_CallableSinkAdapter`."""

    def __init__(self, inner: ParquetTransactionalSink) -> None:
        self._inner = inner

    def write(self, table: str, data: pa.Table) -> None:
        self._inner.write(table, data)

    def commit(self) -> None:
        raise RuntimeError("commit boom")

    def abort(self) -> None:
        self._inner.abort()


def _faker_col(name: str, namespace: str) -> dict[str, Any]:
    return {
        "name": name,
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": namespace,
    }


def _write_source(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _config(tmp_path: Path, qpath: str) -> dict[str, Any]:
    parent = pa.table(
        {
            "id": pa.array(_IDS, type=pa.string()),
            "age": pa.array(_AGE, type=pa.string()),
        }
    )
    child = pa.table(
        {
            "id": pa.array([f"c{i}" for i in range(len(_IDS))], type=pa.string()),
            "parent_id": pa.array(_IDS, type=pa.string()),
        }
    )
    parent_src = _write_source(tmp_path, parent, "parent")
    child_src = _write_source(tmp_path, child, "child")

    return {
        "version": 1,
        "global_settings": {"job_name": "de08-quarantine-commit-fate", "seed": 42},
        "sources": {
            "parent": {"type": "file", "path": parent_src, "format": "parquet"},
            "child": {"type": "file", "path": child_src, "format": "parquet"},
        },
        "targets": {
            "parent": {
                "type": "file",
                "path": str(tmp_path / "parent.out.parquet"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": str(tmp_path / "child.out.parquet"),
                "format": "parquet",
            },
        },
        "tables": [
            {
                "name": "parent",
                "columns": [
                    _faker_col("id", "parent_ns"),
                    {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
                ],
            },
            {"name": "child", "columns": [_faker_col("parent_id", "parent_ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "parent_ns",
            }
        ],
        "quarantine": {"enabled": True, "output_path": qpath, "triggers": ["format_error"]},
    }


def _sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


class TestQuarantinePublishedOnlyOnSinkCommit:
    """DE-08: the quarantine sidecar shares the transactional sink's
    commit-or-discard fate on the sequential (`run_pipeline` auto-routed
    FK) path."""

    def test_commit_failure_leaves_no_quarantine_at_final_path(self, tmp_path: Path) -> None:
        """THE reproduction: a sink whose commit() raises must leave NO
        quarantine file at the final output_path. Fails pre-fix (the file
        was written to `qpath` before `_tsink.commit()` ran and nothing
        removed it on abort); passes post-fix."""
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(tmp_path, qpath)
        sources = _sources(config)
        real = ParquetTransactionalSink(tmp_path / "out")
        sink = _CommitBoomSink(real)

        with pytest.raises(RuntimeError, match="commit boom"):
            run_pipeline(config, sources, engine_version="0.1.0", sink=sink)

        # Table staging is discarded (abort() ran; nothing published)...
        assert not (tmp_path / "out").exists()
        # ...and the quarantine sidecar -- which carries the raw "badX" cell
        # -- must NOT be published either: it shares the sink's abort fate.
        assert not Path(qpath).exists()
        # No orphaned staging file left behind either (best-effort discard).
        leftovers = list(tmp_path.glob("_decoy_quarantine_stage_*"))
        assert leftovers == []

    def test_successful_commit_still_publishes_quarantine(self, tmp_path: Path) -> None:
        """Unchanged happy-path contract: a run whose sink commits
        successfully still publishes the quarantine JSONL at the final path,
        exactly as before this fix."""
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(tmp_path, qpath)
        sources = _sources(config)
        sink = ParquetTransactionalSink(tmp_path / "out")

        result = run_pipeline(config, sources, engine_version="0.1.0", sink=sink)
        assert result.quality_metrics["execution"]["execution_mode"] == "sequential"

        out_age = pq.read_table(tmp_path / "out" / "parent.parquet").column("age").to_pylist()
        assert "badX" not in out_age

        assert Path(qpath).exists()
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["age"] == "badX"
        assert records[0]["_quarantine_trigger"] == "format_error"


class TestQuarantinePublishReplaceFailureAfterCommit:
    """DE-08 residual (dennis): the post-commit publish step itself --
    `publish_staged_jsonl`'s `os.replace` -- can fail (disk full, permission
    denied, ...), distinct from the sink's own `commit()` failing. By the time
    this runs, `_tsink.commit()` has ALREADY succeeded: the masked tables are
    legitimately, correctly published. Only the raw-PII quarantine sidecar's
    publish is in flight. This must still err safe: no raw quarantine file at
    the final path, no orphaned staging file, and the failure surfaces to the
    caller rather than being silently swallowed -- `ParquetTransactionalSink
    .abort()` being called again post-commit (the shared `except BaseException`
    handler doesn't know publish happened after commit) must be a harmless
    no-op, not a re-abort of already-published tables.

    Reproduced with a real OS-level `os.replace` failure -- no mocking of the
    replace call itself -- by pointing `output_path` at a path that is already
    an existing directory: POSIX rename(2) raises EISDIR when the source is a
    regular file and the destination is a directory, so
    `publish_staged_jsonl`'s single `os.replace` genuinely fails.
    """

    def test_replace_failure_after_commit_leaves_no_quarantine_and_no_orphan(
        self, tmp_path: Path
    ) -> None:
        # A pre-existing directory (with an occupant file, to prove it's left
        # completely untouched) sits at the quarantine output path. A file
        # can never be os.replace'd onto an existing directory on POSIX, so
        # this forces publish_staged_jsonl's os.replace to raise for real.
        qpath_dir = tmp_path / "quarantine.jsonl"
        qpath_dir.mkdir()
        occupant = qpath_dir / "occupant.txt"
        occupant.write_text("pre-existing, not a quarantine file")
        qpath = str(qpath_dir)

        config = _config(tmp_path, qpath)
        sources = _sources(config)
        sink = ParquetTransactionalSink(tmp_path / "out")

        with pytest.raises(OSError):
            run_pipeline(config, sources, engine_version="0.1.0", sink=sink)

        # The sink's own commit already succeeded before the quarantine
        # publish step ran: the masked tables ARE legitimately published.
        # That is correct -- they contain masked data, not raw PII -- and
        # there is no protocol under which un-publishing them would be safer
        # (the os.replace that published them already completed).
        assert (tmp_path / "out" / "parent.parquet").exists()
        assert (tmp_path / "out" / "child.parquet").exists()

        # The raw-PII quarantine sidecar must NOT have landed at the final
        # path -- proof the failed os.replace did not partially clobber
        # anything: the pre-existing directory and its occupant are intact.
        assert qpath_dir.is_dir()
        assert occupant.read_text() == "pre-existing, not a quarantine file"
        assert list(qpath_dir.iterdir()) == [occupant]

        # No orphaned staging file left behind: discard_staged_jsonl ran
        # (best-effort, via the shared except clause) even though this
        # exception originated AFTER _tsink.commit() had already succeeded.
        leftovers = list(tmp_path.glob("_decoy_quarantine_stage_*"))
        assert leftovers == []
