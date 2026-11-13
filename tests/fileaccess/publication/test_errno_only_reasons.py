"""The four errno-only file-access errors map to registered `decoy` reasons.

`CommitUncertain`, `LockUnavailable` and `LockTimeout` each have their own pending publication
code; `LeafTooLarge` is a bounded read over its cap and maps to `decoy.publication_failed`. None of
them sits in the reason sentry's `UNCLASSIFIED` list.
"""

from __future__ import annotations

import errno

import pytest

from decoy_engine import reasons
from decoy_engine.fileaccess import _types
from decoy_engine.reason_adapter import reason_from_exception
from tests.sentry.test_reason_registry import UNCLASSIFIED

EXPECTED = {
    "CommitUncertain": ("publication_commit_uncertain", True),
    "LockUnavailable": ("publication_lock_unavailable", True),
    "LockTimeout": ("publication_lock_timeout", True),
    "LeafTooLarge": ("publication_failed", False),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_errno_only_error_has_a_registered_reason(name: str) -> None:
    code, pending = EXPECTED[name]
    spec = reasons.find("decoy", code)
    assert spec is not None
    assert (spec.since == reasons.UNRELEASED) is pending
    record = reason_from_exception(getattr(_types, name)(errno.EIO))
    assert (record.namespace, record.code) == ("decoy", code)
    assert name not in UNCLASSIFIED
