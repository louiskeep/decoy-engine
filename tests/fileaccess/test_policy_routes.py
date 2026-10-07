"""AT-E3, AT-E4, AT-E7, AT-E10, AT-E11, AT-E13: whole routes under a strict policy.

The current-main route corpus runs unchanged under `LocalFileAccess` and under a
`RootedFileAccess` over the same files, and both match the golden record captured on the E1
base. A python audit hook proves no engine code opens a path of the tree during the run
except through a capability's descriptor.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import fileaccess
from decoy_engine.execution import ExecutionError
from decoy_engine.execution._chunked import run_mask_pipeline_chunked
from decoy_engine.execution._isolated_run import run_pipeline_isolated
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.execution.native._chunked_entry import run_mask_chunked
from decoy_engine.fileaccess import (
    FileAccessDenied,
    FileAccessUnavailable,
    FileRequest,
    FileRole,
    LocalFileAccess,
    scope,
)
from decoy_engine.profile._readers import LazySource
from decoy_engine.vault import VaultWriter
from tests.fileaccess import route_corpus as rc
from tests.fileaccess.support import RecordingFileAccess, RootRelative
from tests.unit.execution import _auto_chunk_support as support

pytestmark = pytest.mark.filterwarnings("ignore")


def _ctx(policy: Any) -> dict[str, Any]:
    return {
        "sink": lambda path: ParquetTransactionalSink(path, file_access=policy),
        "lazy": lambda path: LazySource(path, access=policy),
        "kw": {"file_access": policy},
    }


def _golden_record(name: str, tmp: Path, mp: pytest.MonkeyPatch, ctx: dict[str, Any]) -> Any:
    record = rc.JOBS[name](tmp, mp, ctx)
    return json.loads(json.dumps(record, sort_keys=True, default=repr))


@pytest.mark.parametrize("name", sorted(rc.JOBS))
def test_route_corpus_under_the_ambient_policy_object_matches_golden(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    got = _golden_record(name, tmp_path, monkeypatch, _ctx(LocalFileAccess()))
    assert got == rc.load_golden()[name]


@pytest.mark.parametrize("name", sorted(rc.JOBS))
def test_route_corpus_under_a_rooted_policy_matches_golden(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = RootRelative(tmp_path)
    try:
        got = _golden_record(name, tmp_path, monkeypatch, _ctx(policy))
    finally:
        policy.close()
    assert got == rc.load_golden()[name]


@pytest.mark.parametrize("name", sorted(rc.JOBS))
def test_every_job_reaches_the_file_system_only_through_a_capability(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audit hook sees every Python-level open and directory operation. Beneath the job's
    tree, only descriptor paths and the policy's own descriptor-relative calls remain."""
    inner = RootRelative(tmp_path)
    policy = RecordingFileAccess(inner)
    seen: list[tuple[str, str]] = []
    armed = threading.Event()
    prefix = str(tmp_path.resolve())

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if not armed.is_set():
            return
        if event in {"open", "os.listdir", "os.scandir", "os.rename", "os.remove", "os.mkdir"}:
            first = args[0] if args else None
            if isinstance(first, (str, bytes, Path)) and str(first).startswith(prefix):
                seen.append((event, str(first)))

    sys.addaudithook(hook)
    try:
        ctx = _ctx(policy)
        original = rc.run_pipeline

        def armed_run(*a: Any, **k: Any) -> Any:
            armed.set()
            try:
                return original(*a, **k)
            finally:
                armed.clear()

        monkeypatch.setattr(rc, "run_pipeline", armed_run)
        _golden_record(name, tmp_path, monkeypatch, ctx)
    finally:
        armed.clear()
        inner.close()
    if name != "generate_only":
        assert policy.requests, "the job made no policy request"
    assert not seen, seen


def test_isolated_run_under_a_strict_policy_fails_closed_before_any_io(tmp_path: Path) -> None:
    policy = RootRelative(tmp_path)
    try:
        cfg, src = rc._std_source(tmp_path)
        before = sorted(p.name for p in tmp_path.iterdir())
        with pytest.raises(FileAccessUnavailable):
            run_pipeline_isolated(cfg, {"t": src}, engine_version="e1", file_access=policy)
        with pytest.raises(FileAccessUnavailable):
            run_pipeline_isolated(
                cfg,
                {"t": src},
                engine_version="e1",
                isolate=False,
                output_dir=tmp_path / "out",
                file_access=policy,
            )
        assert sorted(p.name for p in tmp_path.iterdir()) == before
        result = run_pipeline_isolated(
            cfg, {"t": src}, engine_version="e1", isolate=False, file_access=policy
        )
        assert result.outcome == "completed"
    finally:
        policy.close()


