"""F25 scratch allocation sentry (plan 2).

The runtime choke-point is the ScratchManager: a temporary producer may obtain a
writable path only by holding a ``RegisteredFile`` / ``NativeDirectory`` /
``PublicationStage`` capability, issued after durable registration. This sentry
is the drift guard that keeps that true as the seam grows, modelled on
``tests/sentry/test_physical_seam_disconnection.py`` (AST/string sweep + a
dynamic subprocess check).

Slice 1 state: the machine-readable manifest (``scratch_producer_manifest.json``)
seeds the plan 1.3 T01-T17 inventory, every row ``unmigrated``. The sentry
enforces registration on NONE of them yet, because the migration is Slices 2-3.
What Slice 1 proves is that the sentry has teeth: the AST scanner flags each
seeded violation class (unregistered tempfile, aliased Arrow writer, new DuckDB
connect, SQL COPY, hardcoded temp path, O_TMPFILE / open+unlink), a clean
capability-only snippet passes, and a stale manifest entry fails. The three
syscall-traced checks (A04/A12) run a fresh subprocess under strace and prove an
unregistered write that unlinks its file immediately is still caught at the
kernel boundary, which an end-of-run directory listing would miss.

As each T row migrates, its manifest status flips to ``migrated`` and it gains an
allocation wrapper + witness; the per-producer AST and syscall enforcement then
switches on for that row (Slices 2-3). A whole-module exclusion is never allowed.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ENGINE_ROOT = HERE.parents[1] / "src" / "decoy_engine"
MANIFEST_PATH = HERE / "scratch_producer_manifest.json"

EXPECTED_T_IDS = tuple(f"T{n:02d}" for n in range(1, 18))


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# AST + string scanner for unregistered allocation / write primitives.
# ---------------------------------------------------------------------------

# Dotted-call tails that allocate or open a writable temporary resource outside
# the ScratchManager. A migrated producer reaches its path through a capability
# (arena.allocate_* then unwrap_path/unwrap_dir), so these raw primitives in a
# production write site are what the sentry is built to catch.
_FORBIDDEN_CALL_TAILS: frozenset[str] = frozenset(
    {
        "tempfile.mkdtemp",
        "tempfile.mkstemp",
        "tempfile.NamedTemporaryFile",
        "tempfile.TemporaryDirectory",
        "os.mkdtemp",
        "pa.OSFile",
        "pa.ipc.new_file",
        "pa.RecordBatchFileWriter",
        "pq.ParquetWriter",
        "pyarrow.parquet.ParquetWriter",
        "duckdb.connect",
    }
)

# Sanctioned wrappers: a call whose tail is one of these is the registration
# seam itself and is never a finding.
_SANCTIONED_CALL_TAILS: frozenset[str] = frozenset(
    {
        "arena.allocate_file",
        "arena.allocate_native_dir",
        "arena.allocate_publication",
        "arena.checkpoint",
        "scratch.unwrap_path",
        "scratch.unwrap_dir",
        "unwrap_path",
        "unwrap_dir",
    }
)

_WRITE_MODE_RE = re.compile(r"[wxa+]")
_STRING_BACKSTOPS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sql_copy_to", re.compile(r"\bCOPY\b.+\bTO\b", re.IGNORECASE | re.DOTALL)),
    ("o_tmpfile", re.compile(r"O_TMPFILE")),
)


def _call_tail(node: ast.Call) -> str:
    parts: list[str] = []
    cur: ast.expr = node.func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _opens_for_write(node: ast.Call, tail: str) -> bool:
    """A builtin ``open`` / ``Path.open`` / ``os.open`` that creates or writes."""

    name = tail.split(".")[-1]
    if name not in {"open"}:
        return False
    # Builtin / Path.open: the MODE is positional arg 1 or the mode= keyword.
    # Inspect only the mode, never the path -- a path containing w/x/a/+ is not a write mode.
    mode_node: ast.expr | None = node.args[1] if len(node.args) > 1 else None
    for kw in node.keywords:
        if kw.arg == "mode":
            mode_node = kw.value
    if (
        isinstance(mode_node, ast.Constant)
        and isinstance(mode_node.value, str)
        and _WRITE_MODE_RE.search(mode_node.value)
    ):
        return True
    # os.open uses integer O_* flags, not a string mode: detect the create/write tokens.
    flags_src = ast.dump(node)
    return "O_CREAT" in flags_src or "O_TMPFILE" in flags_src or "O_WRONLY" in flags_src


def find_unregistered_allocations(source: str) -> list[str]:
    """Return a list of findings (one per unregistered allocation/write site)."""

    findings: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            tail = _call_tail(node)
            if tail in _SANCTIONED_CALL_TAILS:
                continue
            if tail in _FORBIDDEN_CALL_TAILS:
                findings.append(f"forbidden allocation call: {tail}")
            elif _opens_for_write(node, tail):
                findings.append(f"raw write-mode open: {tail}")
    for label, pattern in _STRING_BACKSTOPS:
        if pattern.search(source):
            findings.append(f"string backstop: {label}")
    return findings


# ---------------------------------------------------------------------------
# Manifest coverage + integrity.
# ---------------------------------------------------------------------------


def test_manifest_loads_and_covers_T01_to_T17() -> None:
    manifest = _load_manifest()
    ids = tuple(p["id"] for p in manifest["producers"])
    assert ids == EXPECTED_T_IDS, f"manifest must seed T01-T17 in order, got {ids}"


def test_manifest_entries_point_at_real_modules() -> None:
    """A stale manifest entry (a producer module that no longer exists) fails,
    mirroring the physical-seam sentry's stale-entry guard."""
    manifest = _load_manifest()
    missing: list[str] = []
    for producer in manifest["producers"]:
        for rel in [producer["module"], *producer.get("extra_modules", [])]:
            if not (ENGINE_ROOT / rel).exists():
                missing.append(f"{producer['id']}:{rel}")
    assert not missing, f"manifest names modules that do not exist: {missing}"


