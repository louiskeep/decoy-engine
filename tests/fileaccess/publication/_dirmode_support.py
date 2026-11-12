"""Spawn-child cases for F22 a1a (plan 5.0 rule 1, 5.1 V1-b): directory modes under a umask.

Each case runs in a `spawn` child because the umask is process-wide and two cases kill
themselves. A case reads a JSON spec and writes a JSON result; the parent asserts on it.

Under root the kernel ignores owner bits, so a directory the umask left without them would
open anyway and every refusal cell would pass for the wrong reason. `_emulate_owner_bits`
wraps `os.open` and `os.mkdir` so that a call needing an owner bit the directory lacks raises
`EACCES`, as it does for any other uid; it is on only when the effective uid is 0.
"""

from __future__ import annotations

import builtins
import errno
import io
import json
import multiprocessing
import os
import signal
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.fileaccess._openat2 import openat2_usable

UNIT_FILE = "t.parquet"
UNIT_BYTES = b"F22-A1A-UNIT"
KILL_MARKER = "killed-after-mkdir"

# The strict policy confines every open with openat2, which only Linux has; the ambient cells
# run everywhere.
POLICIES = [
    pytest.param(
        "strict",
        marks=pytest.mark.skipif(not openat2_usable(), reason="strict policy needs Linux openat2"),
    ),
    "ambient",
]


def run_child(target: Callable[[str], None], spec: dict[str, Any], tmp: Path) -> tuple[int, dict]:
    """Run `target(spec_path)` in a fresh `spawn` interpreter; return its exit code and result."""
    spec_path = tmp / f"spec-{os.getpid()}-{len(list(tmp.glob('spec-*')))}.json"
    result_path = spec_path.with_suffix(".result.json")
    spec = {**spec, "result": str(result_path)}
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    proc = multiprocessing.get_context("spawn").Process(target=target, args=(str(spec_path),))
    proc.start()
    proc.join(120)
    if proc.is_alive():  # pragma: no cover - a hung child is a failure, not a pass
        proc.kill()
        proc.join()
        raise AssertionError("child did not finish")
    exitcode = proc.exitcode if proc.exitcode is not None else -1
    result = json.loads(result_path.read_text("utf-8")) if result_path.exists() else {}
    return exitcode, result


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


# -- child side ---------------------------------------------------------------------------------


def _resolve(path: Any, dir_fd: int | None) -> str:
    text = os.fsdecode(path)
    if dir_fd is None or os.path.isabs(text):
        return os.path.abspath(text)
    return os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), text)


def _denied() -> OSError:
    return PermissionError(errno.EACCES, os.strerror(errno.EACCES))


