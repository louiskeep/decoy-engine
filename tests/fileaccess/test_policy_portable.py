"""The descriptor-walk fallback confines as the `openat2` path does.

On a kernel without `openat2` (or elsewhere), `RootedFileAccess(allow_portable_fallback=True)`
opens each component with `O_NOFOLLOW` relative to the previous descriptor. The same
confinement tests run again here with the walk forced on.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from decoy_engine.fileaccess import FileAccessUnavailable, RootedFileAccess, _openat2
from tests.fileaccess.test_policy_output import (  # noqa: F401
    test_dir_transaction_into_a_non_empty_directory_fails_closed_and_keeps_the_stage,
    test_dir_transaction_publishes_atomically_into_an_absent_leaf,
    test_ensure_dir_and_emptiness,
    test_every_name_taking_operation_validates_before_any_syscall,
    test_file_exclusive_refuses_an_existing_leaf_and_replace_swaps,
)
from tests.fileaccess.test_policy_rooted import (  # noqa: F401
    test_directory_capability_children_open_only_beneath_the_original_fd,
    test_every_escape_missing_or_non_regular_is_the_one_denial,
    test_fifo_and_device_are_denied,
    test_magic_link_inside_the_root_is_denied,
    test_reads_a_regular_file_beneath_the_root,
    test_stays_readable_after_the_name_is_unlinked,
)


@pytest.fixture(autouse=True)
def _walk_instead_of_openat2(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(_openat2, "FORCE_PORTABLE", True)
    return


def test_construction_refuses_without_the_flag_when_openat2_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_openat2, "_usable", False)
    with pytest.raises(FileAccessUnavailable):
        RootedFileAccess([])
    RootedFileAccess([], allow_portable_fallback=True)
