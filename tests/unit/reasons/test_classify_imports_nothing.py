"""Classifying a failure must not import engine modules.

A memory-capped isolated child classifies its own failure at the run boundary. The classifier
used to import every registered family module, including `quality.dp` and with it opendp,
scikit-learn and scipy's OpenBLAS. OpenBLAS retries a failed buffer mmap forever, so a child that
had just run out of address space spun at 100% CPU instead of reporting the failure.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

from decoy_engine.reason_adapter import reason_from_exception

_CHILD = textwrap.dedent(
    """
    import sys
    from decoy_engine.reason_adapter import reason_from_exception
    from decoy_engine._progress_events import safe_error_class
    before = set(sys.modules)
    record = reason_from_exception(MemoryError())
    assert safe_error_class("DpError") == "DpError"
    roots = ("decoy_engine", "sklearn", "scipy", "opendp")
    added = sorted(m for m in set(sys.modules) - before if m.split(".")[0] in roots)
    print(record.namespace, record.code, added)
    """
)


def test_classifying_and_naming_a_memory_error_imports_no_family_module() -> None:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    namespace, code, added = proc.stdout.strip().split(" ", 2)
    assert (namespace, code) == ("decoy", "process_out_of_memory")
    assert added == "[]"


def test_a_family_exception_from_an_imported_module_still_classifies() -> None:
    from decoy_engine.quality.dp import DpError

    # An unregistered code on a family class is `unregistered_reason`; a class the adapter did not
    # recognise at all would be `unexpected_error`.
    record = reason_from_exception(DpError(code="no_such_code", message="m"))
    assert (record.namespace, record.code) == ("decoy", "unregistered_reason")
