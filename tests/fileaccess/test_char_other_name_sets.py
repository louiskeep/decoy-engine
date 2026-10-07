"""E1-0 characterization: the output name sets of the other writers (isolated commit staging,
subset materialization, quarantine output paths), pinned as they behave today."""

from __future__ import annotations

import errno
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import _isolated_commit as commit

TABLE = pa.table({"a": [1, 2]})


@pytest.mark.parametrize("name", ["orders", "a b", "x.y", "a\\b", "Ünï", "..x", "x.."])
def test_isolated_staging_accepts_these_names(tmp_path: Path, name: str) -> None:
    staging = tmp_path / "stage"
    commit.stage_tables({name: TABLE}, staging)
    assert [p.name for p in staging.iterdir()] == [f"{name}.parquet"]


def test_isolated_staging_does_not_validate_separators(tmp_path: Path) -> None:
    # A separator name nests a path under staging: it fails because the subdirectory is absent.
    staging = tmp_path / "stage"
    with pytest.raises(OSError):
        commit.stage_tables({"a/b": TABLE}, staging)
    assert list(staging.iterdir()) == []


def test_isolated_commit_is_a_single_rename_and_refuses_a_non_empty_target(tmp_path: Path) -> None:
    staging = tmp_path / "stage"
    commit.stage_tables({"t": TABLE}, staging)
    out = tmp_path / "deep" / "out"
    commit.atomic_commit(staging, out)
    assert [p.name for p in out.iterdir()] == ["t.parquet"]
    assert not staging.exists()
    staging2 = tmp_path / "stage2"
    commit.stage_tables({"u": TABLE}, staging2)
    with pytest.raises(commit.CommitError) as info:
        commit.atomic_commit(staging2, out)
    assert isinstance(info.value.__cause__, OSError)
    assert info.value.__cause__.errno in (errno.ENOTEMPTY, errno.EEXIST)
    assert [p.name for p in out.iterdir()] == ["t.parquet"]
    assert staging2.exists()


def test_isolated_row_errors_and_envelope_read_back_degrade_to_empty(tmp_path: Path) -> None:
    assert commit.read_staged_row_errors(tmp_path) == ()
    (tmp_path / "row_errors.json").write_text("not json")
    assert commit.read_staged_row_errors(tmp_path) == ()
    assert commit.read_envelope(tmp_path / "missing.json") is None
    (tmp_path / "env.json").write_text("{bad")
    assert commit.read_envelope(tmp_path / "env.json") is None
    (tmp_path / "ok.json").write_text('{"k": 1}')
    assert commit.read_envelope(tmp_path / "ok.json") == {"k": 1}


def test_staging_dir_is_a_sibling_of_the_output_dir(tmp_path: Path) -> None:
    out = tmp_path / "o" / "out"
    stage = commit.sibling_staging_dir(out)
    assert stage.parent == out.parent
    assert stage.name.startswith("_decoy_isolated_stage_")
    assert (
        commit.resolve_staging_output_dir(tmp_path / "work", None)
        == tmp_path / "work" / "staging_output"
    )


def test_parquet_roundtrip_of_staged_table(tmp_path: Path) -> None:
    commit.stage_tables({"t": TABLE}, tmp_path / "s")
    assert pq.read_table(tmp_path / "s" / "t.parquet").equals(TABLE)
