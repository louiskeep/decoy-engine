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
sequential path (pinned via `use_byte_estimate_routing=False`, the TB-5
rollback route, so the tiny fixtures stay sequential rather than fitting
full_frame under the default byte estimate), but wraps a real
`ParquetTransactionalSink`
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

import errno
import json
import logging
import os
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import quarantine as _quarantine
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
            run_pipeline(
                config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
            )

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

        result = run_pipeline(
            config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
        )
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
    `publish_staged_jsonl`'s exclusive-create publish -- can fail (a
    pre-existing file at the final path, disk full, permission denied, ...),
    distinct from the sink's own `commit()` failing. By the time this runs,
    `_tsink.commit()` has ALREADY succeeded: the masked tables are
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

    Reproduced with a real OS-level failure -- no mocking of the publish call
    itself -- by pointing `output_path` at a path that is already an existing
    directory: `publish_staged_jsonl`'s `os.link` (dennis re-gate HIGH #2:
    exclusive-create publish, closing the duck-typed-sink gap the name-based
    alias guard could not) raises `FileExistsError` because the destination
    already exists, which `publish_staged_jsonl` converts to a loud
    `ValueError` -- never silently overwritten, and never a bare `os.replace`
    EISDIR anymore.
    """

    def test_replace_failure_after_commit_leaves_no_quarantine_and_no_orphan(
        self, tmp_path: Path
    ) -> None:
        # A pre-existing directory (with an occupant file, to prove it's left
        # completely untouched) sits at the quarantine output path.
        # os.link refuses to create a link at a path that already exists
        # (FileExistsError) regardless of whether the occupant is a file or a
        # directory, so this forces publish_staged_jsonl's exclusive-create
        # to raise for real.
        qpath_dir = tmp_path / "quarantine.jsonl"
        qpath_dir.mkdir()
        occupant = qpath_dir / "occupant.txt"
        occupant.write_text("pre-existing, not a quarantine file")
        qpath = str(qpath_dir)

        config = _config(tmp_path, qpath)
        sources = _sources(config)
        sink = ParquetTransactionalSink(tmp_path / "out")

        with pytest.raises(ValueError, match="refusing to publish quarantine"):
            run_pipeline(
                config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
            )

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

        result = run_pipeline(
            config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
        )
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

        result = run_pipeline(
            config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
        )

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
        """Reuses the exclusive-create-refusal reproduction
        (publish_staged_jsonl's os.link genuinely fails with FileExistsError,
        converted to ValueError, because the destination is a pre-existing
        directory) but asserts directly on whether abort() was invoked,
        rather than only on side effects. Fails pre-fix (abort() called
        unconditionally in the shared except clause); passes post-fix
        (`_committed` guard skips it)."""
        qpath_dir = tmp_path / "quarantine.jsonl"
        qpath_dir.mkdir()
        occupant = qpath_dir / "occupant.txt"
        occupant.write_text("pre-existing, not a quarantine file")
        qpath = str(qpath_dir)

        config = _config(tmp_path, qpath)
        sources = _sources(config)
        real = ParquetTransactionalSink(tmp_path / "out")
        sink = _AbortTrackingSink(real)

        with pytest.raises(ValueError, match="refusing to publish quarantine"):
            run_pipeline(
                config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
            )

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


# ---------------------------------------------------------------------------
# Dennis re-gate (HIGH #1): the commit-first reorder above closed the
# ordering hazard but opened a new one -- `publish_staged_jsonl`'s
# unconditional `os.replace` has no idea `quarantine.output_path` might alias
# a table artifact this same sink just durably committed. Aliasing must
# raise loudly (as the pre-reorder `origin/main` code did via ENOTEMPTY), not
# silently overwrite the masked table with raw JSONL.
# ---------------------------------------------------------------------------


class TestQuarantineOutputPathAliasingCommittedTable:
    def test_aliasing_committed_parquet_raises_and_leaves_table_intact(
        self, tmp_path: Path
    ) -> None:
        """`quarantine.output_path` set to the exact path
        `ParquetTransactionalSink` just committed `parent` to must raise
        instead of silently clobbering the masked table with raw JSONL.
        Fails pre-fix (silent `os.replace` overwrite; the run reports
        success with `parent.parquet` now raw-PII JSONL); passes post-fix
        (`ValueError`, masked `parent.parquet` untouched)."""
        target = tmp_path / "out"
        qpath = str(target / "parent.parquet")  # aliases the committed table
        config = _config(tmp_path, qpath)
        sources = _sources(config)
        sink = ParquetTransactionalSink(target)

        with pytest.raises(ValueError, match="aliases the output artifact"):
            run_pipeline(
                config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
            )

        # The sink's own commit already succeeded (tables ARE legitimately
        # published) before the alias guard runs; the guard raises before any
        # staging/publish is attempted for the quarantine sidecar, so the
        # masked table is untouched -- still valid masked Parquet, not raw
        # JSONL, and the raw "badX" cell never appears in it.
        parent_out = pq.read_table(target / "parent.parquet")
        assert "badX" not in parent_out.column("age").to_pylist()
        assert (target / "child.parquet").exists()

        # No orphaned staging file for the quarantine sidecar either -- the
        # guard raises before `write_jsonl_staged` is ever called.
        leftovers = list(target.glob("_decoy_quarantine_stage_*"))
        assert leftovers == []

    def test_natural_layout_inside_target_dir_still_completes(self, tmp_path: Path) -> None:
        """Sanity companion (also covered by
        `TestNestedLayoutSinkAndQuarantineShareParentDirectory` above): the
        natural, legitimate layout -- quarantine.jsonl living INSIDE the
        sink's own target directory, under its own name, not a table's name
        -- must NOT be treated as an alias and must still complete. Only
        aliasing an actual committed TABLE artifact is refused, never "any
        path inside the target dir"."""
        target = tmp_path / "out"
        qpath = str(target / "quarantine.jsonl")
        config = _config(tmp_path, qpath)
        sources = _sources(config)
        sink = ParquetTransactionalSink(target)

        result = run_pipeline(
            config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
        )
        assert result.quality_metrics["execution"]["execution_mode"] == "sequential"
        assert (target / "parent.parquet").exists()
        assert (target / "child.parquet").exists()
        assert Path(qpath).exists()


