"""`test_isolated_parent_measures` (plan 5.7, 7.2; class 6, untrusted producer; slice c2).

The isolated child is an untrusted producer: the parent establishes the staged set itself, from
the envelope's table names checked against what the stage actually holds, and refuses a
child-written `_decoy_publication.json`, an extra file, a nested directory, a symlink, a
missing table or a table name that is not one path component. Nothing is bound and no stage is
left. Sizes and digests in the receipt and descriptor come from the parent's own measurement
of the bound bytes. The unmodified child stages plain Parquet files only: no descriptor, no
lock file and no nested unit (V2-5).

Red before c2 (start commit `f9054196`): the parent renames whatever the child staged into
`output_dir`, planted entries included, with no descriptor of its own and no receipt; the
streamed child binds its own unit with a descriptor in the stage and leaves a lock file beside it.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from decoy_engine.execution import _isolated_worker, run_pipeline_isolated
from decoy_engine.publication import DESCRIPTOR_NAME, PublicationRequest
from tests.unit.execution.publication._isolated_support import (
    ENGINE_VERSION,
    entries,
    install_fake_worker,
    job,
    sources,
)
from tests.unit.publication._publish_support import descriptor, stages_left

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX isolated route")

PLANTS = ["descriptor", "extra_file", "nested_dir", "symlink", "missing_table", "bad_name"]


@pytest.mark.parametrize("plant", PLANTS)
def test_isolated_parent_measures(
    plant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = install_fake_worker(tmp_path, monkeypatch, plant=plant)
    cfg = job(tmp_path, 3)
    out = tmp_path / "pub" / "out"
    result = run_pipeline_isolated(
        cfg,
        sources(cfg),
        engine_version=ENGINE_VERSION,
        output_dir=out,
        request=PublicationRequest("op-c2"),
    )
    assert marker.read_text() == plant  # non-vacuity: the fake child ran and planted
    assert result.outcome == "crashed" and result.outputs is None
    assert result.committed_output_dir is None
    assert not out.exists()  # nothing of the child's stage was bound
    receipt = result.publication_receipt
    assert receipt is not None
    assert (receipt.outcome, receipt.reason_code) == ("failed", "publication_failed")
    assert (receipt.visible, receipt.durable) == (False, False)
    # refused by the parent's admission, before anything was measured or a descriptor written
    assert receipt.result_id is None and receipt.artifacts == ()
    assert receipt.os_error == ("ELOOP" if plant == "symlink" else None)
    assert result.reason is not None and result.reason.code == "publication_failed"
    assert stages_left(out.parent) == []


def test_isolated_parent_measures_the_bound_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fake child with nothing planted is published; every size and digest is the parent's
    measurement of the bound bytes, and the descriptor is the parent's (its request's id)."""
    marker = install_fake_worker(tmp_path, monkeypatch)
    cfg = job(tmp_path, 3)
    out = tmp_path / "pub" / "out"
    result = run_pipeline_isolated(
        cfg,
        sources(cfg),
        engine_version=ENGINE_VERSION,
        output_dir=out,
        request=PublicationRequest("op-measured"),
    )
    assert marker.read_text() == "none"  # non-vacuity: the fake child staged the table
    assert result.outcome == "completed", result.error
    receipt = result.publication_receipt
    assert receipt is not None and receipt.outcome == "published"
    assert entries(out) == [DESCRIPTOR_NAME, "t.parquet"]
    data = (out / "t.parquet").read_bytes()
    (artifact,) = receipt.artifacts
    assert (artifact.size, artifact.sha256, artifact.rows) == (
        len(data),
        hashlib.sha256(data).hexdigest(),
        2,
    )
    bound = descriptor(out)
    assert bound["operation_id"] == "op-measured"
    assert [(a["path"], a["sha256"]) for a in bound["artifacts"]] == [
        ("t.parquet", artifact.sha256)
    ]
    assert hashlib.sha256((out / DESCRIPTOR_NAME).read_bytes()).hexdigest() == (
        receipt.descriptor_digest
    )


def test_unmodified_child_stages_plain_files_only(tmp_path: Path) -> None:
    """The real worker on a streamed route: its stage holds the Parquet tables and nothing else,
    and nothing appears beside it (no lock file, no nested unit, no descriptor)."""
    from tests.unit.execution.test_isolated_worker_streaming import _fk_config

    config, manifest = _fk_config(tmp_path)
    holder = tmp_path / "holder"
    holder.mkdir()
    stage = holder / "stage"
    stage.mkdir()
    payload = {
        "config": config,
        "sources": manifest,
        "kwargs": {"execution_mode": "out_of_core", "engine_version": ENGINE_VERSION},
        "mem_cap_bytes": None,
        "rlimit_kind": "data",
        "staging_output_dir": str(stage),
    }
    envelope = _isolated_worker._run(payload)
    assert envelope["outcome"] == "completed"
    assert envelope["quality_metrics"]["execution"]["outputs_streamed"] is True  # non-vacuity
    assert sorted(envelope["staged_tables"]) == ["child", "parent"]
    assert entries(stage) == ["child.parquet", "parent.parquet"]
    assert entries(holder) == ["stage"]


def test_isolated_publish_leaves_only_the_unit_and_its_lock(tmp_path: Path) -> None:
    """End to end with the real child: the destination's parent holds the unit and the one
    destination lock file, and the unit holds the tables and the parent's descriptor."""
    cfg = job(tmp_path, 4)
    parent = tmp_path / "pub"
    out = parent / "out"
    result = run_pipeline_isolated(cfg, sources(cfg), engine_version=ENGINE_VERSION, output_dir=out)
    assert result.outcome == "completed", result.error
    assert result.publication_receipt is not None  # non-vacuity: published through the parent
    assert result.publication_receipt.outcome == "published"
    assert entries(parent) == [".out.decoy-lock", "out"]
    assert entries(out) == [DESCRIPTOR_NAME, "t.parquet"]
