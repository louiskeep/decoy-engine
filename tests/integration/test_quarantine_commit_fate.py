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

Cross-model review regressions (Codex, re-landed fix): the first attempt at
this fix staged the quarantine JSONL BEFORE `_tsink.commit()`, which
introduced three new problems, each covered below:

  1. (BLOCKER) `TestNestedLayoutSinkAndQuarantineShareParentDirectory` --
     staging beside the final path before commit put a temp file inside the
     sink's OWN commit-target directory whenever the two share a parent (the
     natural layout: sink target `out/`, quarantine `out/quarantine.jsonl`),
     which made the sink's atomic directory `os.replace` fail closed with
     `ENOTEMPTY` and publish NOTHING.
  2. (MEDIUM) `TestAbortNotCalledAfterSuccessfulCommit` -- the except clause
     called `_tsink.abort()` unconditionally, even after a successful
     commit; a custom sink's abort() could delete already-committed tables.
  3. (MEDIUM) `TestCallableSinkWithSpecialQuarantinePath` -- a plain Callable
     sink (wrapped in `_CallableSinkAdapter`, making `_tsink is not None`)
     took the staged branch too, so a callable sink with a special
     quarantine path like `/dev/null` crashed trying to stage a temp file
     under `/dev`.

The fix reorders to commit-first: `_tsink.commit()` runs, THEN (only on
success) the quarantine JSONL is staged and published, and only for a
genuine `TransactionalSink` (not a plain-Callable-wrapped one).
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
    caller rather than being silently swallowed. Cross-model review (Codex)
    flagged the original fix's reliance on `ParquetTransactionalSink.abort()`
    being a harmless no-op when re-called post-commit as fragile -- a custom
    sink's abort() need not be idempotent. The reland tracks commit success
    with an explicit `_committed` flag and never calls `abort()` once commit
    has succeeded; see `TestAbortNotCalledAfterSuccessfulCommit` below for the
    direct proof.

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


# ---------------------------------------------------------------------------
# Codex finding #1 (BLOCKER): nested layout -- sink target and quarantine's
# parent directory are the SAME directory. Staging before commit put a temp
# file inside the sink's own commit target, so the sink's atomic directory
# os.replace failed closed with ENOTEMPTY and published NOTHING.
# ---------------------------------------------------------------------------


