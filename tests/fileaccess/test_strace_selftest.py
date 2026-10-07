"""D10 self-test: the runtime detector attributes a sentinel reached by each route the plan
names. Without these the denial assertions elsewhere could pass because the detector is blind."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from tests.fileaccess.strace_support import mentions_at_all, run_traced

PRELUDE = """
import os, sys
def mark(path):
    try:
        os.stat(path)
    except OSError:
        pass
"""


def _sentinel(tmp_path: Path) -> tuple[Path, str]:
    token = f"sent{uuid.uuid4().hex[:12]}"
    folder = tmp_path / f"dir{token}"
    folder.mkdir()
    return folder, token


def _run(tmp_path: Path, body: str):  # type: ignore[no-untyped-def]
    script = PRELUDE + '\nmark("/SELF-BEGIN")\n' + body + '\nmark("/SELF-END")\n'
    return run_traced(script, tmp_path).window("SELF-BEGIN", "SELF-END")


def _opened(window: list[str], token: str) -> bool:
    return any(re.match(r"\d+ (open|openat|openat2|creat)\(", ln) and token in ln for ln in window)


def test_an_in_process_open_is_seen(tmp_path: Path) -> None:
    folder, token = _sentinel(tmp_path)
    target = folder / f"{token}.bin"
    target.write_bytes(b"x" * 64)
    window = _run(tmp_path, f"open({str(target)!r}, 'rb').read(8)")
    assert _opened(window, token)
    assert [ln for ln in window if re.match(r"\d+ p?read", ln) and token in ln]


def test_an_open_by_a_child_process_is_seen(tmp_path: Path) -> None:
    folder, token = _sentinel(tmp_path)
    target = folder / f"{token}.bin"
    target.write_bytes(b"x")
    body = f"""
import subprocess
subprocess.run([sys.executable, "-c", "open({str(target)!r}, 'rb').read()"], check=True)
"""
    assert _opened(_run(tmp_path, body), token)


def test_an_open_after_execve_is_seen(tmp_path: Path) -> None:
    folder, token = _sentinel(tmp_path)
    target = folder / f"{token}.bin"
    target.write_bytes(b"x")
    body = f"""
pid = os.fork()
if pid == 0:
    os.execv(sys.executable, [sys.executable, "-c", "open({str(target)!r}, 'rb').read()"])
os.waitpid(pid, 0)
"""
    window = _run(tmp_path, body)
    assert _opened(window, token) and any(ln for ln in window if "execve(" in ln)


def test_a_dir_fd_relative_open_is_attributed_by_name_and_directory(tmp_path: Path) -> None:
    folder, token = _sentinel(tmp_path)
    (folder / f"{token}.bin").write_bytes(b"x")
    body = f"""
dfd = os.open({str(folder)!r}, os.O_RDONLY | os.O_DIRECTORY)
fd = os.open("{token}.bin", os.O_RDONLY, dir_fd=dfd)
os.read(fd, 1)
"""
    window = _run(tmp_path, body)
    assert [ln for ln in window if "openat(" in ln and f"{token}.bin" in ln and token in ln]


def test_a_directory_enumeration_is_seen(tmp_path: Path) -> None:
    folder, token = _sentinel(tmp_path)
    (folder / "a").write_text("a")
    window = _run(tmp_path, f"list(os.scandir({str(folder)!r}))")
    assert [ln for ln in window if "getdents64(" in ln and token in ln]


def test_a_renameat2_publication_is_seen(tmp_path: Path) -> None:
    folder, token = _sentinel(tmp_path)
    (folder / "from").write_text("a")
    body = f"""
import ctypes
libc = ctypes.CDLL(None, use_errno=True)
dfd = os.open({str(folder)!r}, os.O_RDONLY | os.O_DIRECTORY)
rc = libc.renameat2(dfd, b"from", dfd, b"to", 1)  # RENAME_NOREPLACE: a real renameat2
assert rc == 0, ctypes.get_errno()
"""
    window = _run(tmp_path, body)
    assert [ln for ln in window if "renameat2(" in ln and token in ln]


READERS = {
    "pyarrow": "import pyarrow.parquet as pq; pq.read_table({p!r})",
    "pandas": "import pandas as pd; pd.read_parquet({p!r})",
    "polars": "import polars as pl; pl.read_parquet({p!r})",
    "duckdb": "import duckdb; duckdb.sql(\"select count(*) from read_parquet('{p}')\").fetchall()",
}


@pytest.mark.parametrize("lib", sorted(READERS))
def test_a_library_read_is_seen(tmp_path: Path, lib: str) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    folder, token = _sentinel(tmp_path)
    target = folder / f"{token}.parquet"
    pq.write_table(pa.table({"a": [1, 2, 3]}), target)
    window = _run(tmp_path, READERS[lib].format(p=str(target)))
    assert mentions_at_all(window, token), f"{lib}: the read was not attributed to the sentinel"
    assert [ln for ln in window if re.match(r"\d+ (openat|open)", ln) and token in ln], lib


def test_a_duckdb_write_is_seen(tmp_path: Path) -> None:
    folder, token = _sentinel(tmp_path)
    target = folder / f"{token}.parquet"
    body = f"import duckdb\nduckdb.sql(\"copy (select 1 as a) to '{target}' (format parquet)\")\n"
    window = _run(tmp_path, body)
    assert [
        ln
        for ln in window
        if re.match(r"\d+ (openat|open)", ln) and token in ln and "O_CREAT" in ln
    ]