def _emulate_owner_bits(root: str) -> None:
    """Make owner bits bind under uid 0 for paths beneath `root` (see the module docstring)."""
    if os.geteuid() != 0:
        return
    from decoy_engine.fileaccess import _openat2

    _openat2.FORCE_PORTABLE = True  # confined opens then go through os.open, so they bind too
    real_open, real_mkdir = os.open, os.mkdir

    def searchable(full: str) -> bool:
        parent = os.path.dirname(full)
        while parent.startswith(root) and len(parent) > len(root):
            if not os.stat(parent).st_mode & stat.S_IXUSR:
                return False
            parent = os.path.dirname(parent)
        return True

    def guarded_open(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        full = _resolve(path, dir_fd)
        if full.startswith(root):
            if not searchable(full):
                raise _denied()
            reading = (flags & os.O_ACCMODE) == os.O_RDONLY and not flags & os.O_PATH
            try:
                st = os.lstat(full) if flags & os.O_NOFOLLOW else os.stat(full)
            except OSError:
                st = None
            if reading and st is not None and stat.S_ISDIR(st.st_mode):
                if not st.st_mode & stat.S_IRUSR:
                    raise _denied()
            if flags & os.O_CREAT and st is None:
                if not os.stat(os.path.dirname(full)).st_mode & stat.S_IWUSR:
                    raise _denied()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def guarded_mkdir(path: Any, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        full = _resolve(path, dir_fd)
        if full.startswith(root):
            parent_mode = os.stat(os.path.dirname(full)).st_mode
            if not searchable(full) or (parent_mode & 0o300) != 0o300:
                raise _denied()
        real_mkdir(path, mode, dir_fd=dir_fd)

    os.open = guarded_open  # type: ignore[assignment]
    os.mkdir = guarded_mkdir  # type: ignore[assignment]


def _install_kill_after_mkdir(name: str, marker: Path) -> None:
    """SIGKILL right after the `mkdir` of `name` returns, before any owner-bit repair."""
    inner = os.mkdir

    def killing(path: Any, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        inner(path, mode, dir_fd=dir_fd)
        if os.path.basename(os.fsdecode(path)) == name:
            marker.write_text("x", encoding="utf-8")
            os.kill(os.getpid(), signal.SIGKILL)

    os.mkdir = killing  # type: ignore[assignment]


def _install_eexist_hook(name: str, seen: dict[str, Any]) -> None:
    """Make `name` (and hold it open) between the attempt's parent open and its own `mkdir`."""
    inner, real_open = os.mkdir, os.open

    def racing(path: Any, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if os.path.basename(os.fsdecode(path)) == name and "ino" not in seen:
            inner(path, 0o755, dir_fd=dir_fd)
            fd = real_open(path, os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)
            st = os.fstat(fd)
            seen["ino"] = [st.st_dev, st.st_ino]
            seen["held_fd"] = fd  # kept open for the life of the attempt
        inner(path, mode, dir_fd=dir_fd)

    os.mkdir = racing  # type: ignore[assignment]


def _install_spies(found: dict[str, Any]) -> None:
    """Record directory-core and `make_parent_dir_at` calls, validator arguments, `os.umask` and
    `/proc` status opens."""
    from decoy_engine.fileaccess import _component, _output

    found.update(core=[], entry=[], validated=[], umask=0, proc=0)
    core = getattr(_output, "_make_dir_core", None)
    if core is not None:

        def core_spy(parent_fd: int, name: str, mode: int, **kw: Any) -> Any:
            found["core"].append([name, mode])
            return core(parent_fd, name, mode, **kw)

        _output._make_dir_core = core_spy  # type: ignore[attr-defined]

    from decoy_engine.fileaccess import _local

    entry = _output.make_parent_dir_at

    def entry_spy(parent_fd: int, name: str) -> int:
        found["entry"].append(name)
        return entry(parent_fd, name)

    _output.make_parent_dir_at = entry_spy  # type: ignore[assignment]
    _local.make_parent_dir_at = entry_spy  # type: ignore[assignment]

    real_validate = _component.validate_component

    def validate_spy(component: object, field_key: str) -> str:
        found["validated"].append(str(component))
        return real_validate(component, field_key)

    _output.validate_component = validate_spy  # type: ignore[assignment]
    _component.validate_component = validate_spy  # type: ignore[assignment]

    real_umask = os.umask

    def umask_spy(mask: int) -> int:
        found["umask"] += 1
        return real_umask(mask)

    os.umask = umask_spy  # type: ignore[assignment]

    def watch(fn: Callable[..., Any]) -> Callable[..., Any]:
        def opened(path: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(path, (str, bytes, os.PathLike)) and "/proc/self/status" in os.fsdecode(
                path
            ):
                found["proc"] += 1
            return fn(path, *args, **kwargs)

        return opened

    os.open = watch(os.open)  # type: ignore[assignment]
    builtins.open = watch(builtins.open)
    io.open = watch(io.open)  # type: ignore[assignment]


def _output_dir(policy: str, base: Path, rel: str) -> tuple[Any, int]:
    from decoy_engine.fileaccess import FileRequest, FileRole, Root, RootedFileAccess
    from decoy_engine.fileaccess._local import LocalFileAccess

    if policy == "ambient":
        req = FileRequest(FileRole.TARGET_OUTPUT, "write", str(base / rel), "targets.t.path")
        return LocalFileAccess().output_dir(req), -1
    root_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    rooted = RootedFileAccess(
        [Root("main", root_fd, frozenset({FileRole.TARGET_OUTPUT}), frozenset({"write"}))]
    )
    req = FileRequest(FileRole.TARGET_OUTPUT, "write", rel, "targets.t.path")
    return rooted.output_dir(req), root_fd


def _probe(base: Path) -> int:
    """The mode a plain file gets under this umask, made before any Decoy call (non-vacuity)."""
    probe = base / f".probe-{os.getpid()}"
    os.close(os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666))
    got = stat.S_IMODE(os.lstat(probe).st_mode)
    os.chmod(probe, 0o600)
    os.unlink(probe)
    return got


def _write(result: str, data: dict[str, Any]) -> None:
    data.pop("held_fd", None)
    fd = os.open(result, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchmod(fd, 0o600)  # the case's umask would leave it unreadable to a non-root parent
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data))


def publish_case(spec_path: str) -> None:
    """Publish (or `ensure_dir`) into `spec["rel"]` beneath `spec["base"]` under `spec["umask"]`."""
    spec = json.loads(Path(spec_path).read_text("utf-8"))
    base = Path(spec["base"])
    os.umask(spec["umask"])
    found: dict[str, Any] = {"probe": _probe(base)}
    _emulate_owner_bits(str(base))
    if spec.get("spies"):
        _install_spies(found)
    if spec.get("kill_after_mkdir"):
        _install_kill_after_mkdir(spec["kill_after_mkdir"], base.parent / KILL_MARKER)
    if spec.get("eexist_hook"):
        _install_eexist_hook(spec["eexist_hook"], found)
    try:
        out, _root_fd = _output_dir(spec["policy"], base, spec["rel"])
        if spec.get("op") == "ensure_dir":
            out.ensure_dir()
        else:
            txn = out.begin("dir", "exclusive", label=spec.get("label", "table_sink"))
            fd = txn.create_file(UNIT_FILE)
            os.write(fd, UNIT_BYTES)
            os.close(fd)
            txn.publish()
        found["outcome"] = "published"
    except Exception as exc:
        found["outcome"] = "refused"
        found["exc"] = type(exc).__name__
        found["errno"] = getattr(exc, "errno", None)
    _write(spec["result"], found)


STAGE_PREFIXES = {"table_sink": "_decoy_stage_", "publication": "._decoy_publication_pending_"}


def _spy_fsync() -> list[list[int]]:
    """Record the `(st_dev, st_ino)` of every descriptor `os.fsync` is called on."""
    seen: list[list[int]] = []
    real = os.fsync

    def spy(fd: int) -> None:
        st = os.fstat(fd)
        seen.append([st.st_dev, st.st_ino])
        real(fd)

    os.fsync = spy  # type: ignore[assignment]
    return seen


def umask_stage_case(spec_path: str) -> None:
    """One dir txn with a file and a `create_dir` subdirectory, into an existing parent."""
    spec = json.loads(Path(spec_path).read_text("utf-8"))
    base = Path(spec["base"])
    os.umask(spec["umask"])
    found: dict[str, Any] = {"probe": _probe(base)}
    _emulate_owner_bits(str(base))
    parent = base / "out"
    label = spec.get("label", "table_sink")
    synced = _spy_fsync() if label == "publication" else []
    try:
        out, _root_fd = _output_dir(spec["policy"], base, "out/leaf")
        txn = out.begin("dir", "exclusive", label=label)
        fd = txn.create_file(UNIT_FILE)
        os.write(fd, UNIT_BYTES)
        os.close(fd)
        sub = txn.create_dir("sub")
        os.close(sub.create_file("g"))
        prefix = STAGE_PREFIXES[label]
        (stage,) = [p for p in parent.iterdir() if p.name.startswith(prefix)]
        found["stage_mode"] = stat.S_IMODE(os.lstat(stage).st_mode)
        found["sub_mode"] = stat.S_IMODE(os.lstat(stage / "sub").st_mode)
        unit = [stage / UNIT_FILE, stage / "sub" / "g", stage / "sub", stage]
        keys = [[os.lstat(p).st_dev, os.lstat(p).st_ino] for p in unit]
        txn.publish()
        found["unit_keys"] = keys
        found["synced"] = [k for k in keys if k in synced]
        found["outcome"] = "published"
    except Exception as exc:
        found["outcome"] = "refused"
        found["exc"] = type(exc).__name__
        found["errno"] = getattr(exc, "errno", None)
    _write(spec["result"], found)


def setgid_case(spec_path: str) -> None:
    """Both entry points in a set-group-ID parent whose group is not the caller's."""
    spec = json.loads(Path(spec_path).read_text("utf-8"))
    shared = Path(spec["shared"])
    os.umask(spec["umask"])
    found: dict[str, Any] = {}
    _emulate_owner_bits(str(shared.parent))
    from decoy_engine.fileaccess import _output

    fd = os.open(shared, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.close(_output.make_stage_dir_at(fd, "s"))
        made = _output.make_parent_dir_at(fd, "d")  # type: ignore[attr-defined]
        try:
            os.close(_output.make_parent_dir_at(made, "e"))  # type: ignore[attr-defined]
        finally:
            os.close(made)
        found["outcome"] = "made"
    except Exception as exc:
        found["outcome"] = "refused"
        found["exc"] = type(exc).__name__
    finally:
        os.close(fd)
    _write(spec["result"], found)