class TestNestedLayoutSinkAndQuarantineShareParentDirectory:
    """The natural layout: sink target `out/`, quarantine `out/quarantine.jsonl`.

    Before the reland fix, `run_sequential` staged the quarantine JSONL
    beside its final path BEFORE `_tsink.commit()` ran. With this layout that
    staging file landed inside `out/` itself -- the sink's own commit target
    -- making `out/` non-empty before the sink ever tried its atomic
    directory rename. `ParquetTransactionalSink.commit()`'s `os.replace`
    then failed with `OSError` (`ENOTEMPTY`), so the job raised and NOTHING
    was published: not the tables, not the quarantine sidecar.

    Fails pre-fix (ENOTEMPTY, no tables, no quarantine); passes post-fix
    (commit runs first -- `out/` doesn't exist as a real filesystem entry
    from the sink's perspective until its own successful rename -- so
    staging the quarantine JSONL beside it afterward cannot collide).
    """

    def test_nested_layout_completes_with_tables_and_sidecar_published(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "out"
        qpath = str(target / "quarantine.jsonl")
        config = _config(tmp_path, qpath)
        sources = _sources(config)
        sink = ParquetTransactionalSink(target)

        result = run_pipeline(config, sources, engine_version="0.1.0", sink=sink)
        assert result.quality_metrics["execution"]["execution_mode"] == "sequential"

        assert (target / "parent.parquet").exists()
        assert (target / "child.parquet").exists()

        assert Path(qpath).exists()
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["age"] == "badX"
        assert records[0]["_quarantine_trigger"] == "format_error"

        # No orphaned staging file left behind either.
        leftovers = list(target.glob("_decoy_quarantine_stage_*"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# Codex finding #3 (MEDIUM): a plain Callable sink is not a genuine
# TransactionalSink (its commit()/abort() are _CallableSinkAdapter no-ops),
# so it must never take the staged-quarantine branch. Before the reland fix,
# `_tsink is not None` was true for a callable sink too (wrapped in the
# adapter), so a special/non-directory quarantine path like `/dev/null`
# crashed attempting to create a temp file under `/dev` (no write
# permission there for a non-root user).
# ---------------------------------------------------------------------------


class TestCallableSinkWithSpecialQuarantinePath:
    def test_callable_sink_with_dev_null_quarantine_path_does_not_crash(
        self, tmp_path: Path
    ) -> None:
        """Fails pre-fix (PermissionError staging under /dev); passes
        post-fix (a plain Callable sink falls straight to the pre-existing
        direct _write_jsonl(final_path, ...) branch, never staged)."""
        config = _config(tmp_path, "/dev/null")
        sources = _sources(config)
        seen: dict[str, pa.Table] = {}

        def sink(table: str, data: pa.Table) -> None:
            seen[table] = data

        result = run_pipeline(config, sources, engine_version="0.1.0", sink=sink)

        assert set(seen) == {"parent", "child"}
        assert result.quality_metrics["quarantine"]["total_quarantined"] == 1


# ---------------------------------------------------------------------------
# Codex finding #2 (MEDIUM): abort() must never be called once commit() has
# already succeeded -- a custom sink's abort() could otherwise delete tables
# it has already durably committed.
# ---------------------------------------------------------------------------


class _AbortTrackingSink:
    """Wraps a real ParquetTransactionalSink; write()/commit() delegate to it
    unchanged, but abort() records whether it was ever called, so a test can
    assert the fix #2 contract directly rather than relying on
    ParquetTransactionalSink.abort() happening to be a harmless no-op when
    self._staging is already None post-commit."""

    def __init__(self, inner: ParquetTransactionalSink) -> None:
        self._inner = inner
        self.abort_called = False

    def write(self, table: str, data: pa.Table) -> None:
        self._inner.write(table, data)

    def commit(self) -> None:
        self._inner.commit()

    def abort(self) -> None:
        self.abort_called = True
        self._inner.abort()


class TestAbortNotCalledAfterSuccessfulCommit:
    def test_post_commit_publish_failure_does_not_call_abort(self, tmp_path: Path) -> None:
        """Reuses the EISDIR reproduction (publish_staged_jsonl's os.replace
        genuinely fails because the destination is a pre-existing directory)
        but asserts directly on whether abort() was invoked, rather than only
        on side effects. Fails pre-fix (abort() called unconditionally in the
        shared except clause); passes post-fix (`_committed` guard skips it)."""
        qpath_dir = tmp_path / "quarantine.jsonl"
        qpath_dir.mkdir()
        occupant = qpath_dir / "occupant.txt"
        occupant.write_text("pre-existing, not a quarantine file")
        qpath = str(qpath_dir)

        config = _config(tmp_path, qpath)
        sources = _sources(config)
        real = ParquetTransactionalSink(tmp_path / "out")
        sink = _AbortTrackingSink(real)

        with pytest.raises(OSError):
            run_pipeline(config, sources, engine_version="0.1.0", sink=sink)

        assert sink.abort_called is False, (
            "abort() must not run after a successful commit -- a custom "
            "sink's abort() could otherwise delete already-committed tables"
        )
        assert (tmp_path / "out" / "parent.parquet").exists()
        assert (tmp_path / "out" / "child.parquet").exists()


# ---------------------------------------------------------------------------
# Reorder proof: a sink whose commit() itself raises must leave no
# quarantine file anywhere -- not staged, not published -- because the whole
# quarantine write now happens strictly AFTER a successful commit.
# ---------------------------------------------------------------------------


class TestSinkCommitFailureWritesNoQuarantineAnywhere:
    def test_commit_failure_never_attempts_quarantine_staging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stronger than `test_commit_failure_leaves_no_quarantine_at_final_path`
        above: proves `quarantine.write_jsonl_staged` is never even called
        when the sink's own commit() fails, confirming the reorder (not just
        that a staged file happened to get discarded)."""
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(tmp_path, qpath)
        sources = _sources(config)
        real = ParquetTransactionalSink(tmp_path / "out")
        sink = _CommitBoomSink(real)

        from decoy_engine.execution import _sequential as seq_mod

        calls: list[str] = []
        original = seq_mod.write_jsonl_staged

        def _tracking(*args: object, **kwargs: object) -> Path:
            calls.append("called")
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(seq_mod, "write_jsonl_staged", _tracking)

        with pytest.raises(RuntimeError, match="commit boom"):
            run_pipeline(config, sources, engine_version="0.1.0", sink=sink)

        assert calls == [], (
            "write_jsonl_staged must not be called when the sink's own "
            "commit() fails -- the whole quarantine write is deferred until "
            "after a successful commit"
        )
        assert not (tmp_path / "out").exists()
        assert not Path(qpath).exists()