def _chunk_job(_tmp_path: Path) -> tuple[dict[str, Any], list[pa.Table]]:
    src = pa.table({"r": pa.array([f"s{i}" for i in range(support.ROWS)])})
    cfg = support.make_cfg([support.redact_col("r")], path="/dev/null")
    chunks = [src.slice(i, support.CHUNK) for i in range(0, support.ROWS, support.CHUNK)]
    return cfg, chunks


def _denied_corpus_cfg(tmp_path: Path, corpus: Path) -> dict[str, Any]:
    cfg = support.make_cfg(
        [
            {
                "name": "r",
                "strategy": "code_set",
                "namespace": "ns",
                "provider_config": {"code_set": "x", "corpus_source": f"customer:{corpus}"},
            }
        ],
        path="/dev/null",
    )
    return cfg


class _Counting:
    def __init__(self, chunks: list[pa.Table]) -> None:
        self.chunks = chunks
        self.pulled = 0

    def __iter__(self) -> Iterator[pa.Table]:
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk


@pytest.mark.parametrize("entry", [run_mask_pipeline_chunked, run_mask_chunked])
def test_chunked_entries_refuse_a_denied_resource_before_the_second_chunk(
    entry: Any, tmp_path: Path
) -> None:
    """AT-E10: both chunked entry points raise the same error for the same denied resource
    before any chunk beyond the first is read."""
    root = tmp_path / "root"
    root.mkdir()
    corpus = tmp_path / "outside.parquet"
    pq.write_table(pa.table({"code": ["A", "B"]}), corpus)
    cfg = _denied_corpus_cfg(tmp_path, corpus)
    _, chunks = _chunk_job(tmp_path)
    policy = RootRelative(root)
    outcomes: list[str] = []
    try:
        for fn in (run_mask_pipeline_chunked, run_mask_chunked):
            counting = _Counting(chunks)
            try:
                list(
                    fn(
                        cfg,
                        counting,
                        table="t",
                        engine_version="e1",
                        file_access=policy,
                    )
                )
            except (FileAccessDenied, ExecutionError) as exc:
                outcomes.append(f"{type(exc).__name__}:{exc}")
            assert counting.pulled <= 1
    finally:
        policy.close()
    assert len(outcomes) == 2 and outcomes[0] == outcomes[1], outcomes


def test_lazy_iterator_is_judged_by_the_policy_it_was_created_under(tmp_path: Path) -> None:
    """AT-E11: consumption after the entry point returned, under another installed policy,
    nested inside a second iterator and from two threads uses the creating policy. The input
    iterable touches the policy on every chunk, so each step is observable."""
    root = tmp_path / "root"
    (root / "d").mkdir(parents=True)
    src = pa.table({"r": pa.array([f"s{i}" for i in range(support.ROWS)])})
    cfg = support.make_cfg([support.redact_col("r")], path="/dev/null")
    chunks = [src.slice(i, support.CHUNK) for i in range(0, support.ROWS, support.CHUNK)]
    probe = FileRequest(FileRole.SPILL_HINT, "stat", str(root / "d"), "targets.probe")

    def touching(seen: list[Any]) -> Iterator[pa.Table]:
        for chunk in chunks:
            policy = fileaccess.current()
            seen.append(policy)
            policy.stat(probe)
            yield chunk

    allowing = RootRelative(root)
    try:
        seen: list[Any] = []
        it = run_mask_pipeline_chunked(
            cfg, touching(seen), table="t", engine_version="e1", file_access=allowing
        )
        assert fileaccess.installed() is None
        revoking = _RevokeAfter(allowing, allow=0)
        with scope(revoking):  # type: ignore[arg-type]
            assert fileaccess.installed() is revoking
            first = next(it)
        other = RootRelative(root)
        try:
            with scope(other):
                second = next(it)
        finally:
            other.close()
        assert first.num_rows and second.num_rows
        assert revoking.calls == 0, "the iterator consulted a policy installed later"
        assert seen == [allowing, allowing]

        flaky = _RevokeAfter(allowing, allow=1)
        bound = run_mask_pipeline_chunked(
            cfg, touching([]), table="t", engine_version="e1", file_access=flaky
        )
        next(bound)
        with pytest.raises(FileAccessDenied):
            next(bound)

        results: dict[str, int] = {}

        def drain(label: str) -> None:
            local = run_mask_pipeline_chunked(
                cfg, touching([]), table="t", engine_version="e1", file_access=allowing
            )
            results[label] = sum(t.num_rows for t in local)

        threads = [threading.Thread(target=drain, args=(n,)) for n in ("a", "b")]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert results == {"a": support.ROWS, "b": support.ROWS}
    finally:
        allowing.close()


