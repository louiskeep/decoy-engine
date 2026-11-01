"""`FileAccessDenied` has a registry identity, so a denial crosses a boundary as a code, not text."""

from __future__ import annotations

from decoy_engine import reasons
from decoy_engine.fileaccess import FileAccessDenied
from decoy_engine.reason_adapter import reason_from_exception


def test_a_denial_maps_to_its_registered_reason() -> None:
    record = reason_from_exception(FileAccessDenied("sources.t.path"))
    assert (record.namespace, record.code) == ("decoy", "file_access_denied")
    assert reasons.find("decoy", "file_access_denied") is not None
    assert "sources.t.path" not in repr(record)


def test_a_subclass_or_other_class_is_not_the_denial() -> None:
    record = reason_from_exception(PermissionError("file access denied: x"))
    assert (record.namespace, record.code) != ("decoy", "file_access_denied")
