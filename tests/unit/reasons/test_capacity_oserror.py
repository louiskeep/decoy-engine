"""The one rule for "this OSError means the disk is full", shared by the reason adapter and the
staging writer."""

from __future__ import annotations

import errno

import pytest

from decoy_engine.reason_adapter import is_capacity_oserror


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (OSError(errno.ENOSPC, "x"), True),
        (OSError(errno.EDQUOT, "x"), True),
        (OSError(errno.EIO, "x"), False),
        (OSError(errno.EACCES, "x"), False),
        (OSError("no errno"), False),
        (ValueError("x"), False),
    ],
)
def test_is_capacity_oserror(exc: BaseException, expected: bool) -> None:
    assert is_capacity_oserror(exc) is expected


def test_the_predicate_does_not_walk_the_cause_chain() -> None:
    outer = RuntimeError("wrapped")
    outer.__cause__ = OSError(errno.ENOSPC, "x")
    assert is_capacity_oserror(outer) is False