class _RevokeAfter:
    """Delegates the first `allow` reads, then refuses every request."""

    def __init__(self, inner: RootRelative, *, allow: int) -> None:
        self.inner = inner
        self.allow = allow
        self.calls = 0

    def _gate(self, req: FileRequest) -> None:
        self.calls += 1
        if self.calls > self.allow:
            raise FileAccessDenied(req.field_key)

    def open_read(self, req: FileRequest) -> Any:
        self._gate(req)
        return self.inner.open_read(req)

    def open_dir(self, req: FileRequest) -> Any:
        self._gate(req)
        return self.inner.open_dir(req)

    def stat(self, req: FileRequest) -> Any:
        self._gate(req)
        return self.inner.stat(req)

    def output_dir(self, req: FileRequest) -> Any:
        self._gate(req)
        return self.inner.output_dir(req)

    def allows_subprocess(self) -> bool:
        return False


def test_a_delayed_vault_writer_writes_through_the_policy_it_was_built_under(
    tmp_path: Path,
) -> None:
    """AT-E13: the writer keeps its policy; another installed policy changes nothing."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    policy_a = RootRelative(root_a)
    policy_b = RootRelative(root_b)
    try:
        writer = VaultWriter(b"12345678", file_access=policy_a)
        writer.add([("ns", "m", "s")])
        out_a = policy_a.output_dir(
            FileRequest(FileRole.VAULT, "write", str(root_a / "job_1.dcyvault"), "vault")
        )
        with scope(policy_b):
            writer.write(out_a)
            with pytest.raises(FileAccessUnavailable):
                writer.write(str(root_b / "path.dcyvault"))
        assert [p.name for p in root_a.iterdir()] == ["job_1.dcyvault"]
        assert list(root_b.iterdir()) == []
    finally:
        policy_a.close()
        policy_b.close()


def test_run_pipeline_under_a_strict_policy_never_writes_outside_its_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    policy = RootRelative(root)
    try:
        _cfg, src = rc._std_source(root)
        escaped = tmp_path / "escaped"
        with pytest.raises(FileAccessDenied):
            ParquetTransactionalSink(escaped, file_access=policy).write("t", src)
        assert not escaped.exists()
    finally:
        policy.close()


def test_every_entry_point_refuses_before_any_syscall_once_ambient_is_forbidden() -> None:
    """AT-E2: with `forbid_ambient()` set, each `ENTRY_POINTS` member called with no policy
    raises `FileAccessUnavailable` from its wrapper, before it looks at its arguments."""
    code = textwrap.dedent(
        """
        import importlib
        from decoy_engine import fileaccess
        from decoy_engine.fileaccess import ENTRY_POINTS, FileAccessUnavailable, LocalFileAccess

        fileaccess.forbid_ambient()
        bad = []
        for dotted in ENTRY_POINTS:
            module, _, name = dotted.rpartition(".")
            fn = getattr(importlib.import_module(module), name)
            for kwargs in ({}, {"file_access": LocalFileAccess()}):
                try:
                    fn(**kwargs)
                except FileAccessUnavailable:
                    continue
                except Exception as exc:
                    bad.append((dotted, type(exc).__name__))
                else:
                    bad.append((dotted, "returned"))
        assert not bad, bad
        try:
            fileaccess.current()
        except FileAccessUnavailable:
            print("ok")
        """
    )
    out = subprocess.run(  # noqa: S603 - runs this interpreter on a fixed script
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


def test_probe_recovery_is_inconclusive_under_a_strict_policy(rooted) -> None:  # type: ignore[no-untyped-def]
    """The probe needs a child process a strict policy cannot serve: the answer is None
    (inconclusive, leave routing to the static estimate), never a refusal."""
    from decoy_engine import fileaccess
    from decoy_engine.execution._pipeline_routing_signals import resolve_probe_recovery

    class _Profile:
        tables = [object()]

    with fileaccess.scope(rooted):
        verdict = resolve_probe_recovery(
            True,
            True,
            _Profile(),
            {},
            {},
            None,
            None,
            config={},
            engine_version="e",
        )
    assert verdict is None


def test_the_isolated_route_fails_closed_with_a_typed_error_under_a_strict_policy(rooted) -> None:  # type: ignore[no-untyped-def]
    import pyarrow as pa

    from decoy_engine.execution import run_pipeline_isolated
    from decoy_engine.fileaccess import FileAccessUnavailable

    with pytest.raises(FileAccessUnavailable, match="run_pipeline_isolated"):
        run_pipeline_isolated(
            {"tables": []},
            {"t": pa.table({"a": [1]})},
            engine_version="e",
            file_access=rooted,
        )
