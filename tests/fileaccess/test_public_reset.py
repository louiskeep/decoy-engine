"""`fileaccess.reset_for_tests` is the public test-isolation hook hosts use instead of `_entry`."""

from __future__ import annotations

import pytest

from decoy_engine import fileaccess
from decoy_engine.fileaccess import FileAccessUnavailable, _entry


def test_reset_lifts_both_latches() -> None:
    fileaccess.forbid_ambient()
    _entry._strict_in_use = True
    try:
        with pytest.raises(FileAccessUnavailable):
            fileaccess.current()
        fileaccess.reset_for_tests()
        assert not fileaccess.ambient_forbidden()
        assert fileaccess.is_ambient()
        assert fileaccess.current() is not None
    finally:
        fileaccess.reset_for_tests()


def test_it_is_public() -> None:
    assert "reset_for_tests" in fileaccess.__all__
