"""AT-E3 (runtime attribution) and the cold-import control for the registered import-time read.

A job runs in a child interpreter under `strace -f`. After the policy and the job's files are
built, the traced window contains only the engine run. Every syscall that names the job's
directory by path string would be a direct open the policy never saw; the only path-based
syscalls the policy itself makes are `openat2` relative to an already-open root descriptor.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from tests.fileaccess.strace_support import (
    mentions_at_all,
    mentions_by_path,
    run_traced,
)

HEADER = """
import os, sys
sys.path.insert(0, {repo!r})
import pyarrow as pa
import pyarrow.parquet as pq
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
from decoy_engine.fileaccess import FileAccessDenied
from decoy_engine.profile import profile_source
from decoy_engine.profile._readers import LazySource
from tests.fileaccess.support import RootRelative
from tests.unit.execution import _auto_chunk_support as support

def mark(path):
    try:
        os.stat(path)
    except OSError:
        pass


root = {root!r}
outside = {outside!r}
os.makedirs(root); os.makedirs(outside)
src = pa.table({{"h": [f"u{{i}}@x.example" for i in range(40)], "r": [f"s{{i}}" for i in range(40)]}})
path = support.write_source(src, os.path.join(root, "s.parquet"))
pq.write_table(src, os.path.join(outside, "secret.parquet"))
os.symlink(outside, os.path.join(root, "link"))
cfg = support.make_cfg([support.hash_col("h"), support.redact_col("r")], path=path)
policy = RootRelative(root)
"""

REPO = str(Path(__file__).resolve().parents[2])


def _dirs(tmp_path: Path) -> tuple[str, str, str, str]:
    inside_token = f"in{uuid.uuid4().hex[:10]}"
    outside_token = f"out{uuid.uuid4().hex[:10]}"
    base = tmp_path / "w"
    return (
        inside_token,
        outside_token,
        str(base / inside_token / "root"),
        str(base / outside_token),
    )


def test_a_streamed_run_touches_the_tree_only_through_the_policy(tmp_path: Path) -> None:
    inside, outside, root, out = _dirs(tmp_path)
    script = (
        HEADER.format(repo=REPO, root=root, outside=out)
        + """
lazy = LazySource(__import__("pathlib").Path(path), access=policy)
sink = ParquetTransactionalSink(os.path.join(root, "out"), file_access=policy)
mark("/ATE3-BEGIN")
run_pipeline(cfg, sources={"t": lazy}, sink=sink, file_access=policy,
             **support.run_kwargs())
mark("/ATE3-END")
assert os.path.isdir(os.path.join(root, "out"))
"""
    )
    window = run_traced(script, tmp_path).window("ATE3-BEGIN", "ATE3-END")
    assert window, "the window captured no syscalls"
    direct = mentions_by_path(window, inside)
    assert not direct, "path-based syscalls on the job tree:\n" + "\n".join(direct[:20])
    assert any("openat2(" in ln for ln in window), "the policy opened nothing with openat2"
    assert not mentions_at_all(window, "RENAME_EXCHANGE")


def test_a_policy_decided_denial_makes_no_syscall_naming_the_target(tmp_path: Path) -> None:
    inside, outside, root, out = _dirs(tmp_path)
    secret = out + "/secret.parquet"
    script = (
        HEADER.format(repo=REPO, root=root, outside=out)
        + f"""
from decoy_engine.fileaccess import FileRole
from decoy_engine.fileaccess._rooted import Root, RootedFileAccess
fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
policy = RootedFileAccess([Root("r", fd, frozenset(FileRole), frozenset({"read", "stat"}), prefix="data")])
mark("/ATE3-BEGIN")
for target in ({secret!r}, "../{outside}/secret.parquet", "/etc/hostname"):
    config = {{"sources": {{"t": {{"type": "file", "format": "parquet", "path": target}}}}}}
    try:
        profile_source(config, file_access=policy)
    except FileAccessDenied:
        pass
    else:
        raise SystemExit("not denied")
