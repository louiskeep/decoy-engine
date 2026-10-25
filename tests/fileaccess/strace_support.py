"""The runtime detector: `strace -f` over a child interpreter, and a parser for what it saw.

PEP 578 audit hooks raise no event for `os.stat` and see nothing a native library opens, so the
detector for "this process touched that file" is `strace`. The helper fails, never skips, when
`strace` is missing or ptrace is refused (set `STRACE` to a binary outside `PATH`). `-f` follows
every process the child creates and `-yy` decodes descriptor arguments, so a sentinel reached
through an inherited or passed descriptor is attributed to its path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

TRACE_SET = (
    "open,openat,openat2,creat,stat,lstat,fstat,newfstatat,statx,access,faccessat,faccessat2,"
    "readlink,readlinkat,getdents64,mkdir,mkdirat,rename,renameat,renameat2,link,linkat,symlink,"
    "symlinkat,unlink,unlinkat,rmdir,truncate,ftruncate,chmod,fchmodat,utimensat,chdir,fchdir,"
    "execve,execveat,clone,clone3,fork,vfork,sendmsg,recvmsg,pidfd_getfd,"
    "read,pread64,readv,preadv,close"
)
_FD_DECORATION = re.compile(r"\d+<[^>]*>")


def strace_binary() -> str:
    found = os.environ.get("STRACE") or shutil.which("strace")
    if not found:
        raise RuntimeError("strace is required by the runtime detector and is not installed")
    return found


@dataclass
class Trace:
    lines: list[str]

    def window(self, begin: str, end: str) -> list[str]:
        """The syscalls between the two marker `stat` calls the traced script makes."""

        # The execve line prints the whole script, so match the marker's own stat call.
        def marker(name: str) -> str:
            return f'stat("/{name}"'

        start = next(i for i, ln in enumerate(self.lines) if marker(begin) in ln)
        stop = next(i for i, ln in enumerate(self.lines) if marker(end) in ln and i > start)
        return self.lines[start + 1 : stop]


def undecorated(line: str) -> str:
    """`line` without `-yy` descriptor decorations, so what remains names paths by string."""
    return _FD_DECORATION.sub("", line)


def mentions_by_path(lines: list[str], token: str) -> list[str]:
    """Syscalls that name `token` in a path string (a direct, path-based access)."""
    return [ln for ln in lines if token in undecorated(ln)]


def mentions_at_all(lines: list[str], token: str) -> list[str]:
    return [ln for ln in lines if token in ln]


def run_traced(script: str, tmp_path: Path, *, timeout: int = 300) -> Trace:
    """Run `script` in a fresh interpreter under strace and return what it traced."""
    out = tmp_path / "strace.out"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    cmd = [
        strace_binary(),
        "-f",
        "-qq",
        "-s",
        "65535",
        "-yy",
        "-e",
        f"trace={TRACE_SET}",
        "-o",
        str(out),
        sys.executable,
        "-c",
        textwrap.dedent(script),
    ]
    done = subprocess.run(  # noqa: S603 - fixed argv built from this interpreter and strace
        cmd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )
    if done.returncode != 0 or not out.exists():
        raise RuntimeError(f"traced script failed ({done.returncode}): {done.stderr[-2000:]}")
    return Trace(out.read_text(errors="replace").splitlines())
