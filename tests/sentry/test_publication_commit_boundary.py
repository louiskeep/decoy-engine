"""Sentry: only the publication backend may bind a result to a final destination.

Construction rule (F22): serializers create attempt-owned artifacts; validation creates a
sealed preparation; ONLY `decoy_engine.publication` binds a preparation to a destination;
release code resolves that binding. The structural scan below is paired with runtime spies
on real files, because banning one spelling of `os.replace` proves nothing by itself.

The allowlist is SCOPED, not a blanket ban: each entry names why that mutation is not an
approved-output publication (source snapshot, private child intermediate, standalone byte
writer, post-commit diagnostic).
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine import publication as pub
from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution._transactional_sink import ManagedDatasetSink

SRC = Path(__file__).parents[2] / "src" / "decoy_engine"

_FUNCS = {
    ("os", "replace"),
    ("os", "rename"),
    ("os", "link"),
    ("shutil", "move"),
    ("shutil", "copytree"),
    ("shutil", "copy"),
    ("shutil", "copy2"),
    ("shutil", "copyfile"),
    ("shutil", "copyfileobj"),
}

# (relative path, call) -> (expected count, category, why it is not an output publication)
ALLOWLIST: dict[tuple[str, str], tuple[int, str, str]] = {
    ("publication/_local_directory.py", "os.replace"): (
        1,
        "backend",
        "immutable_directory/v1 binding",
    ),
    ("publication/_managed.py", "os.replace"): (
        3,
        "backend",
        "managed generation promote, CURRENT switch, backup publish",
    ),
    ("publication/_managed.py", "shutil.copyfile"): (
        2,
        "backend",
        "protected backup / restore copy",
    ),
    ("publication/_retention.py", "os.rename"): (
        2,
        "backend",
        "retention move; descriptor neutralization",
    ),
    ("execution/_transactional_sink.py", "os.replace"): (
        1,
        "private_intermediate",
        "ParquetPreparationSink seals a child's private directory; never writes a descriptor",
    ),
    ("profile/_fixed_width_writer.py", "os.replace"): (
        1,
        "standalone_byte_writer",
        "single local byte file; not a dataset receipt",
    ),
    ("connectors/postgres_snapshot.py", "os.replace"): (
        1,
        "source_snapshot",
        "read-only source snapshot export, not an approved output",
    ),
    ("quarantine.py", "os.link"): (
        1,
        "post_commit_diagnostic",
        "default-mode quarantine sidecar; required sidecars use sidecars_in_unit",
    ),
}


def _scan() -> dict[tuple[str, str], int]:
    found: dict[tuple[str, str], int] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            recv = node.func.value
            call = None
            if isinstance(recv, ast.Name) and (recv.id, node.func.attr) in _FUNCS:
                call = f"{recv.id}.{node.func.attr}"
            elif node.func.attr == "rename" or (
                node.func.attr == "replace"
                and len(node.args) == 1
                and not node.keywords
                and not (isinstance(recv, ast.Name) and recv.id == "dataclasses")
            ):
                call = f"<path>.{node.func.attr}"
            if call:
                found[(rel, call)] = found.get((rel, call), 0) + 1
    return found


def test_every_final_destination_mutation_is_inventoried() -> None:
    found = _scan()
    unexpected = {k: v for k, v in found.items() if k not in ALLOWLIST}
    assert not unexpected, f"new publication-style mutation outside the backend: {unexpected}"
    stale = {k for k in ALLOWLIST if k not in found}
    assert not stale, f"allowlist entries with no call site (delete them): {stale}"
    wrong = {k: (found[k], ALLOWLIST[k][0]) for k in ALLOWLIST if found[k] != ALLOWLIST[k][0]}
    assert not wrong, f"call-site count drifted (review the new site): {wrong}"


def test_output_publication_sites_live_only_in_the_publication_backend() -> None:
    outside = [
        k
        for k, (_, cat, _why) in ALLOWLIST.items()
        if cat == "backend" and not k[0].startswith("publication/")
    ]
    assert outside == []
    non_backend_output = {cat for (_, _), (_, cat, _) in ALLOWLIST.items()} - {
        "backend",
        "private_intermediate",
        "standalone_byte_writer",
        "source_snapshot",
        "post_commit_diagnostic",
    }
    assert not non_backend_output


def test_only_the_publication_package_authors_descriptors() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("publication/"):
            continue
        text = path.read_text(encoding="utf-8")
        if "_decoy_publication" in text or "_decoy_evidence_core" in text:
            offenders.append(rel)
    assert offenders == [], f"descriptor file names referenced outside publication/: {offenders}"


# ---------------------------------------------------------------------------
# Runtime spies on real files.
# ---------------------------------------------------------------------------


class _Spy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, str, str]] = []
        real = os.replace

        def spy(src: object, dst: object) -> None:
            caller = sys._getframe(1).f_globals["__name__"]
            self.calls.append((caller, str(src), str(dst)))
            real(src, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "replace", spy)

    def into(self, destination: Path) -> list[str]:
        return [c for c, _src, dst in self.calls if Path(dst) == destination]


def test_runtime_immutable_commit_binds_through_the_backend_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _Spy(monkeypatch)
    dest = tmp_path / "out"
    sink = ParquetTransactionalSink(dest)
    sink.write("t", pa.table({"x": [1]}))
    sink.commit()
    assert spy.into(dest) == ["decoy_engine.publication._local_directory"]
    assert len(spy.calls) == 1


def test_runtime_managed_commit_uses_only_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _Spy(monkeypatch)
    dest = tmp_path / "ds"
    sink = ManagedDatasetSink(dest)
    sink.write("t", pa.table({"x": [1]}))
    sink.commit()
    assert {c for c, _, _ in spy.calls} == {"decoy_engine.publication._managed"}
    assert [Path(dst).name for _, _, dst in spy.calls][-1] == "CURRENT"


def test_runtime_isolated_fallback_publishes_only_through_the_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution._isolated_run import run_pipeline_isolated
    from tests.unit.execution import _r3_scenarios as sc

    cfg, tables = sc.two_tables(tmp_path)
    out = tmp_path / "published"
    spy = _Spy(monkeypatch)
    result = run_pipeline_isolated(
        cfg, tables, engine_version=sc.ENGINE_VERSION, output_dir=out, isolate=False
    )
    assert result.outcome == "completed"
    assert spy.into(out) == ["decoy_engine.publication._local_directory"]


def test_runtime_child_preparation_cannot_be_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution._transactional_sink import ParquetPreparationSink

    spy = _Spy(monkeypatch)
    sink = ParquetPreparationSink(tmp_path / "child")
    sink.write("t", pa.table({"x": [1]}))
    envelope = sink.commit()
    assert isinstance(envelope, pub.ChildPreparationEnvelope)
    assert [c for c, _, _ in spy.calls] == ["decoy_engine.execution._transactional_sink"]
    assert pub.inspect_destination(tmp_path / "child").kind == "legacy"  # no binding, no descriptor
    assert pub.lookup_committed(pub.PublicationRequest(destination=tmp_path / "child")) is None
    assert not hasattr(envelope, "outcome")  # not a CommitReceipt: nothing accepts it as one


def test_runtime_staging_and_retained_data_never_resolve_as_committed(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    sink = ParquetTransactionalSink(
        dest,
        request=pub.PublicationRequest(destination=dest),
    )
    sink.write("t", pa.table({"x": [1]}))
    sink.record_validation([pub.CheckResult("c", "fail")])
    with pytest.raises(pub.PublicationRefusedError):
        sink.commit()
    (staged,) = tmp_path.glob("_decoy_stage_*")
    assert pub.lookup_committed(pub.PublicationRequest(destination=staged)) is None
    assert pub.inspect_destination(dest).kind == "absent"
    sink.abort()
