"""AT-E3/AT-E11 for the lazy readers (B6b): authorization precedes every read, and the
authorized handle lives exactly as long as the batches that read it."""

from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.fileaccess import FileAccessDenied
from decoy_engine.profile._readers import LazySource
from tests.fileaccess.support import RecordingFileAccess, RootRelative


def _open_fds(root: Path) -> int:
    """Descriptors that point into the test tree: unrelated interpreter fds come and go."""
    count = 0
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        count += target.startswith(str(root))
    return count


@pytest.fixture
def tree(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    table = pa.table({"a": list(range(50)), "b": [f"v{i}" for i in range(50)]})
    inside_file = root / "s.parquet"
    pq.write_table(table, inside_file, row_group_size=10)
    outside_file = outside / "s.parquet"
    pq.write_table(table, outside_file)
    policy = RecordingFileAccess(RootRelative(root))
    yield root, inside_file, outside_file, policy
    policy.inner.close()  # type: ignore[attr-defined]


def test_every_lazy_operation_asks_the_policy_before_it_reads(tree) -> None:  # type: ignore[no-untyped-def]
    _root, _inside, outside_file, policy = tree
    lazy = LazySource(outside_file, access=policy)
    operations = {
        "footer_facts": lambda: lazy.footer_facts(),
        "schema": lambda: lazy.schema,
        "num_rows": lambda: lazy.num_rows,
        "column_null_counts": lambda: lazy.column_null_counts(),
        "to_table": lambda: lazy.to_table(),
        "iter_batches": lambda: next(iter(lazy.iter_batches(5))),
        "open_batches": lambda: lazy.open_batches(5),
    }
    for name, call in operations.items():
        before = len(policy.requests)
        with pytest.raises(FileAccessDenied):
            call()
        assert len(policy.requests) == before + 1, name
        assert policy.requests[-1][3] == "denied", name


def test_a_granted_lazy_source_reads_through_one_request_per_operation(tree) -> None:  # type: ignore[no-untyped-def]
    _root, inside, _outside, policy = tree
    lazy = LazySource(inside, access=policy)
    assert lazy.num_rows == 50
    assert lazy.footer_facts().num_rows == 50
    assert lazy.to_table().num_rows == 50
    assert sum(b.num_rows for b in lazy.iter_batches(7)) == 50
    assert all(r[3] == "granted" for r in policy.requests) and len(policy.requests) == 4


def test_the_handle_outlives_a_rename_and_is_closed_with_the_batches(tree) -> None:  # type: ignore[no-untyped-def]
    root, inside, _outside, policy = tree
    lazy = LazySource(inside, access=policy)
    baseline = _open_fds(root)
    opened = lazy.open_batches(10)
    first = next(opened.batches)
    os.replace(inside, root / "moved.parquet")
    os.symlink(root / "moved.parquet", inside)
    rest = sum(b.num_rows for b in opened.batches)
    assert first.num_rows + rest == 50
    assert _open_fds(root) > baseline
    opened.close()
    opened.close()
    assert _open_fds(root) == baseline


def test_an_interrupted_consumption_still_releases_the_handle(tree) -> None:  # type: ignore[no-untyped-def]
    root, inside, _outside, policy = tree
    lazy = LazySource(inside, access=policy)
    baseline = _open_fds(root)
    opened = lazy.open_batches(10)
    next(opened.batches)
    opened.close()
    assert _open_fds(root) == baseline
    gen = lazy.iter_batches(10)
    next(gen)
    gen.close()
    assert _open_fds(root) == baseline
