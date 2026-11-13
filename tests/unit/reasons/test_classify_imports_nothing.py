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
    from decoy_engine.safe_output._exception import safe_error_type
    before = set(sys.modules)
    record = reason_from_exception(MemoryError())
    assert safe_error_type(MemoryError()).value == "MemoryError"
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


_REEXPORT_CHILD = textwrap.dedent(
    """
    import sys
    from decoy_engine.contracts._declared_base import DeclaredCastContractError
    from decoy_engine.execution.native.protocol.providers_base import DrawSiteProtocolError
    from decoy_engine.reason_adapter import reason_from_exception
    from decoy_engine.safe_output._exception import safe_error_type
    reexports = ("decoy_engine.contracts.declared_casting",
                 "decoy_engine.execution.native._draw_site_providers")
    from decoy_engine.profiling import scan_set
    # Package imports may load the re-export modules as a side effect; a live failure whose class
    # came from the defining module alone does not, so model that by dropping them.
    for mod in reexports:
        sys.modules.pop(mod, None)
    for cls in (DeclaredCastContractError, DrawSiteProtocolError):
        exc = cls.__new__(cls)
        record = reason_from_exception(exc)
        passes = scan_set.passes_through(exc)
        print(cls.__name__, record.code, passes, safe_error_type(exc).value)
    """
)


def test_a_family_defined_in_an_unreexported_module_still_classifies() -> None:
    """Only the defining module is imported (not the re-export); classification must not lose it."""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _REEXPORT_CHILD],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    rows = [line.split(" ") for line in proc.stdout.strip().splitlines()]
    assert [r[0] for r in rows] == ["DeclaredCastContractError", "DrawSiteProtocolError"]
    for name, code, passes, label in rows:
        assert code != "unexpected_error", name
        assert passes == "True", name
        assert label == name, name


def test_every_table_entry_names_its_defining_module() -> None:
    import importlib

    from decoy_engine import reason_adapter

    for module, name, _ in (*reason_adapter._CODED, *reason_adapter._FIXED):
        cls = getattr(importlib.import_module(module), name)
        assert cls.__module__ == module, (module, name, cls.__module__)
