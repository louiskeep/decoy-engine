"""Fixtures for the file-access tests: pre-opened root directories and a rooted policy."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from decoy_engine.fileaccess import FileRole, Root, RootedFileAccess
from tests.fileaccess.x_support import xworld  # noqa: F401 - registers the fixture

READ_ROLES = frozenset(
    {
        FileRole.SOURCE,
        FileRole.REFERENCE,
        FileRole.CORPUS,
        FileRole.SNAPSHOT,
        FileRole.LOOKUP,
        FileRole.SECRET,
        FileRole.VAULT,
        FileRole.CONFIG,
        FileRole.SUBSET_INPUT,
        FileRole.CUSTOM_PROVIDER_ROOT,
        FileRole.MODEL_PACK_ROOT,
    }
)
WRITE_ROLES = frozenset(
    {FileRole.TARGET_OUTPUT, FileRole.QUARANTINE_OUTPUT, FileRole.VAULT, FileRole.SPILL_HINT}
)


@pytest.fixture
def root_dir(short_tmp_path: Path) -> Path:
    """The confinement root, kept short: `test_fifo_and_device_are_denied` binds a Unix socket
    in it, and modules that import that test (the portable walk) get this fixture, not the
    override in test_policy_rooted. A long basetemp would overflow the AF_UNIX limit."""
    root = short_tmp_path / "root"
    root.mkdir()
    return root


@pytest.fixture
def rooted(root_dir: Path) -> Iterator[RootedFileAccess]:
    fd = os.open(root_dir, os.O_RDONLY | os.O_DIRECTORY)
    policy = RootedFileAccess(
        [
            Root(
                "main",
                fd,
                READ_ROLES | WRITE_ROLES,
                frozenset({"read", "stat", "read_dir", "write"}),
            )
        ]
    )
    try:
        yield policy
    finally:
        os.close(fd)