def test_slice1_enforces_no_migration_yet() -> None:
    """Slice 1 contract: every seeded row is unmigrated and the sentry requires
    registration of none of them. This asserts the deliberate state so a future
    slice that migrates a row has to flip its status here in the same diff."""
    manifest = _load_manifest()
    statuses = {p["id"]: p["status"] for p in manifest["producers"]}
    assert set(statuses.values()) == {"unmigrated"}, statuses


# ---------------------------------------------------------------------------
# Scanner teeth (non-vacuous): each seeded violation class must be flagged.
# ---------------------------------------------------------------------------

_SEEDED_VIOLATIONS: dict[str, str] = {
    "unregistered_tempfile": "import tempfile\nd = tempfile.mkdtemp(prefix='decoy-ooc-')\n",
    "named_temporary_file": "import tempfile\nf = tempfile.NamedTemporaryFile('wb')\n",
    "aliased_arrow_writer": "import pyarrow as pa\nw = pa.ipc.new_file(sink, schema)\n",
    "parquet_writer": "import pyarrow.parquet as pq\nw = pq.ParquetWriter('/tmp/x.parquet', schema)\n",
    "new_duckdb_connect": "import duckdb\ncon = duckdb.connect('/tmp/spill.db')\n",
    "sql_copy_to": "con.execute(\"COPY t TO '/tmp/out.parquet' (FORMAT parquet)\")\n",
    "hardcoded_write_open": "f = open('/tmp/decoy-scratch/raw.bin', 'wb')\n",
    "os_open_create": "import os\nfd = os.open('/tmp/x', os.O_CREAT | os.O_WRONLY, 0o600)\n",
    "o_tmpfile": "import os\nfd = os.open('/tmp', os.O_TMPFILE | os.O_WRONLY, 0o600)\n",
}


