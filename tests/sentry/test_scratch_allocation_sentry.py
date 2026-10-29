"""F25 scratch allocation sentry (plan 2).

The runtime choke-point is the ScratchManager / attempt arena: a temporary
producer may obtain a writable path only by holding a ``RegisteredFile`` /
``NativeDirectory`` / ``PublicationStage`` capability, allocated after durable
registration. This sentry is the drift guard that keeps that true as the seam
grows, modelled on ``tests/sentry/test_physical_seam_disconnection.py``.

Three complementary checks (plan 2):

1. An AST + string scan flags each class of unregistered allocation (so the
   scanner is proven non-vacuous on seeded snippets, and a clean capability-only
   snippet passes).
2. Per migrated producer (static drift guard): the module must route its
   temporary work through the capability seam (it imports and calls one of the
   allocation wrappers), it may hold no raw ``tempfile`` / ``mkdtemp`` call
   outside an explicit call-site exemption, and no forbidden-primitive write or
   ``COPY ... TO`` may carry a hardcoded absolute path. A stale manifest entry
   fails.
3. A real out-of-core job is run in a fresh subprocess under ``strace`` for both
   inner lanes, and every data write it makes is asserted to land under the
   attempt arena directory. Seeding one unregistered producer write makes that
   check fail, so it is non-vacuous. This is the real enforcement; the static
   checks are the drift guard (plan 2: static analysis is a drift guard, not a
   proof against arbitrary dynamic Python).

Slice 3 state: T01-T17 are all ``migrated`` and enforced here (Slice 2 migrated
T01-T08, Slice 3 the rest). A whole-module exclusion is never allowed; the only
exemptions are the exact durable-output sinks the plan permits (direct
quarantine JSONL, model-pack output, rotating logs) and the one stage-root
container call, each named with a rationale in the manifest.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
ENGINE_ROOT = REPO_ROOT / "src" / "decoy_engine"
MANIFEST_PATH = HERE / "scratch_producer_manifest.json"
TRACE_CHILD = HERE / "_f25_ooc_trace_child.py"

EXPECTED_T_IDS = tuple(f"T{n:02d}" for n in range(1, 18))
MIGRATED_IDS = frozenset(f"T{n:02d}" for n in range(1, 18))


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Check 1: AST + string scanner for unregistered allocation / write primitives.
# ---------------------------------------------------------------------------

_FORBIDDEN_CALL_TAILS: frozenset[str] = frozenset(
    {
        "tempfile.mkdtemp",
        "tempfile.mkstemp",
        "tempfile.NamedTemporaryFile",
        "tempfile.TemporaryDirectory",
        "os.mkdtemp",
        "pa.OSFile",
        "pa.ipc.new_file",
        "pa.ipc.new_stream",
        "pa.RecordBatchFileWriter",
        "pq.ParquetWriter",
        "pq.write_table",
        "pyarrow.parquet.ParquetWriter",
        "duckdb.connect",
    }
)

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

# The sanctioned allocation wrappers a migrated producer must reach its path
# through (plan 2 check 2: each allowed producer has an allocation wrapper).
_SEAM_FUNCS: frozenset[str] = frozenset(
    {
        "ensure_scratch",
        "edge_work_dir",
        "unwrap_path",
        "unwrap_dir",
        "native_dir_path",
        "as_native_dir",
        "adopt_file",
        "allocate_file",
        "allocate_native_dir",
        # Slice 3 generic seam helpers (decoy_engine.scratch._seam).
        "ensure_native_dir",
        "ensure_publication_stage",
        "ensure_publication_file",
        "ensure_scratch_file",
    }
)

_WRITE_MODE_RE = re.compile(r"[wxa+]")
_COPY_TO_RE = re.compile(r"\bCOPY\b.+\bTO\b", re.IGNORECASE | re.DOTALL)
# An absolute-path literal baked into a write target or SQL string: the drift
# this static guard catches. A path threaded in from a variable (anchored at the
# caller) is verified at runtime by the syscall trace, not here.
_ABS_PATH_LITERAL_RE = re.compile(r"""['"]?/[A-Za-z0-9_.]""")
_STRING_BACKSTOPS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sql_copy_to", _COPY_TO_RE),
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
    name = tail.split(".")[-1]
    if name != "open":
        return False
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
    flags_src = ast.dump(node)
    return "O_CREAT" in flags_src or "O_TMPFILE" in flags_src or "O_WRONLY" in flags_src


def find_unregistered_allocations(source: str) -> list[str]:
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
# AST helpers for the per-migrated-producer drift guard.
# ---------------------------------------------------------------------------


def _literal_text(expr: ast.expr | None) -> str:
    """Concatenate every string literal reachable in ``expr`` (constants and
    f-string segments), so an absolute-path literal baked anywhere in a write
    target or SQL string is visible."""
    if expr is None:
        return ""
    out: list[str] = []
    for node in ast.walk(expr):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return " ".join(out)


def _has_hardcoded_path(expr: ast.expr | None) -> bool:
    return bool(_ABS_PATH_LITERAL_RE.search(_literal_text(expr)))


def _write_sites(tree: ast.Module) -> list[tuple[ast.Call, ast.expr | None]]:
    """Forbidden-primitive WRITE calls and their path argument."""
    sites: list[tuple[ast.Call, ast.expr | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        tail = _call_tail(node)
        leaf = tail.split(".")[-1]
        if tail in {
            "pq.ParquetWriter",
            "pyarrow.parquet.ParquetWriter",
            "pa.ipc.new_file",
            "pa.ipc.new_stream",
            "pa.RecordBatchFileWriter",
        }:
            sites.append((node, node.args[0] if node.args else None))
        elif tail == "pq.write_table":
            sites.append((node, node.args[1] if len(node.args) > 1 else None))
        elif tail == "pa.OSFile":
            mode = node.args[1] if len(node.args) > 1 else None
            if (
                isinstance(mode, ast.Constant)
                and isinstance(mode.value, str)
                and _WRITE_MODE_RE.search(mode.value)
            ):
                sites.append((node, node.args[0] if node.args else None))
        elif leaf == "open" and _opens_for_write(node, tail):
            sites.append((node, node.args[0] if node.args else None))
    return sites


def _copy_to_sites(tree: ast.Module) -> list[ast.expr]:
    """SQL strings passed to a ``.execute(...)`` call that render a DuckDB
    ``COPY ... TO``. Restricted to execute arguments so a module/function
    docstring that merely uses the words "copy" and "to" in prose is never a
    site; only real SQL is checked for a hardcoded path target.
    """
    sites: list[ast.expr] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "execute" or not node.args:
            continue
        sql = node.args[0]
        if _COPY_TO_RE.search(_literal_text(sql)):
            sites.append(sql)
    return sites


def _seam_calls(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            leaf = _call_tail(node).split(".")[-1]
            if leaf in _SEAM_FUNCS:
                out.add(leaf)
    return out


def _raw_temp_calls(tree: ast.Module) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            tail = _call_tail(node)
            if tail.startswith("tempfile.") or tail == "os.mkdtemp":
                out.append(tail)
    return out


def _module_source(rel: str) -> str:
    return (ENGINE_ROOT / rel).read_text(encoding="utf-8")


def _exemptions_for(manifest: dict, rel: str) -> set[str]:
    return {e["symbol"] for e in manifest.get("exemptions", []) if e["module"] == rel}


def _migrated_modules(manifest: dict) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in manifest["producers"]:
        if p["status"] != "migrated":
            continue
        for m in [p["module"], *p.get("extra_modules", [])]:
            if m not in seen:
                seen.add(m)
                out.append(m)
    return out


# ---------------------------------------------------------------------------
# Manifest coverage + integrity.
# ---------------------------------------------------------------------------


def test_manifest_loads_and_covers_T01_to_T17() -> None:
    manifest = _load_manifest()
    ids = tuple(p["id"] for p in manifest["producers"])
    assert ids == EXPECTED_T_IDS, f"manifest must seed T01-T17 in order, got {ids}"


def test_manifest_entries_point_at_real_modules() -> None:
    manifest = _load_manifest()
    missing: list[str] = []
    for producer in manifest["producers"]:
        for rel in [producer["module"], *producer.get("extra_modules", [])]:
            if not (ENGINE_ROOT / rel).exists():
                missing.append(f"{producer['id']}:{rel}")
    assert not missing, f"manifest names modules that do not exist: {missing}"


def test_every_producer_is_migrated_T01_to_T17() -> None:
    """Slice 3 completion: every T row is enforced; no `unmigrated` row remains,
    so the sentry has teeth over the whole inventory (plan Slice 3 gate)."""
    manifest = _load_manifest()
    status = {p["id"]: p["status"] for p in manifest["producers"]}
    migrated = {tid for tid, s in status.items() if s == "migrated"}
    assert migrated == MIGRATED_IDS, migrated
    assert all(s == "migrated" for s in status.values()), status


def test_migrated_rows_carry_allocation_and_witness() -> None:
    manifest = _load_manifest()
    for p in manifest["producers"]:
        if p["status"] == "migrated":
            assert p.get("allocation"), f"{p['id']} migrated without an allocation wrapper"
            assert p.get("witness"), f"{p['id']} migrated without an acceptance witness"


# ---------------------------------------------------------------------------
# Check 1: scanner teeth (non-vacuous).
# ---------------------------------------------------------------------------

_SEEDED_VIOLATIONS: dict[str, str] = {
    "unregistered_tempfile": "import tempfile\nd = tempfile.mkdtemp(prefix='decoy-ooc-')\n",
    "named_temporary_file": "import tempfile\nf = tempfile.NamedTemporaryFile('wb')\n",
    "aliased_arrow_writer": "import pyarrow as pa\nw = pa.ipc.new_file(sink, schema)\n",
    "arrow_stream_writer": "import pyarrow as pa\nw = pa.ipc.new_stream('/tmp/x.arrow', schema)\n",
    "parquet_writer": "import pyarrow.parquet as pq\nw = pq.ParquetWriter('/tmp/x.parquet', schema)\n",
    "parquet_write_table": "import pyarrow.parquet as pq\npq.write_table(t, '/tmp/x.parquet')\n",
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
    clean = textwrap.dedent(
        """
        from decoy_engine.scratch import unwrap_path
        rf = arena.allocate_file(kind, opaque_name)
        path = unwrap_path(rf)
        """
    )
    assert find_unregistered_allocations(clean) == []


# ---------------------------------------------------------------------------
# Check 2: every migrated producer routes its writes through the capability seam.
# ---------------------------------------------------------------------------


def test_migrated_producers_use_the_capability_seam() -> None:
    manifest = _load_manifest()
    offenders: list[str] = []
    for rel in _migrated_modules(manifest):
        if not _seam_calls(ast.parse(_module_source(rel))):
            offenders.append(rel)
    assert not offenders, f"migrated producers do not call the capability seam: {offenders}"


def test_migrated_producers_have_no_raw_tempfile_calls() -> None:
    manifest = _load_manifest()
    offenders: list[str] = []
    for rel in _migrated_modules(manifest):
        allowed = _exemptions_for(manifest, rel)
        for tail in _raw_temp_calls(ast.parse(_module_source(rel))):
            if tail not in allowed:
                offenders.append(f"{rel}:{tail}")
    assert not offenders, f"migrated producers hold unexempted raw temp calls: {offenders}"


def test_migrated_producers_have_no_hardcoded_write_target() -> None:
    """A migrated producer may not bake an absolute path into a ParquetWriter /
    Arrow IPC / write_table / write-mode open, nor into a COPY ... TO: those are
    the hardcoded-path bypass the static guard catches. Legitimate targets are
    threaded in from an arena capability and verified by the syscall trace."""
    manifest = _load_manifest()
    offenders: list[str] = []
    for rel in _migrated_modules(manifest):
        tree = ast.parse(_module_source(rel))
        for node, path_expr in _write_sites(tree):
            if _has_hardcoded_path(path_expr):
                offenders.append(f"{rel}:{_call_tail(node)}@L{node.lineno}")
        for copy_node in _copy_to_sites(tree):
            if _has_hardcoded_path(copy_node):
                offenders.append(f"{rel}:COPY...TO@L{copy_node.lineno}")
    assert not offenders, f"migrated producers write to hardcoded paths: {offenders}"


def test_hardcoded_path_detector_is_non_vacuous() -> None:
    bad = ast.parse(
        "import pyarrow.parquet as pq\npq.write_table(t, '/tmp/leak.parquet')\n"
        "con.execute(\"COPY t TO '/tmp/leak2.parquet' (FORMAT parquet)\")\n"
    )
    assert any(_has_hardcoded_path(p) for _, p in _write_sites(bad))
    assert any(_has_hardcoded_path(c) for c in _copy_to_sites(bad))
    good = ast.parse(
        "import pyarrow.parquet as pq\n"
        "from decoy_engine.scratch import unwrap_path\n"
        "def f(rf):\n"
        "    path = unwrap_path(rf)\n"
        "    pq.write_table(t, path)\n"
    )
    assert not any(_has_hardcoded_path(p) for _, p in _write_sites(good))


def test_seeded_bypass_in_a_migrated_producer_is_caught() -> None:
    """Non-vacuous proof over the Slice-3 set: inject a raw `mkdtemp` and a
    hardcoded write path into a newly migrated producer's source and confirm the
    per-producer drift guard flags both (plan Slice 3: seed a bypass for one
    migrated producer and confirm the sentry fails)."""
    rel = "execution/_transactional_sink.py"
    # The real module passes; a seeded bypass appended to it must not.
    tree_clean = ast.parse(_module_source(rel))
    assert not _raw_temp_calls(tree_clean)
    assert not any(_has_hardcoded_path(p) for _, p in _write_sites(tree_clean))

    bypassed = _module_source(rel) + (
        "\n\ndef _f25_sentry_bypass(t):\n"
        "    import tempfile\n"
        "    _d = tempfile.mkdtemp(prefix='_decoy_stage_')\n"
        "    import pyarrow.parquet as pq\n"
        "    pq.write_table(t, '/tmp/f25_sentry_leak.parquet')\n"
    )
    tree = ast.parse(bypassed)
    assert _raw_temp_calls(tree), "seeded raw tempfile call was not caught"
    assert any(_has_hardcoded_path(p) for _, p in _write_sites(tree)), (
        "seeded hardcoded write path was not caught"
    )


# ---------------------------------------------------------------------------
# Check 3: syscall-traced real OOC run -- every data write is under the arena.
# ---------------------------------------------------------------------------

_CREATE_RE = re.compile(r'openat\([^,]+,\s*"([^"]+)"[^)]*\bO_CREAT\b')


def _strace() -> str | None:
    return shutil.which("strace")


def _run_traced(base: Path, lane: str, *, bypass: bool) -> tuple[str, str]:
    base.mkdir(parents=True, exist_ok=True)
    trace_log = base / "trace.log"
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(base)}
    if bypass:
        env["DECOY_F25_SEED_BYPASS"] = "1"
    strace = _strace()
    assert strace is not None
    proc = subprocess.run(  # noqa: S603 -- fully code-constructed args, trusted
        [
            strace,
            "-f",
            "-qq",
            "-e",
            "trace=openat,creat,renameat,renameat2",
            "-o",
            str(trace_log),
            sys.executable,
            str(TRACE_CHILD),
            str(REPO_ROOT),
            str(base),
            lane,
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.stdout + "\n" + proc.stderr, trace_log.read_text(encoding="utf-8", errors="replace")


def _creates_under_work(trace: str, work: Path) -> list[str]:
    work_str = str(work.resolve())
    hits: list[str] = []
    for match in _CREATE_RE.finditer(trace):
        path = match.group(1)
        if path.startswith(work_str + "/"):
            hits.append(path)
    return hits


@pytest.mark.skipif(_strace() is None, reason="strace not available on this host")
@pytest.mark.parametrize("lane", ["batch", "reorder"])
def test_syscall_trace_data_writes_are_under_the_arena(tmp_path: Path, lane: str) -> None:
    """A04/A12 teeth: run a real OOC job (both inner lanes) with a sink, forcing
    spill, and prove every file it creates under the OOC temp root lives inside a
    registered ``.decoy-arena-*`` directory -- no producer writes to a raw path."""
    base = tmp_path / lane
    stdout, trace = _run_traced(base, lane, bypass=False)
    assert "OOC_RUN_OK" in stdout, f"child did not finish the {lane} run:\n{stdout}"
    creates = _creates_under_work(trace, base / "work")
    assert creates, "trace recorded no data writes under the OOC temp root (vacuous)"
    unregistered = [p for p in creates if ".decoy-arena-" not in p]
    assert not unregistered, f"{lane} lane wrote data outside any arena: {unregistered[:5]}"


@pytest.mark.skipif(_strace() is None, reason="strace not available on this host")
def test_syscall_trace_catches_a_seeded_bypass() -> None:
    """Non-vacuous proof: seeding one unregistered producer write makes the
    under-arena check fail, so the trace has teeth."""
    base = Path(tempfile.mkdtemp(prefix="f25-sentry-bypass-")) / "batch"
    stdout, trace = _run_traced(base, "batch", bypass=True)
    assert "OOC_RUN_OK" in stdout, f"seeded child did not finish:\n{stdout}"
    creates = _creates_under_work(trace, base / "work")
    unregistered = [p for p in creates if ".decoy-arena-" not in p]
    assert any("F25_SENTRY_BYPASS_LEAK.bin" in p for p in unregistered), (
        "the seeded leak was not caught by the under-arena check; "
        f"unregistered writes seen: {unregistered[:5]}"
    )