class TestSinkCommitFailureWritesNoQuarantineAnywhere:
    def test_commit_failure_never_attempts_quarantine_staging(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stronger than `test_commit_failure_leaves_no_quarantine_at_final_path`
        above: proves `quarantine.write_jsonl_staged` is never even called
        when the sink's own commit() fails, confirming the reorder (not just
        that a staged file happened to get discarded). Patches the name in
        `decoy_engine.quarantine` (where `finalize_committed_quarantine`,
        `run_sequential`'s post-commit publish helper, actually calls it from),
        not in `_sequential`, which no longer imports it directly."""
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(tmp_path, qpath)
        sources = _sources(config)
        real = ParquetTransactionalSink(tmp_path / "out")
        sink = _CommitBoomSink(real)

        from decoy_engine import quarantine as quarantine_mod

        calls: list[str] = []
        original = quarantine_mod.write_jsonl_staged

        def _tracking(*args: object, **kwargs: object) -> Path:
            calls.append("called")
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(quarantine_mod, "write_jsonl_staged", _tracking)

        with pytest.raises(RuntimeError, match="commit boom"):
            run_pipeline(
                config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
            )

        assert calls == [], (
            "write_jsonl_staged must not be called when the sink's own "
            "commit() fails -- the whole quarantine write is deferred until "
            "after a successful commit"
        )
        assert not (tmp_path / "out").exists()
        assert not Path(qpath).exists()


# ---------------------------------------------------------------------------
# Dennis + Codex re-gate (final HIGH): `guard_quarantine_not_aliasing_
# committed_table` only fires for sinks that expose `committed_table_path`
# (ParquetTransactionalSink does). A duck-typed TransactionalSink --
# write/commit/abort present, but NOT `committed_table_path` -- routes
# through this same staged-publish path with the guard silently no-op'ing
# (`getattr(sink, "committed_table_path", None)` returns None), so the
# *unconditional* `os.replace` overwrote a just-committed masked table with
# raw-PII JSONL while the run still reported SUCCESS. The root fix makes
# `publish_staged_jsonl` itself exclusive-create (`os.link`), closing this
# uniformly for Parquet, duck-typed, and arbitrary custom sinks alike --
# the earlier name-based guard is now only a friendlier, earlier error for
# the one sink shape that can support it, not the only line of defense.
# ---------------------------------------------------------------------------


class _DuckTypedTransactionalSink:
    """Wraps a real ParquetTransactionalSink; delegates write/commit/abort,
    but deliberately does NOT expose `committed_table_path` -- the shape of
    an arbitrary custom TransactionalSink implementation that satisfies the
    write/commit/abort protocol without also advertising its committed
    table paths. `guard_quarantine_not_aliasing_committed_table`'s
    `getattr(sink, "committed_table_path", None)` returns None for this
    sink, so that guard is a no-op here by design -- the exclusive-create
    publish in `publish_staged_jsonl` is the only remaining line of defense."""

    def __init__(self, inner: ParquetTransactionalSink) -> None:
        self._inner = inner

    def write(self, table: str, data: pa.Table) -> None:
        self._inner.write(table, data)

    def commit(self) -> None:
        self._inner.commit()

    def abort(self) -> None:
        self._inner.abort()


class TestDuckTypedSinkAliasingCommittedTable:
    def test_duck_typed_sink_aliasing_raises_and_leaves_table_intact(self, tmp_path: Path) -> None:
        """`quarantine.output_path` set to the exact path a duck-typed
        TransactionalSink (no `committed_table_path`) just committed `parent`
        to must raise instead of silently clobbering the masked table with
        raw JSONL. The name-based alias guard cannot see this sink at all (no
        `committed_table_path` to call), so a pass here proves the
        exclusive-create publish itself is load-bearing, not just the
        earlier friendlier guard. Fails pre-fix: `os.replace` silently
        overwrites `parent.parquet` with raw quarantine JSONL and the run
        reports SUCCESS (verified separately against the pre-fix source);
        passes post-fix (`ValueError`, masked `parent.parquet` untouched)."""
        target = tmp_path / "out"
        qpath = str(target / "parent.parquet")  # aliases the committed table
        config = _config(tmp_path, qpath)
        sources = _sources(config)
        real = ParquetTransactionalSink(target)
        sink = _DuckTypedTransactionalSink(real)
        assert not hasattr(sink, "committed_table_path")

        with pytest.raises(ValueError, match="refusing to publish quarantine"):
            run_pipeline(
                config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
            )

        # The sink's own commit already succeeded (tables ARE legitimately
        # published) before the exclusive-create publish runs; the masked
        # table must be untouched -- still valid masked Parquet, not raw
        # JSONL, and the raw "badX" cell never appears in it.
        parent_out = pq.read_table(target / "parent.parquet")
        assert "badX" not in parent_out.column("age").to_pylist()
        assert (target / "child.parquet").exists()

        # No orphaned staging file for the quarantine sidecar either.
        leftovers = list(target.glob("_decoy_quarantine_stage_*"))
        assert leftovers == []


class TestQuarantineOutputPathPreExistingNonAliasingFile:
    def test_pre_existing_file_at_output_path_raises_and_is_not_overwritten(
        self, tmp_path: Path
    ) -> None:
        """A plain pre-existing file sits at `quarantine.output_path` that
        does NOT alias any table this run committed (so the name-based alias
        guard would never catch it even for a ParquetTransactionalSink --
        it only checks table paths, not arbitrary pre-existing files). The
        exclusive-create publish must still refuse to overwrite it rather
        than silently clobbering it, and the staged temp file must be
        discarded (no orphan)."""
        target = tmp_path / "out"
        qpath_file = tmp_path / "quarantine.jsonl"
        qpath_file.write_text("pre-existing content, not a quarantine record")
        qpath = str(qpath_file)

        config = _config(tmp_path, qpath)
        sources = _sources(config)
        sink = ParquetTransactionalSink(target)

        with pytest.raises(ValueError, match="refusing to publish quarantine"):
            run_pipeline(
                config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
            )

        # Untouched: still the original content, not raw quarantine JSONL.
        assert qpath_file.read_text() == "pre-existing content, not a quarantine record"

        # The sink's own commit already succeeded -- masked tables published.
        assert (target / "parent.parquet").exists()
        assert (target / "child.parquet").exists()

        # No orphaned staging file left behind (best-effort discard fired).
        leftovers = list(tmp_path.glob("_decoy_quarantine_stage_*")) + list(
            target.glob("_decoy_quarantine_stage_*")
        )
        assert leftovers == []


# ---------------------------------------------------------------------------
# DE-08 LOW (a): the post-link staging `unlink` is best-effort. A failure
# there -- the hardlink already succeeded, so the sidecar IS durably published
# (final path and stage share one inode) -- must NOT be reported as a
# publish/run failure. Before the fix the bare `os.unlink(staged_path)` could
# raise straight out of `publish_staged_jsonl`, and
# `finalize_committed_quarantine`'s catch-all re-raised it, crashing an already
# fully-successful run. Now it is logged and swallowed, mirroring the swallowed
# best-effort cleanup elsewhere in the module.
# ---------------------------------------------------------------------------


class TestPostLinkUnlinkFailureDoesNotFailTheRun:
    def test_staging_unlink_failure_after_successful_link_is_best_effort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The hardlink publish succeeds, then removing the extra staging link
        fails (e.g. a race, or a read-only staging dir). The run must still
        succeed: the sidecar is intact at the final path, a warning is logged,
        and no exception propagates."""
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(tmp_path, qpath)
        sources = _sources(config)
        sink = ParquetTransactionalSink(tmp_path / "out")

        real_unlink = os.unlink

        def _flaky_unlink(path: object, *args: object, **kwargs: object) -> None:
            # Fail only the post-link cleanup of the staging file; leave every
            # other unlink (there are none on the success path here) untouched.
            if "_decoy_quarantine_stage_" in str(path):
                raise OSError(errno.EACCES, "staging dir is read-only")
            real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(_quarantine.os, "unlink", _flaky_unlink)

        with caplog.at_level(logging.WARNING, logger="decoy_engine.quarantine"):
            result = run_pipeline(
                config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
            )

        # Run succeeded and recorded the quarantine sidecar in its manifest.
        assert result.quality_metrics["execution"]["execution_mode"] == "sequential"

        # The sidecar IS durably published at the final path with the right row.
        assert Path(qpath).exists()
        records = [json.loads(line) for line in Path(qpath).read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["age"] == "badX"

        # Masked tables are published and correct.
        assert (tmp_path / "out" / "parent.parquet").exists()

        # The failure was logged, not raised.
        assert any(
            "failed to remove the staging link" in rec.message
            for rec in caplog.records
            if rec.levelno == logging.WARNING
        )


# ---------------------------------------------------------------------------
# DE-08 LOW (b): a filesystem that does not support hardlinks (cross-device
# stage/final, or FAT/exFAT/FUSE/overlay/network mounts) must degrade to a
# CLEAR, actionable error, not an opaque raw OSError straight from os.link.
# Nothing is published; the stage is discarded (no orphan); the fail-closed
# exclusive-create guarantee is never traded for a plain overwrite.
# ---------------------------------------------------------------------------


class TestHardlinkUnsupportedFilesystemDegradesGracefully:
    @pytest.mark.parametrize(
        "err",
        [
            errno.EXDEV,  # "Invalid cross-device link"
            errno.EPERM,  # link(2): filesystem does not support hard links
            getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),  # operation not supported
        ],
    )
    def test_hardlink_unsupported_errno_raises_clear_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int
    ) -> None:
        """os.link raises a hardlink-unsupported errno -> a clear RuntimeError
        naming the filesystem limitation, not a bare OSError. Tables already
        committed stay committed; the staged sidecar is discarded (no orphan);
        nothing is published at the quarantine path."""
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(tmp_path, qpath)
        sources = _sources(config)
        sink = ParquetTransactionalSink(tmp_path / "out")

        def _no_hardlinks(src: object, dst: object, *args: object, **kwargs: object) -> None:
            raise OSError(err, os.strerror(err))

        monkeypatch.setattr(_quarantine.os, "link", _no_hardlinks)

        with pytest.raises(RuntimeError, match="does not support hardlinks"):
            run_pipeline(
                config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
            )

        # The sink's own commit already succeeded -- masked tables published.
        assert (tmp_path / "out" / "parent.parquet").exists()
        assert (tmp_path / "out" / "child.parquet").exists()

        # Nothing published at the quarantine path (fail closed, no overwrite).
        assert not Path(qpath).exists()

        # No orphaned staging file left behind (best-effort discard fired).
        leftovers = list(tmp_path.glob("_decoy_quarantine_stage_*")) + list(
            (tmp_path / "out").glob("_decoy_quarantine_stage_*")
        )
        assert leftovers == []

    def test_ordinary_oserror_still_propagates_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A NON-hardlink OSError (e.g. ENOSPC disk full, or EACCES on the
        directory) is not about hardlink capability, so it must propagate as
        the original OSError -- not be reworded as a 'no hardlinks' error."""
        qpath = str(tmp_path / "quarantine.jsonl")
        config = _config(tmp_path, qpath)
        sources = _sources(config)
        sink = ParquetTransactionalSink(tmp_path / "out")

        def _disk_full(src: object, dst: object, *args: object, **kwargs: object) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(_quarantine.os, "link", _disk_full)

        with pytest.raises(OSError) as exc:
            run_pipeline(
                config, sources, engine_version="0.1.0", sink=sink, use_byte_estimate_routing=False
            )
        assert exc.value.errno == errno.ENOSPC
        assert not isinstance(exc.value, RuntimeError)