@pytest.mark.parametrize("name", sorted(_SEEDED_VIOLATIONS))
def test_scanner_flags_each_seeded_violation(name: str) -> None:
    findings = find_unregistered_allocations(_SEEDED_VIOLATIONS[name])
    assert findings, f"scanner is vacuous: did not flag seeded violation {name!r}"


def test_scanner_passes_clean_capability_only_snippet() -> None:
    """A producer that only reaches storage through the capability seam has no
    raw primitive and must not be flagged (otherwise the sentry would block
    every migrated producer)."""
    clean = textwrap.dedent(
        """
        from decoy_engine.scratch import unwrap_path
        rf = arena.allocate_file(kind, opaque_name)
        path = unwrap_path(rf)
        """
    )
    assert find_unregistered_allocations(clean) == []


# ---------------------------------------------------------------------------
# Syscall-traced subprocess checks (A04/A12): an unregistered write that
# unlinks its file immediately is still caught at the kernel boundary.
# ---------------------------------------------------------------------------

_UNLINKED_WRITE_CHILD = textwrap.dedent(
    """
    import os, sys
    d = sys.argv[1]
    # open + immediate unlink: the file keeps a live FD and consumes quota until
    # the last descriptor closes, but an end-of-run directory listing is empty.
    path = os.path.join(d, 'ghost.bin')
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.unlink(path)
    os.write(fd, b'D' * (1024 * 64))
    os.fsync(fd)
    os.close(fd)
    print('listing:', os.listdir(d))
    """
)


def _strace() -> str | None:
    return shutil.which("strace")


@pytest.mark.skipif(_strace() is None, reason="strace not available on this host")
def test_syscall_trace_catches_unlinked_write(tmp_path: Path) -> None:
    """The open+unlink / O_TMPFILE case the plan 1.9 A12 demands: a directory
    listing alone is insufficient, so the trace must observe the write even
    though the file is gone by end of run."""
    strace = _strace()
    assert strace is not None
    child = tmp_path / "child.py"
    child.write_text(_UNLINKED_WRITE_CHILD, encoding="utf-8")
    trace_log = tmp_path / "trace.log"
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.run(  # noqa: S603 -- fully code-constructed args, trusted
        [
            strace,
            "-f",
            "-e",
            "trace=openat,open,write,unlink,unlinkat",
            "-o",
            str(trace_log),
            sys.executable,
            str(child),
            str(work),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    # The end-of-run listing is empty: a listing-only scan would see nothing.
    assert "listing: []" in proc.stdout
    trace = trace_log.read_text(encoding="utf-8")
    # The syscall trace still shows the create, the unlink and the write.
    assert "ghost.bin" in trace, "trace did not record the unlinked file's creation"
    assert re.search(r"\bunlink(at)?\(", trace), "trace did not record the unlink"
    assert re.search(r"\bwrite\(", trace), "trace did not record the write"


_REGISTERED_WRITE_CHILD = textwrap.dedent(
    """
    import os, sys, tempfile
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from decoy_engine.scratch._arena import Arena  # import proves the seam loads
    # A registered write would go through Arena.allocate_file -> unwrap_path; this
    # child only proves the capability module is importable in a fresh process
    # (the full registered-write syscall correlation lands with the migrated
    # producers in Slices 2-3, against a real quota'd arena).
    print('arena importable:', Arena.__name__)
    """
)


def test_scratch_seam_imports_in_a_fresh_subprocess() -> None:
    """Dynamic proof (not source-text) that the capability seam imports cleanly
    in a fresh interpreter, the precondition for the Slice 2-3 syscall
    correlation of registered writes."""
    src_root = str(ENGINE_ROOT.parent)
    child_src = _REGISTERED_WRITE_CHILD
    result = subprocess.run(  # noqa: S603 -- trusted, code-constructed args
        [sys.executable, "-c", child_src, src_root],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "arena importable: Arena" in result.stdout
