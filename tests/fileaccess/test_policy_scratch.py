"""Engine scratch files are readable only through a reader issued over the engine's own
directory; no public flag turns an arbitrary path into a policy-free read."""

from __future__ import annotations

import inspect
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution.out_of_core import _scratch
from decoy_engine.execution.out_of_core._stage import MaskedKeyStager
from decoy_engine.fileaccess import FileAccessDenied
from decoy_engine.profile._readers import LazySource
from tests.fileaccess.support import RootRelative


def test_lazy_source_has_no_scratch_switch() -> None:
    assert "scratch" not in inspect.signature(LazySource).parameters
    with pytest.raises(TypeError):
        LazySource(Path("x.parquet"), scratch=True)  # type: ignore[call-arg]


def test_a_strict_policy_cannot_be_bypassed_through_a_lazy_source(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.parquet"
    pq.write_table(pa.table({"a": [1]}), outside)
    policy = RootRelative(root)
    try:
        with pytest.raises(FileAccessDenied):
            LazySource(outside, access=policy).to_table()
    finally:
        policy.close()


def test_the_stager_source_reads_its_own_file_and_nothing_else(tmp_path: Path) -> None:
    schema = pa.schema([pa.field("k", pa.int64())])
    stager = MaskedKeyStager(tmp_path / "staged" / "t.parquet", columns=("k",), fixed_schema=schema)
    batch = pa.record_batch([pa.array([1, 2, 3])], schema=schema)
    stager.add(batch, batch)
    stager.close()
    assert stager.source().to_table().num_rows == 3
    elsewhere = tmp_path / "elsewhere.parquet"
    pq.write_table(pa.table({"k": [9]}), elsewhere)
    with pytest.raises(FileAccessDenied):
        LazySource(
            elsewhere, access=_scratch.scratch_access_for(tmp_path / "staged"), field_key="x"
        ).to_table()
    with pytest.raises(FileAccessDenied):
        LazySource(
            tmp_path / "staged" / ".." / "elsewhere.parquet",
            access=_scratch.scratch_access_for(tmp_path / "staged"),
        ).to_table()


def test_a_reader_cannot_be_constructed_without_the_issuer(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        _scratch.ScratchAccess(tmp_path, issuer=object())