mark("/ATE3-END")
"""
    )
    window = run_traced(script, tmp_path).window("ATE3-BEGIN", "ATE3-END")
    assert not mentions_at_all(window, outside), "\n".join(mentions_at_all(window, outside))
    assert not [ln for ln in window if "hostname" in ln]


def test_a_kernel_decided_denial_never_opens_the_target(tmp_path: Path) -> None:
    inside, outside, root, out = _dirs(tmp_path)
    script = (
        HEADER.format(repo=REPO, root=root, outside=out)
        + """
mark("/ATE3-BEGIN")
config = {"sources": {"t": {"type": "file", "format": "parquet", "path": os.path.join(root, "link", "secret.parquet")}}}
try:
    profile_source(config, file_access=policy)
except FileAccessDenied:
    pass
else:
    raise SystemExit("not denied")
mark("/ATE3-END")
"""
    )
    window = run_traced(script, tmp_path).window("ATE3-BEGIN", "ATE3-END")
    assert not mentions_at_all(window, outside), "\n".join(mentions_at_all(window, outside))
    # `-yy` decorates each descriptor with its path, so a read of the target would name it;
    # the `read`/`pread64` calls are in the trace set, so this can fail (see the control below).
    assert not [ln for ln in window if re.match(r"\d+ p?read", ln) and "secret.parquet" in ln]


def test_cold_import_reads_the_packaged_grammar_and_the_detector_sees_it(tmp_path: Path) -> None:
    """Control for the registry entry covering the import-time read (`_when_parser`): the
    detector must see that read, so a new import-time read would not go unnoticed."""
    script = f"""
import os, sys
sys.path.insert(0, {REPO!r})
def mark(path):
    try:
        os.stat(path)
    except OSError:
        pass
mark("/COLD-BEGIN")
import decoy_engine.expressions._when_parser
mark("/COLD-END")
"""
    window = run_traced(script, tmp_path).window("COLD-BEGIN", "COLD-END")
    grammar = [ln for ln in window if "when_grammar.lark" in ln and "openat" in ln]
    assert grammar, "the packaged when_grammar.lark read was not observed at import"
    # Import-time reads that are registered: the shipped name-hint YAML (storm detectors load it
    # at import) and the default log file. Anything else read at import is a new, unregistered one.
    registered = ("storm/name_hints/v1/", "logs/decoy_engine.log")
    package_data = [
        ln
        for ln in window
        if re.match(r"\d+ openat", ln)
        and "decoy_engine" in ln
        and not (re.search(r'"([^"]*)"', ln) or [None, ""])[1].endswith(
            (".py", ".pyc", ".so", ".lark")
        )
        and not any(r in ln for r in registered)
        and "ENOENT" not in ln
        and "O_DIRECTORY" not in ln
    ]
    assert not package_data, "\n".join(package_data[:10])


def test_the_detector_sees_a_direct_open_and_read_of_the_target(tmp_path: Path) -> None:
    """Positive control for the assertions above: a plain path open of the same file, with a
    read, appears in the window as an open naming the target and a read of its descriptor."""
    inside, outside, root, out = _dirs(tmp_path)
    script = (
        HEADER.format(repo=REPO, root=root, outside=out)
        + f"""
mark("/ATE3-BEGIN")
with open({out + "/secret.parquet"!r}, "rb") as fh:
    fh.read(16)
mark("/ATE3-END")
"""
    )
    window = run_traced(script, tmp_path).window("ATE3-BEGIN", "ATE3-END")
    assert [ln for ln in window if re.match(r"\d+ openat", ln) and "secret.parquet" in ln]
    assert [ln for ln in window if re.match(r"\d+ p?read", ln) and "secret.parquet" in ln]
    assert [ln for ln in window if re.match(r"\d+ close", ln) and "secret.parquet" in ln]
